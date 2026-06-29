import weakref
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.const import UnitOfTemperature
from homeassistant.const import TEMPERATURE
from .const import DOMAIN
from . import api


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities) -> bool:
    haier_object = hass.data[DOMAIN][config_entry.entry_id]
    entities = []
    for device in haier_object.devices:
        entities.extend(device.create_entities_sensor())
    if entities:
        async_add_entities(entities)
        haier_object.write_ha_state()
    return True


class HaierSensor(SensorEntity):

    def __init__(self, device: api.HaierDevice):
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
    def native_value(self):
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            return None
        return value


class HaierREFTemperatureSensor(HaierSensor):
    _attr_device_class = TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature"
        self._attr_name = f"{device.device_name} Температура в помещении"


class HaierREFFridgeTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_fridge_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_temperature"
        self._attr_name = f"{device.device_name} Температура холодильной камеры"


class HaierREFFreezerTemperatureSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "current_freezer_temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_temperature"
        self._attr_name = f"{device.device_name} Температура морозильной камеры"


class HaierREFFridgeModeSensor(HaierREFTemperatureSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "fridge_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_fridge_mode"
        self._attr_name = f"{device.device_name} Режим холодильной камеры"

    @property
    def native_value(self) -> float:
        return float(getattr(self._device, self._device_attr_name, 0.0))


class HaierREFFreezerModeSensor(HaierREFFridgeModeSensor):

    def __init__(self, device: api.HaierREF):
        super().__init__(device)
        self._device_attr_name = "freezer_mode"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_freezer_mode"
        self._attr_name = f"{device.device_name} Режим морозильной камеры"


class HaierWMProgramRemainingTimeSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "program_remaining_time"
        # Keep the original unique_id so existing dashboards/entities migrate to
        # the total program countdown instead of becoming unavailable.
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_remaining_time"
        self._attr_name = f"{device.device_name} Оставшееся время программы"
        self._attr_native_unit_of_measurement = "мин"


class HaierWMCycleRemainingTimeSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "cycle_remaining_time"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_cycle_remaining_time"
        self._attr_name = f"{device.device_name} Оставшееся время этапа"
        self._attr_native_unit_of_measurement = "мин"


# Backward-compatible class name for any imports/tests that still reference it.
HaierWMRemainingTimeSensor = HaierWMProgramRemainingTimeSensor


class HaierWMStatusSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "status"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_status"
        self._attr_name = f"{device.device_name} Статус"

    @property
    def native_value(self):
        value = getattr(self._device, self._device_attr_name, None)
        if value in (None, "None", "unknown"):
            return None
        return value


class HaierWMProgramSensor(HaierWMStatusSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program_sensor"
        self._attr_name = f"{device.device_name} Программа"


class HaierWMSelectedProgramSensor(HaierWMStatusSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "selected_program"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_selected_program"
        self._attr_name = f"{device.device_name} Выбранная программа"


class HaierWMTemperatureSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "temperature"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_temperature_sensor"
        self._attr_name = f"{device.device_name} Температура"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS


class HaierWMSpinSpeedSensor(HaierSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "spin_speed"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_spin_speed_sensor"
        self._attr_name = f"{device.device_name} Скорость отжима"
        self._attr_native_unit_of_measurement = "rpm"


class HaierWMEnergySensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "energy"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_energy"
        self._attr_name = f"{device.device_name} Энергия"
        self._attr_native_unit_of_measurement = "kWh"


class HaierWMPowerSensor(HaierSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "power"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_power"
        self._attr_name = f"{device.device_name} Мощность"
        self._attr_native_unit_of_measurement = "W"




class HaierWMWaterRawSensor(HaierSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "water_raw"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_water_raw"
        self._attr_name = f"{device.device_name} Водомер raw"



class HaierWMProgramProgressSensor(HaierSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "program_progress"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_program_progress"
        self._attr_name = f"{device.device_name} Прогресс программы"
        self._attr_native_unit_of_measurement = "%"


class HaierWMRinseCountSensor(HaierSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "rinse_count"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_rinse_count"
        self._attr_name = f"{device.device_name} Осталось полосканий"


class HaierWMDirtLevelSensor(HaierWMStatusSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "dirt_level"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_dirt_level"
        self._attr_name = f"{device.device_name} Уровень загрязнения"


class HaierWMRawDiagnosticSensor(HaierSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: api.HaierWM, attr_name: str, title: str):
        super().__init__(device)
        self._device_attr_name = attr_name
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_{attr_name}"
        self._attr_name = f"{device.device_name} {title}"

class HaierWMPhaseSensor(HaierWMStatusSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "phase"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_phase"
        self._attr_name = f"{device.device_name} Этап программы"




class HaierWMPhaseCodeSensor(HaierSensor):
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "phase_code"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_phase_code"
        self._attr_name = f"{device.device_name} Код этапа"


class HaierWMLegacyPhaseCodeSensor(HaierSensor):
    _attr_entity_registry_enabled_default = False

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "legacy_phase_code"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_legacy_phase_code"
        self._attr_name = f"{device.device_name} Код этапа 90"


class HaierWMDoorLockSensor(HaierWMStatusSensor):

    def __init__(self, device: api.HaierWM):
        super().__init__(device)
        self._device_attr_name = "door_lock"
        self._attr_unique_id = f"{device.device_id}_{device.device_model}_door_lock"
        self._attr_name = f"{device.device_name} Блокировка двери"
