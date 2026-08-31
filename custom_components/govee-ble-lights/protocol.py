"""Govee BLE wire protocol: 20-byte command frames with XOR checksum.

Verified live against an H601C Glide downlight (2026-08-30): frames are
written to GATT characteristic 2b11; the device drops idle connections after
~10 s, which the keep-alive frame prevents.
"""
import json
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
    if not isinstance(payload, (bytes, bytearray, list)):
        # bytes(int) would silently yield zero-bytes (POWER, 1 == POWER OFF)
        raise ValueError(f"invalid payload type: {type(payload).__name__}")
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


# Models whose BLE brightness command takes 0-100 (values above clamp to 100).
# Measured live on H601C 2026-08-30; H601B/D are the same Glide family and
# H601A its 6" sibling. Explicit set: a "H601" prefix would also sweep in
# H6010, an unrelated BR30 bulb with an unverified scale.
PERCENT_BRIGHTNESS_MODELS = {"H601A", "H601B", "H601C", "H601D"}


def scale_brightness(model: str, ha_brightness: int) -> int:
    """Convert HA's 0-255 brightness to the device's BLE scale."""
    if model in PERCENT_BRIGHTNESS_MODELS:
        if ha_brightness <= 0:
            return 0
        return max(1, round(ha_brightness * 100 / 254))
    return ha_brightness


def available_models() -> list[str]:
    """All models offerable in the config flow (own catalog or fallback)."""
    models = {p.stem for p in JSONS_DIR.glob("*.json")}
    models.update(m for m in CATALOG_FALLBACK if resolve_catalog_model(m))
    return sorted(models)


def load_catalog(catalog_model: str) -> tuple[dict, list[str]]:
    """Read a scene catalog and flatten it into (json_data, effect_list).

    Handles both payload shapes: scenes whose lightEffects carry
    specialEffect entries, and (the H601 Glide family, 61 of 65 scenes)
    lightEffects that hold the scenceParam directly with specialEffect=[]
    — the latter are indexed with a -1 sentinel for the specialEffect slot.
    """
    json_data = json.loads((JSONS_DIR / f"{catalog_model}.json").read_text(encoding="utf-8"))
    effect_list: list[str] = []
    for ci, category in enumerate(json_data["data"]["categories"]):
        for si, scene in enumerate(category["scenes"]):
            for li, light_effect in enumerate(scene["lightEffects"]):
                label = " - ".join(
                    part for part in (category.get("categoryName"), scene.get("sceneName"),
                                       light_effect.get("scenceName")) if part)
                specials = light_effect.get("specialEffect") or []
                if specials:
                    for ei in range(len(specials)):
                        effect_list.append(f"{label} [{ci}/{si}/{li}/{ei}]")
                elif light_effect.get("scenceParam"):
                    effect_list.append(f"{label} [{ci}/{si}/{li}/-1]")
    return json_data, effect_list
