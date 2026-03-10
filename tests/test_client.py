"""Tests for ResolumeAgentClient (dry_run mode + unit tests)."""

import asyncio
import pytest
from resolume_mcp.client import ResolumeAgentClient


@pytest.fixture
def client():
    c = ResolumeAgentClient(dry_run=True)
    return c


@pytest.mark.asyncio
async def test_dry_run_connect(client):
    result = await client.connect()
    assert result is True
    assert client._connected is True


@pytest.mark.asyncio
async def test_dry_run_disconnect(client):
    await client.connect()
    await client.disconnect()
    assert client._connected is False


@pytest.mark.asyncio
async def test_dry_run_send_command_set(client):
    await client.connect()
    # Should not raise — just logs
    await client.send_command("set", "/composition/layers/1/video/opacity", 0.5)


@pytest.mark.asyncio
async def test_dry_run_send_command_post(client):
    await client.connect()
    await client.send_command("post", "/composition/decks/add")


@pytest.mark.asyncio
async def test_send_command_not_connected():
    """send_command when not connected should not raise but log error."""
    c = ResolumeAgentClient(dry_run=False)
    # Not connected, ws is None — should bail gracefully
    await c.send_command("set", "/some/path", 1.0)


@pytest.mark.asyncio
async def test_dry_run_connect_clip(client):
    await client.connect()
    await client.connect_clip(layer_index=1, clip_index=3)


@pytest.mark.asyncio
async def test_dry_run_set_layer_opacity(client):
    await client.connect()
    await client.set_layer_opacity(layer_index=1, opacity=0.75)


@pytest.mark.asyncio
async def test_dry_run_set_bpm_fallback(client):
    """With empty state, set_bpm uses path-based fallback."""
    await client.connect()
    await client.set_bpm(128)


@pytest.mark.asyncio
async def test_set_bpm_with_id(client):
    """With state containing tempo id, set_bpm uses by-id format."""
    await client.connect()
    client.state = {"tempocontroller": {"tempo": {"id": 42, "value": 120}}}
    await client.set_bpm(128)


@pytest.mark.asyncio
async def test_set_crossfader_fallback(client):
    await client.connect()
    await client.set_crossfader(0.5)


@pytest.mark.asyncio
async def test_set_crossfader_with_id(client):
    await client.connect()
    client.state = {"crossfader": {"phase": {"id": 99, "value": 0.0}}}
    await client.set_crossfader(0.7)


@pytest.mark.asyncio
async def test_get_bpm_empty_state(client):
    assert client.get_bpm() == {}


@pytest.mark.asyncio
async def test_get_bpm_with_state(client):
    client.state = {"tempocontroller": {"tempo": {"id": 1, "value": 120}}}
    result = client.get_bpm()
    assert result["tempo"]["value"] == 120


# --- Incremental update ---

def test_apply_incremental_update():
    c = ResolumeAgentClient(dry_run=True)
    c.state = {
        "layers": [
            {"name": {"value": "Layer 1"}, "video": {"opacity": {"value": 1.0}}}
        ]
    }
    c._apply_incremental_update("/composition/layers/0/video/opacity", 0.5)
    assert c.state["layers"][0]["video"]["opacity"]["value"] == 0.5


def test_apply_incremental_update_strips_composition():
    c = ResolumeAgentClient(dry_run=True)
    c.state = {"tempocontroller": {"tempo": {"value": 120, "id": 1}}}
    c._apply_incremental_update("/composition/tempocontroller/tempo", 128)
    assert c.state["tempocontroller"]["tempo"]["value"] == 128


def test_apply_incremental_update_unknown_path():
    """Unknown paths should be silently ignored."""
    c = ResolumeAgentClient(dry_run=True)
    c.state = {}
    c._apply_incremental_update("/composition/nonexistent/path", 42)
    # No error raised


# --- Path resolution ---

def test_resolve_path_to_id():
    c = ResolumeAgentClient(dry_run=True)
    c.state = {"tempocontroller": {"tempo": {"id": 42, "value": 120}}}
    assert c._resolve_path_to_id("/composition/tempocontroller/tempo") == 42


def test_resolve_path_to_id_missing():
    c = ResolumeAgentClient(dry_run=True)
    c.state = {}
    assert c._resolve_path_to_id("/composition/nonexistent") is None


# --- add_video_effect ---

@pytest.mark.asyncio
async def test_add_video_effect(client):
    await client.connect()
    client.state = {"layers": [{"id": 10, "name": {"value": "L1"}}]}
    await client.add_video_effect(layer_index=1, effect_name="Blur")


