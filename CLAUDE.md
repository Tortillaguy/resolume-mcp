# CLAUDE.md

Guidance for AI agents working on this codebase.

## Project Purpose

Python package that exposes [Resolume Avenue/Arena](https://resolume.com) WebSocket
control as MCP tools for any MCP-compatible AI agent. One server, two tools:

| Entry point | Tools |
|---|---|
| `resolume-mcp-code` | `search`, `execute` |

`search` introspects the live `ResolumeAgentClient` SDK and composition state.
`execute` runs arbitrary Python against a connected client instance.

## Package Layout

```
resolume_mcp/
  config.py       ← DEFAULT_HOST/PORT/TIMEOUT, read from env vars
  client.py       ← ResolumeAgentClient — all WebSocket/REST logic
  code_server.py  ← search + execute MCP server
examples/         ← standalone SDK usage (no MCP)
```

No database, no file I/O, no external state. All state lives in `client.state`
(an in-memory dict populated from Resolume's WebSocket stream).

## Setup

```bash
pip install -e .

# Confirm entry point installed
which resolume-mcp-code
```

## Key Implementation Details

### WebSocket payload shape — two distinct formats

Resolume's API uses different JSON shapes depending on the action:

```python
# post / remove — uses "path" + "body"
{"action": "post", "path": "/composition/decks/add", "body": None}

# get / set / subscribe / trigger — uses "parameter" + "value"
{"action": "set", "parameter": "/composition/layers/1/video/opacity", "value": 0.5}
```

This split is implemented in `client.send_command()` (`client.py:202`). Getting it
wrong produces a silent no-op in Resolume — the command is accepted but ignored.

### client.state has no "composition" wrapper

Resolume sends the composition as the bare root object:

```python
# WRONG — "composition" key does not exist
client.state["composition"]["layers"]

# CORRECT — state IS the composition
client.state["layers"]
```

The path-walking helpers (`_apply_incremental_update`, `_resolve_path_to_id`) strip
the leading `composition` segment when walking WebSocket paths against `client.state`.

### send_command vs send_and_wait

- `send_command()` — fire-and-forget. Use for playback triggers and opacity sets
  where you don't need confirmation before the next step.
- `send_and_wait()` — registers a `Future` keyed on the path, sends the command,
  then awaits Resolume's echo-back of the updated state. Use when subsequent
  operations depend on the result (e.g. creating a deck before renaming it).

### Async exec in code_server.py

The `execute` tool wraps submitted code as an `async def` body and awaits it within
the existing event loop (`code_server.py:189`). This is intentional:

```python
# asyncio.run() raises "cannot run nested event loop" inside an async MCP handler.
# Instead, wrap and await:
src = f"async def _fn(client):\n{indented}"
exec(compile(src, "<execute>", "exec"), globs)
return await globs["_fn"](client)
```

This is the same pattern Jupyter uses for top-level `await` in cells.

### Testing without a live Resolume instance

`ResolumeAgentClient` accepts `dry_run=True`, which short-circuits all WebSocket
calls and logs them instead:

```python
client = ResolumeAgentClient(dry_run=True)
await client.connect()   # logs "[dry-run] Would connect to ..."
await client.set_bpm(128)  # logs "[dry-run] SET /composition/tempocontroller/tempo"
```

Use `dry_run=True` for unit tests or when Resolume is not running.

### BPM and crossfader use parameter IDs, not paths

`set_bpm()` and `set_crossfader()` resolve to `/parameter/by-id/{id}` using the
numeric ID from `client.state`, not the human-readable path. Resolume's WebSocket
subscribe API requires by-id format; the path-based fallback is a best-effort
alternative for when state hasn't loaded yet.

## Adding a New SDK Method

1. Add the method to `client.py`
2. It will automatically surface in `search()` results via introspection
3. No changes to `code_server.py` needed — `execute()` runs arbitrary code against
   the live client, so new methods are immediately usable
