"""Mappings and small helpers for Haier Evo washing machines.

The RU Evo API exposes WM properties mostly as numeric codes.  This module keeps
all WM-specific code-to-meaning knowledge out of the generic device class so AC
and future appliance support can stay independent.
"""

from __future__ import annotations

PROGRAM_NAMES: dict[str, str] = {
    "1": "Программа 1",
    "13": "Спортивная",
    "57": "Программа 57",
}

# Code 0 is the program selected on the physical washer panel / knob.
# It updates while turning the selector and is intentionally kept separate from
# code 71, which represents the current/running cloud program.
PANEL_PROGRAM_NAMES: dict[str, str] = {
    "1": "Пуховые вещи",
    "2": "Смешанная стирка",
    "3": "Очистка барабана",
    "4": "Быстрая 15'",
    "6": "Отжим",
    "7": "Джинсы",
    "9": "Детская одежда",
    "10": "Хлопок",
    "11": "Синтетика",
    "15": "Деликатная",
    "17": "Шерсть",
    "21": "Спортивная",
    "22": "Бережная",
    "23": "Ежедневная",
    "45": "Особая программа",
    "49": "Гигиена",
    "56": "Авто",
    "59": "УФ-обработка",
    "71": "Освежить",
    "86": "Смешанная",
}

STATUS_NAMES: dict[str, str] = {
    "0": "Выключена",
    "1": "Ожидание",
    "2": "Ожидание",
    "3": "Пауза",
    "4": "Выполняется",
    "5": "Завершено",
    "6": "Ошибка",
    "IN_PROGRESS": "Выполняется",
    "PAUSED": "Пауза",
    "FINISHED": "Завершено",
    "COMPLETED": "Завершено",
    "READY": "Ожидание",
    "IDLE": "Ожидание",
    "ERROR": "Ошибка",
    "PROGRAM_UPDATED": "Выполняется",
}

# For WM_BASE devices code 90 stays at 1 during the whole run, so it is not a
# reliable phase source. Code 18 follows the actual program stage.
#
# This table is intentionally close to hon-revived WASHING_PR_PHASE, but keeps
# the RU Evo values we have confirmed from real HW70-BP12337U1 dumps:
# 5/6 = washing at the beginning of the quick program, 8/9 = rinse,
# 10 = drain before spin, 11/12 = spin, 14/15 = finished.
PHASE_NAMES: dict[str, str] = {
    "0": "Нет",
    "1": "Стирка",
    "2": "Стирка",
    "3": "Распределение белья",
    "4": "Полоскание",
    "5": "Стирка",
    "6": "Стирка",
    "7": "Слив / переход",
    "8": "Полоскание",
    "9": "Полоскание",
    "10": "Слив",
    "11": "Отжим",
    "12": "Отжим",
    "13": "Завершение",
    "14": "Ожидает выгрузки белья",
    "15": "Ожидает выгрузки белья",
    "16": "Стирка",
    "17": "Полоскание",
    "18": "Полоскание",
    "19": "Отложенный старт",
    "20": "Проворачивание",
    "24": "Освежение",
    "25": "Стирка",
    "26": "Нагрев",
    "27": "Стирка",
}

# Reference table from hon-revived. Kept here to simplify future diagnostics for
# other models/regions where the RU override above does not match.
HON_REFERENCE_PHASE_NAMES: dict[str, str] = {
    "0": "ready",
    "1": "washing",
    "2": "washing",
    "3": "spin",
    "4": "rinse",
    "5": "rinse",
    "6": "rinse",
    "7": "drying",
    "8": "drying",
    "9": "steam",
    "10": "ready",
    "11": "spin",
    "12": "weighting",
    "13": "weighting",
    "14": "washing",
    "15": "washing",
    "16": "washing",
    "17": "rinse",
    "18": "rinse",
    "19": "scheduled",
    "20": "tumbling",
    "24": "refresh",
    "25": "washing",
    "26": "heating",
    "27": "washing",
}

DOOR_LOCK_NAMES: dict[str, str] = {
    "0": "Разблокирована",
    "1": "Заблокирована",
    "false": "Разблокирована",
    "true": "Заблокирована",
}

DIRT_LEVEL_NAMES: dict[str, str] = {
    "0": "Не задан",
    "1": "Низкий",
    "2": "Средний",
    "3": "Высокий",
}

