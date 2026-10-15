"""P0 command-line-only native recipes (no dialog, no remembered mode)."""
from __future__ import annotations

import math

from autocad_mcp.tianzheng import TianzhengInputError, encode_response, finite_number
from autocad_mcp.tianzheng_native import NativeRecipe, register
from autocad_mcp.tianzheng_walls import TOLERANCE, _check_picks, _orientation, _same_except

LIVE_VERIFIED: set[str] = set()


def _keys(data, allowed, message):
    if set(data) - ({"command"} | set(allowed)):
        raise TianzhengInputError(message)


def _handles(entry, data, minimum=1, maximum=64, key="selection_handles"):
    handles = data.get(key)
    encode_response(entry, {"type": "selection", "value": handles})
    if not minimum <= len(handles) <= maximum or len({h.upper() for h in handles}) != len(handles):
        raise TianzhengInputError(f"{key} needs {minimum} to {maximum} distinct handles")
    return [h.upper() for h in handles]


def _points(entry, data, count):
    points = data.get("points")
    if not isinstance(points, list) or len(points) != count:
        raise TianzhengInputError(f"points needs exactly {count} world coordinates")
    for point in points:
        encode_response(entry, {"type": "point", "value": point})
    return [[float(v) for v in p] for p in points]


def _classes(before, handles, allowed, message):
    for handle in handles:
        item = before.get(handle)
        if item is None or item["native_class"].upper() not in allowed or "bounds" not in item:
            raise TianzhengInputError(message)


def _unchanged_except(before, after, changing):
    failures = []
    if set(before) - set(after):
        failures.append("objects_removed")
    for key, item in before.items():
        if key not in changing and key in after and after[key] != item:
            fields = sorted(f for f in set(item) | set(after[key]) if item.get(f) != after[key].get(f))
            # A field present on only one side is a failed COM read, not a drawing change;
            # it still fails verification but is reported distinctly.
            if all((f in item) != (f in after[key]) for f in fields):
                failures.append(f"snapshot_read_incomplete:{key}:{','.join(fields)}")
            else:
                failures.append(f"unexpected_change:{key}:{','.join(fields)}")
    return failures


def _created(before, after):
    return [item for key, item in after.items() if key not in before]


def _close(a, b, tol=1e-3):
    return math.isclose(float(a), float(b), abs_tol=tol)


# --- TMarkWall 6.6.1: select building walls, Enter ends selection, Enter leaves highlight.

def _markwall_validate(entry, data):
    _keys(data, {"selection_handles", "parameters"}, "TMarkWall accepts selection_handles and parameters.expected_exterior")
    handles = _handles(entry, data)
    parameters = data.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"expected_exterior"}:
        raise TianzhengInputError("TMarkWall requires parameters.expected_exterior (handles that must become exterior)")
    expected = parameters["expected_exterior"]
    if not isinstance(expected, list) or any(not isinstance(h, str) or h.upper() not in handles for h in expected):
        raise TianzhengInputError("expected_exterior must be a subset of selection_handles")
    return {"command": entry["command"], "selection_handles": handles,
            "parameters": {"expected_exterior": sorted({h.upper() for h in expected})}}


def _markwall_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBWALL"}, "TMarkWall needs initialized T30 walls")
    return {"handles": data["selection_handles"], "exterior": set(data["parameters"]["expected_exterior"])}


def _markwall_verify(plan, before, after):
    failures = _unchanged_except(before, after, set(plan["handles"]))
    if _created(before, after):
        failures.append("unexpected_created_objects")
    for handle in plan["handles"]:
        item = after.get(handle)
        if item is None:
            continue
        if not _same_except(before[handle], item, {"is_external", "io_feature"}):
            failures.append(f"geometry_changed:{handle}")
        if handle in plan["exterior"]:
            if item.get("is_external") != "外墙" or item.get("io_feature") not in {"左外", "右外"}:
                failures.append(f"not_exterior:{handle}")
        elif item.get("is_external") != "内墙":
            failures.append(f"not_interior:{handle}")
    return failures


register("TMARKWALL", NativeRecipe("wall_inside_outside_recognition", ("selection_handles", "parameters"),
    _markwall_validate, _markwall_plan,
    lambda entry, data, plan: f'{entry["command"]}\n{encode_response(entry, {"type": "selection", "value": data["selection_handles"]})}\n\n',
    _markwall_verify, "expected_exterior_walls_recognized_one_side_others_interior_geometry_preserved"))


# --- TMirWinIO 7.3.5: select openings, Enter flips each about its wall centreline.

def _host_centre(before, opening):
    """Axis-aligned straight host wall containing the opening's footprint centre."""
    low, high = opening["bounds"]["min"], opening["bounds"]["max"]
    centre = [(low[0] + high[0]) / 2, (low[1] + high[1]) / 2]
    hosts = []
    for key, item in before.items():
        if item["native_class"].upper() != "TDBWALL" or "bounds" not in item or abs(float(item.get("radius") or 0)) > TOLERANCE:
            continue
        try:
            axis, line = _orientation(item)
        except TianzhengInputError:
            continue
        w_low, w_high = item["bounds"]["min"], item["bounds"]["max"]
        if w_low[axis] - TOLERANCE <= low[axis] and high[axis] <= w_high[axis] + TOLERANCE and low[1 - axis] - TOLERANCE <= line <= high[1 - axis] + TOLERANCE:
            hosts.append((key, axis, line))
    if len(hosts) != 1:
        raise TianzhengInputError("Each opening needs exactly one straight axis-aligned host wall")
    return hosts[0]


def _flip_validate(entry, data):
    _keys(data, {"selection_handles"}, "Opening flip accepts only selection_handles")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=32)}


def _flip_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBOPENING"}, "Opening flip needs initialized T30 openings")
    expected = {}
    for handle in data["selection_handles"]:
        item = before[handle]
        _, axis, line = _host_centre(before, item)
        cross = 1 - axis
        low, high = item["bounds"]["min"][cross], item["bounds"]["max"][cross]
        if _close(line - low, high - line):
            raise TianzhengInputError(f"Opening {handle} is symmetric about the wall; a flip would not be observable")
        expected[handle] = {"axis": cross, "range": [2 * line - high, 2 * line - low]}
    return expected


def _flip_verify(plan, before, after):
    failures = _unchanged_except(before, after, set(plan))
    if _created(before, after):
        failures.append("unexpected_created_objects")
    for handle, spec in plan.items():
        item = after.get(handle)
        if item is None:
            continue
        axis = spec["axis"]
        if not (_close(item["bounds"]["min"][axis], spec["range"][0]) and _close(item["bounds"]["max"][axis], spec["range"][1])):
            failures.append(f"not_mirrored_about_wall:{handle}")
        other = 1 - axis
        if not all(_close(item["bounds"][k][other], before[handle]["bounds"][k][other]) for k in ("min", "max")):
            failures.append(f"moved_along_wall:{handle}")
        for key in ("width", "height", "opening_kind", "opening_subkind", "window_sill", "Layer"):
            if before[handle].get(key) != item.get(key):
                failures.append(f"property_changed:{handle}.{key}")
    return failures


