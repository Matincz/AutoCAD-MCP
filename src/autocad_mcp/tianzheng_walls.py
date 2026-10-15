"""Bounded two-wall corner commands (native T30 prompts, fixed token sequence).

Scope: two straight, symmetric (LeftWidth == RightWidth), world-axis-aligned
T30 walls, one horizontal and one vertical, whose centrelines meet beyond or at
one end of each wall (an L corner, not a crossing or T junction). The command
picks each wall by a typed world point, so both points must be visible in the
current untwisted top view and must not fall within any other object's bounds.
"""
import math

from autocad_mcp.tianzheng import TianzhengInputError, encode_response, finite_number

WALL_CORNER_RECIPES = {"TFILLET", "TCHAMFER"}
# TFillet r=0 and r=500 verified live 2026-10-15.
LIVE_PENDING_WALL_CORNERS: set[str] = set()  # TChamfer 1200/600 verified live 2026-10-15
TOLERANCE = 1e-4
PARAMETERS = {"TFILLET": ("radius",), "TCHAMFER": ("distance1", "distance2")}


def validate_wall_corner_recipe(entry, data):
    command = entry["command"].upper()
    names = PARAMETERS[command]
    if set(data) - {"command", "selection_handles", "points", "parameters"}:
        raise TianzhengInputError(f"Wall corner recipes accept only selection_handles, points and parameters {names}")
    handles = data.get("selection_handles")
    encode_response(entry, {"type": "selection", "value": handles})
    if len(handles) != 2 or len({h.upper() for h in handles}) != 2:
        raise TianzhengInputError("Wall corner recipes need exactly two distinct wall handles")
    points = data.get("points")
    if not isinstance(points, list) or len(points) != 2:
        raise TianzhengInputError("Wall corner recipes need one world-coordinate pick point on each wall, in selection order")
    for point in points:
        encode_response(entry, {"type": "point", "value": point})
    parameters = data.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != set(names):
        raise TianzhengInputError(f"{entry['command']} requires parameters: {list(names)}")
    for name in names:
        finite_number(parameters[name])
        if command == "TFILLET" and parameters[name] < 0:
            raise TianzhengInputError("radius must be zero or positive")
        if command == "TCHAMFER" and parameters[name] <= 0:
            raise TianzhengInputError("Chamfer distances must be positive; use TFillet radius 0 to join a corner")
    return {"command": entry["command"], "selection_handles": [h.upper() for h in handles],
            "points": [[float(v) for v in p] for p in points],
            "parameters": {name: float(parameters[name]) for name in names}}


def corner_trims(data):
    """Centreline distance from the intersection removed from each selected wall, in selection order."""
    parameters = data["parameters"]
    if data["command"].upper() == "TFILLET":
        return [parameters["radius"]] * 2, "arc" if parameters["radius"] > 0 else None
    return [parameters["distance1"], parameters["distance2"]], "line"


def corner_sequence(entry, data):
    first, second = (encode_response(entry, {"type": "point", "value": p}) for p in data["points"])
    if entry["command"].upper() == "TFILLET":
        radius = encode_response(entry, {"type": "number", "value": data["parameters"]["radius"]})
        # Manual 6.3.1: R sets the radius, then pick wall one and wall two; the command ends itself.
        return f'{entry["command"]}\nR\n{radius}{first}{second}'
    one, two = (encode_response(entry, {"type": "number", "value": data["parameters"][k]}) for k in ("distance1", "distance2"))
    # Manual 6.3.2: D, distance 1, distance 2, first wall, second wall. Live
    # 2026-10-15: a trailing ESC reached T30 still inside TChamfer and aborted it
    # with COM "输入无效" (drawing unchanged), so the prompt repeats after the
    # second wall. Both possible prompts default to <退出>, so one Enter ends it.
    return f'{entry["command"]}\nD\n{one}{two}{first}{second}\n'


