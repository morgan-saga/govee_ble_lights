"""Govee BLE wire protocol: 20-byte command frames with XOR checksum.

Verified live against an H601C Glide downlight (2026-08-30): frames are
written to GATT characteristic 2b11; the device drops idle connections after
~10 s, which the keep-alive frame prevents.
"""
import re
from enum import IntEnum
from pathlib import Path

UUID_CONTROL_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b11"
UUID_STATUS_CHARACTERISTIC = "00010203-0405-0607-0809-0a0b0c0d2b10"

FRAME_LEN = 20


class LedCommand(IntEnum):
    """A control command packet's type."""

    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    """The mode in which a color change happens."""

    MANUAL = 0x02
    MICROPHONE = 0x06
    SCENES = 0x05
    SEGMENTS = 0x15


def _checksummed(head: int, body: bytes) -> bytes:
    frame = bytes([head]) + body
    frame += bytes(FRAME_LEN - 1 - len(frame))
    checksum = 0
    for b in frame:
        checksum ^= b
    return frame + bytes([checksum])


def build_frame(cmd: int, payload: bytes | list[int]) -> bytes:
    """Build a 0x33 command frame: cmd byte, payload, zero-pad, XOR checksum."""
    payload = bytes(payload)
    if len(payload) > FRAME_LEN - 3:
        raise ValueError(f"payload too long: {len(payload)} > {FRAME_LEN - 3}")
    return _checksummed(0x33, bytes([cmd & 0xFF]) + payload)


# 0xAA "status request" frame — H601x send no reply but accept the write,
# which resets the firmware idle timer, so it doubles as the keep-alive.
KEEPALIVE_FRAME = _checksummed(0xAA, bytes([0x01]))

JSONS_DIR = Path(__file__).parent / "jsons"

# Models without their own scene catalog that share a close relative's:
# H601C (6") and H601D (4" retrofit) are the same Glide family as H601B.
CATALOG_FALLBACK = {
    "H601C": "H601B",
    "H601D": "H601B",
}

_LOCAL_NAME_RE = re.compile(r"^(?:ihoment|Govee|GBK)_(H[0-9A-F]{4})_?([0-9A-F]*)$")


def parse_local_name(name: str) -> tuple[str, str] | None:
    """Parse a Govee BLE advertisement name into (model, suffix)."""
    match = _LOCAL_NAME_RE.match(name or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def resolve_catalog_model(model: str) -> str | None:
    """Map a model to the model whose scene catalog it uses, or None."""
    if (JSONS_DIR / f"{model}.json").is_file():
        return model
    fallback = CATALOG_FALLBACK.get(model)
    if fallback and (JSONS_DIR / f"{fallback}.json").is_file():
        return fallback
    return None


def available_models() -> list[str]:
    """All models offerable in the config flow (own catalog or fallback)."""
    models = {p.stem for p in JSONS_DIR.glob("*.json")}
    models.update(m for m in CATALOG_FALLBACK if resolve_catalog_model(m))
    return sorted(models)