@pytest.mark.asyncio
async def test_add_video_effect_with_preset(client):
    await client.connect()
    client.state = {"layers": [{"id": 10, "name": {"value": "L1"}}]}
    await client.add_video_effect(layer_index=1, effect_name="Strobe", preset="Solid")


@pytest.mark.asyncio
async def test_add_video_effect_invalid_layer(client):
    await client.connect()
    client.state = {"layers": [{"id": 10}]}
    with pytest.raises(ValueError, match="out of range"):
        await client.add_video_effect(layer_index=5, effect_name="Blur")


# --- Subscribe / Unsubscribe ---

@pytest.mark.asyncio
async def test_subscribe_with_id(client):
    await client.connect()
    client.state = {"tempocontroller": {"tempo": {"id": 42, "value": 120}}}
    await client.subscribe("/composition/tempocontroller/tempo")
    assert "/parameter/by-id/42" in client._subscriptions


@pytest.mark.asyncio
async def test_unsubscribe(client):
    await client.connect()
    client.state = {"tempocontroller": {"tempo": {"id": 42, "value": 120}}}
    await client.subscribe("/composition/tempocontroller/tempo")
    await client.unsubscribe("/composition/tempocontroller/tempo")
    assert "/parameter/by-id/42" not in client._subscriptions


# --- Parameter monitoring ---

@pytest.mark.asyncio
async def test_monitor_parameter(client):
    """monitor_parameter subscribes and registers callback."""
    await client.connect()
    received = []
    await client.monitor_parameter(42, lambda data: received.append(data))
    assert "/parameter/by-id/42" in client._subscriptions
    assert 42 in client._parameter_callbacks
    assert len(client._parameter_callbacks[42]) == 1


@pytest.mark.asyncio
async def test_monitor_parameter_multiple_callbacks(client):
    """Multiple monitors on same ID reuse one subscription."""
    await client.connect()
    cb1 = lambda data: None
    cb2 = lambda data: None
    await client.monitor_parameter(42, cb1)
    await client.monitor_parameter(42, cb2)
    assert len(client._parameter_callbacks[42]) == 2


@pytest.mark.asyncio
async def test_unmonitor_parameter(client):
    """Unmonitor removes callback; last one triggers unsubscribe."""
    await client.connect()
    cb = lambda data: None
    await client.monitor_parameter(42, cb)
    await client.unmonitor_parameter(42, cb)
    assert 42 not in client._parameter_callbacks
    assert "/parameter/by-id/42" not in client._subscriptions


@pytest.mark.asyncio
async def test_unmonitor_parameter_partial(client):
    """Removing one of two callbacks keeps subscription alive."""
    await client.connect()
    cb1 = lambda data: None
    cb2 = lambda data: None
    await client.monitor_parameter(42, cb1)
    await client.monitor_parameter(42, cb2)
    await client.unmonitor_parameter(42, cb1)
    assert 42 in client._parameter_callbacks
    assert "/parameter/by-id/42" in client._subscriptions


# --- Reset parameter ---

@pytest.mark.asyncio
async def test_reset_parameter(client):
    await client.connect()
    await client.reset_parameter(42)


# --- Thumbnail update ---

def test_apply_thumbnail_update():
    c = ResolumeAgentClient(dry_run=True)
    c.state = {
        "layers": [
            {"clips": [{"id": 100, "thumbnail": {"id": 100, "data": "old"}}]}
        ]
    }
    c._apply_thumbnail_update({"id": 100, "data": "new"})
    assert c.state["layers"][0]["clips"][0]["thumbnail"]["data"] == "new"


def test_apply_thumbnail_update_no_match():
    """Thumbnail for unknown clip ID is silently ignored."""
    c = ResolumeAgentClient(dry_run=True)
    c.state = {"layers": [{"clips": [{"id": 100}]}]}
    c._apply_thumbnail_update({"id": 999, "data": "x"})


# --- Sources/effects caches ---

def test_sources_effects_init():
    c = ResolumeAgentClient(dry_run=True)
    assert c.sources == {}
    assert c.effects == {}


# --- Disconnect clears callbacks ---

@pytest.mark.asyncio
async def test_disconnect_clears_parameter_callbacks(client):
    await client.connect()
    await client.monitor_parameter(42, lambda d: None)
    await client.disconnect()
    assert client._parameter_callbacks == {}
