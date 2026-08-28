"""Support lock entity for Xiaomi Miot."""
import logging
import re

from homeassistant.components.lock import (
    DOMAIN as ENTITY_DOMAIN,
    LockEntity as BaseEntity,
    LockEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from . import (
    DOMAIN,
    XIAOMI_CONFIG_SCHEMA as PLATFORM_SCHEMA,  # noqa: F401
    HassEntry,
    XEntity,
    async_setup_config_entry,
    bind_services_to_entries,
)
from .core.miot_spec import MiotAction, MiotProperty
from .core.templates import template

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f'{ENTITY_DOMAIN}.{DOMAIN}'

SERVICE_TO_METHOD = {}

# Action names searched in the whole device spec, in priority order.
DEFAULT_LOCK_ACTIONS = ['remote_lock', 'lock', 'ble_lock']
DEFAULT_UNLOCK_ACTIONS = ['remote_unlock_e', 'remote_unlock', 'unlock', 'ble_unlock']
DEFAULT_OPEN_ACTIONS = ['unlatch', 'open_door', 'open']

# Descriptions of the lock state property, they are matched case insensitively
# and also with the spaces removed, see `MiotProperty.list_search`.
DEFAULT_LOCKED_VALUES = ['Lock', 'Locked', 'LockTongueProtruding']
DEFAULT_UNLOCKED_VALUES = ['Unlock', 'Unlocked', 'Open', 'Opened']
DEFAULT_JAMMED_VALUES = ['Abnormal', 'Jammed', 'LockStalled']

# Output properties telling whether the lock accepted the action, and the values
# that mean it did not. The miot call itself still returns a success code then.
REJECTION_PROPERTIES = ['res', 'result', 'status']
REJECTION_VALUES = ['fail', 'failed', 'failure']

# Locks that take a secret as the action input hand that secret out themselves,
# it has to be read right before every command. Actions asked for it, in priority
# order, and the output properties carrying the secret they answer with.
DEFAULT_SECRET_ACTIONS = ['get_lockmsg', 'get_lock_msg', 'get_secret']
SECRET_PROPERTIES = ['secret', 'token', 'key']

# A rejected command can come back with a fresh token to retry with instead of
# with an error message, the output property carrying it and what one looks like.
CHALLENGE_PROPERTIES = ['msg', 'message', 'secret', 'token']
CHALLENGE_PATTERN = re.compile(r'^[A-Za-z0-9+/_-]{16,}={0,2}$')

# Seconds a momentary lock shows itself unlocked before going back to locked.
DEFAULT_MOMENTARY_SECONDS = 5


async def async_setup_entry(hass, config_entry, async_add_entities):
    HassEntry.init(hass, config_entry).new_adder(ENTITY_DOMAIN, async_add_entities)
    await async_setup_config_entry(hass, config_entry, async_setup_platform, async_add_entities, ENTITY_DOMAIN)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data.setdefault(DATA_KEY, {})
    hass.data[DOMAIN]['add_entities'][ENTITY_DOMAIN] = async_add_entities
    config['hass'] = hass
    bind_services_to_entries(hass, SERVICE_TO_METHOD)


class LockEntity(XEntity, BaseEntity):
    _attr_is_locked = None
    _attr_is_jammed = False
    _attr_supported_features = LockEntityFeature(0)
    _conv_state = None
    _conv_door = None
    _act_lock: MiotAction = None
    _act_unlock: MiotAction = None
    _act_open: MiotAction = None
    _act_secret: MiotAction = None
    _locked_values = None
    _unlocked_values = None
    _jammed_values = None
    _transport = None

    def on_init(self):
        self._attr_available = self.device.available
        # A lock is the main feature of its device, name it after the device itself
        # instead of after the miot service it happens to be built from.
        self._attr_name = None
        self._attr_translation_key = None
        self.entity_id = self.device.spec.generate_entity_id(self, domain=ENTITY_DOMAIN)

        self._conv_state = self.find_state_converter()
        if prop := getattr(self._conv_state, 'prop', None):
            self._locked_values = self.custom_config_list('locked_values') or prop.list_search(
                *(self.custom_config_list('locked_states') or DEFAULT_LOCKED_VALUES)
            )
            self._unlocked_values = self.custom_config_list('unlocked_values') or prop.list_search(
                *(self.custom_config_list('unlocked_states') or DEFAULT_UNLOCKED_VALUES)
            )
            self._jammed_values = prop.list_search(*DEFAULT_JAMMED_VALUES)
            self._attr_extra_state_attributes['state_property'] = prop.full_name
        self._locked_values = self.int_values(self._locked_values)
        self._unlocked_values = self.int_values(self._unlocked_values)
        self._jammed_values = self.int_values(self._jammed_values)

        for attr in self.conv.attrs:
            conv = self.device.find_converter(attr)
            prop = getattr(conv, 'prop', None)
            if isinstance(prop, MiotProperty) and prop.in_list(['*.door_state']):
                self._conv_door = conv
                break

        self._act_lock = self.find_action(self.custom_config_list('lock_action') or DEFAULT_LOCK_ACTIONS)
        self._act_unlock = self.find_action(self.custom_config_list('unlock_action') or DEFAULT_UNLOCK_ACTIONS)
        self._act_open = self.find_action(self.custom_config_list('open_action') or DEFAULT_OPEN_ACTIONS)
        self._act_secret = self.find_action(self.custom_config_list('secret_action') or DEFAULT_SECRET_ACTIONS)
        if self._act_open:
            self._attr_supported_features |= LockEntityFeature.OPEN
        if fmt := self.custom_config('code_format'):
            self._attr_code_format = fmt

        self._attr_extra_state_attributes.update({
            'lock_action': self._act_lock.full_name if self._act_lock else None,
            'unlock_action': self._act_unlock.full_name if self._act_unlock else None,
            'open_action': self._act_open.full_name if self._act_open else None,
            'secret_action': self._act_secret.full_name if self._act_secret else None,
        })

    def find_state_converter(self):
        """The lock state property is not always in the service the entity is built from."""
        names = self.custom_config_list('lock_state_property') or ['lock_state', 'state']
        for attr in [self.attr, *self.conv.attrs]:
            conv = self.device.find_converter(attr)
            prop = getattr(conv, 'prop', None)
            if not isinstance(prop, MiotProperty) or not prop.value_list:
                continue
            if prop.in_list(names):
                return conv
        return None

    def find_action(self, names):
        if not self.device.spec:
            return None
        for name in names:
            for srv in self.device.spec.services.values():
                if action := srv.get_action(name):
                    return action
        return None

    def set_state(self, data: dict):
        if self._conv_state:
            val = self.int_value(self._conv_state.value_from_dict(data))
            if val is not None:
                self._attr_is_locking = False
                self._attr_is_unlocking = False
                self._attr_is_jammed = val in self._jammed_values
                if val in self._locked_values:
                    self._attr_is_locked = True
                elif val in self._unlocked_values:
                    self._attr_is_locked = False
                else:
                    self._attr_is_locked = None
        if self._conv_door:
            val = self._conv_door.value_from_dict(data)
            if val is not None:
                self._attr_extra_state_attributes['door_state'] = self._conv_door.prop.list_description(val)

    async def async_lock(self, **kwargs):
        return await self.async_run_action(self._act_lock, 'lock', **kwargs)

    async def async_unlock(self, **kwargs):
        return await self.async_run_action(self._act_unlock, 'unlock', **kwargs)

    async def async_open(self, **kwargs):
        return await self.async_run_action(self._act_open, 'open', **kwargs)

    async def async_run_action(self, action: MiotAction, key, **kwargs):
        if not action:
            raise HomeAssistantError(f'No miot action found to {key} {self.entity_id}')
        # Each command is a sequence of calls sharing one short lived secret, so
        # the transport is chosen once and the rest of the sequence follows it.
        self._transport = None
        # A command can take a moment to reach the lock, show the transition.
        self._attr_is_locking = key == 'lock'
        self._attr_is_unlocking = key != 'lock'
        self._async_write_ha_state()

        code = kwargs.get('code')
        if code is None and self.needs_secret(action, key):
            code = await self.async_read_secret()
        params = self.action_params(action, key, code)
        result = await self.async_lock_action(action, params)
        outs, rejected = self.action_result(action, result)
        if rejected and (challenge := self.action_challenge(outs)):
            # The lock answered with a token rather than an error, it wants the
            # command signed with it. Retried once, a second rejection is real.
            params = self.action_params(action, key, challenge)
            result = await self.async_lock_action(action, params)
            outs, rejected = self.action_result(action, result)
        # The miot call can succeed while the lock itself refuses the action,
        # eg. when it does not accept the secret sent as the action input.
        success = bool(result) and result.is_success and not rejected
        self._attr_extra_state_attributes[f'{key}_result'] = outs if outs else str(result)
        # Which way the command went. The routing has several opinions and none of
        # them show, so a command failing on one transport and working on the other
        # is otherwise only visible by turning on debug logging.
        self._attr_extra_state_attributes['last_transport'] = result.updater if result else None
        if success and action.out and not outs:
            # The action is declared to answer with a result and did not. The call
            # was accepted, but nothing says the lock acted on it, so do not let
            # that pass in silence.
            self.log.warning(
                '%s: Lock action %s over the %s was accepted but answered nothing '
                'readable: %s', self.entity_id, action.full_name, result.updater, result,
            )
        if not success:
            self.log.warning(
                '%s: Lock action %s over the %s failed: %s',
                self.entity_id, action.full_name, result.updater if result else None,
                outs or result,
            )
            self._attr_is_locking = False
            self._attr_is_unlocking = False
        self._async_write_ha_state()

        if success:
            await self.device.update_main_status()
        return success

    def needs_secret(self, action: MiotAction, key):
        """Whether the lock expects a secret of its own as the action input."""
        if not self._act_secret or self._act_secret == action:
            return False
        if self.custom_config_list(f'{key}_action_params') is not None:
            return False
        return any(
            prop and prop.format == 'string' and prop.in_list(SECRET_PROPERTIES)
            for prop in action.in_properties()
        )

    async def async_read_secret(self):
        """Ask the lock for the secret that authorises the next command.

        The value is a short lived credential, it is deliberately never logged
        nor published as an entity attribute.
        """
        result = await self.async_lock_action(self._act_secret, [])
        outs, rejected = self.action_result(self._act_secret, result)
        if not result or not result.is_success or rejected or not outs:
            self.log.warning(
                '%s: Reading the lock secret with %s failed: %s',
                self.entity_id, self._act_secret.full_name, outs or result,
            )
            return None
        for name in SECRET_PROPERTIES:
            value = outs.get(name)
            if value not in (None, ''):
                return f'{value}'
        return None

    async def async_lock_action(self, action: MiotAction, params):
        """Run the action, keeping the whole command on one transport.

        The lock hands out a secret and expects it back. Reading it one way and
        spending it the other is asking the lock to honour a credential from a
        conversation it was not part of, and the routing in
        `Device.async_call_action` is free to change its mind between two calls
        a fraction of a second apart. So the first call of a command picks the
        transport and the rest of the command is pinned to it.

        The exception is a transport failure, which means the lock never heard
        the command: nothing it already did can be repeated, so that one is
        retried over the cloud and the command carries on from there.
        """
        result = await self.async_call_action(action, params, **self.transport_kwargs())
        if self.transport_failed(result) and self._transport != 'cloud' and self.device.cloud:
            self.log.info(
                '%s: %s did not get through over the LAN (%s), trying the cloud',
                self.entity_id, action.full_name, result.error,
            )
            self._transport = 'cloud'
            result = await self.async_call_action(action, params, cloud=True)
        elif self._transport is None:
            self._transport = result.updater if result else None
        return result

    def transport_kwargs(self):
        """Pin the call to the transport the command started on, if any."""
        if self._transport == 'cloud':
            return {'cloud': True}
        if self._transport == 'local':
            return {'local': True}
        return {}

    @staticmethod
    def transport_failed(result):
        """The command never reached the lock, as opposed to being refused."""
        return bool(result) and result.code == -1 and bool(result.error)

    @staticmethod
    def action_challenge(outs):
        """A rejection carrying a token to retry with, not an error message."""
        if not isinstance(outs, dict):
            return None
        for name in CHALLENGE_PROPERTIES:
            value = outs.get(name)
            if isinstance(value, str) and CHALLENGE_PATTERN.match(value):
                return value
        return None

    def action_result(self, action: MiotAction, result):
        """Locks answer with a result code and a message, decode them for the attributes."""
        out = result.get('out') if result else None
        if not isinstance(out, list) or len(out) != len(action.out):
            return None, False
        decoded = {}
        rejected = False
        for piid, value in zip(action.out, out):
            prop = action.service.properties.get(piid)
            if not prop:
                continue
            if prop.value_list and value is not None:
                value = prop.list_description(value)
                if prop.in_list(REJECTION_PROPERTIES) and f'{value}'.lower() in REJECTION_VALUES:
                    rejected = True
            decoded[prop.name] = value
        return decoded, rejected

    def action_params(self, action: MiotAction, key, code=None):
        """Some locks require a verification secret as the action input."""
        params = self.custom_config_list(f'{key}_action_params')
        if params is None:
            params = [self.empty_param(prop) for prop in action.in_properties()]
        else:
            variables = {'attrs': self.device.props}
            params = [
                v if not isinstance(v, str) else template(v, self.hass).async_render(variables)
                for v in params
            ]
        if code is not None:
            for index, prop in enumerate(action.in_properties()):
                if index < len(params) and prop and prop.format == 'string':
                    params[index] = code
                    break
        return params

    @staticmethod
    def int_values(values):
        return list(dict.fromkeys(int(v) for v in values or []))

    @staticmethod
    def int_value(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def empty_param(prop: MiotProperty):
        if not prop:
            return None
        if prop.format == 'string':
            return ''
        if prop.is_bool:
            return False
        return 0


class MomentaryLockEntity(LockEntity):
    """A lock entity that only ever runs one action, then locks itself again.

    Built from `lock_actions`, for an unlatch that has no state of its own: the
    door springs shut behind you, so there is nothing to read back. Home apps
    speaking through a bridge only offer lock and unlock, and this gives them a
    single meaningful tap.
    """
    _unlisten = None

    def on_init(self):
        self._attr_available = True
        # The action is the whole entity, name it after what it does.
        self._attr_name = 'Unlatch'
        self._attr_translation_key = None
        self._attr_is_locked = True
        self._attr_supported_features = LockEntityFeature(0)
        self._act_lock = None
        self._act_unlock = self._miot_action
        self._act_open = self._miot_action
        self._act_secret = self.find_action(self.custom_config_list('secret_action') or DEFAULT_SECRET_ACTIONS)
        if fmt := self.custom_config('code_format'):
            self._attr_code_format = fmt
        self._attr_extra_state_attributes.update({
            'unlock_action': self._miot_action.full_name,
            'momentary_seconds': self.momentary_seconds,
        })

    @property
    def momentary_seconds(self):
        try:
            return max(1, int(self.custom_config('momentary_seconds', DEFAULT_MOMENTARY_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_MOMENTARY_SECONDS

    def set_state(self, data: dict):
        """The state is ours alone, no property of the device reflects it."""

    async def async_lock(self, **kwargs):
        self.relock()
        self._async_write_ha_state()
        return True

    async def async_open(self, **kwargs):
        return await self.async_unlock(**kwargs)

    async def async_unlock(self, **kwargs):
        self.cancel_relock()
        success = await self.async_run_action(self._act_unlock, 'unlock', **kwargs)
        if not success:
            self.relock()
        else:
            self._attr_is_locked = False
            self._unlisten = async_call_later(self.hass, self.momentary_seconds, self.async_relock)
        self._async_write_ha_state()
        return success

    async def async_relock(self, _now=None):
        self._unlisten = None
        self.relock()
        self._async_write_ha_state()

    def relock(self):
        self.cancel_relock()
        self._attr_is_locked = True
        self._attr_is_locking = False
        self._attr_is_unlocking = False

    def cancel_relock(self):
        if self._unlisten:
            self._unlisten()
            self._unlisten = None

    async def async_will_remove_from_hass(self):
        self.cancel_relock()
        await super().async_will_remove_from_hass()


XEntity.CLS[ENTITY_DOMAIN] = LockEntity
XEntity.CLS['lock_action'] = MomentaryLockEntity
