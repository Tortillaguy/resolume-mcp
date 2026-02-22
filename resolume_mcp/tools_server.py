"""
Resolume MCP Tools Server
--------------------------
Exposes Resolume Avenue/Arena control as 14 named MCP tools plus a live-state
resource so Claude can query and manipulate compositions through natural language.

Run via Claude Desktop or Claude Code .mcp.json config.
For testing startup: resolume-mcp-tools
"""

import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool

from resolume_mcp.client import ResolumeAgentClient
from resolume_mcp.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT

# ---------------------------------------------------------------------------
# Shared client singleton
# ---------------------------------------------------------------------------
_client: ResolumeAgentClient | None = None


async def get_client() -> ResolumeAgentClient:
    """Return the shared client, connecting lazily if needed."""
    global _client
    if _client is None or not _client._connected:
        _client = ResolumeAgentClient(host=DEFAULT_HOST, port=DEFAULT_PORT)
        connected = await _client.connect(timeout=DEFAULT_TIMEOUT)
        if not connected:
            raise RuntimeError(
                f"Could not connect to Resolume at ws://{DEFAULT_HOST}:{DEFAULT_PORT}/api/v1. "
                "Is Resolume running with WebSocket server enabled?"
            )
    return _client


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
app = Server("resolume-controller")


