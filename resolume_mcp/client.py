import asyncio
import json
import logging
import random
import urllib.request
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

from resolume_mcp.config import DEFAULT_TIMEOUT, get_ws_uri

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ResolumeAgent")


class ResolumeAgentClient:
    def __init__(
        self,
        host="localhost",
        port=8080,
        dry_run=False,
        max_reconnect_attempts=10,
    ):
        self._host = host
        self._port = port
        self.uri = get_ws_uri(host, port)
        self.ws = None
        self.state = {}
        self._connected = False
        self.dry_run = dry_run

        self._state_ready = asyncio.Event()  # set when first composition arrives
        self._pending_ack: dict[str, asyncio.Future] = {}  # per-path awaitable ACKs
        self._listen_task: asyncio.Task | None = None       # held for disconnect() cancellation
        self._reconnect_task: asyncio.Task | None = None    # held for disconnect() cancellation
        self._subscriptions: set[str] = set()               # re-subscribed after reconnect
        self._max_reconnect_attempts = max_reconnect_attempts

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self, timeout=DEFAULT_TIMEOUT) -> bool:
        if self.dry_run:
            logger.info(f"[dry-run] Would connect to {self.uri}")
            self._connected = True
            self._state_ready.set()
            return True

        self._state_ready.clear()

        try:
            await self._do_connect()
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False

        # Block until _listen() receives the first composition message.
        # Resolume sends it immediately on connect, so this rarely waits long.
        try:
            await asyncio.wait_for(self._state_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for initial Resolume state")
            return False

        # Re-subscribe any paths from a previous session (reconnect path).
        for path in self._subscriptions:
            await self.send_command("subscribe", path)

        self._connected = True
        return True

    async def _do_connect(self):
        """Open the WebSocket and start the listener task."""
        self.ws = await websockets.connect(self.uri)
        logger.info(f"Connected to Resolume at {self.uri}")
        self._listen_task = asyncio.create_task(self._listen())

    async def disconnect(self) -> None:
        self._connected = False  # suppresses reconnect trigger in _listen()
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self._listen_task:
            self._listen_task.cancel()
        for fut in self._pending_ack.values():
            fut.cancel()
        self._pending_ack.clear()
        if self.ws:
            await self.ws.close()
            self.ws = None
        self._state_ready.clear()
        logger.info("Disconnected from Resolume")

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------

    async def _listen(self):
        """Receive and process all messages from Resolume."""
        try:
            async for raw in self.ws:
                data = json.loads(raw)

                if "decks" in data or "layers" in data:
                    # Full state replacement — Resolume sends the composition
                    # as the bare root object (no "composition" wrapper key)
                    self.state = data
                    self._state_ready.set()
                    logger.debug("Full state received from Resolume")
                    # Resolve any pending ACKs whose path now exists
                    self._resolve_acks_from_state()

                elif "path" in data and "value" in data:
                    # Incremental update — walk the state tree and patch the leaf
                    path: str = data["path"]
                    value = data["value"]
                    self._apply_incremental_update(path, value)
                    # Resolve ACK for this exact path if one is waiting
                    if path in self._pending_ack:
                        fut = self._pending_ack.pop(path)
                        if not fut.done():
                            fut.set_result(value)

        except ConnectionClosed:
            logger.warning("Resolume WebSocket connection closed")
            self._connected = False
            # Cancel all in-flight ACK futures so callers get an error, not a hang
            for fut in self._pending_ack.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket connection closed"))
            self._pending_ack.clear()
            # Always reconnect after unexpected close; disconnect() cancels
            # _listen_task first so this branch is never reached in that case.
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _apply_incremental_update(self, path: str, value):
        """Walk self.state to the parameter dict and patch its 'value' key.

        Resolume's incremental updates have two quirks:
        1. Paths start with /composition/ but self.state IS the composition (no
           wrapper key), so the leading segment must be stripped before walking.
        2. `value` is the new scalar for the parameter's "value" field — not a
           replacement for the whole parameter dict (which also holds id, min,
           max, etc.). So we walk the full path and patch .value on the target.
        """
        segs = [s for s in path.strip("/").split("/") if s]
        if segs and segs[0] == "composition":
            segs = segs[1:]
        if not segs:
            return
        node = self.state
        try:
            for seg in segs:
                try:
                    node = node[int(seg)]
                except (ValueError, TypeError):
                    node = node[seg]
            if isinstance(node, dict):
                node["value"] = value
        except (KeyError, IndexError, TypeError):
            logger.debug(f"Ignoring incremental update for unknown path: {path}")

    def _resolve_acks_from_state(self):
        """After a full state replacement, resolve any pending ACKs."""
        resolved = []
        for path, fut in self._pending_ack.items():
            if not fut.done():
                fut.set_result(None)
            resolved.append(path)
        for path in resolved:
            del self._pending_ack[path]

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    async def _reconnect_loop(self):
        """Exponential backoff reconnect with jitter."""
        for attempt in range(self._max_reconnect_attempts):
            delay = min(2 ** attempt, 60) + random.uniform(0, 1)
            logger.info(f"Reconnecting in {delay:.1f}s (attempt {attempt + 1}/{self._max_reconnect_attempts})")
            await asyncio.sleep(delay)
            try:
                self._state_ready.clear()
                await self._do_connect()
                await asyncio.wait_for(self._state_ready.wait(), timeout=DEFAULT_TIMEOUT)
                for path in self._subscriptions:
                    await self.send_command("subscribe", path)
                self._connected = True
                logger.info("Reconnected to Resolume")
                return
            except Exception as e:
                logger.warning(f"Reconnect attempt {attempt + 1} failed: {e}")

        logger.critical(f"Failed to reconnect after {self._max_reconnect_attempts} attempts")

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    async def send_command(self, action: str, path: str, body_or_value=None):
        """Send a raw WebSocket command to Resolume.

        Resolume's WebSocket API uses two distinct payload shapes:
          - post / remove  → {"action": ..., "path": ..., "body": ...}
          - get / set / subscribe / unsubscribe / trigger
                           → {"action": ..., "parameter": ..., "value": ...}
        """
        if self.dry_run:
            label = "body" if action in ("post", "remove") else "value"
            logger.info(
                f"[dry-run] {action.upper()} {path}"
                + (f" [{label}={body_or_value!r}]" if body_or_value is not None else "")
            )
            return

        if not self._connected or self.ws is None:
            logger.error("Not connected to Resolume")
            return

        if action in ("post", "remove"):
            payload: dict = {"action": action, "path": path}
            if body_or_value is not None:
                payload["body"] = body_or_value
        else:  # get, set, subscribe, unsubscribe, trigger
            payload = {"action": action, "parameter": path}
            if body_or_value is not None:
                payload["value"] = body_or_value

        await self.ws.send(json.dumps(payload))
        logger.debug(f"Sent: {payload}")

    async def send_and_wait(
        self,
        action: str,
        path: str,
        body_or_value=None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Send a command and wait for Resolume to echo back a state update for
        the given path, confirming it was processed.

        Limitation: only one in-flight send_and_wait() per path at a time.
        Sequential deck operations are fine; parallel calls on the same path
        are not supported.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_ack[path] = fut
        try:
            await self.send_command(action, path, body_or_value)
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_ack.pop(path, None)
            raise
        return fut.result()

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def _resolve_path_to_id(self, path: str) -> int | None:
        """Walk self.state along path and return the parameter's numeric 'id' field.

        Used to convert human-readable paths like /composition/tempocontroller/tempo
        into the /parameter/by-id/{id} format that Resolume's WS subscribe requires.
        """
        segs = [s for s in path.strip("/").split("/") if s]
        if segs and segs[0] == "composition":
            segs = segs[1:]
        node = self.state
        try:
            for seg in segs:
                try:
                    node = node[int(seg)]
                except (ValueError, TypeError):
                    node = node[seg]
            return node.get("id") if isinstance(node, dict) else None
        except (KeyError, IndexError, TypeError):
            return None

    async def subscribe(self, path: str) -> None:
        """Subscribe to a parameter path. Resolves to by-id format for Resolume's WS API."""
        param_id = self._resolve_path_to_id(path)
        ws_path = f"/parameter/by-id/{param_id}" if param_id else path
        self._subscriptions.add(ws_path)
        await self.send_command("subscribe", ws_path)

    async def unsubscribe(self, path: str) -> None:
        """Unsubscribe from a parameter path. Resolves to by-id format."""
        param_id = self._resolve_path_to_id(path)
        ws_path = f"/parameter/by-id/{param_id}" if param_id else path
        self._subscriptions.discard(ws_path)
        await self.send_command("unsubscribe", ws_path)

    # ------------------------------------------------------------------
    # High-level agent operations
    # ------------------------------------------------------------------

    async def bootstrap_deck(self, name: str, clips_paths: list[str], grid_width: int = 10):
        """
        Creates a new deck in Resolume, renames it, and populates it with clips.

        Uses send_and_wait() to confirm the deck was created before proceeding,
        then loads clips concurrently (up to 5 at a time) to avoid flooding the
        WebSocket while still being fast.
        """
        logger.info(f"Bootstrapping deck '{name}' with {len(clips_paths)} clips...")

        # Snapshot existing deck IDs so we can diff after the add
        deck_ids_before: set = {d.get("id") for d in self.state.get("decks", [])}

        # Add the deck and wait for Resolume to confirm state update
        await self.send_and_wait("post", "/composition/decks/add")

        # Identify the newly added deck by diffing state
        decks_after = self.state.get("decks", [])
        new_deck = next(
            (d for d in decks_after if d.get("id") not in deck_ids_before),
            None,
        )
        if new_deck is None:
            # Fall back to last deck in list if diff fails (e.g. dry_run)
            new_deck = decks_after[-1] if decks_after else {}

        # Resolume deck indices are 1-based positions in the list
        deck_index = decks_after.index(new_deck) + 1 if new_deck in decks_after else 1

        # Rename the deck
        await self.send_and_wait(
            "set",
            f"/composition/decks/{deck_index}/name",
            body_or_value=name,
        )

        # Load clips concurrently, max 5 in-flight at a time
        sem = asyncio.Semaphore(5)

        async def load_clip(i: int, path: str):
            async with sem:
                col = (i % grid_width) + 1
                layer = (i // grid_width) + 1
                # Resolume /open expects a file:// URL, not a bare POSIX path
                file_url = "file://" + quote(str(path), safe="/:")
                await self.send_command(
                    "post",
                    f"/composition/layers/{layer}/clips/{col}/open",
                    file_url,
                )

        await asyncio.gather(*[load_clip(i, p) for i, p in enumerate(clips_paths)])
        logger.info(f"Deck '{name}' bootstrapped at index {deck_index}")

    async def add_video_effect(self, layer_index: int, effect_id: str):
        """Adds a video effect to a layer."""
        path = f"/composition/layers/{layer_index}/effects/video/add"
        await self.send_command("post", path, effect_id)

    async def set_parameter(self, path: str, value):
        """Sets any Resolume parameter by WebSocket path."""
        await self.send_command("set", path, value)

    # ------------------------------------------------------------------
    # REST helper (one-off reads that don't need a live subscription)
    # ------------------------------------------------------------------

    async def rest_get(self, api_path: str) -> dict:
        """HTTP GET against the Resolume REST API (no WebSocket needed)."""
        url = f"http://{self._host}:{self._port}/api/v1{api_path}"
        loop = asyncio.get_event_loop()

        def _fetch():
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.loads(r.read())

        return await loop.run_in_executor(None, _fetch)

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def connect_clip(self, layer_index: int, clip_index: int):
        """Trigger a clip to play (equivalent to pressing a clip cell)."""
        await self.send_command(
            "trigger",
            f"/composition/layers/{layer_index}/clips/{clip_index}/connect",
        )

    async def connect_column(self, column_index: int):
        """Fire an entire column across all layers simultaneously."""
        await self.send_command(
            "post",
            f"/composition/columns/{column_index}/connect",
        )

    async def disconnect_all(self):
        """Stop all playing clips (blackout)."""
        await self.send_command("post", "/composition/disconnect-all")

    # ------------------------------------------------------------------
    # Layer controls
    # ------------------------------------------------------------------

    async def set_layer_opacity(self, layer_index: int, opacity: float):
        """Set layer opacity (0.0 = invisible, 1.0 = full)."""
        await self.send_command(
            "set",
            f"/composition/layers/{layer_index}/video/opacity",
            opacity,
        )

    async def set_layer_bypass(self, layer_index: int, bypassed: bool):
        """Mute or unmute a layer (bypassed = invisible output)."""
        await self.send_command(
            "set",
            f"/composition/layers/{layer_index}/bypassed",
            bypassed,
        )

    # ------------------------------------------------------------------
    # Global tempo / crossfader
    # ------------------------------------------------------------------

    def get_bpm(self) -> dict:
        """Return the cached tempocontroller state dict (synchronous)."""
        return self.state.get("tempocontroller", {})

    async def set_bpm(self, bpm: float):
        """Change the global tempo. Resolves parameter ID from cached state."""
        tc = self.state.get("tempocontroller", {})
        param_id = tc.get("tempo", {}).get("id")
        if param_id:
            await self.send_command("set", f"/parameter/by-id/{param_id}", bpm)
        else:
            # Fallback: path-based set (works if Resolume exposes the path)
            await self.send_command("set", "/composition/tempocontroller/tempo", bpm)

    async def set_crossfader(self, position: float):
        """Move the A/B crossfader (0.0 = full A, 1.0 = full B)."""
        cf = self.state.get("crossfader", {})
        param_id = cf.get("phase", {}).get("id")
        if param_id:
            await self.send_command("set", f"/parameter/by-id/{param_id}", position)
        else:
            await self.send_command("set", "/composition/crossfader/phase", position)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def list_effects(self) -> dict:
        """Return all available video/audio effects from the REST API."""
        return await self.rest_get("/effects")

    async def list_sources(self) -> dict:
        """Return all available sources (clips, generators, live inputs)."""
        return await self.rest_get("/sources")


if __name__ == "__main__":
    async def main():
        agent = ResolumeAgentClient()
        if await agent.connect():
            print("Connected. Decks:", len(agent.state.get("decks", [])))
            await agent.disconnect()

    asyncio.run(main())
