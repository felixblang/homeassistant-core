"""The switch Component for Meraki PoE control."""

import logging
import meraki
from functools import partial

from homeassistant.components.switch import SwitchEntity
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up Meraki PoE switches via config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    device_registry = dr.async_get(hass)
    entry = hass.data[DOMAIN].get("config_entry")
    if not entry:
        _LOGGER.error("Config entry not found!")
        return

    entities = []
    # Create switches for each device that has active PoE ports.
    for serial, device_data in coordinator.data.items():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, serial)},
            manufacturer="Cisco Meraki",
            name=device_data.get("name", f"Meraki Device {serial}"),
            model=device_data.get("model", "Unknown"),
            sw_version=device_data.get("firmware", "Unknown"),
            connections={("mac", device_data.get("mac", "Unknown"))},
        )
        active_ports = device_data.get("active_poe_ports", {})
        for port_id, port in active_ports.items():
            entities.append(MerakiPoeSwitch(coordinator, serial, port))
    async_add_entities(entities)

    def _handle_new_entity(new_entity):
        # Schedule the addition of the entity on the event loop thread-safely.
        hass.loop.call_soon_threadsafe(lambda: async_add_entities([new_entity]))

    async_dispatcher_connect(
        hass,
        f"{DOMAIN}_new_poe_port",
        _handle_new_entity,
    )


class MerakiPoeSwitch(CoordinatorEntity, RestoreEntity, SwitchEntity):
    """Representation of a Meraki PoE switch for a specific port."""

    def __init__(self, coordinator, serial, port):
        """Initialize the switch entity for a specific port."""
        device_data = coordinator.data.get(serial, {})
        super().__init__(coordinator)
        self._serial = serial
        self._port = port  # Port is a dict from the new API call.
        port_id = port.get("portId")
        lldp = port.get("lldp")
        if lldp and isinstance(lldp, dict) and lldp.get("systemName"):
            self._attr_name = f"Port {port_id} {lldp.get('systemName')}"
        else:
            self._attr_name = f"Port {port_id}"
        self._attr_unique_id = f"{serial}_poe_port_{port_id}"
        self._attr_has_entity_name = True
        # Initialize state based on the new API; default to True if not provided.
        self._enabled = port.get("enabled", True)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "name": device_data.get("name", f"Meraki Device {serial}"),
            "manufacturer": "Cisco Meraki",
            "model": device_data.get("model", "Unknown"),
            "sw_version": device_data.get("firmware", "Unknown"),
            "connections": {("mac", device_data.get("mac", "Unknown"))},
        }

    async def async_added_to_hass(self):
        """Restore state when added to hass and update if needed."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._enabled = last_state.state == "on"
        self.async_write_ha_state()

    @property
    def is_on(self):
        """Return True if the port is enabled."""
        return self._enabled

    async def async_turn_on(self, **kwargs):
        """Enable this port."""
        result = await self.hass.async_add_executor_job(self._set_poe, True)
        if result:
            self._enabled = result.get("enabled", True)
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Disable this port."""
        result = await self.hass.async_add_executor_job(self._set_poe, False)
        if result:
            self._enabled = result.get("enabled", False)
            self.async_write_ha_state()

    def _set_poe(self, enable):
        """Call the Meraki API to change the port state."""
        api_key = self.coordinator.config_entry.data["api_key"]
        dashboard = meraki.DashboardAPI(api_key=api_key, suppress_logging=True)
        try:
            port_id = (
                self._port.get("portId") if isinstance(self._port, dict) else self._port
            )
            response = dashboard.switch.updateDeviceSwitchPort(
                self._serial, port_id, enabled=enable
            )
            return response
        except meraki.APIError as err:
            _LOGGER.error("Failed to set state on port %s: %s", port_id, err)
            return False
