"""Out-of-band ESC for a T30 command left waiting at its own prompt.

While AutoCAD waits for input it rejects COM calls, so COM cancel cannot work.
This posts exactly two ESC key messages to the PID-bound AutoCAD command
window. It is a recovery path only: no other keys, no drawing input.
"""
from __future__ import annotations

import ntpath

from autocad_mcp.tianzheng import TianzhengInputError

PROTECTED_NAME_TOKENS = ("沈阳招商户型",)


def escape_guard(document_name: str, window_title: str, managed_drawings_root: str) -> str:
    """Return the drawing file name if ESC may be posted, else raise."""
    if not isinstance(document_name, str) or not document_name.lower().endswith(".dwg"):
        raise TianzhengInputError("escape needs the isolated test drawing's full .dwg path")
    path = ntpath.normcase(ntpath.normpath(document_name))
    root = ntpath.normcase(ntpath.normpath(managed_drawings_root))
    if not path.startswith(root.rstrip("\\") + "\\"):
        raise TianzhengInputError("escape is limited to drawings inside the managed MCP output folder")
    name = ntpath.basename(document_name)
    if any(token in name or token in window_title for token in PROTECTED_NAME_TOKENS):
        raise TianzhengInputError("escape refused: protected project drawing")
    if name.casefold() not in (window_title or "").casefold():
        raise TianzhengInputError("escape refused: the bound AutoCAD window is not showing this test drawing")
    return name
