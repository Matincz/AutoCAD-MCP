"""Command semantics implemented with bounded, installed ActiveX properties.

No caller property names, method names, LISP or file paths are executed.
"""
from __future__ import annotations

import re
from autocad_mcp.tianzheng import TianzhengInputError, encode_response, finite_number

PROPERTY_RECIPES = {
    "TCHHEIGHT": {"height": "Height", "elevation": "Elevation"},
    "TWALLTHICK": {"width": ("LeftWidth", "RightWidth")},
    "TCHWALLTERM": {"closed": "EndClose"},
    "TGCHANGELAYER": {"layer": "Layer"},
    "TPELCHANGECOLOR": {"color": "Color"},
    "TMARKINTWALL": {"internal":None},
    "TMARKEXTWALL": {"outside":None,"pick_point":None},
    "TCHEWALLHEIGHT": {"height":"Height","elevation":"Elevation"},
    "TEXTTHICK": {"inside_width":None,"outside_width":None},
}
LIVE_PENDING_PROPERTIES=set()


def validate_property_recipe(entry: dict, handles: list, parameters: dict) -> dict:
    command = entry["command"].upper()
    if command not in PROPERTY_RECIPES:
        raise TianzhengInputError("No property recipe for this command")
    encode_response(entry, {"type": "selection", "value": handles})
    if len({h.upper() for h in handles}) != len(handles):
        raise TianzhengInputError("Selection handles must be distinct")
    fields = PROPERTY_RECIPES[command]
    if not isinstance(parameters,dict) or not parameters or set(parameters) - set(fields):
        raise TianzhengInputError(f"parameters must contain only {', '.join(fields)}")
    result = {}
    if command=="TEXTTHICK" and set(parameters)!={"inside_width","outside_width"}:
        raise TianzhengInputError("Exterior thickness needs inside_width and outside_width")
    if command=="TMARKEXTWALL" and (len(handles)!=1 or set(parameters)!={"outside","pick_point"}):
        raise TianzhengInputError("Exterior classification needs one wall, outside and a world-coordinate wall-skin pick_point")
    for field, value in parameters.items():
        if field in {"height", "width", "elevation", "inside_width", "outside_width"}:
            finite_number(value)
            if field not in {"elevation","inside_width"} and value <= 0 or field=="inside_width" and value<0:
                raise TianzhengInputError("Height and width must be positive")
            result[field] = float(value)
        elif field=="internal":
            if value is not True:raise TianzhengInputError("internal must be true; use TMarkExtWall for exterior walls")
            result[field]=True
        elif field=="outside":
            if not isinstance(value,str) or value not in {"left","right"}:raise TianzhengInputError("outside must be left or right; double-exterior mode is not covered")
            result[field]=value
        elif field=="pick_point":
            encode_response(entry,{"type":"point","value":value})
            result[field]=[float(v) for v in value]
        elif field == "closed":
            if not isinstance(value,bool):
                raise TianzhengInputError("closed must be boolean")
            result[field] = value
        elif field == "color":
            if isinstance(value,bool) or not isinstance(value,int) or not 0 <= value <= 256:
                raise TianzhengInputError("color must be an AutoCAD color index from 0 to 256")
            result[field] = value
        elif field == "layer":
            if not isinstance(value,str) or not value or len(value)>255 or re.search(r'[<>/\\":;?*|=\x00-\x1f]',value):
                raise TianzhengInputError("layer must be a valid AutoCAD layer name")
            result[field] = value
    return result


def property_assignments(command: str, parameters: dict) -> dict:
    if command=="TEXTTHICK":return {}  # Assigned per wall's verified exterior side.
    if command in {"TMARKINTWALL","TMARKEXTWALL"}:
        raise TianzhengInputError("Classification requires the native command; IOFeature does not set IsExternal")
    result = {}
    for field,value in parameters.items():
        properties = PROPERTY_RECIPES[command][field]
        if isinstance(properties,tuple):
            result.update({prop:value/2 for prop in properties})
        else:
            result[properties] = ("是" if value else "否") if field == "closed" else value
    return result


def validate_opening_parameters(data: dict) -> dict:
    from autocad_mcp.tianzheng import find_command
    if set(data)!={"selection_handles","parameters"}:
        raise TianzhengInputError("set_opening requires selection_handles and parameters")
    handles=data["selection_handles"]
    encode_response(find_command("TOpening"),{"type":"selection","value":handles})
    if len(handles)!=1:
        raise TianzhengInputError("set_opening currently accepts one initialized ordinary door/window")
    parameters=data["parameters"]
    if not isinstance(parameters,dict) or set(parameters)!={"kind","width","height","sill"}:
        raise TianzhengInputError("Opening parameters must be kind, width, height and sill")
    if parameters["kind"] not in ("door","window"):
        raise TianzhengInputError("Opening kind must be door or window; special openings are not covered")
    values={"kind":parameters["kind"]}
    for name in ("width","height","sill"):
        finite_number(parameters[name])
        if parameters[name]<0 or (name!="sill" and parameters[name]==0):
            raise TianzhengInputError("Opening width/height must be positive and sill non-negative")
        values[name]=float(parameters[name])
    return {"selection_handles":[handles[0].upper()],"parameters":values}
