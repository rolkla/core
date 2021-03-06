"""Helper to help store data."""
import asyncio
from json import JSONEncoder
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Type, Union

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.loader import bind_hass
from homeassistant.util import json as json_util

# mypy: allow-untyped-calls, allow-untyped-defs, no-warn-return-any
# mypy: no-check-untyped-defs

STORAGE_DIR = ".storage"
_LOGGER = logging.getLogger(__name__)


@bind_hass
async def async_migrator(
    hass,
    old_path,
    store,
    *,
    old_conf_load_func=json_util.load_json,
    old_conf_migrate_func=None,
):
    """Migrate old data to a store and then load data.

    async def old_conf_migrate_func(old_data)
    """

    def load_old_config():
        """Load old config."""
        if not os.path.isfile(old_path):
            return None

        return old_conf_load_func(old_path)

    config = await hass.async_add_executor_job(load_old_config)

    if config is None:
        return await store.async_load()

    if old_conf_migrate_func is not None:
        config = await old_conf_migrate_func(config)

    await store.async_save(config)
    await hass.async_add_executor_job(os.remove, old_path)
    return config


@bind_hass
class Store:
    """Class to help storing data."""

    def __init__(
        self,
        hass: HomeAssistant,
        version: int,
        key: str,
        private: bool = False,
        *,
        encoder: Optional[Type[JSONEncoder]] = None,
    ):
        """Initialize storage class."""
        self.version = version
        self.key = key
        self.hass = hass
        self._private = private
        self._data: Optional[Dict[str, Any]] = None
        self._unsub_delay_listener: Optional[CALLBACK_TYPE] = None
        self._unsub_final_write_listener: Optional[CALLBACK_TYPE] = None
        self._write_lock = asyncio.Lock()
        self._load_task: Optional[asyncio.Future] = None
        self._encoder = encoder

    @property
    def path(self):
        """Return the config path."""
        return self.hass.config.path(STORAGE_DIR, self.key)

    async def async_load(self) -> Union[Dict, List, None]:
        """Load data.

        If the expected version does not match the given version, the migrate
        function will be invoked with await migrate_func(version, config).

        Will ensure that when a call comes in while another one is in progress,
        the second call will wait and return the result of the first call.
        """
        if self._load_task is None:
            self._load_task = self.hass.async_add_job(self._async_load())
            assert self._load_task is not None

        return await self._load_task

    async def _async_load(self):
        """Load the data."""
        # Check if we have a pending write
        if self._data is not None:
            data = self._data

            # If we didn't generate data yet, do it now.
            if "data_func" in data:
                data["data"] = data.pop("data_func")()
        else:
            data = await self.hass.async_add_executor_job(
                json_util.load_json, self.path
            )

            if data == {}:
                return None
        if data["version"] == self.version:
            stored = data["data"]
        else:
            _LOGGER.info(
                "Migrating %s storage from %s to %s",
                self.key,
                data["version"],
                self.version,
            )
            stored = await self._async_migrate_func(data["version"], data["data"])

        self._load_task = None
        return stored

    async def async_save(self, data: Union[Dict, List]) -> None:
        """Save data."""
        self._data = {"version": self.version, "key": self.key, "data": data}

        self._async_cleanup_delay_listener()
        self._async_cleanup_final_write_listener()

        if self.hass.state == CoreState.stopping:
            self._async_ensure_final_write_listener()
            return

        await self._async_handle_write_data()

    @callback
    def async_delay_save(self, data_func: Callable[[], Dict], delay: float = 0) -> None:
        """Save data with an optional delay."""
        self._data = {"version": self.version, "key": self.key, "data_func": data_func}

        self._async_cleanup_delay_listener()
        self._async_cleanup_final_write_listener()

        if self.hass.state == CoreState.stopping:
            self._async_ensure_final_write_listener()
            return

        self._unsub_delay_listener = async_call_later(
            self.hass, delay, self._async_callback_delayed_write
        )
        self._async_ensure_final_write_listener()

    @callback
    def _async_ensure_final_write_listener(self):
        """Ensure that we write if we quit before delay has passed."""
        if self._unsub_final_write_listener is None:
            self._unsub_final_write_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_FINAL_WRITE, self._async_callback_final_write
            )

    @callback
    def _async_cleanup_final_write_listener(self):
        """Clean up a stop listener."""
        if self._unsub_final_write_listener is not None:
            self._unsub_final_write_listener()
            self._unsub_final_write_listener = None

    @callback
    def _async_cleanup_delay_listener(self):
        """Clean up a delay listener."""
        if self._unsub_delay_listener is not None:
            self._unsub_delay_listener()
            self._unsub_delay_listener = None

    async def _async_callback_delayed_write(self, _now):
        """Handle a delayed write callback."""
        # catch the case where a call is scheduled and then we stop Home Assistant
        if self.hass.state == CoreState.stopping:
            self._async_ensure_final_write_listener()
            return
        self._unsub_delay_listener = None
        self._async_cleanup_final_write_listener()
        await self._async_handle_write_data()

    async def _async_callback_final_write(self, _event):
        """Handle a write because Home Assistant is in final write state."""
        self._unsub_final_write_listener = None
        self._async_cleanup_delay_listener()
        await self._async_handle_write_data()

    async def _async_handle_write_data(self, *_args):
        """Handle writing the config."""
        data = self._data

        if "data_func" in data:
            data["data"] = data.pop("data_func")()

        self._data = None

        async with self._write_lock:
            try:
                await self.hass.async_add_executor_job(
                    self._write_data, self.path, data
                )
            except (json_util.SerializationError, json_util.WriteError) as err:
                _LOGGER.error("Error writing config for %s: %s", self.key, err)

    def _write_data(self, path: str, data: Dict) -> None:
        """Write the data."""
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))

        _LOGGER.debug("Writing data for %s", self.key)
        json_util.save_json(path, data, self._private, encoder=self._encoder)

    async def _async_migrate_func(self, old_version, old_data):
        """Migrate to the new version."""
        raise NotImplementedError

    async def async_remove(self):
        """Remove all data."""
        try:
            await self.hass.async_add_executor_job(os.unlink, self.path)
        except FileNotFoundError:
            pass