STEAM_FUNCTION_NAMES: dict[str, str] = {
    "0": "Нет",
    "1": "Да",
    "false": "Нет",
    "true": "Да",
}

ON_OFF_NAMES: dict[str, str] = {
    "0": "Выключено",
    "1": "Включено",
    "false": "Выключено",
    "true": "Включено",
}

CODE_TO_FIELD: dict[str, str] = {
    "67": "status",
    "71": "program",
    "50": "temperature",
    "63": "spin_speed",
    "33": "program_remaining_time",
    "51": "cycle_remaining_time",
    "40": "energy",
    "38": "power",
    "39": "water_raw",
    "0": "selected_program",
    "7": "steam_function",
    "15": "delayed_start_enabled",
    "32": "delayed_start_hours",
    "59": "anti_crease",
    "34": "raw_34",
    "37": "program_progress",
    "46": "rinse_count",
    "47": "dirt_level",
    "61": "raw_61",
    "68": "raw_68",
    "18": "phase",
    "90": "legacy_phase_code",
    "21": "door_lock",
}

NUMERIC_FIELDS: set[str] = {
    "temperature",
    "spin_speed",
    "program_remaining_time",
    "delayed_start_hours",
    "cycle_remaining_time",
    "program_duration",
    "remaining_time",
    "energy",
    "power",
    "water_raw",
    "raw_31",
    "raw_34",
    "raw_35",
    "raw_36",
    "raw_95",
    "raw_195",
    "program_progress",
    "rinse_count",
    "raw_61",
    "raw_68",
    "raw_88",
    "raw_89",
    "raw_91",
    "raw_94",
    "raw_117",
    "raw_205",
}

TEXT_FIELDS: set[str] = {"status", "program", "selected_program", "phase", "door_lock", "dirt_level", "steam_function", "delayed_start_enabled", "anti_crease"}


def clean_value(value):
    if value in (None, "", "None", "none", "unknown"):
        return None
    return value


def as_number(value):
    value = clean_value(value)
    if value is None:
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except Exception:
        return None


def fallback_name_by_code(code: str) -> str | None:
    return CODE_TO_FIELD.get(str(code))


def program_name_from_code(value):
    value = clean_value(value)
    if value is None:
        return None
    key = str(value)
    return PROGRAM_NAMES.get(key) or display_program_value(f"program_{key}")




def selected_program_name_from_code(value):
    value = clean_value(value)
    if value is None:
        return None
    key = str(value)
    return PANEL_PROGRAM_NAMES.get(key, f"Программа ({key})")

def display_program_value(value):
    if value is None:
        return None
    text = str(value)
    if text in PROGRAM_NAMES:
        return PROGRAM_NAMES[text]
    if text.startswith("program_"):
        code = text.split("_", 1)[1]
        return PROGRAM_NAMES.get(code, f"Программа {code}")
    return text


def program_display_to_config_value(value):
    if value is None:
        return value
    text = str(value)
    for code, title in PROGRAM_NAMES.items():
        if text == title:
            return f"program_{code}"
    if text.startswith("Программа "):
        return f"program_{text.split(' ', 1)[1]}"
    return text


def map_value(name: str, value):
    text = str(value) if value is not None else None
    if name == "status":
        return STATUS_NAMES.get(text, value)
    if name == "program":
        return program_name_from_code(text)
    if name == "selected_program":
        return selected_program_name_from_code(text)
    if name == "phase":
        return PHASE_NAMES.get(text, f"Этап ({text})" if text not in (None, "None") else None)
    if name == "door_lock":
        return DOOR_LOCK_NAMES.get(text, value)
    if name == "dirt_level":
        if text in DIRT_LEVEL_NAMES:
            return DIRT_LEVEL_NAMES[text]
        return f"Особый ({text})" if text not in (None, "None") else None
    if name == "steam_function":
        return STEAM_FUNCTION_NAMES.get(text, value)
    if name in ("delayed_start_enabled", "anti_crease"):
        return ON_OFF_NAMES.get(text, value)
    return value


def is_active_phase(phase_code) -> bool:
    try:
        phase = int(phase_code)
    except Exception:
        return False
    return 1 <= phase <= 13


def is_finished_phase(phase_code) -> bool:
    try:
        return int(phase_code) in (14, 15)
    except Exception:
        return False
