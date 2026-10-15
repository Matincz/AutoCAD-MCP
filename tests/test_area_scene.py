"""Offline evidence contracts; no CAD connection or drawing modifications."""

import hashlib
import json
from copy import deepcopy
import subprocess
import sys

import ezdxf
import pytest

from autocad_mcp.area_scene import extract_area_scene, index_area_inserts
from autocad_mcp.area_prepare import assess_target, collect_scene, record_decision
from autocad_mcp.area_targets import record_result, select_targets
from autocad_mcp.area_verify import (
    capture_sources,
    decode_tool_payload,
    source_read_plan,
    verify_sources,
)


def test_nested_base_rotation_mirror_provenance_and_standalone_labels():
    doc = ezdxf.new()
    doc.layers.new("Walls")
    inner = doc.blocks.new("inner", base_point=(10, 20))
    line = inner.add_line((11, 22), (14, 22))
    inner.add_mtext("生活阳台", dxfattribs={"insert": (11, 23)})
    outer = doc.blocks.new("outer")
    child = outer.add_blockref(
        "inner", (5, 7), dxfattribs={"rotation": 90, "xscale": 2, "yscale": 3}
    )
    child.add_attrib("ROOM", "玄关", (-2, 10))
    left = doc.modelspace().add_blockref(
        "outer", (100, 200), dxfattribs={"rotation": 90, "xscale": -1, "layer": "Walls"}
    )
    right = doc.modelspace().add_blockref(
        "outer", (300, 400), dxfattribs={"layer": "Walls"}
    )
    doc.modelspace().add_text("客厅", dxfattribs={"insert": (95, 202)})
    doc.modelspace().add_text("远处", dxfattribs={"insert": (1000, 2000)})
    cycle = doc.blocks.new("unselected-cycle")
    cycle.add_blockref("unselected-cycle", (0, 0))
    doc.modelspace().add_blockref("unselected-cycle", (100, 200))

    result = extract_area_scene(
        doc,
        insert_handles=[left.dxf.handle, right.dxf.handle],
        bounds=(80, 190, 310, 430),
        geometry_layers=["Walls"],
    )
    assert not result["issues"]
    assert result["scope"]["unexpanded_modelspace_inserts"] == 1
    a, b = result["geometry"]
    assert a["start"] == pytest.approx([91, 201, 0])
    assert a["end"] == pytest.approx([85, 201, 0])
    assert b["start"] == pytest.approx([299, 409, 0])
    assert a["source_handle"] == b["source_handle"] == line.dxf.handle
    assert a["insert_path"] == [left.dxf.handle, child.dxf.handle]
    assert a["id"] != b["id"] and a["layer"] == "Walls"
    assert {t["text"] for t in result["texts"]} == {"生活阳台", "客厅", "玄关"}
    assert result["texts"][0]["insert"] == pytest.approx([91, 204, 0])
    assert result["texts"][1]["insert"] == pytest.approx([90, 202, 0])
    assert result["texts"][1]["tag"] == "ROOM"
    assert len({t["label"] for t in result["texts"]}) == 5
    assert not result["cad_verified"] and not result["boundary_semantics_verified"]
    assert list(line.dxf.start) == [11, 22, 0]  # Input document remains unchanged.


def test_ocs_bulge_and_nonuniform_curve_are_not_silently_straightened():
    doc = ezdxf.new()
    block = doc.blocks.new("curved")
    block.add_lwpolyline(
        [(0, 0, 1), (2, 0, 0)], format="xyb", dxfattribs={"extrusion": (0, 0, -1)}
    )
    normal = doc.modelspace().add_blockref("curved", (0, 0))
    stretched = doc.modelspace().add_blockref(
        "curved", (10, 0), dxfattribs={"xscale": 2}
    )
    result = extract_area_scene(
        doc,
        insert_handles=[normal.dxf.handle, stretched.dxf.handle],
        bounds=(-3, -2, 15, 2),
    )
    assert len(result["geometry"]) == 1
    p = result["geometry"][0]
    assert p["points"][1] == pytest.approx([-2, 0, 0])
    assert p["bulges"] == [-1, 0]
    assert len(result["issues"]) == 1
    assert result["issues"][0]["id"].startswith(stretched.dxf.handle + "/")


