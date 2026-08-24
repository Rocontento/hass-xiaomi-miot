"""One miio conversation at a time, and commands never queue behind a poll.

The miio session is a single udp exchange whose handshake timestamp lives on the
connection. Two requests in flight at once answer each other's packets, and a
small battery device answers neither -- which is what a second coordinator on the
same device brought about.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.xiaomi_miot import lock  # noqa: F401
from custom_components.xiaomi_miot.lock import LockEntity
from custom_components.xiaomi_miot.core.device import MiotDevice
from custom_components.xiaomi_miot.core.miot_spec import MiotResult

MODEL = "xiaomi.lock.d100e"
GET_LOCKMSG_AIID = 10


class MiioStub:
    """Records how many requests are in flight at the same time."""

    def __init__(self, hold=None):
        self.addr = ("192.168.1.6", 54321)
        self.hold = hold or asyncio.Event()
        self.in_flight = 0
        self.peak = 0
        self.sent = []

    async def send(self, method, params=None, **kwargs):
        self.sent.append((method, params))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0)
            if method == "action":
                return {"id": 1, "result": {"code": 0, "out": [1, "ok"]}}
            return {"id": 1, "result": [{"code": 0}] * len(params or [1])}
        finally:
            self.in_flight -= 1


def miot_device(hass, miio=None):
    return MiotDevice(hass, miio or MiioStub())


@pytest.mark.asyncio
async def test_two_requests_never_share_the_miio_session(hass):
    miio = MiioStub()
    device = miot_device(hass, miio)

    await asyncio.gather(*[device.async_send("get_properties", [{"siid": 1}]) for _ in range(5)])

    assert len(miio.sent) == 5
    assert miio.peak == 1


@pytest.mark.asyncio
async def test_a_chunked_read_holds_the_session_one_chunk_at_a_time(hass):
    """A command must not have to wait for a whole 40 property read to finish."""
    miio = MiioStub()
    device = miot_device(hass, miio)
    seen = []

    async def watcher():
        for _ in range(6):
            seen.append(device.lan_busy)
            await asyncio.sleep(0)

    props = [{"siid": 1, "piid": i} for i in range(30)]
    await asyncio.gather(
        device.async_get_properties(props, max_properties=10),
        watcher(),
    )

    # Three chunks, and the lock is let go in between rather than held throughout.
    assert len(miio.sent) == 3
    assert False in seen


@pytest.mark.asyncio
async def test_the_session_is_free_again_when_a_request_raises(hass):
    miio = MiioStub()
    device = miot_device(hass, miio)

    async def boom(*args, **kwargs):
        raise OSError("network unreachable")

    miio.send = boom
    with pytest.raises(OSError):
        await device.async_send("get_properties", [{"siid": 1}])

    assert device.lan_busy is False


@pytest.mark.asyncio
async def test_local_busy_follows_the_miio_session(hass, make_device, load_miot_spec):
    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    assert device.local_busy is False

    device.local = miot_device(hass)
    assert device.local_busy is False

    async with device.local.lan_lock:
        assert device.local_busy is True
    assert device.local_busy is False


def set_conn_mode(device, mode):
    """`auto` is the mode that lets a device pick between the lan and the cloud."""
    config = {"username": "tester", "conn_mode": mode}
    device.entry.get_config = lambda key=None, default=None: config.get(key, default)


class CloudStub:
    def __init__(self):
        self.actions = []

    async def async_do_action(self, pms):
        self.actions.append(pms)
        return {"code": 0, "out": [1, "ok"]}


@pytest.mark.asyncio
async def test_a_command_goes_to_the_cloud_while_the_lan_is_busy(
    hass, make_device, load_miot_spec
):
    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    set_conn_mode(device, "auto")
    device.local = miot_device(hass)
    device.cloud = CloudStub()
    # A successful poll has already proved the LAN works, so `auto_cloud` is happy.
    device._local_state = True
    assert device.use_local is True

    async with device.local.lan_lock:
        result = await device.async_call_action(18, 1, ["s3cret"])

    assert result.is_success
    assert device.cloud.actions == [
        {"did": "test-device", "siid": 18, "aiid": 1, "in": ["s3cret"]},
    ]
    # Nothing was queued behind the poll.
    assert device.local.miio.sent == []


@pytest.mark.asyncio
async def test_a_busy_lan_does_not_divert_a_local_only_device(
    hass, make_device, load_miot_spec
):
    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    set_conn_mode(device, "local")
    device.local = miot_device(hass)
    device.cloud = CloudStub()
    device._local_state = True
    assert device.local_only is True

    await device.local.lan_lock.acquire()
    task = asyncio.ensure_future(device.async_call_action(18, 1, ["s3cret"]))
    await asyncio.sleep(0)
    assert not task.done()
    device.local.lan_lock.release()
    await task

    # It waits its turn on the lan rather than leaving the network it is pinned to.
    assert device.cloud.actions == []
    assert [method for method, _ in device.local.miio.sent] == ["action"]


@pytest.mark.asyncio
async def test_the_command_follows_the_secret_onto_the_cloud(
    make_device, load_miot_spec
):
    """A LAN timeout takes seconds. Spending a second one on the command after
    the secret was read can outlast the secret, and the lock then refuses a
    command that was authorised correctly."""
    from custom_components.xiaomi_miot.core.converters import MiotLockConv

    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    entity = LockEntity(device, next(c for c in device.converters if isinstance(c, MiotLockConv)))
    device.cloud = object()
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append((aiid, kwargs.get("cloud", False)))
        if not kwargs.get("cloud"):
            return MiotResult({}, code=-1, error="No response from the device")
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["s3cret", 1, "ok"]})
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_unlock() is True

    # The LAN is tried once, for the secret. The command goes where that worked.
    assert calls == [(GET_LOCKMSG_AIID, False), (GET_LOCKMSG_AIID, True), (1, True)]


@pytest.mark.asyncio
async def test_each_command_starts_over_on_the_lan(make_device, load_miot_spec):
    """A LAN that was busy a minute ago is not a reason to stay on the cloud."""
    from custom_components.xiaomi_miot.core.converters import MiotLockConv

    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    entity = LockEntity(device, next(c for c in device.converters if isinstance(c, MiotLockConv)))
    device.cloud = object()
    entity._prefer_cloud = True
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append(kwargs.get("cloud", False))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["s3cret", 1, "ok"]})
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_unlock() is True

    assert calls == [False, False]
