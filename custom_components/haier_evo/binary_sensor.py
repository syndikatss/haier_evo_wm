import weakref
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_binary_sensor())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, device: api.HaierDevice) -> None:
        self._device = weakref.proxy(device)
        self._device_attr_name = None

        device.add_write_ha_state_callback(self.async_write_ha_state)

    @property
    def device_info(self) -> dict:
        return self._device.device_info

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def is_on(self) -> bool:
        return getattr(self._device, self._device_attr_name, False)


class HaierREFBinarySensor(HaierBinarySensor):
    _attr_icon = "mdi:fridge-outline"


class HaierREFDoorSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "door_open"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_open"
        self._attr_name = f"{device.device_name} Дверь"


class HaierREFVacationSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "vacation_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_vacation"
        self._attr_name = f"{device.device_name} Режим Отпуск"


class HaierREFSuperFreezeSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_freeze"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_freeze"
        self._attr_name = f"{device.device_name} Супер-заморозка"


class HaierREFSuperCoolingSensor(HaierREFBinarySensor):

    def __init__(self, device: api.HaierREF) -> None:
        super().__init__(device)
        self._device_attr_name = "super_cooling"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_super_cooling"
        self._attr_name = f"{device.device_name} Супер-охлаждение"


class HaierWMBinarySensor(HaierBinarySensor):
    _attr_icon = "mdi:washing-machine"

    @property
    def is_on(self) -> bool:
        value = getattr(self._device, self._device_attr_name, None)
        return str(value) in ("1", "true", "True", "Да", "Включено", "Заблокирована")


class HaierWMSteamBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "steam_function"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_steam_function_binary"
        self._attr_name = f"{device.device_name} Функция пара"
        self._attr_icon = "mdi:weather-fog"


class HaierWMAntiCreaseBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "anti_crease"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_anti_crease_binary"
        self._attr_name = f"{device.device_name} Антисминание"
        self._attr_icon = "mdi:iron-outline"


class HaierWMDelayedStartBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "delayed_start_enabled"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_delayed_start_binary"
        self._attr_name = f"{device.device_name} Отложенный старт"
        self._attr_icon = "mdi:clock-start"


class HaierWMSoundNotificationBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "sound_notification"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_sound_notification_binary"
        self._attr_name = f"{device.device_name} Звуковое оповещение"
        self._attr_icon = "mdi:volume-high"

    @property
    def is_on(self) -> bool | None:
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            return None
        # Confirmed on HW70-BP12337U1: raw 0 means sound is enabled, raw 1 means muted.
        return str(value) in ("0", "Включено", "Да", "false", "False")


class HaierWMRemoteControlBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "remote_control"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remote_control_binary"
        self._attr_name = f"{device.device_name} Удалённое управление"
        self._attr_icon = "mdi:remote"


class HaierWMStandbyModeBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "standby_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_standby_mode_binary"
        self._attr_name = f"{device.device_name} Режим ожидания"
        self._attr_icon = "mdi:power-sleep"

    @property
    def is_on(self) -> bool | None:
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            return None
        # Confirmed on HW70-BP12337U1: raw 0 means standby mode is enabled, raw 1 means disabled.
        return str(value) in ("0", "Включено", "Да", "false", "False")


class HaierWMChildLockBinarySensor(HaierWMBinarySensor):

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "child_lock"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_child_lock_binary"
        self._attr_name = f"{device.device_name} Детский замок"
        self._attr_icon = "mdi:account-lock"


class HaierWMDoorLockBinarySensor(HaierWMBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "door_lock"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_lock_binary"
        self._attr_name = f"{device.device_name} Блокировка дверцы"
        self._attr_icon = "mdi:door-closed-lock"

    @property
    def is_on(self) -> bool | None:
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            return None
        return not super().is_on


class HaierWMDoorOpenBinarySensor(HaierWMBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(self, device: api.HaierWM) -> None:
        super().__init__(device)
        self._device_attr_name = "door_open"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_open_binary"
        self._attr_name = f"{device.device_name} Дверца"
        self._attr_icon = "mdi:door-open"

    @property
    def is_on(self) -> bool | None:
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            value = getattr(self._device, "raw_31", None)
        if value in (None, "None", "unknown"):
            return None
        return str(value) in ("1", "true", "True", "Да", "Открыта")
