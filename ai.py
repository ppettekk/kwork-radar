"""Генерация черновика отклика через CloseRouter (OpenAI-совместимый API)."""

from __future__ import annotations

import logging
import re
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты пишешь отклики на проекты биржи Kwork от лица фрилансера-разработчика.

Правила:
- Пиши от первого лица, на «вы», по-русски.
- 4-7 предложений, сплошным текстом, без списков и заголовков.
- Первая фраза о самой задаче, а не приветствие-вода. Не начинай со слов
  «Здравствуйте! Меня зовут» и не пересказывай ТЗ заказчику обратно.
- Покажи, что понял задачу: назови стек или подход, которым будешь решать.
- Если в ТЗ есть неоднозначность, задай ровно один уточняющий вопрос в конце.

Строго запрещено:
- Любые упоминания времени и сроков: дни, недели, дедлайны, «быстро»,
  «оперативно», «сегодня же», «в ближайшее время».
- Любые упоминания денег: цена, бюджет, стоимость, оплата, предоплата, скидка.
- Тире любого вида: ни длинное, ни короткое, ни дефис вместо связки.
  Перестраивай предложение так, чтобы тире не требовалось: используй запятую,
  двоеточие, точку или связку «это».
- Эмодзи, markdown, восклицательные знаки.
- Штампы: «имею богатый опыт», «качественно и в срок», «готов приступить
  немедленно», «буду рад сотрудничеству», «в кратчайшие сроки».

Если задача явно вне компетенций из профиля, ответь одной строкой: SKIP

Верни только текст отклика. Без кавычек, без пояснений, без markdown."""

REPAIR_PROMPT = (
    "Перепиши этот текст так, чтобы в нём не осталось ни одного тире и дефиса "
    "между словами. Смысл, объём и тон сохрани. Верни только исправленный текст."
)

# Все виды тире и дефисов, которые встречаются в выводе моделей.
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\uFE58\uFE63\uFF0D"
_DASH_TABLE = {ord(ch): "-" for ch in _DASHES}

_SPACED_DASH = re.compile(r"\s+-\s+")
_LEADING_DASH = re.compile(r"^\s*-\s+", re.MULTILINE)


def normalize(text: str) -> str:
    """Приводит текст к виду без длинных тире и markdown-разметки."""
    text = text.translate(_DASH_TABLE)
    text = re.sub(r"\*\*|__|`|#+\s*", "", text)
    text = _LEADING_DASH.sub("", text)          # маркеры списков
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_dash(text: str) -> bool:
    """Остался ли дефис в роли тире (между словами, с пробелами)."""
    return bool(_SPACED_DASH.search(text))


class DraftWriter:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        profile: str = "",
        enabled: bool = True,
        repair_dashes: bool = True,
    ) -> None:
        self.enabled = enabled
        self.model = model
        self.profile = profile
        self.repair_dashes = repair_dashes
        self._client = (
            AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=45.0)
            if enabled
            else None
        )

    def _user_prompt(self, project: dict[str, Any]) -> str:
        parts = []
        if self.profile:
            parts.append(f"ПРОФИЛЬ ИСПОЛНИТЕЛЯ:\n{self.profile}")
        parts.append(
            "ПРОЕКТ:\n"
            f"Заголовок: {project.get('title') or 'нет'}\n"
            f"Описание:\n{(project.get('description') or 'нет').strip()}"
        )
        parts.append("Напиши отклик.")
        return "\n\n".join(parts)

    async def _complete(self, messages: list[dict[str, str]], temperature: float) -> str:
        response = await self._client.chat.completions.create(
            model=self.model,
            max_tokens=600,
            temperature=temperature,
            messages=messages,
        )
        return (response.choices[0].message.content or "").strip()

    async def draft(self, project: dict[str, Any], temperature: float = 0.7) -> str | None:
        if not self.enabled or self._client is None:
            return None

        try:
            raw = await self._complete(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._user_prompt(project)},
                ],
                temperature,
            )
        except Exception:
            logger.exception("CloseRouter: не удалось сгенерировать отклик")
            return None

        if not raw or raw.upper().startswith("SKIP"):
            return None

        text = normalize(raw)

        # Модель всё же поставила тире: один дешёвый проход на переписывание.
        if self.repair_dashes and has_dash(text):
            try:
                repaired = await self._complete(
                    [
                        {"role": "system", "content": REPAIR_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    0.3,
                )
                if repaired:
                    candidate = normalize(repaired)
                    if not has_dash(candidate):
                        text = candidate
            except Exception:
                logger.warning("CloseRouter: проход по тире не удался, отдаю как есть")

        return text or None