register("TMIRWINIO", NativeRecipe("opening_inside_outside_flip", ("selection_handles",),
    _flip_validate, _flip_plan,
    lambda entry, data, plan: f'{entry["command"]}\n{encode_response(entry, {"type": "selection", "value": data["selection_handles"]})}\n',
    _flip_verify, "openings_mirrored_about_host_wall_centreline_others_unchanged"))


# --- TDimWall 14.2.3: two points of a line crossing walls; one dimension object.

def _dimwall_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TDimWall accepts selection_handles (walls crossed) and two points")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=16), "points": _points(entry, data, 2)}


def _dimwall_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBWALL"}, "TDimWall needs the crossed initialized T30 walls")
    (x1, y1, *_), (x2, y2, *_) = data["points"]
    if not (_close(x1, x2, TOLERANCE) or _close(y1, y2, TOLERANCE)) or (_close(x1, x2, TOLERANCE) and _close(y1, y2, TOLERANCE)):
        raise TianzhengInputError("The measuring line must be a non-zero horizontal or vertical segment")
    axis = 0 if _close(y1, y2, TOLERANCE) else 1  # direction of the measuring line
    s_low, s_high = sorted((data["points"][0][axis], data["points"][1][axis]))
    fixed = data["points"][0][1 - axis]
    crossed = []
    for key, item in before.items():
        if "bounds" not in item:
            continue
        low, high = item["bounds"]["min"], item["bounds"]["max"]
        if low[1 - axis] - TOLERANCE <= fixed <= high[1 - axis] + TOLERANCE and low[axis] < s_high and high[axis] > s_low:
            crossed.append(key)
    if sorted(crossed) != sorted(data["selection_handles"]):
        raise TianzhengInputError(f"The line must cross exactly the listed walls and nothing else; it crosses {sorted(crossed)}")
    faces = []
    for handle in data["selection_handles"]:
        item = before[handle]
        wall_axis, _ = _orientation(item)
        if wall_axis == axis:
            raise TianzhengInputError("The measuring line must cross walls perpendicularly")
        low, high = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
        if not (s_low < low and high < s_high):
            raise TianzhengInputError("Both line ends must lie outside every crossed wall")
        faces.append((low, high))
    _check_picks(before, [], data["points"], view())
    return {"axis": axis, "faces": faces, "line": fixed}


def _dimwall_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1:
        return failures + ["expected_exactly_one_dimension"]
    bounds = created[0].get("bounds")
    axis = plan["axis"]
    span = (min(f[0] for f in plan["faces"]), max(f[1] for f in plan["faces"]))
    if not bounds or not (_close(bounds["min"][axis], span[0]) and _close(bounds["max"][axis], span[1])):
        failures.append("dimension_extent_differs_from_wall_faces")
    return failures


register("TDIMWALL", NativeRecipe("wall_thickness_dimension", ("selection_handles", "points"),
    _dimwall_validate, _dimwall_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"]),
    _dimwall_verify, "one_dimension_spanning_crossed_wall_faces_others_unchanged"))


# --- TNorthThumb 2.4.2: insertion point and direction angle (degrees, X axis = 0).

def _north_validate(entry, data):
    _keys(data, {"points", "parameters"}, "TNorthThumb accepts one point and parameters.angle")
    points = _points(entry, data, 1)
    parameters = data.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"angle"}:
        raise TianzhengInputError("TNorthThumb requires parameters.angle in degrees")
    finite_number(parameters["angle"])
    if not 0 <= parameters["angle"] < 360:
        raise TianzhengInputError("angle must be in [0, 360)")
    return {"command": entry["command"], "selection_handles": [], "points": points, "parameters": {"angle": float(parameters["angle"])}}


def _north_plan(before, data, view):
    point = data["points"][0]
    for key, item in before.items():
        if "bounds" in item and all(item["bounds"]["min"][i] - 3000 <= point[i] <= item["bounds"]["max"][i] + 3000 for i in (0, 1)):
            raise TianzhengInputError(f"Insertion point must be at least 3000 units clear of object {key}")
    return {"point": point, "angle": data["parameters"]["angle"]}


def _north_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1:
        return failures + ["expected_exactly_one_north_arrow"]
    bounds = created[0].get("bounds")
    if not bounds or not all(bounds["min"][i] - TOLERANCE <= plan["point"][i] <= bounds["max"][i] + TOLERANCE for i in (0, 1)):
        failures.append("arrow_does_not_contain_insertion_point")
        return failures
    # Live 2026-10-15: Rotation reads back in degrees (90 -> 90.0, 30 -> 30.0).
    # Bounds cannot carry the angle (the label may stay inside the circle).
    rotation = created[0].get("rotation")
    if rotation is None or abs(math.remainder(float(rotation) - plan["angle"], 360.0)) > 1e-6:
        failures.append("arrow_rotation_differs")
    return failures


register("TNORTHTHUMB", NativeRecipe("north_arrow_symbol", ("points", "parameters"),
    _north_validate, _north_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + encode_response(entry, {"type": "point", "value": data["points"][0]})
        + encode_response(entry, {"type": "number", "value": data["parameters"]["angle"]}),
    _north_verify, "one_symbol_at_insertion_point_others_unchanged"))


def _bounds_union(before, handles):
    lows = [before[h]["bounds"]["min"] for h in handles]
    highs = [before[h]["bounds"]["max"] for h in handles]
    return [min(v[i] for v in lows) for i in (0, 1)], [max(v[i] for v in highs) for i in (0, 1)]


def _clear_point(before, point, margin, message):
    for key, item in before.items():
        if "bounds" in item and all(item["bounds"]["min"][i] - margin <= point[i] <= item["bounds"]["max"][i] + margin for i in (0, 1)):
            raise TianzhengInputError(f"{message}: object {key} is within {margin} units")


# --- TQuickDim 14.2.9: select objects, Enter, dimension line position (default mode 整体).

def _quickdim_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TQuickDim accepts selection_handles and one dimension-line point")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=32), "points": _points(entry, data, 1)}


def _quickdim_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBWALL", "TDBCOLUMN", "TDBOPENING"}, "TQuickDim recipe accepts initialized T30 walls, columns and openings")
    low, high = _bounds_union(before, data["selection_handles"])
    point = data["points"][0]
    if not low[0] < point[0] < high[0] or low[1] - 200 < point[1] < high[1] + 200:
        raise TianzhengInputError("The point must lie within the selection's X span and at least 200 units above or below it (horizontal overall dimension)")
    view_window = view()
    if not all(view_window["min"][i] < point[i] < view_window["max"][i] for i in (0, 1)):
        raise TianzhengInputError("The dimension-line point must be visible in the current view")
    return {"span": [low[0], high[0]], "y": point[1]}


