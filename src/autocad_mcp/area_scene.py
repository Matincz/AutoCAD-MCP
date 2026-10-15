"""Read-only, bounded DXF evidence packs for area-boundary decisions.

This is offline evidence, not a live CAD query or an automatic area classifier.
Run with ``python -m autocad_mcp.area_scene --help``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import time

import ezdxf
from ezdxf.math import Matrix44, Vec3, bulge_to_arc
from ezdxf.proxygraphic import ProxyGraphic

from autocad_mcp.audit import normalize_ezdxf_entity


GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "ARC", "CIRCLE"}
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}


def _layer_name(entity):
    """Custom objects stored as DXFTagStorage (e.g. Tianzheng TCH_WALL) may lack layer."""
    try:
        return entity.dxf.get("layer", "0") or "0"
    except Exception:
        return "0"


def _wcs_geometry(entity):
    data = normalize_ezdxf_entity(entity)
    kind = entity.dxftype()
    normal = Vec3(entity.dxf.get("extrusion", (0, 0, 1)))
    if not normal.isclose((0, 0, 1)) and not normal.isclose((0, 0, -1)):
        raise ValueError("Tilted OCS is not a planar XY boundary")
    if kind == "LWPOLYLINE":
        data["points"] = [list(p) for p in entity.vertices_in_wcs()]
        data["bulges"] = [
            float(p[4]) * (1 if normal.z > 0 else -1) for p in entity.get_points()
        ]
    elif kind in {"ARC", "CIRCLE"}:
        data["center"] = list(entity.ocs().to_wcs(entity.dxf.center))
        if kind == "ARC":
            data["start"] = list(entity.start_point)
            data["end"] = list(entity.end_point)
            data["ccw"] = normal.z > 0
            for key in ("start", "end"):
                point, center = data[key], data["center"]
                data[key + "_angle"] = (
                    math.degrees(math.atan2(point[1] - center[1], point[0] - center[0]))
                    % 360
                )
    elif kind in {"TEXT", "ATTRIB"}:
        data["insert"] = list(entity.ocs().to_wcs(entity.dxf.insert))
    if kind in TEXT_TYPES:
        data["text"] = entity.plain_text()
        if kind == "ATTRIB":
            data["tag"] = entity.dxf.tag
    points = (
        [data["start"], data["end"]]
        if kind == "LINE"
        else data["points"]
        if kind == "LWPOLYLINE"
        else [data["center"]]
        if kind in {"ARC", "CIRCLE"}
        else [data["insert"]]
    )
    if not all(math.isfinite(v) for p in points for v in p):
        raise ValueError("Non-finite geometry")
    if points and max(p[2] for p in points) - min(p[2] for p in points) > 1e-6:
        raise ValueError("Nonplanar geometry is not an XY boundary")
    return data


def _geometry_bounds(data):
    kind = data["type"]
    if kind in {"ARC", "CIRCLE"}:
        x, y = data["center"][:2]
        r = data["radius"]
        box = (x - r, y - r, x + r, y + r)
    else:
        points = [data["start"], data["end"]] if kind == "LINE" else data["points"]
        if not points:
            return None
        # Circular bulges can extend beyond their endpoints. Use complete circle
        # bounds conservatively, without flattening the authoritative curve.
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        if kind == "LWPOLYLINE":
            count = len(points) if data["closed"] else len(points) - 1
            for i in range(count):
                bulge = data["bulges"][i]
                if bulge:
                    center, _, _, radius = bulge_to_arc(
                        points[i][:2], points[(i + 1) % len(points)][:2], bulge
                    )
                    xs.extend([center.x - radius, center.x + radius])
                    ys.extend([center.y - radius, center.y + radius])
        box = (min(xs), min(ys), max(xs), max(ys))
    return box


def _overlaps(data, bounds):
    if data["type"] in TEXT_TYPES:
        x, y = data["insert"][:2]
        return bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]
    box = _geometry_bounds(data)
    if box is None:
        return False
    return (
        box[0] <= bounds[2]
        and box[2] >= bounds[0]
        and box[1] <= bounds[3]
        and box[3] >= bounds[1]
    )


def extract_area_scene(
    document,
    *,
    insert_handles,
    bounds=None,
    geometry_layers=None,
    text_layers=None,
    max_entities=20000,
    max_depth=8,
):
    """Expand only explicit modelspace INSERTs; include local standalone primitives.

    Geometry uses conservative bounding-box overlap, text uses its insertion point.
    No geometry is clipped. Unselected INSERTs are not opened or bbox-evaluated.
    """
    auto_bounds = bounds is None
    if bounds is not None and (
        len(bounds) != 4
        or not all(math.isfinite(x) for x in bounds)
        or not (bounds[0] < bounds[2] and bounds[1] < bounds[3])
    ):
        raise ValueError(
            "bounds must be finite xmin ymin xmax ymax with positive extent"
        )
    if max_entities < 1 or max_depth < 1:
        raise ValueError("Limits must be positive")
    selected = set(h.upper() for h in insert_handles)
    if not selected:
        raise ValueError("At least one explicit modelspace INSERT handle is required")
    model = document.modelspace()
    roots = {e.dxf.handle: e for e in model if e.dxftype() == "INSERT"}
    if selected - roots.keys():
        raise ValueError(
            f"Not modelspace INSERT handles: {sorted(selected - roots.keys())}"
        )
    layers = {x.casefold() for x in geometry_layers} if geometry_layers else None
    label_layers = {x.casefold() for x in text_layers} if text_layers else None
    entities, inserts, issues = [], [], []
    omitted = Counter()
    visited = 0

    def visit(entity, matrix, path, parent_layer, ancestors):
        nonlocal visited
        visited += 1
        if visited > max_entities:
            raise ValueError("Entity visit limit reached; narrow the explicit scope")
        kind = entity.dxftype()
        handle = entity.dxf.handle
        identity = "/".join([*path, handle])
        own_layer = _layer_name(entity)
        layer = own_layer if own_layer != "0" else parent_layer
        if kind == "INSERT":
            block = document.blocks.get(entity.dxf.name)
            if len(path) >= max_depth or entity.dxf.name in ancestors:
                issues.append({"id": identity, "reason": "Block depth limit or cycle"})
                return
            if entity.mcount > 1 or block is None or block.block.is_xref:
                issues.append(
                    {
                        "id": identity,
                        "reason": "MINSERT, missing block or XREF is not expanded",
                    }
                )
                return
            inserts.append(
                {
                    "id": identity,
                    "source": normalize_ezdxf_entity(entity),
                    "zscale": entity.dxf.zscale,
                    "extrusion": list(entity.dxf.extrusion),
                }
            )
            combined = entity.matrix44() @ matrix
            for child in block:
                visit(
                    child,
                    combined,
                    [*path, handle],
                    layer,
                    [*ancestors, entity.dxf.name],
                )
            # Attached attributes are already positioned in the INSERT's parent
            # coordinates; applying the INSERT matrix again would double-transform.
            for attribute in entity.attribs:
                visit(attribute, matrix, [*path, handle], layer, ancestors)
            return
        if kind not in GEOMETRY_TYPES | TEXT_TYPES:
            graphic = getattr(entity, "proxy_graphic", None)
            if graphic is None:
                omitted[kind] += 1
                return
            # Custom objects (Tianzheng walls, openings, ...) are only readable
            # through their proxy graphic. Their primitives are evidence of the
            # drawn shape, not of the object's own editable fields.
            try:
                primitives = list(ProxyGraphic(graphic, document).virtual_entities())
            except Exception as error:
                issues.append({"id": identity, "type": kind, "reason": f"proxy graphic: {error}"})
                return
            if not primitives:
                omitted[f"{kind}:empty_proxy_graphic"] += 1
                return
            for index, primitive in enumerate(primitives):
                primitive_kind = primitive.dxftype()
                if primitive_kind not in GEOMETRY_TYPES | TEXT_TYPES:
                    omitted[f"{kind}:{primitive_kind}"] += 1
                    continue
                primitive_layer = _layer_name(primitive)
                emit(
                    primitive,
                    primitive_kind,
                    matrix,
                    f"{identity}#pg{index}",
                    handle,
                    path,
                    primitive_layer if primitive_layer != "0" else layer,
                    {
                        "type": kind,
                        "handle": handle,
                        "layer": own_layer,
                        "proxy_graphic": True,
                    },
                )
            return
        emit(
            entity,
            kind,
            matrix,
            identity,
            handle,
            path,
            layer,
            normalize_ezdxf_entity(entity),
        )

    def emit(entity, kind, matrix, identity, handle, path, layer, source):
        if (
            kind in GEOMETRY_TYPES
            and layers is not None
            and layer.casefold() not in layers
        ):
            omitted["geometry_layer_filter"] += 1
            return
        if (
            kind in TEXT_TYPES
            and label_layers is not None
            and layer.casefold() not in label_layers
        ):
            omitted["text_layer_filter"] += 1
            return
        try:
            transformed = entity.copy().transform(matrix)
            data = _wcs_geometry(transformed)
            if bounds is not None and not _overlaps(data, bounds):
                return
            if kind in TEXT_TYPES and not data["text"].strip():
                return
        except Exception as error:
            issues.append({"id": identity, "type": kind, "reason": str(error)})
            return
        data.pop("handle", None)
        data.update(
            id=identity,
            source_handle=handle,
            insert_path=path,
            layer=layer,
            source=source,
        )
        entities.append(data)

    # Establish the local window from selected geometry, not every INSERT's bbox.
    for handle, entity in roots.items():
        if handle in selected:
            visit(entity, Matrix44(), [], "0", [])
    if auto_bounds:
        boxes = [
            box
            for e in entities
            if e["type"] in GEOMETRY_TYPES
            if (box := _geometry_bounds(e)) is not None
        ]
        if not boxes:
            raise ValueError("No supported selected geometry; provide explicit bounds")
        lo_x, lo_y = min(b[0] for b in boxes), min(b[1] for b in boxes)
        hi_x, hi_y = max(b[2] for b in boxes), max(b[3] for b in boxes)
        padding = max(hi_x - lo_x, hi_y - lo_y) * 0.05
        if padding <= 0:
            raise ValueError("Degenerate selected extent; provide explicit bounds")
        bounds = (lo_x - padding, lo_y - padding, hi_x + padding, hi_y + padding)
        entities = [e for e in entities if _overlaps(e, bounds)]
    for entity in model:
        if entity.dxftype() != "INSERT":
            visit(entity, Matrix44(), [], "0", [])
    hidden = {
        key.split(":")[0]: count
        for key, count in omitted.items()
        if key.endswith(":empty_proxy_graphic")
    }
    if hidden:
        issues.append(
            {
                "id": "*",
                "reason": "custom objects have no proxy graphic, so their geometry "
                "(e.g. Tianzheng walls/openings) is invisible in this DXF; walls may "
                "be missing from the evidence",
                "counts": hidden,
            }
        )
    texts = [e for e in entities if e["type"] in TEXT_TYPES]
    geometry = [e for e in entities if e["type"] in GEOMETRY_TYPES]
    for i, item in enumerate(texts, 1):
        item["label"] = f"T{i}"
    used_layers = {e["layer"] for e in entities}
    layer_states = [
        {
            "name": layer.dxf.name,
            "color": layer.dxf.color,
            "is_off": layer.is_off(),
            "is_frozen": layer.is_frozen(),
            "is_locked": layer.is_locked(),
        }
        for layer in document.layers
        if layer.dxf.name in used_layers
    ]
    target_roots = []
    for handle in sorted(selected):
        items = [e for e in geometry if e["insert_path"][:1] == [handle]]
        boxes = [b for e in items if (b := _geometry_bounds(e)) is not None]
        target_roots.append(
            {
                "handle": handle,
                "block_name": roots[handle].dxf.name,
                "geometry_count": len(items),
                "observed_wcs_bounds": [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ]
                if boxes
                else None,
                "bounds_scope": "collected_supported_geometry_not_area_boundary",
            }
        )
    return {
        "schema_version": 1,
        "evidence_kind": "offline_dxf",
        "coordinate_system": "WCS",
        "insunits": int(document.header.get("$INSUNITS", 0)),
        "bounds": list(bounds),
        "cad_verified": False,
        "boundary_semantics_verified": False,
        "scope": {
            "insert_handles": sorted(selected),
            "bounds_source": "selected_geometry_plus_5_percent"
            if auto_bounds
            else "explicit",
            "bounds_are_area_boundary": False,
            "geometry_layers": geometry_layers,
            "text_layers": text_layers,
            "geometry_filter": "conservative_bbox_overlap_not_clipped",
            "text_filter": "insertion_point_inside_bounds",
            "supported_types": sorted(GEOMETRY_TYPES | TEXT_TYPES),
            "unexpanded_modelspace_inserts": len(roots) - len(selected),
            "omissions_scope": "visited entities; omitted types not spatially classified",
            "omitted_types": dict(omitted),
            "visited_entities": visited,
        },
        "inserts": inserts,
        "geometry": geometry,
        "texts": texts,
        "target_review": {
            "automatic_semantic_classification": False,
            "roots": target_roots,
            "text_labels": [t["label"] for t in texts],
            "next_action": "Compare these sources with this task's target and inclusion rules; insertion points and room labels alone do not establish a boundary",
        },
        "saved_layer_states_not_live": layer_states,
        "issues": issues,
    }


def index_area_inserts(document):
    """Shallow inventory for choosing a target; never recurse or compute bboxes."""
    blocks = {}
    inserts = []
    for entity in document.modelspace():
        if entity.dxftype() != "INSERT":
            continue
        name = entity.dxf.name
        if name not in blocks:
            block = document.blocks.get(name)
            blocks[name] = dict(Counter(e.dxftype() for e in block)) if block else {}
        inserts.append(
            {
                **normalize_ezdxf_entity(entity),
                "direct_child_types_only": blocks[name],
                "attributes": [
                    {"tag": a.dxf.tag, "text": a.plain_text()} for a in entity.attribs
                ],
            }
        )
    return {
        "evidence_kind": "offline_dxf",
        "cad_verified": False,
        "insunits": int(document.header.get("$INSUNITS", 0)),
        "inserts": inserts,
        "bounds_computed": False,
    }


def render_area_scene(scene, output, *, highlight_layer=None, candidate=None):
    """One numbered preview; original Unicode labels stay in the JSON text table."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Arc, Circle

    fig, ax = plt.subplots(figsize=(10, 8), constrained_layout=True)
    ox, oy, xmax, ymax = scene["bounds"]

    def arc(center, radius, start, end, color, width):
        ax.add_patch(
            Arc(
                (center[0] - ox, center[1] - oy),
                radius * 2,
                radius * 2,
                theta1=math.degrees(start),
                theta2=math.degrees(end),
                color=color,
                lw=width,
            )
        )

    for e in sorted(scene["geometry"], key=lambda e: e["layer"] == highlight_layer):
        color, width = (
            ("#da1590", 2) if e["layer"] == highlight_layer else ("#747c88", 0.7)
        )
        if e["type"] in {"LINE", "LWPOLYLINE"}:
            points = [e["start"], e["end"]] if e["type"] == "LINE" else e["points"]
            closed = e.get("closed", False)
            for i in range(len(points) if closed else len(points) - 1):
                p, q = points[i], points[(i + 1) % len(points)]
                bulge = e.get("bulges", [0] * len(points))[i]
                if bulge:
                    center, start, end, radius = bulge_to_arc(p[:2], q[:2], bulge)
                    arc(center, radius, start, end, color, width)
                else:
                    ax.plot(
                        [p[0] - ox, q[0] - ox],
                        [p[1] - oy, q[1] - oy],
                        color=color,
                        lw=width,
                    )
        elif e["type"] == "CIRCLE":
            ax.add_patch(
                Circle(
                    (e["center"][0] - ox, e["center"][1] - oy),
                    e["radius"],
                    fill=False,
                    color=color,
                    lw=width,
                )
            )
        elif e["type"] == "ARC":
            p, q = (e["start"], e["end"]) if e["ccw"] else (e["end"], e["start"])
            cx, cy = e["center"][:2]
            arc(
                e["center"],
                e["radius"],
                math.atan2(p[1] - cy, p[0] - cx),
                math.atan2(q[1] - cy, q[0] - cx),
                color,
                width,
            )
    for text in scene["texts"]:
        x, y = text["insert"][:2]
        ax.text(
            x - ox,
            y - oy,
            text["label"],
            color="#b00062",
            fontsize=8,
            ha="right" if x > xmax - (xmax - ox) * 0.05 else "left",
            va="top" if y > ymax - (ymax - oy) * 0.05 else "bottom",
        )
    if candidate is not None:
        if candidate.get("coordinate_system") != "WCS" or not candidate.get("closed"):
            raise ValueError("Candidate preview requires a closed WCS ring")
        if any(candidate.get("bulges", [])):
            raise ValueError(
                "Straight candidate preview cannot replace curves with chords"
            )
        points = candidate["points"]
        ring = points + points[:1]
        ax.plot(
            [p[0] - ox for p in ring], [p[1] - oy for p in ring], color="#da1590", lw=2
        )
        for i, (x, y) in enumerate(points, 1):
            ax.text(x - ox, y - oy, str(i), color="#1646aa", fontsize=8)
    ax.set(
        xlim=(0, xmax - ox),
        ylim=(0, ymax - oy),
        aspect="equal",
        title="Offline DXF candidate overlay; not live CAD"
        if candidate
        else "Offline DXF evidence - T labels refer to JSON; not live CAD",
    )
    fig.savefig(output, dpi=160)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--insert", action="append", dest="handles")
    parser.add_argument(
        "--index", action="store_true", help="Shallow INSERT inventory only"
    )
    parser.add_argument(
        "--bounds",
        nargs=4,
        type=float,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
    )
    parser.add_argument("--geometry-layer", action="append", dest="layers")
    parser.add_argument("--text-layer", action="append", dest="text_layers")
    parser.add_argument("--max-entities", type=int, default=20000)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--highlight-layer")
    args = parser.parse_args(argv)
    if args.index and (
        args.handles or args.preview or args.bounds or args.layers or args.text_layers
    ):
        parser.error("--index does not accept selection, filters or preview")
    if not args.index and not args.handles:
        parser.error("Provide --insert or --index")
    for output in (args.output, args.preview):
        if output and (output.resolve() == args.dxf.resolve() or output.exists()):
            parser.error("Outputs must be new paths, distinct from the input DXF")
    if args.preview and args.preview.resolve() == args.output.resolve():
        parser.error("JSON and preview paths must differ")
    start = time.monotonic()
    try:
        document = ezdxf.readfile(args.dxf)
        scene = (
            index_area_inserts(document)
            if args.index
            else extract_area_scene(
                document,
                insert_handles=args.handles,
                bounds=args.bounds,
                geometry_layers=args.layers,
                text_layers=args.text_layers,
                max_entities=args.max_entities,
            )
        )
        with args.dxf.open("rb") as stream:
            digest = (
                hashlib.file_digest(stream, "sha256").hexdigest()
                if hasattr(hashlib, "file_digest")
                else hashlib.sha256(stream.read()).hexdigest()
            )
        scene["source"] = {"path": str(args.dxf.resolve()), "sha256": digest}
        if args.preview:
            render_area_scene(scene, args.preview, highlight_layer=args.highlight_layer)
        scene["elapsed_seconds"] = time.monotonic() - start
        args.output.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    except Exception as error:
        parser.exit(1, f"Area scene failed: {error}\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "geometry": len(scene.get("geometry", [])),
                "texts": len(scene.get("texts", [])),
                "issues": len(scene.get("issues", [])),
                "inserts": len(scene["inserts"]),
                "elapsed_seconds": scene["elapsed_seconds"],
                "cad_verified": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