def test_mirrored_arc_reports_wcs_endpoints_angles_and_clockwise_direction():
    doc = ezdxf.new()
    block = doc.blocks.new("arc")
    block.add_arc((0, 0), 2, 0, 90)
    root = doc.modelspace().add_blockref("arc", (10, 20), dxfattribs={"xscale": -1})
    result = extract_area_scene(
        doc, insert_handles=[root.dxf.handle], bounds=(7, 17, 13, 23)
    )
    assert not result["issues"]
    arc = result["geometry"][0]
    assert arc["center"] == pytest.approx([10, 20, 0])
    assert arc["start"] == pytest.approx([8, 20, 0])
    assert arc["end"] == pytest.approx([10, 22, 0])
    assert arc["start_angle"] == pytest.approx(180)
    assert arc["end_angle"] == pytest.approx(90)
    assert arc["ccw"] is False


def test_scope_reports_omissions_preserves_crossing_lines_and_saved_layer_state():
    doc = ezdxf.new()
    layer = doc.layers.new("hidden")
    layer.off()
    layer.freeze()
    layer.lock()
    block = doc.blocks.new("plan")
    block.add_line((-20, 2), (20, 2), dxfattribs={"layer": "hidden"})
    block.add_spline([(0, 0), (1, 2), (3, 0)])
    insert = doc.modelspace().add_blockref("plan", (0, 0))
    doc.modelspace().add_text("edge", dxfattribs={"insert": (5, 5)})
    doc.modelspace().add_text("outside", dxfattribs={"insert": (5.01, 5)})
    result = extract_area_scene(
        doc, insert_handles=[insert.dxf.handle], bounds=(-5, -5, 5, 5)
    )
    assert result["geometry"][0]["start"] == [-20, 2, 0]
    assert result["geometry"][0]["end"] == [20, 2, 0]
    assert result["scope"]["omitted_types"] == {"SPLINE": 1}
    assert [t["text"] for t in result["texts"]] == ["edge"]
    state = next(
        layer
        for layer in result["saved_layer_states_not_live"]
        if layer["name"] == "hidden"
    )
    assert state["is_off"] and state["is_frozen"] and state["is_locked"]


def test_cycle_limits_and_invalid_target_are_explicit():
    doc = ezdxf.new()
    block = doc.blocks.new("cycle")
    block.add_blockref("cycle", (0, 0))
    root = doc.modelspace().add_blockref("cycle", (0, 0))
    args = dict(insert_handles=[root.dxf.handle], bounds=(-10, -10, 10, 10))
    assert "cycle" in extract_area_scene(doc, **args)["issues"][0]["reason"]
    with pytest.raises(ValueError, match="visit limit"):
        extract_area_scene(doc, **args, max_entities=1)
    with pytest.raises(ValueError, match="Not modelspace INSERT"):
        extract_area_scene(doc, insert_handles=["INVALID"], bounds=args["bounds"])
    with pytest.raises(ValueError, match="bounds"):
        extract_area_scene(
            doc, insert_handles=args["insert_handles"], bounds=(0, 0, 0, 0)
        )