def _quickdim_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or not created[0]["native_class"].upper().startswith("TDBDIMENSION"):
        return failures + ["expected_exactly_one_t30_dimension"]
    bounds = created[0].get("bounds")
    if not bounds or not (_close(bounds["min"][0], plan["span"][0]) and _close(bounds["max"][0], plan["span"][1])):
        failures.append("dimension_span_differs_from_selection")
    elif not bounds["min"][1] - TOLERANCE <= plan["y"] <= bounds["max"][1] + TOLERANCE:
        failures.append("dimension_not_at_requested_line")
    return failures


register("TQUICKDIM", NativeRecipe("overall_dimension_default_mode", ("selection_handles", "points"),
    _quickdim_validate, _quickdim_plan,
    lambda entry, data, plan: f'{entry["command"]}\n{encode_response(entry, {"type": "selection", "value": data["selection_handles"]})}\n'
        + encode_response(entry, {"type": "point", "value": data["points"][0]}),
    _quickdim_verify, "one_horizontal_t30_dimension_spanning_selection_at_requested_line"))


# --- TStatOp 7.5.4: select openings, Enter, table top-left point.

def _statop_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TStatOp accepts opening selection_handles and one top-left point")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=64), "points": _points(entry, data, 1)}


def _statop_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBOPENING"}, "TStatOp needs initialized T30 openings")
    point = data["points"][0]
    _clear_point(before, point, 20000, "Table insertion area is not clear")
    groups = {}
    for handle in data["selection_handles"]:
        item = before[handle]
        if item.get("opening_subkind") is None or "width" not in item or "height" not in item:
            raise TianzhengInputError(f"Opening {handle} type/size is unreadable")
        key = (item["opening_subkind"], f'{round(item["width"])}X{round(item["height"])}')
        groups[key] = groups.get(key, 0) + 1
    return {"point": point, "groups": groups}


def _statop_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or created[0]["native_class"].upper() != "TDBSHEET":
        return failures + ["expected_exactly_one_t30_table"]
    table = created[0]
    bounds = table.get("bounds")
    # Live 2026-10-15: the point is the top-left of the title, which lies
    # outside the TDbSheet bounds; the body starts below it at the same X.
    if not bounds or not _close(bounds["min"][0], plan["point"][0], 1) or not bounds["max"][1] <= plan["point"][1] + 1:
        failures.append("table_not_at_point")
    cells = table.get("cells")
    if table.get("title") != "门窗表" or not cells:
        return failures + ["table_title_or_cells_unreadable"]
    header = cells[0]
    try:
        kind, size, count = header.index("类型"), header.index("洞口尺寸(mm)"), header.index("数量")
    except ValueError:
        return failures + ["table_header_unexpected"]
    rows = {(row[kind], row[size]): row[count] for row in cells[1:]}
    expected = {key: str(value) for key, value in plan["groups"].items()}
    if rows != expected:
        failures.append("table_rows_differ_from_selected_openings")
    return failures


register("TSTATOP", NativeRecipe("opening_schedule_table", ("selection_handles", "points"),
    _statop_validate, _statop_plan,
    lambda entry, data, plan: f'{entry["command"]}\n{encode_response(entry, {"type": "selection", "value": data["selection_handles"]})}\n'
        + encode_response(entry, {"type": "point", "value": data["points"][0]}),
    _statop_verify, "one_table_rows_match_selected_opening_groups_title_at_point"))


# --- TAlignColu 5.3.4: wall face pick, columns, Enter, column edge pick, Enter exits.

def _aligncolu_validate(entry, data):
    _keys(data, {"selection_handles", "wall_handles", "points"}, "TAlignColu accepts wall_handles (one), column selection_handles and points [wall face, column edge]")
    walls_ = _handles(entry, data, maximum=1, key="wall_handles")
    return {"command": entry["command"], "wall_handles": walls_, "selection_handles": _handles(entry, data, maximum=16), "points": _points(entry, data, 2)}


def _aligncolu_plan(before, data, view):
    wall = data["wall_handles"][0]
    _classes(before, [wall], {"TDBWALL"}, "TAlignColu needs an initialized T30 wall")
    _classes(before, data["selection_handles"], {"TDBCOLUMN"}, "TAlignColu needs initialized T30 columns")
    axis, _ = _orientation(before[wall])
    cross = 1 - axis
    face_point, edge_point = data["points"]
    w_low, w_high = before[wall]["bounds"]["min"][cross], before[wall]["bounds"]["max"][cross]
    if not (_close(face_point[cross], w_low, TOLERANCE) or _close(face_point[cross], w_high, TOLERANCE)):
        raise TianzhengInputError("The first point must lie on one face line of the wall")
    face = face_point[cross]
    edges = set()
    for handle in data["selection_handles"]:
        bounds = before[handle]["bounds"]
        if not (_close(edge_point[cross], bounds["min"][cross], TOLERANCE) or _close(edge_point[cross], bounds["max"][cross], TOLERANCE)):
            raise TianzhengInputError("The second point must lie on the same edge line of every column")
        edges.add(round(edge_point[cross], 6))
        if not bounds["min"][axis] < edge_point[axis] < bounds["max"][axis] and handle == data["selection_handles"][0]:
            raise TianzhengInputError("The column edge point must lie within the first column's edge")
    _check_picks(before, [wall, *data["selection_handles"]], data["points"], view())
    delta = face - edge_point[cross]
    if abs(delta) < 1:
        raise TianzhengInputError("Column edge already lies on the wall face")
    hosts = []
    for key, item in before.items():
        if item["native_class"].upper() != "TDBWALL" or "bounds" not in item:
            continue
        if any(all(item["bounds"]["min"][i] < before[c]["bounds"]["max"][i] and item["bounds"]["max"][i] > before[c]["bounds"]["min"][i] for i in (0, 1))
               for c in data["selection_handles"]):
            hosts.append(key)
    return {"cross": cross, "delta": delta, "columns": data["selection_handles"], "hosts": hosts}


def _host_walls_retiled(plan, before, after):
    """Live 2026-10-15: T30 re-splits host walls at an updated column. Each host
    may become collinear pieces that exactly tile its original extent."""
    failures = []
    created = {k: v for k, v in after.items() if k not in before}
    claimed = set()
    for host in plan.get("hosts", []):
        source = before[host]
        axis, _ = _orientation(source)
        low, high = source["bounds"]["min"], source["bounds"]["max"]
        pieces = [after[host]] if host in after else []
        for key, item in created.items():
            b = item.get("bounds")
            if item["native_class"].upper() == "TDBWALL" and b and all(low[i] - TOLERANCE <= b["min"][i] and b["max"][i] <= high[i] + TOLERANCE for i in (0, 1)):
                pieces.append(item);claimed.add(key)
        ranges = sorted((p["bounds"]["min"][axis], p["bounds"]["max"][axis]) for p in pieces)
        tiled = bool(ranges) and _close(ranges[0][0], low[axis]) and _close(ranges[-1][1], high[axis]) and all(
            _close(a[1], b[0]) for a, b in zip(ranges, ranges[1:]))
        if not tiled:
            failures.append(f"host_wall_not_retiled:{host}")
        for piece in pieces:
            if not _same_except(source, piece, {"bounds", "handle"}) or not all(
                    _close(piece["bounds"][k][i], source["bounds"][k][i]) for k in ("min", "max") for i in (1 - axis, 2)):
                failures.append(f"host_wall_piece_changed:{piece['handle']}")
    if set(created) - claimed:
        failures.append("unexpected_created_objects")
    return failures


