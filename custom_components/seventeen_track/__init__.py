"""The seventeen_track component."""
from __future__ import annotations

from datetime import timedelta
import logging

from seventeentrack import Client as SeventeenTrackClient
from seventeentrack.errors import (
    InvalidTrackingNumberError,
    RequestError,
    SeventeenTrackError,
)
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID, CONF_FRIENDLY_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceRegistry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_flow import get_client
from .const import (
    CONF_CARRIER_NAME,
    CONF_SHOW_ARCHIVED,
    CONF_TRACKING_NUMBER,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SHOW_ARCHIVED,
    DOMAIN,
    PLATFORMS,
    SERVICE_ADD_PACKAGE,
)
from .errors import AuthenticationError

_LOGGER = logging.getLogger(__name__)


SERVICE_ADD_PACKAGE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_TRACKING_NUMBER): cv.string,
        vol.Optional(CONF_CARRIER_NAME): cv.string,
        vol.Optional(CONF_FRIENDLY_NAME): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the 17Track component."""
    try:
        client = await get_client(hass, config_entry.data)

    except AuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except SeventeenTrackError as err:
        raise ConfigEntryNotReady(
            f"There was an error while logging in: {err}"
        ) from err

    coordinator = SeventeenTrackDataCoordinator(hass, config_entry, client)

    config_entry.add_update_listener(coordinator.async_options_updated)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_register_service()

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator

    hass.config_entries.async_setup_platforms(config_entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload 17Track Entry from config_entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    ):
        hass.data[DOMAIN].pop(config_entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_ADD_PACKAGE)
            del hass.data[DOMAIN]

    return unload_ok


class SeventeenTrackDataCoordinator(DataUpdateCoordinator):
    """Get the latest data from 17Track."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: SeventeenTrackClient,
    ) -> None:
        """Initialize the data object."""
        self.hass = hass
        self.config_entry: ConfigEntry = config_entry
        self.client = client
        self.account_id: str = client.profile.account_id
        super().__init__(
            self.hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=self.config_entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                )
            ),
        )

    @property
    def show_archived(self) -> bool:
        """Include archived packages when fetching data."""
        return self.config_entry.options.get(CONF_SHOW_ARCHIVED, DEFAULT_SHOW_ARCHIVED)

    async def _async_update_data(self) -> dict[str, dict]:
        """Update 17Track data."""
        try:
            packages = await self.client.profile.packages(
                show_archived=False, tz=str(self.hass.config.time_zone)
            )
            summary = await self.client.profile.summary(show_archived=False)

            if self.show_archived:
                archived_packages = await self.client.profile.packages(
                    show_archived=True, tz=str(self.hass.config.time_zone)
                )
                packages += archived_packages

                archived_summary = await self.client.profile.summary(show_archived=True)
                for status, qty in archived_summary.items():
                    summary[status] += qty

        except SeventeenTrackError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        _LOGGER.debug("New package data received: %s", packages)
        _LOGGER.debug("New summary data received: %s", summary)

        return {"packages": packages, "summary": summary}

    async def async_register_service(self) -> bool:
        """Set up 17Track."""

        async def async_add_package(service: ServiceCall) -> None:
            """Add new package."""
            device_registry: DeviceRegistry = (
                self.hass.helpers.device_registry.async_get(self.hass)
            )
            device = device_registry.async_get(service.data[CONF_DEVICE_ID])
            if device is None:
                _LOGGER.error("17Track device not found")
                return
            for config_entry_id in device.config_entries:
                config_entry = self.hass.config_entries.async_get_entry(config_entry_id)
                if config_entry and config_entry.domain == DOMAIN:
                    client: SeventeenTrackClient = self.hass.data[DOMAIN][
                        config_entry_id
                    ].client
                    break
            try:
                await client.profile.add_package_with_carrier(
                    service.data[CONF_TRACKING_NUMBER],
                    service.data.get(CONF_CARRIER_NAME),
                    service.data.get(CONF_FRIENDLY_NAME),
                )
            except RequestError as err:
                _LOGGER.error("Package exists or could not be added: %s", err)
                raise SystemError(
                    "Package already exists or could not be added"
                ) from err
            except InvalidTrackingNumberError as err:
                _LOGGER.error("Could not set friendly_name: %s", err)
                raise SystemError("Could not set friendly_name") from err
            await self.async_request_refresh()

        self.hass.services.async_register(
            DOMAIN,
            SERVICE_ADD_PACKAGE,
            async_add_package,
            schema=SERVICE_ADD_PACKAGE_SCHEMA,
        )

        return True

    @staticmethod
    async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Triggered by config entry options updates."""
        hass.data[DOMAIN][entry.entry_id].update_interval = timedelta(
            minutes=entry.options[CONF_SCAN_INTERVAL]
        )
        await hass.data[DOMAIN][entry.entry_id].async_request_refresh()
