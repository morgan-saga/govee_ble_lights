from __future__ import annotations

import array
import base64
import logging
import re
import bleak_retry_connector
from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from . import Hub
from .connection import GoveeConnection, GoveeConnectionError
from .const import DOMAIN
from .govee_utils import prepareMultiplePacketsData
from .protocol import (
    JSONS_DIR,
    load_catalog,
    LedCommand,
    LedMode,
    build_frame,
    parse_local_name,
    resolve_catalog_model,
    scale_brightness,
)

_LOGGER = logging.getLogger(__name__)

EFFECT_PARSE = re.compile(r"\[(\d+)/(\d+)/(\d+)/(-?\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199']
# H601 Glide downlights ignore MANUAL (0x02) and segment (0x15) color frames;
# they speak the alternate RGB dialect 0x0D (verified on-device 2026-09-01).
ALT_RGB_MODELS = ['H601B', 'H601C', 'H601D']


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hass, hub, ble_device, config_entry)])


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    def __init__(self, hass: HomeAssistant, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        """Initialize a bluetooth light."""
        self._hass = hass
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._uses_alt_rgb = self._model in ALT_RGB_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None

        parsed = parse_local_name(getattr(ble_device, "name", "") or "")
        suffix = parsed[1] if parsed and parsed[1] else self._mac.replace(":", "")[-4:].upper()
        self._attr_name = f"Govee {self._model} {suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            manufacturer="Govee",
            model=self._model,
            name=self._attr_name,
        )

        self._catalog_model = resolve_catalog_model(self._model)
        self._scene_json: dict | None = None
        self._attr_effect_list: list[str] | None = None

        self._connection = GoveeConnection(
            get_ble_device=self._current_ble_device,
            name=self._attr_name,
            connector=self._connect_client,
        )

    async def async_added_to_hass(self) -> None:
        if self._catalog_model is not None:
            try:
                self._scene_json, self._attr_effect_list = await self._hass.async_add_executor_job(
                    load_catalog, self._catalog_model
                )
            except Exception as err:  # noqa: BLE001 - a bad catalog must not kill the entity
                _LOGGER.warning(
                    "%s: scene catalog %s failed to load (%s); effects disabled",
                    self._attr_name, self._catalog_model, err,
                )

    async def async_will_remove_from_hass(self) -> None:
        await self._connection.disconnect()

    def _current_ble_device(self):
        fresh = bluetooth.async_ble_device_from_address(self._hass, self._mac.upper(), True)
        if fresh is not None:
            self._ble_device = fresh
        return self._ble_device

    async def _connect_client(self, ble_device, disconnected_callback):
        return await bleak_retry_connector.establish_connection(
            BleakClient, ble_device, self.unique_id,
            disconnected_callback=disconnected_callback,
        )

    @property
    def effect_list(self) -> list[str] | None:
        return self._attr_effect_list

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        """Return a unique, Home Assistant friendly identifier for this entity."""
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        commands = [build_frame(LedCommand.POWER, [0x1])]

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            commands.append(build_frame(
                LedCommand.BRIGHTNESS, [scale_brightness(self._model, brightness)]
            ))

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)

            if self._uses_alt_rgb:
                commands.append(build_frame(LedCommand.COLOR, [0x0D, red, green, blue]))
            elif self._is_segmented:
                commands.append(build_frame(LedCommand.COLOR,
                                            [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00, 0x00,
                                             0x00, 0x00, 0xFF, 0x7F]))
            else:
                commands.append(build_frame(LedCommand.COLOR, [LedMode.MANUAL, red, green, blue]))

        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT) or ""
            if effect and self._scene_json is None:
                _LOGGER.warning("%s: effect '%s' requested but no scene catalog is loaded",
                                self._attr_name, effect)
            elif effect:
                search = EFFECT_PARSE.search(effect)
                if search is None:
                    _LOGGER.warning("%s: effect '%s' not recognized (missing [c/s/l/e] index)",
                                    self._attr_name, effect)
                else:
                    try:
                        category = self._scene_json['data']['categories'][int(search.group(1))]
                        scene = category['scenes'][int(search.group(2))]
                        light_effect = scene['lightEffects'][int(search.group(3))]
                        special_idx = int(search.group(4))
                        if special_idx >= 0:
                            param = light_effect['specialEffect'][special_idx]['scenceParam']
                        else:
                            param = light_effect['scenceParam']
                    except (IndexError, KeyError) as err:
                        _LOGGER.warning("%s: effect '%s' no longer matches the catalog (%s)",
                                        self._attr_name, effect, err)
                    else:
                        # The scene payload exceeds one frame; send as chunked 0xa3 packets
                        commands.extend(prepareMultiplePacketsData(
                            0xa3,
                            array.array('B', [0x02]),
                            array.array('B', base64.b64decode(param)),
                        ))

        await self._send(commands)
        self._state = True
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs.get(ATTR_BRIGHTNESS, 255)

    async def async_turn_off(self, **kwargs) -> None:
        await self._send([build_frame(LedCommand.POWER, [0x0])])
        self._state = False

    async def _send(self, commands: list[bytes]) -> None:
        try:
            await self._connection.send(commands)
        except GoveeConnectionError as err:
            raise HomeAssistantError(f"{self._attr_name}: {err}") from err