def _aligncolu_verify(plan, before, after):
    failures = _unchanged_except(before, after, set(plan["columns"]) | set(plan.get("hosts", [])))
    failures += _host_walls_retiled(plan, before, after)
    for handle in plan["columns"]:
        item, source = after.get(handle), before[handle]
        if item is None:
            continue
        for k in ("min", "max"):
            for i in (0, 1):
                expected = source["bounds"][k][i] + (plan["delta"] if i == plan["cross"] else 0)
                if not _close(item["bounds"][k][i], expected):
                    failures.append(f"column_not_aligned:{handle}")
        if not _same_except(source, item, {"bounds"}):
            failures.append(f"column_properties_changed:{handle}")
    return failures


register("TALIGNCOLU", NativeRecipe("column_edge_to_wall_face", ("wall_handles", "selection_handles", "points"),
    _aligncolu_validate, _aligncolu_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + encode_response(entry, {"type": "point", "value": data["points"][0]})
        + encode_response(entry, {"type": "selection", "value": data["selection_handles"]}) + "\n"
        + encode_response(entry, {"type": "point", "value": data["points"][1]}) + "\n",
    _aligncolu_verify, "columns_translated_perpendicular_so_edge_meets_wall_face_host_walls_only_retiled"))


# --- TBreakDim 14.3.13: pick a T30 dimension, add one point beyond its end, Enter.

def _breakdim_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TBreakDim accepts one dimension handle and points [pick on dimension, new point]")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=1), "points": _points(entry, data, 2)}


def _breakdim_plan(before, data, view):
    handle = data["selection_handles"][0]
    item = before.get(handle)
    if item is None or not item["native_class"].upper().startswith("TDBDIMENSION") or "bounds" not in item:
        raise TianzhengInputError("TBreakDim needs a T30 linear dimension")
    pick, new = data["points"]
    low, high = item["bounds"]["min"], item["bounds"]["max"]
    if not all(low[i] <= pick[i] <= high[i] for i in (0, 1)):
        raise TianzhengInputError("The first point must lie on the dimension")
    if low[0] <= new[0] <= high[0] or not low[1] - TOLERANCE <= new[1] <= high[1] + TOLERANCE:
        raise TianzhengInputError("This recipe adds one point beyond the dimension's X ends, within its height band")
    _check_picks(before, [handle], [pick], view())
    return {"handle": handle, "x": [min(low[0], new[0]), max(high[0], new[0])]}


def _breakdim_verify(plan, before, after):
    failures = _unchanged_except(before, after, {plan["handle"]})
    if _created(before, after):
        failures.append("unexpected_created_objects")
    item = after.get(plan["handle"])
    if item and not (_close(item["bounds"]["min"][0], plan["x"][0]) and _close(item["bounds"]["max"][0], plan["x"][1])):
        failures.append("dimension_not_extended_to_new_point")
    return failures


register("TBREAKDIM", NativeRecipe("dimension_add_point_beyond_end", ("selection_handles", "points"),
    _breakdim_validate, _breakdim_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"]) + "\n",
    _breakdim_verify, "same_dimension_extended_to_new_point_others_unchanged"))


# --- TTextReplace 13.3.9: pick source text, Enter, pick target text, Enter.

def _textreplace_validate(entry, data):
    # Live 2026-10-15: the first Enter ended the command (source pick not taken)
    # and the target point fell through to the command line. Prompt flow is not
    # deterministic for a fixed sequence, so the recipe is withdrawn.
    raise TianzhengInputError("TTextReplace is withdrawn: its prompt flow did not match a fixed sequence live")
    _keys(data, {"selection_handles", "points"}, "TTextReplace accepts selection_handles [source, target] and their pick points")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, minimum=2, maximum=2), "points": _points(entry, data, 2)}


def _textreplace_plan(before, data, view):
    _classes(before, data["selection_handles"], {"ACDBTEXT", "TDBTEXT"}, "TTextReplace recipe needs single-line texts")
    source, target = (before[h] for h in data["selection_handles"])
    if "text" not in source or source.get("text") == target.get("text"):
        raise TianzhengInputError("Source text must be readable and differ from the target")
    for handle, point in zip(data["selection_handles"], data["points"]):
        bounds = before[handle]["bounds"]
        if not all(bounds["min"][i] < point[i] < bounds["max"][i] for i in (0, 1)):
            raise TianzhengInputError("Each pick point must lie inside its text's bounds")
    _check_picks(before, data["selection_handles"], data["points"], view())
    return {"source": data["selection_handles"][0], "target": data["selection_handles"][1], "text": source["text"]}


def _textreplace_verify(plan, before, after):
    failures = _unchanged_except(before, after, {plan["target"]})
    if _created(before, after):
        failures.append("unexpected_created_objects")
    item = after.get(plan["target"])
    if item and item.get("text") != plan["text"]:
        failures.append("target_text_not_replaced")
    if item and not _same_except(before[plan["target"]], item, {"text", "bounds"}):
        failures.append("target_other_properties_changed")
    return failures


register("TTEXTREPLACE", NativeRecipe("text_content_replace", ("selection_handles", "points"),
    _textreplace_validate, _textreplace_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + encode_response(entry, {"type": "point", "value": data["points"][0]}) + "\n"
        + encode_response(entry, {"type": "point", "value": data["points"][1]}) + "\n",
    _textreplace_verify, "target_text_equals_source_others_unchanged"))


# --- TAddInsulate 6.3.7: T sets thickness explicitly, pick the wall face side, Enter exits.

def _insulate_validate(entry, data):
    _keys(data, {"selection_handles", "points", "parameters"}, "TAddInsulate accepts one wall handle, one face point and parameters.thickness")
    parameters = data.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"thickness"}:
        raise TianzhengInputError("TAddInsulate requires parameters.thickness")
    finite_number(parameters["thickness"])
    if not 0 < parameters["thickness"] <= 300:
        raise TianzhengInputError("thickness must be in (0, 300]")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=1),
            "points": _points(entry, data, 1), "parameters": {"thickness": float(parameters["thickness"])}}


