"""Registry of bounded native T30 command recipes (one fixed SendCommand sequence each).

Every recipe declares input validation, a precondition plan computed from a
fresh model-space snapshot, the complete server-generated token sequence and an
independent postcondition check. Only commands that run entirely at the command
line (no dialog, no remembered mode that changes the prompt order) qualify.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from autocad_mcp import tianzheng_walls as walls


@dataclass(frozen=True)
class NativeRecipe:
    label: str
    required: tuple
    validate: Callable  # (entry, data) -> normalized data
    plan: Callable      # (before, data, view) -> plan; view() returns the top-view window
    sequence: Callable  # (entry, data, plan) -> str
    verify: Callable    # (plan, before, after) -> list of failed checks
    postcondition: str
    live_verified: bool = False
    notes: tuple = field(default_factory=tuple)
    environment: bool = False  # plan/verify also receive layer state before/after


def _corner_plan(before, data, view):
    trims, connector = walls.corner_trims(data)
    return walls.plan_corner(before, data["selection_handles"], data["points"], trims, connector, view())


NATIVE_RECIPES: dict[str, NativeRecipe] = {}


def register(command: str, recipe: NativeRecipe):
    NATIVE_RECIPES[command.upper()] = recipe


for _name in ("TFILLET", "TCHAMFER"):
    register(_name, NativeRecipe("wall_corner_native", ("selection_handles", "points", "parameters"),
        walls.validate_wall_corner_recipe, _corner_plan,
        lambda entry, data, plan: walls.corner_sequence(entry, data), walls.verify_corner,
        "wall_extents_connector_wall_and_unselected_objects_preserved", live_verified=True))
register("TKATANA", NativeRecipe("wall_split_native", ("selection_handles", "points"),
    walls.validate_wall_split_recipe,
    lambda before, data, view: walls.plan_split(before, data["selection_handles"][0], data["points"][0], view()),
    lambda entry, data, plan: walls.split_sequence(entry, data), walls.verify_split,
    "two_collinear_wall_pieces_and_unselected_objects_preserved", live_verified=True))
register("TOFFSET", NativeRecipe("wall_clear_offset_native", ("selection_handles", "points", "parameters"),
    walls.validate_wall_offset_recipe,
    lambda before, data, view: walls.plan_offset(before, data["selection_handles"][0], data["points"][0], data["parameters"]["distance"], view()),
    lambda entry, data, plan: walls.offset_sequence(entry, data), walls.verify_offset,
    "parallel_wall_at_clear_distance_and_existing_objects_preserved", live_verified=True))
register("TCONVERTCURTAIN", NativeRecipe("wall_curtain_conversion_native", ("selection_handles", "parameters"),
    walls.validate_wall_conversion_recipe,
    lambda before, data, view: walls.plan_conversion(before, data["selection_handles"], data["parameters"]),
    lambda entry, data, plan: walls.conversion_sequence(entry, data), walls.verify_conversion,
    "material_converted_footprint_and_unselected_objects_preserved"))


def native_recipe(command: str) -> NativeRecipe | None:
    # Late import: recipe modules register themselves on import.
    from autocad_mcp import tianzheng_native_p0  # noqa: F401
    return NATIVE_RECIPES.get(command.upper())


def native_commands() -> set[str]:
    from autocad_mcp import tianzheng_native_p0  # noqa: F401
    return set(NATIVE_RECIPES)
