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


def _extract_params_dict(params: dict) -> dict:
    """Extract all params from a dict of name→param_dict, stripping IDs."""
    extracted = {}
    if isinstance(params, dict):
        for pname, pval in params.items():
            p = _extract_param(pval)
            if p is not None:
                extracted[pname] = p
    return extracted


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


def extract_clip_effects(state: dict, layer_index: int, clip_index: int) -> dict:
    """Snapshot all video effects on a specific clip (1-based indices).

    Returns effect list with param values — portable across compositions.
    """
    layers = state.get("layers", [])
    if layer_index < 1 or layer_index > len(layers):
        raise ValueError(f"layer_index {layer_index} out of range (1-{len(layers)})")
    layer = layers[layer_index - 1]
    clips = layer.get("clips", [])
    if clip_index < 1 or clip_index > len(clips):
        raise ValueError(f"clip_index {clip_index} out of range (1-{len(clips)})")
    clip = clips[clip_index - 1]
    video = clip.get("video")
    effects = video.get("effects", []) if isinstance(video, dict) else []
    return {
        "layer_index": layer_index,
        "layer_name": layer.get("name", {}).get("value", ""),
        "clip_index": clip_index,
        "clip_name": clip.get("name", {}).get("value", ""),
        "effects": [_extract_effect(fx) for fx in effects] if isinstance(effects, list) else [],
    }


def extract_crossfader(state: dict) -> dict:
    """Snapshot the crossfader settings (phase, behaviour, curve, mixer)."""
    cf = state.get("crossfader", {})
    out: dict[str, Any] = {}
    for key in ("phase", "behaviour", "curve"):
        param = cf.get(key)
        if isinstance(param, dict) and "value" in param:
            out[key] = param["value"]
    mixer = cf.get("mixer")
    if isinstance(mixer, dict):
        out["mixer"] = _extract_params_dict(mixer)
    return out


def _clip_connected(clip: dict) -> bool:
    """Return True if a clip slot has content loaded."""
    cv = clip.get("connected")
    if isinstance(cv, dict):
        return bool(cv.get("value", False))
    if isinstance(cv, bool):
        return cv
    # Fallback: treat as connected if it has a non-empty name
    name = clip.get("name", {})
    return bool(name.get("value", "") if isinstance(name, dict) else name)


def extract_deck(state: dict, deck_index: int) -> dict:
    """Snapshot a deck's clip content per layer (1-based index).

    Captures which clip slots are connected (have content loaded) in each
    layer, along with clip names, file paths, and effects. This snapshot is
    the input to ``plan_deck_merge`` for safely consolidating clips when
    merging two decks — preserving every clip without overwrites.
    """
    decks = state.get("decks", [])
    if deck_index < 1 or deck_index > len(decks):
        raise ValueError(f"deck_index {deck_index} out of range (1-{len(decks)})")
    deck = decks[deck_index - 1]
    out: dict[str, Any] = {
        "deck_index": deck_index,
        "deck_name": deck.get("name", {}).get("value", ""),
    }
    colorid = deck.get("colorid")
    if isinstance(colorid, dict) and "value" in colorid:
        out["colorid"] = colorid["value"]

    layers = state.get("layers", [])
    layer_snapshots = []
    for i, layer in enumerate(layers):
        clips = layer.get("clips", [])
        connected_clips: list[dict[str, Any]] = []
        for j, clip in enumerate(clips):
            if not _clip_connected(clip):
                continue
            clip_info: dict[str, Any] = {
                "clip_index": j + 1,
                "clip_name": clip.get("name", {}).get("value", ""),
            }
            video = clip.get("video")
            if isinstance(video, dict):
                fileinfo = video.get("fileinfo")
                if isinstance(fileinfo, dict) and fileinfo.get("path"):
                    clip_info["file_path"] = fileinfo["path"]
                effects = video.get("effects", [])
                if isinstance(effects, list) and effects:
                    clip_info["effects"] = [_extract_effect(fx) for fx in effects]
            connected_clips.append(clip_info)

        layer_snapshots.append({
            "layer_index": i + 1,
            "layer_name": layer.get("name", {}).get("value", ""),
            "connected_clips": connected_clips,
        })

    out["layers"] = layer_snapshots
    return out