def _insulate_plan(before, data, view):
    handle = data["selection_handles"][0]
    _classes(before, [handle], {"TDBWALL"}, "TAddInsulate recipe needs an initialized T30 wall")
    item = before[handle]
    if item.get("is_external") != "外墙" or item.get("io_feature") not in {"左外", "右外"}:
        raise TianzhengInputError("Run identification first: the wall must be exterior with one exterior side (manual 6.3.7)")
    axis, centre = _orientation(item)
    point = data["points"][0]
    half = float(item["left_width"])
    if not _close(abs(point[1 - axis] - centre), half, TOLERANCE):
        raise TianzhengInputError("The point must lie on one face line of the wall")
    low, high = item["bounds"]["min"][axis], item["bounds"]["max"][axis]
    if not low + 2 * half < point[axis] < high - 2 * half:
        raise TianzhengInputError("The point must be away from the wall ends")
    _check_picks(before, [handle], [point], view())
    # Live 2026-10-15: the picked face grows outward by the thickness.
    face = 1 if point[1 - axis] > centre else -1
    return {"handle": handle, "thickness": data["parameters"]["thickness"], "cross": 1 - axis, "face": face}


INSULATION_FIELDS = {"insulate", "insu_thick", "left_insu_thick", "right_insu_thick", "bounds"}


def _insulate_verify(plan, before, after):
    failures = _unchanged_except(before, after, {plan["handle"]})
    if _created(before, after):
        failures.append("unexpected_created_objects")
    item, source = after.get(plan["handle"]), before[plan["handle"]]
    if item is None:
        return failures
    changed = {f for f in set(item) | set(source) if item.get(f) != source.get(f)}
    if changed - INSULATION_FIELDS:
        failures.append(f"non_insulation_fields_changed:{','.join(sorted(changed - INSULATION_FIELDS))}")
    side = item.get("left_insu_thick" if source.get("io_feature") == "左外" else "right_insu_thick")
    if side is None or not _close(side, plan["thickness"]):
        failures.append("exterior_side_insulation_thickness_differs")
    # Property defaults alone (e.g. 80) cannot prove insulation was added.
    if item.get("insulate") in (None, "无"):
        failures.append("insulation_not_enabled")
    cross, key = plan["cross"], ("max" if plan["face"] > 0 else "min")
    grown = item["bounds"][key][cross] - source["bounds"][key][cross]
    if not _close(grown * plan["face"], plan["thickness"]):
        failures.append("bounds_not_grown_by_thickness_on_picked_face")
    return failures


register("TADDINSULATE", NativeRecipe("wall_face_insulation", ("selection_handles", "points", "parameters"),
    _insulate_validate, _insulate_plan,
    lambda entry, data, plan: f'{entry["command"]}\nT\n' + encode_response(entry, {"type": "number", "value": data["parameters"]["thickness"]})
        + encode_response(entry, {"type": "point", "value": data["points"][0]}) + "\n",
    _insulate_verify, "exterior_side_insulation_thickness_only_insulation_fields_changed"))


# --- TDim3 14.2.1 (mode 2, no axis dimensions): line from outside across one wall, Enter.

def _dim3_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TDim3 accepts the crossed wall (selection_handles) and two points")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, maximum=1), "points": _points(entry, data, 2)}


def _dim3_plan(before, data, view):
    wall = data["selection_handles"][0]
    _classes(before, [wall], {"TDBWALL"}, "TDim3 recipe needs an initialized straight T30 wall")
    axis, centre = _orientation(before[wall])
    start, end = data["points"]
    cross = 1 - axis
    if not (_close(start[axis], end[axis], TOLERANCE)):
        raise TianzhengInputError("The line must be perpendicular to the wall")
    low, high = before[wall]["bounds"]["min"], before[wall]["bounds"]["max"]
    if not (low[axis] < start[axis] < high[axis]) or not (min(start[cross], end[cross]) < low[cross] and max(start[cross], end[cross]) > high[cross]):
        raise TianzhengInputError("The line must cross the wall between its ends, starting outside")
    crossed = [k for k, item in before.items() if "bounds" in item and k != wall and
               item["bounds"]["min"][axis] <= start[axis] <= item["bounds"]["max"][axis] and
               item["bounds"]["min"][cross] < max(start[cross], end[cross]) and item["bounds"]["max"][cross] > min(start[cross], end[cross])]
    if crossed:
        raise TianzhengInputError(f"The line must cross only the wall; it also crosses {crossed}")
    openings = [k for k, item in before.items() if item["native_class"].upper() == "TDBOPENING" and
                low[axis] <= item["bounds"]["min"][axis] and item["bounds"]["max"][axis] <= high[axis] and
                item["bounds"]["min"][cross] - TOLERANCE <= centre <= item["bounds"]["max"][cross] + TOLERANCE]
    if not openings:
        raise TianzhengInputError("The wall must host at least one opening")
    _check_picks(before, [wall], data["points"], view())
    return {"axis": axis, "span": [low[axis], high[axis]], "start": start, "openings": openings}


def _dim3_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or not created[0]["native_class"].upper().startswith("TDBDIMENSION"):
        return failures + ["expected_exactly_one_t30_dimension"]
    bounds, axis = created[0].get("bounds"), plan["axis"]
    if not bounds or not (_close(bounds["min"][axis], plan["span"][0]) and _close(bounds["max"][axis], plan["span"][1])):
        failures.append("dimension_span_differs_from_wall")
    return failures


register("TDIM3", NativeRecipe("opening_dimension_without_axis_dims", ("selection_handles", "points"),
    _dim3_validate, _dim3_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"]) + "\n",
    _dim3_verify, "one_dimension_along_wall_span_others_unchanged"))



# --- TAxisDote 4.3.6: no prompt; toggles axis display between dash-dot and continuous.

def _axisdote_validate(entry, data):
    _keys(data, set(), "TAxisDote accepts no parameters")
    return {"command": entry["command"], "selection_handles": []}


def _axisdote_plan(before, data, view):
    axes = [k for k, item in before.items() if item.get("Layer") == "DOTE"]
    if not axes:
        raise TianzhengInputError("TAxisDote needs at least one axis line on layer DOTE")
    return {"axes": axes}


def _continuous(linetype):
    return str(linetype).upper() in {"CONTINUOUS", ""}


def _axisdote_verify(plan, before, after):
    env_before, env_after = plan["environment_before"], plan["environment_after"]
    layer_before, layer_after = env_before["layers"].get("DOTE", {}), env_after["layers"].get("DOTE", {})
    layer_toggled = _continuous(layer_before.get("linetype")) != _continuous(layer_after.get("linetype"))
    entity_toggled = all(after.get(k) and _continuous(before[k].get("linetype")) != _continuous(after[k].get("linetype")) for k in plan["axes"])
    failures = _unchanged_except(before, after, set(plan["axes"]) if entity_toggled else set())
    if _created(before, after):
        failures.append("unexpected_created_objects")
    if layer_toggled == entity_toggled:
        failures.append("axis_linetype_not_toggled_exactly_once")
    others = {k: v for k, v in env_before["layers"].items() if k != "DOTE"}
    if others != {k: v for k, v in env_after["layers"].items() if k != "DOTE"}:
        failures.append("other_layers_changed")
    for key in plan["axes"]:
        if after.get(key) and not _same_except(before[key], after[key], {"linetype"}):
            failures.append(f"axis_geometry_changed:{key}")
    return failures


