"""GoveeConnection: held connection, keep-alive, idle release, retry.

Behavior anchored to the live H601C findings (2026-08-30): the device kills
idle BLE clients after ~10 s, so the connection must stay warm with 0xAA
keep-alives between commands, and per-command reconnects are what made the
upstream integration unusably slow.
"""
import asyncio

import pytest

from gbl.connection import GoveeConnection, GoveeConnectionError
from gbl.protocol import KEEPALIVE_FRAME, UUID_CONTROL_CHARACTERISTIC

POWER_ON = bytes([0x33, 0x01, 0x01] + [0x00] * 16 + [0x33])
POWER_OFF = bytes([0x33, 0x01, 0x00] + [0x00] * 16 + [0x32])


class FakeClient:
    def __init__(self, fail_writes: int = 0):
        self.writes: list[tuple[str, bytes]] = []
        self.fail_writes = fail_writes
        self.is_connected = True
        self.disconnected_callback = None

    async def write_gatt_char(self, uuid, data, response=False):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise RuntimeError("simulated write failure")
        self.writes.append((uuid, bytes(data)))

    async def disconnect(self):
        self.is_connected = False

    def drop_from_remote(self):
        """Simulate the device closing the link (e.g. idle timeout)."""
        self.is_connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)

    def control_writes(self) -> list[bytes]:
        return [d for (u, d) in self.writes if d != KEEPALIVE_FRAME]

    def keepalive_writes(self) -> list[bytes]:
        return [d for (u, d) in self.writes if d == KEEPALIVE_FRAME]


def make_connector(clients: list, fail_connects: int = 0):
    async def connector(ble_device, disconnected_callback):
        connector.calls += 1
        if connector.fail_connects > 0:
            connector.fail_connects -= 1
            raise RuntimeError("simulated connect failure")
        client = clients.pop(0)
        client.disconnected_callback = disconnected_callback
        return client

    connector.calls = 0
    connector.fail_connects = fail_connects
    return connector


def make_connection(connector, **kwargs) -> GoveeConnection:
    defaults = {"keepalive_interval": 10.0, "hold_seconds": 60.0}
    defaults.update(kwargs)
    return GoveeConnection(
        get_ble_device=lambda: object(),
        name="test-light",
        connector=connector,
        **defaults,
    )


async def test_send_connects_and_writes_frames_in_order():
    client = FakeClient()
    connector = make_connector([client])
    conn = make_connection(connector)

    await conn.send([POWER_ON, POWER_OFF])

    assert connector.calls == 1
    assert client.control_writes() == [POWER_ON, POWER_OFF]
    assert all(u == UUID_CONTROL_CHARACTERISTIC for (u, _) in client.writes)
    await conn.disconnect()


async def test_second_send_reuses_held_connection():
    client = FakeClient()
    connector = make_connector([client])
    conn = make_connection(connector)

    await conn.send([POWER_ON])
    await conn.send([POWER_OFF])

    assert connector.calls == 1
    assert client.control_writes() == [POWER_ON, POWER_OFF]
    await conn.disconnect()


async def test_write_failure_reconnects_and_retries():
    failing = FakeClient(fail_writes=1)
    fresh = FakeClient()
    connector = make_connector([failing, fresh])
    conn = make_connection(connector)

    await conn.send([POWER_ON])

    assert connector.calls == 2
    assert fresh.control_writes() == [POWER_ON]
    await conn.disconnect()


async def test_connect_failure_raises_instead_of_returning_none():
    connector = make_connector([], fail_connects=99)
    conn = make_connection(connector)

    with pytest.raises(GoveeConnectionError):
        await conn.send([POWER_ON])


async def test_keepalives_flow_while_connection_held():
    client = FakeClient()
    connector = make_connector([client])
    conn = make_connection(connector, keepalive_interval=0.05, hold_seconds=60.0)

    await conn.send([POWER_ON])
    await asyncio.sleep(0.24)

    assert len(client.keepalive_writes()) >= 2
    await conn.disconnect()


async def test_idle_connection_released_after_hold_period():
    client = FakeClient()
    connector = make_connector([client])
    conn = make_connection(connector, keepalive_interval=0.05, hold_seconds=0.12)

    await conn.send([POWER_ON])
    await asyncio.sleep(0.3)

    assert conn.connected is False
    assert client.is_connected is False


async def test_remote_disconnect_then_next_send_reconnects():
    first = FakeClient()
    second = FakeClient()
    connector = make_connector([first, second])
    conn = make_connection(connector)

    await conn.send([POWER_ON])
    first.drop_from_remote()
    assert conn.connected is False

    await conn.send([POWER_OFF])

    assert connector.calls == 2
    assert second.control_writes() == [POWER_OFF]
    await conn.disconnect()


async def test_send_after_disconnect_raises_closed():
    client = FakeClient()
    connector = make_connector([client, FakeClient()])
    conn = make_connection(connector)
    await conn.send([POWER_ON])
    await conn.disconnect()
    with pytest.raises(GoveeConnectionError):
        await conn.send([POWER_OFF])
    assert connector.calls == 1


async def test_stale_disconnect_callback_does_not_clobber_new_client():
    first = FakeClient(fail_writes=1)
    second = FakeClient()
    connector = make_connector([first, second])
    conn = make_connection(connector)
    await conn.send([POWER_ON])          # first write fails -> reconnect to second
    first.drop_from_remote()             # late callback from superseded client
    assert conn.connected is True
    await conn.disconnect()
