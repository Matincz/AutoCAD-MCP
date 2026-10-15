"""Offline drawing-scoped result ledger and evidence-backed candidate shortlist.

Historical associations are duplicate-risk hints, never live verification or CAD
write permission. No bounding-box or entity-count heuristic assigns ownership.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import ntpath
from pathlib import Path

from autocad_mcp.area_verify import verify_sources
from autocad_mcp.workspace import sha256_file, write_json_atomic


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _drawing(path):
    return ntpath.normcase(ntpath.normpath(path))


def _evidence(record):
    if not record.get("evidence"):
        raise ValueError("A recorded association requires actual evidence files")
    for reference in record["evidence"]:
        if sha256_file(Path(reference["path"])) != reference["sha256"]:
            raise ValueError("Recorded evidence changed; reconcile before selection")


def record_result(ledger_path, drawing_path, record):
    """Append a reviewed association, preserving earlier evidence and uncertainty.

    The caller supplies verified readbacks and semantic provenance; this function
    checks structure/integrity, not the truth of an agent's semantic assertion.
    """
    ledger_path = Path(ledger_path)
    for key in ("kind", "task_scope", "association_basis"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"Record requires {key}")
    if record.get("status") not in {"readback_verified", "semantics_unresolved"}:
        raise ValueError("Record must preserve its actual verification status")
    if type(record.get("saved")) is not bool:
        raise ValueError("Record requires explicit saved state")
    source = record["source_insert"]
    if source.get("type") != "INSERT" or not all(
        k in source
        for k in ("handle", "name", "layer", "insert", "rotation", "xscale", "yscale")
    ):
        raise ValueError("Record requires the source INSERT snapshot")
    boundary = record["boundary"]
    if (
        boundary.get("type") != "LWPOLYLINE"
        or boundary.get("closed") is not True
        or not boundary.get("handle")
        or not boundary.get("layer")
        or len(boundary.get("points", [])) < 3
        or len(boundary.get("bulges", [])) != len(boundary["points"])
    ):
        raise ValueError("Record requires a closed boundary readback, not a bbox")
    _evidence(record)
    ledger = (
        _load(ledger_path)
        if ledger_path.exists()
        else {"schema_version": 1, "drawing_path": drawing_path, "records": []}
    )
    if _drawing(ledger["drawing_path"]) != _drawing(drawing_path):
        raise ValueError("Wrong drawing for this ledger")
    for old in ledger["records"]:
        if old.get("status") == "invalidated":
            continue  # AutoCAD reuses handles of objects lost before saving.
        if old["boundary"]["handle"] == boundary["handle"]:
            if old == record:
                return ledger
            raise ValueError(
                "Boundary already recorded; reconcile instead of overwriting"
            )
    ledger["records"].append(record)
    write_json_atomic(ledger_path, ledger)
    return ledger


def invalidate_result(ledger_path, drawing_path, boundary_handle, reason):
    """Mark a recorded boundary as no longer existing, keeping it as history.

    Typical cause: the line was never saved and the CAD session ended, so its
    handle may now belong to an unrelated object.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Invalidation requires a concrete reason")
    ledger_path = Path(ledger_path)
    ledger = _load(ledger_path)
    if _drawing(ledger["drawing_path"]) != _drawing(drawing_path):
        raise ValueError("Wrong drawing for this ledger")
    active = [
        r
        for r in ledger["records"]
        if r.get("status") != "invalidated" and r["boundary"]["handle"] == boundary_handle
    ]
    if not active:
        raise ValueError(f"No active record for boundary {boundary_handle}")
    for record in active:
        record["invalidated"] = {
            "reason": reason.strip(),
            "previous_status": record["status"],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        record["status"] = "invalidated"
    write_json_atomic(ledger_path, ledger)
    return ledger


def select_targets(index, ledger, *, drawing_path, kind):
    """Propose unrecorded siblings of documented targets of this task kind.

    Same definition is only a localization lead: local room/use evidence is still
    required before accepting a target. Unrelated definitions remain unclassified.
    """
    if _drawing(ledger["drawing_path"]) != _drawing(drawing_path):
        raise ValueError("Wrong drawing for this ledger")
    source = index["source"]
    if sha256_file(Path(source["path"])) != source["sha256"]:
        raise ValueError("Index source changed; do not use stale target identities")
    inserts = {item["handle"]: item for item in index["inserts"]}
    records = [
        r for r in ledger["records"] if r["kind"] == kind and r.get("status") != "invalidated"
    ]
    invalidated = [
        {
            "insert_handle": r["source_insert"]["handle"],
            "boundary_handle": r["boundary"]["handle"],
            "reason": r["invalidated"]["reason"],
        }
        for r in ledger["records"]
        if r["kind"] == kind and r.get("status") == "invalidated"
    ]
    known, conflicts, names = [], [], set()
    for record in records:
        _evidence(record)
        expected = record["source_insert"]
        handle = expected["handle"]
        check = verify_sources(
            {"sources": {handle: expected}, "protected_handles": [], "limitations": []},
            inserts,
        )
        if not check["source_fields_match"]:
            conflicts.append({"handle": handle, "differences": check["differences"]})
        else:
            names.add(expected["name"])
        known.append(
            {
                "insert_handle": handle,
                "boundary_handle": record["boundary"]["handle"],
                "status": record["status"],
                "task_scope": record["task_scope"],
                "saved": record["saved"],
                "action": "review_prior_result_before_any_new_creation",
            }
        )
    known_handles = {r["insert_handle"] for r in known}
    candidates = (
        [
            {
                "insert": item,
                "basis": "same_definition_as_recorded_task_target",
                "next_action": "collect_once_then_assess_local_use_and_duplicate_risk",
            }
            for handle, item in sorted(inserts.items())
            if handle not in known_handles and item["name"] in names
        ]
        if not conflicts
        else []
    )
    return {
        "phase": "record_conflict"
        if conflicts
        else "candidates_need_local_review"
        if candidates
        else "needs_localization",
        "kind": kind,
        "known_results": known,
        "invalidated_results": invalidated,
        "conflicts": conflicts,
        "candidates": candidates,
        "unclassified_count": sum(
            item["name"] not in names and handle not in known_handles
            for handle, item in inserts.items()
        ),
        "next_action": "Reconcile historical identities"
        if conflicts
        else "Use local evidence to assess one candidate"
        if candidates
        else "No supported unrecorded sibling; use a specific new local region/use clue, not entity counts or a reset session",
        "all_drawing_targets_completed": False,
        "cad_verified": False,
        "cad_write_authorized": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--file", type=Path, required=True)
    invalidate = sub.add_parser("invalidate")
    invalidate.add_argument("--boundary", required=True)
    invalidate.add_argument("--reason", required=True)
    select = sub.add_parser("select")
    select.add_argument("--index", type=Path, required=True)
    select.add_argument("--kind", required=True)
    select.add_argument("--output", type=Path, required=True)
    for command in (record, invalidate, select):
        command.add_argument("--ledger", type=Path, required=True)
        command.add_argument("--drawing", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            result = record_result(args.ledger, args.drawing, _load(args.file))
        elif args.command == "invalidate":
            result = invalidate_result(args.ledger, args.drawing, args.boundary, args.reason)
        else:
            if args.output.exists() or args.output.resolve() in {
                args.index.resolve(),
                args.ledger.resolve(),
            }:
                raise ValueError("Selection output must be a new path")
            # A drawing without a ledger simply has no recorded results yet.
            ledger = (
                _load(args.ledger)
                if args.ledger.exists()
                else {"schema_version": 1, "drawing_path": args.drawing, "records": []}
            )
            result = select_targets(
                _load(args.index),
                ledger,
                drawing_path=args.drawing,
                kind=args.kind,
            )
            write_json_atomic(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(
            json.dumps({"error": str(error), "cad_verified": False}, ensure_ascii=False)
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