def _orientation(item):
    """Return (axis_along, centre) for a straight symmetric axis-aligned wall."""
    low, high = item["bounds"]["min"], item["bounds"]["max"]
    width = float(item["left_width"]) + float(item["right_width"])
    dx, dy = high[0] - low[0], high[1] - low[1]
    if math.isclose(dy, width, abs_tol=TOLERANCE) and dx > width + TOLERANCE:
        return 0, (low[1] + high[1]) / 2
    if math.isclose(dx, width, abs_tol=TOLERANCE) and dy > width + TOLERANCE:
        return 1, (low[0] + high[0]) / 2
    raise TianzhengInputError("Wall recipe needs world-axis-aligned straight walls whose bounds match their width")


def _straight_wall(before, handle, point, on_face=False):
    """Validate one straight symmetric axis-aligned wall and its pick point."""
    item = before.get(handle)
    if item is None:
        raise TianzhengInputError("Wall handles must belong to current model space")
    if item["native_class"].upper() != "TDBWALL" or "bounds" not in item:
        raise TianzhengInputError("Wall recipes require initialized T30 walls with readable bounds")
    if abs(float(item.get("radius") or 0.0)) > TOLERANCE:
        raise TianzhengInputError("Arc walls are outside this wall recipe")
    if not math.isclose(float(item["left_width"]), float(item["right_width"]), abs_tol=TOLERANCE):
        raise TianzhengInputError("Wall recipes currently require symmetric walls (LeftWidth == RightWidth)")
    low, high = item["bounds"]["min"], item["bounds"]["max"]
    slack = -TOLERANCE if on_face else TOLERANCE
    if not all(low[i] + slack < point[i] < high[i] - slack for i in (0, 1)):
        raise TianzhengInputError("Each pick point must lie inside its own wall's XY bounds")
    return _orientation(item)


def _check_picks(before, handles, points, view):
    # Typed points select through the screen pick box: require visibility and no ambiguity.
    margin = view["pick_margin"]
    for point in points:
        if not all(view["min"][i] < point[i] < view["max"][i] for i in (0, 1)):
            raise TianzhengInputError("Pick points must be visible in the current view; zoom the test area first")
        for handle, item in before.items():
            if handle in handles or "bounds" not in item:
                continue
            low, high = item["bounds"]["min"], item["bounds"]["max"]
            if all(low[i] - margin <= point[i] <= high[i] + margin for i in (0, 1)):
                raise TianzhengInputError(f"Pick point is ambiguous: object {handle} is within the pick box")


def plan_corner(before, handles, points, trims, connector, view):
    """Check preconditions on a fresh snapshot and derive the expected result."""
    walls = [(handle, before.get(handle), *_straight_wall(before, handle, point)) for handle, point in zip(handles, points)]
    if walls[0][2] == walls[1][2]:
        raise TianzhengInputError("Parallel walls (automatic connecting arc) are outside this fillet recipe")
    horizontal = walls[0] if walls[0][2] == 0 else walls[1]
    vertical = walls[1] if horizontal is walls[0] else walls[0]
    corner = [vertical[3], horizontal[3]]
    trim = dict(zip(handles, trims))
    expected = {}
    for handle, item, axis, _ in (horizontal, vertical):
        low, high = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
        target = corner[axis]
        if low + TOLERANCE < target < high - TOLERANCE:
            raise TianzhengInputError("Crossing or T-junction walls are outside this fillet recipe")
        direction = 1.0 if target >= high - TOLERANCE else -1.0
        end = target - direction * trim[handle]
        new_low, new_high = (low, end) if direction > 0 else (end, high)
        if new_high - new_low <= TOLERANCE:
            raise TianzhengInputError("Radius/distance consumes a whole wall; choose a smaller value")
        expected[handle] = {"axis": axis, "range": [new_low, new_high], "direction": direction}
    if connector:
        keys = ("left_width", "right_width", "material", "height", "elevation")
        if any(horizontal[1].get(k) != vertical[1].get(k) for k in keys):
            raise TianzhengInputError("A connecting arc/chamfer wall needs walls with equal widths, material, height and elevation (manual 6.3.1)")
    # Manual 6.3.1: never fillet again where an earlier arc wall (or any other
    # object) already occupies the corner; the caller must delete it first.
    half = float(horizontal[1]["left_width"])
    region_low, region_high = [], []
    for axis, wall in ((0, horizontal), (1, vertical)):
        bounds = wall[1]["bounds"]
        near = bounds["max"][axis] if expected[wall[0]]["direction"] > 0 else bounds["min"][axis]
        region_low.append(min(near, corner[axis] - half))
        region_high.append(max(near, corner[axis] + half))
    for handle, item in before.items():
        if handle in handles or "bounds" not in item:
            continue
        low, high = item["bounds"]["min"], item["bounds"]["max"]
        if all(low[i] < region_high[i] - TOLERANCE and high[i] > region_low[i] + TOLERANCE for i in (0, 1)):
            raise TianzhengInputError(f"Corner region is occupied by object {handle} (e.g. an earlier fillet arc wall); delete it before filleting again")
    _check_picks(before, handles, points, view)
    return {"corner": corner, "connector": connector, "trims": [trim[horizontal[0]], trim[vertical[0]]], "walls": expected,
            "half_width": float(horizontal[1]["left_width"]),
            # Per world axis: direction from the wall body toward the corner.
            "signs": [expected[horizontal[0]]["direction"], expected[vertical[0]]["direction"]]}


