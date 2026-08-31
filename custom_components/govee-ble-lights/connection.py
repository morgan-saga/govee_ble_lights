"""Held BLE connection with keep-alive for one Govee device.

Govee firmware drops clients idle for ~10 s, and connecting costs 1-4 s, so
per-command connects make dimming unusable. This holds the link, keeps it
warm with 0xAA frames, and releases it after a quiet period so a multi-light
estate doesn't exhaust the adapter's concurrent-connection budget.

The `connector` is injected (production: bleak_retry_connector via light.py)
so this module stays importable and testable without bleak installed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import KEEPALIVE_FRAME, UUID_CONTROL_CHARACTERISTIC

_LOGGER = logging.getLogger(__name__)

# bleak_retry_connector.establish_connection retries internally (up to 4
# attempts, 20-60 s each); wrapping it in another retry loop turned an
# unreachable lamp into a multi-minute service-call hang.
CONNECT_ATTEMPTS = 1
SEND_ATTEMPTS = 2

Connector = Callable[[Any, Callable[[Any], None]], Awaitable[Any]]


class GoveeConnectionError(Exception):
    """Connecting to or writing to the device failed after retries."""


class GoveeConnection:
    """Owns the BLE client lifecycle for a single device."""

    def __init__(
        self,
        get_ble_device: Callable[[], Any],
        name: str,
        connector: Connector,
        keepalive_interval: float = 4.0,
        hold_seconds: float = 600.0,
    ) -> None:
        self._get_ble_device = get_ble_device
        self._name = name
        self._connector = connector
        self._keepalive_interval = keepalive_interval
        self._hold_seconds = hold_seconds
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._maintain_task: asyncio.Task | None = None
        self._last_activity = 0.0
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "is_connected", False))

    async def send(self, frames: list[bytes]) -> None:
        """Write control frames, connecting or reconnecting as needed."""
        async with self._lock:
            if self._closed:
                raise GoveeConnectionError(f"{self._name}: connection closed")
            last_error: Exception | None = None
            for attempt in range(SEND_ATTEMPTS):
                try:
                    client = await self._ensure_connected()
                    for frame in frames:
                        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, frame, False)
                    self._touch()
                    return
                except GoveeConnectionError:
                    raise
                except Exception as err:  # noqa: BLE001 - any BLE failure triggers one reconnect
                    last_error = err
                    _LOGGER.debug("%s: write failed (attempt %d): %s", self._name, attempt + 1, err)
                    await self._drop_client()
            raise GoveeConnectionError(f"{self._name}: send failed after reconnect") from last_error

    async def disconnect(self) -> None:
        """Release the connection permanently and stop the keep-alive task."""
        self._closed = True
        task = self._maintain_task
        self._maintain_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        async with self._lock:
            await self._drop_client()

    async def _ensure_connected(self) -> Any:
        if self._closed:
            raise GoveeConnectionError(f"{self._name}: connection closed")
        if self.connected:
            self._touch()
            return self._client
        last_error: Exception | None = None
        for _ in range(CONNECT_ATTEMPTS):
            try:
                self._client = await self._connector(self._get_ble_device(), self._handle_disconnect)
                break
            except Exception as err:  # noqa: BLE001
                last_error = err
        else:
            raise GoveeConnectionError(
                f"{self._name}: connect failed after {CONNECT_ATTEMPTS} attempts"
            ) from last_error
        self._touch()
        if self._maintain_task is None or self._maintain_task.done():
            self._maintain_task = asyncio.get_running_loop().create_task(self._maintain())
        return self._client

    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()

    def _handle_disconnect(self, client: Any) -> None:
        # A late callback from a superseded client must not clobber the
        # freshly established connection.
        if client is self._client:
            self._client = None

    async def _maintain(self) -> None:
        """Keep the held connection warm; release it after a quiet period."""
        try:
            while self.connected and not self._closed:
                await asyncio.sleep(self._keepalive_interval)
                async with self._lock:
                    if self._closed or not self.connected:
                        return
                    idle = asyncio.get_running_loop().time() - self._last_activity
                    if idle >= self._hold_seconds:
                        _LOGGER.debug("%s: idle %.0fs, releasing connection", self._name, idle)
                        await self._drop_client()
                        return
                    if idle < self._keepalive_interval:
                        # Real traffic just reset the firmware timer; skip.
                        continue
                    try:
                        # response=True so a zombie link (device gone, BlueZ
                        # not yet aware) fails here instead of acking locally.
                        await self._client.write_gatt_char(
                            UUID_CONTROL_CHARACTERISTIC, KEEPALIVE_FRAME, True
                        )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("%s: keep-alive failed: %s", self._name, err)
                        await self._drop_client()
                        return
        except asyncio.CancelledError:
            raise

    async def _drop_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
