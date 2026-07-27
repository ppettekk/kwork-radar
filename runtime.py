"""Изменяемые на лету параметры.

Значения из .env служат базой, поверх них ложатся правки из базы данных,
сделанные через бота. Поэтому настройки переживают перезапуск, а .env
остаётся нетронутым и годится как исходное состояние.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from config import Settings
from storage import Storage


@dataclass(frozen=True, slots=True)
class Field:
    kind: str                      # int | bool | list_int | list_str | str
    label: str
    hint: str = ""
    minimum: int | None = None
    steps: tuple[int, ...] = ()    # кнопки быстрых прибавок
    presets: tuple[str, ...] = ()  # кнопки готовых значений


FIELDS: dict[str, Field] = {
    "paused": Field("bool", "Пауза", "опрос биржи остановлен"),
    "llm_enabled": Field("bool", "Черновики ИИ", "генерировать отклики"),
    "max_offers": Field(
        "int", "Максимум откликов", "у проекта, 0 и больше",
        minimum=0, steps=(-1, 1), presets=("1", "3", "5", "10"),
    ),
    "min_price": Field(
        "int", "Бюджет от", "рублей, 0 = без нижней границы",
        minimum=0, steps=(-1000, -500, 500, 1000), presets=("0", "1000", "3000", "5000"),
    ),
    "max_price": Field(
        "int", "Бюджет до", "рублей, 0 = без верхней границы",
        minimum=0, steps=(-5000, -1000, 1000, 5000), presets=("0", "10000", "30000"),
    ),
    "hiring_from": Field(
        "int", "Процент найма от", "0 до 100, отсекает заказчиков без найма",
        minimum=0, steps=(-10, 10), presets=("0", "30", "50", "70"),
    ),
    "poll_interval": Field(
        "int", "Интервал опроса", "секунд, не меньше 30",
        minimum=30, steps=(-15, 15), presets=("30", "45", "60", "120"),
    ),
    "categories": Field(
        "list_int", "Рубрики", "id через запятую, all = все, пусто = избранные",
    ),
    "include_keywords": Field(
        "list_str", "Ключевые слова", "через запятую, пусто = любые",
    ),
    "exclude_keywords": Field("list_str", "Стоп-слова", "через запятую"),
    "llm_model": Field(
        "str", "Модель", "идентификатор из CloseRouter",
        presets=(
            "openai/gpt-5.4-mini",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-terra",
            "google/gemini-3-flash-preview",
        ),
    ),
}


def _parse(field: Field, raw: str) -> Any:
    raw = raw.strip()
    if field.kind == "bool":
        low = raw.lower()
        if low in {"on", "1", "true", "да", "вкл", "yes"}:
            return True
        if low in {"off", "0", "false", "нет", "выкл", "no"}:
            return False
        raise ValueError("нужно on или off")
    if field.kind == "int":
        if not raw.lstrip("-").isdigit():
            raise ValueError("нужно целое число")
        value = int(raw)
        if field.minimum is not None and value < field.minimum:
            raise ValueError(f"не меньше {field.minimum}")
        return value
    if field.kind == "list_int":
        if not raw or raw.lower() in {"-", "пусто", "none"}:
            return []
        if raw.lower() == "all":
            return ["all"]
        try:
            return [int(x.strip()) for x in raw.split(",") if x.strip()]
        except ValueError as err:
            raise ValueError("нужны числа через запятую") from err
    if field.kind == "list_str":
        if not raw or raw.lower() in {"-", "пусто", "none"}:
            return []
        return [x.strip().lower() for x in raw.split(",") if x.strip()]
    return raw


def _dump(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ",".join(str(x) for x in value)
    return str(value)


def show(key: str, value: Any) -> str:
    """Человекочитаемое представление для панели."""
    field = FIELDS[key]
    if field.kind == "bool":
        return "включено" if value else "выключено"
    if field.kind == "list_int":
        if not value:
            return "избранные"
        if value == ["all"]:
            return "все"
        return f"{len(value)} шт: {', '.join(str(x) for x in value[:6])}" + (
            "..." if len(value) > 6 else ""
        )
    if field.kind == "list_str":
        return ", ".join(value) if value else "нет"
    if key in {"min_price", "max_price"} and not value:
        return "без ограничения"
    if key == "hiring_from" and not value:
        return "без ограничения"
    return str(value)


class Runtime:
    """Читает .env один раз, дальше живёт правками из базы."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self._settings = settings
        self._storage = storage
        self._base: dict[str, Any] = {
            "paused": False,
            "llm_enabled": settings.llm_enabled,
            "max_offers": settings.max_offers,
            "min_price": settings.min_price,
            "max_price": settings.max_price,
            "hiring_from": settings.hiring_from,
            "poll_interval": settings.poll_interval,
            "categories": list(settings.categories),
            "include_keywords": list(settings.include_keywords),
            "exclude_keywords": list(settings.exclude_keywords),
            "llm_model": settings.llm_model,
        }
        self._values = dict(self._base)
        self._load()

    def _load(self) -> None:
        for key, raw in self._storage.get_settings().items():
            if key not in FIELDS:
                continue
            try:
                self._values[key] = _parse(FIELDS[key], raw)
            except ValueError:
                continue

    def __getattr__(self, item: str) -> Any:
        try:
            return self.__dict__["_values"][item]
        except KeyError as err:
            raise AttributeError(item) from err

    def get(self, key: str) -> Any:
        return self._values[key]

    def set(self, key: str, raw: str) -> Any:
        if key not in FIELDS:
            raise ValueError(f"неизвестный параметр: {key}")
        value = _parse(FIELDS[key], raw)
        self._values[key] = value
        self._storage.set_setting(key, _dump(value))
        return value

    def bump(self, key: str, delta: int) -> Any:
        """Прибавка кнопкой, с нижней границей поля."""
        field = FIELDS[key]
        if field.kind != "int":
            raise ValueError(f"{key} нельзя менять шагом")
        value = max(field.minimum or 0, int(self._values[key]) + delta)
        return self.set(key, str(value))

    def clear(self, key: str) -> Any:
        if FIELDS[key].kind not in {"list_int", "list_str"}:
            raise ValueError(f"{key} нельзя очистить")
        return self.set(key, "")

    def toggle(self, key: str) -> bool:
        if FIELDS[key].kind != "bool":
            raise ValueError(f"{key} нельзя переключить")
        return self.set(key, "off" if self._values[key] else "on")

    def reset(self, key: str) -> Any:
        if key not in FIELDS:
            raise ValueError(f"неизвестный параметр: {key}")
        self._values[key] = self._base[key]
        self._storage.del_setting(key)
        return self._values[key]

    def is_overridden(self, key: str) -> bool:
        return self._values[key] != self._base[key]

    def items(self) -> list[tuple[str, Field, Any, bool]]:
        return [
            (key, field, self._values[key], self.is_overridden(key))
            for key, field in FIELDS.items()
        ]
