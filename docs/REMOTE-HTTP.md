# Remote access over Streamable HTTP

stdio remains the default transport. Use Streamable HTTP when the MCP client
runs on another machine, for example Claude Code on macOS controlling AutoCAD
inside a Parallels Windows VM.

## Server (Windows, inside the interactive desktop session)

The server must run in the same logged-in desktop session as AutoCAD: the
File IPC backend posts window messages and the native worker pipe is
current-user only. Do not start it from SSH, a service, or `prlctl exec`.

```powershell
uv run autocad-mcp --transport streamable-http --host 10.211.55.4 --port 8765
```

| Flag | Environment variable | Default |
|------|----------------------|---------|
| `--transport` | `AUTOCAD_MCP_SERVER_TRANSPORT` | `stdio` |
| `--host` | `AUTOCAD_MCP_HTTP_HOST` | `127.0.0.1` |
| `--port` | `AUTOCAD_MCP_HTTP_PORT` | `8765` |
| `--allowed-hosts` | `AUTOCAD_MCP_HTTP_ALLOWED_HOSTS` | (none) |

DNS-rebinding protection stays enabled. Requests are accepted only when the
`Host` header matches localhost, the bind host, or an `--allowed-hosts` entry
(comma-separated, e.g. `10.211.55.4:8765`). When binding `0.0.0.0`, list the
reachable addresses in `--allowed-hosts`.

There is no authentication. Bind to a host-only or VM-private address and
restrict the Windows firewall rule to the client's subnet.

## Client (macOS)

```bash
claude mcp add --transport http autocad http://10.211.55.4:8765/mcp
```
