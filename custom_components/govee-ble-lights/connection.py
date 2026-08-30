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

CONNECT_ATTEMPTS = 3
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
        hold_seconds: float = 120.0,
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

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "is_connected", False))

    async def send(self, frames: list[bytes]) -> None:
        """Write control frames, connecting or reconnecting as needed."""
        async with self._lock:
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
        """Release the connection and stop the keep-alive task."""
        task = self._maintain_task
        self._maintain_task = None
        await self._drop_client()
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _ensure_connected(self) -> Any:
        if self.connected:
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
            self._maintain_task = asyncio.get_event_loop().create_task(self._maintain())
        return self._client

    def _touch(self) -> None:
        self._last_activity = asyncio.get_event_loop().time()

    def _handle_disconnect(self, _client: Any) -> None:
        self._client = None

    async def _maintain(self) -> None:
        """Keep the held connection warm; release it after a quiet period."""
        try:
            while self.connected:
                await asyncio.sleep(self._keepalive_interval)
                if not self.connected:
                    return
                idle = asyncio.get_event_loop().time() - self._last_activity
                if idle >= self._hold_seconds:
                    _LOGGER.debug("%s: idle %.0fs, releasing connection", self._name, idle)
                    await self.disconnect()
                    return
                async with self._lock:
                    if not self.connected:
                        return
                    try:
                        await self._client.write_gatt_char(
                            UUID_CONTROL_CHARACTERISTIC, KEEPALIVE_FRAME, False
                        )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("%s: keep-alive failed: %s", self._name, err)
                        await self._drop_client()
                        return
        except asyncio.CancelledError:
            pass

    async def _drop_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