# --- Tools ------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_composition",
            description=(
                "Returns a summary of the current Resolume composition state: "
                "deck names, layer count, and clip count per deck."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="send_command",
            description=(
                "Low-level raw WebSocket command to Resolume. "
                "Use get_composition or the resource first to find the right path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "WebSocket action: 'get', 'set', 'post', 'subscribe', 'unsubscribe'",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path, e.g. /composition/layers/1/clips/1/connect",
                    },
                    "value": {
                        "type": ["string", "number", "boolean", "null"],
                        "description": "Optional value to send with the command",
                    },
                },
                "required": ["action", "path"],
            },
        ),
        Tool(
            name="add_video_effect",
            description="Adds a video effect to a specific layer by index.",
            inputSchema={
                "type": "object",
                "properties": {
                    "layer_index": {
                        "type": "integer",
                        "description": "1-based layer index in the composition",
                    },
                    "effect_id": {
                        "type": "string",
                        "description": "Resolume effect identifier, e.g. 'Glow'",
                    },
                },
                "required": ["layer_index", "effect_id"],
            },
        ),
        Tool(
            name="set_parameter",
            description=(
                "Sets any Resolume parameter by its WebSocket API path. "
                "Useful for opacity, speed, BPM sync, and other per-clip/layer values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Full WebSocket path, e.g. /composition/layers/1/master",
                    },
                    "value": {
                        "description": "Value to set (string, number, or boolean)",
                    },
                },
                "required": ["path", "value"],
            },
        ),
        Tool(
            name="connect_clip",
            description="Trigger a clip to play (equivalent to pressing a clip cell in Resolume).",
            inputSchema={
                "type": "object",
                "properties": {
                    "layer_index": {"type": "integer", "description": "1-based layer index"},
                    "clip_index": {"type": "integer", "description": "1-based clip index"},
                },
                "required": ["layer_index", "clip_index"],
            },
        ),
        Tool(
            name="connect_column",
            description="Fire an entire column (all layers simultaneously, deck-synchronized).",
            inputSchema={
                "type": "object",
                "properties": {
                    "column_index": {"type": "integer", "description": "1-based column index"},
                },
                "required": ["column_index"],
            },
        ),
        Tool(
            name="disconnect_all",
            description="Stop all playing clips in the composition (blackout).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="set_layer_opacity",
            description="Fade a layer in or out (0.0 = invisible, 1.0 = full opacity).",
            inputSchema={
                "type": "object",
                "properties": {
                    "layer_index": {"type": "integer", "description": "1-based layer index"},
                    "opacity": {
                        "type": "number",
                        "description": "Opacity value between 0.0 and 1.0",
                    },
                },
                "required": ["layer_index", "opacity"],
            },
        ),
        Tool(
            name="set_layer_bypass",
            description=(
                "Mute or unmute a layer. Bypassed layers produce no output "
                "but continue playing internally."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "layer_index": {"type": "integer", "description": "1-based layer index"},
                    "bypassed": {"type": "boolean", "description": "True to mute, False to unmute"},
                },
                "required": ["layer_index", "bypassed"],
            },
        ),
        Tool(
            name="get_bpm",
            description=(
                "Read the current tempo from Resolume's live state. "
                "Returns the full tempocontroller dict including tempo, beattime, and phase."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="set_bpm",
            description="Change the global composition tempo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bpm": {"type": "number", "description": "Tempo in BPM, e.g. 128.0"},
                },
                "required": ["bpm"],
            },
        ),
        Tool(
            name="set_crossfader",
            description="Move the A/B crossfader (0.0 = full A, 1.0 = full B).",
            inputSchema={
                "type": "object",
                "properties": {
                    "position": {
                        "type": "number",
                        "description": "Crossfader position between 0.0 and 1.0",
                    },
                },
                "required": ["position"],
            },
        ),
        Tool(
            name="list_effects",
            description=(
                "Show all available video and audio effects grouped by category. "
                "Use this to discover effect IDs for add_video_effect."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_sources",
            description=(
                "Show all available video/audio sources (clips, generators, live inputs) "
                "grouped by type."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "get_composition":
            return await _tool_get_composition()
        elif name == "send_command":
            return await _tool_send_command(
                arguments["action"],
                arguments["path"],
                arguments.get("value"),
            )
        elif name == "add_video_effect":
            return await _tool_add_video_effect(
                arguments["layer_index"],
                arguments["effect_id"],
            )
        elif name == "set_parameter":
            return await _tool_set_parameter(arguments["path"], arguments["value"])
        elif name == "connect_clip":
            return await _tool_connect_clip(arguments["layer_index"], arguments["clip_index"])
        elif name == "connect_column":
            return await _tool_connect_column(arguments["column_index"])
        elif name == "disconnect_all":
            return await _tool_disconnect_all()
        elif name == "set_layer_opacity":
            return await _tool_set_layer_opacity(arguments["layer_index"], arguments["opacity"])
        elif name == "set_layer_bypass":
            return await _tool_set_layer_bypass(arguments["layer_index"], arguments["bypassed"])
        elif name == "get_bpm":
            return await _tool_get_bpm()
        elif name == "set_bpm":
            return await _tool_set_bpm(arguments["bpm"])
        elif name == "set_crossfader":
            return await _tool_set_crossfader(arguments["position"])
        elif name == "list_effects":
            return await _tool_list_effects()
        elif name == "list_sources":
            return await _tool_list_sources()
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# --- Resources --------------------------------------------------------------

@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri="resolume://composition",
            name="Live Resolume Composition State",
            description=(
                "Full JSON state of the current Resolume composition. "
                "Read this before making decisions about what decks/layers exist."
            ),
            mimeType="application/json",
        )
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    if uri == "resolume://composition":
        client = await get_client()
        data = await client.rest_get("/composition")
        return json.dumps(data, indent=2)
    raise ValueError(f"Unknown resource: {uri}")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

_CONNECTED_STATES = {"Connected", "Connected & previewing"}


async def _tool_get_composition() -> list[TextContent]:
    client = await get_client()
    state = await client.rest_get("/composition")
    decks = state.get("decks", [])
    layers = state.get("layers", [])

    summary_lines = [
        f"Resolume composition — {len(decks)} deck(s), {len(layers)} layer(s)",
        "",
    ]
    for deck in decks:
        deck_name = deck.get("name", {}).get("value", "<unnamed>")
        # Count only clips that belong to this deck and are truly connected.
        # Resolume's "connected" field is a ParamState string: "Empty",
        # "Disconnected", "Previewing", "Connected", "Connected & previewing".
        clip_count = sum(
            1
            for clip in deck.get("clips", [])
            if clip.get("connected", {}).get("value") in _CONNECTED_STATES
        )
        summary_lines.append(f"  Deck: {deck_name}  (clips connected: {clip_count})")

    if not decks:
        summary_lines.append("  (no decks loaded)")

    return [TextContent(type="text", text="\n".join(summary_lines))]


async def _tool_send_command(action: str, path: str, value=None) -> list[TextContent]:
    client = await get_client()
    await client.send_command(action, path, value)
    msg = f"Sent: {action.upper()} {path}"
    if value is not None:
        msg += f" = {value!r}"
    return [TextContent(type="text", text=msg)]


async def _tool_add_video_effect(layer_index: int, effect_id: str) -> list[TextContent]:
    client = await get_client()
    await client.add_video_effect(layer_index, effect_id)
    return [TextContent(
        type="text",
        text=f"Added effect '{effect_id}' to layer {layer_index}",
    )]


async def _tool_set_parameter(path: str, value) -> list[TextContent]:
    client = await get_client()
    await client.set_parameter(path, value)
    return [TextContent(type="text", text=f"Set {path} = {value!r}")]


async def _tool_connect_clip(layer_index: int, clip_index: int) -> list[TextContent]:
    client = await get_client()
    await client.connect_clip(layer_index, clip_index)
    return [TextContent(
        type="text",
        text=f"Triggered clip at layer {layer_index}, clip {clip_index}",
    )]


async def _tool_connect_column(column_index: int) -> list[TextContent]:
    client = await get_client()
    await client.connect_column(column_index)
    return [TextContent(type="text", text=f"Fired column {column_index}")]


async def _tool_disconnect_all() -> list[TextContent]:
    client = await get_client()
    await client.disconnect_all()
    return [TextContent(type="text", text="Disconnected all clips (blackout)")]


async def _tool_set_layer_opacity(layer_index: int, opacity: float) -> list[TextContent]:
    client = await get_client()
    await client.set_layer_opacity(layer_index, opacity)
    return [TextContent(type="text", text=f"Set layer {layer_index} opacity to {opacity}")]


async def _tool_set_layer_bypass(layer_index: int, bypassed: bool) -> list[TextContent]:
    client = await get_client()
    await client.set_layer_bypass(layer_index, bypassed)
    state_str = "bypassed (muted)" if bypassed else "active"
    return [TextContent(type="text", text=f"Layer {layer_index} is now {state_str}")]


async def _tool_get_bpm() -> list[TextContent]:
    client = await get_client()
    tc = client.get_bpm()
    if not tc:
        return [TextContent(type="text", text="tempocontroller not yet in state")]
    return [TextContent(type="text", text=json.dumps(tc, indent=2))]


async def _tool_set_bpm(bpm: float) -> list[TextContent]:
    client = await get_client()
    await client.set_bpm(bpm)
    return [TextContent(type="text", text=f"Set BPM to {bpm}")]


async def _tool_set_crossfader(position: float) -> list[TextContent]:
    client = await get_client()
    await client.set_crossfader(position)
    return [TextContent(type="text", text=f"Set crossfader to {position}")]


def _format_effects_table(data: dict) -> str:
    """Format the effects REST response into a readable table.

    Resolume effects use 'idstring' as the identifier (e.g. 'Resolume Bitcrusher',
    'c_35de2952-...'). Pass idstring to add_video_effect as the effect_id.
    """
    lines: list[str] = []
    for category, effects in data.items():
        if not isinstance(effects, list):
            continue
        lines.append(f"\n[{category}]")
        for e in effects:
            name = e.get("name", "?").strip()
            idstring = e.get("idstring", "")
            lines.append(f"  {name:<40} {idstring}")
    return "\n".join(lines) if lines else json.dumps(data, indent=2)


def _format_sources_table(data: dict) -> str:
    """Format the sources REST response into a readable table."""
    lines: list[str] = []
    for category, sources in data.items():
        if not isinstance(sources, list):
            continue
        lines.append(f"\n[{category}]")
        for s in sources:
            name = s.get("name", "?").strip()
            idstring = s.get("idstring", "")
            lines.append(f"  {name:<40} {idstring}")
    return "\n".join(lines) if lines else json.dumps(data, indent=2)


async def _tool_list_effects() -> list[TextContent]:
    client = await get_client()
    data = await client.list_effects()
    return [TextContent(type="text", text=_format_effects_table(data))]


async def _tool_list_sources() -> list[TextContent]:
    client = await get_client()
    data = await client.list_sources()
    return [TextContent(type="text", text=_format_sources_table(data))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main_cli():
    asyncio.run(main())


if __name__ == "__main__":
    main_cli()
