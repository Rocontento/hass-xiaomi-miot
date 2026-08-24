"""How often the d100e lock is woken up, and by whom.

It runs on batteries: every property read is a radio round trip it pays for,
so only the lock and the door are polled often.
"""
import pytest

from custom_components.xiaomi_miot import lock  # noqa: F401
from custom_components.xiaomi_miot.core.miot_spec import MiotSpec

MODEL = "xiaomi.lock.d100e"
LOCK_STATE = "prop.19.12"
DOOR_STATE = "prop.20.1"
LOCK_MAH = "prop.21.1"


def model_device(make_device, load_miot_spec):
    return make_device(load_miot_spec("xiaomi.lock.d100e.json"), model=MODEL, customizes=None)


async def coordinators(device):
    lst = await device.init_miot_coordinators(device.custom_config_integer("interval_seconds"))
    # Names are prefixed with the device and the entry.
    return {coo.name.rsplit("-", 1)[-1]: coo for coo in lst}


def props_of(coo, device=None):
    """The unique props a coordinator's mapping covers."""
    mapping = next(
        cell.cell_contents
        for cell in coo.update_method.__closure__
        if isinstance(cell.cell_contents, dict)
    )
    return {MiotSpec.unique_prop(v) for v in mapping.values()}


async def test_only_the_lock_and_the_door_are_polled_often(hass, make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    coos = await coordinators(device)

    fast = coos["chunk_1"]
    assert fast.update_interval.total_seconds() == 60
    assert props_of(fast) == {LOCK_STATE, DOOR_STATE}


async def test_everything_else_is_polled_rarely(hass, make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    coos = await coordinators(device)

    rest = coos["miot_status"]
    assert rest.update_interval.total_seconds() == 900
    props = props_of(rest)
    assert LOCK_MAH in props
    # The fast properties are not read twice.
    assert not props & {LOCK_STATE, DOOR_STATE}


async def test_a_command_only_refreshes_the_fast_properties(hass, make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    await coordinators(device)

    # `update_main_status`, awaited after every lock command, must not pull the
    # whole spec back over the radio.
    assert [coo.name.rsplit("-", 1)[-1] for coo in device.main_coordinators] == ["chunk_1"]


async def test_no_cloud_polling_is_inherited_from_the_generic_lock_config(
    hass,
    make_device,
    load_miot_spec,
):
    device = model_device(make_device, load_miot_spec)

    # `*.lock.*` polls the cloud for bluetooth lock events every other interval.
    assert device.miio_cloud_props == []
    assert not device.custom_config_list("sensor_attributes")
    assert not device.custom_config_list("binary_sensor_attributes")


@pytest.mark.parametrize("key", ["sensor_properties", "button_actions", "switch_properties"])
async def test_the_entities_are_kept(hass, make_device, load_miot_spec, key):
    """Polling less must not take entities away, only slow them down."""
    device = model_device(make_device, load_miot_spec)

    assert device.custom_config_list(key)
