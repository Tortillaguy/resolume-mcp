"""
Resolume MCP Code Server
------------------------
Exposes exactly two MCP tools — `search` and `execute` — so Claude can write
Python directly against a live ResolumeAgentClient instance.

Advantages over tools_server.py:
- Multi-step VJ operations (fade out → switch clip → fade in) in ONE tool call
- Token cost: ~2×schema regardless of API surface size
- No round-trip per operation; single execute() call dispatches everything

Run via Claude Desktop or Claude Code .mcp.json config.
For testing startup: resolume-mcp-code
"""

import asyncio
import contextlib
import inspect
import io

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import GetPromptResult, Prompt, PromptMessage, TextContent, Tool

from resolume_mcp.behaviors import Action, Behavior, BehaviorManager, Condition
from resolume_mcp.client import ResolumeAgentClient
from resolume_mcp.config import BEHAVIORS_PATH, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT, SNAPSHOTS_DIR
from resolume_mcp.snapshots import (
    SnapshotStore,
    execute_deck_merge,
    extract_clip_effects,
    extract_crossfader,
    extract_deck,
    extract_layer_effects,
    extract_layer_group,
    extract_layer_settings,
    plan_deck_merge,
    restore_clip_effects,
    restore_crossfader,
    restore_layer_effects,
    restore_layer_group,
    restore_layer_settings,
)

# ---------------------------------------------------------------------------
# Prompt: portable quickstart documentation for AI agents
# ---------------------------------------------------------------------------
_QUICKSTART = """\
## Resolume MCP: VJ Workflow Quickstart

### What is this?

[Resolume Avenue/Arena](https://resolume.com) is professional VJ software for live
visual performance. This MCP server exposes its WebSocket/REST API to you via two
tools: `search` and `execute`. You can fire clips, adjust layer opacity, set BPM,
fade layers, add effects, and orchestrate multi-step VJ routines — all from a single
`execute()` call.

### Two-tool pattern

1. **`search(query)`** — Discover what the SDK can do before writing code.
   Returns matching `ResolumeAgentClient` method signatures + docstrings, and live
   composition state paths (e.g. searching "bpm" shows the BPM state path and the
   `set_bpm()` method signature).

2. **`execute(code)`** — Run Python against the live client. `client` is pre-injected.
   Use `await` for all async methods. Use `print()` to surface values.

Always `search` first when you don't know the exact method name or state path.

### client.state structure — two critical quirks

**1. No `"composition"` wrapper key.** Resolume sends the composition as the bare
root object:

```python
# WRONG — "composition" key does not exist
client.state["composition"]["layers"]  # KeyError

# CORRECT — state IS the composition
client.state["layers"]
```

**2. All scalar values are dicts, not primitives.** Every parameter is wrapped:

```python
# WRONG
client.state["layers"][0]["name"]           # returns {"value": "Layer #", ...}
client.state["layers"][0]["bypassed"]       # returns {"value": False, ...}

# CORRECT — always extract ["value"]
client.state["layers"][0]["name"]["value"]       # "Layer #"
client.state["layers"][0]["bypassed"]["value"]   # False
```

### Layer and clip indexing

SDK methods use **1-based indexing** matching Resolume's UI (Layer 1 = first layer,
Clip 1 = first slot). The `client.state["layers"]` list is 0-based in Python, so
`client.state["layers"][0]` is "Layer 1" in the UI.

### Async execution model

All `execute()` code runs inside an existing asyncio event loop. Use `await` freely.
Do NOT call `asyncio.run()` — it raises "cannot run nested event loop".

---

### Example: Read current state (BPM, layers, decks)

```python
layers = client.state.get("layers", [])
decks = client.state.get("decks", [])
bpm = client.state["tempocontroller"]["tempo"]["value"]
print(f"BPM: {bpm}")
print(f"Layers: {len(layers)}")
print(f"Decks: {len(decks)}")
for i, layer in enumerate(layers):
    name = layer["name"]["value"]
    bypassed = layer["bypassed"]["value"]
    print(f"  L{i+1}: {name!r} bypassed={bypassed}")
```

### Example: Fire a clip (layer 1, clip slot 3)

```python
await client.connect_clip(layer_index=1, clip_index=3)
print("Clip fired")
```

### Example: Fade out → switch clip → fade in

```python
import asyncio

# Fade layer 1 to 0 over ~500 ms
for v in range(10, -1, -1):
    await client.set_layer_opacity(layer_index=1, opacity=v / 10)
    await asyncio.sleep(0.05)

# Switch to clip slot 2
await client.connect_clip(layer_index=1, clip_index=2)

# Fade back in
for v in range(0, 11):
    await client.set_layer_opacity(layer_index=1, opacity=v / 10)
    await asyncio.sleep(0.05)

print("Crossfade complete")
```

### Example: Set BPM to 128

```python
await client.set_bpm(128)
print("BPM set to 128")
```

### Example: Mute a layer and add a video effect

```python
await client.set_layer_bypass(layer_index=2, bypassed=True)
print("Layer 2 bypassed")

# effect_id is an effect identifier string — use search("effect") to find valid IDs
await client.add_video_effect(layer_index=1, effect_id="Blur")
print("Effect added to layer 1")
```

---

### Useful search queries to start

- `search("layer")` — layer control methods + state paths
- `search("clip")` — clip connect/disconnect
- `search("bpm")` — tempo control
- `search("opacity")` — opacity setters
- `search("effect")` — video effect methods
"""