def _same_except(a, b, ignored):
    keys = (set(a) | set(b)) - ignored
    for key in keys:
        left, right = a.get(key), b.get(key)
        if isinstance(left, float) and isinstance(right, float):
            if not math.isclose(left, right, abs_tol=TOLERANCE):
                return False
        elif left != right:
            return False
    return True


def verify_corner(plan, before, after):
    """Return a list of failed checks; empty means every postcondition held."""
    failures = []
    created = [item for handle, item in after.items() if handle not in before]
    if sorted(set(before) - set(after)):
        failures.append("objects_removed")
    for handle, item in before.items():
        if handle in plan["walls"] or handle not in after:
            continue
        if after[handle] != item:
            failures.append(f"unselected_changed:{handle}")
    for handle, spec in plan["walls"].items():
        result = after.get(handle)
        if result is None:
            continue
        if not _same_except(before[handle], result, {"bounds"}):
            failures.append(f"wall_properties_changed:{handle}")
        axis, (low, high) = spec["axis"], spec["range"]
        bounds = result.get("bounds")
        other = 1 - axis
        if not bounds or not (math.isclose(bounds["min"][axis], low, abs_tol=1e-3) and math.isclose(bounds["max"][axis], high, abs_tol=1e-3)):
            failures.append(f"wall_extent_mismatch:{handle}")
        elif not all(math.isclose(bounds[k][i], before[handle]["bounds"][k][i], abs_tol=TOLERANCE)
                     for k in ("min", "max") for i in (other, 2)):
            failures.append(f"wall_cross_section_changed:{handle}")
    if not plan["connector"]:
        if created:
            failures.append("unexpected_created_objects")
        return failures
    walls = [item for item in created if item["native_class"].upper() == "TDBWALL"]
    if len(created) != 1 or len(walls) != 1:
        failures.append("expected_exactly_one_connecting_wall")
        return failures
    joint = walls[0]
    source = before[next(iter(plan["walls"]))]
    for key in ("left_width", "right_width", "height", "elevation", "material"):
        if joint.get(key) != source.get(key) and not (
                isinstance(joint.get(key), float) and isinstance(source.get(key), float)
                and math.isclose(joint[key], source[key], abs_tol=TOLERANCE)):
            failures.append(f"connector_property_mismatch:{key}")
    w = plan["half_width"]
    if plan["connector"] == "arc":
        r = plan["trims"][0]
        if not math.isclose(float(joint.get("radius") or 0.0), r, abs_tol=1e-3):
            failures.append("arc_radius_mismatch")
        # Quarter arc tangent to both centrelines: its XY extent spans from each
        # tangent point to the outer wall face beyond the corner.
        extents = []
        for axis, sign in enumerate(plan["signs"]):
            extents.append(sorted((plan["corner"][axis] - sign * r, plan["corner"][axis] + sign * w)))
    else:
        if abs(float(joint.get("radius") or 0.0)) > TOLERANCE:
            failures.append("chamfer_wall_is_not_straight")
        # Straight wall between the two trimmed centreline ends with square ends.
        start = [plan["corner"][0] - plan["signs"][0] * plan["trims"][0], plan["corner"][1]]
        end = [plan["corner"][0], plan["corner"][1] - plan["signs"][1] * plan["trims"][1]]
        length = math.dist(start, end)
        offset = [w * abs(end[1] - start[1]) / length, w * abs(end[0] - start[0]) / length]
        extents = [[min(start[a], end[a]) - offset[a], max(start[a], end[a]) + offset[a]] for a in (0, 1)]
    bounds = joint.get("bounds")
    for axis, (low, high) in enumerate(extents):
        if not bounds or not (math.isclose(bounds["min"][axis], low, abs_tol=1e-3) and math.isclose(bounds["max"][axis], high, abs_tol=1e-3)):
            failures.append(f"connector_extent_mismatch:{'xy'[axis]}")
    return failures


