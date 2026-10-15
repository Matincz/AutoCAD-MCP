"""Guarded T30 command sessions over AutoCAD's documented ActiveX API.

No keyboard/mouse automation and no arbitrary caller LISP. Interactive commands
are not database transactions: cancel ends the command but retains already drawn
objects. Completion and created-object verification are separate fields.
"""
from __future__ import annotations

import uuid
import ntpath
import math
import sys
import time
from functools import wraps

import structlog

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.com_sta import sta_async_method, com_hresult, RETRYABLE_HRESULTS
from autocad_mcp.tianzheng import ALIGNMENT_RECIPES, GLOBAL_DISPLAY_RECIPES, GLOBAL_LAYER_RECIPES, SELECTION_RECIPES, _curve_key, command_is_owned, compile_drawing_recipe, compile_global_recipe, compile_selection_recipe, encode_response, find_command, verify_alignment, verify_global_layers, verify_global_visibility, verify_selection_mutation
from autocad_mcp.tianzheng_properties import PROPERTY_RECIPES, property_assignments
from autocad_mcp.tianzheng_templates import TEMPLATE_RECIPES, COLUMN_SECTIONS
from autocad_mcp.tianzheng_transforms import TRANSFORM_RECIPES, DISTRIBUTION_RECIPES
from autocad_mcp.tianzheng import finite_number, TianzhengInputError
from autocad_mcp.tianzheng_native import native_recipe
from autocad_mcp.tianzheng import LAYER_SELECTION_RECIPES


NATIVE_WATCHDOG_SECONDS = 20.0
log = structlog.get_logger()


def _timed_read(method):
    """One record per STA read phase, including failures; no per-getter logging."""
    @wraps(method)
    def timed(*args, **kwargs):
        started = time.perf_counter()
        completed = False
        try:
            result = method(*args, **kwargs)
            completed = True
            return result
        finally:
            log.info(f"tianzheng.read.{method.__name__.removeprefix('_t30_')}", completed=completed,
                     duration_ms=round((time.perf_counter() - started) * 1000, 3))
    return timed


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in (sorted(value) if isinstance(value, set) else value)]
    return value


def _failure(code: str, message: str, **details) -> CommandResult:
    return CommandResult(ok=False, error_code=code, error=message, payload=details,
                         recoverable=False)


