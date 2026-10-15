"""Plan and verify directed area-source reads; never writes CAD or retries calls.

The CLI is offline. capture_sources accepts an existing client's call function so
one connection can be reused without embedding a service address or credentials.
"""

from __future__ import annotations

import argparse
import json
import math
import ntpath
from pathlib import Path
import time

from autocad_mcp.workspace import write_json_atomic


def source_read_plan(scene, decision, *, protected_handles=()):
    """Use raw definition coordinates plus all ancestor INSERTs, not WCS as raw."""
    if decision.get("unresolved") or not decision.get("edges"):
        raise ValueError("Resolve boundary decisions before planning live verification")
    geometry = {e["id"]: e for e in scene["geometry"]}
    selected = {s for edge in decision["edges"] for s in edge["sources"]}
    if not selected or selected - geometry.keys():
        raise ValueError("Boundary sources must exist in this scene")
    items = [geometry[s] for s in sorted(selected)] + scene["texts"]
    parents = {
        "/".join(item["insert_path"][:i])
        for item in items
        for i in range(1, len(item["insert_path"]) + 1)
    }
    inserts = {e["id"]: e for e in scene["inserts"]}
    if parents - inserts.keys():
        raise ValueError("Missing ancestor INSERT evidence")
    sources = {}
    presence_only = set()
    for item in items + [inserts[p] for p in sorted(parents)]:
        raw = item.get("source")
        if not raw or not raw.get("handle"):
            raise ValueError(
                "Scene lacks raw source records; regenerate with the current collector"
            )
        if raw.get("proxy_graphic"):
            # Custom-object fields are not exposed by entity.get; only check
            # that the same object still exists.
            presence_only.add(raw["handle"])
            continue
        if raw["type"] not in {
            "LINE",
            "LWPOLYLINE",
            "ARC",
            "CIRCLE",
            "TEXT",
            "MTEXT",
            "INSERT",
        }:
            raise ValueError(f"Unsupported source verification: {raw['type']}")
        handle = raw["handle"]
        if handle in sources and sources[handle] != raw:
            raise ValueError(f"Conflicting raw evidence for {handle}")
        sources[handle] = raw
    return {
        "source": scene.get("source"),
        "insunits": scene["insunits"],
        "sources": sources,
        "protected_handles": sorted(set(protected_handles)),
        "presence_only_handles": sorted(presence_only),
        "handles": sorted(set(sources) | set(protected_handles) | presence_only),
        "source_ids": sorted(selected),
        "scope": "raw source fields and ancestor XY transforms; no semantic or write authorization",
        "limitations": [
            "Current entity.get does not expose block base points or INSERT zscale/extrusion; those are not live-verified",
            "Layer name/color do not prove lock/freeze state",
            *(
                ["Custom-object (proxy graphic) sources are checked for presence only, not geometry"]
                if presence_only
                else []
            ),
        ],
    }


def decode_tool_payload(result):
    """Decode recorded JSON-RPC/MCP envelopes, rejecting tool-level failures."""
    if "error" in result:
        raise ValueError(f"RPC failed: {result['error']}")
    result = result.get("result", result)
    if result.get("isError"):
        raise ValueError(f"MCP tool failed: {result.get('content')}")
    if "structuredContent" in result:
        result = result["structuredContent"]
    elif "content" in result:
        texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
        if len(texts) != 1:
            raise ValueError("Expected one structured tool response")
        result = json.loads(texts[0])
    if result.get("ok") is not True or "payload" not in result:
        raise ValueError(f"Unsuccessful or malformed tool response: {result}")
    return result["payload"]


def _compare(expected, actual, path, differences):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            differences.append(path)
            return
        for key, value in expected.items():
            if key not in actual:
                differences.append(f"{path}.{key}:missing")
            else:
                _compare(value, actual[key], f"{path}.{key}", differences)
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            differences.append(path)
        else:
            for i, (a, b) in enumerate(zip(expected, actual)):
                _compare(a, b, f"{path}[{i}]", differences)
    elif type(expected) in (float, int):
        if type(actual) not in (float, int) or not math.isfinite(actual):
            differences.append(path)
        else:
            delta = actual - expected
            if path.endswith((".rotation", ".start_angle", ".end_angle")):
                delta = (delta + 180) % 360 - 180
            if not math.isfinite(expected) or abs(delta) > 1e-5:
                differences.append(path)
    elif type(actual) is not type(expected) or expected != actual:
        differences.append(path)


