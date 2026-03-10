"""Tests for the behaviors engine."""

import json
import os
import tempfile

import pytest
import pytest_asyncio

from resolume_mcp.behaviors import (
    Action,
    Behavior,
    BehaviorManager,
    Condition,
    check_condition,
    _behavior_from_dict,
    _behavior_to_dict,
)
from resolume_mcp.client import ResolumeAgentClient


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,val,cond_val,expected", [
    ("any", 42, None, True),
    ("any", None, None, True),
    ("truthy", True, None, True),
    ("truthy", False, None, False),
    ("truthy", 1, None, True),
    ("truthy", 0, None, False),
    ("falsy", False, None, True),
    ("falsy", True, None, False),
    ("eq", 5, 5, True),
    ("eq", 5, 6, False),
    ("eq", True, True, True),
    ("neq", 5, 6, True),
    ("neq", 5, 5, False),
    ("gt", 10, 5, True),
    ("gt", 5, 10, False),
    ("lt", 5, 10, True),
    ("lt", 10, 5, False),
    ("gte", 5, 5, True),
    ("gte", 4, 5, False),
    ("lte", 5, 5, True),
    ("lte", 6, 5, False),
])
def test_check_condition(op, val, cond_val, expected):
    assert check_condition(Condition(op=op, value=cond_val), val) == expected


def test_check_condition_unknown_op():
    assert check_condition(Condition(op="bogus"), 1) is False


def test_check_condition_type_error():
    """Comparing incompatible types returns False, not an exception."""
    assert check_condition(Condition(op="gt", value="hello"), 5) is False


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

def test_behavior_serialize_roundtrip():
    b = Behavior(
        id="abc123",
        name="test",
        trigger_param_id=42,
        condition=Condition(op="eq", value=True),
        action=Action(type="toggle_parameter", params={"path": "/parameter/by-id/99"}),
        enabled=True,
        description="a test behavior",
    )
    d = _behavior_to_dict(b)
    # Runtime fields excluded
    assert "fire_count" not in d
    assert "last_error" not in d
    assert "_callback" not in d
    # Round-trip
    b2 = _behavior_from_dict(d)
    assert b2.id == "abc123"
    assert b2.name == "test"
    assert b2.trigger_param_id == 42
    assert b2.condition.op == "eq"
    assert b2.condition.value is True
    assert b2.action.type == "toggle_parameter"
    assert b2.enabled is True


# ---------------------------------------------------------------------------
# BehaviorManager with dry_run client
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    c = ResolumeAgentClient(dry_run=True)
    return c


@pytest.fixture
def persist_path(tmp_path):
    return str(tmp_path / "behaviors.json")


@pytest_asyncio.fixture
async def manager(client, persist_path):
    await client.connect()
    mgr = BehaviorManager(client, persist_path)
    yield mgr
    await mgr.stop()


def _make_behavior(**kwargs) -> Behavior:
    defaults = dict(
        name="test",
        trigger_param_id=42,
        condition=Condition(op="truthy"),
        action=Action(type="toggle_parameter", params={"path": "/parameter/by-id/99"}),
    )
    defaults.update(kwargs)
    return Behavior(**defaults)


@pytest.mark.asyncio
async def test_add_and_list(manager):
    b = await manager.add(_make_behavior())
    assert b.id  # auto-generated
    items = manager.list()
    assert len(items) == 1
    assert items[0]["name"] == "test"


@pytest.mark.asyncio
async def test_add_persists(manager, persist_path):
    await manager.add(_make_behavior())
    assert os.path.exists(persist_path)
    with open(persist_path) as f:
        data = json.load(f)
    assert data["version"] == 1
    assert len(data["behaviors"]) == 1


@pytest.mark.asyncio
async def test_remove(manager):
    b = await manager.add(_make_behavior())
    assert await manager.remove(b.id) is True
    assert manager.list() == []


@pytest.mark.asyncio
async def test_remove_not_found(manager):
    assert await manager.remove("nonexistent") is False


@pytest.mark.asyncio
async def test_enable_disable(manager):
    b = await manager.add(_make_behavior())
    await manager.disable(b.id)
    assert manager.get(b.id).enabled is False
    await manager.enable(b.id)
    assert manager.get(b.id).enabled is True


@pytest.mark.asyncio
async def test_disable_not_found(manager):
    assert await manager.disable("nonexistent") is False


@pytest.mark.asyncio
async def test_start_loads_persisted(client, persist_path):
    """A new manager loads behaviors from the file on start."""
    await client.connect()
    mgr1 = BehaviorManager(client, persist_path)
    await mgr1.add(_make_behavior(name="persisted"))
    await mgr1.stop()

    mgr2 = BehaviorManager(client, persist_path)
    await mgr2.start()
    items = mgr2.list()
    assert len(items) == 1
    assert items[0]["name"] == "persisted"
    await mgr2.stop()


@pytest.mark.asyncio
async def test_start_no_file(client, persist_path):
    """Start with no file is fine — empty behaviors."""
    await client.connect()
    mgr = BehaviorManager(client, persist_path)
    await mgr.start()
    assert mgr.list() == []
    await mgr.stop()


