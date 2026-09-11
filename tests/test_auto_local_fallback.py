"""Reading the device when the cloud cannot be reached.

For a device whose state is read from the cloud because that costs it nothing:
worth having while the cloud answers, and with the internet down the device is
the only one left who knows its own state.

The d100e itself does not ask for this any more -- waking its wifi turned out to
be slow as well as expensive, so nothing reaches it over the LAN unless a command
cannot get through any other way. Its spec is borrowed here as a device to test
the option with.
"""
from datetime import timedelta

import pytest

from custom_components.xiaomi_miot import lock  # noqa: F401
from custom_components.xiaomi_miot.core.device import MiotDevice
from custom_components.xiaomi_miot.core.miot_spec import MiotSpec

try:
    from miio import DeviceException
except ImportError:  # pragma: no cover
    from micloud.micloudexception import MiCloudException as DeviceException

from micloud.micloudexception import MiCloudException

MODEL = "xiaomi.lock.d100e"
LOCK_STATE_PROP = "prop.19.12"
AUTO_LOCAL = {"auto_local": True, "auto_local_interval": 300}


class MiioStub:
    def __init__(self, fail=False):
        self.addr = ("192.168.1.6", 54321)
        self.fail = fail
        self.sent = []

    async def send(self, method, params=None, **kwargs):
        self.sent.append((method, params))
        if self.fail:
            return {}  # what a timeout looks like, raises `No response from the device`
        return {
            "id": 1,
            "result": [{"did": p["did"], "siid": p["siid"], "piid": p["piid"], "code": 0, "value": 0}
                       for p in params],
        }


class CloudStub:
    def __init__(self, fail=False):
        self.fail = fail
        self.reads = 0

    async def async_get_properties_for_mapping(self, did, mapping):
        self.reads += 1
        if self.fail:
            raise MiCloudException("Network is unreachable")
        return [
            {"did": did, "siid": v["siid"], "piid": v["piid"], "code": 0, "value": 0}
            for v in mapping.values()
        ]


def d100e(hass, make_device, load_miot_spec, *, cloud_fails=False, lan_fails=False,
          customizes=AUTO_LOCAL):
    # `Device.customizes` is recomputed on every read, so an override has to go
    # in through the fixture rather than onto the dict it hands back.
    device = make_device(
        load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL, customizes=customizes
    )
    config = {"username": "tester", "conn_mode": "auto"}
    device.entry.get_config = lambda key=None, default=None: config.get(key, default)
    device.local = MiotDevice(hass, MiioStub(fail=lan_fails))
    device.cloud = CloudStub(fail=cloud_fails)
    return device


def small_mapping(device):
    return {"lock_state": {"siid": 19, "piid": 12}}


@pytest.mark.asyncio
async def test_the_lan_is_left_alone_while_the_cloud_answers(hass, make_device, load_miot_spec):
    """The whole point. A healthy cloud must never wake the lock."""
    device = d100e(hass, make_device, load_miot_spec)

    results = await device.update_miot_status(small_mapping(device))

    assert results.updater == "cloud"
    assert device.cloud.reads == 1
    assert device.local.miio.sent == []
    assert device.available is True


@pytest.mark.asyncio
async def test_the_lock_is_read_when_the_cloud_cannot_be(hass, make_device, load_miot_spec):
    device = d100e(hass, make_device, load_miot_spec, cloud_fails=True)

    results = await device.update_miot_status(small_mapping(device))

    assert results.updater == "local"
    assert not results.is_empty
    assert [m for m, _ in device.local.miio.sent] == ["get_properties"]
    # The state is known, so the entities stay usable through the outage.
    assert device.available is True


@pytest.mark.asyncio
async def test_the_cloud_is_tried_again_on_the_next_poll(hass, make_device, load_miot_spec):
    """The fallback must not become the new normal once the internet is back."""
    device = d100e(hass, make_device, load_miot_spec, cloud_fails=True)
    await device.update_miot_status(small_mapping(device))
    assert device.local.miio.sent

    device.cloud.fail = False
    device.local.miio.sent.clear()
    results = await device.update_miot_status(small_mapping(device))

    assert results.updater == "cloud"
    assert device.local.miio.sent == []
    assert device._cloud_fails == 0


@pytest.mark.asyncio
async def test_both_being_down_is_reported_once(hass, make_device, load_miot_spec):
    device = d100e(hass, make_device, load_miot_spec, cloud_fails=True, lan_fails=True)

    results = await device.update_miot_status(small_mapping(device))

    assert results.is_empty
    assert results.errors
    # Tried the cloud, then the lock, and did not go round again.
    assert device.cloud.reads == 1
    assert len(device.local.miio.sent) == 1


@pytest.mark.asyncio
async def test_a_locally_read_device_does_not_gain_a_round_trip(
    hass, make_device, load_miot_spec
):
    """`auto_local` is about cloud reads. A device already read over the lan
    keeps the `auto_cloud` behaviour and is not sent back to the lan."""
    device = d100e(
        hass, make_device, load_miot_spec, cloud_fails=True, lan_fails=True,
        customizes={"miot_local": True, "auto_cloud": True, **AUTO_LOCAL},
    )
    assert device.use_local is True

    await device.update_miot_status(small_mapping(device))

    # One local attempt, one cloud attempt, and no second visit to the lock.
    assert len(device.local.miio.sent) == 1
    assert device.cloud.reads == 1


@pytest.mark.asyncio
async def test_the_second_outage_poll_does_not_wake_the_lock_again(
    hass, make_device, load_miot_spec
):
    """A minute after the first fallback read the state has not changed enough to
    be worth the batteries, and the outage may have a long way to run."""
    device = d100e(hass, make_device, load_miot_spec, cloud_fails=True)
    mapping = small_mapping(device)

    first = await device.update_miot_status(mapping)
    assert first.updater == "local"
    assert len(device.local.miio.sent) == 1

    second = await device.update_miot_status(mapping)

    # Not read again, and the state it did read still stands.
    assert len(device.local.miio.sent) == 1
    assert second is first
    assert device.available is True


@pytest.mark.asyncio
async def test_the_lock_is_read_again_once_the_interval_is_up(
    hass, make_device, load_miot_spec
):
    device = d100e(hass, make_device, load_miot_spec, cloud_fails=True)
    mapping = small_mapping(device)

    await device.update_miot_status(mapping)
    assert len(device.local.miio.sent) == 1

    device._local_fallback_at -= timedelta(seconds=301)
    results = await device.update_miot_status(mapping)

    assert results.updater == "local"
    assert len(device.local.miio.sent) == 2


@pytest.mark.asyncio
async def test_an_outage_is_answered_at_once(hass, make_device, load_miot_spec):
    """The waiting is between reads, not before the first one."""
    device = d100e(hass, make_device, load_miot_spec)
    mapping = small_mapping(device)

    await device.update_miot_status(mapping)
    assert device.local.miio.sent == []

    device.cloud.fail = True
    results = await device.update_miot_status(mapping)

    assert results.updater == "local"
    assert len(device.local.miio.sent) == 1


@pytest.mark.asyncio
async def test_without_an_interval_every_failed_poll_falls_through(
    hass, make_device, load_miot_spec
):
    """What a mains powered device wants: no rationing, the poll costs it nothing."""
    device = d100e(
        hass, make_device, load_miot_spec, cloud_fails=True,
        customizes={"auto_local": True},  # no interval
    )
    assert device.custom_config("auto_local_interval") is None
    mapping = small_mapping(device)

    await device.update_miot_status(mapping)
    await device.update_miot_status(mapping)

    assert len(device.local.miio.sent) == 2