def plan_deck_merge(source: dict, target: dict) -> dict:
    """Plan how to merge source deck clips into target without overwrites.

    For each layer, classifies each connected source clip as:
    - ``direct``: source clip's slot is empty in target → place as-is
    - ``move``: slot is occupied in target → relocate to next available slot
    - ``collision``: no empty slot could be found (should not happen in
      practice; only occurs if the composition is completely full)

    The planner tracks all assigned slots in a single set so direct
    placements and moves never conflict with each other.

    Returns a merge plan dict; pass it to ``execute_deck_merge`` to apply.
    """
    target_by_layer: dict[int, dict] = {
        l["layer_index"]: l for l in target.get("layers", [])
    }

    layer_plans = []
    for snap_layer in source.get("layers", []):
        layer_index = snap_layer["layer_index"]
        target_layer = target_by_layer.get(layer_index, {})

        source_clips = snap_layer.get("connected_clips", [])
        if not source_clips:
            continue

        # All occupied slots in the target (grows as we assign destinations)
        all_occupied: set[int] = {
            c["clip_index"] for c in target_layer.get("connected_clips", [])
        }

        # Determine search horizon for empty slots
        source_indices = {c["clip_index"] for c in source_clips}
        max_search = max(all_occupied | source_indices, default=0) + len(source_clips) + 1

        def next_empty(occupied: set[int], limit: int) -> int | None:
            for slot in range(1, limit + 1):
                if slot not in occupied:
                    return slot
            return None

        direct: list[dict] = []
        moves: list[dict] = []
        collisions: list[dict] = []

        for clip in source_clips:
            src_idx = clip["clip_index"]
            if src_idx not in all_occupied:
                direct.append(clip)
                all_occupied.add(src_idx)
            else:
                dest = next_empty(all_occupied, max_search)
                if dest is not None:
                    moves.append({"clip": clip, "to_index": dest})
                    all_occupied.add(dest)
                else:
                    collisions.append(clip)

        layer_plans.append({
            "layer_index": layer_index,
            "layer_name": snap_layer["layer_name"],
            "direct": direct,
            "moves": moves,
            "collisions": collisions,
        })

    return {
        "source_deck": source.get("deck_name", ""),
        "target_deck": target.get("deck_name", ""),
        "layers": layer_plans,
    }


def extract_layer_group(state: dict, group_index: int) -> dict:
    """Snapshot a layer group's settings (1-based index)."""
    groups = state.get("layergroups", [])
    if group_index < 1 or group_index > len(groups):
        raise ValueError(f"group_index {group_index} out of range (1-{len(groups)})")
    group = groups[group_index - 1]
    out: dict[str, Any] = {
        "group_index": group_index,
        "name": group.get("name", {}).get("value", ""),
    }
    for key in ("bypassed", "solo", "master", "crossfadergroup",
                "ignorecolumntrigger"):
        param = group.get(key)
        if isinstance(param, dict) and "value" in param:
            out[key] = param["value"]
    # Capture which layer indices are in the group
    layers_in_group = group.get("layers", [])
    if isinstance(layers_in_group, list):
        out["layer_names"] = [
            l.get("name", {}).get("value", "") for l in layers_in_group
            if isinstance(l, dict)
        ]
    return out


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


async def restore_clip_effects(
    client: ResolumeAgentClient,
    snapshot: dict,
    target_layer_index: int,
    target_clip_index: int,
) -> dict:
    """Restore effect parameters from a clip snapshot to a target clip.

    Same name-matching logic as restore_layer_effects.
    """
    layers = client.state.get("layers", [])
    if target_layer_index < 1 or target_layer_index > len(layers):
        raise ValueError(f"target_layer_index {target_layer_index} out of range (1-{len(layers)})")
    target_layer = layers[target_layer_index - 1]
    clips = target_layer.get("clips", [])
    if target_clip_index < 1 or target_clip_index > len(clips):
        raise ValueError(f"target_clip_index {target_clip_index} out of range (1-{len(clips)})")
    target_clip = clips[target_clip_index - 1]

    video = target_clip.get("video")
    live_effects = video.get("effects", []) if isinstance(video, dict) else []
    if not isinstance(live_effects, list):
        live_effects = []

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

        if "bypassed" in snap_fx and live_fx.get("bypassed"):
            param_id = _find_param_id(live_fx["bypassed"])
            if param_id:
                await client.send_command("set", f"/parameter/by-id/{param_id}", snap_fx["bypassed"])
                fx_applied["params_set"] += 1

        for pname, psnap in snap_fx.get("params", {}).items():
            live_params = live_fx.get("params", {})
            live_p = live_params.get(pname) if isinstance(live_params, dict) else None
            if live_p is None:
                continue
            param_id = _find_param_id(live_p)
            if param_id:
                await client.send_command("set", f"/parameter/by-id/{param_id}", psnap["value"])
                fx_applied["params_set"] += 1

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