# ---------------------------------------------------------------------------
# Shared client singleton (identical lifecycle to tools_server.py)
# ---------------------------------------------------------------------------
_client: ResolumeAgentClient | None = None


async def get_client() -> ResolumeAgentClient:
    """Return the shared client, connecting lazily on first call."""
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


_behavior_manager: BehaviorManager | None = None


async def get_behavior_manager() -> BehaviorManager:
    """Return the shared behavior manager, creating and starting on first call."""
    global _behavior_manager
    if _behavior_manager is None:
        client = await get_client()
        _behavior_manager = BehaviorManager(client, BEHAVIORS_PATH, snapshot_store=_snapshot_store)
        await _behavior_manager.start()
    return _behavior_manager


_snapshot_store = SnapshotStore(SNAPSHOTS_DIR)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
app = Server("resolume-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description=(
                "Search available ResolumeAgentClient methods and live composition state paths. "
                "Use this to discover what the SDK can do before writing execute() code. "
                "Returns matching method signatures, docstrings, and matching state keys."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term, e.g. 'bpm', 'layer', 'clip', 'opacity'",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="execute",
            description=(
                "Run Python against the live Resolume client. "
                "`client` is a connected ResolumeAgentClient. "
                "Use `await` for async methods. Use `print()` to surface values. "
                "Multi-step operations can be written as a single block. "
                "Example: await client.connect_clip(1, 1)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code block. `client` is pre-injected. "
                            "Use await for async methods, print() for output."
                        ),
                    }
                },
                "required": ["code"],
            },
        ),
        Tool(
            name="behaviors",
            description=(
                "Manage persistent reactive behaviors. A behavior monitors a Resolume "
                "parameter and performs an action when a condition is met. "
                "Behaviors survive server restarts. "
                "Subcommands: list, add, remove, enable, disable."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "enum": ["list", "add", "remove", "enable", "disable"],
                        "description": "Operation to perform",
                    },
                    "id": {
                        "type": "string",
                        "description": "Behavior ID (for remove/enable/disable)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable name (for add)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description (for add)",
                    },
                    "trigger_param_id": {
                        "type": "integer",
                        "description": "Numeric parameter ID to monitor (for add). Use search to find IDs.",
                    },
                    "condition": {
                        "type": "object",
                        "description": (
                            "When to fire. {op, value?}. "
                            "op: any|eq|neq|gt|lt|gte|lte|truthy|falsy"
                        ),
                        "properties": {
                            "op": {"type": "string"},
                            "value": {},
                        },
                        "required": ["op"],
                    },
                    "action": {
                        "type": "object",
                        "description": (
                            "What to do. {type, params}. "
                            "type: set_parameter|toggle_parameter|toggle_parameters|set_parameters|restore_snapshot"
                        ),
                        "properties": {
                            "type": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["type", "params"],
                    },
                },
                "required": ["subcommand"],
            },
        ),
        Tool(
            name="snapshots",
            description=(
                "Save and restore Resolume composition state slices. "
                "Capture effects, settings, crossfader, decks, layer groups, or clip presets, "
                "then restore them to the same or different target. "
                "Snapshots match by name (not ID), so they work across compositions. "
                "Use 'merge' to safely consolidate clips from two deck snapshots — "
                "colliding clips are relocated to the next empty slot per layer. "
                "Subcommands: save, load, merge, list, delete, show."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subcommand": {
                        "type": "string",
                        "enum": ["save", "load", "merge", "list", "delete", "show"],
                        "description": "Operation to perform",
                    },
                    "name": {
                        "type": "string",
                        "description": "Snapshot name (for save/load/delete/show)",
                    },
                    "source_name": {
                        "type": "string",
                        "description": "Source deck snapshot name (for merge)",
                    },
                    "target_name": {
                        "type": "string",
                        "description": "Target deck snapshot name (for merge)",
                    },
                    "snapshot_type": {
                        "type": "string",
                        "enum": [
                            "layer_effects", "layer_settings", "clip_effects",
                            "crossfader", "deck", "layer_group",
                        ],
                        "description": "What to capture (for save). Default: layer_effects",
                    },
                    "layer_index": {
                        "type": "integer",
                        "description": "1-based layer index (for layer_effects, layer_settings, clip_effects)",
                    },
                    "clip_index": {
                        "type": "integer",
                        "description": "1-based clip index (for clip_effects)",
                    },
                    "deck_index": {
                        "type": "integer",
                        "description": "1-based deck index (for deck)",
                    },
                    "group_index": {
                        "type": "integer",
                        "description": "1-based layer group index (for layer_group)",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, show the merge plan without executing (for merge)",
                    },
                },
                "required": ["subcommand"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search":
            return await _tool_search(arguments["query"])
        elif name == "execute":
            return await _tool_execute(arguments["code"])
        elif name == "behaviors":
            return await _tool_behaviors(arguments)
        elif name == "snapshots":
            return await _tool_snapshots(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="quickstart",
            description="VJ workflow guide — how to control Resolume live with this server",
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
    if name != "quickstart":
        raise ValueError(f"Unknown prompt: {name}")
    return GetPromptResult(
        description="Resolume MCP quickstart for VJ workflows",
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=_QUICKSTART),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _search_client_methods(query: str) -> list[str]:
    """Introspect ResolumeAgentClient and return methods matching the query."""
    q = query.lower()
    results = []
    for name, obj in inspect.getmembers(ResolumeAgentClient):
        if name.startswith("_"):
            continue
        if q not in name.lower() and not (obj.__doc__ and q in obj.__doc__.lower()):
            continue
        try:
            sig = inspect.signature(obj)
        except (ValueError, TypeError):
            sig = ""
        first_doc_line = (obj.__doc__ or "").strip().splitlines()[0] if obj.__doc__ else ""
        results.append(f"client.{name}{sig}\n  → {first_doc_line}")
    return results


def _search_state_paths(state: dict, query: str, prefix: str = "", max_depth: int = 4) -> list[str]:
    """Walk composition state and collect paths whose key contains the query term."""
    q = query.lower()
    found: list[str] = []

    def _walk(node, path: str, depth: int):
        if depth > max_depth:
            return
        if isinstance(node, dict):
            for key, val in node.items():
                child_path = f"{path}/{key}"
                if q in str(key).lower():
                    # Show value hint for leaf-like nodes
                    if isinstance(val, dict) and "value" in val:
                        found.append(f"{child_path}  (value={val['value']!r})")
                    else:
                        found.append(child_path)
                _walk(val, child_path, depth + 1)
        elif isinstance(node, list):
            for i, item in enumerate(node[:5]):  # limit list expansion to first 5
                _walk(item, f"{path}/{i}", depth + 1)

    _walk(state, prefix or "state", 0)
    return found


async def _tool_search(query: str) -> list[TextContent]:
    client = await get_client()

    methods = _search_client_methods(query)
    state_paths = _search_state_paths(client.state, query)

    sections: list[str] = []

    if methods:
        sections.append("## Client methods\n" + "\n\n".join(methods))
    else:
        sections.append(f"## Client methods\n(no methods match '{query}')")

    if state_paths:
        sections.append("## Composition state paths\n" + "\n".join(state_paths))
    else:
        sections.append(f"## Composition state paths\n(no state keys match '{query}')")

    return [TextContent(type="text", text="\n\n".join(sections))]


async def _run_user_code(client: ResolumeAgentClient, code: str):
    """Wrap submitted code as async def body, compile, exec, and await it.

    This avoids the nested-event-loop problem: asyncio.run() raises
    'cannot run nested event loop' when called from inside an async handler.
    Wrapping as async def and awaiting works within the existing loop.
    """
    indented = "\n".join(f"    {line}" for line in code.splitlines())
    src = f"async def _fn(client):\n{indented}"
    globs: dict = {}
    exec(compile(src, "<execute>", "exec"), globs)
    return await globs["_fn"](client)


async def _tool_execute(code: str) -> list[TextContent]:
    client = await get_client()
    buf = io.StringIO()
    result = None
    try:
        with contextlib.redirect_stdout(buf):
            result = await _run_user_code(client, code)
    except Exception as e:
        captured = buf.getvalue().rstrip()
        error_text = f"Error: {type(e).__name__}: {e}"
        if captured:
            error_text = f"{captured}\n{error_text}"
        return [TextContent(type="text", text=error_text)]

    parts: list[str] = []
    if buf.getvalue():
        parts.append(buf.getvalue().rstrip())
    if result is not None:
        parts.append(f"→ {result!r}")

    return [TextContent(type="text", text="\n".join(parts) or "(no output)")]


async def _tool_behaviors(arguments: dict) -> list[TextContent]:
    mgr = await get_behavior_manager()
    sub = arguments["subcommand"]

    if sub == "list":
        items = mgr.list()
        # Always show the built-in dashboard naming watcher first
        dn = mgr.dashboard_naming
        status = "ON" if dn.enabled else "OFF"
        lines = [f"[{status}] dashboard_opacity_rename (id=dashboard_naming) [built-in]"]
        if not items:
            if not lines:
                return [TextContent(type="text", text="No behaviors registered.")]
        for b in items:
            status = "ON" if b["enabled"] else "OFF"
            fires = b.get("fire_count", 0)
            err = b.get("last_error")
            line = f"[{status}] {b['name']} (id={b['id']}, trigger={b['trigger_param_id']}, fires={fires})"
            if err:
                line += f"\n  last_error: {err}"
            lines.append(line)
        return [TextContent(type="text", text="\n".join(lines))]

    elif sub == "add":
        b = Behavior(
            name=arguments.get("name", "Untitled"),
            description=arguments.get("description", ""),
            trigger_param_id=arguments["trigger_param_id"],
            condition=Condition(**arguments.get("condition", {"op": "any"})),
            action=Action(**arguments["action"]),
        )
        result = await mgr.add(b)
        return [TextContent(type="text", text=f"Added behavior {result.name!r} (id={result.id})")]

    elif sub == "remove":
        ok = await mgr.remove(arguments["id"])
        if ok:
            return [TextContent(type="text", text=f"Removed behavior {arguments['id']}")]
        return [TextContent(type="text", text=f"Behavior {arguments['id']} not found")]

    elif sub == "enable":
        if arguments.get("id") == "dashboard_naming":
            mgr.dashboard_naming.enable()
            return [TextContent(type="text", text="Enabled dashboard_opacity_rename")]
        ok = await mgr.enable(arguments["id"])
        if ok:
            return [TextContent(type="text", text=f"Enabled behavior {arguments['id']}")]
        return [TextContent(type="text", text=f"Behavior {arguments['id']} not found")]

    elif sub == "disable":
        if arguments.get("id") == "dashboard_naming":
            mgr.dashboard_naming.disable()
            return [TextContent(type="text", text="Disabled dashboard_opacity_rename")]
        ok = await mgr.disable(arguments["id"])
        if ok:
            return [TextContent(type="text", text=f"Disabled behavior {arguments['id']}")]
        return [TextContent(type="text", text=f"Behavior {arguments['id']} not found")]

    else:
        return [TextContent(type="text", text=f"Unknown subcommand: {sub}")]


async def _tool_snapshots(arguments: dict) -> list[TextContent]:
    sub = arguments["subcommand"]

    if sub == "list":
        items = _snapshot_store.list()
        if not items:
            return [TextContent(type="text", text="No snapshots saved.")]
        lines = []
        for s in items:
            if s["type"] == "deck":
                detail = f"deck {s.get('deck_name', '?')!r} ({s.get('num_clips', '?')} clips)"
            else:
                detail = f"{s.get('layer_name', '')} L{s.get('layer_index', '?')}"
            lines.append(f"{s['name']} ({s['type']}) — {detail} [{s['created']}]")
        return [TextContent(type="text", text="\n".join(lines))]

    elif sub == "save":
        client = await get_client()
        name = arguments.get("name")
        if not name:
            return [TextContent(type="text", text="Error: 'name' is required for save")]
        snap_type = arguments.get("snapshot_type", "layer_effects")
        layer_index = arguments.get("layer_index")
        clip_index = arguments.get("clip_index")
        deck_index = arguments.get("deck_index")
        group_index = arguments.get("group_index")

        if snap_type in ("layer_effects", "layer_settings", "clip_effects") and not layer_index:
            return [TextContent(type="text", text="Error: 'layer_index' is required for this snapshot type")]

        if snap_type == "layer_effects":
            data = extract_layer_effects(client.state, layer_index)
        elif snap_type == "layer_settings":
            data = extract_layer_settings(client.state, layer_index)
        elif snap_type == "clip_effects":
            if not clip_index:
                return [TextContent(type="text", text="Error: 'clip_index' is required for clip_effects")]
            data = extract_clip_effects(client.state, layer_index, clip_index)
        elif snap_type == "crossfader":
            data = extract_crossfader(client.state)
        elif snap_type == "deck":
            if not deck_index:
                return [TextContent(type="text", text="Error: 'deck_index' is required for deck")]
            data = extract_deck(client.state, deck_index)
        elif snap_type == "layer_group":
            if not group_index:
                return [TextContent(type="text", text="Error: 'group_index' is required for layer_group")]
            data = extract_layer_group(client.state, group_index)
        else:
            return [TextContent(type="text", text=f"Unknown snapshot_type: {snap_type}")]

        _snapshot_store.save(name, snap_type, data)
        summary = f"Saved snapshot {name!r} ({snap_type})"
        if snap_type == "deck":
            n_connected = sum(
                len(l.get("connected_clips", [])) for l in data.get("layers", [])
            )
            summary += f" — {data.get('deck_name', '')!r}, {n_connected} connected clips"
        else:
            if "effects" in data:
                summary += f" — {len(data['effects'])} effects"
            if data.get("layer_name"):
                summary += f" from {data['layer_name']!r}"
        return [TextContent(type="text", text=summary)]

    elif sub == "load":
        client = await get_client()
        name = arguments.get("name")
        if not name:
            return [TextContent(type="text", text="Error: 'name' is required for load")]

        snap = _snapshot_store.load(name)
        if snap is None:
            return [TextContent(type="text", text=f"Snapshot {name!r} not found")]

        snap_type = snap.get("type", "")
        data = snap.get("data", {})
        layer_index = arguments.get("layer_index")
        clip_index = arguments.get("clip_index")
        group_index = arguments.get("group_index")

        if snap_type == "layer_effects":
            if not layer_index:
                return [TextContent(type="text", text="Error: 'layer_index' is required for loading layer_effects")]
            result = await restore_layer_effects(client, data, layer_index)
            applied = result["applied"]
            skipped = result["skipped"]
            lines = [f"Restored {name!r} to L{layer_index}:"]
            for a in applied:
                lines.append(f"  {a['effect']}: {a['params_set']} params set")
            if skipped:
                lines.append(f"  Skipped (not in target): {', '.join(skipped)}")
            return [TextContent(type="text", text="\n".join(lines))]

        elif snap_type == "layer_settings":
            if not layer_index:
                return [TextContent(type="text", text="Error: 'layer_index' is required for loading layer_settings")]
            result = await restore_layer_settings(client, data, layer_index)
            return [TextContent(
                type="text",
                text=f"Restored {name!r} settings to L{layer_index}: {result['params_set']} params set",
            )]

        elif snap_type == "clip_effects":
            if not layer_index:
                return [TextContent(type="text", text="Error: 'layer_index' is required for loading clip_effects")]
            if not clip_index:
                return [TextContent(type="text", text="Error: 'clip_index' is required for loading clip_effects")]
            result = await restore_clip_effects(client, data, layer_index, clip_index)
            applied = result["applied"]
            skipped = result["skipped"]
            lines = [f"Restored {name!r} to L{layer_index} C{clip_index}:"]
            for a in applied:
                lines.append(f"  {a['effect']}: {a['params_set']} params set")
            if skipped:
                lines.append(f"  Skipped (not in target): {', '.join(skipped)}")
            return [TextContent(type="text", text="\n".join(lines))]

        elif snap_type == "crossfader":
            result = await restore_crossfader(client, data)
            return [TextContent(
                type="text",
                text=f"Restored crossfader from {name!r}: {result['params_set']} params set",
            )]

        elif snap_type == "layer_group":
            if not group_index:
                return [TextContent(type="text", text="Error: 'group_index' is required for loading layer_group")]
            result = await restore_layer_group(client, data, group_index)
            return [TextContent(
                type="text",
                text=f"Restored {name!r} to group {group_index}: {result['params_set']} params set",
            )]

        elif snap_type == "deck":
            return [TextContent(
                type="text",
                text=(
                    f"Deck snapshot {name!r} contains clip content — use subcommand "
                    f"'merge' with source_name and target_name to consolidate clips."
                ),
            )]

        else:
            return [TextContent(type="text", text=f"Unknown snapshot type: {snap_type}")]

    elif sub == "merge":
        source_name = arguments.get("source_name")
        target_name = arguments.get("target_name")
        if not source_name or not target_name:
            return [TextContent(type="text", text="Error: 'source_name' and 'target_name' are required for merge")]

        source_snap = _snapshot_store.load(source_name)
        target_snap = _snapshot_store.load(target_name)
        if source_snap is None:
            return [TextContent(type="text", text=f"Snapshot {source_name!r} not found")]
        if target_snap is None:
            return [TextContent(type="text", text=f"Snapshot {target_name!r} not found")]
        if source_snap.get("type") != "deck" or target_snap.get("type") != "deck":
            return [TextContent(type="text", text="Error: both snapshots must be of type 'deck'")]

        plan = plan_deck_merge(source_snap["data"], target_snap["data"])

        if arguments.get("dry_run"):
            lines = [
                f"Merge plan: {source_name!r} → {target_name!r}",
                f"Source deck: {plan['source_deck']}  Target deck: {plan['target_deck']}",
            ]
            for lp in plan["layers"]:
                lines.append(f"\n  Layer {lp['layer_index']} {lp['layer_name']!r}:")
                if lp["direct"]:
                    names = [c["clip_name"] or f"C{c['clip_index']}" for c in lp["direct"]]
                    lines.append(f"    direct (no conflict): {names}")
                for m in lp["moves"]:
                    c = m["clip"]
                    label = c["clip_name"] or f"C{c['clip_index']}"
                    lines.append(f"    move: {label!r} → slot {m['to_index']}")
                if lp["collisions"]:
                    lines.append(f"    UNRESOLVABLE: {[c['clip_name'] for c in lp['collisions']]}")
            return [TextContent(type="text", text="\n".join(lines))]

        client = await get_client()
        result = await execute_deck_merge(client, plan)

        lines = [f"Merged {source_name!r} → {target_name!r}:"]
        for lr in result["layers"]:
            lines.append(f"  Layer {lr['layer_index']} {lr['layer_name']!r}:")
            lines.append(f"    direct (already in place): {lr['direct_count']}")
            for m in lr["moved"]:
                lines.append(f"    moved: {m['clip']!r} C{m['from']} → C{m['to']}")
            for s in lr["skipped"]:
                lines.append(f"    skipped: {s['clip']!r} ({s['reason']})")
            if lr["collisions"]:
                lines.append(f"    unresolvable: {', '.join(lr['collisions'])}")
        return [TextContent(type="text", text="\n".join(lines))]

    elif sub == "show":
        name = arguments.get("name")
        if not name:
            return [TextContent(type="text", text="Error: 'name' is required for show")]
        snap = _snapshot_store.load(name)
        if snap is None:
            return [TextContent(type="text", text=f"Snapshot {name!r} not found")]
        data = snap.get("data", {})
        snap_type = snap.get("type", "")
        lines = [f"Snapshot: {name} ({snap_type})", f"Created: {snap.get('created', '')}"]

        if snap_type == "deck":
            lines.append(f"Deck: {data.get('deck_name', '')} (index {data.get('deck_index', '?')})")
            if "colorid" in data:
                lines.append(f"  colorid: {data['colorid']}")
            for snap_layer in data.get("layers", []):
                clips = snap_layer.get("connected_clips", [])
                lines.append(
                    f"  L{snap_layer['layer_index']} {snap_layer['layer_name']!r}: "
                    f"{len(clips)} connected clips"
                )
                for c in clips:
                    n_fx = len(c.get("effects", []))
                    has_file = "file" if c.get("file_path") else "source"
                    lines.append(
                        f"    C{c['clip_index']} {c['clip_name']!r} [{has_file}]"
                        + (f", {n_fx} effects" if n_fx else "")
                    )
        else:
            if data.get("layer_name"):
                lines.append(f"Layer: {data['layer_name']} (index {data.get('layer_index', '?')})")
            if data.get("clip_name"):
                lines.append(f"Clip: {data['clip_name']} (index {data.get('clip_index', '?')})")

            if "effects" in data:
                lines.append(f"Effects ({len(data['effects'])}):")
                for fx in data["effects"]:
                    bp = f", bypassed={fx['bypassed']}" if "bypassed" in fx else ""
                    n_params = len(fx.get("params", {}))
                    lines.append(f"  {fx['name']}{bp}, {n_params} params")

            for key in ("bypassed", "solo", "master", "video_opacity", "crossfadergroup",
                         "maskmode", "ignorecolumntrigger", "faderstart", "phase",
                         "behaviour", "curve", "colorid"):
                if key in data:
                    lines.append(f"  {key}: {data[key]}")

            if "mixer" in data:
                lines.append("  mixer:")
                for pname, pval in data["mixer"].items():
                    lines.append(f"    {pname}: {pval.get('value', '?')}")

            if "layer_names" in data:
                lines.append(f"  layers: {', '.join(data['layer_names'])}")

        return [TextContent(type="text", text="\n".join(lines))]

    elif sub == "delete":
        name = arguments.get("name")
        if not name:
            return [TextContent(type="text", text="Error: 'name' is required for delete")]
        if _snapshot_store.delete(name):
            return [TextContent(type="text", text=f"Deleted snapshot {name!r}")]
        return [TextContent(type="text", text=f"Snapshot {name!r} not found")]

    else:
        return [TextContent(type="text", text=f"Unknown subcommand: {sub}")]


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
