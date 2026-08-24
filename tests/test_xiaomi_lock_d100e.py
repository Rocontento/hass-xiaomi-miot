from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.lock import LockEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.xiaomi_miot import (  # noqa: F401
    binary_sensor, button, lock, number, select, sensor, switch,
)
from custom_components.xiaomi_miot.button import ButtonEntity
from custom_components.xiaomi_miot.lock import LockEntity
from custom_components.xiaomi_miot.core.converters import MiotLockConv, MiotActionConv
from custom_components.xiaomi_miot.core.miot_spec import MiotResult

MODEL = "xiaomi.lock.d100e"

LOCK_UNLOCK_SIID = 18
REMOTE_UNLOCK_AIID = 1
REMOTE_LOCK_AIID = 3
EMERGENCY_UNLOCK_AIID = 4
LOCK_STATE_PROP = "prop.19.12"


def model_device(make_device, load_miot_spec):
    return make_device(
        load_miot_spec("xiaomi.lock.d100e.json"),
        model=MODEL,
        customizes=None,
    )


def lock_entity(device):
    converter = next(c for c in device.converters if isinstance(c, MiotLockConv))
    return LockEntity(device, converter)


def test_lock_converter_is_built_from_the_state_service(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    converters = [c for c in device.converters if isinstance(c, MiotLockConv)]

    assert len(converters) == 1
    converter = converters[0]
    # The state lives in `lock-information` while the actions live in `lock-unlock`.
    assert converter.service.iid == 19
    assert converter.prop.unique_prop == LOCK_STATE_PROP
    assert converter.full_name == "lock.lock_information.lock_state"
    assert {
        device.find_converter(attr).prop.unique_prop
        for attr in converter.attrs
    } == {LOCK_STATE_PROP, "prop.20.1"}


def test_lock_entity_identity_and_supported_features(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)

    assert entity.entity_id == "lock.xiaomi_d100e_eeff"
    assert entity.unique_id == f"{device.unique_id}-19"
    # Named after the device, not after the `lock-information` miot service.
    assert entity._attr_name is None
    assert entity._attr_translation_key is None
    assert entity.supported_features & LockEntityFeature.OPEN


def test_actions_resolved_across_services(make_device, load_miot_spec):
    entity = lock_entity(model_device(make_device, load_miot_spec))

    assert entity._act_lock.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{REMOTE_LOCK_AIID}"
    assert entity._act_unlock.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{REMOTE_UNLOCK_AIID}"
    # Unlocking retracts the tongue, so it is also what Home Assistant calls "open".
    assert entity._act_open.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{REMOTE_UNLOCK_AIID}"
    assert entity._attr_extra_state_attributes["unlock_action"] == "lock_unlock.remote_unlock_e"


@pytest.mark.parametrize(
    "value, is_locked, is_jammed",
    [
        (0, True, False),   # Lock
        (1, False, False),  # Unlock
        (2, True, False),   # LockTongueProtruding
        (3, None, True),    # Abnormal
    ],
)
def test_lock_state_mapping(make_device, load_miot_spec, value, is_locked, is_jammed):
    entity = lock_entity(model_device(make_device, load_miot_spec))

    assert entity._locked_values == [0, 2]
    assert entity._unlocked_values == [1]
    assert entity._jammed_values == [3]

    entity.set_state({entity._conv_state.full_name: value})
    assert entity.is_locked is is_locked
    assert entity.is_jammed is is_jammed
    assert entity.is_locking is False
    assert entity.is_unlocking is False


def test_door_state_is_exposed_as_an_attribute(make_device, load_miot_spec):
    entity = lock_entity(model_device(make_device, load_miot_spec))

    for value, description in [(0, "Open"), (1, "Close"), (2, "NotDetected")]:
        entity.set_state({entity._conv_door.full_name: value})
        assert entity._attr_extra_state_attributes["door_state"] == description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, aiid",
    [
        ("async_lock", REMOTE_LOCK_AIID),
        ("async_unlock", REMOTE_UNLOCK_AIID),
        ("async_open", REMOTE_UNLOCK_AIID),
    ],
)
async def test_lock_commands_call_the_matching_miot_action(
    make_device,
    load_miot_spec,
    method,
    aiid,
):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action = AsyncMock(return_value=MiotResult({"code": 0}))
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await getattr(entity, method)()

    # `secret` is a string input of the action, it is sent empty unless customized.
    device.async_call_action.assert_awaited_once_with(LOCK_UNLOCK_SIID, aiid, [""])
    device.update_main_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_transition_state_is_shown_while_the_cloud_call_is_in_flight(
    make_device,
    load_miot_spec,
):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    entity.set_state({entity._conv_state.full_name: 0})
    seen = []

    async def slow_action(*args, **kwargs):
        seen.append((entity.is_locking, entity.is_unlocking))
        return MiotResult({"code": 0})

    device.async_call_action = slow_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    assert seen == [(False, True)]
    # The real state comes back with the next poll, `set_state` clears the transition.
    entity.set_state({entity._conv_state.full_name: 1})
    assert (entity.is_locked, entity.is_locking, entity.is_unlocking) == (False, False, False)


