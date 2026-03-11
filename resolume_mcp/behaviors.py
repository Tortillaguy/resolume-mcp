"""
Behavior engine for Resolume MCP
---------------------------------
Named, persistent reactive rules: when a parameter changes and a condition
is met, perform a declarative action. Survives server restarts via JSON file.
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from resolume_mcp.client import ResolumeAgentClient

logger = logging.getLogger("ResolumeAgent.behaviors")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Condition:
    op: str = "any"       # any|eq|neq|gt|lt|gte|lte|truthy|falsy
    value: Any = None     # comparison operand (ignored for any/truthy/falsy)


@dataclass
class Action:
    type: str = ""                               # set_parameter|toggle_parameter|set_parameters
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Behavior:
    id: str = ""
    name: str = ""
    trigger_param_id: int = 0
    condition: Condition = field(default_factory=Condition)
    action: Action = field(default_factory=Action)
    enabled: bool = True
    description: str = ""
    # Runtime-only (not persisted)
    fire_count: int = field(default=0, repr=False)
    last_error: str | None = field(default=None, repr=False)
    _callback: Any = field(default=None, repr=False)


_RUNTIME_FIELDS = {"fire_count", "last_error", "_callback"}

_CONDITION_OPS = {
    "any":    lambda v, _: True,
    "truthy": lambda v, _: bool(v),
    "falsy":  lambda v, _: not bool(v),
    "eq":     lambda v, c: v == c,
    "neq":    lambda v, c: v != c,
    "gt":     lambda v, c: v > c,
    "lt":     lambda v, c: v < c,
    "gte":    lambda v, c: v >= c,
    "lte":    lambda v, c: v <= c,
}


def check_condition(cond: Condition, value: Any) -> bool:
    fn = _CONDITION_OPS.get(cond.op)
    if fn is None:
        return False
    try:
        return fn(value, cond.value)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _behavior_to_dict(b: Behavior) -> dict:
    d = asdict(b)
    for k in _RUNTIME_FIELDS:
        d.pop(k, None)
    return d


def _behavior_from_dict(d: dict) -> Behavior:
    cond = Condition(**d.pop("condition", {}))
    action = Action(**d.pop("action", {}))
    d.pop("fire_count", None)
    d.pop("last_error", None)
    d.pop("_callback", None)
    return Behavior(condition=cond, action=action, **d)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class DashboardNamingWatcher:
    """Watches for new dashboard bindings named 'Opacity' and renames them.

    When an effect's Opacity parameter is bound to a dashboard knob the knob
    inherits the generic label "Opacity", which is ambiguous when multiple
    effects are on the same layer/clip. This watcher detects the binding
    event (a structural change in a dashboard ParameterCollection) and
    renames the knob to the display name of the effect that owns the parameter.

    Rename mechanism: ``remove`` the old key + ``post`` under the new name.
    Both are standard WebSocket ``post``/``remove`` actions (path+body format),
    the same pattern used for adding effects or opening clips.

    Scopes watched: composition dashboard, every layer dashboard, every clip
    dashboard — all can have parameters bound to them.
    """

    def __init__(self, client: ResolumeAgentClient):
        self._client = client
        # Maps scope_key → frozenset of dashboard param names seen last update
        self._prev_keys: dict[str, frozenset] = {}
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        self._client.add_state_listener(self._on_state)

    def stop(self) -> None:
        self._client.remove_state_listener(self._on_state)

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def _on_state(self, state: dict) -> None:
        if not self._enabled:
            return
        scopes = self._collect_scopes(state)
        for scope_key, ws_path, dashboard in scopes:
            self._check(state, scope_key, ws_path, dashboard)

    def _collect_scopes(self, state: dict):
        """Yield (scope_key, ws_path, dashboard_dict) for every dashboard in state."""
        yield "composition", "/composition", state.get("dashboard", {})
        for i, layer in enumerate(state.get("layers", [])):
            yield f"layer_{i+1}", f"/composition/layers/{i+1}", layer.get("dashboard", {})
            for j, clip in enumerate(layer.get("clips", [])):
                yield (
                    f"clip_{i+1}_{j+1}",
                    f"/composition/layers/{i+1}/clips/{j+1}",
                    clip.get("dashboard", {}),
                )

    def _check(self, state: dict, scope_key: str, ws_path: str, dashboard: dict) -> None:
        if not isinstance(dashboard, dict):
            return
        current = frozenset(dashboard.keys())
        prev = self._prev_keys.get(scope_key, frozenset())
        self._prev_keys[scope_key] = current

        for key in current - prev:
            if key.lower() == "opacity":
                entry = dashboard[key]
                param_id = entry.get("id") if isinstance(entry, dict) else None
                if not param_id:
                    continue
                effect_name = self._find_effect_name(state, param_id)
                if effect_name and effect_name.lower() != "opacity":
                    loop = asyncio.get_event_loop()
                    loop.create_task(
                        self._rename(ws_path, key, effect_name, entry)
                    )
                    logger.info(
                        f"Dashboard binding: renaming {key!r} → {effect_name!r} at {ws_path}"
                    )

    def _find_effect_name(self, state: dict, param_id: int) -> str | None:
        """Scan all effects in state for one whose params include this ID."""
        for layer in state.get("layers", []):
            name = self._scan_effects(layer.get("video", {}).get("effects", []), param_id)
            if name:
                return name
            for clip in layer.get("clips", []):
                video = clip.get("video")
                fx_list = video.get("effects", []) if isinstance(video, dict) else []
                name = self._scan_effects(fx_list, param_id)
                if name:
                    return name
        return None

    def _scan_effects(self, effects: list, param_id: int) -> str | None:
        for fx in effects:
            if not isinstance(fx, dict):
                continue
            for pval in (fx.get("params") or {}).values():
                if isinstance(pval, dict) and pval.get("id") == param_id:
                    return fx.get("display_name") or fx.get("name") or ""
            for pval in (fx.get("mixer") or {}).values():
                if isinstance(pval, dict) and pval.get("id") == param_id:
                    return fx.get("display_name") or fx.get("name") or ""
        return None

    async def _rename(self, ws_path: str, old_key: str, new_key: str, entry: dict) -> None:
        """Remove the old dashboard key and post it under the new name."""
        try:
            await self._client.send_command(
                "remove", f"{ws_path}/dashboard/{old_key}"
            )
            await self._client.send_command(
                "post", f"{ws_path}/dashboard/{new_key}", entry
            )
        except Exception as e:
            logger.warning(f"Dashboard rename {old_key!r} → {new_key!r} failed: {e}")


class BehaviorManager:

    def __init__(self, client: ResolumeAgentClient, persist_path: str, snapshot_store=None):
        self._client = client
        self._persist_path = persist_path
        self._snapshot_store = snapshot_store  # Optional SnapshotStore for restore_snapshot action
        self._behaviors: dict[str, Behavior] = {}  # id → Behavior
        self.dashboard_naming = DashboardNamingWatcher(client)

    # --- CRUD ---

    async def add(self, behavior: Behavior) -> Behavior:
        if not behavior.id:
            behavior.id = uuid.uuid4().hex[:12]
        self._behaviors[behavior.id] = behavior
        if behavior.enabled:
            await self._activate(behavior)
        self._persist()
        return behavior

    async def remove(self, behavior_id: str) -> bool:
        b = self._behaviors.pop(behavior_id, None)
        if b is None:
            return False
        await self._deactivate(b)
        self._persist()
        return True

    async def enable(self, behavior_id: str) -> bool:
        b = self._behaviors.get(behavior_id)
        if b is None:
            return False
        b.enabled = True
        await self._activate(b)
        self._persist()
        return True

    async def disable(self, behavior_id: str) -> bool:
        b = self._behaviors.get(behavior_id)
        if b is None:
            return False
        b.enabled = False
        await self._deactivate(b)
        self._persist()
        return True

    def list(self) -> list[dict]:
        results = []
        for b in self._behaviors.values():
            d = _behavior_to_dict(b)
            d["fire_count"] = b.fire_count
            d["last_error"] = b.last_error
            results.append(d)
        return results

    def get(self, behavior_id: str) -> Behavior | None:
        return self._behaviors.get(behavior_id)

    # --- Lifecycle ---

    async def start(self) -> None:
        self.dashboard_naming.start()
        for b in self._load():
            self._behaviors[b.id] = b
            if b.enabled:
                await self._activate(b)
        if self._behaviors:
            logger.info(f"Loaded {len(self._behaviors)} behaviors ({sum(1 for b in self._behaviors.values() if b.enabled)} enabled)")

    async def stop(self) -> None:
        self.dashboard_naming.stop()
        for b in self._behaviors.values():
            await self._deactivate(b)

    # --- Activation ---

    async def _activate(self, b: Behavior) -> None:
        if b._callback is not None:
            return  # already active
        cb = self._make_callback(b)
        b._callback = cb
        await self._client.monitor_parameter(b.trigger_param_id, cb)

    async def _deactivate(self, b: Behavior) -> None:
        if b._callback is None:
            return
        await self._client.unmonitor_parameter(b.trigger_param_id, b._callback)
        b._callback = None

    def _make_callback(self, behavior: Behavior):
        def _on_change(data: dict):
            if not behavior.enabled:
                return
            value = data.get("value")
            if not check_condition(behavior.condition, value):
                return
            behavior.fire_count += 1
            loop = asyncio.get_event_loop()
            task = loop.create_task(self._execute_action(behavior, value))
            task.add_done_callback(lambda t: self._handle_task_result(behavior, t))
        return _on_change

    def _handle_task_result(self, behavior: Behavior, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            behavior.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"Behavior {behavior.name!r} action failed: {behavior.last_error}")
        else:
            behavior.last_error = None

    # --- Action execution ---

    async def _execute_action(self, behavior: Behavior, trigger_value: Any) -> None:
        action = behavior.action

        if action.type == "set_parameter":
            path = action.params["path"]
            value = action.params["value"]
            await self._client.send_command("set", path, value)

        elif action.type == "toggle_parameter":
            path = action.params["path"]
            # Read current value from state by walking by-id
            current = self._read_param_value(path)
            if isinstance(current, bool):
                await self._client.send_command("set", path, not current)
            elif isinstance(current, (int, float)):
                await self._client.send_command("set", path, 1 - current)
            else:
                # Fallback: try boolean toggle
                await self._client.send_command("set", path, not current)

        elif action.type == "toggle_parameters":
            for p in action.params.get("parameters", []):
                path = p["path"]
                current = self._read_param_value(path)
                if isinstance(current, bool):
                    await self._client.send_command("set", path, not current)
                elif isinstance(current, (int, float)):
                    await self._client.send_command("set", path, 1 - current)
                else:
                    await self._client.send_command("set", path, not current)

        elif action.type == "set_parameters":
            for p in action.params.get("parameters", []):
                await self._client.send_command("set", p["path"], p["value"])

        elif action.type == "restore_snapshot":
            if self._snapshot_store is None:
                raise ValueError("No snapshot store configured")
            from resolume_mcp.snapshots import (
                restore_clip_effects, restore_crossfader, restore_layer_effects,
                restore_layer_group, restore_layer_settings,
            )
            snap_name = action.params.get("name")
            if not snap_name:
                raise ValueError("restore_snapshot requires 'name' in params")
            snap = self._snapshot_store.load(snap_name)
            if snap is None:
                raise ValueError(f"Snapshot {snap_name!r} not found")
            snap_type = snap.get("type", "")
            data = snap.get("data", {})
            target_layer = action.params.get("layer_index")
            target_clip = action.params.get("clip_index")
            target_group = action.params.get("group_index")

            if snap_type == "layer_effects":
                await restore_layer_effects(self._client, data, target_layer or data.get("layer_index", 1))
            elif snap_type == "layer_settings":
                await restore_layer_settings(self._client, data, target_layer or data.get("layer_index", 1))
            elif snap_type == "clip_effects":
                await restore_clip_effects(
                    self._client, data,
                    target_layer or data.get("layer_index", 1),
                    target_clip or data.get("clip_index", 1),
                )
            elif snap_type == "crossfader":
                await restore_crossfader(self._client, data)
            elif snap_type == "layer_group":
                await restore_layer_group(self._client, data, target_group or data.get("group_index", 1))
            else:
                raise ValueError(f"Cannot restore snapshot type: {snap_type}")

        else:
            raise ValueError(f"Unknown action type: {action.type}")

    def _read_param_value(self, path: str) -> Any:
        """Read a parameter's current value from client state.

        Handles /parameter/by-id/{id} paths by scanning the state tree
        for a dict with matching id, then returning its 'value' field.
        """
        if path.startswith("/parameter/by-id/"):
            try:
                param_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return None
            return self._find_param_value_by_id(self._client.state, param_id)
        return None

    def _find_param_value_by_id(self, node: Any, param_id: int, max_depth: int = 10) -> Any:
        """Recursively search state for a parameter dict with matching id."""
        if max_depth <= 0:
            return None
        if isinstance(node, dict):
            if node.get("id") == param_id and "value" in node:
                return node["value"]
            for v in node.values():
                result = self._find_param_value_by_id(v, param_id, max_depth - 1)
                if result is not None:
                    return result
        elif isinstance(node, list):
            for item in node:
                result = self._find_param_value_by_id(item, param_id, max_depth - 1)
                if result is not None:
                    return result
        return None

    # --- Persistence ---

    def _persist(self) -> None:
        dirpath = os.path.dirname(self._persist_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        data = {
            "version": 1,
            "behaviors": [_behavior_to_dict(b) for b in self._behaviors.values()],
        }
        tmp = self._persist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._persist_path)

    def _load(self) -> list[Behavior]:
        if not os.path.exists(self._persist_path):
            return []
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            return [_behavior_from_dict(d) for d in data.get("behaviors", [])]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to load behaviors from {self._persist_path}: {e}")
            return []