@pytest.mark.asyncio
async def test_callback_registered_on_add(manager, client):
    """Adding an enabled behavior registers a monitor callback."""
    b = await manager.add(_make_behavior())
    assert b.trigger_param_id in client._parameter_callbacks
    assert len(client._parameter_callbacks[b.trigger_param_id]) == 1


@pytest.mark.asyncio
async def test_callback_removed_on_remove(manager, client):
    b = await manager.add(_make_behavior())
    await manager.remove(b.id)
    assert b.trigger_param_id not in client._parameter_callbacks


@pytest.mark.asyncio
async def test_callback_removed_on_disable(manager, client):
    b = await manager.add(_make_behavior())
    await manager.disable(b.id)
    # Callback should be removed
    assert b._callback is None


@pytest.mark.asyncio
async def test_disabled_behavior_not_activated(manager, client):
    b = _make_behavior(enabled=False)
    await manager.add(b)
    assert b._callback is None
    assert b.trigger_param_id not in client._parameter_callbacks


# ---------------------------------------------------------------------------
# find_param_value_by_id
# ---------------------------------------------------------------------------

def test_find_param_value_by_id(client, persist_path):
    import asyncio
    mgr = BehaviorManager(client, persist_path)
    client.state = {
        "layers": [
            {
                "video": {
                    "effects": {
                        "video": [
                            {"id": 99, "bypassed": {"id": 200, "value": False}},
                        ]
                    }
                }
            }
        ]
    }
    result = mgr._find_param_value_by_id(client.state, 200)
    assert result is False


def test_find_param_value_by_id_not_found(client, persist_path):
    mgr = BehaviorManager(client, persist_path)
    client.state = {}
    result = mgr._find_param_value_by_id(client.state, 999)
    assert result is None


# ---------------------------------------------------------------------------
# restore_snapshot action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_snapshot_action(client, persist_path, tmp_path):
    """A behavior with restore_snapshot action loads and applies a snapshot."""
    from resolume_mcp.snapshots import SnapshotStore

    await client.connect()
    client.state = {
        "layers": [
            {
                "id": 1,
                "name": {"value": "L1", "id": 10},
                "bypassed": {"value": False, "id": 11, "valuetype": "ParamBoolean"},
                "solo": {"value": False, "id": 12, "valuetype": "ParamBoolean"},
                "master": {"value": 1.0, "id": 13, "valuetype": "ParamRange"},
                "video": {
                    "opacity": {"value": 1.0, "id": 20, "valuetype": "ParamRange"},
                    "effects": [
                        {
                            "id": 100,
                            "name": "Blur",
                            "params": {
                                "Amount": {"id": 101, "value": 0.0, "valuetype": "ParamRange"},
                            },
                        },
                    ],
                },
                "clips": [],
            }
        ],
    }

    store = SnapshotStore(str(tmp_path / "snaps"))
    store.save("my-fx", "layer_effects", {
        "layer_index": 1,
        "effects": [
            {"name": "Blur", "params": {"Amount": {"value": 0.8, "valuetype": "ParamRange"}}},
        ],
    })

    mgr = BehaviorManager(client, persist_path, snapshot_store=store)
    b = Behavior(
        name="restore-on-trigger",
        trigger_param_id=11,
        condition=Condition(op="any"),
        action=Action(type="restore_snapshot", params={"name": "my-fx", "layer_index": 1}),
    )
    await mgr.add(b)
    # Manually execute the action
    await mgr._execute_action(b, True)
    # No exception means success (dry_run client logs the SET calls)
    await mgr.stop()


@pytest.mark.asyncio
async def test_restore_snapshot_no_store(client, persist_path):
    """restore_snapshot without a snapshot store raises ValueError."""
    await client.connect()
    mgr = BehaviorManager(client, persist_path, snapshot_store=None)
    b = Behavior(
        name="no-store",
        trigger_param_id=11,
        condition=Condition(op="any"),
        action=Action(type="restore_snapshot", params={"name": "test"}),
    )
    await mgr.add(b)
    with pytest.raises(ValueError, match="No snapshot store"):
        await mgr._execute_action(b, True)
    await mgr.stop()


@pytest.mark.asyncio
async def test_restore_snapshot_not_found(client, persist_path, tmp_path):
    """restore_snapshot with a missing snapshot raises ValueError."""
    from resolume_mcp.snapshots import SnapshotStore

    await client.connect()
    store = SnapshotStore(str(tmp_path / "snaps"))
    mgr = BehaviorManager(client, persist_path, snapshot_store=store)
    b = Behavior(
        name="missing-snap",
        trigger_param_id=11,
        condition=Condition(op="any"),
        action=Action(type="restore_snapshot", params={"name": "nonexistent"}),
    )
    await mgr.add(b)
    with pytest.raises(ValueError, match="not found"):
        await mgr._execute_action(b, True)
    await mgr.stop()