@pytest.mark.asyncio
async def test_unlock_code_is_sent_as_the_action_secret(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action = AsyncMock(return_value=MiotResult({"code": 0}))
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock(code="s3cret")

    device.async_call_action.assert_awaited_once_with(
        LOCK_UNLOCK_SIID,
        REMOTE_UNLOCK_AIID,
        ["s3cret"],
    )


@pytest.mark.asyncio
async def test_failed_action_keeps_state_and_reports_the_result(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    entity.set_state({entity._conv_state.full_name: 0})
    device.async_call_action = AsyncMock(return_value=MiotResult({"code": -704042011}))
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    assert entity.is_locked is True
    assert entity.is_unlocking is False
    assert "-704042011" in entity._attr_extra_state_attributes["unlock_result"]
    device.update_main_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_refusal_is_detected_even_when_the_miot_call_succeeds(
    make_device,
    load_miot_spec,
):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    entity.set_state({entity._conv_state.full_name: 0})
    # `remote-unlock-e` answers with `res` (0 Fail) and `msg`.
    device.async_call_action = AsyncMock(
        return_value=MiotResult({"code": 0, "out": [0, "secret invalid"]}),
    )
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    assert entity._attr_extra_state_attributes["unlock_result"] == {
        "res": "Fail",
        "msg": "secret invalid",
    }
    assert entity.is_locked is True
    assert entity.is_unlocking is False
    device.update_main_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_acceptance_is_decoded_into_the_attributes(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action = AsyncMock(
        return_value=MiotResult({"code": 0, "out": [1, "ok"]}),
    )
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_open()

    assert entity._attr_extra_state_attributes["open_result"] == {
        "res": "Success",
        "msg": "ok",
    }
    device.update_main_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_action_raises(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    entity._act_open = None

    with pytest.raises(HomeAssistantError):
        await entity.async_open()


def test_emergency_unlock_is_available_as_a_button(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    converter = next(
        c for c in device.converters
        if isinstance(c, MiotActionConv)
        and c.action.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{EMERGENCY_UNLOCK_AIID}"
    )

    assert converter.domain == "button"
    entity = ButtonEntity(device, converter)
    # `emergency-unlock` takes no input, so no secret is required for it.
    assert converter.action.ins == []
    assert entity.attr == "button.lock_unlock.emergency_unlock"
    assert device.encode({converter.full_name: []}) == {
        "method": "action",
        "param": {
            "did": device.did,
            "siid": LOCK_UNLOCK_SIID,
            "aiid": EMERGENCY_UNLOCK_AIID,
            "in": [],
        },
    }


def collect_entities(device):
    collected = {
        domain: []
        for domain in ["binary_sensor", "button", "lock", "number", "select", "sensor", "switch"]
    }
    for domain, entities in collected.items():
        device.entry.adders[domain] = (
            lambda new, update_before_add=False, bucket=entities: bucket.extend(new)
        )
        device.add_entities(domain)
    return collected


def test_complete_entity_set(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    with patch("custom_components.xiaomi_miot.core.device.async_call_later"):
        entities = collect_entities(device)

    assert len(entities["lock"]) == 1
    assert {entity.entity_id for entity in entities["button"]} >= {
        "button.xiaomi_d100e_eeff_emergency_unlock",
        "button.xiaomi_d100e_eeff_ble_lock",
        "button.xiaomi_d100e_eeff_ble_unlock",
    }
    # The lock service properties are still exposed for automations and diagnostics.
    assert {entity._miot_property.unique_prop for entity in entities["sensor"] if entity._miot_property} >= {
        LOCK_STATE_PROP,   # lock-state
        "prop.21.1",       # lock battery
        "prop.21.2",       # keypad battery
    }
    assert {entity._miot_property.unique_prop for entity in entities["number"]} == {
        "prop.18.5",  # lock-tongue-time
        "prop.26.2",  # close-door-lock-time
        "prop.26.4",  # unlock-autolock-time
    }
