"""Pure geometry for the read-only ``tianzheng.walls`` operation.

T30 walls inside blocks expose their StartPoint/EndPoint/LeftWidth/RightWidth
in block-definition coordinates. These helpers compose the INSERT chain into a
WCS affine transform and derive centre line and side lines. No COM here.

Side naming follows the wall's own direction in its block definition: ``left``
is the side LeftWidth measures. A mirrored chain swaps which WCS side that is;
``mirrored`` reports it so callers never guess.
"""
from __future__ import annotations

import math

from autocad_mcp.tianzheng import TianzhengInputError

EPS = 1e-9
MAX_INSERT_HANDLES = 16
MAX_DEPTH = 8
MAX_WALLS = 2000

Matrix = list[list[float]]  # 4x4 row-major affine


def identity() -> Matrix:
    return [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def matmul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def apply(m: Matrix, p) -> list[float]:
    x, y, z = (list(p) + [0.0, 0.0, 0.0])[:3]
    return [m[r][0] * x + m[r][1] * y + m[r][2] * z + m[r][3] for r in range(3)]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _unit(v):
    length = math.sqrt(sum(c * c for c in v))
    if length < EPS:
        raise ValueError("zero-length vector")
    return [c / length for c in v]


def ocs_axes(normal) -> tuple[list[float], list[float], list[float]]:
    """AutoCAD arbitrary-axis algorithm: OCS X, Y, Z in WCS."""
    n = _unit(list(normal))
    if abs(n[0]) < 1 / 64 and abs(n[1]) < 1 / 64:
        ax = _unit(_cross([0.0, 1.0, 0.0], n))
    else:
        ax = _unit(_cross([0.0, 0.0, 1.0], n))
    return ax, _unit(_cross(n, ax)), n


def insert_matrix(insertion, rotation: float, scale, base=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0)) -> Matrix:
    """Block-definition coordinates -> parent coordinates for one INSERT.

    ActiveX reports InsertionPoint in the parent's coordinates (WCS for model
    space); Rotation is measured in the INSERT's OCS.
    """
    sx, sy, sz = scale
    c, s = math.cos(rotation), math.sin(rotation)
    # Rotation * Scale in OCS, then OCS axes to parent axes.
    local = [[c * sx, -s * sy, 0.0], [s * sx, c * sy, 0.0], [0.0, 0.0, sz]]
    ax, ay, az = ocs_axes(normal)
    ocs = [[ax[r], ay[r], az[r]] for r in range(3)]
    linear = [[sum(ocs[r][k] * local[k][c2] for k in range(3)) for c2 in range(3)] for r in range(3)]
    shift = [insertion[r] - sum(linear[r][k] * base[k] for k in range(3)) for r in range(3)]
    return [linear[0] + [shift[0]], linear[1] + [shift[1]], linear[2] + [shift[2]], [0.0, 0.0, 0.0, 1.0]]


def plan_determinant(m: Matrix) -> float:
    return m[0][0] * m[1][1] - m[0][1] * m[1][0]


def plan_scale(m: Matrix) -> float | None:
    """Uniform plan scale, or None when the XY part is not a similarity."""
    cx = math.hypot(m[0][0], m[1][0])
    cy = math.hypot(m[0][1], m[1][1])
    dot = m[0][0] * m[0][1] + m[1][0] * m[1][1]
    if abs(cx - cy) > 1e-7 * max(cx, cy, 1.0) or abs(dot) > 1e-7 * max(cx * cy, 1.0):
        return None
    if abs(m[2][0]) > 1e-9 or abs(m[2][1]) > 1e-9:
        return None  # plan is tilted out of XY; the wall is not a plan curve any more
    return cx


def _r(values, digits=6):
    return [round(float(v), digits) for v in values]


def _offset_line(start, end, distance):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length  # left normal
    return ([start[0] + nx * distance, start[1] + ny * distance, start[2]],
            [end[0] + nx * distance, end[1] + ny * distance, end[2]])


def arc_center(start, end, radius: float, bulge: float):
    chord = math.hypot(end[0] - start[0], end[1] - start[1])
    sagitta = bulge * chord / 2
    r = abs(radius) if radius else (chord / 2) * (1 + bulge * bulge) / (2 * abs(bulge))
    mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
    dx, dy = (end[0] - start[0]) / chord, (end[1] - start[1]) / chord
    # Centre lies left of the chord for 0 < bulge < 1; h < 0 flips it past 180 degrees.
    h = r - abs(sagitta)
    side = 1.0 if bulge > 0 else -1.0
    return [mx - dy * h * side, my + dx * h * side, start[2]]


def bulge_from_center(start, end, center) -> float:
    """CCW bulge from start to end around centre."""
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = (a1 - a0) % (2 * math.pi)
    return math.tan(sweep / 4)