async def restore_crossfader(client: ResolumeAgentClient, snapshot: dict) -> dict:
    """Restore crossfader settings from a snapshot."""
    cf = client.state.get("crossfader", {})
    params_set = 0

    for key in ("phase", "behaviour", "curve"):
        if key not in snapshot:
            continue
        live_param = cf.get(key)
        param_id = _find_param_id(live_param)
        if param_id:
            await client.send_command("set", f"/parameter/by-id/{param_id}", snapshot[key])
            params_set += 1

    for pname, psnap in snapshot.get("mixer", {}).items():
        live_mixer = cf.get("mixer", {})
        live_p = live_mixer.get(pname) if isinstance(live_mixer, dict) else None
        param_id = _find_param_id(live_p) if live_p else None
        if param_id:
            await client.send_command("set", f"/parameter/by-id/{param_id}", psnap["value"])
            params_set += 1

    return {"params_set": params_set}


async def restore_layer_group(
    client: ResolumeAgentClient,
    snapshot: dict,
    target_group_index: int,
) -> dict:
    """Restore layer group settings from a snapshot."""
    groups = client.state.get("layergroups", [])
    if target_group_index < 1 or target_group_index > len(groups):
        raise ValueError(f"target_group_index {target_group_index} out of range (1-{len(groups)})")
    target = groups[target_group_index - 1]
    params_set = 0

    for key in ("bypassed", "solo", "master", "crossfadergroup",
                "ignorecolumntrigger"):
        if key not in snapshot:
            continue
        live_param = target.get(key)
        param_id = _find_param_id(live_param)
        if param_id:
            await client.send_command("set", f"/parameter/by-id/{param_id}", snapshot[key])
            params_set += 1

    return {"params_set": params_set}


async def execute_deck_merge(
    client: ResolumeAgentClient,
    plan: dict,
) -> dict:
    """Execute a merge plan produced by ``plan_deck_merge`` over WebSocket.

    For each layer:
    - ``direct`` clips already occupy an empty target slot — no action needed.
    - ``move`` clips are opened into the destination slot via WebSocket
      ``post`` + ``/open``, then the source slot is cleared via ``/clear``.
      Source-based clips without a ``file_path`` are skipped with a note.
    """
    import urllib.parse

    layer_results = []

    for layer_plan in plan.get("layers", []):
        layer_index = layer_plan["layer_index"]
        layer_name = layer_plan["layer_name"]
        moved: list[dict] = []
        skipped: list[dict] = []
        collisions = layer_plan.get("collisions", [])

        for entry in layer_plan.get("moves", []):
            clip = entry["clip"]
            src_idx = clip["clip_index"]
            dest_idx = entry["to_index"]
            file_path = clip.get("file_path")

            if not file_path:
                skipped.append({
                    "clip": clip["clip_name"],
                    "from": src_idx,
                    "to": dest_idx,
                    "reason": "no file_path (source-based clip — move manually)",
                })
                continue

            file_url = "file://" + urllib.parse.quote(file_path, safe="/:")
            await client.send_command(
                "post",
                f"/composition/layers/{layer_index}/clips/{dest_idx}/open",
                file_url,
            )
            await client.send_command(
                "post",
                f"/composition/layers/{layer_index}/clips/{src_idx}/clear",
                None,
            )
            moved.append({"clip": clip["clip_name"], "from": src_idx, "to": dest_idx})

        layer_results.append({
            "layer_index": layer_index,
            "layer_name": layer_name,
            "direct_count": len(layer_plan.get("direct", [])),
            "moved": moved,
            "skipped": skipped,
            "collisions": [c["clip_name"] for c in collisions],
        })

    return {"layers": layer_results}


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
                data = meta.get("data", {})
                snap_type = meta.get("type", "unknown")
                entry: dict[str, Any] = {
                    "name": meta.get("name", fname[:-5]),
                    "type": snap_type,
                    "created": meta.get("created", ""),
                }
                if snap_type == "deck":
                    entry["deck_name"] = data.get("deck_name", "")
                    entry["deck_index"] = data.get("deck_index", "")
                    entry["num_clips"] = sum(
                        len(l.get("connected_clips", [])) for l in data.get("layers", [])
                    )
                else:
                    entry["layer_name"] = data.get("layer_name", "")
                    entry["layer_index"] = data.get("layer_index", "")
                results.append(entry)
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
