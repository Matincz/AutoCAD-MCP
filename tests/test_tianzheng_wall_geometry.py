from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from autocad_mcp import server
from autocad_mcp.tianzheng import TianzhengInputError
from autocad_mcp.tianzheng_wall_geometry import (
    apply, insert_matrix, matmul, validate_walls_request, wall_geometry,
)
from tests.test_tianzheng import Runtime


def close(a, b, tol=1e-6):
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b))


def test_insert_matrix_rotation_scale_and_base_point():
    m = insert_matrix([100, 200, 0], math.pi / 2, [2, 2, 1], base=[10, 0, 0])
    # (20,0) - base -> (10,0) -> scale (20,0) -> rot90 (0,20) -> + insertion
    assert close(apply(m, [20, 0, 0]), [100, 220, 0])


def test_insert_matrix_mirror_x_and_extrusion_normal_flip():
    mirrored = insert_matrix([0, 0, 0], 0.0, [-1, 1, 1])
    assert close(apply(mirrored, [5, 3, 0]), [-5, 3, 0])
    # Normal (0,0,-1): arbitrary axis gives OCS X = -WCS X, so it is a mirror too.
    flipped = insert_matrix([0, 0, 0], 0.0, [1, 1, 1], normal=[0, 0, -1])
    assert close(apply(flipped, [5, 3, 0]), [-5, 3, 0])


def test_nested_composition_rotation_then_mirror():
    outer = insert_matrix([1000, 0, 0], math.pi / 2, [1, 1, 1])
    inner = insert_matrix([0, 100, 0], 0.0, [-1, 1, 1])
    m = matmul(outer, inner)
    # inner: (10,0)->(-10,100); outer rot90: (-100,-10) + (1000,0)
    assert close(apply(m, [10, 0, 0]), [900, -10, 0])


def test_straight_wall_edges_and_thickness_follow_left_width():
    geo = wall_geometry({"start": [0, 0, 0], "end": [1000, 0, 0], "left_width": 150, "right_width": 50},
                        insert_matrix([0, 0, 0], 0.0, [1, 1, 1]))
    assert geo["kind"] == "line" and geo["thickness"] == 200 and not geo["mirrored"]
    assert geo["left_edge"] == {"start": [0, 150, 0], "end": [1000, 150, 0]}
    assert geo["right_edge"] == {"start": [0, -50, 0], "end": [1000, -50, 0]}


def test_mirrored_wall_keeps_source_side_semantics():
    geo = wall_geometry({"start": [0, 0, 0], "end": [1000, 0, 0], "left_width": 150, "right_width": 50},
                        insert_matrix([0, 0, 0], 0.0, [1, -1, 1]))
    assert geo["mirrored"]
    # LeftWidth side is still reported as left_edge, now below the centre line in WCS.
    assert geo["left_edge"]["start"] == [0, -150, 0]
    assert geo["right_edge"]["start"] == [0, 50, 0]


def test_arc_wall_from_center_transforms_and_flips_bulge_under_mirror():
    local = {"start": [100, 0, 0], "end": [0, 100, 0], "left_width": 10, "right_width": 20,
             "radius": 100, "center": [0, 0, 0]}
    plain = wall_geometry(local, insert_matrix([0, 0, 0], 0.0, [1, 1, 1]))
    assert plain["kind"] == "arc" and math.isclose(plain["centerline"]["bulge"], math.tan(math.pi / 8))
    # CCW arc: left side is toward the centre.
    assert close(plain["left_edge"]["start"], [90, 0, 0]) and close(plain["right_edge"]["start"], [120, 0, 0])
    mirrored = wall_geometry(local, insert_matrix([500, 0, 0], 0.0, [-2, 2, 1]))
    assert mirrored["mirrored"] and math.isclose(mirrored["centerline"]["bulge"], -math.tan(math.pi / 8))
    assert close(mirrored["center"], [500, 0, 0]) and math.isclose(mirrored["radius"], 200)
    assert close(mirrored["centerline"]["end"], [500, 200, 0]) and math.isclose(mirrored["thickness"], 60)


def test_arc_wall_from_bulge_and_unrepresentable_cases():
    geo = wall_geometry({"start": [100, 0, 0], "end": [-100, 0, 0], "left_width": 5, "right_width": 5,
                         "radius": 100, "bulge": 1.0}, insert_matrix([0, 0, 0], 0.0, [1, 1, 1]))
    assert close(geo["center"], [0, 0, 0]) and math.isclose(geo["radius"], 100)
    skew = wall_geometry({"start": [100, 0, 0], "end": [-100, 0, 0], "left_width": 5, "right_width": 5,
                          "radius": 100, "bulge": 1.0}, insert_matrix([0, 0, 0], 0.0, [1, 2, 1]))
    assert "error" in skew
    unknown = wall_geometry({"start": [100, 0, 0], "end": [-100, 0, 0], "left_width": 5, "right_width": 5,
                             "radius": 100}, insert_matrix([0, 0, 0], 0.0, [1, 1, 1]))
    assert "error" in unknown


