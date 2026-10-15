import asyncio

from autocad_mcp.backends.base import CommandResult
from autocad_mcp.server import _entity_get_many


class _Backend:
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    async def entity_get_with_semantics(self, handle):
        self.calls.append(handle)
        if handle in self.failures:
            code = self.failures[handle]
            return CommandResult(ok=False, error="boom", error_code=code)
        return CommandResult(ok=True, payload={"handle": handle})


def test_get_many_reads_serially_and_keeps_entity_level_errors():
    backend = _Backend({"B": "E_ENTITY_NOT_FOUND"})
    result = asyncio.run(_entity_get_many(backend, ["A", "B", "A", "C"]))
    assert result.ok
    assert backend.calls == ["A", "B", "C"]
    assert set(result.payload["entities"]) == {"A", "C"}
    assert result.payload["errors"]["B"]["code"] == "E_ENTITY_NOT_FOUND"


def test_get_many_stops_on_session_failure():
    backend = _Backend({"B": "E_COM_STA_TIMEOUT"})
    result = asyncio.run(_entity_get_many(backend, ["A", "B", "C"]))
    assert not result.ok
    assert result.error_code == "E_COM_STA_TIMEOUT"
    assert backend.calls == ["A", "B"]
    assert result.payload["stopped_at"] == "B"


def test_get_many_rejects_bad_input():
    for bad in (None, [], ["A"] * 201, ["A", ""]):
        result = asyncio.run(_entity_get_many(_Backend({}), bad))
        assert result.error_code == "E_PARAMETER_REJECTED"