register("TAXISDOTE", NativeRecipe("axis_linetype_toggle", (),
    _axisdote_validate, _axisdote_plan, lambda entry, data, plan: f'{entry["command"]}\n',
    _axisdote_verify, "axis_linetype_toggled_between_dashdot_and_continuous_only", environment=True))


# --- TClipAxis 4.3.5 rectangle mode: two corners, the main prompt repeats, Enter exits.

def _clipaxis_validate(entry, data):
    _keys(data, {"points"}, "TClipAxis accepts two rectangle corner points")
    return {"command": entry["command"], "selection_handles": [], "points": _points(entry, data, 2)}


def _segment(item):
    start, end = item.get("start_point"), item.get("end_point")
    if not start or not end:
        return None
    if _close(start[0], end[0], TOLERANCE):
        return 1, start[0], sorted((start[1], end[1]))
    if _close(start[1], end[1], TOLERANCE):
        return 0, start[1], sorted((start[0], end[0]))
    return None


def _clipaxis_plan(before, data, view):
    (x1, y1, *_), (x2, y2, *_) = data["points"]
    low, high = [min(x1, x2), min(y1, y2)], [max(x1, x2), max(y1, y2)]
    if high[0] - low[0] < 1 or high[1] - low[1] < 1:
        raise TianzhengInputError("The clipping rectangle must have positive size")
    clipped, untouched = {}, []
    for key, item in before.items():
        if "bounds" not in item:
            continue
        b = item["bounds"]
        overlaps = all(b["min"][i] < high[i] and b["max"][i] > low[i] for i in (0, 1))
        if not overlaps:
            continue
        seg = _segment(item) if item["native_class"].upper() == "ACDBLINE" and item.get("Layer") == "DOTE" else None
        if seg is None:
            raise TianzhengInputError(f"Only axis-aligned DOTE axis lines may intersect the rectangle; {key} does")
        axis, fixed, (a, b2) = seg
        if not (low[axis] > a + 1 and high[axis] < b2 - 1):
            raise TianzhengInputError("Each clipped axis must pass fully through the rectangle (two remaining pieces)")
        clipped[key] = {"axis": axis, "fixed": fixed, "pieces": [[a, low[axis]], [high[axis], b2]]}
    if not clipped:
        raise TianzhengInputError("The rectangle must cross at least one axis line")
    return {"clipped": clipped}


def _clipaxis_verify(plan, before, after):
    # Live 2026-10-15: T30 erases a clipped axis line and creates the two
    # remaining pieces as new lines; keeping the original handle is also accepted.
    kept = {k: v for k, v in before.items() if k not in plan["clipped"] or k in after}
    failures = _unchanged_except(kept, after, set(plan["clipped"]))
    created = {k: v for k, v in after.items() if k not in before}
    claimed = set()
    for key, spec in plan["clipped"].items():
        pieces = []
        for handle, item in [(key, after.get(key))] + list(created.items()):
            if not item or item["native_class"].upper() != "ACDBLINE" or item.get("Layer") != "DOTE":
                continue
            seg = _segment(item)
            if seg and seg[0] == spec["axis"] and _close(seg[1], spec["fixed"]):
                pieces.append(seg[2]);claimed.add(handle)
        if sorted(pieces) != sorted(pieces) or len(pieces) != 2 or not all(
                _close(p[0], q[0]) and _close(p[1], q[1]) for p, q in zip(sorted(pieces), spec["pieces"])):
            failures.append(f"axis_not_clipped_to_rectangle:{key}")
    if set(created) - claimed:
        failures.append("unexpected_created_objects")
    return failures


register("TCLIPAXIS", NativeRecipe("axis_rectangle_clip", ("points",),
    _clipaxis_validate, _clipaxis_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"]) + "\n",
    _clipaxis_verify, "axis_lines_lose_exactly_the_rectangle_interval_others_unchanged"))


# --- TDimMP 14.2.15: start, second point, dimension-line point, Enter ends.
# The modeless dialog's mode (free/horizontal/vertical) is remembered; points on
# one horizontal line give the same result in free and horizontal mode, and a
# vertical mode would fail verification without leaving a prompt open.

def _dimmp_validate(entry, data):
    _keys(data, {"points"}, "TDimMP accepts points [start, end, dimension-line point]")
    return {"command": entry["command"], "selection_handles": [], "points": _points(entry, data, 3)}


def _dimmp_plan(before, data, view):
    start, end, line = data["points"]
    if not _close(start[1], end[1], TOLERANCE) or abs(end[0] - start[0]) < 1:
        raise TianzhengInputError("This recipe measures two points on one horizontal line")
    if abs(line[1] - start[1]) < 100:
        raise TianzhengInputError("The dimension line must be at least 100 units off the measured points")
    for point in data["points"]:
        _clear_point(before, point, 2000, "Measured points must be clear of other objects")
    # Live 2026-10-15: T30 extension lines have a fixed length and do not reach
    # the measured points, so only the dimension-line position is checked.
    return {"span": sorted((start[0], end[0])), "y": [line[1]]}


def _dimmp_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or not created[0]["native_class"].upper().startswith("TDBDIMENSION"):
        return failures + ["expected_exactly_one_t30_dimension"]
    bounds = created[0].get("bounds")
    if not bounds or not (_close(bounds["min"][0], plan["span"][0]) and _close(bounds["max"][0], plan["span"][1])):
        failures.append("dimension_span_differs_from_points")
    elif not all(bounds["min"][1] - TOLERANCE <= y <= bounds["max"][1] + TOLERANCE for y in plan["y"]):
        failures.append("dimension_not_at_requested_line")
    return failures


register("TDIMMP", NativeRecipe("point_to_point_dimension_horizontal", ("points",),
    _dimmp_validate, _dimmp_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"]) + "\n",
    _dimmp_verify, "one_dimension_spanning_points_at_requested_line"))


# --- TUpdSpace 8.2.1: select the walls of one rectangular room, Enter, then one
# point. The point answers the building-area label prompt that appears only when
# that remembered dialog option is on; otherwise it reaches the idle command line
# as an unknown command. No trailing Enter, so nothing can repeat a command.

def _updspace_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TUpdSpace accepts the room's wall handles and one outside label point")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, minimum=4, maximum=4), "points": _points(entry, data, 1)}


def _room_interior(before, handles):
    _classes(before, handles, {"TDBWALL"}, "Room recipes need the four initialized walls of one rectangular room")
    horizontal, vertical = [], []
    for handle in handles:
        axis, centre = _orientation_any(before[handle])
        (horizontal if axis == 0 else vertical).append((centre, handle))
    if len(horizontal) != 2 or len(vertical) != 2:
        raise TianzhengInputError("Need two horizontal and two vertical walls")
    (_, bottom), (_, top) = sorted(horizontal)
    (_, left), (_, right) = sorted(vertical)
    b = lambda h: before[h]["bounds"]
    interior = [[b(left)["max"][0], b(bottom)["max"][1]], [b(right)["min"][0], b(top)["min"][1]]]
    if interior[1][0] - interior[0][0] < 500 or interior[1][1] - interior[0][1] < 500:
        raise TianzhengInputError("Room interior too small")
    return interior