def test_validation_limits_handles_and_depth():
    assert validate_walls_request({"insert_handles": ["74ce2", "74CE2"]}) == {"insert_handles": ["74CE2"], "max_depth": 8}
    for bad in ({"insert_handles": []}, {"insert_handles": ["XYZ"]}, {"insert_handles": ["1"], "max_depth": 0},
                {"insert_handles": ['1") (command "erase"']}):
        with pytest.raises(TianzhengInputError):
            validate_walls_request(bad)


# --- fake COM block tree ---------------------------------------------------

class Block:
    def __init__(self, items, origin=(0.0, 0.0, 0.0)):
        self.items, self.Origin, self.IsXRef = items, list(origin), False
    @property
    def Count(self): return len(self.items)
    def Item(self, index): return self.items[index]


def wall(handle, start, end, left, right, low, high):
    return SimpleNamespace(Handle=handle, ObjectName="TDbWall", Layer="A-WALL", StartPoint=start, EndPoint=end,
                           LeftWidth=left, RightWidth=right, Radius=0.0, GetBoundingBox=lambda: (low, high))


def insert(handle, name, point, rotation=0.0, sx=1.0, sy=1.0):
    return SimpleNamespace(Handle=handle, ObjectName="AcDbBlockReference", Name=name, InsertionPoint=point,
                           Rotation=rotation, XScaleFactor=sx, YScaleFactor=sy, ZScaleFactor=1.0, Normal=[0, 0, 1])


def runtime_with(blocks, model_inserts):
    runtime = Runtime()
    runtime.document.Blocks = SimpleNamespace(Item=lambda name: blocks[name])
    runtime.document.HandleToObject = lambda handle: model_inserts[handle]
    return runtime


@pytest.mark.asyncio
async def test_walls_reads_real_74ce2_wall_and_matches_bounds_snapshot():
    # test-cad.dwg: INSERT 74CE2 of FFCVCXC; wall 74F4F block-local bounds from wall-snapshot.json.
    w = wall("74F4F", [-50, -200, 0], [-50, -2400, 0], 100, 100, [-150, -2400, 0], [50, -200, 3000])
    ref = insert("74CE2", "FFCVCXC", [254355.7889209801, 18812.65321285883, 0.0])
    runtime = runtime_with({"FFCVCXC": Block([w])}, {"74CE2": ref})
    result = await runtime._tianzheng_walls(runtime.context, {"insert_handles": ["74CE2"], "max_depth": 8})
    assert result.ok and result.payload["count"] == 1 and runtime.document.sent == []
    item = result.payload["walls"][0]
    expected_box = [254205.788921, 16412.653213, 254405.788921, 18612.653213]  # walls-wcs.json
    box = item["bounds_wcs"]["min"][:2] + item["bounds_wcs"]["max"][:2]
    assert close(box, expected_box, 1e-5)
    xs = [item[e][p][0] for e in ("left_edge", "right_edge") for p in ("start", "end")]
    ys = [item[e][p][1] for e in ("left_edge", "right_edge") for p in ("start", "end")]
    assert close([min(xs), min(ys), max(xs), max(ys)], expected_box, 1e-5)
    assert item["path"] == ["74CE2"] and item["thickness"] == 200


@pytest.mark.asyncio
async def test_walls_traverses_nested_mirrored_rotated_blocks_with_cache():
    inner_wall = wall("W2", [0, 0, 0], [100, 0, 0], 20, 10, [0, -10, 0], [100, 20, 0])
    blocks = {
        "OUTER": Block([wall("W1", [0, 0, 0], [0, 50, 0], 5, 5, [-5, 0, 0], [5, 50, 0]),
                        insert("I1", "INNER", [10, 0, 0], sx=-1.0),
                        insert("I2", "INNER", [0, 300, 0])], origin=(0, 0, 0)),
        "INNER": Block([inner_wall], origin=(0, 0, 0)),
    }
    runtime = runtime_with(blocks, {"A0": insert("A0", "OUTER", [1000, 0, 0], rotation=math.pi / 2)})
    result = await runtime._tianzheng_walls(runtime.context, {"insert_handles": ["A0"], "max_depth": 8})
    walls = {(w["handle"], tuple(w["path"])): w for w in result.payload["walls"]}
    assert len(walls) == 3
    mirrored = walls[("W2", ("A0", "I1"))]
    # INNER (100,0) -> mirror (-100,0) -> +I1 (-90,0) -> OUTER rot90 (0,-90) -> +A0 (1000,-90)
    assert mirrored["mirrored"] and close(mirrored["centerline"]["end"], [1000, -90, 0])
    # LeftWidth 20 at local +Y -> mirror keeps +Y -> rot90 -> -X
    assert close(mirrored["left_edge"]["start"], [980, 10, 0])
    plain = walls[("W2", ("A0", "I2"))]
    assert not plain["mirrored"] and close(plain["centerline"]["start"], [700, 0, 0])
    # Depth limit stops at the outer block.
    shallow = await runtime._tianzheng_walls(runtime.context, {"insert_handles": ["A0"], "max_depth": 1})
    assert shallow.payload["count"] == 1 and {s["reason"] for s in shallow.payload["skipped"]} == {"max_depth_reached"}