class TianzhengMixin:
    """Uses the FileIPC backend's PID-bound STA and canonical revision context."""

    def _t30_idempotent_call(self, callback):
        """Retry only reads or fixed-value setters, never Copy/Move/SendCommand.

        This handles only explicit COM call-rejected/server-retry-later replies.
        Timeouts, crashes and other errors are never retried.
        """
        for attempt in range(4):
            try:return callback()
            except Exception as error:
                if com_hresult(error) not in RETRYABLE_HRESULTS or attempt==3:raise
                self._com_executor.cooperative_sleep(0.05*(2**attempt))

    def _t30_read(self, callback):
        """Read-only COM access: also retry transient late-binding failures.

        Live T30 sessions intermittently raise AttributeError (e.g. Item.Color)
        on plain property reads. Only getters use this; writes never do.
        """
        for attempt in range(4):
            try:return self._t30_idempotent_call(callback)
            except AttributeError:
                if attempt==3:raise
                self._com_executor.cooperative_sleep(0.05*(2**attempt))

    def _t30_jobs(self) -> dict:
        if not hasattr(self, "_tianzheng_sessions"):
            self._tianzheng_sessions = {}
        return self._tianzheng_sessions

    def _t30_document(self, context: dict):
        application = self._get_autocad_application()
        document = application.ActiveDocument
        actual = str(document.Name)
        expected = str(context.get("document_name", ""))
        # Native Database.SaveAs may record a path without renaming DrawingN.dwg.
        # Match the canonical document name, never just a filename from the path.
        if not expected or ntpath.basename(actual).casefold() != ntpath.basename(expected).casefold():
            raise ValueError(f"E_DOCUMENT_ID_MISMATCH: active COM document {actual!r} differs from {expected!r}")
        if ntpath.isabs(expected):
            full_name = str(document.FullName)
            if ntpath.normcase(ntpath.normpath(full_name)) != ntpath.normcase(ntpath.normpath(expected)):
                raise ValueError("E_DOCUMENT_ID_MISMATCH: COM document path differs from the native context")
        return application, document

    @staticmethod
    def _t30_command_state(document) -> dict:
        return {"command_active": int(document.GetVariable("CMDACTIVE")),
                "command_names": str(document.GetVariable("CMDNAMES") or "")}

    @_timed_read
    def _t30_environment(self, document) -> dict:
        layers = {}
        read = self._t30_read
        collection = read(lambda:document.Layers)
        for index in range(int(read(lambda:collection.Count))):
            layer = read(lambda:collection.Item(index))
            layers[str(read(lambda:layer.Name))] = {"on": bool(read(lambda:layer.LayerOn)), "frozen": bool(read(lambda:layer.Freeze)),
                                      "locked": bool(read(lambda:layer.Lock)), "color": int(read(lambda:layer.Color)),
                                      "linetype": str(read(lambda:layer.Linetype))}
        return {"layers": layers, "current_layer": str(read(lambda:document.GetVariable("CLAYER")))}

    @_timed_read
    def _t30_snapshot(self, document) -> dict:
        space = self._t30_idempotent_call(lambda:document.ModelSpace)
        count = int(self._t30_idempotent_call(lambda:space.Count))
        if count > 20000:
            raise ValueError("E_TIANZHENG_SNAPSHOT_LIMIT: use a smaller isolated drawing for command verification")
        result = {}
        for index in range(count):
            entity = self._t30_read(lambda:space.Item(index))
            handle = str(self._t30_read(lambda:entity.Handle)).upper()
            # Optional properties: AttributeError means "not on this class"; never retry it.
            read = lambda name:self._t30_idempotent_call(lambda:getattr(entity,name))
            item = {"handle": handle}
            for name in ("ObjectName", "EntityName", "Layer"):
                try:
                    item[name] = str(read(name))
                except Exception:
                    pass  # Older/custom T30 COM wrappers lack ObjectName.
            native_class = item.get("ObjectName") or item.get("EntityName") or "unreadable_custom_class"
            # Observed ActiveX class names differ from the installed DXF names.
            # These mappings were independently checked against entget on
            # real T30 objects. Unknown custom classes remain unverified.
            item["native_class"] = native_class
            item["entity_type"] = {"TDBWALL": "TCH_WALL", "TDBOPENING": "TCH_OPENING",
                                   "TDBBANDWIN": "TCH_CORNER_WINDOW"}.get(native_class.upper(), native_class)
            if native_class=="unreadable_custom_class":
                item["geometry_unreadable"]=True
                result[handle]=item
                continue
            try:
                low, high = self._t30_read(lambda:entity.GetBoundingBox())
                item["bounds"] = {"min": list(low), "max": list(high)}
            except Exception:
                pass
            if native_class.upper() in {"TDBCOLUMN","TDBRECT"} and "bounds" not in item:
                item["geometry_unreadable"]=True
                result[handle]=item
                continue
            if native_class.upper() in {"TDBCOLUMN","TDBRECT"} and "bounds" in item and all(
                abs(item["bounds"]["max"][axis]-item["bounds"]["min"][axis]) < 1e-7 for axis in (0,1)):
                # Empty T30 constructors can expose a valid IDispatch yet crash
                # inside dimension getters. Do not inspect their shape properties.
                item["uninitialized_custom_geometry"] = True
                result[handle] = item
                continue
            for name in ("Length", "Width", "Height", "Thickness", "Deep"):
                try:
                    item[name.lower()] = float(read(name))
                except Exception:
                    pass
            for name, label in (("Coordinates", "coordinates"), ("StartPoint", "start_point"),
                                ("EndPoint", "end_point"), ("Center", "center"), ("Normal","normal")):
                try:
                    item[label] = [float(value) for value in read(name)]
                except Exception:
                    pass
            if native_class.upper()=="ACDBPOLYLINE" and "coordinates" in item:
                try:
                    item["bulges"]=[float(self._t30_idempotent_call(lambda:entity.GetBulge(vertex))) for vertex in range(len(item["coordinates"])//2)]
                except Exception:
                    pass
            if "TEXT" in native_class.upper():
                # AutoCAD text: TextString; T30 TDbText: Text.
                for name in ("TextString","Text"):
                    try:item["text"]=str(read(name));break
                    except Exception:pass
            for name, label in (("Radius", "radius"), ("Closed", "closed"), ("Visible", "visible")):
                try:
                    item[label] = read(name)
                except Exception:
                    pass
            for name,label in (("Elevation","elevation"),("Elev","elev"),("LeftWidth","left_width"),
                               ("RightWidth","right_width"),("TotalWidth","total_width"),("EndClose","end_closed"),("Color","color"),("Linetype","linetype"),
                               ("IOFeature","io_feature"),("IsExternal","is_external"),("Style","material"),
                               ("Insulate","insulate"),("InsuThick","insu_thick"),("LeftInsuThick","left_insu_thick"),("RightInsuThick","right_insu_thick")):
                try:
                    item[label] = read(name)
                except Exception:
                    pass
            if item["entity_type"] in {"TCH_OPENING", "TCH_CORNER_WINDOW"}:
                for name, label in (("GetKind", "opening_kind"), ("GetSubKind", "opening_subkind"), ("WinSill", "window_sill")):
                    try:
                        item[label] = read(name)
                    except Exception:
                        pass
                try:
                    codes, values = self._t30_idempotent_call(lambda:entity.GetXData(""))
                    item["xdata_handle_references"] = [str(v) for c,v in zip(codes or [],values or []) if c==1005]
                except Exception:
                    pass
            if native_class.upper()=="TDBSPACE":
                for name,label in (("UseArea","use_area"),("OccuArea","occu_area"),("Perimeter","perimeter"),("Name","room_name"),
                                   ("SpaceType","space_type"),("StuccoThickness","stucco_thickness"),("ShowOutLine","show_outline")):
                    try:item[label]=read(name)
                    except Exception:pass
            if native_class.upper()=="TDBSHEET":
                try:
                    item["title"]=str(read("Title"))
                    rows,cols=int(read("RowNum")),int(read("ColNum"))
                    item["rows"],item["cols"]=rows,cols
                    if 0<rows<=200 and 0<cols<=30:
                        # Text(Row, Column) is a parameterized property getter; read only inside RowNum x ColNum.
                        base=self._t30_sheet_index_base(entity)
                        item["cells"]=[[str(self._t30_idempotent_call(lambda:entity.Text(r+base,c+base))) for c in range(cols)] for r in range(rows)]
                except Exception as error:
                    item["sheet_read_error"]=str(error)[:200]
            if native_class.upper()=="TDBSYMBNORTHTHUMB":
                try:item["rotation"]=float(read("Rotation"))
                except Exception:pass
            if native_class.upper()=="TDBCOLUMN":
                for name,label in (("SectionShapeText","section_shape"),("Diameter","diameter"),("SideLength","side_length"),("Rotation","rotation")):
                    try:item[label]=read(name)
                    except Exception:pass
            result[handle] = item
        return result

    def _t30_sheet_index_base(self, entity) -> int:
        try:
            self._t30_idempotent_call(lambda:entity.Text(0,0))
            return 0
        except Exception:
            return 1

    def _t30_result(self, job: dict, document) -> CommandResult:
        state = self._t30_command_state(document)
        owned = command_is_owned(job["entry"], state["command_names"])
        if owned:
            job["seen_active"] = True
        after = self._t30_snapshot(document)
        created = [item for key, item in after.items() if key not in job["before"]]
        removed = sorted(set(job["before"]) - set(after))
        modified = [item for key, item in after.items()
                    if key in job["before"] and item != job["before"][key]]
        expected = job["entry"].get("expected_entity_types", [])
        verified = [item for item in created if any(
            typename.upper() == item["entity_type"].upper() for typename in expected)]
        command = job["entry"]["command"].upper()
        recipe_verified = None
        environment_after = None
        if command in ALIGNMENT_RECIPES and job.get("selection_handles"):
            recipe_verified = verify_alignment(command, job["selection_handles"], job["before"], after)
        elif command in SELECTION_RECIPES and job.get("selection_handles"):
            environment_after = self._t30_environment(document)
            recipe_verified = verify_selection_mutation(command, job["selection_handles"], job["before"], after, job["environment_before"], environment_after)
        elif command in GLOBAL_LAYER_RECIPES and job.get("automatic"):
            environment_after = self._t30_environment(document)
            recipe_verified = verify_global_layers(command, job["environment_before"], environment_after) and job["before"] == after
        elif command in GLOBAL_DISPLAY_RECIPES and job.get("automatic"):
            recipe_verified = verify_global_visibility(job["before"], after)
        if job.get("cancelled"):
            status = "cancelled" if not owned else "cancelling"
        elif owned:
            status = "awaiting_input"
        elif state["command_active"]:
            status = "blocked_by_other_command_or_dialog"
        elif job.get("sequence_completed") or job["seen_active"] or created or removed or modified:
            status = "completed"
        else:
            status = "no_effect_or_waiting_for_modeless_panel"
        payload = {"job_id": job["job_id"], "command": job["entry"]["command"],
                   "name": job["entry"]["name"], "status": status,
                   "completed": status == "completed", "command_sent": True,
                   "drawn": bool(created), "geometry_verified": recipe_verified if recipe_verified is not None else bool(verified) if expected else None,
                   "postcondition": job["entry"]["execution_contract"]["recipe"] or "manual_interaction",
                   "environment_before": job.get("environment_before"), "environment_after": environment_after,
                   "expected_entity_types": expected, "created_objects": created,
                   "modified_objects": modified, "removed_handles": removed,
                   "verified_objects": verified, "entity_count": len(after), **state,
                   "dialog_parameters": "existing_T30_defaults",
                   "atomic": False, "cancel_rolls_back": False,
                   "notes": job["entry"].get("notes", [])}
        job["last_result"] = payload
        return CommandResult(ok=True, payload=payload)

    @sta_async_method("tianzheng.inspect", idempotent=True, timeout=45)
    async def _tianzheng_inspect(self, context: dict) -> CommandResult:
        _, document = self._t30_document(context)
        state = self._t30_command_state(document)
        snapshot = self._t30_snapshot(document)
        return CommandResult(ok=True, payload={**state, "entity_count": len(snapshot),
            "entities": list(snapshot.values()), "execution_transport": "AutoCAD.ActiveX.SendCommand",
            "environment": self._t30_environment(document), "arbitrary_lisp": False})

    @sta_async_method("tianzheng.api_info", idempotent=True, timeout=10)
    async def _tianzheng_api_info(self, context: dict, selection_handles: list, library_type_indices: list | None = None) -> CommandResult:
        _, document = self._t30_document(context)
        state = self._t30_command_state(document)
        if state["command_active"]:
            return _failure("E_TIANZHENG_COMMAND_BUSY", "API metadata lookup requires an idle drawing")
        snapshot = self._t30_snapshot(document)
        if any(h.upper() not in snapshot for h in selection_handles):
            return _failure("E_ENTITY_NOT_FOUND", "API metadata handles must belong to current model space")
        objects, libraries, library_interfaces = [], {}, []
        def member_info(info):
            members = []
            for index in range(256):
                try:
                    descriptor = info.GetFuncDesc(index)
                except Exception:
                    break
                members.append({"names": list(info.GetNames(descriptor[0])),
                    "documentation": list(info.GetDocumentation(descriptor[0])),
                    "descriptor": repr(descriptor)})
            return members
        for handle in selection_handles:
            entity = document.HandleToObject(handle.upper())
            info = entity._oleobj_.GetTypeInfo()
            values = {}
            properties = () if snapshot[handle.upper()].get("uninitialized_custom_geometry") or snapshot[handle.upper()].get("geometry_unreadable") else ("Height", "Elevation", "LeftWidth", "RightWidth", "EndClose", "Style", "IOFeature", "IsExternal", "Insulate", "InsuThick", "Width", "Deep", "Length", "Elev", "BaseType", "GetKind", "GetSubKind")
            for prop in properties:
                try:
                    value = getattr(entity, prop)
                    if isinstance(value,(int,float,str,bool)):
                        values[prop] = value
                except Exception:
                    pass
            objects.append({"handle": handle.upper(), "native_class": snapshot[handle.upper()]["native_class"],
                            "interface": list(info.GetDocumentation(-1)), "members": member_info(info), "property_values": values})
            try:
                library, _ = info.GetContainingTypeLib()
                name = str(library.GetDocumentation(-1)[0])
                if name not in libraries:
                    libraries[name] = [{"index": i, "documentation": list(library.GetDocumentation(i))}
                                       for i in range(min(int(library.GetTypeInfoCount()), 1024))]
                    for index in library_type_indices or []:
                        if index >= int(library.GetTypeInfoCount()):
                            return _failure("E_TIANZHENG_PARAMETER_REJECTED", "Type index is outside this installed library")
                        type_info = library.GetTypeInfo(index)
                        library_interfaces.append({"library": name, "index": index,
                            "documentation": list(type_info.GetDocumentation(-1)),
                            "interface_guid": str(type_info.GetTypeAttr()[0]), "members": member_info(type_info)})
            except Exception:
                pass
        return CommandResult(ok=True, payload={"objects": objects, "type_libraries": libraries, "library_interfaces": library_interfaces,
                                               "executed_methods": False, "source": "installed_IDispatch_type_metadata"})

    def _t30_set_property(self, entity, prop: str, value):
        if isinstance(value,(list,tuple)) and sys.platform == "win32":
            import pythoncom
            from win32com.client import VARIANT
            value = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(value))
        self._t30_idempotent_call(lambda:setattr(entity,prop,value))

    def _t30_resolve_targets(self, context: dict, handles: list):
        _, document = self._t30_idempotent_call(lambda:self._t30_document(context))
        state = self._t30_idempotent_call(lambda:self._t30_command_state(document))
        if state["command_active"] or state["command_names"]:
            return _failure("E_TIANZHENG_COMMAND_BUSY", "Finish the active command before modifying properties", **state)
        for job in self._t30_jobs().values():
            if job["doc_id"] == context["doc_id"] and not job.get("released"):
                return _failure("E_TIANZHENG_JOB_BUSY", "Finish the existing T30 job first",job_id=job["job_id"])
        before = self._t30_snapshot(document)
        if any(h.upper() not in before for h in handles):
            return _failure("E_ENTITY_NOT_FOUND", "Property handles must belong to current model space")
        if any(before[h.upper()].get("uninitialized_custom_geometry") for h in handles):
            return _failure("E_TIANZHENG_TEMPLATE_UNINITIALIZED","Selection contains an uninitialized T30 shape; no shape getters or mutations were invoked")
        if any(before[h.upper()].get("geometry_unreadable") for h in handles):
            return _failure("E_TIANZHENG_GEOMETRY_UNREADABLE","Source class/bounds could not be read; no shape getters or mutations were invoked")
        return document, before, [(h.upper(),self._t30_idempotent_call(lambda:document.HandleToObject(h.upper()))) for h in handles]

    def _t30_apply_changes(self, document, before: dict, changes: list, verify_after=None) -> CommandResult:
        attempted = []
        try:
            # All old values are read before this method receives any changes.
            for handle,entity,prop,old,new in changes:
                attempted.append((handle,entity,prop,old))
                self._t30_set_property(entity,prop,new)
            for _,entity,_,_,_ in changes:
                self._t30_idempotent_call(lambda:entity.Update())
            for handle,entity,prop,_,expected in changes:
                actual = self._t30_idempotent_call(lambda:getattr(entity,prop))
                if isinstance(expected,(int,float)) and not isinstance(expected,bool):
                    matches = math.isclose(float(actual),expected,abs_tol=1e-6,rel_tol=0)
                elif isinstance(expected,(list,tuple)):
                    matches = len(actual)==len(expected) and all(math.isclose(float(a),b,abs_tol=1e-6,rel_tol=0) for a,b in zip(actual,expected))
                else:
                    matches = actual == expected
                if not matches:
                    raise ValueError(f"Property readback differs: {handle}.{prop}")
            after=self._t30_snapshot(document)
            if verify_after is not None and not verify_after(after):
                raise ValueError("Property postconditions did not match requested semantics")
        except Exception as error:
            rollback_errors = []
            for handle,entity,prop,old in reversed(attempted):
                try:
                    self._t30_set_property(entity,prop,old)
                    self._t30_idempotent_call(lambda:entity.Update())
                except Exception as rollback_error:
                    rollback_errors.append({"handle":handle,"property":prop,"error":str(rollback_error)})
            after = self._t30_snapshot(document)
            return _failure("E_TIANZHENG_PROPERTY_WRITE_FAILED",str(error),rollback_attempted=True,
                rollback_complete=not rollback_errors and before==after,rollback_errors=rollback_errors,atomic=False)
        return CommandResult(ok=True,payload={"completed":True,"command_sent":False,"drawn":False,
            "geometry_verified":True,"postcondition":"typed_property_readback_and_object_diff",
            "execution_transport":"AutoCAD.ActiveX.object_properties","atomic":False,
            "created_objects":[e for h,e in after.items() if h not in before],
            "modified_objects":[e for h,e in after.items() if h in before and e!=before[h]],
            "removed_handles":sorted(set(before)-set(after)),"entity_count":len(after),
            "applied_properties":[{"handle":h,"property":p,"previous":old,"actual":new} for h,_,p,old,new in changes]})

    @sta_async_method("tianzheng.properties",timeout=45)
    async def _tianzheng_properties(self, context: dict, command: str, handles: list, parameters: dict) -> CommandResult:
        resolved = self._t30_resolve_targets(context,handles)
        if isinstance(resolved,CommandResult):
            return resolved
        document,before,objects = resolved
        if command.upper() in {"TMARKINTWALL","TMARKEXTWALL"}:
            return self._t30_classify_walls(document,before,command,handles,parameters)
        assignments = property_assignments(command.upper(),parameters)
        changes = []
        for handle,entity in objects:
            kind = before[handle]["native_class"].upper()
            if command.upper() in {"TCHEWALLHEIGHT","TEXTTHICK"} and kind!="TDBWALL":
                return _failure("E_TIANZHENG_OBJECT_TYPE","Wall classification/exterior properties require initialized T30 walls")
            if command.upper() in {"TCHEWALLHEIGHT","TEXTTHICK"} and before[handle].get("is_external")!="外墙":
                return _failure("E_TIANZHENG_EXTERIOR_WALL_REQUIRED","Exterior wall changes require walls already recognized as exterior")
            if command.upper()=="TEXTTHICK":
                side=before[handle].get("io_feature")
                if side not in {"左外","右外"}:return _failure("E_TIANZHENG_EXTERIOR_SIDE_REQUIRED","Thickness needs one unambiguous exterior side")
                assignments={"LeftWidth":parameters["outside_width" if side=="左外" else "inside_width"],
                    "RightWidth":parameters["inside_width" if side=="左外" else "outside_width"]}
            if command.upper() == "TCHHEIGHT" and kind not in {"TDBWALL","TDBCOLUMN"}:
                return _failure("E_TIANZHENG_OBJECT_TYPE","Height recipe requires T30 walls or standard columns")
            if command.upper() in {"TWALLTHICK","TCHWALLTERM"} and kind != "TDBWALL":
                return _failure("E_TIANZHENG_OBJECT_TYPE","Wall parameter recipe requires T30 walls")
            if command.upper() == "TPELCHANGECOLOR" and kind in {"ACDBBLOCKREFERENCE","TDBMULTIVIEWINSERT"}:
                return _failure("E_TIANZHENG_ADAPTER_REQUIRED","Block-internal color handling requires a separate adapter")
            for prop,value in assignments.items():
                changes.append((handle,entity,prop,self._t30_idempotent_call(lambda:getattr(entity,prop)),value))
        created_layer = None
        if "layer" in parameters:
            try:
                document.Layers.Item(parameters["layer"])
            except Exception:
                created_layer = document.Layers.Add(parameters["layer"])
        verify = None
        if command.upper() in {"TCHEWALLHEIGHT","TEXTTHICK"}:
            selected = {h for h,_ in objects}
            verify = lambda after: set(before)==set(after) and all(
                after[h].get("is_external")=="外墙" and after[h].get("io_feature")==before[h].get("io_feature")
                if h in selected else before[h]==after[h] for h in before)
        result = self._t30_apply_changes(document,before,changes,verify_after=verify)
        if not result.ok and created_layer is not None:
            try:
                created_layer.Delete()
            except Exception as error:
                result.payload["rollback_complete"] = False
                result.payload["layer_cleanup_error"] = str(error)
        result.payload["command"] = command
        result.payload["command_invoked"] = False
        return result

    @sta_async_method("tianzheng.set_opening",timeout=45)
    async def _tianzheng_set_opening(self, context: dict, data: dict) -> CommandResult:
        resolved = self._t30_resolve_targets(context,data["selection_handles"])
        if isinstance(resolved,CommandResult):return resolved
        document,before,objects = resolved
        handle,entity = objects[0]
        source=before[handle]
        if source["native_class"].upper()!="TDBOPENING" or source.get("opening_subkind") not in {"普通门","普通窗"}:
            return _failure("E_TIANZHENG_OBJECT_TYPE","Only initialized ordinary doors/windows are supported")
        p=data["parameters"]
        kind,subkind=("门","普通门") if p["kind"]=="door" else ("窗","普通窗")
        if source.get("opening_kind")!=kind or source.get("opening_subkind")!=subkind:
            return _failure("E_TIANZHENG_OPENING_KIND_REQUIRED","GetSubKind does not convert a door into a window; supply an initialized opening of the requested kind")
        assignments={"Width":p["width"],"Height":p["height"],"WinSill":p["sill"]}
        changes=[(handle,entity,prop,getattr(entity,prop),value) for prop,value in assignments.items()]
        def verify(after):
            return set(before)==set(after) and after[handle].get("opening_kind")==kind and after[handle].get("opening_subkind")==subkind and all(
                before[h]==after[h] for h in before if h!=handle)
        result=self._t30_apply_changes(document,before,changes,verify_after=verify)
        result.payload.update(command="TOpening",command_invoked=False,
            postcondition="ordinary_opening_kind_dimensions_and_unselected_objects_preserved",
            host_association_verified=False,all_modes_verified=False)
        return result

    def _t30_classify_walls(self, document, before: dict, command: str, handles: list, parameters: dict) -> CommandResult:
        # IsExternal is read-only. IOFeature alone cannot perform classification.
        from autocad_mcp.tianzheng_properties import validate_property_recipe
        entry = find_command(command)
        parameters = validate_property_recipe(entry,handles,parameters)
        selected = {h.upper() for h in handles}
        if any(before[h]["native_class"].upper()!="TDBWALL" for h in selected):
            return _failure("E_TIANZHENG_OBJECT_TYPE","Classification requires initialized T30 walls")
        if int(document.GetVariable("WORLDUCS"))!=1 or abs(float(document.GetVariable("VIEWTWIST")))>1e-7:
            return _failure("E_TIANZHENG_UCS_UNSUPPORTED","Classification requires untwisted world coordinates")
        exterior = command.upper()=="TMARKEXTWALL"
        if exterior:
            handle = handles[0].upper()
            point = parameters["pick_point"]
            bounds = before[handle].get("bounds")
            if not bounds or any(not bounds["min"][i]-1e-6<=point[i]<=bounds["max"][i]+1e-6 for i in (0,1)):
                return _failure("E_TIANZHENG_PARAMETER_REJECTED","Wall-skin pick point must be within the selected wall's XY bounds")
            # T30 asks for a skin position, not an AutoCAD entity/pick pair.
            token = encode_response(entry,{"type":"point","value":point})
            sequence = f'TMarkExtWall\n{token}\n'
        else:
            token = encode_response(entry,{"type":"selection","value":handles}).strip()
            sequence = f'TMarkIntWall\n{token}\n\n\n'
        details = {"command":entry["command"],"command_invoked":True,"atomic":False,
            "rollback_attempted":False,"rollback_complete":False,
            "execution_transport":"AutoCAD.ActiveX.SendCommand.fixed_classification_recipe"}
        try:
            document.SendCommand(sequence)
        except Exception as error:
            return _failure("E_TIANZHENG_SEND_UNCERTAIN",str(error),**details,
                command_may_have_started=True,retry_command=False,next_action="inspect_actual_interface_before_any_further_write")
        try:
            details["last_prompt"] = str(document.GetVariable("LASTPROMPT"))
            after = self._t30_snapshot(document)
            state = self._t30_command_state(document)
        except Exception as error:
            return _failure("E_TIANZHENG_RESULT_UNAVAILABLE",str(error),**details,
                retry_command=False,next_action="inspect_actual_interface_before_any_further_write")
        expected = "外墙" if exterior else "内墙"
        side = {"left":"左外","right":"右外"}.get(parameters.get("outside"),"双内")
        preserved = set(before)==set(after) and all(
            ({k:v for k,v in before[h].items() if k not in {"is_external","io_feature"}} ==
             {k:v for k,v in after[h].items() if k not in {"is_external","io_feature"}})
            if h in selected else before[h]==after[h] for h in before)
        verified = preserved and not state["command_active"] and not state["command_names"] and all(
            after[h].get("is_external")==expected and after[h].get("io_feature")==side for h in selected)
        details.update(completed=not state["command_active"] and not state["command_names"],
            geometry_verified=verified,postcondition="classification_and_side_with_geometry_and_unselected_objects_preserved",
            before_objects=[before[h] for h in selected],
            modified_objects=[e for h,e in after.items() if h in before and e!=before[h]],
            created_objects=[e for h,e in after.items() if h not in before],
            removed_handles=sorted(set(before)-set(after)),entity_count=len(after),**state)
        if not verified:
            return _failure("E_TIANZHENG_POSTCONDITION_NOT_MET","Native wall classification did not meet classification, side or preservation checks",
                **details,retry_command=False,next_action="inspect_before_recovery",drawing_state="changed" if before!=after else "unchanged")
        return CommandResult(ok=True,payload=details)

    def _t30_top_view(self, document) -> dict:
        """World XY window of the current untwisted top view and pick-box size."""
        if int(document.GetVariable("WORLDUCS"))!=1 or abs(float(document.GetVariable("VIEWTWIST")))>1e-7 or any(
                abs(float(a)-b)>1e-7 for a,b in zip(document.GetVariable("VIEWDIR"),[0,0,1])):
            raise TianzhengInputError("Point-picked wall recipes require an untwisted world top view")
        centre = [float(v) for v in document.GetVariable("VIEWCTR")]
        height = float(document.GetVariable("VIEWSIZE"))
        screen = [float(v) for v in document.GetVariable("SCREENSIZE")]
        width = height*screen[0]/screen[1]
        # Twice the pick box half-size as a safety margin for other objects.
        margin = 2*float(document.GetVariable("PICKBOX"))*height/screen[1]
        return {"min":[centre[0]-width/2,centre[1]-height/2],"max":[centre[0]+width/2,centre[1]+height/2],"pick_margin":margin}

    @sta_async_method("tianzheng.native_recipe",timeout=45)
    async def _tianzheng_native(self, context: dict, data: dict) -> CommandResult:
        from autocad_mcp.tianzheng_native import native_recipe
        recipe = native_recipe(data["command"])
        resolved = self._t30_resolve_targets(context,data.get("selection_handles") or [])
        if isinstance(resolved,CommandResult):return resolved
        document,before,_ = resolved
        entry = find_command(data["command"])
        try:
            plan = recipe.plan(before,data,lambda:self._t30_top_view(document))
            sequence = recipe.sequence(entry,data,plan)
        except TianzhengInputError as error:
            return _failure("E_TIANZHENG_PRECONDITION_FAILED",str(error),command_sent=False)
        verify, postcondition = recipe.verify, recipe.postcondition
        details = {"parameters":data.get("parameters"),"plan":_jsonable(plan)}
        if recipe.environment:
            plan["environment_before"] = self._t30_environment(document)
        details.update({"command":entry["command"],"command_invoked":True,"atomic":False,"rollback_attempted":False,
            "execution_transport":"AutoCAD.ActiveX.SendCommand.fixed_native_recipe","recipe":recipe.label})
        # Watchdog: a recipe whose prompt flow was misjudged leaves T30 waiting and
        # SendCommand blocked. Before the STA timeout, cancel via the same guarded
        # out-of-band ESC (never re-sends input) and report the run as incomplete.
        import threading
        watchdog = {"fired": False}
        def fire():
            watchdog["fired"] = True
            try:watchdog["escape"] = self._t30_post_escape(context["document_name"]).to_dict()
            except Exception as error:watchdog["escape"] = {"ok": False, "error": str(error)}
        timer = threading.Timer(NATIVE_WATCHDOG_SECONDS, fire) if sys.platform == "win32" else None
        if timer:timer.daemon=True;timer.start()
        try:
            document.SendCommand(sequence)
        except Exception as error:
            if timer:timer.cancel()
            return _failure("E_TIANZHENG_SEND_UNCERTAIN",str(error),**details,watchdog_escape=watchdog.get("escape"),
                command_may_have_started=True,retry_command=False,next_action="inspect_actual_interface_before_any_further_write")
        if timer:timer.cancel()
        if watchdog["fired"]:
            details["watchdog_escape"] = watchdog.get("escape")
        try:
            self._com_executor.cooperative_sleep(0.3)
            state = self._t30_command_state(document)
            details["last_prompt"] = str(document.GetVariable("LASTPROMPT"))
            if command_is_owned(entry,state["command_names"]):
                # A missed pick leaves T30 waiting at its own prompt; end only that command.
                document.SendCommand("\x1b\x1b")
                self._com_executor.cooperative_sleep(0.2)
                details["cancelled_own_prompt"] = True
                state = self._t30_command_state(document)
            after = self._t30_snapshot(document)
        except Exception as error:
            return _failure("E_TIANZHENG_RESULT_UNAVAILABLE",str(error),**details,
                retry_command=False,next_action="inspect_actual_interface_before_any_further_write")
        if recipe.environment:
            plan["environment_after"] = self._t30_environment(document)
        failures = verify(plan,before,after)
        if watchdog["fired"]:
            failures.append("command_waited_for_input_watchdog_escaped")
        if state["command_active"] or state["command_names"]:
            failures.append("command_still_active")
        details.update(completed=not state["command_active"] and not state["command_names"] and not details.get("cancelled_own_prompt"),
            geometry_verified=not failures,postcondition=postcondition,
            failed_checks=failures,before_objects=[before[h] for h in data.get("selection_handles") or []],
            created_objects=[e for h,e in after.items() if h not in before],
            modified_objects=[e for h,e in after.items() if h in before and e!=before[h]],
            removed_handles=sorted(set(before)-set(after)),entity_count=len(after),**state)
        if failures:
            return _failure("E_TIANZHENG_POSTCONDITION_NOT_MET","Native command result did not meet the recipe postconditions",
                **details,retry_command=False,next_action="inspect_before_recovery",drawing_state="changed" if before!=after else "unchanged")
        return CommandResult(ok=True,payload=details)

    @sta_async_method("tianzheng.project_elevation",timeout=45)
    async def _tianzheng_project_elevation(self, context: dict, handles: list) -> CommandResult:
        resolved = self._t30_resolve_targets(context,handles)
        if isinstance(resolved,CommandResult):
            return resolved
        document,before,objects = resolved
        changes = []
        for handle,entity in objects:
            kind = before[handle]["native_class"].upper()
            if kind == "ACDBLINE":
                for prop in ("StartPoint","EndPoint"):
                    old = list(getattr(entity,prop))
                    changes.append((handle,entity,prop,old,[*old[:2],0.0]))
            elif kind in {"ACDBCIRCLE","ACDBARC"}:
                normal = list(entity.Normal)
                if any(abs(normal[i])>1e-6 for i in (0,1)):
                    return _failure("E_TIANZHENG_OBJECT_TYPE","Projection of tilted circles/arcs needs a separate curve adapter")
                old = list(entity.Center)
                changes.append((handle,entity,"Center",old,[*old[:2],0.0]))
            elif kind == "ACDBPOLYLINE":
                changes.append((handle,entity,"Elevation",entity.Elevation,0.0))
            elif kind in {"TDBWALL","TDBCOLUMN"}:
                changes.append((handle,entity,"Elevation",entity.Elevation,0.0))
            else:
                return _failure("E_TIANZHENG_OBJECT_TYPE","Elevation projection supports native line/circle/arc/LWPolyline and T30 wall/column")
        result = self._t30_apply_changes(document,before,changes)
        result.payload.update(command="TModElev",command_invoked=False,postcondition="world_XOY_projection_preserving_XY")
        if result.ok:
            after = self._t30_snapshot(document)
            if any(abs(after[h]["bounds"]["min"][2])>1e-6 or abs(after[h]["bounds"]["max"][2])>1e-6 for h,_ in objects if before[h]["native_class"].upper().startswith("ACDB")):
                return _failure("E_TIANZHENG_POSTCONDITION_NOT_MET","Elevation readback did not project native objects onto XOY",**result.payload)
        return result

    @sta_async_method("tianzheng.template_create",timeout=45)
    async def _tianzheng_template_create(self, context: dict, data: dict) -> CommandResult:
        command = data["command"].upper()
        spec = TEMPLATE_RECIPES[command]
        resolved = self._t30_resolve_targets(context,[data["template_handle"]])
        if isinstance(resolved,CommandResult):
            return resolved
        document,before,objects = resolved
        handle,template = objects[0]
        source = before[handle]
        if source["native_class"].upper() != spec["native_class"].upper():
            return _failure("E_TIANZHENG_OBJECT_TYPE","Template has the wrong initialized T30 class")
        bounds = source.get("bounds",{})
        if not bounds or any(bounds["max"][axis]-bounds["min"][axis]<=1e-7 for axis in (0,1)):
            return _failure("E_TIANZHENG_TEMPLATE_UNINITIALIZED","Template must have initialized nonzero geometry before reading shape parameters")
        numeric = {name:float(getattr(template,prop)) for name,prop in spec["numeric_properties"].items()}
        x_size = numeric["width"] if command == "TGCOLUMN" else numeric["length"]
        y_size = numeric["depth"] if command == "TGCOLUMN" else numeric["width"]
        if x_size<=0 or y_size<=0 or not bounds or not math.isclose(bounds["max"][0]-bounds["min"][0],x_size,abs_tol=1e-5,rel_tol=0) or not math.isclose(bounds["max"][1]-bounds["min"][1],y_size,abs_tol=1e-5,rel_tol=0):
            return _failure("E_TIANZHENG_TEMPLATE_UNINITIALIZED","Template must have nonzero, axis-aligned geometry matching its dimensions; empty factories and annotated/rotated prototypes are rejected")
        if command == "TGCOLUMN" and str(template.SectionShapeText) != "矩形":
            return _failure("E_TIANZHENG_OBJECT_TYPE","Column template recipe currently requires rectangular section")
        section = COLUMN_SECTIONS.get(data["parameters"].get("shape")) if command=="TGCOLUMN" else None
        properties = {section[1]:section[2],"height":"Height"} if section else spec["numeric_properties"]
        parameters = {k:v for k,v in data["parameters"].items() if k!="shape"}
        created = []
        try:
            for point in data["points"]:
                entity = template.Copy()
                created.append(entity)
                if section:
                    self._t30_set_property(entity,"SectionShapeText",section[0])
                    self._t30_set_property(entity,"Rotation",0.0)
                for name,value in parameters.items():
                    self._t30_set_property(entity,properties[name],value)
                entity.Update()
                lower,upper = entity.GetBoundingBox()
                anchor = [(lower[0]+upper[0])/2,(lower[1]+upper[1])/2,lower[2]]
                # T30 plan-view columns keep their footprint in the XY plane.
                # Elevation is a semantic property, independent of those bounds.
                destination = [point[0],point[1],anchor[2]]
                if sys.platform == "win32":
                    import pythoncom
                    from win32com.client import VARIANT
                    entity.Move(VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,tuple(anchor)),VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,tuple(destination)))
                else:
                    entity.Move(anchor,destination)
                elevation_property = "Elevation" if command=="TGCOLUMN" else "Elev"
                self._t30_set_property(entity,elevation_property,point[2])
                entity.Update()
                lower,upper=entity.GetBoundingBox()
                expected_x=parameters[section[1]]*section[3] if section else parameters["width"] if command=="TGCOLUMN" else parameters["length"]
                expected_y=parameters[section[1]]*section[4] if section else parameters["depth"] if command=="TGCOLUMN" else parameters["width"]
                actual=[(lower[0]+upper[0])/2,(lower[1]+upper[1])/2,float(getattr(entity,elevation_property)),upper[0]-lower[0],upper[1]-lower[1]]
                expected=[*point,expected_x,expected_y]
                if any(not math.isclose(a,b,abs_tol=1e-5,rel_tol=0) for a,b in zip(actual,expected)):
                    raise ValueError(f"Clone bounds do not match placement and requested dimensions: actual={actual}, expected={expected}")
                # T30 Rotation is degrees and may report 359.9999999999999 for zero.
                if section and (str(entity.SectionShapeText)!=section[0] or not math.isclose(math.remainder(float(entity.Rotation),360.0),0.0,abs_tol=1e-7)):
                    raise ValueError(f"Column section or rotation differs: shape={entity.SectionShapeText!r}, rotation={entity.Rotation!r}, expected_shape={section[0]!r}")
                for name,value in parameters.items():
                    if not math.isclose(float(getattr(entity,properties[name])),value,abs_tol=1e-6,rel_tol=0):
                        raise ValueError("Template parameter readback differs")
            after=self._t30_snapshot(document)
            if after.get(handle)!=before[handle]:
                raise ValueError("Original initialized template changed during copy")
        except Exception as error:
            cleanup_errors=[]
            for entity in reversed(created):
                try:entity.Delete()
                except Exception as cleanup_error:cleanup_errors.append(str(cleanup_error))
            return _failure("E_TIANZHENG_TEMPLATE_CREATE_FAILED",str(error),cleanup_errors=cleanup_errors,
                rollback_complete=not cleanup_errors and self._t30_snapshot(document)==before,atomic=False)
        objects=[after[str(entity.Handle).upper()] for entity in created]
        return CommandResult(ok=True,payload={"command":data["command"],"command_invoked":False,"command_sent":False,
            "completed":True,"drawn":True,"geometry_verified":True,"execution_transport":"AutoCAD.ActiveX.initialized_template_copy",
            "postcondition":"class_dimensions_placement_and_unchanged_template","created_objects":objects,
            "template_handle":handle,"template_unchanged":True,"entity_count":len(after),"atomic":False})

    @sta_async_method("tianzheng.measure",idempotent=True,timeout=45)
    async def _tianzheng_measure(self,context,data):
        resolved=self._t30_resolve_targets(context,data["selection_handles"])
        if isinstance(resolved,CommandResult):return resolved
        document,before,objects=resolved
        command=data["command"].upper()
        if command=="TGETEXTEND":
            if any("bounds" not in before[h] for h,_ in objects):
                return _failure("E_TIANZHENG_GEOMETRY_UNREADABLE","Every measured object needs readable world bounds")
            lower=[min(before[h]["bounds"]["min"][axis] for h,_ in objects) for axis in range(3)]
            upper=[max(before[h]["bounds"]["max"][axis] for h,_ in objects) for axis in range(3)]
            for value in lower+upper:finite_number(value)
            measured={"bounds":{"min":lower,"max":upper},"size":[b-a for a,b in zip(lower,upper)],"bounds_semantics":"ActiveX_current_object_bounds"}
        else:
            lengths=[]
            for handle,entity in objects:
                source=before[handle];kind=source["native_class"].upper()
                if kind=="ACDBLINE":
                    value=math.dist(source["start_point"],source["end_point"])
                elif kind=="ACDBCIRCLE":value=2*math.pi*float(source["radius"])
                elif kind=="ACDBPOLYLINE":value=float(self._t30_idempotent_call(lambda:entity.Length))
                elif kind=="ACDBARC":value=float(self._t30_idempotent_call(lambda:entity.ArcLength))
                else:return _failure("E_TIANZHENG_OBJECT_TYPE","Length measurement supports native lines, circles, arcs and lightweight polylines")
                finite_number(value)
                lengths.append({"handle":handle,"native_class":source["native_class"],"length":value})
            measured={"lengths":lengths,"total_length":sum(item["length"] for item in lengths)}
        after=self._t30_snapshot(document)
        if before!=after:return _failure("E_TIANZHENG_READ_CONTEXT_CHANGED","Drawing objects changed during read-only measurement")
        return CommandResult(ok=True,payload={"command":data["command"],"command_invoked":False,"completed":True,
            "measurement_verified":True,"writes_drawing":False,"units":"drawing_units","count":len(objects),
            "execution_transport":"AutoCAD.ActiveX.read_geometry",**measured})

    @sta_async_method("tianzheng.layer_selection",timeout=45)
    async def _tianzheng_layer_selection(self,context,data):
        resolved = self._t30_resolve_targets(context,data["selection_handles"])
        if isinstance(resolved,CommandResult):return resolved
        document,before,objects = resolved
        environment = self._t30_idempotent_call(lambda:self._t30_environment(document))
        command = data["command"].upper()
        field,value,other = LAYER_SELECTION_RECIPES[command]
        selected_layers = {before[h]["Layer"] for h,_ in objects}
        names = [name for name in environment["layers"] if (name not in selected_layers if other else name in selected_layers)]
        if field=="frozen" and value and environment["current_layer"] in names:
            return _failure("E_TIANZHENG_CURRENT_LAYER","Cannot freeze the current layer; retain an unfrozen current layer first")
        prop = "LayerOn" if field=="on" else "Freeze"
        attempted = []
        try:
            for name in names:
                layer = self._t30_idempotent_call(lambda:document.Layers.Item(name))
                attempted.append((layer,environment["layers"][name][field]))
                self._t30_idempotent_call(lambda:setattr(layer,prop,value))
            after = self._t30_snapshot(document)
            new_environment = self._t30_idempotent_call(lambda:self._t30_environment(document))
            if not verify_selection_mutation(command,data["selection_handles"],before,after,environment,new_environment):
                raise ValueError("Layer state, preserved flags or drawing objects did not match")
        except Exception as error:
            failures=[]
            for layer,old in reversed(attempted):
                try:self._t30_idempotent_call(lambda:setattr(layer,prop,old))
                except Exception as cleanup:failures.append(str(cleanup))
            return _failure("E_TIANZHENG_LAYER_WRITE_FAILED",str(error),rollback_complete=not failures and self._t30_snapshot(document)==before and self._t30_environment(document)==environment,rollback_errors=failures,atomic=False)
        return CommandResult(ok=True,payload={"command":data["command"],"command_invoked":False,"command_sent":False,
            "completed":True,"drawn":False,"geometry_verified":True,"atomic":False,
            "execution_transport":"AutoCAD.ActiveX.layer_properties","postcondition":"layer_state_and_preserved_objects",
            "changed_layers":names,"environment_before":environment,"environment_after":new_environment,
            "created_objects":[],"modified_objects":[],"removed_handles":[],"entity_count":len(after)})

    @staticmethod
    def _t30_move(entity, vector):
        origin = [0.0,0.0,0.0]
        if sys.platform == "win32":
            import pythoncom
            from win32com.client import VARIANT
            origin = VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,tuple(origin))
            vector = VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_R8,tuple(vector))
        entity.Move(origin,vector)

    @sta_async_method("tianzheng.translate",timeout=45)
    async def _tianzheng_translate(self, context, data):
        resolved = self._t30_resolve_targets(context,data["selection_handles"])
        if isinstance(resolved,CommandResult): return resolved
        document,before,objects = resolved
        allowed = {"ACDBLINE","ACDBCIRCLE","ACDBARC","ACDBPOLYLINE","TDBWALL","TDBCOLUMN","TDBRECT"}
        if any(before[h]["native_class"].upper() not in allowed for h,_ in objects):
            return _failure("E_TIANZHENG_OBJECT_TYPE","Translation supports bounded native curves and initialized T30 wall/column/rectangle objects")
        if any("bounds" not in before[h] for h,_ in objects):
            return _failure("E_TIANZHENG_GEOMETRY_UNREADABLE","Source bounds could not be read; no copy or move was invoked",handles=[h for h,_ in objects if "bounds" not in before[h]])
        copy_mode = data["command"].upper()=="TDRAGCOPY"
        distribution = data["command"].upper() in DISTRIBUTION_RECIPES
        if distribution:
            if int(document.GetVariable("WORLDUCS"))!=1 or abs(float(document.GetVariable("VIEWTWIST")))>1e-7 or any(abs(float(a)-b)>1e-7 for a,b in zip(document.GetVariable("VIEWDIR"),[0,0,1])):
                return _failure("E_TIANZHENG_VIEW_UNSUPPORTED","Equal edge spacing currently requires an untwisted world top view")
            axis = DISTRIBUTION_RECIPES[data["command"].upper()]
            ordered = sorted(objects,key=lambda item:(before[item[0]]["bounds"]["min"][axis],int(item[0],16)))
            position = before[ordered[0][0]]["bounds"]["min"][axis]
            offsets={}
            for handle,_ in ordered:
                bounds=before[handle]["bounds"]
                vector=[0.0,0.0,0.0];vector[axis]=position-bounds["min"][axis]
                finite_number(vector[axis]);offsets[handle]=vector
                position+=bounds["max"][axis]-bounds["min"][axis]+data["parameters"]["spacing"]
        else:
            offsets={h:data["parameters"]["offset"] for h,_ in objects}
        attempted = []
        try:
            for handle,source in objects:
                vector=offsets[handle]
                entity = source.Copy() if copy_mode else source
                custom = before[handle]["native_class"].upper().startswith("TDB")
                prop = "Elev" if before[handle]["native_class"].upper()=="TDBRECT" else "Elevation"
                old_elevation = float(getattr(entity,prop)) if custom else None
                displacement = [*vector[:2],0.0 if custom else vector[2]]
                record = [handle,entity,displacement,prop,old_elevation,False]
                attempted.append(record)
                self._t30_move(entity,displacement)
                record[5] = True
                if custom: self._t30_set_property(entity,prop,old_elevation+vector[2])
                entity.Update()
            after = self._t30_snapshot(document)
            for handle,entity,displacement,prop,elevation,_ in attempted:
                vector=offsets[handle]
                target = after[str(entity.Handle).upper()]
                geometry_offset = [*vector[:2],0.0 if before[handle]["native_class"].upper() in {"TDBCOLUMN","TDBRECT"} else vector[2]]
                for bound in ("min","max"):
                    expected = [value+geometry_offset[i] for i,value in enumerate(before[handle]["bounds"][bound])]
                    if any(not math.isclose(a,b,abs_tol=1e-5,rel_tol=0) for a,b in zip(target["bounds"][bound],expected)):
                        raise ValueError("Translated bounds do not match requested world offset")
                if elevation is not None and not math.isclose(float(getattr(entity,prop)),elevation+vector[2],abs_tol=1e-6,rel_tol=0):
                    raise ValueError("T30 semantic elevation did not match requested offset")
                for key in ("native_class","Layer","length","width","deep","height","radius","color","linetype"):
                    if key in before[handle] and target.get(key)!=before[handle][key]:
                        if isinstance(before[handle][key],(int,float)) and math.isclose(float(target.get(key)),before[handle][key],abs_tol=1e-5,rel_tol=0): continue
                        raise ValueError(f"Translation changed preserved property {key}")
            selected = {h for h,_ in objects}
            if any(after.get(h)!=old for h,old in before.items() if copy_mode or h not in selected):
                raise ValueError("Translation unexpectedly changed an unselected/source object")
            if len(after)!=len(before)+(len(objects) if copy_mode else 0):
                raise ValueError("Translation produced an unexpected entity count")
            if distribution:
                for left,right in zip(ordered,ordered[1:]):
                    gap=after[right[0]]["bounds"]["min"][axis]-after[left[0]]["bounds"]["max"][axis]
                    if not math.isclose(gap,data["parameters"]["spacing"],abs_tol=1e-5,rel_tol=0):raise ValueError("Actual edge spacing differs from requested spacing")
        except Exception as error:
            failures = []
            for _,entity,displacement,prop,elevation,moved in reversed(attempted):
                try:
                    if copy_mode: entity.Delete()
                    else:
                        if moved: self._t30_move(entity,[-v for v in displacement])
                        if elevation is not None: self._t30_set_property(entity,prop,elevation)
                        entity.Update()
                except Exception as cleanup: failures.append(str(cleanup))
            return _failure("E_TIANZHENG_TRANSLATE_FAILED",str(error),rollback_complete=not failures and self._t30_snapshot(document)==before,rollback_errors=failures,atomic=False)
        return CommandResult(ok=True,payload={"command":data["command"],"command_invoked":False,"command_sent":False,
            "completed":True,"drawn":copy_mode,"geometry_verified":True,"atomic":False,
            "execution_transport":"AutoCAD.ActiveX.Copy.Move" if copy_mode else "AutoCAD.ActiveX.Move",
            "postcondition":"world_offset_bounds_elevation_count_and_preserved_objects",
            **({"spacing":data["parameters"]["spacing"],"axis":axis,"spacing_mode":"edge_gap","anchor_handle":ordered[0][0]} if distribution else {"offset":data["parameters"]["offset"]}),
            "created_objects":[e for h,e in after.items() if h not in before],
            "modified_objects":[e for h,e in after.items() if h in before and e!=before[h]],
            "removed_handles":[],"entity_count":len(after)})

    @sta_async_method("tianzheng.start", timeout=45)
    async def _tianzheng_start(self, context: dict, command: str, points: list | None = None, wall_handles: list | None = None, selection_handles: list | None = None, automatic: bool = False) -> CommandResult:
        entry = find_command(command)
        if entry["command"].upper() == "TBANWIN":
            entry["expected_entity_types"] = ["TCH_OPENING", "TCH_CORNER_WINDOW"]
        if entry["automation"] != "interactive":
            return _failure("E_TIANZHENG_ADAPTER_REQUIRED",
                "This command needs a verified workspace-aware adapter before it may execute", command=entry)
        _, document = self._t30_document(context)
        sequence = (compile_selection_recipe(entry, selection_handles) if selection_handles is not None else
                    compile_drawing_recipe(entry, points, wall_handles) if points is not None else
                    compile_global_recipe(entry) if automatic else None)
        if sequence is not None and int(document.GetVariable("WORLDUCS")) != 1:
            return _failure("E_TIANZHENG_UCS_UNSUPPORTED", "Drawing recipes currently require the world coordinate system")
        state = self._t30_command_state(document)
        if state["command_active"] or state["command_names"]:
            return _failure("E_TIANZHENG_COMMAND_BUSY", "AutoCAD is already executing a command; no command was cancelled", **state)
        if wall_handles:
            objects = self._t30_snapshot(document)
            if any(objects.get(handle.upper(), {}).get("entity_type") != "TCH_WALL" for handle in wall_handles):
                return _failure("E_TIANZHENG_OBJECT_TYPE", "Band window handles must reference existing T30 walls")
        if selection_handles:
            objects = self._t30_snapshot(document)
            if any(handle.upper() not in objects or "bounds" not in objects[handle.upper()] for handle in selection_handles):
                return _failure("E_ENTITY_NOT_FOUND", "Alignment needs existing model-space objects with readable bounds")
            selected = [objects[handle.upper()] for handle in selection_handles]
            if entry["command"].upper() in {"TFREEZELAYER","TFREEZEOTHERLAYER"}:
                current_layer = self._t30_environment(document)["current_layer"]
                selected_layers = {item["Layer"] for item in selected}
                affects_current = current_layer not in selected_layers if entry["command"].upper()=="TFREEZEOTHERLAYER" else current_layer in selected_layers
                if affects_current:
                    return _failure("E_TIANZHENG_CURRENT_LAYER","Cannot freeze the current layer; select/set an unfrozen retained layer first")
            if entry["command"].upper() == "TREVPLINE" and any(
                item["native_class"].upper() not in {"ACDBPOLYLINE", "ACDB2DPOLYLINE", "ACDB3DPOLYLINE"}
                or item.get("closed", True) or "coordinates" not in item for item in selected):
                return _failure("E_TIANZHENG_OBJECT_TYPE", "Reverse recipe currently requires open native polylines with readable vertices")
            if entry["command"].upper() == "TREMOVEDUP":
                for item in selected:
                    _curve_key(item)
            if entry["command"].upper() == "TMODELEV" and any(item["native_class"].upper() not in {"ACDBLINE", "ACDBPOLYLINE", "ACDBCIRCLE", "ACDBARC"} for item in selected):
                return _failure("E_TIANZHENG_OBJECT_TYPE", "Elevation recipe currently supports native lines, circles, arcs and lightweight polylines")
        # A previous job whose state is unknown may still be in a modeless panel.
        for prior in self._t30_jobs().values():
            if prior["doc_id"] == context["doc_id"] and not prior.get("released"):
                return _failure("E_TIANZHENG_JOB_BUSY", "Finish or cancel the existing T30 job before starting another", job_id=prior["job_id"])
        job_id = "t30-" + uuid.uuid4().hex
        job = {"job_id": job_id, "doc_id": context["doc_id"], "entry": entry,
               "document_name": context["document_name"], "before": self._t30_snapshot(document),
               "seen_active": False, "cancelled": False, "released": False,
               "selection_handles": selection_handles, "automatic": automatic,
               "environment_before": self._t30_environment(document) if entry["command"].upper() in SELECTION_RECIPES or automatic and entry["command"].upper() in GLOBAL_LAYER_RECIPES else None}
        self._t30_jobs()[job_id] = job
        try:
            if sequence is None:
                document.PostCommand(entry["command"] + "\n")
            else:
                document.SendCommand(sequence)
        except Exception as error:
            # Delivery may have occurred before COM failed. Keep the job and
            # refuse automatic retries of this non-idempotent command.
            return _failure("E_TIANZHENG_SEND_UNCERTAIN", str(error), job_id=job_id,
                            command_may_have_started=True)
        if sequence is None:
            # PostCommand can immediately enter a selection prompt. AutoCAD then
            # rejects synchronous COM reads, so returning metadata must not read it.
            payload = {"job_id": job_id, "command": entry["command"], "name": entry["name"],
                       "status": "queued_for_manual_interaction", "command_sent": True,
                       "completed": False, "drawn": False, "geometry_verified": None,
                       "manual_interaction_required": True, "state_verified": False,
                       "atomic": False, "cancel_rolls_back": False}
            job["last_result"] = payload
            return CommandResult(ok=True, payload=payload)
        job["sequence_completed"] = True
        self._com_executor.cooperative_sleep(0.3)
        try:
            result = self._t30_result(job, document)
        except Exception as error:
            # A command may be delivered yet still block the result query.
            # Retain its identity and never convert this into a successful draw.
            return _failure("E_TIANZHENG_RESULT_UNAVAILABLE", str(error), job_id=job_id,
                            command_sent=True, completed=False, geometry_verified=False,
                            retry_command=False, next_action="inspect_after_command_finishes")
        if result.payload["completed"]:
            job["released"] = True
        if sequence is not None and (not result.payload["completed"] or not result.payload["geometry_verified"]):
            return _failure("E_TIANZHENG_POSTCONDITION_NOT_MET",
                "Drawing recipe did not complete with independently verified T30 objects; inspect before retrying",
                **result.payload)
        return result

    def _t30_job(self, context: dict, job_id: str) -> dict:
        job = self._t30_jobs().get(job_id)
        if not job or job["doc_id"] != context["doc_id"]:
            raise ValueError("E_TIANZHENG_JOB_MISMATCH: job does not belong to the active document")
        if job["document_name"] != context["document_name"]:
            raise ValueError("E_DOCUMENT_ID_MISMATCH: job document identity changed")
        return job

    @sta_async_method("tianzheng.status", idempotent=True, timeout=45)
    async def _tianzheng_status(self, context: dict, job_id: str) -> CommandResult:
        job = self._t30_job(context, job_id)
        _, document = self._t30_document(context)
        result = self._t30_result(job, document)
        if result.payload["status"] in ("completed", "cancelled"):
            job["released"] = True
        return result

    @sta_async_method("tianzheng.input", timeout=45)
    async def _tianzheng_input(self, context: dict, job_id: str, response: dict) -> CommandResult:
        job = self._t30_job(context, job_id)
        encoded = encode_response(job["entry"], response)
        application, document = self._t30_document(context)
        state = self._t30_command_state(document)
        if job["cancelled"] or job["released"] or not command_is_owned(job["entry"], state["command_names"]):
            return _failure("E_TIANZHENG_PROMPT_NOT_OWNED", "The requested T30 command is not waiting for input; nothing was sent", **state)
        if float(str(application.Version).split('s')[0]) >= 20:
            return _failure("E_TIANZHENG_BATCH_REQUIRED", "AutoCAD 2015+ waits for SendCommand completion; use execute with a complete drawing recipe", **state)
        if response["type"] == "selection":
            handles = self._t30_snapshot(document)
            if any(h.upper() not in handles for h in response["value"]):
                return _failure("E_ENTITY_NOT_FOUND", "Selection includes a missing or non-model-space handle; nothing was sent")
        try:
            document.SendCommand(encoded)
        except Exception as error:
            return _failure("E_TIANZHENG_SEND_UNCERTAIN", str(error), job_id=job_id,
                            input_may_have_been_consumed=True)
        self._com_executor.cooperative_sleep(0.2)
        result = self._t30_result(job, document)
        if result.payload["completed"]:
            job["released"] = True
        return result

    @sta_async_method("tianzheng.cancel", timeout=45)
    async def _tianzheng_cancel(self, context: dict, job_id: str) -> CommandResult:
        job = self._t30_job(context, job_id)
        _, document = self._t30_document(context)
        state = self._t30_command_state(document)
        if command_is_owned(job["entry"], state["command_names"]):
            document.SendCommand("\x1b\x1b")
            self._com_executor.cooperative_sleep(0.2)
        elif state["command_active"]:
            return _failure("E_TIANZHENG_PROMPT_NOT_OWNED", "Refusing to cancel another command", **state)
        job["cancelled"] = True
        job["released"] = True
        return self._t30_result(job, document)

    def tianzheng_escape(self, document_name: str) -> CommandResult:
        """Post two ESC key messages to the PID-bound command window (no COM)."""
        result = self._t30_post_escape(document_name)
        if result.ok:
            name = result.payload["drawing"]
            released=[]
            for job in self._t30_jobs().values():
                if not job.get("released") and ntpath.basename(str(job.get("document_name",""))).casefold()==name.casefold():
                    job["cancelled"]=job["released"]=True;released.append(job["job_id"])
            result.payload["released_jobs"]=released
        return result

    def _t30_post_escape(self, document_name: str) -> CommandResult:
        from autocad_mcp.tianzheng_escape import escape_guard
        from autocad_mcp.workspace import output_root
        if sys.platform != "win32":
            return _failure("E_UNSUPPORTED_OPERATION","escape needs the Windows AutoCAD host")
        import ctypes, time
        import win32gui
        from autocad_mcp.backends.file_ipc import find_autocad_window, _window_process_id
        expected_pid = getattr(self,"_acad_process_id",None)
        hwnd = find_autocad_window(expected_pid) if expected_pid else None
        if not hwnd or _window_process_id(hwnd) != expected_pid:
            return _failure("E_AUTOCAD_INSTANCE_AMBIGUOUS","No AutoCAD window bound to the expected process; nothing posted")
        title = str(win32gui.GetWindowText(hwnd) or "")
        try:
            name = escape_guard(document_name,title,str(output_root()/"drawings"))
        except TianzhengInputError as error:
            return _failure("E_TIANZHENG_ESCAPE_REFUSED",str(error),window_title=title,posted=False)
        target = None
        def find_mdi(child,_):
            nonlocal target
            if win32gui.GetClassName(child)=="MDIClient":
                target=child
                return False
            return True
        try:win32gui.EnumChildWindows(hwnd,find_mdi,None)
        except Exception:pass
        target = target or hwnd
        post = ctypes.windll.user32.PostMessageW
        for _ in range(2):
            post(target,0x0100,0x1B,0)
            post(target,0x0101,0x1B,0)
            time.sleep(0.1)
        return CommandResult(ok=True,payload={"posted":"ESC x2","target_hwnd":int(target),"process_id":expected_pid,
            "window_title":title,"drawing":name,"com_used":False,
            "next_action":"ensure_ready_then_inspect_to_confirm_command_inactive"})

    async def tianzheng_command(self, operation: str, context: dict, data: dict) -> CommandResult:
        if operation=="measure":return await self._tianzheng_measure(context,data)
        if operation=="set_opening":return await self._tianzheng_set_opening(context,data)
        if operation == "execute" and native_recipe(data["command"]) is not None:
            return await self._tianzheng_native(context,data)
        if operation == "execute" and data["command"].upper() in LAYER_SELECTION_RECIPES:
            return await self._tianzheng_layer_selection(context,data)
        if operation == "execute" and data["command"].upper() in TRANSFORM_RECIPES:
            return await self._tianzheng_translate(context,data)
        if operation == "execute" and "template_handle" in data:
            return await self._tianzheng_template_create(context,data)
        if operation == "execute" and data["command"].upper() in PROPERTY_RECIPES:
            return await self._tianzheng_properties(context,data["command"],data["selection_handles"],data["parameters"])
        if operation == "execute" and data["command"].upper() == "TMODELEV":
            return await self._tianzheng_project_elevation(context,data["selection_handles"])
        if operation == "factory_probe":
            return _failure("E_TIANZHENG_FACTORY_DISABLED", "Uninitialized custom-object factories are disabled; use verified commands or initialized templates")
        if operation == "api_info":
            return await self._tianzheng_api_info(context, data["selection_handles"], data.get("library_type_indices"))
        if operation == "jobs":
            return CommandResult(ok=True, payload={"jobs": [
                {"job_id": job["job_id"], "command": job["entry"]["command"],
                 "released": job["released"], "last_result": job.get("last_result"),
                 "state_verified": False} for job in self._t30_jobs().values() if job["doc_id"] == context["doc_id"]],
                 "source": "server_session_metadata", "live_cad_state_queried": False})
        if operation == "registry":
            before = await self._tianzheng_inspect(context)
            if before.payload["command_active"]:
                return _failure("E_TIANZHENG_COMMAND_BUSY", "Registry lookup requires an idle test drawing")
            entries = data["entries"]
            return await self._dispatch("t30-command-lookup", {"names":"|".join(entry["command"] for entry in entries)})
        methods = {"inspect": self._tianzheng_inspect, "start": self._tianzheng_start,
                   "execute": self._tianzheng_start,
                   "status": self._tianzheng_status, "input": self._tianzheng_input,
                   "cancel": self._tianzheng_cancel}
        if operation not in methods:
            return _failure("E_UNSUPPORTED_OPERATION", "Unknown Tianzheng operation")
        if operation == "inspect":
            return await methods[operation](context)
        if operation == "start":
            return await methods[operation](context, data["command"])
        if operation == "execute":
            return await methods[operation](context, data["command"], data.get("points"), data.get("wall_handles"), data.get("selection_handles"), automatic=True)
        if operation == "input":
            return await methods[operation](context, data["job_id"], data["response"])
        return await methods[operation](context, data["job_id"])