def wall_geometry(local: dict, matrix: Matrix) -> dict:
    """Centre line and side lines of one wall, transformed to WCS.

    ``local`` keys: start, end, left_width, right_width, optional radius,
    center, bulge (all in block-definition coordinates).
    """
    start, end = [float(v) for v in local["start"]][:3], [float(v) for v in local["end"]][:3]
    while len(start) < 3: start.append(0.0)
    while len(end) < 3: end.append(0.0)
    lw, rw = float(local["left_width"]), float(local["right_width"])
    det = plan_determinant(matrix)
    scale = plan_scale(matrix)
    mirrored = det < 0
    out = {"mirrored": mirrored, "thickness": None}
    radius = float(local.get("radius") or 0.0)
    is_arc = abs(radius) > EPS or local.get("bulge") not in (None, 0, 0.0)
    if math.hypot(end[0] - start[0], end[1] - start[1]) < EPS:
        return {**out, "kind": "degenerate", "error": "zero-length wall"}
    if not is_arc:
        left = _offset_line(start, end, lw)
        right = _offset_line(start, end, -rw)
        thickness = (lw + rw) * (scale if scale is not None else math.sqrt(abs(det)))
        return {**out, "kind": "line", "thickness": round(thickness, 6),
                "centerline": {"start": _r(apply(matrix, start)), "end": _r(apply(matrix, end))},
                "left_edge": {"start": _r(apply(matrix, left[0])), "end": _r(apply(matrix, left[1]))},
                "right_edge": {"start": _r(apply(matrix, right[0])), "end": _r(apply(matrix, right[1]))},
                **({} if scale is not None else {"warning": "non-uniform INSERT scale: thickness approximated"})}
    if scale is None:
        return {**out, "kind": "arc", "error": "arc wall under non-uniform or tilted INSERT scale cannot be represented as an arc"}
    bulge = local.get("bulge")
    center = local.get("center")
    if bulge is None and center is not None:
        bulge = bulge_from_center(start, end, center)
        out["bulge_source"] = "center_assumed_ccw"
    elif bulge is not None:
        bulge = float(bulge)
        out["bulge_source"] = "bulge_property"
    else:
        return {**out, "kind": "arc", "error": "arc wall exposes Radius but neither Bulge nor Center",
                "radius_local": radius}
    center = arc_center(start, end, radius, bulge) if local.get("center") is None else [float(v) for v in local["center"]][:3]
    r = math.hypot(start[0] - center[0], start[1] - center[1])

    def ring(dist):
        # Left of a CCW arc (positive bulge) points toward the centre.
        rr = r - dist if bulge > 0 else r + dist
        if rr <= EPS:
            return None
        def proj(p):
            ang = math.atan2(p[1] - center[1], p[0] - center[0])
            return [center[0] + rr * math.cos(ang), center[1] + rr * math.sin(ang), p[2]]
        return proj(start), proj(end)

    wcs_bulge = -bulge if mirrored else bulge

    def seg(pair):
        if pair is None:
            return None
        return {"start": _r(apply(matrix, pair[0])), "end": _r(apply(matrix, pair[1])), "bulge": round(wcs_bulge, 9)}

    return {**out, "kind": "arc", "thickness": round((lw + rw) * scale, 6),
            "radius": round(r * scale, 6), "center": _r(apply(matrix, center)),
            "centerline": seg((start, end)), "left_edge": seg(ring(lw)), "right_edge": seg(ring(-rw))}


def validate_walls_request(data: dict) -> dict:
    handles = data.get("insert_handles")
    if not isinstance(handles, list) or not 1 <= len(handles) <= MAX_INSERT_HANDLES:
        raise TianzhengInputError(f"insert_handles must list 1..{MAX_INSERT_HANDLES} INSERT handles")
    clean = []
    for handle in handles:
        if not isinstance(handle, str) or not handle or len(handle) > 16 or any(c not in "0123456789abcdefABCDEF" for c in handle):
            raise TianzhengInputError("insert_handles must be hexadecimal entity handles")
        clean.append(handle.upper())
    depth = data.get("max_depth", MAX_DEPTH)
    if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= MAX_DEPTH:
        raise TianzhengInputError(f"max_depth must be an integer 1..{MAX_DEPTH}")
    return {"insert_handles": list(dict.fromkeys(clean)), "max_depth": depth}


def axis_aligned(m: Matrix, tol: float = 1e-9) -> bool:
    """True when the plan part of ``m`` only scales/mirrors/rotates by 90 degrees."""
    return (abs(m[0][1]) < tol and abs(m[1][0]) < tol) or (abs(m[0][0]) < tol and abs(m[1][1]) < tol)


def wall_from_bounds(bounds_wcs: dict, matrix: Matrix, max_aspect: float = 0.5) -> dict:
    """Infer an orthogonal straight wall from its WCS bounding box.

    Fallback for walls whose own properties are unreadable (T30 walls inside
    block definitions). Valid only for straight walls parallel to the block
    axes: a slightly skewed wall still yields a thin box with a wrong thickness,
    and arc walls cannot be told apart. Callers must treat the result as an
    assumption, not as read geometry.
    """
    if not axis_aligned(matrix):
        return {"usable": False, "reason": "INSERT is rotated by a non-right angle; bounds are not the wall outline"}
    lo, hi = bounds_wcs["min"], bounds_wcs["max"]
    dx, dy = hi[0] - lo[0], hi[1] - lo[1]
    if min(dx, dy) <= EPS or min(dx, dy) > max_aspect * max(dx, dy):
        return {"usable": False, "reason": "bounds too square to tell wall direction"}
    z = lo[2]
    if dx >= dy:
        mid = (lo[1] + hi[1]) / 2
        faces = ([lo[0], lo[1], z], [hi[0], lo[1], z]), ([lo[0], hi[1], z], [hi[0], hi[1], z])
        center = ([lo[0], mid, z], [hi[0], mid, z])
        direction, thickness = "x", dy
    else:
        mid = (lo[0] + hi[0]) / 2
        faces = ([lo[0], lo[1], z], [lo[0], hi[1], z]), ([hi[0], lo[1], z], [hi[0], hi[1], z])
        center = ([mid, lo[1], z], [mid, hi[1], z])
        direction, thickness = "y", dx
    line = lambda pair: {"start": _r(pair[0]), "end": _r(pair[1])}  # noqa: E731
    return {
        "usable": True,
        "assumption": "straight wall parallel to block axes; inferred from bounding box, not read",
        "direction": direction,
        "thickness": round(thickness, 6),
        "centerline": line(center),
        "faces": [line(faces[0]), line(faces[1])],
    }
