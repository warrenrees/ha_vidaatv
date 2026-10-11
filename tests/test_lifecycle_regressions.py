"""Offline regression tests. Only the loopback test opens a local TCP socket."""
from __future__ import annotations
import ast
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import logging
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
import paho.mqtt.client as mqtt
import pyvidaa.async_client as ac
from pyvidaa.client import VidaaTV, AppListTimeoutError, AppNotFoundError
from pyvidaa.config.storage import TokenStorage
from pyvidaa.topics import *

def load_coordinator():
    path = ROOT / 'custom_components' / 'vidaa_tv' / 'coordinator.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'VidaaTVDataUpdateCoordinator')
    class Base:
        def __class_getitem__(cls, item): return cls
        async def async_shutdown(self): pass
    namespace = {'asyncio': asyncio, 'DataUpdateCoordinator': Base, 'Any': object}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace['VidaaTVDataUpdateCoordinator']


class CoordinatorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unload_cancels_and_joins_entity_probe_tasks(self):
        cls = load_coordinator()
        c = cls.__new__(cls)
        c._stopping, c._background_tasks = False, set()
        c.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        started, finished = asyncio.Event(), asyncio.Event()
        async def probe():
            started.set()
            try: await asyncio.Event().wait()
            finally: finished.set()
        task = c.async_create_task(probe())
        await started.wait()
        await c.async_shutdown()
        self.assertTrue(task.cancelled())
        self.assertTrue(finished.is_set())
        self.assertEqual(c._background_tasks, set())
        coroutine = probe()
        self.assertIsNone(c.async_create_task(coroutine))
        self.assertIsNone(coroutine.cr_frame)


def load_function(filename, name, namespace):
    path = ROOT / 'custom_components' / 'vidaa_tv' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), 'exec'), namespace)
    return namespace[name]


class EntryCleanupTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self):
        return dict(asyncio=asyncio, _LOGGER=logging.getLogger('test'),
                    DEFAULT_AUTH_MODE='auto', DEFAULT_USE_SSL=True, DEFAULT_PORT=36669,
                    AUTH_MODE_STATIC='static', TIMEOUT_CONNECT=1,
                    auth_mode_kwargs=lambda value: {}, **{
                        'CONF_' + name: name.lower() for name in (
                            'HOST', 'PORT', 'MAC', 'DEVICE_ID', 'BRAND', 'CERTFILE',
                            'KEYFILE', 'USE_SSL', 'AUTH_MODE', 'HW_MAC')})

    async def test_cancelled_entry_setup_closes_its_client(self):
        started = asyncio.Event()
        async def connect(**kwargs):
            started.set()
            await asyncio.Event().wait()
        tv = SimpleNamespace(async_connect=connect, async_close=AsyncMock())
        namespace = self.namespace()
        namespace['AsyncVidaaTV'] = lambda **kwargs: tv
        setup = load_function('__init__.py', 'async_setup_entry', namespace)
        entry = SimpleNamespace(data={'host': 'tv.invalid'}, options={})
        task = asyncio.create_task(setup(SimpleNamespace(), entry))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        tv.async_close.assert_awaited_once()

    async def test_cancelled_connection_validation_closes_its_client(self):
        started = asyncio.Event()
        async def connect(**kwargs):
            started.set()
            await asyncio.Event().wait()
        tv = SimpleNamespace(async_connect=connect, async_close=AsyncMock())
        namespace = self.namespace()
        namespace.update(AsyncVidaaTV=lambda **kwargs: tv, probe_ip=lambda host: None,
                         CannotConnect=RuntimeError)
        validate = load_function('config_flow.py', 'validate_connection', namespace)
        hass = SimpleNamespace(async_add_executor_job=AsyncMock(return_value=None))
        task = asyncio.create_task(validate(hass, 'tv.invalid', 36669, auth_mode='static'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        tv.async_close.assert_awaited_once()

    async def test_successful_unload_shuts_down_before_close(self):
        order = []
        async def shutdown(): order.append('shutdown')
        async def close(): order.append('close')
        unload = load_function('__init__.py', 'async_unload_entry', {'PLATFORMS': []})
        entry = SimpleNamespace(runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(async_shutdown=shutdown), tv=SimpleNamespace(async_close=close)))
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)))
        self.assertTrue(await unload(hass, entry))
        self.assertEqual(order, ['shutdown', 'close'])

    async def test_close_still_runs_if_coordinator_shutdown_fails(self):
        close = AsyncMock()
        unload = load_function('__init__.py', 'async_unload_entry', {'PLATFORMS': []})
        entry = SimpleNamespace(runtime_data=SimpleNamespace(
            coordinator=SimpleNamespace(async_shutdown=AsyncMock(side_effect=RuntimeError('test-only'))),
            tv=SimpleNamespace(async_close=close)))
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)))
        with self.assertRaises(RuntimeError): await unload(hass, entry)
        close.assert_awaited_once()


class StandbyPollingTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        cls = load_coordinator()
        self.clock = 0.0
        self.awake = False
        self.attempts = []
        ns = cls._async_update_data.__globals__
        ns.update(time=SimpleNamespace(monotonic=lambda: self.clock),
                  _LOGGER=logging.getLogger('test.coordinator'),
                  UpdateFailed=type('UpdateFailed', (Exception,), {}),
                  ConfigEntryAuthFailed=type('ConfigEntryAuthFailed', (Exception,), {}),
                  HomeAssistantError=type('HomeAssistantError', (Exception,), {}),
                  STATE_FAKE_SLEEP='fake_sleep_0', STATE_REMOTE_LAUNCHER='remote_launcher',
                  ACTIVITY_HOME='Home', APPS={}, CONF_HOST='host',
                  _ipv4_broadcast_subnet=lambda value: None, wake_tv=lambda *args: None)
        c = cls.__new__(cls)
        c._stopping = False
        c._background_tasks = set()
        c._connection_lock = asyncio.Lock()
        c._power_lock = asyncio.Lock()
        c._online_update_interval = timedelta(seconds=30)
        c._standby_update_interval = timedelta(seconds=5)
        c.update_interval = c._online_update_interval
        c._auth_failures = 0
        c.data = None
        c.tv = SimpleNamespace(is_connected=False, is_muted=False,
                               async_reset=AsyncMock(), async_power_on=AsyncMock(),
                               async_get_state=AsyncMock(return_value={'statetype': 'app', 'name': 'test'}),
                               async_get_volume=AsyncMock(return_value=10))
        async def connect(**kwargs):
            self.attempts.append(self.clock)
            c.tv.is_connected = self.awake
            return self.awake
        c.tv.async_connect = connect
        c._async_maybe_refresh_token = AsyncMock()
        c._async_fetch_device_info = AsyncMock()
        c.entry = SimpleNamespace(data={'host': 'tv.invalid'})
        c.hass = SimpleNamespace(async_add_executor_job=AsyncMock(),
                                 async_create_task=asyncio.create_task)
        c._wol_targets = lambda: ['00:11:22:33:44:55']
        async def refresh(): c.data = await c._async_update_data()
        c.async_request_refresh = refresh
        return c

    async def test_offline_polls_keep_normal_schedule_without_fetching_state(self):
        c = self.coordinator()
        for second in range(0, 100, 5):
            self.clock = float(second)
            data = await c._async_update_data()
            self.assertFalse(data['is_on'])
            self.assertEqual(c.update_interval, timedelta(seconds=5))
        self.assertEqual(self.attempts, list(range(0, 100, 5)))
        c.tv.async_get_state.assert_not_awaited()
        self.assertEqual(c.tv.async_reset.await_count, len(self.attempts))

    async def test_external_wake_recovers_on_next_normal_poll(self):
        c = self.coordinator()
        await c._async_update_data()
        self.awake = True
        self.clock = 5.0
        data = await c._async_update_data()
        self.assertTrue(data['is_on'])
        self.assertEqual(c.update_interval, timedelta(seconds=30))
        self.clock = 20.0
        await c._async_update_data()
        self.assertEqual(self.attempts, [0, 5])

    async def test_explicit_wake_refreshes_immediately(self):
        c = self.coordinator()
        await c._async_update_data()
        self.awake = True
        self.clock = 1.0
        await c.async_turn_on()
        self.assertTrue(c.data['is_on'])
        self.assertEqual(self.attempts, [0, 1])
        c.tv.async_power_on.assert_not_awaited()

    async def test_entity_metadata_waits_until_connected(self):
        c = self.coordinator()
        c.tv.async_get_apps = AsyncMock(return_value=[{'name': 'test'}])
        c.tv.async_get_sources = AsyncMock(return_value=[{'name': 'HDMI1'}])
        self.assertIsNone(await c.async_get_apps())
        self.assertIsNone(await c.async_get_sources())
        c.tv.async_get_apps.assert_not_awaited()
        c.tv.async_get_sources.assert_not_awaited()
        c.tv.is_connected = True
        self.assertEqual(await c.async_get_apps(), [{'name': 'test'}])
        self.assertEqual(await c.async_get_sources(), [{'name': 'HDMI1'}])


class WakeCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        factory = StandbyPollingTests()
        c = factory.coordinator()
        c._WAKE_RETRY_INTERVAL = .001
        c._WAKE_TIMEOUT = .1
        c.async_request_refresh = AsyncMock()
        return c

    async def test_slow_wake_waits_for_connection_and_state_without_power_toggle(self):
        c = self.coordinator()
        attempts = 0
        async def connect(**kwargs):
            nonlocal attempts
            attempts += 1
            c.tv.is_connected = attempts >= 3
            return c.tv.is_connected
        c.tv.async_connect = connect
        async def state(**kwargs):
            self.assertTrue(c.tv.is_connected)
            return {'statetype': 'app'}
        c.tv.async_get_state.side_effect = state
        await c.async_turn_on()
        self.assertEqual(attempts, 3)
        c.tv.async_power_on.assert_not_awaited()
        c.async_request_refresh.assert_awaited_once()

    async def test_wake_confirmation_does_not_require_a_launcher_broadcast(self):
        c = self.coordinator()
        c.tv.is_connected = True
        c.tv.async_get_state.side_effect = [None, {'statetype': 'fake_sleep_1'}]
        await c.async_turn_on()
        self.assertEqual(c.tv.async_get_state.await_count, 2)
        c.tv.async_power_on.assert_not_awaited()

    async def test_parallel_wakes_send_at_most_one_toggle_then_wait_for_ready(self):
        c = self.coordinator()
        c.tv.is_connected = True
        states = iter(['fake_sleep_0', 'fake_sleep_0', 'fake_sleep_1', 'app'])
        async def state(**kwargs): return {'statetype': next(states, 'app')}
        c.tv.async_get_state.side_effect = state
        c.tv.async_power_on.return_value = True
        await asyncio.gather(c.async_turn_on(), c.async_turn_on())
        c.tv.async_power_on.assert_awaited_once()
        self.assertEqual(c.async_request_refresh.await_count, 2)

    async def test_missing_tv_raises_one_action_error_without_sending_commands(self):
        c = self.coordinator()
        with self.assertRaisesRegex(Exception, 'did not become ready'):
            await c.async_turn_on()
        c.tv.async_power_on.assert_not_awaited()
        c.tv.async_get_state.assert_not_awaited()
        c.async_request_refresh.assert_not_awaited()
        self.assertFalse(c._power_lock.locked())

    async def test_unload_cancels_wake_before_any_more_commands(self):
        c = self.coordinator()
        started = asyncio.Event()
        async def connect(**kwargs):
            started.set()
            await asyncio.Event().wait()
        c.tv.async_connect = connect
        task = asyncio.create_task(c.async_turn_on())
        await started.wait()
        await c.async_shutdown()
        with self.assertRaises(asyncio.CancelledError): await task
        c.tv.async_power_on.assert_not_awaited()
        self.assertFalse(c._connection_lock.locked())
        self.assertFalse(c._power_lock.locked())
        self.assertEqual(c._background_tasks, set())

    async def test_caller_cancellation_stops_owned_wake_task(self):
        c = self.coordinator()
        started = asyncio.Event()
        async def connect(**kwargs):
            started.set()
            await asyncio.Event().wait()
        c.tv.async_connect = connect
        task = asyncio.create_task(c.async_turn_on())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(c._background_tasks, set())

    async def test_poll_and_wake_share_one_reconnect_without_resetting_live_client(self):
        c = self.coordinator()
        started, release = asyncio.Event(), asyncio.Event()
        async def connect(**kwargs):
            started.set()
            await release.wait()
            c.tv.is_connected = True
            return True
        c.tv.async_connect = connect
        poll = asyncio.create_task(c._async_update_data())
        await started.wait()
        wake = asyncio.create_task(c.async_turn_on())
        await asyncio.sleep(0)
        release.set()
        result, _ = await asyncio.gather(poll, wake)
        self.assertTrue(result['is_on'])
        c.tv.async_reset.assert_awaited_once()
        c.tv.async_power_on.assert_not_awaited()


class DeviceRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_lookup_scopes_to_current_entry_and_handles_not_created_device(self):
        for identifier, device in (('tv-device', None), (None, SimpleNamespace(id='owned', model='old', sw_version='old'))):
            cls = load_coordinator()
            c = cls.__new__(cls)
            c._device_info_fetched = c._device_info_unsupported = False
            c._device_info_misses = 0
            c.tv = SimpleNamespace(async_get_device_info=AsyncMock(return_value={'model_name': 'model', 'tv_version': 'version'}))
            c.entry = SimpleNamespace(data={'device_id': identifier}, entry_id='our-entry')
            c.hass = object()
            def lookup(key, config_entry_id):
                self.assertEqual(key, ('vidaa_tv', identifier or 'our-entry'))
                self.assertEqual(config_entry_id, 'our-entry')
                return device
            registry = SimpleNamespace(async_get_device_by_identifier=lookup, async_update_device=Mock())
            cls._async_fetch_device_info.__globals__.update(
                dr=SimpleNamespace(async_get=lambda hass: registry), CONF_DEVICE_ID='device_id',
                DOMAIN='vidaa_tv', _LOGGER=logging.getLogger('test'))
            await c._async_fetch_device_info()
            if device:
                registry.async_update_device.assert_called_once_with('owned', model='model', sw_version='version')
            else:
                registry.async_update_device.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
