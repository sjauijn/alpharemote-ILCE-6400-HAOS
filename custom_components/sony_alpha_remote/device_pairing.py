"""BLE pairing/bonding helper.

The camera prompts for confirmation on its own screen (a "Pair with
<host>?" dialog) during pairing. For BlueZ to deliver that prompt to
anything, an Agent must be registered on org.bluez.AgentManager1 --
normally provided as a side effect of running `bluetoothctl` or opening a
desktop Bluetooth settings panel. On a headless Home Assistant OS host,
neither is running, so bleak's `BleakClient.pair()` fails outright with
`org.bluez.Error.AuthenticationFailed`: BlueZ has no agent to ask.

This module registers a minimal, temporary agent (see bluez_agent.py) for
the duration of the pairing attempt, so the whole flow works from the UI
with no SSH/bluetoothctl step required.

Note: BleakClient.pair() returns None on success and raises on failure
(it does not return a boolean) as of bleak >= 1.0. A failed attempt can
leave a half-bonded device entry in BlueZ, which would make a retry
confusing (e.g. "already paired" when it plainly isn't usable), so on
failure this module also calls unpair() to remove that stale entry.
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from .bluez_agent import bluez_pairing_agent

_LOGGER = logging.getLogger(__name__)

PAIR_TIMEOUT = 30.0
UNPAIR_TIMEOUT = 10.0


class CameraPairingError(Exception):
    """Raised when pairing/bonding with the camera fails."""


async def async_pair_camera(ble_device: BLEDevice) -> None:
    """Pair (bond) with the camera over BLE.

    Registers a temporary auto-accept BlueZ pairing agent for the duration
    of the call so the confirmation prompt shown on the camera's screen can
    actually be answered, then calls BleakClient.pair() -- the same D-Bus
    Pair() call `bluetoothctl pair` makes under the hood.

    Raises CameraPairingError with a human-readable reason on failure.
    """
    client = BleakClient(ble_device)
    connected = False
    try:
        async with asyncio.timeout(PAIR_TIMEOUT):
            async with bluez_pairing_agent():
                await client.connect()
                connected = True
                try:
                    await client.pair()
                except NotImplementedError as err:


                    _LOGGER.debug(
                        "pair() not implemented on this backend: %s", err
                    )
    except TimeoutError as err:
        await _async_cleanup_failed_pairing(client, connected)
        raise CameraPairingError(
            "Timed out while pairing. If the camera showed a confirmation "
            "prompt, make sure to accept it there within a few seconds. "
            "Otherwise, make sure the camera is turned on, awake, and "
            "within range."
        ) from err
    except BleakError as err:
        await _async_cleanup_failed_pairing(client, connected)
        raise CameraPairingError(f"Bluetooth error while pairing: {err}") from err
    else:
        try:
            await client.disconnect()
        except BleakError:
            pass


async def _async_cleanup_failed_pairing(client: BleakClient, connected: bool) -> None:
    """Best-effort cleanup so a retry doesn't get stuck on a stale bond."""
    if not connected:
        return
    try:
        await client.unpair()
    except (BleakError, NotImplementedError, AttributeError):


        pass
    try:
        await client.disconnect()
    except BleakError:
        pass


async def async_forget_camera(mac: str) -> None:
    """Remove the camera's pairing/bond from BlueZ entirely.

    This is the equivalent of running, over SSH::

        bluetoothctl
        remove <mac>

    It talks to BlueZ directly over D-Bus (Adapter1.RemoveDevice), which
    deletes the device's bonded/paired entry -- the same one that would
    otherwise keep showing up in `bluetoothctl devices` after the
    integration is removed from Home Assistant, blocking a clean re-pair
    once the camera's own network settings are reset.

    Safe to call even if the device was never paired, is powered off, or
    is out of range: unlike unpair(), this does not require an active BLE
    connection. Any failure is logged and swallowed -- this runs during
    config entry removal, and a cleanup failure here should not prevent
    the entry itself from being removed.
    """
    bus: MessageBus | None = None
    try:
        async with asyncio.timeout(UNPAIR_TIMEOUT):
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

            root_introspection = await bus.introspect("org.bluez", "/")
            root_proxy = bus.get_proxy_object("org.bluez", "/", root_introspection)
            om = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()

            mac_path_fragment = "dev_" + mac.upper().replace(":", "_")
            device_path = None
            adapter_path = None
            for path, interfaces in objects.items():
                if "org.bluez.Device1" in interfaces and path.endswith(
                    mac_path_fragment
                ):
                    device_path = path

                    adapter_path = path.rsplit("/", 1)[0]
                    break

            if device_path is None or adapter_path is None:
                _LOGGER.debug(
                    "No BlueZ device entry found for %s, nothing to remove", mac
                )
                return

            adapter_introspection = await bus.introspect("org.bluez", adapter_path)
            adapter_proxy = bus.get_proxy_object(
                "org.bluez", adapter_path, adapter_introspection
            )
            adapter = adapter_proxy.get_interface("org.bluez.Adapter1")
            await adapter.call_remove_device(device_path)
            _LOGGER.debug("Removed BlueZ device entry for %s", mac)
    except TimeoutError:
        _LOGGER.warning("Timed out removing BlueZ pairing for %s", mac)
    except DBusError as err:
        _LOGGER.warning("Could not remove BlueZ pairing for %s: %s", mac, err)
    except Exception:

        _LOGGER.exception("Unexpected error removing BlueZ pairing for %s", mac)
    finally:
        if bus is not None:
            bus.disconnect()
