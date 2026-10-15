"""Persistent, bounded offline evidence preparation; never calls or authorizes CAD."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import ezdxf

from autocad_mcp.area_scene import extract_area_scene, render_area_scene
from autocad_mcp.workspace import sha256_file, write_json_atomic


@contextmanager
def _session(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "preparation.lock"
    try:
        stream = lock.open("x")
    except FileExistsError:
        raise ValueError(
            "Preparation is locked; inspect the prior process, do not start another session"
        ) from None
    try:
        with stream:
            yield directory, directory / "state.json"
    finally:
        lock.unlink()


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _summary(state, *, cached=False):
    phase = state["phase"]
    next_action = {
        "evidence_ready": "Read scene texts and preview once; record boundary decisions or one specific gap",
        "target_review": "Review the collected geometry against this task, then assess accept/reject; do not infer semantics from insertion points",
        "target_rejected": "Replace the invalid selection once within this session with --replace-reason; preserve collection history",
        "needs_clarification": "Ask the recorded boundary question; do not collect more evidence",
        "ready_for_candidate": "Build and preflight candidate; live source verification and all CAD guards still required",
        "source_changed": "Reconcile source change; old evidence and decisions are stale",
        "collection_failed": "Inspect the offline error; one targeted supplement may remain, never reset the task directory",
        "collecting": "Collection incomplete; inspect the existing process before any retry",
    }[phase]
    return {
        "phase": phase,
        "next_action": next_action,
        "cached": cached,
        "collections": state["collections"],
        "cache_hits": state.get("cache_hits", 0),
        "task": state.get("task"),
        "target_assessment": state.get("target_assessment"),
        "replacements": state.get("replacements", []),
        "decision": state.get("decision"),
        "cad_verified": False,
        "boundary_semantics_verified": False,
        "cad_write_authorized": False,
    }


def _check_source(state, path):
    if sha256_file(Path(state["source"]["path"])) != state["source"]["sha256"]:
        state["phase"] = "source_changed"
        write_json_atomic(path, state)
        raise ValueError(
            "Source changed; reconcile it before using any cached evidence"
        )


def collect_scene(
    directory,
    dxf,
    *,
    insert_handles,
    bounds=None,
    geometry_layers=None,
    text_layers=None,
    area_layer="面积线",
    gap=None,
    task=None,
    replace_reason=None,
):
    """Two collections, plus one explicit invalid-target replacement, per task."""
    dxf = Path(dxf).resolve()
    if task is not None:
        if (
            not isinstance(task, dict)
            or any(
                not isinstance(task.get(k), str) or not task[k].strip()
                for k in ("target", "boundary_rule", "authority")
            )
            or any(
                not isinstance(task.get(k), list)
                or any(not isinstance(v, str) or not v.strip() for v in task[k])
                for k in ("include", "exclude")
            )
        ):
            raise ValueError(
                "Task requires target, boundary_rule, authority and explicit include/exclude lists"
            )
    request = {
        "insert_handles": sorted({h.upper() for h in insert_handles}),
        "bounds": list(bounds) if bounds is not None else None,
        "geometry_layers": sorted({*geometry_layers, area_layer}, key=str.casefold)
        if geometry_layers
        else None,
        "text_layers": sorted(set(text_layers), key=str.casefold)
        if text_layers
        else None,
        "area_layer": area_layer,
    }
    with _session(directory) as (directory, path):
        if path.exists():
            state = _load(path)
            if str(dxf) != state["source"]["path"]:
                raise ValueError("A task cannot silently switch its source drawing")
            _check_source(state, path)
            if task is not None and task != state.get("task"):
                raise ValueError(
                    "Task semantics cannot change inside a preparation session"
                )
            for record in state["collections"]:
                if request == record["request"]:
                    if not set(state["target_inserts"]).issubset(
                        request["insert_handles"]
                    ):
                        raise ValueError(
                            "Rejected target evidence cannot become the current selection again"
                        )
                    if record["status"] != "complete":
                        raise ValueError(
                            "Previous collection did not complete; inspect its error"
                        )
                    for name, digest in record["artifact_hashes"].items():
                        if sha256_file(directory / name) != digest:
                            raise ValueError(
                                "Cached evidence was modified; inspect rather than silently regenerate"
                            )
                    state["cache_hits"] += 1
                    write_json_atomic(path, state)
                    return _summary(state, cached=True)
            if state["phase"] not in {
                "evidence_ready",
                "collection_failed",
                "target_review",
                "target_rejected",
            }:
                raise ValueError(
                    f"Collection is closed in phase {state['phase']}; follow next_action"
                )
            replacing = bool(replace_reason)
            if replacing:
                if state["phase"] != "target_rejected" or state.get("replacements"):
                    raise ValueError(
                        "Replacement requires a rejected target and is allowed only once"
                    )
                if not isinstance(replace_reason, str) or not replace_reason.strip():
                    raise ValueError("Replacement requires a concrete reason")
                if request["insert_handles"] == state["target_inserts"]:
                    raise ValueError("Replacement must select a different target")
            elif state["phase"] == "target_rejected":
                raise ValueError(
                    "Use --replace-reason within this session for a rejected target"
                )
            elif not set(state["target_inserts"]).issubset(request["insert_handles"]):
                raise ValueError(
                    "Keep the selected target; do not restart with another apartment"
                )
            if not replacing and (not isinstance(gap, str) or not gap.strip()):
                raise ValueError(
                    "A changed collection requires --gap naming the missing boundary evidence"
                )
            budget = 3 if replacing or state.get("replacements") else 2
            if len(state["collections"]) >= budget:
                state["phase"] = "needs_clarification"
                state["decision"] = {
                    "edges": [],
                    "unresolved": [(gap or replace_reason).strip()],
                }
                write_json_atomic(path, state)
                raise ValueError(
                    "Supplement budget exhausted; ask the specific question, do not collect again"
                )
            if replacing:
                state["replacements"] = [
                    {
                        "from": state["target_inserts"],
                        "to": request["insert_handles"],
                        "reason": replace_reason,
                        "assessment": state["target_assessment"],
                        "at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
                state["target_inserts"] = request["insert_handles"]
            state.pop("target_assessment", None)
            state.pop("decision", None)
        else:
            if replace_reason:
                raise ValueError("Replacement must keep the existing task session")
            if any(p.name != "preparation.lock" for p in directory.iterdir()):
                raise ValueError(
                    "Use an empty task directory for the initial collection"
                )
            state = {
                "schema_version": 1,
                "source": {"path": str(dxf), "sha256": sha256_file(dxf)},
                "target_inserts": request["insert_handles"],
                "phase": "collecting",
                "collections": [],
                "cache_hits": 0,
                "task": task,
            }
        number = len(state["collections"]) + 1
        scene_name, preview_name = f"scene-{number}.json", f"scene-{number}.png"
        record = {
            "request": request,
            "gap": gap,
            "status": "started",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "scene": scene_name,
            "preview": preview_name,
        }
        state["collections"].append(record)
        state["phase"] = "collecting"
        write_json_atomic(path, state)
        started = time.monotonic()
        try:
            scene = extract_area_scene(
                ezdxf.readfile(dxf),
                insert_handles=request["insert_handles"],
                bounds=request["bounds"],
                geometry_layers=request["geometry_layers"],
                text_layers=request["text_layers"],
            )
            _check_source(state, path)
            scene["source"] = state["source"]
            render_area_scene(
                scene, directory / preview_name, highlight_layer=area_layer
            )
            write_json_atomic(directory / scene_name, scene)
            record.update(
                status="complete",
                finished_at=datetime.now(timezone.utc).isoformat(),
                geometry_count=len(scene["geometry"]),
                text_count=len(scene["texts"]),
                issue_count=len(scene["issues"]),
                elapsed_seconds=time.monotonic() - started,
                artifact_hashes={
                    n: sha256_file(directory / n) for n in (scene_name, preview_name)
                },
            )
            state["phase"] = "target_review" if state.get("task") else "evidence_ready"
        except Exception as error:
            record.update(
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=str(error),
                elapsed_seconds=time.monotonic() - started,
            )
            if state["phase"] != "source_changed":
                state["phase"] = "collection_failed"
            write_json_atomic(path, state)
            raise
        write_json_atomic(path, state)
        return _summary(state)


def assess_target(directory, assessment):
    """Record task-relative agent judgment; geometry does not classify its use."""
    with _session(directory) as (_, path):
        state = _load(path)
        _check_source(state, path)
        if state["phase"] not in {"target_review", "evidence_ready"}:
            raise ValueError("Target assessment requires current collected evidence")
        record = state["collections"][-1]
        for name, digest in record["artifact_hashes"].items():
            if sha256_file(Path(directory) / name) != digest:
                raise ValueError("Cached evidence was modified")
        scene = _load(Path(directory) / record["scene"])
        ids = {e["id"] for e in scene["geometry"] + scene["texts"]}
        if (
            assessment.get("result") not in {"accept", "reject"}
            or not isinstance(assessment.get("reason"), str)
            or not assessment["reason"].strip()
        ):
            raise ValueError(
                "Assessment requires accept/reject and a task-relative reason"
            )
        if not assessment.get("sources") or any(
            s not in ids for s in assessment["sources"]
        ):
            raise ValueError(
                "Assessment sources must identify current scene geometry/text"
            )
        state["target_assessment"] = assessment
        state["phase"] = (
            "evidence_ready" if assessment["result"] == "accept" else "target_rejected"
        )
        write_json_atomic(path, state)
        return _summary(state)


def record_decision(directory, decision):
    """Record agent/user judgment, not an automatic proof of boundary semantics."""
    with _session(directory) as (directory, path):
        state = _load(path)
        _check_source(state, path)
        edges, unresolved = decision.get("edges"), decision.get("unresolved")
        # A failed collection can still leave a real boundary question (found from
        # other evidence); record it so the session pauses instead of re-collecting.
        question_only = (
            state["phase"] == "collection_failed" and edges == [] and bool(unresolved)
        )
        if not question_only and state["phase"] not in {
            "evidence_ready",
            "needs_clarification",
            "ready_for_candidate",
        }:
            raise ValueError("Collect usable evidence before recording decisions")
        if (
            not question_only
            and state.get("task")
            and state.get("target_assessment", {}).get("result") != "accept"
        ):
            raise ValueError(
                "Accept the target against this task before deciding its boundary"
            )
        if not isinstance(edges, list) or not isinstance(unresolved, list):
            raise ValueError("Decision requires edges and unresolved lists")
        if any(not isinstance(q, str) or not q.strip() for q in unresolved):
            raise ValueError(
                "Each unresolved item must name a concrete boundary question"
            )
        if not edges and not unresolved:
            raise ValueError("Provide sourced edges or a specific question")
        sources = set()
        for record in state["collections"]:
            if record["status"] != "complete":
                continue
            if not set(state["target_inserts"]).issubset(
                record["request"]["insert_handles"]
            ):
                continue  # Rejected selections remain history, never boundary evidence.
            for name, digest in record["artifact_hashes"].items():
                if sha256_file(Path(directory) / name) != digest:
                    raise ValueError(
                        "Cached evidence was modified; do not use it for decisions"
                    )
            scene = _load(Path(directory) / record["scene"])
            sources.update(e["id"] for e in scene["geometry"])
        for edge in edges:
            if not all(
                isinstance(edge.get(k), str) and edge[k].strip()
                for k in ("segment", "rule")
            ):
                raise ValueError("Each edge needs segment and rule")
            if (
                not isinstance(edge.get("sources"), list)
                or not edge["sources"]
                or any(s not in sources for s in edge["sources"])
            ):
                raise ValueError(
                    "Edge sources must refer to geometry IDs in the collected scenes"
                )
        if state["phase"] == "needs_clarification" and not unresolved:
            if (
                not isinstance(decision.get("resolution"), str)
                or not decision["resolution"].strip()
            ):
                raise ValueError(
                    "Record the user's answer or new evidence in resolution before clearing a question"
                )
        state["decision"] = decision
        state["decision_recorded_at"] = datetime.now(timezone.utc).isoformat()
        state["phase"] = "needs_clarification" if unresolved else "ready_for_candidate"
        write_json_atomic(path, state)
        return _summary(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("dxf", type=Path)
    collect.add_argument(
        "--insert", action="append", required=True, dest="insert_handles"
    )
    collect.add_argument("--bounds", nargs=4, type=float)
    collect.add_argument("--geometry-layer", action="append", dest="geometry_layers")
    collect.add_argument("--text-layer", action="append", dest="text_layers")
    collect.add_argument("--area-layer", default="面积线")
    collect.add_argument("--gap")
    collect.add_argument(
        "--task",
        type=Path,
        help="Task-specific target/include/exclude/boundary_rule/authority JSON",
    )
    collect.add_argument("--replace-reason")
    assess = sub.add_parser("assess")
    assess.add_argument("--file", required=True, type=Path)
    preview = sub.add_parser("preview")
    preview.add_argument("--candidate", required=True, type=Path)
    preview.add_argument("--output", required=True, type=Path)
    decide = sub.add_parser("decide")
    decide.add_argument("--file", required=True, type=Path)
    status = sub.add_parser("status")
    for p in (collect, assess, decide, status, preview):
        p.add_argument("--session", required=True, type=Path)
    args = vars(parser.parse_args(argv))
    command, directory = args.pop("command"), args.pop("session")
    try:
        if command == "collect":
            if args["task"]:
                args["task"] = _load(args["task"])
            result = collect_scene(directory, **args)
        elif command == "assess":
            result = assess_target(directory, _load(args["file"]))
        elif command == "decide":
            result = record_decision(directory, _load(args["file"]))
        elif command == "preview":
            with _session(directory) as (_, path):
                state = _load(path)
                _check_source(state, path)
                if state["phase"] != "ready_for_candidate":
                    raise ValueError(
                        "Record a resolved boundary decision before previewing"
                    )
                record = state["collections"][-1]
                scene_path = directory / record["scene"]
                if (
                    sha256_file(scene_path)
                    != record["artifact_hashes"][record["scene"]]
                ):
                    raise ValueError("Cached evidence was modified")
                if (
                    args["output"].exists()
                    or args["output"].resolve() == args["candidate"].resolve()
                ):
                    raise ValueError("Preview output must be a new path")
                render_area_scene(
                    _load(scene_path),
                    args["output"],
                    candidate=_load(args["candidate"]),
                )
                result = {"preview": str(args["output"]), "cad_verified": False}
        else:
            with _session(directory) as (_, path):
                state = _load(path)
                _check_source(state, path)
                result = _summary(state)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError) as error:
        print(
            json.dumps({"error": str(error), "cad_verified": False}, ensure_ascii=False)
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
