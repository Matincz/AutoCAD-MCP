"""Evidence-backed T30 command catalogue and non-executable input values.

This interface never accepts scripts, arbitrary command strings, filenames or
macros. A catalogue entry documents meaning, not an assertion of live support.
"""
from __future__ import annotations

import copy
import json
import math
import re
from functools import lru_cache
from pathlib import Path


class TianzhengInputError(ValueError):
    error_code = "E_TIANZHENG_PARAMETER_REJECTED"


class TianzhengRecipeUnavailable(TianzhengInputError):
    error_code = "E_TIANZHENG_RECIPE_UNAVAILABLE"


@lru_cache(maxsize=1)
def command_catalogue() -> dict:
    path = Path(__file__).parent / "data" / "tianzheng_commands.json"
    return json.loads(path.read_text(encoding="utf-8"))


def find_command(value: str) -> dict:
    if not isinstance(value, str) or not value.strip():
        raise TianzhengInputError("command must be a catalogue command, label or unambiguous alias")
    key = value.strip().casefold()
    entries = command_catalogue()["commands"]
    exact = [entry for entry in entries if entry["command"].casefold() == key]
    matches = exact or [entry for entry in entries if key in {
        name.casefold() for name in entry["names"] + entry["aliases"]
    }]
    if len(matches) != 1:
        raise TianzhengInputError("Unknown or ambiguous Tianzheng command; use its full catalogue command name")
    return _with_execution_contract(matches[0])


def list_commands(query: str = "", category: str | None = None, offset: int = 0, limit: int = 30) -> dict:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise TianzhengInputError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise TianzhengInputError("limit must be an integer between 1 and 200")
    entries = command_catalogue()["commands"]
    query = str(query).strip().casefold()
    selected = [entry for entry in entries
                if (not category or entry["category"] == category)
                and (not query or query in json.dumps(entry, ensure_ascii=False).casefold())]
    return {"product": command_catalogue()["product"], "total": len(selected),
            "offset": offset, "commands": [_with_execution_contract(entry) for entry in selected[offset:offset + limit]],
            "categories": sorted({entry["category"] for entry in entries}),
            "source_notes": command_catalogue()["source_notes"]}


