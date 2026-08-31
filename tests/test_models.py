"""Advertisement-name parsing and scene-catalog model resolution.

Real advertisement names captured in the 2026-08-30 scan:
ihoment_H601C_60DD, ihoment_H601D_8D5B, Govee_H605C_4645, GBK_H61A0_C927.
"""
from gbl.protocol import (available_models, load_catalog, parse_local_name,
                          resolve_catalog_model)


def test_parses_ihoment_prefix():
    assert parse_local_name("ihoment_H601C_60DD") == ("H601C", "60DD")


def test_parses_govee_prefix():
    assert parse_local_name("Govee_H605C_4645") == ("H605C", "4645")


def test_parses_gbk_prefix():
    assert parse_local_name("GBK_H61A0_C927") == ("H61A0", "C927")


def test_non_govee_name_returns_none():
    assert parse_local_name("SCHLAGE00153243") is None


def test_empty_name_returns_none():
    assert parse_local_name("") is None


def test_model_with_own_catalog_resolves_to_itself():
    assert resolve_catalog_model("H601B") == "H601B"


def test_h601c_falls_back_to_h601b_catalog():
    assert resolve_catalog_model("H601C") == "H601B"


def test_h601d_falls_back_to_h601b_catalog():
    assert resolve_catalog_model("H601D") == "H601B"


def test_unknown_model_resolves_to_none():
    assert resolve_catalog_model("H9999") is None


def test_available_models_includes_fallback_models():
    models = available_models()
    assert "H601B" in models
    assert "H601C" in models
    assert "H601D" in models
    assert models == sorted(models)


def test_h601b_catalog_flattens_lighteffect_level_scenes():
    _, effects = load_catalog("H601B")
    assert len(effects) >= 60, f"only {len(effects)} effects flattened"
    assert any("/-1]" in e for e in effects)


def test_h601b_effect_labels_are_not_blank():
    _, effects = load_catalog("H601B")
    for e in effects:
        label = e.rsplit(" [", 1)[0]
        assert label.strip(" -"), f"blank label in {e!r}"
