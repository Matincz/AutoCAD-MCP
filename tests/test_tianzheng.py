from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from autocad_mcp import server
from autocad_mcp.backends.base import CommandResult
from autocad_mcp.backends.tianzheng import TianzhengMixin
from autocad_mcp.tianzheng import (
    TianzhengInputError, command_catalogue, command_is_owned, compile_drawing_recipe, encode_response,
    find_command, list_commands,
)


def test_catalogue_preserves_scope_and_evidence():
    entries = command_catalogue()["commands"]
    assert len(entries) >= 453
    assert len({entry["command"].upper() for entry in entries}) == len(entries)
    assert sum(entry["meaning_evidence"] in {"manual-index","manual-body-summary","manual-section-and-installed-alias"} for entry in entries) >= 411
    assert all(not entry["verified_live"] for entry in entries)
    assert list_commands("墙", limit=200)["total"] > 20
    assert find_command("HZQT")["command"].upper() == "TGWALL"


@pytest.mark.parametrize("value", ["TGWall\n_ERASE", "(command \"erase\")", "_.LINE", "", "绘制轴网"])
def test_rejects_unknown_injected_and_ambiguous_commands(value):
    with pytest.raises(TianzhengInputError):
        find_command(value)


@pytest.mark.parametrize("response", [
    {"type": "point", "value": [True, 0]},
    {"type": "point", "value": [float("nan"), 0]},
    {"type": "number", "value": "200\nERASE"},
    {"type": "keyword", "value": "C\nERASE"},
    {"type": "selection", "value": ['AB\") (command "erase")']},
    {"type": "text", "value": "ERASE"},
    {"type": "enter", "macro": "ERASE"},
])
def test_prompt_responses_cannot_carry_commands_or_code(response):
    with pytest.raises(TianzhengInputError):
        encode_response(find_command("TGWall"), response)


def test_typed_responses_and_owned_prompt():
    wall = find_command("TGWall")
    assert encode_response(wall, {"type": "point", "value": [1, 2, 0]}) == "1,2,0\n"
    assert encode_response(wall, {"type": "selection", "value": ["ab"]}) == '(ssadd (handent "AB") (ssadd))\n'
    assert command_is_owned(wall, "TGWALL")
    assert not command_is_owned(wall, "LINE")


class Space:
    def __init__(self): self.items = []
    @property
    def Count(self): return len(self.items)
    def Item(self, index): return self.items[index]


class Document:
    Name = "Drawing2.dwg"
    def __init__(self):
        self.ModelSpace = Space()
        self.command = ""
        self.sent = []
        self.points = []
    def GetVariable(self, name):
        if name == "WORLDUCS": return 1
        return self.command if name == "CMDNAMES" else int(bool(self.command))
    def PostCommand(self, text): self.SendCommand(text)
    def SendCommand(self, text):
        self.sent.append(text)
        if text.count("\n") > 1:
            for part in text.splitlines(): self.SendCommand(part+"\n")
            return
        if text.strip().upper() == "TGWALL": self.command = "TGWALL"
        elif text == "\x1b\x1b" or text == "\n": self.command = ""
        elif "," in text:
            self.points.append(text)
            if len(self.points) >= 2:
                entity = SimpleNamespace(Handle=f"{len(self.ModelSpace.items)+1:X}", ObjectName="TCH_WALL", Layer="WALL")
                self.ModelSpace.items.append(entity)


class Runtime(TianzhengMixin):
    name = "file_ipc"
    def __init__(self):
        self.document = Document()
        self._com_executor = SimpleNamespace(in_executor_thread=True, cooperative_sleep=lambda _: None)
        self.context = {"doc_id": "test-doc", "active_doc_id": "test-doc", "revision": 3,
                        "document_name": "Drawing2.dwg"}
        self.guard_calls = []
    def _get_autocad_application(self): return SimpleNamespace(ActiveDocument=self.document, Version="19.1")
    async def document_context(self): return CommandResult(ok=True, payload=dict(self.context))
    async def record_document_mutation(self, doc_id): return await self.document_context()
    async def require_document_context(self, doc_id, revision, **kwargs):
        self.guard_calls.append((doc_id, revision, kwargs))
        if doc_id != self.context["doc_id"] or revision != self.context["revision"]:
            return CommandResult(ok=False, error="stale", error_code="E_DOCUMENT_REVISION_MISMATCH")
        return await self.document_context()


@pytest.mark.asyncio
async def test_lifecycle_verifies_custom_wall_without_claiming_send_is_drawn():
    runtime = Runtime()
    started = await runtime._tianzheng_start(runtime.context, "TGWall")
    assert started.payload["status"] == "queued_for_manual_interaction"
    assert not started.payload["drawn"] and not started.payload["completed"]
    job = started.payload["job_id"]
    first = await runtime._tianzheng_input(runtime.context, job, {"type": "point", "value": [0, 0]})
    assert not first.payload["drawn"]
    second = await runtime._tianzheng_input(runtime.context, job, {"type": "point", "value": [6000, 0]})
    assert second.payload["drawn"] and second.payload["geometry_verified"]
    assert not second.payload["completed"]
    ended = await runtime._tianzheng_input(runtime.context, job, {"type": "enter"})
    assert ended.payload["completed"]
    assert not ended.payload["atomic"] and not ended.payload["cancel_rolls_back"]


@pytest.mark.asyncio
async def test_switched_document_and_other_command_are_never_written_or_cancelled():
    runtime = Runtime()
    start = await runtime._tianzheng_start(runtime.context, "TGWall")
    job = start.payload["job_id"]
    runtime.document.command = "ERASE"
    sent = list(runtime.document.sent)
    result = await runtime._tianzheng_cancel(runtime.context, job)
    assert not result.ok and result.error_code == "E_TIANZHENG_PROMPT_NOT_OWNED"
    assert runtime.document.sent == sent
    runtime.document.Name = "user-project.dwg"
    with pytest.raises(ValueError, match="E_DOCUMENT_ID_MISMATCH"):
        await runtime._tianzheng_input(runtime.context, job, {"type": "point", "value": [1, 2]})
    assert runtime.document.sent == sent


@pytest.mark.asyncio
async def test_missing_selection_does_not_send_expression():
    runtime = Runtime()
    start = await runtime._tianzheng_start(runtime.context, "TGWall")
    result = await runtime._tianzheng_input(runtime.context, start.payload["job_id"], {"type": "selection", "value": ["BAD"]})
    assert not result.ok and result.error_code == "E_ENTITY_NOT_FOUND"
    assert len(runtime.document.sent) == 1