def test_cli_extracts_without_overwriting_source_or_existing_results(tmp_path):
    doc = ezdxf.new()
    block = doc.blocks.new("unit")
    block.add_line((1, 2), (4, 6))
    root = doc.modelspace().add_blockref("unit", (0, 0))
    source, output = tmp_path / "input.dxf", tmp_path / "scene.json"
    doc.saveas(source)
    original = source.read_bytes()
    command = [
        sys.executable,
        "-m",
        "autocad_mcp.area_scene",
        str(source),
        "--insert",
        root.dxf.handle,
        "--bounds",
        "0",
        "0",
        "10",
        "10",
        "--output",
        str(output),
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    data = json.loads(output.read_text())
    assert data["geometry"][0]["start"] == [1, 2, 0]
    assert data["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    result_bytes = output.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_bytes() == result_bytes and source.read_bytes() == original
    command[-1] = str(source)
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert source.read_bytes() == original


def test_automatic_window_is_local_and_inventory_never_expands_other_blocks():
    doc = ezdxf.new()
    block = doc.blocks.new("unit", base_point=(1, 2))
    block.add_lwpolyline([(1, 2), (9, 2), (9, 5), (1, 5)], close=True)
    root = doc.modelspace().add_blockref(
        "unit", (100, 200), dxfattribs={"rotation": 90}
    )
    root.add_attrib("TYPE", "住宅", (99, 204))
    doc.modelspace().add_text("生活阳台", dxfattribs={"insert": (96.8, 203)})
    doc.modelspace().add_text("远处", dxfattribs={"insert": (50000, 60000)})
    bad = doc.blocks.new("cycle")
    bad.add_blockref("cycle", (0, 0))
    doc.modelspace().add_blockref("cycle", (100, 200))
    result = extract_area_scene(doc, insert_handles=[root.dxf.handle])
    assert result["bounds"] == pytest.approx([96.6, 199.6, 100.4, 208.4])
    assert {t["text"] for t in result["texts"]} == {"住宅", "生活阳台"}
    assert result["scope"]["bounds_are_area_boundary"] is False
    assert (
        not result["issues"] and result["scope"]["unexpanded_modelspace_inserts"] == 1
    )
    inventory = index_area_inserts(doc)
    assert inventory["bounds_computed"] is False
    assert len(inventory["inserts"]) == 2
    assert inventory["inserts"][0]["attributes"] == [{"tag": "TYPE", "text": "住宅"}]


@pytest.fixture
def preparation_input(tmp_path):
    doc = ezdxf.new()
    block = doc.blocks.new("unit")
    block.add_lwpolyline(
        [(0, 0), (8000, 0), (8000, 3000), (0, 3000)],
        close=True,
        dxfattribs={"layer": "WALL"},
    )
    root = doc.modelspace().add_blockref("unit", (100, 200))
    other = doc.modelspace().add_blockref("unit", (30000, 40000))
    doc.modelspace().add_text("客厅", dxfattribs={"insert": (500, 800)})
    doc.modelspace().add_lwpolyline(
        [(200, 300), (500, 300), (500, 600)], close=True, dxfattribs={"layer": "面积线"}
    )
    source = tmp_path / "source.dxf"
    doc.saveas(source)
    return source, root.dxf.handle, other.dxf.handle


def test_preparation_reuses_evidence_limits_supplements_and_preserves_target(
    tmp_path, preparation_input
):
    source, handle, other = preparation_input
    session = tmp_path / "session"
    args = dict(insert_handles=[handle], geometry_layers=["WALL"])
    original = source.read_bytes()
    first = collect_scene(session, source, **args)
    scene_bytes = (session / "scene-1.json").read_bytes()
    scene = json.loads(scene_bytes)
    assert {e["layer"] for e in scene["geometry"]} == {"WALL", "面积线"}
    assert first["phase"] == "evidence_ready" and not first["cad_write_authorized"]
    cached = collect_scene(session, source, **args)
    assert (
        cached["cached"]
        and cached["cache_hits"] == 1
        and len(cached["collections"]) == 1
    )
    assert (session / "scene-1.json").read_bytes() == scene_bytes
    with pytest.raises(ValueError, match="Keep the selected target"):
        collect_scene(session, source, insert_handles=[other], gap="north wall")
    with pytest.raises(ValueError, match="requires --gap"):
        collect_scene(session, source, bounds=(-100, -100, 10000, 5000), **args)
    supplement = collect_scene(
        session, source, bounds=(-100, -100, 10000, 5000), gap="north wall", **args
    )
    assert len(supplement["collections"]) == 2
    with pytest.raises(ValueError, match="budget exhausted"):
        collect_scene(
            session,
            source,
            bounds=(-200, -200, 11000, 6000),
            gap="which north face?",
            **args,
        )
    state = json.loads((session / "state.json").read_text())
    assert state["phase"] == "needs_clarification"
    assert state["decision"]["unresolved"] == ["which north face?"]
    assert (
        len(list(session.glob("scene-*.json"))) == 2 and source.read_bytes() == original
    )


def test_decisions_require_evidence_and_resolution_but_never_authorize_cad(
    tmp_path, preparation_input
):
    source, handle, _ = preparation_input
    session = tmp_path / "session"
    collect_scene(session, source, insert_handles=[handle])
    scene = json.loads((session / "scene-1.json").read_text())
    decision = {
        "edges": [
            {"segment": "outer", "sources": ["invented"], "rule": "given outline"}
        ],
        "unresolved": [],
    }
    with pytest.raises(ValueError, match="geometry IDs"):
        record_decision(session, decision)
    record_decision(
        session, {"edges": [], "unresolved": ["North: inner or outer face?"]}
    )
    with pytest.raises(ValueError, match="Collection is closed"):
        collect_scene(
            session,
            source,
            insert_handles=[handle],
            bounds=(0, 0, 10000, 5000),
            gap="north",
        )
    decision["edges"][0]["sources"] = [scene["geometry"][0]["id"]]
    with pytest.raises(ValueError, match="resolution"):
        record_decision(session, decision)
    decision["resolution"] = "User confirmed north inner face"
    result = record_decision(session, decision)
    assert result["phase"] == "ready_for_candidate"
    assert (
        not result["cad_write_authorized"] and not result["boundary_semantics_verified"]
    )
    assert "preflight" in result["next_action"]
    (session / "scene-1.json").write_text("{}")
    with pytest.raises(ValueError, match="modified"):
        record_decision(session, decision)


def test_source_change_invalidates_cache_and_decisions(tmp_path, preparation_input):
    source, handle, _ = preparation_input
    session = tmp_path / "session"
    collect_scene(session, source, insert_handles=[handle])
    doc = ezdxf.readfile(source)
    doc.modelspace().add_line((1, 2), (3, 4))
    doc.saveas(source)
    with pytest.raises(ValueError, match="Source changed"):
        collect_scene(session, source, insert_handles=[handle])
    with pytest.raises(ValueError, match="Source changed"):
        record_decision(session, {"edges": [], "unresolved": ["north?"]})
    assert json.loads((session / "state.json").read_text())["phase"] == "source_changed"


def test_failed_collection_can_use_remaining_supplement_without_reset(
    tmp_path, preparation_input
):
    source, handle, _ = preparation_input
    session = tmp_path / "session"
    with pytest.raises(ValueError, match="No supported selected geometry"):
        collect_scene(
            session, source, insert_handles=[handle], geometry_layers=["WRONG"]
        )
    result = collect_scene(
        session,
        source,
        insert_handles=[handle],
        geometry_layers=["WALL"],
        gap="Correct the missing wall layer",
    )
    assert [r["status"] for r in result["collections"]] == ["failed", "complete"]
    scene = json.loads((session / "scene-2.json").read_text())
    result = record_decision(
        session,
        {
            "edges": [
                {
                    "segment": "given outline",
                    "sources": [scene["geometry"][0]["id"]],
                    "rule": "Explicit test outline",
                }
            ],
            "unresolved": [],
        },
    )
    assert (
        result["phase"] == "ready_for_candidate" and not result["cad_write_authorized"]
    )


def test_task_relative_review_and_one_replacement_preserve_budget(
    tmp_path, preparation_input
):
    source, handle, other = preparation_input
    task = {
        "target": "核心筒",
        "include": ["楼梯", "电梯", "前室"],
        "exclude": [],
        "boundary_rule": "指定外墙面",
        "authority": "本次用户要求",
    }
    session = tmp_path / "task"
    first = collect_scene(session, source, insert_handles=[handle], task=task)
    assert first["phase"] == "target_review" and first["task"] == task
    scene = json.loads((session / "scene-1.json").read_text())
    old_id = scene["geometry"][0]["id"]
    decision = {
        "edges": [{"segment": "外侧", "sources": [old_id], "rule": "指定墙面"}],
        "unresolved": [],
    }
    with pytest.raises(ValueError):
        record_decision(session, decision)
    assess_target(
        session,
        {
            "result": "reject",
            "reason": "当前源没有任务指定的核心筒",
            "sources": [old_id],
        },
    )
    with pytest.raises(ValueError, match="existing task"):
        collect_scene(
            tmp_path / "new-session",
            source,
            insert_handles=[other],
            replace_reason="选错",
        )
    replaced = collect_scene(
        session,
        source,
        insert_handles=[other],
        replace_reason="核对局部图后改选核心筒源",
    )
    assert len(replaced["collections"]) == 2 and len(replaced["replacements"]) == 1
    assert replaced["task"] == task
    current = json.loads((session / "scene-2.json").read_text())["geometry"][0]["id"]
    assess_target(
        session,
        {"result": "accept", "reason": "该源符合本次核心筒目标", "sources": [current]},
    )
    with pytest.raises(ValueError, match="geometry IDs"):
        record_decision(session, decision)
    with pytest.raises(ValueError, match="Rejected target"):
        collect_scene(session, source, insert_handles=[handle])
    collect_scene(
        session,
        source,
        insert_handles=[other],
        bounds=(29000, 39000, 40000, 45000),
        gap="补相邻墙",
    )
    with pytest.raises(ValueError, match="budget exhausted"):
        collect_scene(
            session,
            source,
            insert_handles=[other],
            bounds=(28000, 38000, 41000, 46000),
            gap="仍缺墙",
        )
    assert len(json.loads((session / "state.json").read_text())["collections"]) == 3


def test_same_geometry_can_be_accepted_for_core_without_excluding_common_spaces(
    tmp_path, preparation_input
):
    source, handle, _ = preparation_input
    task = {
        "target": "核心筒",
        "include": ["前室", "楼梯", "管井"],
        "exclude": ["住宅"],
        "boundary_rule": "用户标定线",
        "authority": "用户当前任务",
    }
    session = tmp_path / "core"
    collect_scene(session, source, insert_handles=[handle], task=task)
    scene = json.loads((session / "scene-1.json").read_text())
    sid = scene["geometry"][0]["id"]
    assess_target(
        session, {"result": "accept", "reason": "已对照当前标定区域", "sources": [sid]}
    )
    decision = {
        "edges": [{"segment": "指定外围", "sources": [sid], "rule": "包含前室与管井"}],
        "unresolved": [],
    }
    result = record_decision(session, decision)
    assert result["phase"] == "ready_for_candidate" and result["task"]["exclude"] == [
        "住宅"
    ]
    with pytest.raises(ValueError, match="semantics cannot change"):
        collect_scene(
            session, source, insert_handles=[handle], task={**task, "exclude": ["前室"]}
        )


@pytest.fixture
def source_plan():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    block = doc.blocks.new("core", base_point=(10, 20))
    block.add_line((11, 22), (17, 25))
    root = doc.modelspace().add_blockref(
        "core", (100, 200), dxfattribs={"xscale": -1, "rotation": 90, "layer": "CORE"}
    )
    scene = extract_area_scene(doc, insert_handles=[root.dxf.handle])
    assert scene["target_review"]["roots"][0]["observed_wcs_bounds"] == pytest.approx(
        [95, 193, 98, 199]
    )
    edge = scene["geometry"][0]
    assert edge["layer"] == "CORE" and edge["source"]["layer"] == "0"
    decision = {"edges": [{"sources": [edge["id"]]}], "unresolved": []}
    plan = source_read_plan(scene, decision)
    return plan, deepcopy(plan["sources"]), root.dxf.handle, edge["source_handle"]


def test_source_checks_raw_coordinates_inherited_layer_and_mirror(source_plan):
    plan, entities, root, line = source_plan
    assert verify_sources(plan, entities)["source_fields_match"]
    assert len(plan["handles"]) == 2  # Leaf plus its ancestor, not all drawing objects.
    for handle, field, value in [
        (line, "start", [11, 22.01, 0]),
        (root, "xscale", 1),
        (line, "layer", "CORE"),
    ]:
        changed = deepcopy(entities)
        changed[handle][field] = value
        assert not verify_sources(plan, changed)["source_fields_match"]
    missing = deepcopy(entities)
    del missing[line]["end"]
    assert not verify_sources(plan, missing)["source_fields_match"]
    entities[root]["rotation"] += 360
    assert verify_sources(plan, entities)["source_fields_match"]


def test_capture_parses_actual_payloads_and_rejects_changed_context(source_plan):
    plan, entities, _, _ = source_plan
    context = dict(
        session_id="s",
        doc_id="d",
        active_doc_id="d",
        active_path=r"C:\\project\\a.dwg",
        revision=7,
        worker_generation=42,
    )

    def run(changed=None, layer_payload=None):
        contexts = iter([context, {**context, **(changed or {})}])

        def call(name, **args):
            if name == "system":
                return {"ready": True, "dispatcher": {"loaded": True}}
            if name == "transaction":
                return next(contexts)
            if name == "drawing":
                return {"INSUNITS": 4, "CMDACTIVE": 0, "CMDNAMES": ""}
            if name == "layer":
                return (
                    layer_payload
                    if layer_payload is not None
                    else {"layers": [{"name": "CORE", "color": 6}]}
                )
            if name == "entity" and args["operation"] == "get_many":
                ids = args["data"]["entity_ids"]
                assert len(ids) <= 200
                return {"entities": {h: entities[h] for h in ids}, "errors": {}}
            raise AssertionError("Only directed read operations allowed")

        return capture_sources(plan, call, expected_path=r"C:\project\a.dwg")

    assert run()["verification"]["source_fields_match"]
    for change in (
        {"revision": 8},
        {"worker_generation": 43},
        {"active_path": r"C:\other.dwg"},
    ):
        with pytest.raises(ValueError):
            run(change)
    with pytest.raises(ValueError, match="layer.list"):
        run(layer_payload=[{"name": "CORE"}])
    response = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"ok": True, "payload": {"layers": []}}),
                }
            ]
        }
    }
    assert decode_tool_payload(response) == {"layers": []}
    with pytest.raises(ValueError):
        decode_tool_payload({"ok": False, "error_code": "E_AUTOCAD_CRASHED"})


