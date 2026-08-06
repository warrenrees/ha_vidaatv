"""Tests for the Hisense TV media player entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.vidaa_tv.const import ACTIVITY_HOME, DOMAIN
from custom_components.vidaa_tv.media_player import (
    MAX_SOURCE_PROBES,
    PARALLEL_UPDATES,
    VidaaTVMediaPlayer,
)

from .conftest import MOCK_CONFIG_ENTRY_DATA, create_mock_config_entry


def test_parallel_updates_is_set() -> None:
    """Test that PARALLEL_UPDATES is properly defined."""
    assert PARALLEL_UPDATES == 1


async def test_media_player_setup(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player entity setup."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Check runtime_data was set
    assert entry.runtime_data is not None


async def test_media_player_state_on(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player state when TV is on."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["is_on"] is True


async def test_media_player_state_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player state when TV is off."""
    mock_vidaa_tv.async_get_state = AsyncMock(
        return_value={"statetype": "fake_sleep_0"}
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["is_on"] is False


async def test_media_player_volume(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player volume level."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["volume"] == 50


async def test_media_player_turn_on(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    mock_wake_tv: MagicMock,
) -> None:
    """Test media player turn on command."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_turn_on()

    mock_vidaa_tv.async_power_on.assert_called_once()


async def test_media_player_turn_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player turn off command."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_turn_off()

    mock_vidaa_tv.async_send_key.assert_called_once_with("KEY_POWER")


async def test_media_player_select_source(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player source selection."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_select_source("HDMI1")

    mock_vidaa_tv.async_set_source.assert_called_once_with("HDMI1")


async def test_media_player_launch_app(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player app launch."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_launch_app("netflix")

    mock_vidaa_tv.async_launch_app.assert_called_once_with("netflix")


async def test_media_player_send_key(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test media player send key."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_send_key("KEY_PLAY")

    mock_vidaa_tv.async_send_key.assert_called_once_with("KEY_PLAY")


# --- Source reporting -------------------------------------------------------
#
# The contract these tests defend: the source the entity reports is always
# either absent or an exact member of source_list. A consumer that cannot match
# it has to guess - Home Assistant's HomeKit bridge falls back to source_list[0]
# - which is how a TV sitting on HDMI 2 was reported to HomeKit as "Netflix".


def _make_player(
    data: dict | None,
    *,
    available: bool = True,
    sources: list[dict] | None = None,
    apps: list[dict] | None = None,
) -> VidaaTVMediaPlayer:
    """Build the entity around a stub coordinator, no hass required."""
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.available = available
    coordinator.async_select_source = AsyncMock()
    coordinator.async_launch_app = AsyncMock()
    coordinator.async_send_key = AsyncMock()

    entry = MagicMock()
    entry.data = {"device_id": "001122334455"}
    entry.entry_id = "test_entry"

    player = VidaaTVMediaPlayer(coordinator, entry)
    player._sources = sources if sources is not None else [
        {"sourceid": "0", "sourcename": "TV", "displayname": "TV Channels"},
        {"sourceid": "4", "sourcename": "HDMI2", "displayname": "PlayStation"},
    ]
    player._apps = apps if apps is not None else [{"name": "Netflix"}]
    player._rebuild_source_list()
    return player


async def _setup_with_state(
    hass: HomeAssistant, mock_vidaa_tv: MagicMock, tv_state: dict
) -> str:
    """Set the TV to one state, set up the integration, return the entity id."""
    mock_vidaa_tv.async_get_state = AsyncMock(return_value=tv_state)

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return hass.states.async_entity_ids("media_player")[0]


@pytest.mark.parametrize(
    ("tv_state", "expected_source"),
    [
        # An app is a source: it is in source_list and select_source launches it.
        ({"statetype": "app", "name": "netflix", "url": "netflix://"}, "Netflix"),
        # sourceswitch quotes displayname, which is the label the list uses.
        (
            {
                "statetype": "sourceswitch",
                "sourceid": "4",
                "sourcename": "HDMI2",
                "displayname": "HDMI 2",
            },
            "HDMI 2",
        ),
        # A set that sends no displayname names the same input by sourcename.
        (
            {"statetype": "sourceswitch", "sourceid": "4", "sourcename": "HDMI2"},
            "HDMI 2",
        ),
        # livetv carries neither, only sourceid.
        (
            {
                "statetype": "livetv",
                "sourceid": "TV",
                "channel_name": "ROMCOM K-Drama",
                "channel_num": "5001",
            },
            "TV Channels",
        ),
        ({"statetype": "remote_launcher"}, ACTIVITY_HOME),
    ],
)
async def test_reported_source_is_always_in_the_source_list(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    tv_state: dict,
    expected_source: str,
) -> None:
    """Whatever the TV calls the input, the entity reports the list's name."""
    entity_id = await _setup_with_state(hass, mock_vidaa_tv, tv_state)

    state = hass.states.get(entity_id)
    assert state.attributes["source"] == expected_source
    assert state.attributes["source"] in state.attributes["source_list"]


async def test_source_list_leads_with_home(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A consumer that cannot match the source picks the first entry.

    Home is a far less wrong guess than whichever app happened to sort first.
    """
    entity_id = await _setup_with_state(
        hass, mock_vidaa_tv, {"statetype": "remote_setting"}
    )

    state = hass.states.get(entity_id)
    assert state.attributes["source_list"][0] == ACTIVITY_HOME


async def test_no_source_reported_while_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A sleeping TV is showing nothing, so it names nothing."""
    entity_id = await _setup_with_state(
        hass, mock_vidaa_tv, {"statetype": "fake_sleep_0"}
    )

    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes.get("source") is None


@pytest.mark.parametrize("requested", ["HDMI 2", "HDMI2", "4"])
async def test_select_source_accepts_every_name_the_tv_uses(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    requested: str,
) -> None:
    """Automations written against the old list keep working.

    Whichever name is asked for, the TV is sent the sourcename its SOURCE_MAP
    understands - "HDMI 2" would be published verbatim as a source id and
    silently ignored.
    """
    entity_id = await _setup_with_state(
        hass, mock_vidaa_tv, {"statetype": "remote_launcher"}
    )

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": entity_id, "source": requested},
        blocking=True,
    )

    mock_vidaa_tv.async_set_source.assert_called_once_with("HDMI2")


async def test_select_source_launches_an_app(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Apps share the list with inputs, and must not be sent as a source."""
    entity_id = await _setup_with_state(
        hass, mock_vidaa_tv, {"statetype": "remote_launcher"}
    )

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": entity_id, "source": "Netflix"},
        blocking=True,
    )

    mock_vidaa_tv.async_launch_app.assert_called_once_with("Netflix")
    mock_vidaa_tv.async_set_source.assert_not_called()


async def test_select_home_navigates_to_the_launcher(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Home is the launcher, not an app - it is reached with the key."""
    entity_id = await _setup_with_state(
        hass, mock_vidaa_tv, {"statetype": "app", "name": "netflix"}
    )

    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": entity_id, "source": ACTIVITY_HOME},
        blocking=True,
    )

    mock_vidaa_tv.async_send_key.assert_called_once_with("KEY_HOME")
    mock_vidaa_tv.async_launch_app.assert_not_called()


def test_source_is_dropped_when_the_coordinator_is_unavailable() -> None:
    """coordinator.data keeps its last value when a poll fails.

    Reporting the TV off while still naming what it was watching is worse than
    naming nothing - and left HomeKit showing a stale input.
    """
    data = {"is_on": True, "statetype": "app", "app": "Netflix", "source": "Netflix"}

    assert _make_player(data).source == "Netflix"
    assert _make_player(data, available=False).source is None
    assert _make_player(data, available=False).app_name is None


def test_unknown_source_names_are_passed_through() -> None:
    """A TV whose source list never arrived is no worse off than before."""
    player = _make_player(
        {"is_on": True, "source": "SCART"}, sources=[], apps=[]
    )

    assert player.source == "SCART"
    assert player.source_list == [ACTIVITY_HOME]


def test_source_probe_is_retried_on_the_next_power_on() -> None:
    """One silent reply must not cost the source list for the whole session.

    Bounded, though: a set that never answers must not spawn probe jobs on
    every update forever.
    """
    player = _make_player({"is_on": True}, sources=[], apps=[])
    player.hass = MagicMock()
    # The probe is never run here, only counted; close it so it is not left
    # dangling as a never-awaited coroutine.
    player.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    player.async_write_ha_state = MagicMock()

    for _ in range(MAX_SOURCE_PROBES + 2):
        player._handle_coordinator_update()
    assert player.hass.async_create_task.call_count == MAX_SOURCE_PROBES

    player.coordinator.data = {"is_on": False}
    player._handle_coordinator_update()
    assert player._source_probe_attempts == 0

    player.coordinator.data = {"is_on": True}
    player._handle_coordinator_update()
    assert player.hass.async_create_task.call_count == MAX_SOURCE_PROBES + 1


async def test_input_is_learned_when_the_tv_has_no_source_list(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """The reported failure: on HDMI 2, HomeKit showed Netflix.

    Some sets answer sourcelist with nothing, leaving source_list holding apps
    and nothing else - so the current input matched no entry and a consumer
    picking source_list[0] named the first app. The state broadcast names the
    input, so take it from there.
    """
    mock_vidaa_tv.async_get_sources = AsyncMock(return_value=None)
    entity_id = await _setup_with_state(
        hass,
        mock_vidaa_tv,
        {
            "statetype": "sourceswitch",
            "sourceid": "4",
            "sourcename": "HDMI2",
            "displayname": "HDMI 2",
        },
    )

    state = hass.states.get(entity_id)
    assert state.attributes["source"] == "HDMI 2"
    assert state.attributes["source"] in state.attributes["source_list"]

    # Learned, not just displayed: it has to be selectable afterwards, by the
    # sourcename the library maps to a source id.
    await hass.services.async_call(
        "media_player",
        "select_source",
        {"entity_id": entity_id, "source": "HDMI 2"},
        blocking=True,
    )
    mock_vidaa_tv.async_set_source.assert_called_once_with("HDMI2")


async def test_live_tv_is_learned_by_sourceid_alone(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """livetv names its input by sourceid and nothing else."""
    mock_vidaa_tv.async_get_sources = AsyncMock(return_value=None)
    entity_id = await _setup_with_state(
        hass,
        mock_vidaa_tv,
        {
            "statetype": "livetv",
            "sourceid": "TV",
            "channel_name": "ROMCOM K-Drama",
            "channel_num": "5001",
        },
    )

    state = hass.states.get(entity_id)
    assert state.attributes["source"] == "TV"
    assert state.attributes["source"] in state.attributes["source_list"]


def test_a_known_source_is_not_learned_twice() -> None:
    """Learning must converge - source_list churn rebuilds HomeKit accessories."""
    player = _make_player(
        {
            "is_on": True,
            "statetype": "sourceswitch",
            "source": "PlayStation",
            "state": {"sourceid": "4", "sourcename": "HDMI2", "displayname": "PlayStation"},
        }
    )
    before = list(player.source_list)

    player._learn_current_source()
    player._learn_current_source()

    assert player.source_list == before