@pytest.mark.asyncio
async def test_server_rejects_stale_revision_and_replays_mutation(monkeypatch, tmp_path):
    runtime = Runtime()
    async def backend(): return runtime
    monkeypatch.setattr(server, "get_backend", backend)
    monkeypatch.setenv("AUTOCAD_MCP_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "_journal", None)
    result = await server.tianzheng(operation="start", doc_id="test-doc", expected_revision=2,
        idempotency_key="stale-wall", data={"command":"TGWall"})
    value = json.loads(result) if isinstance(result, str) else result.structuredContent
    assert value["error"]["code"] == "E_DOCUMENT_REVISION_MISMATCH"
    assert runtime.document.sent == []
    params = dict(operation="start", doc_id="test-doc", expected_revision=3,
                  idempotency_key="one-wall", data={"command":"TGWall"})
    await server.tianzheng(**params)
    await server.tianzheng(**params)
    assert runtime.document.sent == ["TgWall\n"]


@pytest.mark.asyncio
async def test_opening_parameters_require_fresh_context_and_verify_category(monkeypatch,tmp_path):
    runtime=Runtime()
    opening=SimpleNamespace(Handle="1",ObjectName="TDbOpening",Layer="WINDOW",
        GetKind="门",GetSubKind="普通门",Width=900.0,Height=2100.0,WinSill=0.0,
        GetBoundingBox=lambda:([0,0,0],[900,240,0]),Update=lambda:None)
    runtime.document.ModelSpace.items=[opening]
    runtime.document.HandleToObject=lambda _:opening
    async def backend():return runtime
    monkeypatch.setattr(server,"get_backend",backend)
    monkeypatch.setenv("AUTOCAD_MCP_OUTPUT_ROOT",str(tmp_path))
    monkeypatch.setattr(server,"_journal",None)
    data={"selection_handles":["1"],"parameters":{"kind":"window","width":1350,"height":1450,"sill":850}}
    async def request(key,revision):
        result=await server.tianzheng(operation="set_opening",doc_id="test-doc",expected_revision=revision,
            idempotency_key=key,data=data)
        return json.loads(result) if isinstance(result,str) else result.structuredContent
    rejected=await request("stale-opening",2)
    assert rejected["error"]["code"]=="E_DOCUMENT_REVISION_MISMATCH"
    assert opening.Width==900
    failed=await request("category-not-changed",3)
    assert not failed["ok"] and failed["error"]["code"]=="E_TIANZHENG_OPENING_KIND_REQUIRED"
    assert (opening.GetSubKind,opening.Width,opening.Height,opening.WinSill)==("普通门",900,2100,0)
    data["parameters"]["kind"]="door"
    passed=await request("door-dimensions",3)
    assert passed["ok"] and not passed["payload"]["host_association_verified"]
    assert (opening.Width,opening.Height,opening.WinSill)==(1350,1450,850)


@pytest.mark.asyncio
async def test_export_is_catalogued_but_not_sent_and_catalogue_is_offline(monkeypatch):
    async def backend(): raise AssertionError("No backend access permitted")
    monkeypatch.setattr(server, "get_backend", backend)
    result = await server.tianzheng(operation="catalogue", data={"query":"墙"})
    value = json.loads(result) if isinstance(result, str) else result.structuredContent
    assert value["ok"]
    result = await server.tianzheng(operation="start", data={"command":"TSaveAs"})
    value = json.loads(result) if isinstance(result, str) else result.structuredContent
    assert value["error"]["code"] == "E_TIANZHENG_ADAPTER_REQUIRED"


def test_versioned_com_attachment_without_generic_progid(monkeypatch):
    from autocad_mcp.backends import file_ipc
    calls = []
    app = SimpleNamespace(HWND=200)
    def active(progid):
        calls.append(progid)
        if progid == "AutoCAD.Application.25": return app
        raise RuntimeError("invalid class string")
    client = SimpleNamespace(GetActiveObject=active)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(file_ipc, "_window_process_id", lambda _: 123)
    backend = file_ipc.FileIPCBackend()
    backend._acad_process_id = 123
    assert backend._get_autocad_application() is app
    assert calls == ["AutoCAD.Application", "AutoCAD.Application.25"]


def test_versioned_fallback_never_attaches_wrong_cad_process(monkeypatch):
    from autocad_mcp.backends import file_ipc
    client = SimpleNamespace(GetActiveObject=lambda _: SimpleNamespace(HWND=200))
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(file_ipc, "_window_process_id", lambda _: 999)
    backend = file_ipc.FileIPCBackend()
    backend._acad_process_id = 123
    with pytest.raises(RuntimeError, match="no running COM ProgID matched"):
        backend._get_autocad_application()


@pytest.mark.asyncio
async def test_complete_recipe_does_not_leave_a_pending_wall_command():
    runtime = Runtime()
    result = await runtime._tianzheng_start(runtime.context, "TGWall", [[0,0],[6000,0]])
    assert result.payload["completed"] and result.payload["geometry_verified"]
    assert runtime.document.command == ""
    assert runtime.document.sent[0] == "TgWall\n0,0\n6000,0\n\n\n"


@pytest.mark.asyncio
async def test_modern_autocad_rejects_split_input_before_com_send(monkeypatch):
    runtime = Runtime()
    start = await runtime._tianzheng_start(runtime.context, "TGWall")
    monkeypatch.setattr(runtime, "_get_autocad_application", lambda: SimpleNamespace(ActiveDocument=runtime.document,Version="25.0s"))
    sent = list(runtime.document.sent)
    result = await runtime._tianzheng_input(runtime.context, start.payload["job_id"], {"type":"point","value":[0,0]})
    assert result.error_code == "E_TIANZHENG_BATCH_REQUIRED"
    assert runtime.document.sent == sent


def test_complete_recipe_is_not_a_raw_command_batch():
    wall = find_command("TGWall")
    with pytest.raises(TianzhengInputError): compile_drawing_recipe(wall, [["ERASE",0],[1,1]])
    with pytest.raises(TianzhengInputError): compile_drawing_recipe(find_command("TWallThick"), [[0,0]])
    with pytest.raises(TianzhengInputError): compile_drawing_recipe(find_command("TGColumn"), [[0,0]])


def test_real_t30_activex_classes_are_canonicalized_for_readback():
    document = Document()
    document.ModelSpace.items = [SimpleNamespace(Handle="101", ObjectName="TDbWall", Layer="WALL"),
                                 SimpleNamespace(Handle="179", ObjectName="TDbOpening", Layer="WINDOW")]
    result = Runtime()._t30_snapshot(document)
    assert result["101"]["entity_type"] == "TCH_WALL"
    assert result["179"]["entity_type"] == "TCH_OPENING"
    assert result["101"]["native_class"] == "TDbWall"


def test_band_window_recipe_uses_only_validated_existing_handles():
    entry = find_command("TBanWin")
    code = compile_drawing_recipe(entry, [[4000,10000],[6000,10000]], ["100"])
    assert code == 'TBanWin\n4000,10000\n6000,10000\n(ssadd (handent "100") (ssadd))\n\n'
    with pytest.raises(TianzhengInputError):
        compile_drawing_recipe(entry, [[1,2],[3,4]], ['100") (command "erase")'])
    with pytest.raises(TianzhengInputError):
        compile_drawing_recipe(entry, [[1,2],[3,4]])


@pytest.mark.asyncio
async def test_band_window_recipe_refuses_non_wall_selection_before_send():
    runtime = Runtime()
    runtime.document.ModelSpace.items=[SimpleNamespace(Handle="100",ObjectName="AcDbLine",Layer="0")]
    result = await runtime._tianzheng_start(runtime.context,"TBanWin",[[1,2],[3,4]],["100"])
    assert result.error_code == "E_TIANZHENG_OBJECT_TYPE"
    assert runtime.document.sent == []


@pytest.mark.asyncio
async def test_postcommand_does_not_read_com_after_entering_selection_prompt():
    runtime = Runtime()
    def post(text):
        runtime.document.sent.append(text)
        def busy(*args): raise AssertionError("CAD rejected synchronous COM during selection")
        runtime.document.GetVariable = busy
    runtime.document.PostCommand = post
    result = await runtime._tianzheng_start(runtime.context, "ALXLEFT")
    assert result.ok and result.payload["manual_interaction_required"]
    assert not result.payload["state_verified"] and not result.payload["completed"]
    metadata = await runtime.tianzheng_command("jobs", runtime.context, {})
    assert metadata.payload["jobs"][0]["job_id"] == result.payload["job_id"]
    assert not metadata.payload["live_cad_state_queried"]


def test_alignment_recipe_and_independent_geometry_verification():
    from autocad_mcp.tianzheng import compile_selection_recipe, verify_alignment
    assert compile_selection_recipe(find_command("ALXLEFT"), ["1", "2"]).endswith('(ssadd (handent "2") (ssadd (handent "1") (ssadd)))\n\n')
    for handles in [["1"], ["a", "A"], ["1", []], ["1", '2") (command "erase")']]:
        with pytest.raises(TianzhengInputError): compile_selection_recipe(find_command("ALXLEFT"), handles)
    before = {"1": {"bounds": {"min":[0,0,0], "max":[10,10,0]}},
              "2": {"bounds": {"min":[20,30,0], "max":[40,50,0]}}}
    after = {"1": before["1"], "2": {"bounds": {"min":[0,30,0], "max":[20,50,0]}}}
    assert verify_alignment("ALXLEFT", ["1","2"], before, after)
    assert not verify_alignment("ALXLEFT", ["1","2"], before, before)
    after["2"]["bounds"]["max"][1] = 60
    assert not verify_alignment("ALXLEFT", ["1","2"], before, after)


@pytest.mark.asyncio
async def test_alignment_missing_handle_is_rejected_before_send():
    runtime = Runtime()
    result = await runtime._tianzheng_start(runtime.context, "ALXLEFT", selection_handles=["1","2"])
    assert result.error_code == "E_ENTITY_NOT_FOUND"
    assert runtime.document.sent == []


@pytest.mark.asyncio
async def test_every_unadapted_catalogue_command_fails_before_backend_access(monkeypatch):
    async def backend(): raise AssertionError("No CAD access before a complete recipe exists")
    monkeypatch.setattr(server, "get_backend", backend)
    supported = {"TGWALL", "TOPENING", "TBANWIN", "ALXLEFT", "ALXRIGHT", "ALXMID", "ALYTOP", "ALYBOTTOM", "ALYMID", "TOPENALLLAYER", "TUNFREEZEALLLAYER", "TSWITCHALLLAYER", "TREVPLINE", "TREMOVEDUP", "TLOCKLAYER", "TLOCKOTHERLAYER", "TUNLOCKLAYER", "TMODELEV", "TMKHIDE", "TLOCVIS", "TUNHIDE", "TUNHIDEALL", "TCHHEIGHT", "TWALLTHICK", "TCHWALLTERM", "TGCHANGELAYER", "TPELCHANGECOLOR"}
    supported.update({"TGCOLUMN", "TRECT"})
    supported.update({"TMOVE", "TDRAGMOVE", "TDRAGCOPY"})
    supported.update({"HORIDISTRIBUTION","VERTDISTRIBUTION"})
    supported.update({"TGETEXTEND","TLENGTHSUMM"})
    supported.update({"TMARKINTWALL","TMARKEXTWALL","TCHEWALLHEIGHT","TEXTTHICK"})
    supported.update({"TOFFLAYER", "TOFFOTHERLAYER", "TFREEZELAYER", "TFREEZEOTHERLAYER"})
    supported.update({"TONLAYER", "TUNFREEZELAYER"})
    supported.update({"TFILLET","TCHAMFER","TKATANA","TOFFSET","TCONVERTCURTAIN","TMARKWALL","TMIRWINIO","TDIMWALL","TNORTHTHUMB","TSTATOP","TALIGNCOLU","TBREAKDIM","TADDINSULATE","TDIM3","TAXISDOTE","TCLIPAXIS","TDIMMP","TUPDSPACE","TSPAREA","TCOUNTAREA","TPLUSTEXT"})
    for entry in command_catalogue()["commands"]:
        description = find_command(entry["command"])
        assert description["execution_contract"]["automated_recipe_available"] == (entry["command"].upper() in supported)
        if entry["command"].upper() in supported:
            continue
        # Test routing/parameter contracts independently of the outer per-minute
        # transport limit, which has its own coverage and remains unchanged.
        result = await server.tianzheng.__wrapped__(operation="execute", data={"command":entry["command"], "points":[[0,0],[1,1]]})
        value = json.loads(result) if isinstance(result, str) else result.structuredContent
        assert not value["ok"]
        expected = "E_TIANZHENG_RECIPE_UNAVAILABLE" if entry["automation"] == "interactive" else "E_TIANZHENG_ADAPTER_REQUIRED"
        assert value["error"]["code"] == expected


@pytest.mark.asyncio
async def test_result_query_failure_preserves_job_identity_and_never_claims_drawn():
    runtime = Runtime()
    def unreadable(*args): raise RuntimeError("result query rejected")
    runtime._t30_result = unreadable
    result = await runtime._tianzheng_start(runtime.context, "TGWall", [[0,0],[100,0]])
    assert result.error_code == "E_TIANZHENG_RESULT_UNAVAILABLE"
    assert result.payload["job_id"] in runtime._t30_jobs()
    assert result.payload["command_sent"] and not result.payload["geometry_verified"]
    assert not result.payload["retry_command"]


def test_layer_verification_preserves_other_flags_and_matches_selected_scope():
    from autocad_mcp.tianzheng import verify_global_layers, verify_selection_mutation
    import copy
    old = {"current_layer":"0", "layers": {"0":{"on":True,"frozen":False,"locked":False,"color":7},
                                          "test":{"on":False,"frozen":True,"locked":True,"color":1}}}
    new = copy.deepcopy(old);new["layers"]["test"]["on"] = True
    assert verify_global_layers("TOPENALLLAYER",old,new)
    new["layers"]["test"]["locked"] = False
    assert not verify_global_layers("TOPENALLLAYER",old,new)
    new = copy.deepcopy(old);new["layers"]["test"]["locked"] = False
    objects = {"1":{"Layer":"test"}}
    assert verify_selection_mutation("TUNLOCKLAYER",["1"],objects,objects,old,new)
    new["layers"]["0"]["locked"] = True
    assert not verify_selection_mutation("TUNLOCKLAYER",["1"],objects,objects,old,new)


def test_curve_reverse_and_duplicate_removal_have_geometry_postconditions():
    from autocad_mcp.tianzheng import verify_selection_mutation
    poly = {"1":{"native_class":"AcDbPolyline","coordinates":[0,0,100,0,100,100],"closed":False,"Layer":"0","bounds":{"min":[0,0,0],"max":[100,100,0]}}}
    after = {"1":dict(poly["1"],coordinates=[100,100,100,0,0,0])}
    assert verify_selection_mutation("TREVPLINE",["1"],poly,after,{}, {})
    assert not verify_selection_mutation("TREVPLINE",["1"],poly,poly,{}, {})
    line = {"native_class":"AcDbLine","Layer":"0","start_point":[0,0,0],"end_point":[100,0,0]}
    before = {"1":line,"2":dict(line,start_point=[100,0,0],end_point=[0,0,0])}
    assert verify_selection_mutation("TREMOVEDUP",["1","2"],before,{"1":line},{},{})
    assert not verify_selection_mutation("TREMOVEDUP",["1","2"],before,before,{},{})


def test_visibility_requires_unchanged_geometry_and_elevation_preserves_xy():
    from autocad_mcp.tianzheng import verify_global_visibility, verify_selection_mutation
    before = {"1":{"native_class":"AcDbLine","Layer":"0","visible":True,"start_point":[0,0,100],"end_point":[100,0,100],"bounds":{"min":[0,0,100],"max":[100,0,100]}}}
    hidden = {"1":dict(before["1"],visible=False)}
    assert verify_selection_mutation("TMKHIDE",["1"],before,hidden,{},{})
    assert not verify_selection_mutation("TMKHIDE",["1"],before,before,{},{})
    assert verify_global_visibility(hidden,before)
    flat = {"1":dict(before["1"],start_point=[0,0,0],end_point=[100,0,0],bounds={"min":[0,0,0],"max":[100,0,0]})}
    assert verify_selection_mutation("TMODELEV",["1"],before,flat,{},{})
    flat["1"]["end_point"][0] = 200
    assert not verify_selection_mutation("TMODELEV",["1"],before,flat,{},{})


def test_typed_property_parameters_reject_code_wrong_types_and_unknown_fields():
    from autocad_mcp.tianzheng_properties import validate_property_recipe, property_assignments
    assert validate_property_recipe(find_command("TChHeight"),["1"],{"height":3500}) == {"height":3500.0}
    assert property_assignments("TWALLTHICK",{"width":400}) == {"LeftWidth":200,"RightWidth":200}
    for command,parameters in [("TChHeight",{"height":True}),("TWallThick",{"width":-1}),
        ("TChHeight",{"Elevation;Delete":0}),("TGChangeLayer",{"layer":"a\n(command erase)"}),
        ("TPelChangeColor",{"color":257}),("TChWallTerm",{"closed":"yes"})]:
        with pytest.raises(TianzhengInputError):validate_property_recipe(find_command(command),["1"],parameters)


def test_property_partial_failure_restores_every_attempted_property():
    runtime = Runtime()
    class Entity:
        Handle = "1"
        ObjectName = "TDbWall"
        Layer = "WALL"
        Height = 3000.0
        Elevation = 0.0
        def __setattr__(self,name,value):
            if name == "Elevation" and value == 100:raise RuntimeError("setter refused")
            object.__setattr__(self,name,value)
        def Update(self): pass
    entity = Entity();runtime.document.ModelSpace.items = [entity]
    before = runtime._t30_snapshot(runtime.document)
    result = runtime._t30_apply_changes(runtime.document,before,[
        ("1",entity,"Height",3000,4000),("1",entity,"Elevation",0,100)])
    assert not result.ok and result.payload["rollback_complete"]
    assert entity.Height == 3000 and entity.Elevation == 0


@pytest.mark.asyncio
async def test_unsafe_factory_probe_is_disabled_before_any_cad_access(monkeypatch):
    async def backend():raise AssertionError("Factory route must never connect to CAD")
    monkeypatch.setattr(server,"get_backend",backend)
    result = await server.tianzheng.__wrapped__(operation="factory_probe",data={"object_type":"column"})
    value = json.loads(result) if isinstance(result,str) else result.structuredContent
    assert value["error"]["code"] == "E_TIANZHENG_FACTORY_DISABLED"


def test_empty_custom_column_never_reads_crash_prone_dimensions():
    class EmptyColumn:
        Handle="1"
        ObjectName="TDbColumn"
        EntityName="TDbColumn"
        Layer="COLUMN"
        def GetBoundingBox(self):return [0,0,0],[0,0,0]
        def __getattr__(self,name):
            if name in {"Width","Height","Deep"}:raise AssertionError("Uninitialized shape getter must not run")
            raise AttributeError(name)
    document=Document();document.ModelSpace.items=[EmptyColumn()]
    snapshot=Runtime()._t30_snapshot(document)
    assert snapshot["1"]["uninitialized_custom_geometry"]
    assert "height" not in snapshot["1"]


def test_template_recipe_accepts_only_typed_dimensions_and_handle():
    from autocad_mcp.tianzheng_templates import validate_template_recipe
    entry=find_command("TGColumn")
    data={"command":"TGColumn","template_handle":"1","points":[[0,0]],"parameters":{"width":600,"depth":400,"height":3000}}
    assert validate_template_recipe(entry,data)["points"]==[[0.0,0.0,0.0]]
    circular={**data,"parameters":{"shape":"circle","diameter":750,"height":3450}}
    assert validate_template_recipe(entry,circular)["parameters"]["diameter"]==750
    for patch in [{"template_handle":"1\nERASE"},{"points":[[False,0]]},{"parameters":{"width":600,"depth":0,"height":3000}},{"parameters":{"Width;Delete":600}}]:
        with pytest.raises(TianzhengInputError):validate_template_recipe(entry,{**data,**patch})


@pytest.mark.asyncio
async def test_initialized_column_copy_preserves_xy_footprint_and_separate_elevation():
    runtime = Runtime()
    class Column:
        ObjectName = EntityName = "TDbColumn"
        Layer = "COLUMN"
        SectionShapeText = "矩形"
        def __init__(self, handle):
            self.Handle = handle
            self.Width = self.Deep = 600.0
            self.Diameter = 600.0
            self.Rotation = 0.0
            self.Height = 3000.0
            self.Elevation = 0.0
            self.center = [1000.0, 2000.0]
        def GetBoundingBox(self):
            x,y = self.center
            if self.SectionShapeText=="圆形":
                return [x-self.Diameter/2,y-self.Diameter/2,0],[x+self.Diameter/2,y+self.Diameter/2,0]
            return [x-self.Width/2,y-self.Deep/2,0], [x+self.Width/2,y+self.Deep/2,0]
        def Update(self):
            if self.SectionShapeText=="圆形":
                self.Rotation=359.9999999999999
        def Copy(self):
            import copy
            result = copy.deepcopy(self)
            result.Handle = str(len(runtime.document.ModelSpace.items)+1)
            runtime.document.ModelSpace.items.append(result)
            return result
        def Move(self, old, new):
            old,new = getattr(old,"value",old),getattr(new,"value",new)
            self.center = [self.center[i]+new[i]-old[i] for i in (0,1)]
            self.Elevation += new[2]-old[2]
        def Delete(self): runtime.document.ModelSpace.items.remove(self)
    original = Column("1")
    runtime.document.ModelSpace.items = [original]
    runtime.document.HandleToObject = lambda handle: next(item for item in runtime.document.ModelSpace.items if item.Handle == handle)
    before = runtime._t30_snapshot(runtime.document)
    result = await runtime._tianzheng_template_create(runtime.context, {
        "command":"TGColumn", "template_handle":"1", "points":[[0,0,0],[3000,0,200]],
        "parameters":{"width":600.0,"depth":400.0,"height":3300.0}})
    assert result.ok and result.payload["template_unchanged"]
    after = runtime._t30_snapshot(runtime.document)
    assert after["1"] == before["1"]
    assert after["3"]["elevation"] == 200
    assert after["3"]["bounds"]["min"] == [2700,-200,0]
    assert after["3"]["deep"] == 400 and after["3"]["height"] == 3300
    assert find_command("TGColumn")["execution_contract"]["automated_recipe_available"]
    assert find_command("TRect")["execution_contract"]["automated_recipe_available"]
    moved = await runtime._tianzheng_translate(runtime.context, {
        "command":"TMove","selection_handles":["2"],"parameters":{"offset":[100,200,300]}})
    assert moved.ok and not moved.payload["created_objects"]
    assert runtime._t30_snapshot(runtime.document)["2"]["elevation"] == 300
    copied = await runtime._tianzheng_translate(runtime.context, {
        "command":"TDragCopy","selection_handles":["2"],"parameters":{"offset":[1000,0,100]}})
    assert copied.ok and copied.payload["entity_count"] == 4
    assert copied.payload["created_objects"][0]["elevation"] == 400
    circular=await runtime._tianzheng_template_create(runtime.context,{
        "command":"TGColumn","template_handle":"1","points":[[5300,7700,175]],
        "parameters":{"shape":"circle","diameter":750,"height":3450}})
    assert circular.ok and runtime._t30_snapshot(runtime.document)["1"]==before["1"]
    column=circular.payload["created_objects"][0]
    assert column["section_shape"]=="圆形" and column["diameter"]==750
    assert column["bounds"]=={"min":[4925,7325,0],"max":[5675,8075,0]}
    assert column["elevation"]==175 and column["height"]==3450
    assert runtime._t30_snapshot(runtime.document)["2"]["elevation"] == 300
    old_variable=runtime.document.GetVariable
    runtime.document.GetVariable=lambda name:[0,0,1] if name=="VIEWDIR" else 0 if name=="VIEWTWIST" else old_variable(name)
    fixed=runtime._t30_snapshot(runtime.document)["4"]
    distributed=await runtime._tianzheng_translate(runtime.context,{"command":"HoriDistribution","selection_handles":["1","2","3"],"parameters":{"spacing":100.0}})
    assert distributed.ok and distributed.payload["spacing_mode"]=="edge_gap"
    after=runtime._t30_snapshot(runtime.document)
    assert after["4"]==fixed
    ordered=sorted([after[h]["bounds"] for h in ["1","2","3"]],key=lambda b:b["min"][0])
    assert all(right["min"][0]-left["max"][0]==100 for left,right in zip(ordered,ordered[1:]))


def test_translation_rejects_macros_nonfinite_offsets_and_duplicate_handles():
    from autocad_mcp.tianzheng_transforms import validate_transform_recipe
    entry = find_command("TMove")
    data = {"command":"TMove","selection_handles":["a"],"parameters":{"offset":[100,200]}}
    assert validate_transform_recipe(entry,data)["parameters"]["offset"] == [100.0,200.0,0.0]
    for patch in [{"parameters":{"offset":[float('nan'),0]}},{"parameters":{"offset":[0,0]}},
                  {"parameters":{"offset":[0,False]}},{"parameters":{"offset":[1,2],"macro":"ERASE"}},
                  {"selection_handles":["a","A"]},{"points":[[0,0]]}]:
        with pytest.raises(TianzhengInputError): validate_transform_recipe(entry,{**data,**patch})


def test_layer_visibility_and_freeze_verification_preserves_other_flags():
    import copy
    from autocad_mcp.tianzheng import verify_selection_mutation
    before={"1":{"Layer":"A"},"2":{"Layer":"B"}}
    environment={"current_layer":"A","layers":{
        "A":{"on":True,"frozen":False,"locked":False,"color":1},
        "B":{"on":True,"frozen":False,"locked":True,"color":2}}}
    after=copy.deepcopy(environment);after["layers"]["B"]["frozen"]=True
    assert verify_selection_mutation("TFREEZEOTHERLAYER",["1"],before,before,environment,after)
    after["layers"]["B"]["locked"]=False
    assert not verify_selection_mutation("TFREEZEOTHERLAYER",["1"],before,before,environment,after)
    after=copy.deepcopy(environment);after["layers"]["A"]["on"]=False
    assert verify_selection_mutation("TOFFLAYER",["1"],before,before,environment,after)


@pytest.mark.asyncio
async def test_layer_write_rolls_back_partial_changes_and_refuses_current_layer_freeze():
    runtime=Runtime()
    class Layer:
        Freeze=Lock=False
        Color=1;Linetype="Continuous"
        def __init__(self,name):self.Name=name;self.LayerOn=True
        def __setattr__(self,name,value):
            if name=="LayerOn" and getattr(self,"Name",None)=="C" and value is False:raise ValueError("Simulated layer write failure")
            object.__setattr__(self,name,value)
    class Layers(Space):
        def Item(self,key):
            return next(layer for layer in self.items if layer.Name==key) if isinstance(key,str) else self.items[key]
    runtime.document.Layers=Layers()
    runtime.document.Layers.items=[Layer("A"),Layer("B"),Layer("C")]
    runtime.document.ModelSpace.items=[SimpleNamespace(Handle="1",ObjectName="AcDbLine",Layer="A")]
    runtime.document.HandleToObject=lambda _:runtime.document.ModelSpace.items[0]
    old_variable=runtime.document.GetVariable
    runtime.document.GetVariable=lambda name:"A" if name=="CLAYER" else old_variable(name)
    result=await runtime._tianzheng_layer_selection(runtime.context,{"command":"TFreezeLayer","selection_handles":["1"]})
    assert not result.ok and result.error_code=="E_TIANZHENG_CURRENT_LAYER"
    result=await runtime._tianzheng_layer_selection(runtime.context,{"command":"TOffOtherLayer","selection_handles":["1"]})
    assert not result.ok and result.payload["rollback_complete"]
    assert all(layer.LayerOn for layer in runtime.document.Layers.items)


def test_idempotent_com_retry_is_bounded_and_does_not_retry_other_errors():
    runtime=Runtime();calls=[];delays=[]
    runtime._com_executor.cooperative_sleep=delays.append
    class Rejected(Exception):hresult=-2147418111
    def read():
        calls.append(1)
        if len(calls)<3:raise Rejected()
        return 42
    assert runtime._t30_idempotent_call(read)==42
    assert len(calls)==3 and delays==[0.05,0.1]
    calls.clear()
    def rejected():calls.append(1);raise Rejected()
    with pytest.raises(Rejected):runtime._t30_idempotent_call(rejected)
    assert len(calls)==4
    calls.clear()
    def timed_out():calls.append(1);raise TimeoutError('uncertain call')
    with pytest.raises(TimeoutError):runtime._t30_idempotent_call(timed_out)
    assert len(calls)==1


def test_unreadable_class_never_enters_shape_getters():
    class UnknownShape:
        Handle="1";Layer="0"
        def __getattr__(self,name):
            if name in {"ObjectName","EntityName"}:raise AttributeError(name)
            raise AssertionError('Unknown class must not be probed: '+name)
    runtime=Runtime();runtime.document.ModelSpace.items=[UnknownShape()]
    assert runtime._t30_snapshot(runtime.document)["1"]["geometry_unreadable"]


@pytest.mark.asyncio
async def test_read_only_measurements_preserve_geometry_and_do_not_send_commands():
    import math
    runtime=Runtime()
    line=SimpleNamespace(Handle="1",ObjectName="AcDbLine",Layer="0",StartPoint=[0,0,0],EndPoint=[3,4,0],Length=5.0,
        GetBoundingBox=lambda:([0,0,0],[3,4,0]))
    circle=SimpleNamespace(Handle="2",ObjectName="AcDbCircle",Layer="0",Center=[10,0,0],Radius=2.0,
        GetBoundingBox=lambda:([8,-2,0],[12,2,0]))
    runtime.document.ModelSpace.items=[line,circle]
    runtime.document.HandleToObject=lambda handle:line if handle=="1" else circle
    before=runtime._t30_snapshot(runtime.document)
    bounds=await runtime._tianzheng_measure(runtime.context,{"command":"TGetExtend","selection_handles":["1","2"]})
    assert bounds.ok and bounds.payload["bounds"]=={"min":[0,-2,0],"max":[12,4,0]}
    length=await runtime._tianzheng_measure(runtime.context,{"command":"TLengthSumm","selection_handles":["1","2"]})
    assert length.ok and math.isclose(length.payload["total_length"],5+4*math.pi)
    assert runtime._t30_snapshot(runtime.document)==before and runtime.document.sent==[]


@pytest.mark.asyncio
async def test_exterior_wall_changes_require_classification_and_apply_inside_outside_widths():
    runtime=Runtime()
    class Wall:
        Handle="1";ObjectName=EntityName="TDbWall";Layer="WALL"
        IOFeature="双内";LeftWidth=RightWidth=120.0;Height=3000.0;Elevation=0.0
        IsExternal="内墙"
        def GetBoundingBox(self):return [0,-self.RightWidth,self.Elevation],[1000,self.LeftWidth,self.Elevation+self.Height]
        def Update(self):pass
    wall=Wall();runtime.document.ModelSpace.items=[wall];runtime.document.HandleToObject=lambda _:wall
    rejected=await runtime._tianzheng_properties(runtime.context,"TChEWallHeight",["1"],{"height":4200})
    assert not rejected.ok and rejected.error_code=="E_TIANZHENG_EXTERIOR_WALL_REQUIRED" and wall.Height==3000
    # Real T30 allows a side feature without recognizing the wall as exterior.
    wall.IOFeature="左外"
    rejected=await runtime._tianzheng_properties(runtime.context,"TExtThick",["1"],{"inside_width":80.0,"outside_width":180.0})
    assert not rejected.ok and rejected.error_code=="E_TIANZHENG_EXTERIOR_WALL_REQUIRED"
    assert wall.LeftWidth==wall.RightWidth==120
    runtime.document.SendCommand=lambda _:None
    exterior=await runtime._tianzheng_properties(runtime.context,"TMarkExtWall",["1"],{"outside":"left","pick_point":[500,120,0]})
    assert not exterior.ok and not exterior.payload["geometry_verified"] and wall.IsExternal=="内墙"
    wall.IsExternal="外墙"
    thickness=await runtime._tianzheng_properties(runtime.context,"TExtThick",["1"],{"inside_width":80.0,"outside_width":180.0})
    assert thickness.ok and wall.LeftWidth==180 and wall.RightWidth==80
    wall.IOFeature="右外"
    thickness=await runtime._tianzheng_properties(runtime.context,"TExtThick",["1"],{"inside_width":65.0,"outside_width":210.0})
    assert thickness.ok and wall.LeftWidth==65 and wall.RightWidth==210
    interior=await runtime._tianzheng_properties(runtime.context,"TMarkIntWall",["1"],{"internal":True})
    assert not interior.ok and wall.IsExternal=="外墙"
    wall.IOFeature="双外"
    ambiguous=await runtime._tianzheng_properties(runtime.context,"TExtThick",["1"],{"inside_width":80.0,"outside_width":180.0})
    assert not ambiguous.ok and ambiguous.error_code=="E_TIANZHENG_EXTERIOR_SIDE_REQUIRED"


def _corner_runtime():
    runtime=Runtime()
    class Wall:
        ObjectName=EntityName="TDbWall";Layer="WALL";LeftWidth=RightWidth=120.0;Height=3000.0;Elevation=0.0;Radius=0.0;Style="砖"
        def __init__(self,handle,low,high):self.Handle=handle;self.low=list(low);self.high=list(high)
        def GetBoundingBox(self):return [*self.low,0.0],[*self.high,3000.0]
    horizontal=Wall("221",[40000,-120],[41500,120]);vertical=Wall("222",[41880,500],[42120,2000])
    runtime.document.ModelSpace.items=[horizontal,vertical]
    runtime.document.HandleToObject=lambda h:{e.Handle:e for e in runtime.document.ModelSpace.items}[h]
    variables={"VIEWDIR":[0,0,1],"VIEWTWIST":0.0,"VIEWCTR":[41000,1000,0],"VIEWSIZE":4000.0,"SCREENSIZE":[1600,800],"PICKBOX":3,"LASTPROMPT":""}
    old=runtime.document.GetVariable
    runtime.document.GetVariable=lambda name:variables[name] if name in variables else old(name)
    return runtime,horizontal,vertical,Wall


@pytest.mark.asyncio
async def test_wall_fillet_radius_zero_verifies_extended_corner_and_rejects_unsafe_picks():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    data={"command":"TFillet","selection_handles":["221","222"],"points":[[40750,0],[42000,1250]],"parameters":{"radius":0.0}}
    sent=[]
    def fillet(text):
        sent.append(text);horizontal.high[0]=42000;vertical.low[1]=0
    runtime.document.SendCommand=fillet
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and result.payload["geometry_verified"] and result.payload["completed"]
    assert sent==["TFillet\nR\n0\n40750,0\n42000,1250\n"]
    assert result.payload["plan"]["corner"]==[42000,0]
    # Re-running on the joined corner is now a crossing/end case; a pick outside the view or near another object is refused unsent.
    horizontal.high[0]=41500;vertical.low[1]=500;sent.clear()
    for points in ([[40750,0],[42000,5000]],[[40750,90000],[42000,1250]]):
        refused=await runtime.tianzheng_command("execute",runtime.context,{**data,"points":points})
        assert not refused.ok and refused.error_code=="E_TIANZHENG_PRECONDITION_FAILED"
    other=Wall("230",[40700,-300],[40800,-130]);runtime.document.ModelSpace.items.append(other)
    refused=await runtime.tianzheng_command("execute",runtime.context,{**data,"points":[[40750,-110],[42000,1250]]})
    assert not refused.ok and "ambiguous" in refused.error
    runtime.document.ModelSpace.items.pop()
    vertical.low=[41880,-500]
    refused=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not refused.ok and "T-junction" in refused.error and sent==[]


@pytest.mark.asyncio
async def test_wall_fillet_radius_requires_one_matching_arc_and_reports_wrong_result_without_retry():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    data={"command":"TFillet","selection_handles":["221","222"],"points":[[40750,0],[42000,1250]],"parameters":{"radius":300.0}}
    def fillet(_):
        horizontal.high[0]=41700;vertical.low[1]=300
        arc=Wall("223",[41700,-120],[42120,300]);arc.Radius=300.0
        runtime.document.ModelSpace.items.append(arc)
    runtime.document.SendCommand=fillet
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and result.payload["created_objects"][0]["radius"]==300.0
    sent=[]
    runtime.document.SendCommand=lambda text:sent.append(text)
    repeated=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not repeated.ok and "occupied by object 223" in repeated.error and sent==[]
    # Same picks with walls left untouched: postcondition fails, nothing is re-sent.
    runtime2,h2,v2,_=_corner_runtime()
    calls=[]
    runtime2.document.SendCommand=lambda text:calls.append(text)
    failed=await runtime2.tianzheng_command("execute",runtime2.context,data)
    assert not failed.ok and failed.error_code=="E_TIANZHENG_POSTCONDITION_NOT_MET"
    assert failed.payload["retry_command"] is False and len(calls)==1
    assert "wall_extent_mismatch:221" in failed.payload["failed_checks"]
    h2.LeftWidth=100.0;h2.RightWidth=100.0;h2.low[1]=-100;h2.high[1]=100
    refused=await runtime2.tianzheng_command("execute",runtime2.context,data)
    assert not refused.ok and "equal widths" in refused.error and len(calls)==1


@pytest.mark.asyncio
async def test_wall_chamfer_maps_distances_to_selection_order_and_checks_diagonal_wall():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    # First selected wall is the vertical one: distance1 trims it, distance2 trims the horizontal wall.
    data={"command":"TChamfer","selection_handles":["222","221"],"points":[[42000,1250],[40750,0]],"parameters":{"distance1":900.0,"distance2":400.0}}
    sent=[]
    def chamfer(text):
        sent.append(text);horizontal.high[0]=41600;vertical.low[1]=900
        s=120*900/((400**2+900**2)**0.5);c=120*400/((400**2+900**2)**0.5)
        diagonal=Wall("223",[41600-s,-c],[42000+s,900+c]);runtime.document.ModelSpace.items.append(diagonal)
    runtime.document.SendCommand=chamfer
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and result.payload["geometry_verified"], result.payload
    assert sent==["TChamfer\nD\n900\n400\n42000,1250\n40750,0\n\n"]
    # Swapping the distances would be a different wall layout and must fail verification.
    runtime2,h2,v2,W2=_corner_runtime()
    def swapped(_):
        h2.high[0]=41100;v2.low[1]=400
        runtime2.document.ModelSpace.items.append(W2("223",[41100,-120],[42120,400]))
    runtime2.document.SendCommand=swapped
    failed=await runtime2.tianzheng_command("execute",runtime2.context,data)
    assert not failed.ok and "wall_extent_mismatch:221" in failed.payload["failed_checks"]
    bad=await server.tianzheng.__wrapped__(operation="execute",data={**data,"parameters":{"distance1":0,"distance2":400}})
    value=json.loads(bad) if isinstance(bad,str) else bad.structuredContent
    assert not value["ok"] and "positive" in value["error"]["message"]


@pytest.mark.asyncio
async def test_wall_split_needs_centreline_point_and_two_collinear_pieces():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    data={"command":"tkatana","selection_handles":["221"],"points":[[40600,0]]}
    sent=[]
    def split(text):
        sent.append(text);horizontal.high[0]=40600
        runtime.document.ModelSpace.items.append(Wall("223",[40600,-120],[41500,120]))
        runtime.document.ModelSpace.items.append(SimpleNamespace(Handle="225",ObjectName="GlwKatana",Layer="GRID",GetBoundingBox=lambda:([40600,-120,0],[40600,120,0])))
    runtime.document.SendCommand=split
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and result.payload["geometry_verified"] and sent==["tkatana\n40600,0\n\n"]
    # Off-centreline or near-end points are refused unsent; a wrong split position fails verification.
    for point in ([40800,60],[40100,0]):
        refused=await runtime.tianzheng_command("execute",runtime.context,{**data,"selection_handles":["223"],"points":[point]})
        assert not refused.ok and refused.error_code=="E_TIANZHENG_PRECONDITION_FAILED"
    assert len(sent)==1
    runtime2,h2,_,W2=_corner_runtime()
    def wrong(_):
        h2.high[0]=40900;runtime2.document.ModelSpace.items.append(W2("223",[40900,-120],[41500,120]))
        runtime2.document.ModelSpace.items.append(SimpleNamespace(Handle="225",ObjectName="GlwKatana",Layer="GRID",GetBoundingBox=lambda:([40900,-120,0],[40900,120,0])))
    runtime2.document.SendCommand=wrong
    failed=await runtime2.tianzheng_command("execute",runtime2.context,data)
    assert not failed.ok and {"piece_extent_mismatch","break_line_position_mismatch"} <= set(failed.payload["failed_checks"])


@pytest.mark.asyncio
async def test_wall_clear_offset_places_parallel_wall_on_picked_side():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    data={"command":"TOffset","selection_handles":["221"],"points":[[40750,-120]],"parameters":{"distance":800.0}}
    sent=[]
    def offset(text):
        sent.append(text);runtime.document.ModelSpace.items.append(Wall("223",[40000,-1160],[41500,-920]))
    runtime.document.SendCommand=offset
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and result.payload["geometry_verified"] and sent==["TOffset\n800\n40750,-120\n\n"]
    # Points inside the wall body do not choose a side in T30; nothing is sent.
    refused=await runtime.tianzheng_command("execute",runtime.context,{**data,"points":[[40750,-90]]})
    assert not refused.ok and "Side pick" in refused.error and len(sent)==1
    runtime2,_,_,W2=_corner_runtime()
    runtime2.document.SendCommand=lambda _:runtime2.document.ModelSpace.items.append(W2("223",[40000,920],[41500,1160]))
    wrong_side=await runtime2.tianzheng_command("execute",runtime2.context,data)
    assert not wrong_side.ok and "new_wall_extent_mismatch:y" in wrong_side.payload["failed_checks"]


@pytest.mark.asyncio
async def test_curtain_conversion_round_trip_replaces_objects_by_footprint(monkeypatch):
    from autocad_mcp import tianzheng_walls
    blocked=await server.tianzheng.__wrapped__(operation="execute",data={"command":"TConvertCurtain","selection_handles":["221"],"parameters":{"direction":"to_curtain"}})
    value=json.loads(blocked) if isinstance(blocked,str) else blocked.structuredContent
    assert not value["ok"] and "sticky" in value["error"]["message"]
    monkeypatch.setattr(tianzheng_walls,"CONVERSION_ENABLED",True)
    runtime,horizontal,vertical,Wall=_corner_runtime()
    items=runtime.document.ModelSpace.items
    class Curtain(Wall):
        ObjectName=EntityName="TDbCurtainWall";Layer="CURTWALL";Style=None
    sent=[]
    def convert(text):
        sent.append(text)
        handle=text.split('handent "')[1].split('"')[0]
        old=next(e for e in items if e.Handle==handle)
        items.remove(old);new=f"{int(handle,16)+2:X}"
        items.append(Curtain(new,old.low,old.high) if "\nQ\n" not in text else Wall(new,old.low,old.high))
    runtime.document.SendCommand=convert
    to_curtain={"command":"TConvertCurtain","selection_handles":["221"],"parameters":{"direction":"to_curtain"}}
    result=await runtime.tianzheng_command("execute",runtime.context,to_curtain)
    assert result.ok, result.payload
    assert sent[-1]=='TConvertCurtain\n(ssadd (handent "221") (ssadd))\n\n\n'
    again=await runtime.tianzheng_command("execute",runtime.context,{**to_curtain,"selection_handles":["223"]})  # 221 -> curtain 223
    assert not again.ok and "T30 walls" in again.error and len(sent)==1
    disabled=await server.tianzheng.__wrapped__(operation="execute",data={**to_curtain,"selection_handles":["223"],"parameters":{"direction":"to_wall","material":4}})
    value=json.loads(disabled) if isinstance(disabled,str) else disabled.structuredContent
    assert not value["ok"] and "disabled" in value["error"]["message"]
    from autocad_mcp import tianzheng_walls
    tianzheng_walls.CURTAIN_TARGET_MATERIALS[4]="砖"
    try:
        back=await runtime.tianzheng_command("execute",runtime.context,{**to_curtain,"selection_handles":["223"],"parameters":{"direction":"to_wall","material":4}})
    finally:
        tianzheng_walls.CURTAIN_TARGET_MATERIALS.clear()
    assert back.ok and sent[-1]=='TConvertCurtain\nQ\n(ssadd (handent "223") (ssadd))\n\n4\n\n'
    unverified=await server.tianzheng.__wrapped__(operation="execute",data={**to_curtain,"parameters":{"direction":"to_wall","material":6}})
    value=json.loads(unverified) if isinstance(unverified,str) else unverified.structuredContent
    assert not value["ok"] and "live-verified" in value["error"]["message"]
    # A conversion that also replaces the other wall is reported, not accepted.
    def overreach(text):
        convert(text);items.remove(vertical);items.append(Curtain("230",vertical.low,vertical.high))
    runtime.document.SendCommand=overreach
    wrong=await runtime.tianzheng_command("execute",runtime.context,{**to_curtain,"selection_handles":["225"]})  # 223 -> wall 225 (via the temporarily enabled path)
    assert not wrong.ok and {"removed_objects_differ_from_selection","unselected_changed:222"} <= set(wrong.payload["failed_checks"])


@pytest.mark.asyncio
async def test_markwall_requires_expected_exterior_and_checks_each_wall():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    horizontal.IOFeature=vertical.IOFeature="双内";horizontal.IsExternal=vertical.IsExternal="内墙"
    data={"command":"TMarkWall","selection_handles":["221","222"],"parameters":{"expected_exterior":["221"]}}
    sent=[]
    def mark(text):
        sent.append(text);horizontal.IsExternal="外墙";horizontal.IOFeature="左外"
    runtime.document.SendCommand=mark
    result=await runtime.tianzheng_command("execute",runtime.context,data)
    assert result.ok and sent==['TMarkWall\n(ssadd (handent "222") (ssadd (handent "221") (ssadd)))\n\n\n']
    runtime.document.SendCommand=lambda _:setattr(vertical,"IsExternal","外墙")
    wrong=await runtime.tianzheng_command("execute",runtime.context,{**data,"parameters":{"expected_exterior":["221","222"]}})
    assert not wrong.ok and "not_exterior:222" in wrong.payload["failed_checks"]


@pytest.mark.asyncio
async def test_opening_flip_and_wall_dimension_and_north_arrow_postconditions():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    items=runtime.document.ModelSpace.items
    class Door:
        Handle="230";ObjectName=EntityName="TDbOpening";Layer="WINDOW";Width=900.0;Height=2100.0;WinSill=0.0;GetKind="门";GetSubKind="普通门"
        low=[40300,0];high=[41200,900]
        def GetBoundingBox(self):return [*self.low,0.0],[*self.high,0.0]
    door=Door();items.append(door)
    def flip(_):door.low=[40300,-900];door.high=[41200,0]
    runtime.document.SendCommand=flip
    flipped=await runtime.tianzheng_command("execute",runtime.context,{"command":"TMirWinIO","selection_handles":["230"]})
    assert flipped.ok, flipped.payload
    runtime.document.SendCommand=lambda _:None
    unflipped=await runtime.tianzheng_command("execute",runtime.context,{"command":"TMirWinIO","selection_handles":["230"]})
    assert not unflipped.ok and "not_mirrored_about_wall:230" in unflipped.payload["failed_checks"]
    items.remove(door)
    class Dim:
        Handle="231";ObjectName=EntityName="TDbDimension";Layer="DIM"
        def GetBoundingBox(self):return [41500.0,-120.0,0.0],[41700.0,120.0,0.0]
    runtime.document.SendCommand=lambda _:items.append(Dim())
    dim={"command":"TDimWall","selection_handles":["221"],"points":[[41000,-500],[41000,500]]}
    measured=await runtime.tianzheng_command("execute",runtime.context,dim)
    assert measured.ok, measured.payload
    items.pop()
    crossing=await runtime.tianzheng_command("execute",runtime.context,{**dim,"points":[[41000,-500],[41000,600]],"selection_handles":["222"]})
    assert not crossing.ok and "crosses ['221']" in crossing.error
    class Arrow:
        Handle="232";ObjectName=EntityName="TDbSymbNorthThumb";Layer="DIM";Rotation=90.0
        def GetBoundingBox(self):return [49500.0,9500.0,0.0],[50500.0,10900.0,0.0]
    sent=[]
    def north(text):sent.append(text);items.append(Arrow())
    runtime.document.SendCommand=north
    runtime.document.GetVariable=(lambda old:lambda name:[45000,5000,0] if name=="VIEWCTR" else 20000.0 if name=="VIEWSIZE" else old(name))(runtime.document.GetVariable)
    arrow=await runtime.tianzheng_command("execute",runtime.context,{"command":"TNorthThumb","points":[[50000,10000]],"parameters":{"angle":90}})
    assert arrow.ok and sent==["TNorthThumb\n50000,10000\n90\n"]
    items.pop()
    wrong=await runtime.tianzheng_command("execute",runtime.context,{"command":"TNorthThumb","points":[[50000,10000]],"parameters":{"angle":0}})
    assert not wrong.ok and "arrow_rotation_differs" in wrong.payload["failed_checks"]


def _box(handle, cls, low, high, **extra):
    item = SimpleNamespace(Handle=handle, ObjectName=cls, EntityName=cls, Layer="0", **extra)
    item.low, item.high = list(low), list(high)
    item.GetBoundingBox = lambda: ([*item.low, 0.0], [*item.high, 0.0])
    return item


@pytest.mark.asyncio
async def test_second_batch_recipes_verify_observable_results():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    items=runtime.document.ModelSpace.items
    items.append(_box("300","TDbDimension2",[40000,-900],[41500,-120]))
    # Extending dimension 300 to a new point beyond its right end.
    dim=next(i for i in items if i.Handle=="300")
    def extend(_):dim.high[0]=42000
    runtime.document.SendCommand=extend
    runtime.document.GetVariable=(lambda old:lambda name:20000.0 if name=="VIEWSIZE" else old(name))(runtime.document.GetVariable)
    grown=await runtime.tianzheng_command("execute",runtime.context,{"command":"TBreakDim","selection_handles":["300"],"points":[[40500,-500],[42000,-500]]})
    assert grown.ok, grown.payload
    inside=await runtime.tianzheng_command("execute",runtime.context,{"command":"TBreakDim","selection_handles":["300"],"points":[[40500,-500],[41000,-500]]})
    assert not inside.ok and inside.error_code=="E_TIANZHENG_PRECONDITION_FAILED"
    items.remove(dim)
    # Column bottom edge aligned to the wall's bottom face.
    column=_box("302","TDbColumn",[40700,-300],[41300,300],Height=3000.0)
    items.append(column)
    def align(text):
        column.low[1]+=180;column.high[1]+=180
        horizontal.high[0]=41000;items.append(Wall("305",[41000,-120],[41500,120]))
    runtime.document.SendCommand=align
    aligned=await runtime.tianzheng_command("execute",runtime.context,{"command":"TAlignColu","wall_handles":["221"],"selection_handles":["302"],"points":[[40300,-120],[41000,-300]]})
    assert aligned.ok, aligned.payload
    items.remove(next(i for i in items if i.Handle=="305"));horizontal.high[0]=41500
    runtime.document.SendCommand=lambda _:setattr(vertical,"Height",2800.0)
    column.low[1]=-300;column.high[1]=300
    stuck=await runtime.tianzheng_command("execute",runtime.context,{"command":"TAlignColu","wall_handles":["221"],"selection_handles":["302"],"points":[[40300,-120],[41000,-300]]})
    assert not stuck.ok and "column_not_aligned:302" in stuck.payload["failed_checks"] and any(c.startswith("unexpected_change:222") for c in stuck.payload["failed_checks"])
    vertical.Height=3000.0
    items.remove(column)
    withdrawn=await server.tianzheng.__wrapped__(operation="execute",data={"command":"TTextReplace","selection_handles":["303","304"],"points":[[50500,150],[50500,2150]]})
    value=json.loads(withdrawn) if isinstance(withdrawn,str) else withdrawn.structuredContent
    assert not value["ok"] and value["error"]["code"] in {"E_TIANZHENG_RECIPE_UNAVAILABLE","E_TIANZHENG_PARAMETER_REJECTED"}


@pytest.mark.asyncio
async def test_opening_table_rows_must_match_selected_groups():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    items=runtime.document.ModelSpace.items
    doors=[_box(h,"TDbOpening",[40100+i*500,0],[40500+i*500,800],Width=w,Height=2100.0,GetKind="门",GetSubKind="普通门") for i,(h,w) in enumerate((("230",900.0),("231",900.0),("232",1000.0)))]
    items.extend(doors)
    cells=[["类型","设计编号","洞口尺寸(mm)","数量"],["普通门","","900X2100","2"],["普通门","","1000X2100","1"]]
    def table(_):
        sheet=_box("240","TDbSheet",[100000,57000],[120000,59400],Title="门窗表",RowNum=3,ColNum=4)
        sheet.Text=lambda r,c:cells[r-1][c-1] if r>0 and c>0 else (_ for _ in ()).throw(ValueError("1-based"))
        items.append(sheet)
    runtime.document.SendCommand=table
    data={"command":"TStatOp","selection_handles":["230","231","232"],"points":[[100000,60000]]}
    ok=await runtime.tianzheng_command("execute",runtime.context,data)
    assert ok.ok, ok.payload
    items.pop();cells[1][3]="1"
    miscounted=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not miscounted.ok and "table_rows_differ_from_selected_openings" in miscounted.payload["failed_checks"]



@pytest.mark.asyncio
async def test_insulation_requires_exterior_wall_and_checks_exterior_side_thickness():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    horizontal.IsExternal="外墙";horizontal.IOFeature="右外";horizontal.LeftInsuThick=horizontal.RightInsuThick=0.0
    data={"command":"TAddInsulate","selection_handles":["221"],"points":[[40750,-120]],"parameters":{"thickness":60}}
    sent=[]
    horizontal.Insulate="无"
    def insulate(text):sent.append(text);horizontal.RightInsuThick=60.0;horizontal.Insulate="外侧";horizontal.low[1]=-180
    runtime.document.SendCommand=insulate
    ok=await runtime.tianzheng_command("execute",runtime.context,data)
    assert ok.ok and sent==["TAddInsulate\nT\n60\n40750,-120\n\n"], ok.payload
    horizontal.RightInsuThick=0.0;horizontal.Insulate="无";horizontal.low[1]=-120
    runtime.document.SendCommand=lambda _:setattr(horizontal,"LeftInsuThick",60.0)
    wrong=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not wrong.ok and {"exterior_side_insulation_thickness_differs","insulation_not_enabled","bounds_not_grown_by_thickness_on_picked_face"} <= set(wrong.payload["failed_checks"])
    horizontal.IsExternal="内墙"
    refused=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not refused.ok and "identification" in refused.error


@pytest.mark.asyncio
async def test_axis_linetype_toggle_must_flip_exactly_one_representation():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    class Layer:
        Freeze=Lock=False;Color=1
        def __init__(self,name,linetype):self.Name=name;self.LayerOn=True;self.Linetype=linetype
    layers=[Layer("0","Continuous"),Layer("DOTE","DASHDOT")]
    runtime.document.Layers=SimpleNamespace(Count=2,Item=lambda i:layers[i])
    runtime.document.GetVariable=(lambda old:lambda name:"0" if name=="CLAYER" else old(name))(runtime.document.GetVariable)
    axis=_box("310","AcDbLine",[0,0],[0,9000],Linetype="ByLayer");axis.Layer="DOTE"
    runtime.document.ModelSpace.items.append(axis)
    sent=[]
    def toggle(text):sent.append(text);layers[1].Linetype="Continuous"
    runtime.document.SendCommand=toggle
    ok=await runtime.tianzheng_command("execute",runtime.context,{"command":"TAxisDote"})
    assert ok.ok and sent==["TAxisDote\n"], ok.payload
    runtime.document.SendCommand=lambda _:None
    none=await runtime.tianzheng_command("execute",runtime.context,{"command":"TAxisDote"})
    assert not none.ok and "axis_linetype_not_toggled_exactly_once" in none.payload["failed_checks"]


@pytest.mark.asyncio
async def test_axis_clip_requires_exact_remaining_pieces():
    runtime,horizontal,vertical,Wall=_corner_runtime()
    items=runtime.document.ModelSpace.items
    def line(handle,y0,y1):
        item=_box(handle,"AcDbLine",[0,y0],[0,y1]);item.Layer="DOTE";item.StartPoint=[0.0,y0,0.0];item.EndPoint=[0.0,y1,0.0];return item
    axis=line("320",0,9000);items.append(axis)
    def clip(text):
        axis.high[1]=3000;axis.EndPoint=[0.0,3000.0,0.0];items.append(line("321",5000,9000))
    runtime.document.SendCommand=clip
    data={"command":"TClipAxis","points":[[-1000,3000],[1000,5000]]}
    ok=await runtime.tianzheng_command("execute",runtime.context,data)
    assert ok.ok, ok.payload
    items.remove(next(i for i in items if i.Handle=="321"));axis.high[1]=9000;axis.EndPoint=[0.0,9000.0,0.0]
    def short(text):
        axis.high[1]=2500;axis.EndPoint=[0.0,2500.0,0.0];items.append(line("322",5000,9000))
    runtime.document.SendCommand=short
    bad=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not bad.ok and "axis_not_clipped_to_rectangle:320" in bad.payload["failed_checks"]
    items.remove(next(i for i in items if i.Handle=="322"));axis.high[1]=9000;axis.EndPoint=[0.0,9000.0,0.0]
    def replace(text):
        items.remove(axis);items.extend([line("323",0,3000),line("324",5000,9000)])
    runtime.document.SendCommand=replace
    replaced=await runtime.tianzheng_command("execute",runtime.context,data)
    assert replaced.ok, replaced.payload


@pytest.mark.asyncio
async def test_room_search_and_query_check_areas_against_wall_interior():
    runtime=Runtime()
    class Wall:
        ObjectName=EntityName="TDbWall";Layer="WALL";LeftWidth=RightWidth=120.0;Height=3000.0;Elevation=0.0;Radius=0.0;IsExternal="内墙";IOFeature="双内"
        def __init__(self,handle,low,high):self.Handle=handle;self.low=low;self.high=high
        def GetBoundingBox(self):return [*self.low,0.0],[*self.high,3000.0]
    walls=[Wall("1",[0,-120],[6000,120]),Wall("2",[5880,0],[6120,4000]),Wall("3",[0,3880],[6000,4120]),Wall("4",[-120,0],[120,4000])]
    items=runtime.document.ModelSpace.items;items.extend(walls)
    runtime.document.HandleToObject=lambda h:{e.Handle:e for e in items}[h]
    def space(handle,kind,area,perimeter,low,high):
        return _box(handle,"TDbSpace",low,high,SpaceType=kind,UseArea=area,Perimeter=perimeter,StuccoThickness=0.0,Name="房间")
    def search(_):
        items.append(space("10","房间面积","21.658",19040.0,[2500,1500],[3500,2500]))
        items.append(space("11","建筑轮廓面积","26.458",20960.0,[2500,-5500],[3500,-4500]))
        for w in walls:w.IsExternal="外墙";w.IOFeature="右外"
    runtime.document.SendCommand=search
    data={"command":"TUpdSpace","selection_handles":["1","2","3","4"],"points":[[3000,-5000]]}
    ok=await runtime.tianzheng_command("execute",runtime.context,data)
    assert ok.ok, ok.payload
    for h in ("10","11"):items.remove(next(i for i in items if i.Handle==h))
    for w in walls:w.IsExternal="内墙";w.IOFeature="双内"
    def wrong(_):items.append(space("12","房间面积","24.000",20000.0,[2500,1500],[3500,2500]))
    runtime.document.SendCommand=wrong
    bad=await runtime.tianzheng_command("execute",runtime.context,data)
    assert not bad.ok and any(c.startswith("room_area_differs") for c in bad.payload["failed_checks"])
    items.remove(next(i for i in items if i.Handle=="12"))
    withdrawn=await server.tianzheng.__wrapped__(operation="execute",data={"command":"TSpArea","selection_handles":["1","2","3","4"],"points":[[1000,1000]]})
    value=json.loads(withdrawn) if isinstance(withdrawn,str) else withdrawn.structuredContent
    assert not value["ok"] and "withdrawn" in value["error"]["message"]
    runtime.document.SendCommand=lambda _:items.append(_box("14","TDbDimension2",[10000,8750],[13000,9700]))
    runtime.document.GetVariable=(lambda old:lambda name:[0,0,1] if name=="VIEWDIR" else 0.0 if name=="VIEWTWIST" else old(name))(runtime.document.GetVariable)
    dim=await runtime.tianzheng_command("execute",runtime.context,{"command":"TDimMP","points":[[10000,10000],[13000,10000],[11500,9000]]})
    assert dim.ok, dim.payload


def test_escape_guard_only_allows_the_managed_test_drawing_in_the_bound_window():
    from autocad_mcp.tianzheng_escape import escape_guard
    root = r"C:\Users\matincz\AutoCAD-MCP-output\drawings"
    path = root + r"\T30-test.dwg"
    assert escape_guard(path, "T30天正建筑软件 For Autodesk AutoCAD 2025  T30-test.dwg", root) == "T30-test.dwg"
    for bad_path, title in ((path, "T30 ... 沈阳招商户型2026091011_t3.dwg"),
                            (r"C:\Projects\沈阳招商户型2026091011_t3.dwg", "沈阳招商户型2026091011_t3.dwg"),
                            (r"C:\Users\matincz\Desktop\T30-test.dwg", "T30-test.dwg"),
                            (path, "AutoCAD 2025  Drawing1.dwg")):
        with pytest.raises(TianzhengInputError):
            escape_guard(bad_path, title, root)
