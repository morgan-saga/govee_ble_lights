"""Frame building — golden vectors verified live against an H601C on 2026-08-30."""
import pytest

from gbl.protocol import KEEPALIVE_FRAME, LedCommand, build_frame


def test_power_on_frame_matches_live_verified_bytes():
    frame = build_frame(LedCommand.POWER, [0x01])
    assert frame == bytes([0x33, 0x01, 0x01] + [0x00] * 16 + [0x33])


def test_power_off_frame_matches_live_verified_bytes():
    frame = build_frame(LedCommand.POWER, [0x00])
    assert frame == bytes([0x33, 0x01, 0x00] + [0x00] * 16 + [0x32])


def test_frame_is_always_20_bytes():
    assert len(build_frame(LedCommand.BRIGHTNESS, [0x7F])) == 20


def test_checksum_is_xor_of_all_preceding_bytes():
    frame = build_frame(LedCommand.BRIGHTNESS, [0xAB, 0x12])
    expected = 0
    for b in frame[:-1]:
        expected ^= b
    assert frame[-1] == expected


def test_keepalive_frame_matches_live_accepted_bytes():
    assert KEEPALIVE_FRAME == bytes([0xAA, 0x01] + [0x00] * 17 + [0xAB])


def test_payload_longer_than_17_bytes_rejected():
    with pytest.raises(ValueError):
        build_frame(LedCommand.COLOR, [0x00] * 18)


def test_bare_int_payload_rejected():
    with pytest.raises(ValueError):
        build_frame(LedCommand.POWER, 1)