WALL_SPLIT_RECIPES = {"TKATANA"}
LIVE_PENDING_WALL_SPLITS: set[str] = set()  # tkatana verified live 2026-10-15


def validate_wall_split_recipe(entry, data):
    if set(data) - {"command", "selection_handles", "points"}:
        raise TianzhengInputError("Wall split accepts only selection_handles (one wall) and points (one point on it)")
    handles, points = data.get("selection_handles"), data.get("points")
    encode_response(entry, {"type": "selection", "value": handles})
    if len(handles) != 1 or not isinstance(points, list) or len(points) != 1:
        raise TianzhengInputError("Wall split needs exactly one wall handle and one split point")
    encode_response(entry, {"type": "point", "value": points[0]})
    return {"command": entry["command"], "selection_handles": [handles[0].upper()],
            "points": [[float(v) for v in points[0]]]}


def split_sequence(entry, data):
    point = encode_response(entry, {"type": "point", "value": data["points"][0]})
    # Manual 6.2.2: the wall prompt repeats until Enter (<退出>).
    return f'{entry["command"]}\n{point}\n'


def plan_split(before, handle, point, view):
    axis, centre = _straight_wall(before, handle, point)
    item = before[handle]
    width = float(item["left_width"]) + float(item["right_width"])
    if abs(point[1 - axis] - centre) > TOLERANCE:
        raise TianzhengInputError("Split point must lie on the wall centreline")
    low, high = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
    if not (low + width < point[axis] < high - width):
        raise TianzhengInputError("Split point must be more than one wall width from each wall end")
    _check_picks(before, [handle], [point], view)
    return {"handle": handle, "axis": axis, "ranges": [[low, point[axis]], [point[axis], high]]}


