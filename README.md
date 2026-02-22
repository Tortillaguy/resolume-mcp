# resolume-mcp

> **Control Resolume Avenue/Arena from Claude.** Tell your AI agent to fire a clip,
> fade a layer, switch decks, or orchestrate a full set transition — and it happens
> live in Resolume via WebSocket.

```
"Fade out layer 1, connect clip 3 on layer 2, then bring layer 1 back up"
→ Claude executes three WebSocket commands in one shot, no round-trips
```

This is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server.
It gives Claude a direct line into Resolume's WebSocket API so you can use natural
language — or write multi-step automation scripts — to control your composition
in real time.

---

## What can you do with it?

Once connected, you can ask Claude things like:

- *"What clips are currently connected?"*
- *"Set the BPM to 128 and fire column 3"*
- *"Fade layer 1 to zero, switch to the fire deck, then fade back up"*
- *"Add a Glow effect to layer 2 and set its intensity to 0.7"*
- *"Loop through all layers and mute the ones with opacity below 0.3"*

Multi-step sequences — the kind that would normally require multiple tool calls —
run as a single `execute()` call. Claude writes the Python, you see the result.

---

## Prerequisites

- **Resolume Avenue or Arena ≥ 7** with the WebSocket server enabled
- **Python ≥ 3.11**
- **Claude Desktop** or **Claude Code**

### Enable Resolume's WebSocket server

1. Open Resolume → **Preferences** → **WebServer**
2. Enable the server (default port: `8080`)
3. Note the IP address if Claude is running on a different machine than Resolume

---

## Install

```bash
pip install git+https://github.com/cachorueda/resolume-mcp.git
```

Or clone and install in editable mode:

```bash
git clone https://github.com/cachorueda/resolume-mcp.git
pip install -e resolume-mcp/
```

---

## Claude Desktop config

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "resolume-mcp": {
      "command": "resolume-mcp-code",
      "env": { "RESOLUME_HOST": "192.168.0.77" }
    }
  }
}
```

Omit `"env"` entirely if Resolume is on the same machine as Claude (defaults to `localhost`).

Restart Claude Desktop after editing the config. You should see `resolume-mcp` listed
in the MCP servers panel.

---

## Claude Code config

Add to `.mcp.json` in your project root (or `~/.claude/.mcp.json` for global use):

```json
{
  "mcpServers": {
    "resolume-mcp": {
      "command": "resolume-mcp-code",
      "env": { "RESOLUME_HOST": "192.168.0.77" }
    }
  }
}
```

---

## How it works: code mode

Two tools, zero boilerplate.

**`search(query)`** — before writing code, ask what's available:

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
# Ask Claude: "fade layer 1 out, connect clip 3, fade back in"
await client.set_layer_opacity(1, 0.0)
await client.connect_clip(1, 3)
await client.set_layer_opacity(1, 1.0)
print("done")
```

That entire sequence is one tool call. Compare this to a named-tool server where
each line would be a separate round-trip — code mode is faster and lets Claude
write conditional logic, loops, and multi-layer operations naturally.

---

## Named tools (alternative mode)

Switch to `resolume-mcp-tools` in your MCP config for 14 discrete named tools.
Better for simple, one-shot operations when you don't need multi-step scripting.

| Tool | What it does |
|---|---|
| `get_composition` | Summary of decks, layers, and connected clips |
| `connect_clip` | Trigger a clip cell to play |
| `connect_column` | Fire all clips in a column simultaneously |
| `disconnect_all` | Stop all clips (blackout) |
| `set_layer_opacity` | Fade a layer in or out (0.0–1.0) |
| `set_layer_bypass` | Mute or unmute a layer |
| `get_bpm` | Read current tempo from live state |
| `set_bpm` | Change global composition tempo |
| `set_crossfader` | Move A/B crossfader (0.0–1.0) |
| `add_video_effect` | Add a video effect to a layer |
| `set_parameter` | Set any parameter by WebSocket path |
| `send_command` | Raw WebSocket command (low-level escape hatch) |
| `list_effects` | Browse available effects with their IDs |
| `list_sources` | Browse available sources grouped by type |

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
