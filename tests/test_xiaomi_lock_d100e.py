from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.lock import LockEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.xiaomi_miot import (  # noqa: F401
    binary_sensor, button, lock, number, select, sensor, switch,
)
from custom_components.xiaomi_miot.button import ButtonEntity
from custom_components.xiaomi_miot.lock import LockEntity, MomentaryLockEntity
from custom_components.xiaomi_miot.core.converters import MiotLockConv, MiotActionConv
from custom_components.xiaomi_miot.core.miot_spec import MiotResult

MODEL = "xiaomi.lock.d100e"

LOCK_UNLOCK_SIID = 18
REMOTE_UNLOCK_AIID = 1
REMOTE_LOCK_AIID = 3
EMERGENCY_UNLOCK_AIID = 4
GET_LOCKMSG_AIID = 10
LOCK_STATE_PROP = "prop.19.12"


def call_action_stub(secret_out, action_out=None):
    """`get-lockmsg` answers with the secret, the command with res/msg."""
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append((siid, aiid, params))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult(secret_out)
        return MiotResult(action_out if action_out is not None else {"code": 0, "out": [1, "ok"]})

    return call_action, calls


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
        # The bolt is withdrawn and only the spring latch is out: unlocked, but
        # not unlatched. Reporting it as locked hid every unlock from the user.
        (2, False, False),  # LockTongueProtruding
        (3, None, True),    # Abnormal
    ],
)
def test_lock_state_mapping(make_device, load_miot_spec, value, is_locked, is_jammed):
    entity = lock_entity(model_device(make_device, load_miot_spec))

    assert entity._locked_values == [0]
    assert entity._unlocked_values == [1, 2]
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
    call_action, calls = call_action_stub({"code": 0, "out": ["s3cret", 1, "ok"]})
    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await getattr(entity, method)()

    # The secret is read first, then sent as the input of the command itself.
    assert calls == [
        (LOCK_UNLOCK_SIID, GET_LOCKMSG_AIID, []),
        (LOCK_UNLOCK_SIID, aiid, ["s3cret"]),
    ]
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

    async def slow_action(siid, aiid, params=None, **kwargs):
        seen.append((entity.is_locking, entity.is_unlocking))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["s3cret", 1, "ok"]})
        return MiotResult({"code": 0})

    device.async_call_action = slow_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    # Reading the secret is part of the wait, the transition shows through both calls.
    assert seen == [(False, True), (False, True)]
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
        await entity.async_unlock(code="typed-by-the-user")

    # A code given by the user wins, the lock is not asked for a secret at all.
    device.async_call_action.assert_awaited_once_with(
        LOCK_UNLOCK_SIID,
        REMOTE_UNLOCK_AIID,
        ["typed-by-the-user"],
    )


@pytest.mark.asyncio
async def test_secret_action_is_resolved_from_the_spec(make_device, load_miot_spec):
    entity = lock_entity(model_device(make_device, load_miot_spec))

    assert entity._act_secret.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{GET_LOCKMSG_AIID}"
    assert entity._attr_extra_state_attributes["secret_action"] == "lock_unlock.get_lockmsg"
    # `emergency-unlock` takes no input, so it must not trigger a secret read.
    assert entity.needs_secret(entity._act_unlock, "unlock") is True
    assert entity.needs_secret(
        entity._act_unlock.service.get_action("emergency_unlock"), "unlock"
    ) is False


@pytest.mark.asyncio
async def test_customized_action_params_win_over_the_secret_action(
    make_device,
    load_miot_spec,
):
    device = make_device(
        load_miot_spec("xiaomi.lock.d100e.json"),
        model=MODEL,
        customizes={"unlock_action_params": ["fixed"]},
    )
    entity = lock_entity(device)
    device.async_call_action = AsyncMock(return_value=MiotResult({"code": 0}))
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    device.async_call_action.assert_awaited_once_with(
        LOCK_UNLOCK_SIID,
        REMOTE_UNLOCK_AIID,
        ["fixed"],
    )


