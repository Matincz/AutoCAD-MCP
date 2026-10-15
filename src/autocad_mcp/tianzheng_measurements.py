"""Read-only T30 measurement semantics; no annotation or drawing writes."""
from autocad_mcp.tianzheng import TianzhengInputError, TianzhengRecipeUnavailable, encode_response

MEASUREMENT_RECIPES = {"TGETEXTEND", "TLENGTHSUMM"}
LIVE_PENDING_MEASUREMENTS = set()


def validate_measurement(entry,data):
    if entry["command"].upper() not in MEASUREMENT_RECIPES:
        raise TianzhengRecipeUnavailable("No read-only measurement recipe for this command")
    if set(data)-{"command","selection_handles"}:
        raise TianzhengInputError("Measurement accepts only command and selection_handles")
    handles=data.get("selection_handles")
    encode_response(entry,{"type":"selection","value":handles})
    if len({h.upper() for h in handles})!=len(handles):raise TianzhengInputError("Measurement handles must be distinct")
    return {"command":entry["command"],"selection_handles":[h.upper() for h in handles]}
