"""A local miot action answers in a different shape to the cloud.

    lan:    'out': [{'piid': 3, 'value': 1}, {'piid': 4, 'value': 'ok'}]
    cloud:  'out': [1, 'ok']

Everything downstream reads the cloud shape. Read the lan shape as though it
were the cloud one and a wrapped value gets formatted into the next command,
which hands the lock its own envelope where the secret should be:

    msg: "{'piid': 2, 'value': 'qtxOxrZK8h0dcG+IKKKGtw=='}"

That is the lock echoing back what it was sent, and refusing it.
"""
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.xiaomi_miot import lock  # noqa: F401
from custom_components.xiaomi_miot.lock import LockEntity
from custom_components.xiaomi_miot.core.converters import MiotLockConv
from custom_components.xiaomi_miot.core.device import MiotDevice
from custom_components.xiaomi_miot.core.miot_spec import MiotResult

MODEL = "xiaomi.lock.d100e"
LOCK_UNLOCK_SIID = 18
REMOTE_UNLOCK_AIID = 1
GET_LOCKMSG_AIID = 10


def d100e(make_device, load_miot_spec):
    device = make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL)
    conv = next(c for c in device.converters if isinstance(c, MiotLockConv))
    return device, LockEntity(device, conv)


def get_action(device, aiid):
    return device.spec.services[LOCK_UNLOCK_SIID].actions[aiid]


def test_a_wrapped_answer_is_unwrapped_in_the_order_the_action_declares(
    make_device, load_miot_spec
):
    device, _ = d100e(make_device, load_miot_spec)
    action = get_action(device, GET_LOCKMSG_AIID)
    assert action.out == [2, 3, 4]

    # Deliberately out of order, the piid is what says which output is which.
    out = [
        {"piid": 4, "value": "ok"},
        {"piid": 2, "value": "s3cret"},
        {"piid": 3, "value": 1},
    ]
    assert action.out_values(out) == ["s3cret", 1, "ok"]


def test_a_cloud_answer_is_left_alone(make_device, load_miot_spec):
    device, _ = d100e(make_device, load_miot_spec)
    action = get_action(device, GET_LOCKMSG_AIID)

    assert action.out_values(["s3cret", 1, "ok"]) == ["s3cret", 1, "ok"]
    assert action.out_values(None) is None
    assert action.out_values([]) == []


def test_a_missing_output_comes_back_as_nothing(make_device, load_miot_spec):
    device, _ = d100e(make_device, load_miot_spec)
    action = get_action(device, GET_LOCKMSG_AIID)

    assert action.out_values([{"piid": 3, "value": 0}]) == [None, 0, None]


class MiioStub:
    def __init__(self, out):
        self.addr = ("192.168.1.6", 54321)
        self.out = out
        self.sent = []

    async def send(self, method, params=None, **kwargs):
        self.sent.append((method, params))
        return {"id": 1, "result": {"code": 0, "out": self.out}}


@pytest.mark.asyncio
async def test_a_local_call_hands_back_the_cloud_shape(hass, make_device, load_miot_spec):
    device, _ = d100e(make_device, load_miot_spec)
    device.local = MiotDevice(
        hass,
        MiioStub([
            {"piid": 2, "value": "s3cret"},
            {"piid": 3, "value": 1},
            {"piid": 4, "value": "ok"},
        ]),
    )
    device._local_state = True

    result = await device.async_call_action(
        LOCK_UNLOCK_SIID, GET_LOCKMSG_AIID, [], local=True
    )

    assert result.updater == "local"
    assert result.get("out") == ["s3cret", 1, "ok"]


@pytest.mark.asyncio
async def test_the_lock_is_sent_the_secret_and_not_its_own_envelope(
    make_device, load_miot_spec
):
    """The regression this file is named after, end to end."""
    device, entity = d100e(make_device, load_miot_spec)
    entity.entity_id = "lock.d100e"
    sent = []

    async def call_action(siid, aiid, params=None, **kwargs):
        sent.append((aiid, params))
        if aiid == GET_LOCKMSG_AIID:
            # What the lan hands back, already unwrapped by `async_call_action`.
            return MiotResult({"code": 0, "out": ["qtxOxrZK8h0dcG+IKKKGtw==", 1, "ok"]})
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_open() is True

    assert sent == [
        (GET_LOCKMSG_AIID, []),
        (REMOTE_UNLOCK_AIID, ["qtxOxrZK8h0dcG+IKKKGtw=="]),
    ]


@pytest.mark.asyncio
async def test_a_wrapped_secret_is_never_formatted_into_a_command(
    make_device, load_miot_spec
):
    """Belt and braces: even handed a wrapped value, the secret is what is sent."""
    device, entity = d100e(make_device, load_miot_spec)
    entity.entity_id = "lock.d100e"
    sent = []

    async def call_action(siid, aiid, params=None, **kwargs):
        sent.append((aiid, params))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({
                "code": 0,
                "out": [{"piid": 2, "value": "s3cret"}, 1, "ok"],
            })
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_open()

    assert sent[1] == (REMOTE_UNLOCK_AIID, ["s3cret"])