def verify_split(plan, before, after):
    failures = []
    handle, axis = plan["handle"], plan["axis"]
    if set(before) - set(after):
        failures.append("objects_removed")
    for key, item in before.items():
        if key != handle and key in after and after[key] != item:
            failures.append(f"unselected_changed:{key}")
    created = [item for key, item in after.items() if key not in before]
    walls = [item for item in created if item["native_class"].upper() == "TDBWALL"]
    # Manual 6.2.2 / live 2026-10-15: T30 also adds a GlwKatana break line
    # (zero length along the wall, spanning its width at the split point).
    marks = [item for item in created if item["native_class"].upper() == "GLWKATANA"]
    if len(created) != 2 or len(walls) != 1 or len(marks) != 1:
        failures.append("expected_one_new_wall_and_one_break_line")
        return failures
    mark, source = marks[0].get("bounds"), before[handle]["bounds"]
    split_at = plan["ranges"][0][1]
    if not mark or not all(math.isclose(mark[k][axis], split_at, abs_tol=1e-3) for k in ("min", "max")) or not all(
            math.isclose(mark[k][1 - axis], source[k][1 - axis], abs_tol=1e-3) for k in ("min", "max")):
        failures.append("break_line_position_mismatch")
    pieces = [after.get(handle), walls[0]]
    if pieces[0] is None:
        return failures
    ranges = sorted([p["bounds"]["min"][axis], p["bounds"]["max"][axis]] for p in pieces)
    for actual, expected in zip(ranges, plan["ranges"]):
        if not all(math.isclose(a, b, abs_tol=1e-3) for a, b in zip(actual, expected)):
            failures.append("piece_extent_mismatch")
    for piece in pieces:
        if not _same_except(before[handle], piece, {"bounds", "handle"}):
            failures.append(f"piece_properties_changed:{piece['handle']}")
        if not all(math.isclose(piece["bounds"][k][i], before[handle]["bounds"][k][i], abs_tol=TOLERANCE)
                   for k in ("min", "max") for i in (1 - axis, 2)):
            failures.append(f"piece_cross_section_changed:{piece['handle']}")
    return failures


WALL_OFFSET_RECIPES = {"TOFFSET"}
LIVE_PENDING_WALL_OFFSETS: set[str] = set()  # TOffset both sides verified live 2026-10-15


def validate_wall_offset_recipe(entry, data):
    if set(data) - {"command", "selection_handles", "points", "parameters"}:
        raise TianzhengInputError("Clear-distance offset accepts only selection_handles, points and parameters.distance")
    handles, points, parameters = data.get("selection_handles"), data.get("points"), data.get("parameters")
    encode_response(entry, {"type": "selection", "value": handles})
    if len(handles) != 1 or not isinstance(points, list) or len(points) != 1:
        raise TianzhengInputError("Clear-distance offset needs one wall handle and one side pick point on it")
    encode_response(entry, {"type": "point", "value": points[0]})
    if not isinstance(parameters, dict) or set(parameters) != {"distance"}:
        raise TianzhengInputError("Clear-distance offset requires parameters: {distance}")
    finite_number(parameters["distance"])
    if parameters["distance"] <= 0:
        raise TianzhengInputError("Clear distance must be positive")
    return {"command": entry["command"], "selection_handles": [handles[0].upper()],
            "points": [[float(v) for v in points[0]]], "parameters": {"distance": float(parameters["distance"])}}


def offset_sequence(entry, data):
    distance = encode_response(entry, {"type": "number", "value": data["parameters"]["distance"]})
    point = encode_response(entry, {"type": "point", "value": data["points"][0]})
    # Manual 6.3.6: distance, wall side pick (prompt repeats), Enter draws and ends.
    return f'{entry["command"]}\n{distance}{point}\n'


def plan_offset(before, handle, point, distance, view):
    axis, centre = _straight_wall(before, handle, point, on_face=True)
    item = before[handle]
    half = float(item["left_width"])
    side = point[1 - axis] - centre
    # Live 2026-10-15: a point inside the wall body is not taken as a side; the
    # pick must be on the chosen face line (as for TMarkExtWall's skin pick).
    if not math.isclose(abs(side), half, abs_tol=TOLERANCE):
        raise TianzhengInputError("Side pick point must lie on the chosen wall face line")
    low_end, high_end = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
    if not (low_end + 2 * half < point[axis] < high_end - 2 * half):
        raise TianzhengInputError("Side pick point must be away from the wall ends")
    sign = 1.0 if side > 0 else -1.0
    new_centre = centre + sign * (2 * half + distance)
    _check_picks(before, [handle], [point], view)
    low, high = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
    # The new wall's footprint must be free so the expected result is unambiguous.
    for key, other in before.items():
        if key == handle or "bounds" not in other:
            continue
        o_low, o_high = other["bounds"]["min"], other["bounds"]["max"]
        cross = (new_centre - half - distance / 2, new_centre + half)
        if sign < 0:
            cross = (new_centre - half, new_centre + half + distance / 2)
        if o_low[axis] < high and o_high[axis] > low and o_low[1 - axis] < cross[1] and o_high[1 - axis] > cross[0]:
            raise TianzhengInputError(f"Offset target area is occupied by object {key}")
    return {"handle": handle, "axis": axis, "range": [low, high], "cross": [new_centre - half, new_centre + half], "distance": distance}