def _updspace_plan(before, data, view):
    interior = _room_interior(before, data["selection_handles"])
    b = lambda h: before[h]["bounds"]
    for key, item in before.items():
        if key in data["selection_handles"] or "bounds" not in item:
            continue
        if all(item["bounds"]["min"][i] < interior[1][i] and item["bounds"]["max"][i] > interior[0][i] for i in (0, 1)):
            raise TianzhengInputError(f"Room interior must be empty; object {key} is inside")
    point = data["points"][0]
    if all(interior[0][i] - 3000 < point[i] < interior[1][i] + 3000 for i in (0, 1)):
        raise TianzhengInputError("The label point must be outside the building")
    outer = [[min(b(h)["min"][i] for h in data["selection_handles"]) for i in (0, 1)],
             [max(b(h)["max"][i] for h in data["selection_handles"]) for i in (0, 1)]]
    return {"interior": interior, "outer": outer, "point": point, "walls": list(data["selection_handles"])}


def _orientation_any(item):
    low, high = item["bounds"]["min"], item["bounds"]["max"]
    return (0, (low[1] + high[1]) / 2) if high[0] - low[0] > high[1] - low[1] else (1, (low[0] + high[0]) / 2)


def _updspace_verify(plan, before, after):
    """Live 2026-10-15: TDbSpace bounds cover only the label; UseArea (m2, text)
    and Perimeter carry the boundary. The building-area object (建筑轮廓面积)
    exists only when that remembered dialog option is on; if present it must
    match the selected walls' outer extent and sit at the given point."""
    # The remembered 识别内外 option may also classify the selected walls; only
    # those two fields may change, and only to a valid exterior classification.
    failures = _unchanged_except(before, after, set(plan["walls"]))
    for handle in plan["walls"]:
        old, new = before[handle], after.get(handle)
        if new is None or not _same_except(old, new, {"is_external", "io_feature"}):
            failures.append(f"wall_changed_beyond_classification:{handle}")
        elif new != old and (new.get("is_external") != "外墙" or new.get("io_feature") not in {"左外", "右外"}):
            failures.append(f"wall_classification_invalid:{handle}")
    created = _created(before, after)
    rooms = [c for c in created if c.get("space_type") == "房间面积"]
    outlines = [c for c in created if c.get("space_type") == "建筑轮廓面积"]
    others = [c for c in created if c not in rooms and c not in outlines]
    if len(rooms) != 1 or len(outlines) > 1 or others:
        return failures + [f"unexpected_objects:rooms={len(rooms)},outlines={len(outlines)},others={len(others)}"]
    room, (low, high) = rooms[0], plan["interior"]
    stucco = float(room.get("stucco_thickness") or 0.0)
    width, depth = high[0] - low[0] - 2 * stucco, high[1] - low[1] - 2 * stucco
    if not math.isclose(float(room.get("use_area") or -1), width * depth / 1e6, abs_tol=0.0011):
        failures.append(f"room_area_differs:{room.get('use_area')}")
    if not _close(room.get("perimeter") or -1, 2 * (width + depth), 0.5):
        failures.append(f"room_perimeter_differs:{room.get('perimeter')}")
    label = room.get("bounds")
    if not label or not all(low[i] < label["min"][i] and label["max"][i] < high[i] for i in (0, 1)):
        failures.append("room_label_not_inside_room")
    if outlines:
        outline, (o_low, o_high) = outlines[0], plan["outer"]
        o_w, o_d = o_high[0] - o_low[0], o_high[1] - o_low[1]
        if not math.isclose(float(outline.get("use_area") or -1), o_w * o_d / 1e6, abs_tol=0.0011) or not _close(outline.get("perimeter") or -1, 2 * (o_w + o_d), 0.5):
            failures.append(f"building_area_differs:{outline.get('use_area')}")
        b = outline.get("bounds")
        if not b or not all(b["min"][i] - 1 <= plan["point"][i] <= b["max"][i] + 1 for i in (0, 1)):
            failures.append("building_area_label_not_at_point")
    return failures


register("TUPDSPACE", NativeRecipe("room_search_single_rectangle", ("selection_handles", "points"),
    _updspace_validate, _updspace_plan,
    lambda entry, data, plan: f'{entry["command"]}\n{encode_response(entry, {"type": "selection", "value": data["selection_handles"]})}\n'
        + encode_response(entry, {"type": "point", "value": data["points"][0]}),
    _updspace_verify, "room_use_area_perimeter_match_interior_optional_building_outline_matches_outer"))


# --- TSpArea 8.2.4 (room query mode): one label point inside a wall room; the
# prompt repeats, Enter exits. The modeless dialog's query mode is remembered;
# another mode would ignore the point and the Enter still ends the command.

def _sparea_validate(entry, data):
    # Live 2026-10-15: the room-area object was correct (21.658 at the point) but
    # the command also reset elevations of unrelated walls 124/128 to 0 and
    # changed door 204's sill (80 -> 330). Destructive side effect: withdrawn.
    raise TianzhengInputError("TSpArea is withdrawn: live run changed unrelated walls' elevation and a door sill")
    _keys(data, {"selection_handles", "points"}, "TSpArea accepts the four room wall handles and one label point inside")
    return {"command": entry["command"], "selection_handles": _handles(entry, data, minimum=4, maximum=4), "points": _points(entry, data, 1)}


def _sparea_plan(before, data, view):
    interior = _room_interior(before, data["selection_handles"])
    point = data["points"][0]
    low, high = interior
    if not all(low[i] + 300 < point[i] < high[i] - 300 for i in (0, 1)):
        raise TianzhengInputError("The label point must be inside the room, 300 units from the walls")
    _clear_point({k: v for k, v in before.items() if k not in data["selection_handles"]}, point, 300, "Label point must be clear of existing labels")
    return {"interior": interior, "point": point}


def _sparea_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or created[0].get("space_type") != "房间面积":
        return failures + [f"expected_one_room_area_object:{[c['native_class'] + ':' + str(c.get('space_type')) for c in created]}"]
    room, (low, high) = created[0], plan["interior"]
    stucco = float(room.get("stucco_thickness") or 0.0)
    width, depth = high[0] - low[0] - 2 * stucco, high[1] - low[1] - 2 * stucco
    if not math.isclose(float(room.get("use_area") or -1), width * depth / 1e6, abs_tol=0.0011):
        failures.append(f"room_area_differs:{room.get('use_area')}")
    label = room.get("bounds")
    if not label or not all(label["min"][i] - 1 <= plan["point"][i] <= label["max"][i] + 1 for i in (0, 1)):
        failures.append("area_label_not_at_point")
    return failures


