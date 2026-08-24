"""Vendors ship untranslated specs whose descriptions are the string "NA".

Home Assistant then names every entity "NA", which is how this surfaced on
`xiaomi.lock.d100e`: `switch.xiaomi_d100e_2296_unlock_auto_lock` had the right
entity id, from the urn, but "NA" as its friendly name, from the translation.
"""
import pytest

from custom_components.xiaomi_miot.core.miot_spec import MiotSpec, valid_description

TYPE = "urn:miot-spec-v2:device:lock:0000A038:xiaomi-d100e:2"


def spec_data():
    return {
        "type": TYPE,
        "description": "Lock",
        "services": [
            {
                "iid": 26,
                "type": "urn:xiaomi-spec:service:convenient-service:00007809:xiaomi-d100e:1",
                "description": "",
                "properties": [
                    {
                        "iid": 3,
                        "type": "urn:xiaomi-spec:property:unlock-auto-lock:00000003:xiaomi-d100e:1",
                        "description": "",
                        "format": "bool",
                        "access": ["read", "notify", "write"],
                    },
                ],
            },
        ],
    }


def langs(description):
    return {"en": {"service:026:property:003": description}}


@pytest.mark.parametrize("placeholder", ["NA", "na", "N/A", "", "  ", "null", "-"])
def test_placeholder_translations_fall_back_to_the_name(hass, placeholder):
    spec = MiotSpec(hass, spec_data(), langs(placeholder))
    prop = spec.services[26].properties[3]

    assert prop.name == "unlock_auto_lock"
    # The service is untranslated too, so both fall back to their urn names.
    assert prop.friendly_desc == "convenient_service unlock_auto_lock"


def test_a_real_translation_is_still_used(hass):
    spec = MiotSpec(hass, spec_data(), langs("Auto lock after unlocking"))
    prop = spec.services[26].properties[3]

    assert "Auto lock after unlocking" in prop.friendly_desc


def test_a_placeholder_in_the_spec_itself_is_dropped(hass):
    data = spec_data()
    data["services"][0]["properties"][0]["description"] = "NA"
    spec = MiotSpec(hass, data)
    prop = spec.services[26].properties[3]

    assert prop.description == ""
    assert prop.friendly_desc == "convenient_service unlock_auto_lock"


def test_untranslated_value_lists_show_the_raw_value(hass):
    data = spec_data()
    data["services"][0]["properties"][0].update({
        "format": "uint8",
        "value-list": [
            {"value": 0, "description": "NA"},
            {"value": 1, "description": "Unlock"},
        ],
    })
    spec = MiotSpec(hass, data)
    prop = spec.services[26].properties[3]

    # Better a number than "NA", and the described value is untouched.
    assert prop.list_description(0) == "0"
    assert prop.list_description(1) == "Unlock"


@pytest.mark.parametrize(
    "value, expected",
    [("NA", ""), ("n/a", ""), (None, ""), ("  Lock  ", "Lock"), (0, "0")],
)
def test_valid_description(value, expected):
    assert valid_description(value) == expected
