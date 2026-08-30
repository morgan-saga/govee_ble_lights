"""Load the hyphen-named component dir as importable package `gbl`.

The component lives at custom_components/govee-ble-lights (hyphenated, so a
plain `import` statement can't reach it) and its __init__.py imports Home
Assistant, which isn't installed here. Registering a synthetic package with
__path__ pointing at the directory lets tests import the pure modules
(gbl.protocol, gbl.connection) without executing the HA-coupled __init__.
"""
import pathlib
import sys
import types

_COMPONENT_DIR = pathlib.Path(__file__).parent.parent / "custom_components" / "govee-ble-lights"

_pkg = types.ModuleType("gbl")
_pkg.__path__ = [str(_COMPONENT_DIR)]
sys.modules.setdefault("gbl", _pkg)
