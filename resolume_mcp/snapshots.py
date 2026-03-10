"""
Snapshot engine for Resolume MCP
---------------------------------
Capture and restore slices of Resolume composition state (effects, params,
layer settings) across compositions. Persisted as JSON files.

Snapshots store human-readable names (effect name, param name) rather than
numeric IDs, so they can be restored to a different composition where IDs
differ but the structural layout is the same.
"""

import json
import logging
import os
import time
from typing import Any

from resolume_mcp.client import ResolumeAgentClient

logger = logging.getLogger("ResolumeAgent.snapshots")


# ---------------------------------------------------------------------------
# Extraction: pull data from client.state
# ---------------------------------------------------------------------------

def _extract_param(param: dict) -> dict | None:
    """Extract a portable parameter snapshot (no IDs, just name/value/type)."""
    if not isinstance(param, dict) or "value" not in param:
        return None
    out = {"value": param["value"], "valuetype": param.get("valuetype", "")}
    # Preserve range bounds for context (helps agents understand the param)
    for key in ("min", "max"):
        if key in param:
            out[key] = param[key]
    return out


def _extract_effect(effect: dict) -> dict:
    """Extract a portable effect snapshot."""
    out: dict[str, Any] = {"name": effect.get("name", "")}
    if effect.get("display_name"):
        out["display_name"] = effect["display_name"]
    bp = effect.get("bypassed")
    if bp and isinstance(bp, dict):
        out["bypassed"] = bp.get("value", False)
    # Extract params
    params = effect.get("params", {})
    if isinstance(params, dict):
        extracted = {}
        for pname, pval in params.items():
            p = _extract_param(pval)
            if p is not None:
                extracted[pname] = p
        if extracted:
            out["params"] = extracted
    # Extract mixer params if present
    mixer = effect.get("mixer")
    if isinstance(mixer, dict):
        extracted = {}
        for pname, pval in mixer.items():
            p = _extract_param(pval)
            if p is not None:
                extracted[pname] = p
        if extracted:
            out["mixer"] = extracted
    return out


def extract_layer_effects(state: dict, layer_index: int) -> dict:
    """Snapshot all video effects on a layer (1-based index).

    Returns a dict with layer metadata and a list of effect snapshots.
    """
    layers = state.get("layers", [])
    if layer_index < 1 or layer_index > len(layers):
        raise ValueError(f"layer_index {layer_index} out of range (1-{len(layers)})")
    layer = layers[layer_index - 1]
    effects = layer.get("video", {}).get("effects", [])
    return {
        "layer_name": layer.get("name", {}).get("value", ""),
        "layer_index": layer_index,
        "effects": [_extract_effect(fx) for fx in effects] if isinstance(effects, list) else [],
    }


def extract_layer_settings(state: dict, layer_index: int) -> dict:
    """Snapshot layer-level settings (opacity, bypass, master, crossfader group)."""
    layers = state.get("layers", [])
    if layer_index < 1 or layer_index > len(layers):
        raise ValueError(f"layer_index {layer_index} out of range (1-{len(layers)})")
    layer = layers[layer_index - 1]
    settings: dict[str, Any] = {
        "layer_name": layer.get("name", {}).get("value", ""),
        "layer_index": layer_index,
    }
    # Extract known layer-level params
    for key in ("bypassed", "solo", "master", "crossfadergroup", "maskmode",
                "ignorecolumntrigger", "faderstart"):
        param = layer.get(key)
        if isinstance(param, dict) and "value" in param:
            settings[key] = param["value"]
    # Video-level opacity
    opacity = layer.get("video", {}).get("opacity")
    if isinstance(opacity, dict) and "value" in opacity:
        settings["video_opacity"] = opacity["value"]
    return settings


# ---------------------------------------------------------------------------
# Restore: apply snapshot to current state
# ---------------------------------------------------------------------------

def _find_param_id(param_dict: dict) -> int | None:
    """Get the numeric ID from a live state parameter dict."""
    if isinstance(param_dict, dict):
        return param_dict.get("id")
    return None