def verify_offset(plan, before, after):
    failures = []
    if set(before) - set(after):
        failures.append("objects_removed")
    for key, item in before.items():
        if key in after and after[key] != item:
            failures.append(f"existing_changed:{key}")
    created = [item for key, item in after.items() if key not in before]
    if len(created) != 1 or created[0]["native_class"].upper() != "TDBWALL":
        failures.append("expected_exactly_one_new_wall")
        return failures
    wall, axis = created[0], plan["axis"]
    if not _same_except(before[plan["handle"]], wall, {"bounds", "handle"}):
        failures.append("new_wall_properties_differ")
    bounds = wall.get("bounds")
    expected = {axis: plan["range"], 1 - axis: plan["cross"]}
    for index, (low, high) in expected.items():
        if not bounds or not (math.isclose(bounds["min"][index], low, abs_tol=1e-3) and math.isclose(bounds["max"][index], high, abs_tol=1e-3)):
            failures.append(f"new_wall_extent_mismatch:{'xy'[index]}")
    return failures


WALL_CONVERSION_RECIPES = {"TCONVERTCURTAIN"}
CONVERSION_ENABLED = False
LIVE_PENDING_WALL_CONVERSIONS = {"TCONVERTCURTAIN"}
# Manual 6.2.6 material menu; only codes whose stored Style string has been
# observed live are accepted.
# Live 2026-10-15: curtain->wall with code 4 timed out waiting at the main
# prompt (sequence/prompt order unconfirmed), so no code is accepted for now.
CURTAIN_TARGET_MATERIALS: dict[int, str] = {}
UNVERIFIED_CURTAIN_TARGET_MATERIALS = {4: "砖"}


def validate_wall_conversion_recipe(entry, data):
    # Live 2026-10-15: TConvertCurtain starts in the direction used last time
    # (sticky Q toggle), which cannot be read before sending, so any fixed
    # sequence can leave the command waiting at its own prompt. Disabled.
    if not CONVERSION_ENABLED:
        raise TianzhengInputError("TConvertCurtain is disabled: its starting direction is sticky and unreadable, so a fixed sequence can hang")
    if set(data) - {"command", "selection_handles", "parameters"}:
        raise TianzhengInputError("Curtain conversion accepts only selection_handles and parameters {direction, material?}")
    handles, parameters = data.get("selection_handles"), data.get("parameters")
    encode_response(entry, {"type": "selection", "value": handles})
    if len(handles) > 32 or len({h.upper() for h in handles}) != len(handles):
        raise TianzhengInputError("Curtain conversion accepts 1 to 32 distinct wall handles")
    if not isinstance(parameters, dict) or parameters.get("direction") not in {"to_curtain", "to_wall"}:
        raise TianzhengInputError("parameters.direction must be to_curtain or to_wall")
    if parameters["direction"] == "to_curtain" and set(parameters) != {"direction"}:
        raise TianzhengInputError("to_curtain accepts no other parameters")
    if parameters["direction"] == "to_wall":
        material = parameters.get("material")
        if set(parameters) != {"direction", "material"} or isinstance(material, bool) or material not in CURTAIN_TARGET_MATERIALS:
            raise TianzhengInputError("to_wall is disabled until its prompt sequence is live-verified (live-verified codes only)")
    return {"command": entry["command"], "selection_handles": [h.upper() for h in handles], "parameters": dict(parameters)}