def verify_sources(plan, entities):
    """Missing fields or changed coordinates cannot pass by matching type/layer."""
    differences = []
    for handle, expected in plan["sources"].items():
        _compare(expected, entities.get(handle), handle, differences)
    for handle in plan.get("presence_only_handles", []):
        if handle not in entities or entities[handle].get("handle") != handle:
            differences.append(f"{handle}:missing_source_entity")
    for handle in plan["protected_handles"]:
        if handle not in entities or entities[handle].get("handle") != handle:
            differences.append(f"{handle}:missing_protected_entity")
    return {
        "source_fields_match": not differences,
        "differences": differences,
        "checked_source_count": len(plan["sources"]),
        "limitations": plan["limitations"],
        "cad_write_authorized": False,
        "boundary_semantics_verified": False,
    }


def _context_identity(context, expected_path):
    if ntpath.normcase(ntpath.normpath(context["active_path"])) != ntpath.normcase(
        ntpath.normpath(expected_path)
    ):
        raise ValueError("Wrong active drawing; no further calls")
    if context["doc_id"] != context["active_doc_id"]:
        raise ValueError("Active document identity mismatch")
    return tuple(
        context[k] for k in ("session_id", "doc_id", "revision", "worker_generation")
    )


DECLARED_UNITS = {"mm": 4, "m": 6}


def capture_sources(plan, call, *, expected_path, declared_units=None):
    """Normal-entry reads, serial and single-attempt; not a recovery operation.

    call(name, **arguments) must return the successful payload or raise, like the
    existing recorded Client.call. On any failure this function stops immediately.
    declared_units={"units": "mm"|"m", "evidence": "..."} is accepted only when
    the drawing itself leaves INSUNITS unspecified (0); it never overrides 4/6.
    Entities are read with entity.get_many (one request per 200 handles).
    """
    started = time.monotonic()
    ready = call("system", operation="ensure_ready")
    if (
        ready.get("ready") is not True
        or ready.get("dispatcher", {}).get("loaded") is not True
    ):
        raise ValueError("CAD/dispatcher is not ready")
    before = call("transaction", operation="context")
    identity = _context_identity(before, expected_path)
    variables = call(
        "drawing",
        operation="get_variables",
        data={"names": ["INSUNITS", "CMDACTIVE", "CMDNAMES"]},
    )
    if variables.get("CMDACTIVE") != 0 or variables.get("CMDNAMES") != "":
        raise ValueError("CAD is not idle")
    units_declaration = None
    if variables.get("INSUNITS") != plan["insunits"]:
        raise ValueError("Live INSUNITS differs from the scene evidence")
    if plan["insunits"] == 0 and declared_units:
        code = DECLARED_UNITS.get(str(declared_units.get("units")))
        evidence = str(declared_units.get("evidence") or "").strip()
        if code is None or not evidence:
            raise ValueError("declared_units needs units mm|m and concrete evidence")
        units_declaration = {"units": declared_units["units"], "code": code, "evidence": evidence}
    elif plan["insunits"] not in (4, 6):
        raise ValueError("Units must match explicit mm/m evidence")
    layers = call("layer", operation="list")
    if not isinstance(layers, dict) or not isinstance(layers.get("layers"), list):
        raise ValueError("Expected layer.list payload {layers: [...]}")
    entities = {}
    handles = list(plan["handles"])
    for start in range(0, len(handles), 200):
        batch = call(
            "entity", operation="get_many", data={"entity_ids": handles[start : start + 200]}
        )
        if batch.get("errors"):
            raise ValueError(f"Entity reads failed: {batch['errors']}")
        entities.update(batch["entities"])
    after = call("transaction", operation="context")
    if _context_identity(after, expected_path) != identity:
        raise ValueError(
            "Document/revision/generation changed during reads; discard snapshot"
        )
    return {
        "context_before": before,
        "context_after": after,
        "variables": variables,
        "units_declaration": units_declaration,
        "layers": layers["layers"],
        "entities": entities,
        "verification": verify_sources(plan, entities),
        "elapsed_seconds": time.monotonic() - started,
        "timing_scope": "read-only capture including guards; not total agent preparation",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--scene", type=Path, required=True)
    plan.add_argument("--decision", type=Path, required=True)
    plan.add_argument("--protect", action="append", default=[])
    check = sub.add_parser("check")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--snapshot", type=Path, required=True)
    for p in (plan, check):
        p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    def load(p):
        return json.loads(p.read_text(encoding="utf-8"))

    if args.output.exists():
        parser.error("Output must be a new path")
    if args.command == "plan":
        result = source_read_plan(
            load(args.scene), load(args.decision), protected_handles=args.protect
        )
    else:
        result = verify_sources(load(args.plan), load(args.snapshot)["entities"])
    write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("source_fields_match", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
