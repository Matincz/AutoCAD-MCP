from __future__ import annotations

from pathlib import Path

import pytest

from autocad_mcp import server


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch):
    for name in (
        "AUTOCAD_MCP_SERVER_TRANSPORT",
        "AUTOCAD_MCP_HTTP_HOST",
        "AUTOCAD_MCP_HTTP_PORT",
        "AUTOCAD_MCP_HTTP_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = server.mcp.settings
    saved = (settings.host, settings.port, settings.transport_security)
    yield
    settings.host, settings.port, settings.transport_security = saved


def test_stdio_is_default_and_leaves_settings_untouched():
    before = server.mcp.settings.transport_security

    assert server.configure_server_transport([]) == "stdio"
    assert server.mcp.settings.transport_security is before


def test_http_binds_requested_host_and_allows_it():
    transport = server.configure_server_transport(
        ["--transport", "streamable-http", "--host", "10.211.55.4", "--port", "9001"]
    )

    settings = server.mcp.settings
    assert transport == "streamable-http"
    assert (settings.host, settings.port) == ("10.211.55.4", 9001)
    assert settings.transport_security.enable_dns_rebinding_protection is True
    assert "10.211.55.4:*" in settings.transport_security.allowed_hosts
    assert "localhost:*" in settings.transport_security.allowed_hosts


def test_wildcard_bind_requires_explicit_allowed_hosts(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_SERVER_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AUTOCAD_MCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTOCAD_MCP_HTTP_ALLOWED_HOSTS", "10.211.55.4:8765, win-vm.local:8765")

    assert server.configure_server_transport([]) == "streamable-http"
    allowed = server.mcp.settings.transport_security.allowed_hosts
    assert "0.0.0.0:*" not in allowed
    assert "10.211.55.4:8765" in allowed
    assert "win-vm.local:8765" in allowed


async def test_transaction_create_resolves_name_into_output_workspace(monkeypatch, tmp_path):
    from autocad_mcp.backends.base import CommandResult

    monkeypatch.setenv("AUTOCAD_MCP_OUTPUT_ROOT", str(tmp_path))
    requested = []

    class FakeBackend:
        name = "file_ipc"

        async def drawing_create(self, name, idempotency_key=None):
            requested.append(name)
            return CommandResult(ok=True, payload={})

    async def fake_backend():
        return FakeBackend()

    monkeypatch.setattr(server, "get_backend", fake_backend)
    monkeypatch.setattr(server, "_begin_journaled_mutation", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_finish_journaled_mutation", lambda key, result: result)

    await server.transaction(operation="create", idempotency_key="k1", data={"name": "native-smoke"})

    created = Path(requested[0])
    assert created.parent == (tmp_path / "drawings").resolve()
    assert created.name.startswith("native-smoke-") and created.suffix == ".dwg"
