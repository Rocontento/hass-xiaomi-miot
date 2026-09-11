"""How often the d100e lock is woken up, and by whom.

It runs on batteries, and every property read over the LAN is a radio round trip
it pays for out of them. So its state is read from the cloud, which the lock
reports to on its own terms and which costs it nothing to be asked, while
commands still go straight to the lock.
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
    lst = await device.init_miot_coordinators(device.custom_config_integer("interval_seconds") or 60)
    # Names are prefixed with the device and the entry.
    return {coo.name.rsplit("-", 1)[-1]: coo for coo in lst}


def props_of(coo):
    """The unique props a coordinator's mapping covers."""
    mapping = next(
        cell.cell_contents
        for cell in coo.update_method.__closure__
        if isinstance(cell.cell_contents, dict)
    )
    return {MiotSpec.unique_prop(v) for v in mapping.values()}


def test_nothing_reaches_the_lock_over_the_lan(make_device, load_miot_spec):
    """Its wifi sleeps between conversations. Waking it is slow and expensive,
    and the connection it holds open to the cloud is neither."""
    device = model_device(make_device, load_miot_spec)

    assert not device.custom_config_bool("miot_local")
    assert not device.custom_config_bool("miot_local_action")
    assert not device.custom_config_bool("auto_local")
    assert device.custom_config_bool("miot_cloud_action") is True


async def test_the_whole_spec_is_read_at_once_on_the_default_interval(
    hass, make_device, load_miot_spec
):
    """Nothing is rationed any more, so nothing has to be split or slowed down."""
    device = model_device(make_device, load_miot_spec)
    coos = await coordinators(device)

    assert list(coos) == ["miot_status"]
    assert coos["miot_status"].update_interval.total_seconds() == 60
    assert device.custom_config("interval_seconds") is None
    assert not device.custom_config_list("chunk_coordinators")

    props = props_of(coos["miot_status"])
    assert {LOCK_STATE, DOOR_STATE, LOCK_MAH} <= props


async def test_a_command_refreshes_the_state(hass, make_device, load_miot_spec):
    device = model_device(make_device, load_miot_spec)
    await coordinators(device)

    # `update_main_status`, awaited after every lock command.
    assert [coo.name.rsplit("-", 1)[-1] for coo in device.main_coordinators] == ["miot_status"]


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
    device = model_device(make_device, load_miot_spec)

    assert device.custom_config_list(key)
