"""Typed creation using initialized T30 templates, never empty ARX constructors.

The first rectangle/column template must be produced by a supported T30 command
and validated in an isolated drawing. Rectangular columns have passed live
placement, dimensions and elevation readback; rectangles have also passed.
"""
from __future__ import annotations

import math
from autocad_mcp.tianzheng import TianzhengInputError, encode_response, finite_number

TEMPLATE_RECIPES = {
    "TGCOLUMN": {"native_class":"TDbColumn", "numeric_properties":{"width":"Width","depth":"Deep","height":"Height"}, "required_parameters":{"width","depth","height"}},
    "TRECT": {"native_class":"TDbRect", "numeric_properties":{"length":"Length","width":"Width","height":"Height"}, "required_parameters":{"length","width"}},
}

VERIFIED_TEMPLATE_RECIPES = {"TGCOLUMN", "TRECT"}
COLUMN_SECTIONS = {
    "circle": ("圆形", "diameter", "Diameter", 1.0, 1.0),
    "hexagon": ("正六边形", "side_length", "SideLength", 2.0, math.sqrt(3)),
    "octagon": ("正八边形", "side_length", "SideLength", 1+math.sqrt(2), 1+math.sqrt(2)),
}


def validate_template_recipe(entry: dict, data: dict) -> dict:
    spec=TEMPLATE_RECIPES[entry["command"].upper()]
    if set(data)-{"command","template_handle","points","parameters"}:
        raise TianzhengInputError("Template creation accepts only command, template_handle, points and parameters")
    encode_response(entry,{"type":"selection","value":[data.get("template_handle")]})
    points=data.get("points")
    if not isinstance(points,list) or not 1 <= len(points) <= 32:
        raise TianzhengInputError("Template creation needs 1 to 32 placement points")
    normalized=[]
    for point in points:
        encode_response(entry,{"type":"point","value":point})
        normalized.append([float(v) for v in point]+([0.0] if len(point)==2 else []))
    parameters=data.get("parameters")
    shape = parameters.get("shape") if isinstance(parameters,dict) else None
    if entry["command"].upper()=="TGCOLUMN" and isinstance(shape,str) and shape in COLUMN_SECTIONS:
        _,dimension,prop,_,_=COLUMN_SECTIONS[shape]
        spec={"required_parameters":{dimension,"height","shape"},"numeric_properties":{dimension:prop,"height":"Height","shape":None}}
    if not isinstance(parameters,dict) or not spec["required_parameters"] <= set(parameters) or set(parameters)-set(spec["numeric_properties"]):
        raise TianzhengInputError("Template parameters do not match this shape's schema")
    values={}
    for key,value in parameters.items():
        if key=="shape":
            values[key]=value
            continue
        finite_number(value)
        if value<0 or value==0 and (key!="height" or entry["command"].upper()=="TGCOLUMN"):
            raise TianzhengInputError("Shape dimensions must be positive; only rectangle height can be zero")
        values[key]=float(value)
    return {"command":entry["command"],"template_handle":data["template_handle"].upper(),"points":normalized,"parameters":values}