def conversion_sequence(entry, data):
    selection = encode_response(entry, {"type": "selection", "value": data["selection_handles"]})
    if data["parameters"]["direction"] == "to_curtain":
        # Select, Enter ends selection and converts, Enter at the main prompt exits.
        return f'{entry["command"]}\n{selection}\n\n'
    material = encode_response(entry, {"type": "number", "value": data["parameters"]["material"]})
    # Q switches to curtain->wall, select, Enter, material code, Enter exits.
    return f'{entry["command"]}\nQ\n{selection}\n{material}\n'


def plan_conversion(before, handles, parameters):
    to_curtain = parameters["direction"] == "to_curtain"
    source_class = "TDBWALL" if to_curtain else "TDBCURTAINWALL"
    for handle in handles:
        item = before.get(handle)
        if item is None or item["native_class"].upper() != source_class or "bounds" not in item:
            raise TianzhengInputError(f"{parameters['direction']} needs initialized {'T30 walls' if to_curtain else 'T30 curtain walls'} with readable bounds")
    # Live 2026-10-15: conversion erases each object and creates a new one of
    # the other class with the same footprint, so footprints must be distinct.
    footprints = [_footprint(before[h]) for h in handles]
    for handle, item in before.items():
        if handle not in handles and "bounds" in item and _footprint(item) in footprints:
            raise TianzhengInputError(f"Object {handle} shares a selected footprint; result matching would be ambiguous")
    if len(set(footprints)) != len(footprints):
        raise TianzhengInputError("Selected walls must have distinct footprints")
    return {"handles": list(handles), "direction": parameters["direction"],
            "target_class": "TDBCURTAINWALL" if to_curtain else "TDBWALL",
            "material": None if to_curtain else CURTAIN_TARGET_MATERIALS[parameters["material"]]}


def _footprint(item):
    return tuple(round(item["bounds"][k][i], 3) for k in ("min", "max") for i in (0, 1, 2))


def verify_conversion(plan, before, after):
    failures = []
    selected = set(plan["handles"])
    removed = set(before) - set(after)
    if removed != selected:
        failures.append("removed_objects_differ_from_selection")
    for key, item in before.items():
        if key not in selected and after.get(key) != item:
            failures.append(f"unselected_changed:{key}")
    created = {_footprint(item): item for key, item in after.items() if key not in before}
    if len(created) != len(selected) or len([k for k in after if k not in before]) != len(selected):
        failures.append("created_count_mismatch")
    for key in plan["handles"]:
        source = before[key]
        result = created.get(_footprint(source))
        if result is None:
            failures.append(f"replacement_missing:{key}")
            continue
        if result["native_class"].upper() != plan["target_class"]:
            failures.append(f"replacement_class_mismatch:{key}")
        if plan["material"] is not None and result.get("material") != plan["material"]:
            failures.append(f"material_mismatch:{key}")
        for field in ("height", "elevation", "left_width", "right_width", "io_feature"):
            if field in source and result.get(field) != source[field] and not (
                    isinstance(source[field], float) and isinstance(result.get(field), float)
                    and math.isclose(source[field], result[field], abs_tol=TOLERANCE)):
                failures.append(f"preserved_field_changed:{key}.{field}")
    return failures


WALL_RECIPES = WALL_CORNER_RECIPES | WALL_SPLIT_RECIPES | WALL_OFFSET_RECIPES | WALL_CONVERSION_RECIPES
LIVE_PENDING_WALLS = LIVE_PENDING_WALL_CORNERS | LIVE_PENDING_WALL_SPLITS | LIVE_PENDING_WALL_OFFSETS | LIVE_PENDING_WALL_CONVERSIONS


def validate_wall_recipe(entry, data):
    if entry["command"].upper() in WALL_SPLIT_RECIPES:
        return validate_wall_split_recipe(entry, data)
    if entry["command"].upper() in WALL_OFFSET_RECIPES:
        return validate_wall_offset_recipe(entry, data)
    if entry["command"].upper() in WALL_CONVERSION_RECIPES:
        return validate_wall_conversion_recipe(entry, data)
    return validate_wall_corner_recipe(entry, data)
