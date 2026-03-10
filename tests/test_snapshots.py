"""Tests for the snapshot engine."""

import json
import os

import pytest
import pytest_asyncio

from resolume_mcp.client import ResolumeAgentClient
from resolume_mcp.snapshots import (
    SnapshotStore,
    extract_clip_effects,
    extract_crossfader,
    extract_deck,
    extract_layer_effects,
    extract_layer_group,
    extract_layer_settings,
    restore_clip_effects,
    restore_crossfader,
    restore_layer_effects,
    restore_layer_group,
    restore_layer_settings,
    _extract_effect,
    _extract_param,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_STATE = {
    "layers": [
        {
            "id": 1,
            "name": {"value": "BG FX", "id": 10},
            "bypassed": {"value": False, "id": 11, "valuetype": "ParamBoolean"},
            "solo": {"value": False, "id": 12, "valuetype": "ParamBoolean"},
            "master": {"value": 1.0, "id": 13, "valuetype": "ParamRange", "min": 0.0, "max": 1.0},
            "crossfadergroup": {"value": 0, "id": 14, "valuetype": "ParamChoice"},
            "maskmode": {"value": 0, "id": 15, "valuetype": "ParamChoice"},
            "ignorecolumntrigger": {"value": False, "id": 16, "valuetype": "ParamBoolean"},
            "faderstart": {"value": False, "id": 17, "valuetype": "ParamBoolean"},
            "video": {
                "opacity": {"value": 0.8, "id": 20, "valuetype": "ParamRange", "min": 0.0, "max": 1.0},
                "effects": [
                    {
                        "id": 100,
                        "name": "Transform",
                        "display_name": "Transform",
                        "params": {
                            "Position X": {"id": 101, "valuetype": "ParamRange", "value": 0.0, "min": -100.0, "max": 100.0},
                            "Scale": {"id": 102, "valuetype": "ParamRange", "value": 100.0, "min": 0.0, "max": 1000.0},
                        },
                    },
                    {
                        "id": 200,
                        "name": "Blur",
                        "display_name": "Blur",
                        "bypassed": {"id": 201, "valuetype": "ParamBoolean", "value": True},
                        "params": {
                            "Amount": {"id": 202, "valuetype": "ParamRange", "value": 0.5, "min": 0.0, "max": 1.0},
                        },
                        "mixer": {
                            "Opacity": {"id": 203, "valuetype": "ParamRange", "value": 0.75, "min": 0.0, "max": 1.0},
                        },
                    },
                ],
            },
            "clips": [
                {
                    "id": 500,
                    "name": {"value": "Clip 1", "id": 501},
                    "video": {
                        "effects": [
                            {
                                "id": 510,
                                "name": "Transform",
                                "display_name": "Transform",
                                "params": {
                                    "Position X": {"id": 511, "valuetype": "ParamRange", "value": 5.0, "min": -100.0, "max": 100.0},
                                },
                            },
                            {
                                "id": 520,
                                "name": "Glow",
                                "display_name": "Glow",
                                "bypassed": {"id": 521, "valuetype": "ParamBoolean", "value": False},
                                "params": {
                                    "Intensity": {"id": 522, "valuetype": "ParamRange", "value": 0.6, "min": 0.0, "max": 1.0},
                                },
                            },
                        ],
                    },
                },
                {
                    "id": 600,
                    "name": {"value": "Clip 2", "id": 601},
                    "video": {
                        "effects": [
                            {
                                "id": 610,
                                "name": "Transform",
                                "display_name": "Transform",
                                "params": {
                                    "Position X": {"id": 611, "valuetype": "ParamRange", "value": -5.0, "min": -100.0, "max": 100.0},
                                },
                            },
                        ],
                    },
                },
            ],
        },
        {
            "id": 2,
            "name": {"value": "FG FX", "id": 30},
            "bypassed": {"value": False, "id": 31, "valuetype": "ParamBoolean"},
            "solo": {"value": False, "id": 32, "valuetype": "ParamBoolean"},
            "master": {"value": 1.0, "id": 33, "valuetype": "ParamRange", "min": 0.0, "max": 1.0},
            "video": {
                "opacity": {"value": 1.0, "id": 40, "valuetype": "ParamRange"},
                "effects": [
                    {
                        "id": 300,
                        "name": "Transform",
                        "display_name": "Transform",
                        "params": {
                            "Position X": {"id": 301, "valuetype": "ParamRange", "value": 10.0, "min": -100.0, "max": 100.0},
                            "Scale": {"id": 302, "valuetype": "ParamRange", "value": 50.0, "min": 0.0, "max": 1000.0},
                        },
                    },
                    {
                        "id": 400,
                        "name": "Blur",
                        "display_name": "Blur",
                        "bypassed": {"id": 401, "valuetype": "ParamBoolean", "value": False},
                        "params": {
                            "Amount": {"id": 402, "valuetype": "ParamRange", "value": 0.2, "min": 0.0, "max": 1.0},
                        },
                        "mixer": {
                            "Opacity": {"id": 403, "valuetype": "ParamRange", "value": 1.0, "min": 0.0, "max": 1.0},
                        },
                    },
                ],
            },
            "clips": [],
        },
    ],
    "crossfader": {
        "phase": {"value": 0.5, "id": 700, "valuetype": "ParamRange", "min": 0.0, "max": 1.0},
        "behaviour": {"value": 0, "id": 701, "valuetype": "ParamChoice"},
        "curve": {"value": 1, "id": 702, "valuetype": "ParamChoice"},
        "mixer": {
            "Opacity": {"id": 703, "valuetype": "ParamRange", "value": 1.0, "min": 0.0, "max": 1.0},
        },
    },
    "decks": [
        {
            "id": 800,
            "name": {"value": "Main Deck", "id": 801},
            "colorid": {"value": 3, "id": 802, "valuetype": "ParamChoice"},
        },
        {
            "id": 810,
            "name": {"value": "FX Deck", "id": 811},
            "colorid": {"value": 5, "id": 812, "valuetype": "ParamChoice"},
        },
    ],
    "layergroups": [
        {
            "id": 900,
            "name": {"value": "Group A", "id": 901},
            "bypassed": {"value": False, "id": 902, "valuetype": "ParamBoolean"},
            "solo": {"value": False, "id": 903, "valuetype": "ParamBoolean"},
            "master": {"value": 0.8, "id": 904, "valuetype": "ParamRange", "min": 0.0, "max": 1.0},
            "crossfadergroup": {"value": 1, "id": 905, "valuetype": "ParamChoice"},
            "ignorecolumntrigger": {"value": True, "id": 906, "valuetype": "ParamBoolean"},
            "layers": [
                {"name": {"value": "BG FX"}},
                {"name": {"value": "FG FX"}},
            ],
        },
    ],
}


@pytest.fixture
def client():
    c = ResolumeAgentClient(dry_run=True)
    c.state = SAMPLE_STATE
    return c


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(str(tmp_path / "snapshots"))


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------

def test_extract_param():
    p = _extract_param({"id": 1, "value": 0.5, "valuetype": "ParamRange", "min": 0.0, "max": 1.0})
    assert p == {"value": 0.5, "valuetype": "ParamRange", "min": 0.0, "max": 1.0}


def test_extract_param_no_value():
    assert _extract_param({"id": 1}) is None


def test_extract_param_not_dict():
    assert _extract_param("hello") is None


def test_extract_effect():
    fx = {
        "id": 200,
        "name": "Blur",
        "display_name": "Blur",
        "bypassed": {"id": 201, "value": True},
        "params": {
            "Amount": {"id": 202, "value": 0.5, "valuetype": "ParamRange"},
        },
    }
    result = _extract_effect(fx)
    assert result["name"] == "Blur"
    assert result["bypassed"] is True
    assert "Amount" in result["params"]
    assert result["params"]["Amount"]["value"] == 0.5
    # No IDs in output
    assert "id" not in result
    assert "id" not in result["params"]["Amount"]


def test_extract_layer_effects():
    data = extract_layer_effects(SAMPLE_STATE, 1)
    assert data["layer_name"] == "BG FX"
    assert data["layer_index"] == 1
    assert len(data["effects"]) == 2
    assert data["effects"][0]["name"] == "Transform"
    assert data["effects"][1]["name"] == "Blur"
    assert data["effects"][1]["bypassed"] is True
    assert "Amount" in data["effects"][1]["params"]


def test_extract_layer_effects_invalid_index():
    with pytest.raises(ValueError, match="out of range"):
        extract_layer_effects(SAMPLE_STATE, 5)


def test_extract_layer_settings():
    data = extract_layer_settings(SAMPLE_STATE, 1)
    assert data["layer_name"] == "BG FX"
    assert data["bypassed"] is False
    assert data["master"] == 1.0
    assert data["video_opacity"] == 0.8


def test_extract_layer_settings_invalid_index():
    with pytest.raises(ValueError, match="out of range"):
        extract_layer_settings(SAMPLE_STATE, 0)


# ---------------------------------------------------------------------------
# Restore tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_layer_effects(client):
    await client.connect()
    # Snapshot from layer 1, restore to layer 2
    snap = extract_layer_effects(client.state, 1)
    result = await restore_layer_effects(client, snap, 2)
    # Both Transform and Blur exist in both layers
    assert len(result["applied"]) == 2
    assert result["skipped"] == []
    # Check params were set
    for a in result["applied"]:
        assert a["params_set"] > 0


@pytest.mark.asyncio
async def test_restore_layer_effects_skips_missing(client):
    await client.connect()
    # Snapshot with an effect that doesn't exist in target
    snap = {
        "effects": [
            {"name": "NonExistent", "params": {"Foo": {"value": 1.0}}},
            {"name": "Blur", "params": {"Amount": {"value": 0.9}}},
        ]
    }
    result = await restore_layer_effects(client, snap, 1)
    assert "NonExistent" in result["skipped"]
    assert len(result["applied"]) == 1
    assert result["applied"][0]["effect"] == "Blur"


@pytest.mark.asyncio
async def test_restore_layer_effects_invalid_index(client):
    await client.connect()
    with pytest.raises(ValueError, match="out of range"):
        await restore_layer_effects(client, {"effects": []}, 99)


@pytest.mark.asyncio
async def test_restore_layer_settings(client):
    await client.connect()
    snap = extract_layer_settings(client.state, 1)
    result = await restore_layer_settings(client, snap, 2)
    assert result["params_set"] > 0


@pytest.mark.asyncio
async def test_restore_layer_settings_invalid_index(client):
    await client.connect()
    with pytest.raises(ValueError, match="out of range"):
        await restore_layer_settings(client, {}, 99)


# ---------------------------------------------------------------------------
# SnapshotStore tests
# ---------------------------------------------------------------------------

def test_store_save_and_load(store):
    data = {"layer_name": "Test", "effects": []}
    store.save("my-snap", "layer_effects", data)
    loaded = store.load("my-snap")
    assert loaded is not None
    assert loaded["name"] == "my-snap"
    assert loaded["type"] == "layer_effects"
    assert loaded["data"] == data


def test_store_load_not_found(store):
    assert store.load("nonexistent") is None


def test_store_list(store):
    store.save("snap-a", "layer_effects", {"layer_name": "L1", "layer_index": 1})
    store.save("snap-b", "layer_settings", {"layer_name": "L2", "layer_index": 2})
    items = store.list()
    assert len(items) == 2
    names = [i["name"] for i in items]
    assert "snap-a" in names
    assert "snap-b" in names


def test_store_list_empty(store):
    assert store.list() == []


def test_store_delete(store):
    store.save("to-delete", "layer_effects", {})
    assert store.delete("to-delete") is True
    assert store.load("to-delete") is None


def test_store_delete_not_found(store):
    assert store.delete("nonexistent") is False


def test_store_overwrite(store):
    store.save("snap", "layer_effects", {"version": 1})
    store.save("snap", "layer_effects", {"version": 2})
    loaded = store.load("snap")
    assert loaded["data"]["version"] == 2


# ---------------------------------------------------------------------------
# Clip effects tests
# ---------------------------------------------------------------------------

def test_extract_clip_effects():
    data = extract_clip_effects(SAMPLE_STATE, 1, 1)
    assert data["layer_name"] == "BG FX"
    assert data["clip_name"] == "Clip 1"
    assert data["clip_index"] == 1
    assert len(data["effects"]) == 2
    assert data["effects"][0]["name"] == "Transform"
    assert data["effects"][1]["name"] == "Glow"
    assert data["effects"][1]["params"]["Intensity"]["value"] == 0.6


def test_extract_clip_effects_invalid_layer():
    with pytest.raises(ValueError, match="out of range"):
        extract_clip_effects(SAMPLE_STATE, 99, 1)


def test_extract_clip_effects_invalid_clip():
    with pytest.raises(ValueError, match="out of range"):
        extract_clip_effects(SAMPLE_STATE, 1, 99)


@pytest.mark.asyncio
async def test_restore_clip_effects(client):
    await client.connect()
    snap = extract_clip_effects(client.state, 1, 1)
    # Restore clip 1 effects to clip 2 (only Transform matches)
    result = await restore_clip_effects(client, snap, 1, 2)
    assert len(result["applied"]) == 1
    assert result["applied"][0]["effect"] == "Transform"
    assert "Glow" in result["skipped"]


@pytest.mark.asyncio
async def test_restore_clip_effects_invalid_layer(client):
    await client.connect()
    with pytest.raises(ValueError, match="out of range"):
        await restore_clip_effects(client, {"effects": []}, 99, 1)


@pytest.mark.asyncio
async def test_restore_clip_effects_invalid_clip(client):
    await client.connect()
    with pytest.raises(ValueError, match="out of range"):
        await restore_clip_effects(client, {"effects": []}, 1, 99)


# ---------------------------------------------------------------------------
# Crossfader tests
# ---------------------------------------------------------------------------

def test_extract_crossfader():
    data = extract_crossfader(SAMPLE_STATE)
    assert data["phase"] == 0.5
    assert data["behaviour"] == 0
    assert data["curve"] == 1
    assert "Opacity" in data["mixer"]
    assert data["mixer"]["Opacity"]["value"] == 1.0


def test_extract_crossfader_empty():
    data = extract_crossfader({})
    assert data == {}


@pytest.mark.asyncio
async def test_restore_crossfader(client):
    await client.connect()
    snap = extract_crossfader(client.state)
    result = await restore_crossfader(client, snap)
    assert result["params_set"] >= 3  # phase, behaviour, curve + mixer


# ---------------------------------------------------------------------------
# Deck tests
# ---------------------------------------------------------------------------

def test_extract_deck():
    data = extract_deck(SAMPLE_STATE, 1)
    assert data["name"] == "Main Deck"
    assert data["deck_index"] == 1
    assert data["colorid"] == 3


def test_extract_deck_second():
    data = extract_deck(SAMPLE_STATE, 2)
    assert data["name"] == "FX Deck"
    assert data["colorid"] == 5


def test_extract_deck_invalid_index():
    with pytest.raises(ValueError, match="out of range"):
        extract_deck(SAMPLE_STATE, 99)


# ---------------------------------------------------------------------------
# Layer group tests
# ---------------------------------------------------------------------------

def test_extract_layer_group():
    data = extract_layer_group(SAMPLE_STATE, 1)
    assert data["name"] == "Group A"
    assert data["group_index"] == 1
    assert data["bypassed"] is False
    assert data["master"] == 0.8
    assert data["crossfadergroup"] == 1
    assert data["ignorecolumntrigger"] is True
    assert data["layer_names"] == ["BG FX", "FG FX"]


def test_extract_layer_group_invalid_index():
    with pytest.raises(ValueError, match="out of range"):
        extract_layer_group(SAMPLE_STATE, 99)


@pytest.mark.asyncio
async def test_restore_layer_group(client):
    await client.connect()
    snap = extract_layer_group(client.state, 1)
    result = await restore_layer_group(client, snap, 1)
    assert result["params_set"] >= 3  # bypassed, master, crossfadergroup, etc.