@pytest.mark.asyncio
async def test_the_secret_is_never_published_as_an_attribute(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action, _ = call_action_stub({"code": 0, "out": ["s3cret", 1, "ok"]})
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        await entity.async_unlock()

    assert "s3cret" not in str(entity._attr_extra_state_attributes)


@pytest.mark.asyncio
async def test_a_refused_secret_read_still_runs_the_command(make_device, load_miot_spec):
    """The lock answers `res` 0 (Fail): there is no secret to send, but the
    command is still attempted the way it was before secrets were read."""
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action, calls = call_action_stub(
        {"code": 0, "out": ["", 0, "err"]},
        action_out={"code": 0, "out": [0, "err"]},
    )
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_unlock() is False

    assert calls[-1] == (LOCK_UNLOCK_SIID, REMOTE_UNLOCK_AIID, [""])
    assert entity._attr_extra_state_attributes["unlock_result"] == {
        "res": "Fail",
        "msg": "err",
    }


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

    # The stateful lock, plus the momentary unlatch one for bridged home apps.
    assert {entity.entity_id for entity in entities["lock"]} == {
        "lock.xiaomi_d100e_eeff",
        "lock.xiaomi_d100e_eeff_remote_unlock_e",
    }
    assert [type(e).__name__ for e in entities["lock"]].count("MomentaryLockEntity") == 1
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


@pytest.mark.asyncio
async def test_a_rejection_carrying_a_token_is_retried_with_it(
    make_device,
    load_miot_spec,
):
    """The lock can refuse a command and answer with a fresh token in `msg`
    rather than with an error, meaning "sign the retry with this"."""
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append((aiid, params))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["stale", 1, "ok"]})
        if params == ["stale"]:
            return MiotResult({"code": 0, "out": [0, "3DoiPIFcSanfvJaOfcGL+w=="]})
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_open() is True

    assert calls == [
        (GET_LOCKMSG_AIID, []),
        (REMOTE_UNLOCK_AIID, ["stale"]),
        (REMOTE_UNLOCK_AIID, ["3DoiPIFcSanfvJaOfcGL+w=="]),
    ]
    assert entity._attr_extra_state_attributes["open_result"] == {"res": "Success", "msg": "ok"}
    device.update_main_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_plain_error_message_is_not_retried(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.async_call_action, calls = call_action_stub(
        {"code": 0, "out": ["s3cret", 1, "ok"]},
        action_out={"code": 0, "out": [0, "err"]},
    )
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_unlock() is False

    # `err` is an error, not a token: one secret read and one command, no retry.
    assert len(calls) == 2


def test_a_token_is_told_apart_from_an_error_message():
    assert LockEntity.action_challenge({"msg": "3DoiPIFcSanfvJaOfcGL+w=="})
    assert LockEntity.action_challenge({"msg": "4R9nwoJplypdARXOkpyTvw=="})
    assert not LockEntity.action_challenge({"msg": "err"})
    assert not LockEntity.action_challenge({"msg": "secret invalid"})
    assert not LockEntity.action_challenge({"msg": ""})
    assert not LockEntity.action_challenge(None)


def momentary_entity(device):
    converter = next(
        c for c in device.converters
        if c.domain == "lock" and isinstance(c, MiotActionConv)
    )
    return MomentaryLockEntity(device, converter)


def test_a_momentary_lock_is_built_from_the_unlatch_action(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    converters = [c for c in device.converters if c.domain == "lock"]

    # The stateful lock and the momentary one, side by side on the same device.
    assert len(converters) == 2
    action_conv = next(c for c in converters if isinstance(c, MiotActionConv))
    assert action_conv.action.unique_prop == f"action.{LOCK_UNLOCK_SIID}.{REMOTE_UNLOCK_AIID}"
    assert action_conv.option.get("entity_type") == "lock_action"

    entity = MomentaryLockEntity(device, action_conv)
    assert entity.entity_id == "lock.xiaomi_d100e_eeff_remote_unlock_e"
    assert entity.unique_id != lock_entity(device).unique_id
    assert entity._attr_name == "Unlatch"
    # No `open`: a home app behind a bridge only ever sees lock and unlock.
    assert not entity.supported_features & LockEntityFeature.OPEN
    assert entity.is_locked is True


@pytest.mark.asyncio
async def test_the_momentary_lock_unlatches_then_locks_itself(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = momentary_entity(device)
    device.async_call_action, calls = call_action_stub({"code": 0, "out": ["s3cret", 1, "ok"]})
    device.update_main_status = AsyncMock()
    relock = []

    with patch("custom_components.xiaomi_miot.lock.async_call_later") as later:
        later.side_effect = lambda hass, delay, action: relock.append((delay, action)) or (lambda: None)
        with patch.object(MomentaryLockEntity, "_async_write_ha_state"):
            assert await entity.async_unlock() is True

            # Same secret handling as the real lock entity.
            assert calls == [
                (LOCK_UNLOCK_SIID, GET_LOCKMSG_AIID, []),
                (LOCK_UNLOCK_SIID, REMOTE_UNLOCK_AIID, ["s3cret"]),
            ]
            assert entity.is_locked is False

            delay, callback = relock[0]
            assert delay == 5
            await callback()
            assert entity.is_locked is True
            assert entity.is_unlocking is False


@pytest.mark.asyncio
async def test_the_momentary_lock_stays_locked_when_the_action_fails(
    make_device,
    load_miot_spec,
):
    device = model_device(make_device, load_miot_spec)
    entity = momentary_entity(device)
    device.async_call_action, _ = call_action_stub(
        {"code": 0, "out": ["s3cret", 1, "ok"]},
        action_out={"code": 0, "out": [0, "err"]},
    )
    device.update_main_status = AsyncMock()

    with patch("custom_components.xiaomi_miot.lock.async_call_later") as later:
        with patch.object(MomentaryLockEntity, "_async_write_ha_state"):
            assert await entity.async_unlock() is False

    assert entity.is_locked is True
    assert entity.is_unlocking is False
    later.assert_not_called()


@pytest.mark.asyncio
async def test_locking_the_momentary_lock_touches_no_action(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = momentary_entity(device)
    device.async_call_action = AsyncMock()

    with patch.object(MomentaryLockEntity, "_async_write_ha_state"):
        assert await entity.async_lock() is True

    # There is nothing to lock: the door already sprang shut behind you.
    assert entity.is_locked is True
    device.async_call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pending_relock_is_cancelled_by_a_new_unlatch(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    entity = momentary_entity(device)
    device.async_call_action, _ = call_action_stub({"code": 0, "out": ["s3cret", 1, "ok"]})
    device.update_main_status = AsyncMock()
    cancelled = []

    with patch("custom_components.xiaomi_miot.lock.async_call_later") as later:
        later.side_effect = lambda *a: lambda: cancelled.append(True)
        with patch.object(MomentaryLockEntity, "_async_write_ha_state"):
            await entity.async_unlock()
            await entity.async_unlock()

    # The first timer must not lock the entity while the second unlatch stands.
    assert cancelled == [True]
    assert entity.is_locked is False


def test_the_momentary_delay_is_configurable(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("xiaomi.lock.d100e.json"),
        model=MODEL,
        customizes={"lock_actions": "remote_unlock_e", "momentary_seconds": 12},
    )
    assert momentary_entity(device).momentary_seconds == 12


def test_a_broken_momentary_delay_falls_back_to_the_default(make_device, load_miot_spec):
    device = make_device(
        load_miot_spec("xiaomi.lock.d100e.json"),
        model=MODEL,
        customizes={"lock_actions": "remote_unlock_e", "momentary_seconds": "soon"},
    )
    assert momentary_entity(device).momentary_seconds == 5


def test_the_lock_is_driven_locally_with_the_cloud_as_a_fallback(make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)

    assert device.custom_config_bool("miot_local") is True
    assert device.custom_config_bool("auto_cloud") is True
    # Actions are no longer pinned to the cloud, the lock answers miot over the LAN.
    assert not device.custom_config_bool("miot_cloud_action")


@pytest.mark.asyncio
async def test_a_command_that_never_reached_the_lock_is_retried_over_the_cloud(
    make_device,
    load_miot_spec,
):
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.cloud = object()
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append(kwargs.get("cloud", False))
        if not kwargs.get("cloud"):
            # What `Device.async_call_action` returns for a transport failure.
            return MiotResult({}, code=-1, error="No response from the device")
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["s3cret", 1, "ok"]})
        return MiotResult({"code": 0, "out": [1, "ok"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_open() is True

    # The secret read falls back to the cloud, and the command follows it there
    # rather than spending another LAN timeout while the secret grows stale.
    assert calls == [False, True, True]


@pytest.mark.asyncio
async def test_a_refusal_is_not_retried_over_the_cloud(make_device, load_miot_spec):
    """The lock heard the command and said no. Sending it again could repeat
    something it already did, so the cloud is not tried."""
    device = model_device(make_device, load_miot_spec)
    entity = lock_entity(device)
    device.cloud = object()
    calls = []

    async def call_action(siid, aiid, params=None, **kwargs):
        calls.append(kwargs.get("cloud", False))
        if aiid == GET_LOCKMSG_AIID:
            return MiotResult({"code": 0, "out": ["s3cret", 1, "ok"]})
        return MiotResult({"code": 0, "out": [0, "err"]})

    device.async_call_action = call_action
    device.update_main_status = AsyncMock()

    with patch.object(LockEntity, "_async_write_ha_state"):
        assert await entity.async_unlock() is False

    assert calls == [False, False]


@pytest.mark.parametrize(
    "result, failed",
    [
        (MiotResult({}, code=-1, error="No response from the device"), True),
        (MiotResult({"code": 0, "out": [0, "err"]}), False),  # refused, but heard
        (MiotResult({"code": -704042011}), False),             # a real miot error
        (MiotResult({"code": 0}), False),
        (None, False),
    ],
)
def test_transport_failure_is_told_apart_from_a_refusal(result, failed):
    assert LockEntity.transport_failed(result) is failed
