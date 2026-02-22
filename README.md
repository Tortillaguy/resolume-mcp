# resolume-mcp

> **Control Resolume Avenue/Arena from any MCP-compatible AI agent.** Tell your
> agent to fire a clip, fade a layer, switch decks, or orchestrate a full set
> transition — and it happens live in Resolume via WebSocket.

```
"Fade out layer 1, connect clip 3 on layer 2, then bring layer 1 back up"
→ three WebSocket commands, one tool call, no round-trips
```

This is a [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server.
It gives your AI agent a direct line into Resolume's WebSocket API so you can use
natural language — or write multi-step automation scripts — to control your
composition in real time.

Works with Claude, Cursor, Windsurf, and any other MCP-compatible agent.

---

## What can you do with it?

Once connected, you can ask your agent things like:

- *"What clips are currently connected?"*
- *"Set the BPM to 128 and fire column 3"*
- *"Fade layer 1 to zero, switch to clip 3, then fade back up"*
- *"Add a Glow effect to layer 2 and set its intensity to 0.7"*
- *"Loop through all layers and mute the ones with opacity below 0.3"*

Multi-step sequences run as a single `execute()` call. The agent writes the
Python, you see the result in Resolume.

---

## Prerequisites

- **Resolume Avenue or Arena ≥ 7** with the WebSocket server enabled
- **Python ≥ 3.11**
- Any MCP-compatible AI agent (Claude Desktop, Claude Code, Cursor, etc.)

### Enable Resolume's WebSocket server

1. Open Resolume → **Preferences** → **WebServer**
2. Enable the server (default port: `8080`)
3. Note the IP address if your agent is running on a different machine than Resolume

---

## Install

```bash
pip install git+https://github.com/Tortillaguy/resolume-mcp.git
```

Or clone and install in editable mode:

```bash
git clone https://github.com/Tortillaguy/resolume-mcp.git
pip install -e resolume-mcp/
```

---

## Configure your agent

Add the server to your agent's MCP config. Set `RESOLUME_HOST` to the IP of the
machine running Resolume if it's not the same machine as your agent.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "resolume-mcp": {
      "command": "resolume-mcp-code",
      "env": { "RESOLUME_HOST": "192.168.1.10" }
    }
  }
}
```

**Claude Code** (`.mcp.json` in your project root):

```json
{
  "mcpServers": {
    "resolume-mcp": {
      "command": "resolume-mcp-code",
      "env": { "RESOLUME_HOST": "192.168.1.10" }
    }
  }
}
```

**Cursor / Windsurf / other MCP clients** — use the same JSON shape in their
respective MCP config files. The `command` is the entry point installed by pip;
`env` overrides are optional.

Omit `"env"` entirely if Resolume is on the same machine as your agent (defaults to `localhost`).

---

## How it works: two tools

**`search(query)`** — discover what the SDK can do and inspect live composition state:

```
search("opacity")

→ client.set_layer_opacity(self, layer_index: int, opacity: float)
    Set layer opacity (0.0 = invisible, 1.0 = full)

  state/layers/0/video/opacity  (value=1.0)
  state/layers/1/video/opacity  (value=0.8)
```

**`execute(code)`** — run Python against the live Resolume client.
`client` is pre-injected, `await` works, `print()` surfaces output:

```python
# "fade layer 1 out, connect clip 3, fade back in"
await client.set_layer_opacity(1, 0.0)
await client.connect_clip(1, 3)
await client.set_layer_opacity(1, 1.0)
print("done")
```

That entire sequence is one tool call. A traditional named-tool server would require
three separate round-trips; `execute()` does it in one, and the agent can add
conditional logic, loops, or timing between steps naturally.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `RESOLUME_HOST` | `localhost` | IP or hostname of the machine running Resolume |
| `RESOLUME_PORT` | `8080` | Resolume WebSocket server port |
| `RESOLUME_TIMEOUT` | `10.0` | Connection timeout in seconds |

Set these in the `"env"` block of your MCP config, or export them in your shell.

---

## Examples

See [`examples/`](examples/) for standalone scripts that use `ResolumeAgentClient`
directly without MCP — useful for testing or building your own automations:

- [`basic_connection.py`](examples/basic_connection.py) — connect, read BPM, disconnect
- [`fade_and_switch.py`](examples/fade_and_switch.py) — fade out, switch clip, fade in

---

## License

MIT