async def restore_layer_effects(
    client: ResolumeAgentClient,
    snapshot: dict,
    target_layer_index: int,
) -> dict:
    """Restore effect parameters from a snapshot to a target layer.

    Matches effects by name. For each matched effect, sets all params
    that exist in both the snapshot and the live layer. Skips effects
    that don't exist in the target.

    Returns a summary of what was applied and what was skipped.
    """
    layers = client.state.get("layers", [])
    if target_layer_index < 1 or target_layer_index > len(layers):
        raise ValueError(f"target_layer_index {target_layer_index} out of range (1-{len(layers)})")

    target_layer = layers[target_layer_index - 1]
    live_effects = target_layer.get("video", {}).get("effects", [])
    if not isinstance(live_effects, list):
        live_effects = []

    # Index live effects by name for matching
    live_by_name: dict[str, dict] = {}
    for fx in live_effects:
        name = fx.get("name", "")
        if name:
            live_by_name[name] = fx

    applied = []
    skipped = []

    for snap_fx in snapshot.get("effects", []):
        fx_name = snap_fx.get("name", "")
        live_fx = live_by_name.get(fx_name)

        if live_fx is None:
            skipped.append(fx_name)
            continue

        fx_applied = {"effect": fx_name, "params_set": 0}

        # Restore bypassed
        if "bypassed" in snap_fx and live_fx.get("bypassed"):
            param_id = _find_param_id(live_fx["bypassed"])
            if param_id:
                await client.send_command("set", f"/parameter/by-id/{param_id}", snap_fx["bypassed"])
                fx_applied["params_set"] += 1

        # Restore params
        for pname, psnap in snap_fx.get("params", {}).items():
            live_params = live_fx.get("params", {})
            live_p = live_params.get(pname) if isinstance(live_params, dict) else None
            if live_p is None:
                continue
            param_id = _find_param_id(live_p)
            if param_id:
                await client.send_command("set", f"/parameter/by-id/{param_id}", psnap["value"])
                fx_applied["params_set"] += 1

        # Restore mixer params
        for pname, psnap in snap_fx.get("mixer", {}).items():
            live_mixer = live_fx.get("mixer", {})
            live_p = live_mixer.get(pname) if isinstance(live_mixer, dict) else None
            if live_p is None:
                continue
            param_id = _find_param_id(live_p)
            if param_id:
                await client.send_command("set", f"/parameter/by-id/{param_id}", psnap["value"])
                fx_applied["params_set"] += 1

        applied.append(fx_applied)

    return {"applied": applied, "skipped": skipped}


async def restore_layer_settings(
    client: ResolumeAgentClient,
    snapshot: dict,
    target_layer_index: int,
) -> dict:
    """Restore layer-level settings from a snapshot."""
    layers = client.state.get("layers", [])
    if target_layer_index < 1 or target_layer_index > len(layers):
        raise ValueError(f"target_layer_index {target_layer_index} out of range (1-{len(layers)})")

    target_layer = layers[target_layer_index - 1]
    params_set = 0

    for key in ("bypassed", "solo", "master", "crossfadergroup", "maskmode",
                "ignorecolumntrigger", "faderstart"):
        if key not in snapshot:
            continue
        live_param = target_layer.get(key)
        param_id = _find_param_id(live_param)
        if param_id:
            await client.send_command("set", f"/parameter/by-id/{param_id}", snapshot[key])
            params_set += 1

    if "video_opacity" in snapshot:
        opacity_param = target_layer.get("video", {}).get("opacity")
        param_id = _find_param_id(opacity_param)
        if param_id:
            await client.send_command("set", f"/parameter/by-id/{param_id}", snapshot["video_opacity"])
            params_set += 1

    return {"params_set": params_set}


# ---------------------------------------------------------------------------
# Snapshot store (persistence)
# ---------------------------------------------------------------------------

class SnapshotStore:
    """Manages named snapshots on disk."""

    def __init__(self, store_dir: str):
        self._dir = store_dir

    def save(self, name: str, snapshot_type: str, data: dict) -> dict:
        """Save a snapshot. Returns metadata."""
        os.makedirs(self._dir, exist_ok=True)
        meta = {
            "name": name,
            "type": snapshot_type,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data": data,
        }
        path = os.path.join(self._dir, f"{name}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, path)
        return {"name": name, "type": snapshot_type, "path": path}

    def load(self, name: str) -> dict | None:
        """Load a snapshot by name. Returns None if not found."""
        path = os.path.join(self._dir, f"{name}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def list(self) -> list[dict]:
        """List all saved snapshots (metadata only, no data)."""
        if not os.path.isdir(self._dir):
            return []
        results = []
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path) as f:
                    meta = json.load(f)
                results.append({
                    "name": meta.get("name", fname[:-5]),
                    "type": meta.get("type", "unknown"),
                    "created": meta.get("created", ""),
                    "layer_name": meta.get("data", {}).get("layer_name", ""),
                    "layer_index": meta.get("data", {}).get("layer_index", ""),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    def delete(self, name: str) -> bool:
        """Delete a snapshot. Returns False if not found."""
        path = os.path.join(self._dir, f"{name}.json")
        if not os.path.exists(path):
            return False
        os.remove(path)
        return True