@pytest.mark.asyncio
async def test_walls_falls_back_when_block_item_hides_wall_members():
    class GenericEntity:  # Block.Item wrapper bound to the base entity interface
        Handle, ObjectName, Layer = "W9", "TDbWall", "A-WALL"
        def GetBoundingBox(self): return ([0, -5, 0], [100, 5, 0])
        def __getattr__(self, name): raise AttributeError(f"Item.{name}")
    full = wall("W9", [0, 0, 0], [100, 0, 0], 5, 5, [0, -5, 0], [100, 5, 0])
    runtime = runtime_with({"B": Block([GenericEntity()])}, {"A1": insert("A1", "B", [0, 0, 0]), "W9": full})
    result = await runtime._tianzheng_walls(runtime.context, {"insert_handles": ["A1"], "max_depth": 8})
    item = result.payload["walls"][0]
    assert item["access"] == "handle_to_object" and item["thickness"] == 10 and not result.payload["errors"]


@pytest.mark.asyncio
async def test_walls_rejects_non_insert_handle():
    runtime = runtime_with({}, {"1": SimpleNamespace(ObjectName="TDbWall")})
    result = await runtime._tianzheng_walls(runtime.context, {"insert_handles": ["1"], "max_depth": 8})
    assert not result.ok and result.error_code == "E_TIANZHENG_OBJECT_TYPE"


@pytest.mark.asyncio
async def test_server_walls_is_read_only_and_validated(monkeypatch):
    calls = []
    class Backend:
        async def tianzheng_command(self, operation, context, data):
            calls.append((operation, data))
            from autocad_mcp.backends.base import CommandResult
            return CommandResult(ok=True, payload={"walls": []})
    async def backend(): return Backend()
    async def guard(_backend, doc_id):
        from autocad_mcp.backends.base import CommandResult
        return CommandResult(ok=True, payload={"doc_id": doc_id})
    async def attach(_backend, result, **_kwargs): return result
    monkeypatch.setattr(server, "get_backend", backend)
    monkeypatch.setattr(server, "_guard_document_read", guard)
    monkeypatch.setattr(server, "_attach_document_context", attach)
    raw = await server.tianzheng(operation="walls", doc_id="d", data={"insert_handles": ["74ce2"]})
    value = json.loads(raw) if isinstance(raw, str) else raw.structuredContent
    assert value["ok"] and calls == [("walls", {"insert_handles": ["74CE2"], "max_depth": 8})]
    bad = await server.tianzheng(operation="walls", doc_id="d", data={"insert_handles": ["74ce2"], "points": []})
    bad = json.loads(bad) if isinstance(bad, str) else bad.structuredContent
    assert not bad["ok"] and len(calls) == 1


def test_wall_from_bounds_infers_orthogonal_walls_only():
    from autocad_mcp.tianzheng_wall_geometry import identity, insert_matrix, wall_from_bounds

    # 74CE2/74F4F live bounds: a 200 thick wall along Y.
    bounds = {"min": [254205.788921, 16412.653213, 0.0], "max": [254405.788921, 18612.653213, 3000.0]}
    wall = wall_from_bounds(bounds, identity())
    assert wall["usable"] and wall["direction"] == "y" and wall["thickness"] == 200.0
    assert wall["centerline"]["start"][0] == 254305.788921
    assert {f["start"][0] for f in wall["faces"]} == {254205.788921, 254405.788921}
    mirrored = insert_matrix([0, 0, 0], 0.0, [-1, 1, 1])
    assert wall_from_bounds(bounds, mirrored)["usable"]
    rotated = insert_matrix([0, 0, 0], 0.5, [1, 1, 1])
    assert not wall_from_bounds(bounds, rotated)["usable"]
    square = {"min": [0, 0, 0], "max": [200, 300, 3000]}
    assert not wall_from_bounds(square, identity())["usable"]
