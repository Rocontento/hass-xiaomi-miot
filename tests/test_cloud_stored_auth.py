"""Tests for reusing a stored Xiaomi session without logging in again."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.storage import Store

from custom_components.xiaomi_miot.core.xiaomi_cloud import MiotCloud

USER_ID = "123456789"
SERVER = "de"
STORED = {
    CONF_USERNAME: USER_ID,
    "server_country": SERVER,
    "user_id": USER_ID,
    "service_token": "stored-service-token",
    "ssecurity": "stored-ssecurity",
    "sid": "xiaomiio",
    "device_id": "abcdef0123456789",
}


async def store_auth(hass, uid=USER_ID, server=SERVER, data=None):
    store = Store(hass, 1, f"xiaomi_miot/auth-{uid}-{server}.json")
    await store.async_save(data if data is not None else STORED)


def config_flow_input(username=USER_ID, server=SERVER):
    """What a config flow knows: no user id, it has not logged in yet."""
    return {
        CONF_USERNAME: username,
        CONF_PASSWORD: "hunter2",
        "server_country": server,
    }


async def test_stored_session_restores_user_id(hass):
    await store_auth(hass)

    cloud = await MiotCloud.from_token(hass, config_flow_input(), login=False)

    assert cloud.service_token == "stored-service-token"
    assert cloud.ssecurity == "stored-ssecurity"
    # Without the user id the restored token cannot be used at all.
    assert cloud.user_id == USER_ID
    assert cloud.client_id == "abcdef0123456789"


async def test_restored_session_can_open_an_api_session(hass):
    await store_auth(hass)

    cloud = await MiotCloud.from_token(hass, config_flow_input(), login=False)

    # `api_session` raises when either the token or the user id is missing.
    assert cloud.api_session() is not None


async def test_restored_session_reads_the_cached_device_list(hass):
    await store_auth(hass)
    devices = [{"did": "1", "name": "Lock"}, {"did": "2", "name": "Plug"}]
    await Store(hass, 1, f"xiaomi_miot/devices-{USER_ID}-{SERVER}.json").async_save({
        "update_time": 9999999999,
        "devices": devices,
        "homes": [],
    })

    cloud = await MiotCloud.from_token(hass, config_flow_input(), login=False)
    with patch.object(MiotCloud, "get_home_devices", AsyncMock()) as remote:
        assert await cloud.async_get_devices() == devices
    remote.assert_not_awaited()


async def test_no_stored_session_leaves_the_cloud_unauthenticated(hass):
    cloud = await MiotCloud.from_token(hass, config_flow_input(), login=False)

    assert not cloud.service_token
    assert not cloud.user_id


async def test_stored_session_is_looked_up_by_the_typed_username(hass):
    await store_auth(hass)

    # The store is keyed by user id, so an email does not find that session.
    cloud = await MiotCloud.from_token(
        hass,
        config_flow_input(username="someone@example.com"),
        login=False,
    )

    assert not cloud.service_token
    assert not cloud.user_id


async def test_stored_session_is_per_server(hass):
    await store_auth(hass)

    cloud = await MiotCloud.from_token(hass, config_flow_input(server="cn"), login=False)

    assert not cloud.service_token
    assert not cloud.user_id


@pytest.mark.parametrize("stored_user_id", [None, "", 0])
async def test_stored_session_without_user_id_stays_empty(hass, stored_user_id):
    await store_auth(hass, data={**STORED, "user_id": stored_user_id})

    cloud = await MiotCloud.from_token(hass, config_flow_input(), login=False)

    assert cloud.user_id == ""
