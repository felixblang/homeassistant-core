"""The Cisco Meraki Integration."""

from datetime import timedelta
import functools
import logging

import meraki

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

DOMAIN = "hass_meraki"
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Meraki from a config entry."""
    api_key = entry.data.get("api_key")
    org_id = entry.data.get("org_id")
    network_id = entry.data.get("network_id")

    if not api_key:
        _LOGGER.error("API Key is missing!")
        return False

    dashboard = meraki.DashboardAPI(api_key=api_key, suppress_logging=True)

    async def _get_ports(serial):
        """Call the Meraki API to get port statuses for a device."""
        try:
            return await hass.async_add_executor_job(
                functools.partial(
                    dashboard.switch.getDeviceSwitchPortsStatuses,
                    serial,
                    timespan=300,
                )
            )
        except meraki.APIError as err:
            _LOGGER.error("Failed to get ports for serial %s: %s", serial, err)
            return False

    async def _get_neighbors(serial):
        """Call the Meraki API to get LLDP/CDP neighbor data for a device."""
        try:
            return await hass.async_add_executor_job(
                dashboard.devices.getDeviceLldpCdp, serial
            )
        except meraki.APIError as err:
            _LOGGER.error("Failed to get neighbors for serial %s: %s", serial, err)
            return False

    async def async_update_data():
        """Fetch latest device data and update port information including PoE allocation and LLDP neighbor data."""
        data = {}
        try:
            # Determine networks to query.
            if network_id:
                networks = [{"id": network_id}]
            else:
                networks = await hass.async_add_executor_job(
                    dashboard.organizations.getOrganizationNetworks, org_id
                )

            # Get all devices in the organization.
            all_devices = await hass.async_add_executor_job(
                dashboard.organizations.getOrganizationDevices, org_id
            )

            # Get availability info.
            devices_availabilities = await hass.async_add_executor_job(
                dashboard.organizations.getOrganizationDevicesAvailabilities,
                org_id,
            )

            if network_id:
                all_devices = [
                    device
                    for device in all_devices
                    if device.get("networkId") == network_id
                ]

            # Create an availability map keyed by serial.
            avail_map = {item["serial"]: item for item in devices_availabilities}

            # Save static device properties.
            for device in all_devices:
                serial = device.get("serial")
                if not serial:
                    continue
                device_avail = avail_map.get(serial, {})
                status = device_avail.get("status", "Unknown")
                data[serial] = {
                    "name": device.get("name", f"Meraki Device {serial}"),
                    "model": device.get("model", "Unknown"),
                    "firmware": device.get("firmware", "Unknown"),
                    "mac": device.get("mac", "Unknown"),
                    "networkId": device.get("networkId"),
                    "serial": serial,
                    "productType": device.get("productType", None),
                    "state": status,
                    "client_count": 0,
                    "clients": {},
                    # We'll store the full ports data here.
                    "ports": {},
                }
                # For switches, get port statuses using the new API call.
                if device.get("productType") == "switch":
                    ports = await _get_ports(serial)
                    if ports:
                        data[serial]["ports"] = ports

            # Initialize a set to track dispatched dynamic entities.
            dispatched_ports = hass.data.setdefault(DOMAIN, {}).setdefault(
                "dispatched_ports", set()
            )

            # Process ports for each switch device.
            for serial, device_data in data.items():
                if device_data.get("productType") != "switch":
                    continue
                ports = device_data.get("ports") or []
                # Get neighbor info for this device.
                neighbors = await _get_neighbors(serial)
                neighbor_ports = {}
                if neighbors:
                    neighbor_ports = neighbors.get("ports") or {}
                # Build active PoE ports using the new API field.
                active_poe_ports = {}
                for port in ports:
                    # Check if the new API field "poe.isAllocated" is true.
                    if port.get("poe", {}).get("isAllocated"):
                        port_id = port.get("portId")
                        # Merge LLDP neighbor info if available.
                        if port_id in neighbor_ports:
                            port["lldp"] = neighbor_ports[port_id].get("lldp")
                        active_poe_ports[port_id] = port
                # Store active PoE ports separately for dynamic updates.
                device_data["active_poe_ports"] = active_poe_ports

                # Dispatch dynamic entity creation only for new active ports.
                from .switch import MerakiPoeSwitch

                for port_id, port in active_poe_ports.items():
                    unique_id = f"{serial}_poe_port_{port_id}"
                    if unique_id not in dispatched_ports:
                        dispatched_ports.add(unique_id)
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_new_poe_port",
                            MerakiPoeSwitch(coordinator, serial, port),
                        )

            # Optional: Get active clients and assign them to devices.
            for network in networks:
                clients = await hass.async_add_executor_job(
                    functools.partial(
                        dashboard.networks.getNetworkClients,
                        network["id"],
                        perPage=5000,
                    )
                )
                for client in clients:
                    if client.get("status") != "Online":
                        continue
                    serial = client.get("recentDeviceSerial")
                    client_id = client.get("id")
                    if serial in data:
                        data[serial]["client_count"] += 1
                        data[serial]["clients"][client_id] = client
                        port = client.get("switchport")
                        if port and port in data[serial].get("active_poe_ports", {}):
                            data[serial]["active_poe_ports"][port]["client"] = client

        except Exception as e:
            _LOGGER.error(f"Error updating Meraki data: {e}")
        return data

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="meraki_coordinator",
        update_method=async_update_data,
        update_interval=timedelta(seconds=15),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["coordinator"] = coordinator
    hass.data[DOMAIN]["config_entry"] = entry

    # Forward setup to sensor and switch platforms.
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    await hass.config_entries.async_forward_entry_setups(entry, ["switch"])

    return True