def test_target_ledger_preserves_associations_without_bbox_or_count_inference(tmp_path):
    doc = ezdxf.new()
    block = doc.blocks.new("unit")
    block.add_line((0, 0), (3000, 7000))
    known = doc.modelspace().add_blockref("unit", (0, 0))
    sibling = doc.modelspace().add_blockref(
        "unit", (0, 0)
    )  # Same bbox, different instance.
    unrelated = doc.blocks.new("unclassified")
    for i in range(50):
        unrelated.add_line((i, 0), (i, 100))
    unknown = doc.modelspace().add_blockref("unclassified", (0, 0))
    source = tmp_path / "input.dxf"
    doc.saveas(source)
    index = index_area_inserts(doc)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    index["source"] = {"path": str(source), "sha256": digest}
    evidence = tmp_path / "readback.json"
    evidence.write_text('{"recorded_readback":true}')
    record = {
        "kind": "户型",
        "task_scope": "不含阳台的套内单环",
        "association_basis": "original create request and source identity, not bbox overlap",
        "status": "semantics_unresolved",
        "saved": False,
        "source_insert": {
            k: v
            for k, v in index["inserts"][0].items()
            if k != "direct_child_types_only"
        },
        "boundary": {
            "type": "LWPOLYLINE",
            "handle": "ABC",
            "layer": "面积线",
            "closed": True,
            "points": [[0, 0], [3000, 0], [0, 7000]],
            "bulges": [0, 0, 0],
        },
        "evidence": [
            {
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }
        ],
    }
    ledger_path = tmp_path / "input.area-targets.json"
    drawing = r"C:\project\input.dwg"
    ledger = record_result(ledger_path, drawing, record)
    assert record_result(ledger_path, drawing, record) == ledger  # No duplicate record.
    result = select_targets(index, ledger, drawing_path=drawing, kind="户型")
    assert [c["insert"]["handle"] for c in result["candidates"]] == [sibling.dxf.handle]
    assert result["known_results"][0]["insert_handle"] == known.dxf.handle
    assert result["known_results"][0]["status"] == "semantics_unresolved"
    assert result["unclassified_count"] == 1
    assert unknown.dxf.handle not in [
        c["insert"]["handle"] for c in result["candidates"]
    ]
    assert not result["cad_write_authorized"]

    core = select_targets(index, ledger, drawing_path=drawing, kind="核心筒")
    assert core["known_results"] == [] and core["candidates"] == []
    assert (
        core["phase"] == "needs_localization"
        and not core["all_drawing_targets_completed"]
    )
    second = deepcopy(record)
    second["source_insert"] = {
        k: v for k, v in index["inserts"][1].items() if k != "direct_child_types_only"
    }
    second["boundary"]["handle"] = "ABD"
    ledger = record_result(ledger_path, drawing, second)
    exhausted = select_targets(index, ledger, drawing_path=drawing, kind="户型")
    assert exhausted["phase"] == "needs_localization" and not exhausted["candidates"]
    assert not exhausted["all_drawing_targets_completed"]

    changed = deepcopy(index)
    changed["inserts"][0]["xscale"] = -1
    conflict = select_targets(changed, ledger, drawing_path=drawing, kind="户型")
    assert conflict["phase"] == "record_conflict" and not conflict["candidates"]
    for action in (
        lambda: select_targets(
            index, ledger, drawing_path=r"C:\other.dwg", kind="户型"
        ),
        lambda: record_result(ledger_path, drawing, {**record, "saved": True}),
        lambda: record_result(
            tmp_path / "bad.json",
            drawing,
            {**record, "boundary": {"bounds": [0, 0, 1, 1]}},
        ),
    ):
        with pytest.raises(ValueError):
            action()
    evidence.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="evidence changed"):
        select_targets(index, ledger, drawing_path=drawing, kind="户型")