register("TSPAREA", NativeRecipe("room_area_query_label", ("selection_handles", "points"),
    _sparea_validate, _sparea_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + encode_response(entry, {"type": "point", "value": data["points"][0]}) + "\n",
    _sparea_verify, "one_room_area_object_at_point_matching_interior"))


# --- TCountArea 8.2.12: select room objects, Enter, table insertion point.

def _countarea_validate(entry, data):
    # Live 2026-10-15: the selection prompt ignored an (ssadd) expression, so each
    # room is picked by a point on its label; the last point is the table corner.
    _keys(data, {"selection_handles", "points"}, "TCountArea accepts room handles, one label pick per room, then the table point")
    handles = _handles(entry, data, maximum=8)
    return {"command": entry["command"], "selection_handles": handles, "points": _points(entry, data, len(handles) + 1)}


def _countarea_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBSPACE"}, "TCountArea recipe counts T30 room objects")
    rooms = {h: before[h] for h in data["selection_handles"]}
    if any(r.get("space_type") != "房间面积" or r.get("use_area") is None for r in rooms.values()):
        raise TianzhengInputError("Select room-area objects with readable areas")
    point = data["points"][-1]
    for handle, pick in zip(data["selection_handles"], data["points"]):
        label = before[handle]["bounds"]
        if not all(label["min"][i] < pick[i] < label["max"][i] for i in (0, 1)):
            raise TianzhengInputError(f"Pick point must lie on room {handle}'s label")
    _check_picks(before, data["selection_handles"], data["points"][:-1], view())
    _clear_point(before, point, 20000, "Table insertion area is not clear")
    return {"point": point, "areas": {h: str(r["use_area"]) for h, r in rooms.items()},
            "total": round(sum(float(r["use_area"]) for r in rooms.values()), 3)}


def _countarea_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1 or created[0]["native_class"].upper() != "TDBSHEET":
        return failures + [f"expected_one_table:{[c['native_class'] for c in created]}"]
    table = created[0]
    cells = table.get("cells") or []
    flat = [c for row in cells for c in row]
    # Live 2026-10-15: the table shows 编号/名称/面积 with areas rounded to 2 decimals.
    numbers = []
    for cell in flat:
        try:numbers.append(float(cell))
        except ValueError:pass
    for handle, area in plan["areas"].items():
        if not any(abs(n - float(area)) <= 0.0051 for n in numbers):
            failures.append(f"room_area_missing:{handle}:{area}")
    if len(cells) != len(plan["areas"]) + 1:
        failures.append(f"table_row_count:{len(cells)}")
    bounds = table.get("bounds")
    if not bounds or not _close(bounds["min"][0], plan["point"][0], 1):
        failures.append("table_not_at_point")
    return failures


register("TCOUNTAREA", NativeRecipe("room_area_statistics_table", ("selection_handles", "points"),
    _countarea_validate, _countarea_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"][:-1]) + "\n"
        # Live 2026-10-15: after placing the table the prompt waits again; Enter exits.
        + encode_response(entry, {"type": "point", "value": data["points"][-1]}) + "\n",
    _countarea_verify, "one_table_listing_each_selected_room_area"))


# --- TPlusText 8.2.11 (command-line mode): pick room labels, Enter, result label point.

def _plustext_validate(entry, data):
    _keys(data, {"selection_handles", "points"}, "TPlusText accepts room handles, one label pick per room, then the result point")
    handles = _handles(entry, data, maximum=8)
    return {"command": entry["command"], "selection_handles": handles, "points": _points(entry, data, len(handles) + 1)}


def _plustext_plan(before, data, view):
    _classes(before, data["selection_handles"], {"TDBSPACE"}, "TPlusText recipe sums T30 room objects")
    rooms = {h: before[h] for h in data["selection_handles"]}
    if any(r.get("space_type") != "房间面积" or r.get("use_area") is None for r in rooms.values()):
        raise TianzhengInputError("Select room-area objects with readable areas")
    for handle, pick in zip(data["selection_handles"], data["points"]):
        label = before[handle]["bounds"]
        if not all(label["min"][i] < pick[i] < label["max"][i] for i in (0, 1)):
            raise TianzhengInputError(f"Pick point must lie on room {handle}'s label")
    _check_picks(before, data["selection_handles"], data["points"][:-1], view())
    point = data["points"][-1]
    _clear_point(before, point, 3000, "Result label area is not clear")
    # Manual 8.2.11 / live 2026-10-15: each area is rounded to the area precision
    # (2 decimals here) before summing: 21.66+13.14+12.26 = 47.06, not 47.054.
    return {"point": point, "areas": {h: str(r["use_area"]) for h, r in rooms.items()},
            "total": round(sum(round(float(r["use_area"]) + 1e-9, 2) for r in rooms.values()), 2)}


def _plustext_verify(plan, before, after):
    failures = _unchanged_except(before, after, set())
    created = _created(before, after)
    if len(created) != 1:
        return failures + [f"expected_one_result_label:{[c['native_class'] for c in created]}"]
    text = str(created[0].get("text") or "")
    import re
    values = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", text)]
    if not any(abs(v - plan["total"]) <= 0.0051 for v in values):
        failures.append(f"result_differs_from_sum:{text!r}!={plan['total']}")
    bounds = created[0].get("bounds")
    if not bounds or not all(bounds["min"][i] - 1 <= plan["point"][i] <= bounds["max"][i] + 1 for i in (0, 1)):
        failures.append("result_label_not_at_point")
    return failures


register("TPLUSTEXT", NativeRecipe("room_area_sum_label", ("selection_handles", "points"),
    _plustext_validate, _plustext_plan,
    lambda entry, data, plan: f'{entry["command"]}\n' + "".join(encode_response(entry, {"type": "point", "value": p}) for p in data["points"][:-1]) + "\n"
        + encode_response(entry, {"type": "point", "value": data["points"][-1]}),
    _plustext_verify, "one_label_showing_the_sum_of_selected_room_areas_at_point"))

# Live-verified 2026-10-15 in the isolated test drawing (see work/t30-p0-*-results.json).
LIVE_VERIFIED.update({"TMARKWALL", "TDIMWALL", "TMIRWINIO", "TNORTHTHUMB", "TBREAKDIM", "TSTATOP", "TALIGNCOLU", "TADDINSULATE", "TDIM3", "TAXISDOTE", "TCLIPAXIS", "TDIMMP", "TUPDSPACE", "TCOUNTAREA", "TPLUSTEXT"})


def _mark_verified():
    from dataclasses import replace
    from autocad_mcp.tianzheng_native import NATIVE_RECIPES
    for name in LIVE_VERIFIED:
        NATIVE_RECIPES[name] = replace(NATIVE_RECIPES[name], live_verified=True)


_mark_verified()
# TQuickDim: remembered dimension mode (连续加整体) changes the result; withdrawn per user rule.
from autocad_mcp.tianzheng_native import NATIVE_RECIPES as _ALL  # noqa: E402
_ALL.pop("TQUICKDIM", None)
_ALL.pop("TTEXTREPLACE", None)
