"""Bounded world-coordinate translation, including initialized T30 objects."""
from autocad_mcp.tianzheng import TianzhengInputError, encode_response, finite_number

DISTRIBUTION_RECIPES = {"HORIDISTRIBUTION":0,"VERTDISTRIBUTION":1}
TRANSFORM_RECIPES = {"TMOVE", "TDRAGMOVE", "TDRAGCOPY"} | set(DISTRIBUTION_RECIPES)
LIVE_PENDING_TRANSFORMS = set()


def validate_transform_recipe(entry, data):
    if set(data)-{"command", "selection_handles", "parameters"}:
        raise TianzhengInputError("Translation accepts only selection_handles and parameters.offset")
    handles = data.get("selection_handles")
    encode_response(entry, {"type":"selection", "value":handles})
    if len(set(h.upper() for h in handles)) != len(handles):
        raise TianzhengInputError("Translation handles must be distinct")
    parameters = data.get("parameters")
    if entry["command"].upper() in DISTRIBUTION_RECIPES:
        if len(handles)<2 or not isinstance(parameters,dict) or set(parameters)!={"spacing"}:
            raise TianzhengInputError("Distribution needs at least two handles and parameters.spacing")
        finite_number(parameters["spacing"])
        if parameters["spacing"]<=0:raise TianzhengInputError("Distribution spacing must be positive")
        return {"command":entry["command"],"selection_handles":[h.upper() for h in handles],
                "parameters":{"spacing":float(parameters["spacing"])}}
    if not isinstance(parameters,dict) or set(parameters)!={"offset"}:
        raise TianzhengInputError("Translation requires parameters: {offset: [dx,dy,dz]}")
    vector = parameters["offset"]
    if not isinstance(vector,list) or len(vector) not in (2,3):
        raise TianzhengInputError("offset must be a two or three dimensional vector")
    for value in vector: finite_number(value)
    vector = [float(value) for value in vector]+([0.0] if len(vector)==2 else [])
    if not any(vector): raise TianzhengInputError("offset must not be zero")
    return {"command":entry["command"], "selection_handles":[h.upper() for h in handles],
            "parameters":{"offset":vector}}