def test_target_select_cli_treats_missing_ledger_as_no_records(tmp_path, capsys):
    import json as _json

    from autocad_mcp.area_targets import main as targets_main

    doc = ezdxf.new()
    doc.blocks.new("unit").add_line((0, 0), (3000, 7000))
    doc.modelspace().add_blockref("unit", (0, 0))
    source = tmp_path / "input.dxf"
    doc.saveas(source)
    index = index_area_inserts(doc)
    index["source"] = {
        "path": str(source),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    index_path = tmp_path / "index.json"
    index_path.write_text(_json.dumps(index))
    output = tmp_path / "selection.json"
    code = targets_main(
        [
            "select", "--index", str(index_path), "--kind", "户型",
            "--output", str(output), "--ledger", str(tmp_path / "input.area-targets.json"),
            "--drawing", r"C:\plan.dwg",
        ]
    )
    assert code == 0, capsys.readouterr().out
    result = _json.loads(output.read_text())
    assert result["known_results"] == []
    assert not (tmp_path / "input.area-targets.json").exists()


def test_failed_collection_can_still_record_a_question_but_not_edges(
    tmp_path, preparation_input
):
    source, handle, _ = preparation_input
    session = tmp_path / "session"
    with pytest.raises(ValueError, match="No supported selected geometry"):
        collect_scene(
            session, source, insert_handles=[handle], geometry_layers=["WRONG"]
        )
    with pytest.raises(ValueError, match="Collect usable evidence"):
        record_decision(
            session,
            {"edges": [{"segment": "n", "sources": ["x"], "rule": "r"}], "unresolved": ["q?"]},
        )
    result = record_decision(
        session, {"edges": [], "unresolved": ["North room: storage or balcony?"]}
    )
    assert result["phase"] == "needs_clarification"
    assert not result["cad_write_authorized"]


def test_capture_accepts_declared_units_only_for_unspecified_drawings(source_plan):
    plan, entities, _, _ = source_plan
    context = dict(session_id="s", doc_id="d", active_doc_id="d",
                   active_path=r"C:\\project\\a.dwg", revision=7, worker_generation=42)

    def run(insunits, declared):
        local = {**plan, "insunits": insunits}

        def call(name, **args):
            if name == "system":
                return {"ready": True, "dispatcher": {"loaded": True}}
            if name == "transaction":
                return context
            if name == "drawing":
                return {"INSUNITS": insunits, "CMDACTIVE": 0, "CMDNAMES": ""}
            if name == "layer":
                return {"layers": []}
            ids = args["data"]["entity_ids"]
            return {"entities": {h: entities[h] for h in ids}, "errors": {}}

        return capture_sources(local, call, expected_path=r"C:\project\a.dwg",
                               declared_units=declared)

    with pytest.raises(ValueError, match="mm/m"):
        run(0, None)
    with pytest.raises(ValueError, match="evidence"):
        run(0, {"units": "mm", "evidence": ""})
    snapshot = run(0, {"units": "mm", "evidence": "200 exterior walls, 3000 storey"})
    assert snapshot["units_declaration"]["code"] == 4
    with pytest.raises(ValueError, match="mm/m"):
        run(1, {"units": "mm", "evidence": "x"})


def test_invalidated_result_frees_reused_handle_and_stays_as_history(tmp_path):
    from autocad_mcp.area_targets import invalidate_result

    doc = ezdxf.new()
    doc.blocks.new("unit").add_line((0, 0), (3000, 7000))
    known = doc.modelspace().add_blockref("unit", (0, 0))
    source = tmp_path / "input.dxf"
    doc.saveas(source)
    index = index_area_inserts(doc)
    index["source"] = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    evidence = tmp_path / "readback.json"
    evidence.write_text('{"readback":true}')
    record = {
        "kind": "户型", "task_scope": "t", "association_basis": "create request",
        "status": "readback_verified", "saved": False,
        "source_insert": {k: v for k, v in index["inserts"][0].items() if k != "direct_child_types_only"},
        "boundary": {"type": "LWPOLYLINE", "handle": "9782C", "layer": "面积线", "closed": True,
                     "points": [[0, 0], [1, 0], [0, 1]], "bulges": [0, 0, 0]},
        "evidence": [{"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
    }
    ledger_path, drawing = tmp_path / "input.area-targets.json", r"C:\p\input.dwg"
    record_result(ledger_path, drawing, record)
    reused = deepcopy(record)
    reused["boundary"]["points"] = [[0, 0], [2, 0], [0, 2]]
    with pytest.raises(ValueError, match="already recorded"):
        record_result(ledger_path, drawing, reused)
    with pytest.raises(ValueError, match="reason"):
        invalidate_result(ledger_path, drawing, "9782C", " ")
    invalidate_result(ledger_path, drawing, "9782C", "unsaved; CAD session ended")
    with pytest.raises(ValueError, match="No active record"):
        invalidate_result(ledger_path, drawing, "9782C", "again")
    ledger = record_result(ledger_path, drawing, reused)  # Reused handle is free again.
    assert [r["status"] for r in ledger["records"]] == ["invalidated", "readback_verified"]
    evidence.write_text("changed")  # History evidence is not re-verified.
    result = select_targets(index, json.loads(ledger_path.read_text())
                            | {"records": ledger["records"][:1]}, drawing_path=drawing, kind="户型")
    assert result["known_results"] == []
    assert result["invalidated_results"][0]["boundary_handle"] == "9782C"
    assert known.dxf.handle == result["invalidated_results"][0]["insert_handle"]
