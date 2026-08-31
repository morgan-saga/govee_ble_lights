"""Per-model brightness scaling.

Measured live 2026-08-30: H601C accepts 0-100 over BLE (values above 100
clamp to 100), while H605C accepts raw 0-255. HA hands integrations 0-255.
"""
from gbl.protocol import scale_brightness


def test_h601_scales_ha_max_to_100():
    assert scale_brightness("H601C", 254) == 100


def test_h601_scales_midpoint_to_50():
    assert scale_brightness("H601D", 127) == 50


def test_h601_low_value_keeps_floor_of_1():
    assert scale_brightness("H601B", 2) == 1


def test_h601_zero_stays_zero():
    assert scale_brightness("H601C", 0) == 0


def test_raw_scale_model_passes_through():
    assert scale_brightness("H605C", 128) == 128


def test_h6010_unrelated_bulb_not_swept_by_prefix():
    assert scale_brightness("H6010", 255) == 255


def test_unknown_model_passes_through():
    assert scale_brightness("H9999", 200) == 200