def finite_number(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TianzhengInputError("Coordinates and dimensions must be finite numbers")
    if abs(value) > 1e10:
        raise TianzhengInputError("Coordinate or dimension exceeds the supported range")
    return format(value, ".15g")


def encode_response(entry: dict, data: dict) -> str:
    """Encode exactly one response to the currently owned command prompt."""
    if not isinstance(data, dict) or set(data) - {"type", "value"}:
        raise TianzhengInputError("input accepts only type and value; scripts/macros are unavailable")
    kind, value = data.get("type"), data.get("value")
    if kind == "enter" and value is None:
        return "\n"
    if kind == "point":
        if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
            raise TianzhengInputError("point requires [x,y] or [x,y,z]")
        return ",".join(finite_number(v) for v in value) + "\n"
    if kind == "number":
        return finite_number(value) + "\n"
    if kind == "keyword":
        if not isinstance(value, str) or value.upper() not in entry.get("keywords", []):
            raise TianzhengInputError("Keyword is not documented for this command")
        return value.upper() + "\n"
    if kind == "selection":
        if not isinstance(value, list) or not 1 <= len(value) <= 256:
            raise TianzhengInputError("selection requires 1 to 256 existing entity handles")
        if any(not isinstance(h, str) or not re.fullmatch(r"[0-9A-Fa-f]{1,16}", h) for h in value):
            raise TianzhengInputError("Selection handles must be hexadecimal, never LISP expressions")
        # The expression is generated entirely by the server from checked handles.
        return _selection_expression(value)
    raise TianzhengInputError("input type must be point, number, selection, keyword or enter")


def _selection_expression(handles: list[str]) -> str:
    # Nested ssadd builds a selection set without accepting caller code.
    expression = "(ssadd)"
    for handle in handles:
        expression = f'(ssadd (handent "{handle.upper()}") {expression})'
    return expression + "\n"


def command_is_owned(entry: dict, command_names: str) -> bool:
    names = {part.lstrip("_-' ").upper() for part in re.split(r"[\s']+", command_names) if part}
    allowed = {entry["command"].upper(), *[alias.upper() for alias in entry["aliases"]]}
    return bool(names & allowed)


ALIGNMENT_RECIPES = {
    "ALXLEFT": (0, "min"), "ALXRIGHT": (0, "max"), "ALXMID": (0, "center"),
    "ALYTOP": (1, "max"), "ALYBOTTOM": (1, "min"), "ALYMID": (1, "center"),
}

GLOBAL_LAYER_RECIPES = {
    "TOPENALLLAYER": ("on", True), "TUNFREEZEALLLAYER": ("frozen", False),
    "TSWITCHALLLAYER": ("on", "toggle"),
}
LAYER_SELECTION_RECIPES = {
    "TOFFLAYER": ("on",False,False), "TOFFOTHERLAYER": ("on",False,True),
    "TONLAYER": ("on",True,False), "TFREEZELAYER": ("frozen",True,False),
    "TFREEZEOTHERLAYER": ("frozen",True,True), "TUNFREEZELAYER": ("frozen",False,False),
}
SELECTION_RECIPES = {"TREVPLINE", "TREMOVEDUP", "TLOCKLAYER", "TLOCKOTHERLAYER", "TUNLOCKLAYER", "TMODELEV", "TMKHIDE", "TLOCVIS", "TUNHIDE"} | set(LAYER_SELECTION_RECIPES)
GLOBAL_DISPLAY_RECIPES = {"TUNHIDEALL"}


def compile_global_recipe(entry: dict) -> str:
    if entry["command"].upper() not in GLOBAL_LAYER_RECIPES and entry["command"].upper() not in GLOBAL_DISPLAY_RECIPES:
        raise TianzhengRecipeUnavailable("No complete zero-input recipe for this command")
    return entry["command"] + "\n"


def _with_execution_contract(entry: dict) -> dict:
    from autocad_mcp.tianzheng_properties import PROPERTY_RECIPES, LIVE_PENDING_PROPERTIES
    from autocad_mcp.tianzheng_templates import TEMPLATE_RECIPES, VERIFIED_TEMPLATE_RECIPES
    from autocad_mcp.tianzheng_transforms import TRANSFORM_RECIPES, DISTRIBUTION_RECIPES, LIVE_PENDING_TRANSFORMS
    from autocad_mcp.tianzheng_measurements import MEASUREMENT_RECIPES, LIVE_PENDING_MEASUREMENTS
    from autocad_mcp.tianzheng_native import native_recipe
    result = copy.deepcopy(entry)
    name = entry["command"].upper()
    if name in ALIGNMENT_RECIPES:
        required = ["selection_handles"]
        recipe = "selection_alignment"
    elif name in VERIFIED_TEMPLATE_RECIPES:
        required, recipe = ["template_handle", "points", "parameters"], "initialized_template_copy"
    elif name in TRANSFORM_RECIPES:
        required, recipe = ["selection_handles", "parameters"], "world_translation"
    elif name in MEASUREMENT_RECIPES:
        required, recipe = ["selection_handles"], "read_measurement"
    elif native_recipe(name) is not None:
        required, recipe = list(native_recipe(name).required), native_recipe(name).label
    elif name in PROPERTY_RECIPES:
        required, recipe = ["selection_handles", "parameters"], "typed_object_properties"
    elif name in SELECTION_RECIPES:
        required, recipe = ["selection_handles"], "selection_mutation"
    elif name in GLOBAL_LAYER_RECIPES:
        required, recipe = [], "global_layer_state"
    elif name in GLOBAL_DISPLAY_RECIPES:
        required, recipe = [], "global_visibility"
    elif name in {"TGWALL", "TOPENING", "TBANWIN"}:
        required = ["points", "wall_handles"] if name == "TBANWIN" else ["points"]
        recipe = "creation"
    else:
        required, recipe = [], None
    result["execution_contract"] = {"automated_recipe_available": recipe is not None,
        "recipe": recipe, "required_parameters": required,
        "manual_start_available": entry["automation"] == "interactive",
        "all_modes_verified": False}
    if native_recipe(name) is not None:
        result["execution_contract"]["recipe_verified_live"] = native_recipe(name).live_verified
    elif recipe is not None:
        result["execution_contract"]["recipe_verified_live"] = name not in LIVE_PENDING_TRANSFORMS | LIVE_PENDING_MEASUREMENTS | LIVE_PENDING_PROPERTIES
    if name in MEASUREMENT_RECIPES:
        result["execution_contract"].update(operation="measure",writes_drawing=False,supported_mode="measurement_without_annotation")
    if name in LAYER_SELECTION_RECIPES:
        result["execution_contract"].update(recipe="typed_layer_state")
    if name in VERIFIED_TEMPLATE_RECIPES:
        result["execution_contract"].update(parameter_fields=list(TEMPLATE_RECIPES[name]["numeric_properties"]),
            supported_shape="axis_aligned_rectangular_column" if name=="TGCOLUMN" else "axis_aligned_rectangle", template_required=True)
        if name=="TGCOLUMN":
            result["execution_contract"].update(additional_shapes={
                "circle":{"parameters":["shape","diameter","height"],"recipe_verified_live":True},
                "hexagon":{"parameters":["shape","side_length","height"],"recipe_verified_live":True},
                "octagon":{"parameters":["shape","side_length","height"],"recipe_verified_live":True}},
                source_template_shape="axis_aligned_rectangle",additional_shape_rotation=0)
    elif name in TEMPLATE_RECIPES:
        result["execution_contract"].update(candidate_recipe="initialized_template_copy",
            candidate_required_parameters=["template_handle","points","parameters"],
            candidate_parameter_fields=list(TEMPLATE_RECIPES[name]["numeric_properties"]),
            candidate_verification="pending_live_initialized_template_test")
    if name in PROPERTY_RECIPES:
        result["execution_contract"]["parameter_fields"] = list(PROPERTY_RECIPES[name])
        if name in {"TMARKEXTWALL","TMARKINTWALL"}:
            result["execution_contract"].update(recipe="native_wall_classification",
                classification_property="IsExternal",side_property="IOFeature",
                supported_mode="single_wall_skin_pick" if name=="TMARKEXTWALL" else "selected_initialized_walls",
                automatic_rollback=False,uncertain_send_retry=False,
                uncovered_modes=["curtain_walls","double_exterior","automatic_building_boundary_recognition"])
        if name in {"TMARKEXTWALL","TMARKINTWALL","TCHEWALLHEIGHT","TEXTTHICK"}:
            result["execution_contract"]["live_verified_fixture"]="isolated_horizontal_straight_T30_wall_in_world_top_view"
    if name=="TOPENING":
        result["execution_contract"]["candidate_property_operation"]={"operation":"set_opening",
            "parameters":["kind","width","height","sill"],"kind_is_precondition":True,
            "host_association_verified":False,"ordinary_window_verified_live":False,
            "uncovered_modes":["door_window_conversion","special_openings","host_reassignment"]}
    if name in TRANSFORM_RECIPES:
        if name in DISTRIBUTION_RECIPES:
            result["execution_contract"].update(recipe="equal_edge_spacing",parameter_fields=["spacing"],supported_mode="edge_gap_in_world_top_view")
            if name in LIVE_PENDING_TRANSFORMS:result["execution_contract"]["candidate_verification"]="pending_live_spacing_test"
        else:
            result["execution_contract"].update(parameter_fields=["offset"], supported_mode="translation_only")
    return result


def compile_selection_recipe(entry: dict, handles: list[str]) -> str:
    """The six manual section 18.3 alignment commands share one selection prompt."""
    command = entry["command"].upper()
    if command not in ALIGNMENT_RECIPES and command not in SELECTION_RECIPES:
        raise TianzhengRecipeUnavailable("No complete selection recipe for this command")
    minimum = 2 if command in ALIGNMENT_RECIPES else 1
    if not isinstance(handles, list) or len(handles) < minimum:
        raise TianzhengInputError(f"Recipe requires at least {minimum} distinct existing handles")
    selection = encode_response(entry, {"type": "selection", "value": handles})
    if len({h.upper() for h in handles}) != len(handles):
        raise TianzhengInputError("Alignment requires distinct handles")
    if command == "TMODELEV":
        # Supply a typed selection-set token to the command processor rather
        # than text at the custom selection prompt. No caller LISP is accepted.
        return f'(command-s "{entry["command"]}" {selection.strip()} "")\n'
    return entry["command"] + "\n" + selection + "\n"


def _curve_key(entity: dict) -> tuple:
    kind = entity["native_class"].upper()
    if kind == "ACDBLINE":
        points = sorted([tuple(round(v, 8) for v in entity[key]) for key in ("start_point", "end_point")])
        return kind, entity["Layer"], tuple(points)
    if kind == "ACDBCIRCLE":
        normal=list(entity.get("normal",[0,0,1]))
        first=next((v for v in normal if abs(v)>1e-8),1)
        if first<0:normal=[-v for v in normal]
        return kind, entity["Layer"], tuple(round(v, 8) for v in entity["center"]), round(entity["radius"], 8),tuple(round(v,8) for v in normal)
    raise TianzhengInputError("Exact duplicate cleanup currently requires lines or circles")


def verify_selection_mutation(command: str, handles: list[str], before: dict, after: dict,
                              old_environment: dict, new_environment: dict) -> bool:
    selected = [h.upper() for h in handles]
    try:
        if command == "TREVPLINE":
            for h in selected:
                old, new = before[h], after[h]
                stride = 2 if old["native_class"].upper() == "ACDBPOLYLINE" else 3
                vertices = [old["coordinates"][i:i+stride] for i in range(0, len(old["coordinates"]), stride)]
                reversed_coordinates = [v for point in reversed(vertices) for v in point]
                if old.get("closed") or new["coordinates"] != reversed_coordinates:
                    return False
                if old["bounds"] != new["bounds"] or old["Layer"] != new["Layer"]:
                    return False
                if "bulges" in old and new.get("bulges",[])[:-1] != [-v for v in reversed(old["bulges"][:-1])]:
                    return False
            return True
        if command == "TREMOVEDUP":
            expected = {_curve_key(before[h]) for h in selected}
            actual = [e for h, e in after.items() if h in selected or h not in before]
            return (len(actual) == len(expected) and {_curve_key(e) for e in actual} == expected
                    and all(after.get(h) == e for h, e in before.items() if h not in selected))
        if command == "TMODELEV":
            for h in selected:
                old, new = before[h], after[h]
                if new["bounds"]["min"][2] != 0 or new["bounds"]["max"][2] != 0:
                    return False
                for k in ("min", "max"):
                    if old["bounds"][k][:2] != new["bounds"][k][:2]:
                        return False
                for k in ("start_point", "end_point", "center"):
                    if k in old and new[k] != [*old[k][:2], 0]:
                        return False
                if old["native_class"] != new["native_class"] or old["Layer"] != new["Layer"]:
                    return False
            return all(after.get(h) == e for h,e in before.items() if h not in selected)
        if command in {"TMKHIDE", "TLOCVIS", "TUNHIDE"}:
            if set(before) != set(after):
                return False
            for h, old in before.items():
                desired = h in selected if command == "TLOCVIS" else False if command == "TMKHIDE" and h in selected else True if command == "TUNHIDE" and h in selected else old["visible"]
                if after[h] != dict(old, visible=desired):
                    return False
            return True
        if command in {"TLOCKLAYER", "TLOCKOTHERLAYER", "TUNLOCKLAYER"}:
            layers = {before[h]["Layer"] for h in selected}
            for name, old in old_environment["layers"].items():
                desired = (name not in layers) if command == "TLOCKOTHERLAYER" else True if command == "TLOCKLAYER" and name in layers else False if command == "TUNLOCKLAYER" and name in layers else old["locked"]
                new = new_environment["layers"][name]
                if new["locked"] != desired or any(new[k] != old[k] for k in ("on", "frozen", "color")):
                    return False
            return before == after
        if command in LAYER_SELECTION_RECIPES:
            layers = {before[h]["Layer"] for h in selected}
            field,value,other = LAYER_SELECTION_RECIPES[command]
            for name,old in old_environment["layers"].items():
                affected = name not in layers if other else name in layers
                expected = value if affected else old[field]
                new = new_environment["layers"][name]
                if new[field]!=expected or any(new[k]!=old[k] for k in ("on","frozen","locked","color") if k!=field):return False
            return before==after and old_environment["current_layer"]==new_environment["current_layer"]
    except (KeyError, TypeError, TianzhengInputError):
        return False
    return False


def verify_global_visibility(before: dict, after: dict) -> bool:
    return bool(before) and set(before) == set(after) and all(after[h] == dict(e, visible=True) for h,e in before.items())


def verify_global_layers(command: str, old_environment: dict, new_environment: dict) -> bool:
    field, value = GLOBAL_LAYER_RECIPES[command]
    try:
        for name, old in old_environment["layers"].items():
            new = new_environment["layers"][name]
            expected = not old[field] if value == "toggle" else value
            if new[field] != expected or any(new[k] != old[k] for k in ("on", "frozen", "locked", "color") if k != field):
                return False
        return old_environment["current_layer"] == new_environment["current_layer"]
    except (KeyError, TypeError):
        return False


def verify_alignment(command: str, handles: list[str], before: dict, after: dict) -> bool:
    """Check the manual's target and preserved size/perpendicular position."""
    axis, mode = ALIGNMENT_RECIPES[command.upper()]
    try:
        old = [before[h.upper()]["bounds"] for h in handles]
        new = [after[h.upper()]["bounds"] for h in handles]
        lower = min(b["min"][axis] for b in old)
        upper = max(b["max"][axis] for b in old)
        target = lower if mode == "min" else upper if mode == "max" else (lower + upper) / 2
        for a, b in zip(old, new):
            actual = b[mode][axis] if mode != "center" else (b["min"][axis] + b["max"][axis]) / 2
            if not math.isclose(actual, target, abs_tol=1e-5, rel_tol=0):
                return False
            for dim in range(3):
                if not math.isclose(a["max"][dim] - a["min"][dim], b["max"][dim] - b["min"][dim], abs_tol=1e-5, rel_tol=0):
                    return False
                if dim != axis and any(not math.isclose(a[k][dim], b[k][dim], abs_tol=1e-5, rel_tol=0) for k in ("min", "max")):
                    return False
        return True
    except (KeyError, IndexError, TypeError):
        return False


def compile_drawing_recipe(entry: dict, points: list, wall_handles: list | None = None) -> str:
    """Complete, audited prompt sequences for ordinary T30 creation modes.

    Modern SendCommand waits for the whole command to finish. Avoid exposing a
    raw token batch: only creation recipes with documented prompt order qualify.
    """
    command = entry["command"].upper()
    if command not in {"TGWALL", "TOPENING", "TBANWIN"}:
        raise TianzhengRecipeUnavailable("No complete drawing recipe for this command; use start for manual interaction")
    minimum = 2 if command in {"TGWALL", "TBANWIN"} else 1
    if not isinstance(points, list) or not minimum <= len(points) <= 64:
        raise TianzhengInputError(f"This recipe requires {minimum} to 64 coordinates")
    values = [encode_response(entry, {"type": "point", "value": point}) for point in points]
    if command == "TBANWIN":
        if len(points) != 2:
            raise TianzhengInputError("Band window needs exactly two wall positions")
        selection = encode_response(entry, {"type":"selection", "value":wall_handles})
        return entry["command"] + "\n" + "".join(values) + selection + "\n"
    if wall_handles is not None:
        raise TianzhengInputError("wall_handles is only supported by TBanWin")
    if command == "TGWALL" and any(points[i] == points[i+1] for i in range(len(points)-1)):
        raise TianzhengInputError("Consecutive wall points must differ")
    # Wall: end the chain, then end the command's next-chain prompt. Opening:
    # end the repeated placement prompt after the final insertion.
    ending = "\n\n" if command == "TGWALL" else "\n"
    return entry["command"] + "\n" + "".join(values) + ending
