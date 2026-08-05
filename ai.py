"""Генерация черновика отклика через CloseRouter (OpenAI-совместимый API)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты пишешь отклики на заказы биржи Kwork от лица исполнителя.

Заказчик читает двадцать откликов подряд. Почти все начинаются одинаково
и пересказывают его же ТЗ будущим временем. Твоя задача написать тот,
который выделяется, поэтому строй отклик по схеме AIDA.

A. ВНИМАНИЕ (первое предложение после «Здравствуйте!»)
Одна мысль, показывающая, что ты понял задачу глубже, чем «сделать лендинг».
Это может быть: главная сложность задачи, ключевая развилка, неочевидное
следствие, или что на самом деле определит результат.
Пример для лендинга по домам: «В таких лендингах всё решает переход от
каталога к заявке: человек смотрит проекты, а пишет только когда видит цену
и сроки рядом с фото.»
Запрещено начинать с «Посмотрел ТЗ», «Изучил ваше задание», «Готов сделать»
и с пересказа того, что нужно заказчику.

I. ИНТЕРЕС (одно-два предложения)
Конкретное решение именно этой сложности. Не список этапов работы,
а один приём, ход или структура, до которых додумался ты.
Заказчик должен подумать «а он прав».

D. ЖЕЛАНИЕ (одно-два предложения)
Что он получит на руки и чем это лучше обычного результата. Говори
про итог, а не про процесс. Плохо: «продумаю визуальную иерархию».
Хорошо: «страницу, где заявку видно с любого места без прокрутки вверх».

A. ДЕЙСТВИЕ (последнее предложение)
Один конкретный вопрос, ответ на который нужен тебе для старта, и на который
легко ответить одной строкой. Не «расскажите подробнее» и не «есть ли ТЗ».
Вопрос должен показывать, что ты уже думаешь о работе.

ЖЁСТКИЕ ОГРАНИЧЕНИЯ ПО ФОРМЕ

Максимум 5 предложений. Меньше можно, больше нельзя.
Не больше двух предложений подряд, начинающихся с глагола.
Не перечисляй этапы работы через запятую: «соберу карточки, выстрою формы,
обращу внимание на адаптив» это худшее, что можно написать.
Не пересказывай ТЗ. Заказчик его написал десять минут назад.
Никаких «также», «кроме того», «отдельно».

ОБЛАСТЬ ЗАДАЧИ

Определи, о чём заказ, и говори на языке этой области: монтаж, дизайн,
тексты, таблицы, разработка. Если в ТЗ нет ни слова про программирование,
слова «код», «скрипт», «стек», «API» запрещены полностью.

Отклик строится от задачи заказчика. Профиль дан фоном для тона и уровня,
это НЕ список разрешённых тем. Не пиши «это не мой профиль» и не отказывайся.

ЗАПРЕЩЕНО

Проверяемые факты о себе: годы опыта, портфолио, число заказов, прошлые
клиенты, образование, сертификаты, отзывы. Всё это видно в профиле рядом
с откликом. «Сделаю так-то» можно про любую задачу, «я делал это сто раз» нельзя.
Сроки и время: дни, недели, дедлайны, «быстро», «оперативно».
Деньги: цена, бюджет, стоимость, оплата, предоплата.
Тире любого вида. Перестраивай предложение.
Эмодзи, markdown, списки, заголовки.
Восклицательные знаки, кроме приветствия.
Штампы: «имею богатый опыт», «качественно и в срок», «готов приступить»,
«буду рад сотрудничеству», «работаю на результат», «под ключ».

ЛИЧНЫЕ ДАННЫЕ

Имя, город, статус занятости упоминай только если заказчик спросил или это
по делу. Возраст не упоминай никогда, если о нём не спросили.

ТОН

Живой человек, говорящий по делу. Короткие предложения, обращение на «вы».
Первое слово всегда «Здравствуйте!».

Отвечай SKIP одной строкой только если задачу физически невозможно выполнить
удалённо или нужна лицензия. Незнакомая область поводом для SKIP не является.

Верни только текст отклика. Без кавычек, пояснений и markdown."""

REPAIR_PROMPT = (
    "Перепиши этот текст так, чтобы в нём не осталось ни одного тире и дефиса "
    "между словами. Смысл, объём и тон сохрани. Верни только исправленный текст."
)

# Все виды тире и дефисов, которые встречаются в выводе моделей.
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u2043\uFE58\uFE63\uFF0D"
_DASH_TABLE = {ord(ch): "-" for ch in _DASHES}

_GREETING = re.compile(
    r"^\W*(здравствуй|добрый\s+(день|вечер)|доброе\s+утро|доброго|приветствую|привет|салют|хай)",
    re.IGNORECASE,
)
_SPACED_DASH = re.compile(r"\s+-\s+")
_LEADING_DASH = re.compile(r"^\s*-\s+", re.MULTILINE)


def ensure_greeting(text: str, greeting: str) -> str:
    """Модель регулярно забывает поздороваться, поэтому проверяем сами."""
    if not greeting or _GREETING.match(text):
        return text
    return f"{greeting} {text.lstrip()}"


def dashes_to_hyphen(text: str) -> str:
    """Только замена тире, без чистки markdown: годится для чужого текста."""
    return text.translate(_DASH_TABLE)


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


# Глаголы будущего времени, с которых начинается «список этапов».
_VERB_START = re.compile(
    r"^\s*(сделаю|соберу|продумаю|обращу|выстрою|подберу|настрою|напишу|создам|"
    r"разработаю|подготовлю|проверю|добавлю|учту|реализую|оформлю|отрисую|сверстаю)",
    re.IGNORECASE,
)
_BANNED_OPENERS = re.compile(
    r"^\s*(здравствуйте!?\s*)?(посмотрел|изучил|ознакомился|прочитал|прочёл|"
    r"готов\s+(сделать|выполнить|взяться))",
    re.IGNORECASE,
)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def quality_report(text: str) -> dict[str, Any]:
    """Быстрая проверка на болезни, которые видно без модели."""
    parts = sentences(text)
    body = parts[1:] if parts else []          # без приветствия
    verb_runs, run = 0, 0
    for s in body:
        run = run + 1 if _VERB_START.match(s) else 0
        verb_runs = max(verb_runs, run)
    longest = max((len(s.split()) for s in parts), default=0)
    return {
        "sentences": len(parts),
        "too_long": len(parts) > 6,
        "verb_run": verb_runs,
        "listy": verb_runs >= 3 or any(s.count(",") >= 4 for s in parts),
        "banned_opener": bool(_BANNED_OPENERS.match(text)),
        "longest_sentence": longest,
    }


class DraftWriter:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        profile: str = "",
        enabled: bool = True,
        repair_dashes: bool = True,
        greeting: str = "Здравствуйте!",
        stats_hook: Any = None,
        timeout: float = 90.0,
        retries: int = 2,
    ) -> None:
        self.enabled = enabled
        self.greeting = greeting
        self.model = model
        self.profile = profile
        self.repair_dashes = repair_dashes
        self.stats_hook = stats_hook
        self.last_error = ""
        # Клиент создаём всегда, когда есть ключ: enabled переключается на лету
        # из панели бота, пересоздавать соединение ради этого не нужно.
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self._client = (
            AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,          # повторы делаем сами, с логом и паузой
            )
            if api_key
            else None
        )

    @property
    def has_client(self) -> bool:
        return self._client is not None

    def _count(self, event: str) -> None:
        if self.stats_hook is not None:
            try:
                self.stats_hook(event)
            except Exception:
                logger.debug("Счётчик %s не записался", event)

    def _user_prompt(self, project: dict[str, Any]) -> str:
        # Задача идёт первой: она определяет содержание отклика.
        parts = [
            "ЗАДАЧА ЗАКАЗЧИКА (главное, от этого пляши):\n"
            f"Заголовок: {project.get('title') or 'нет'}\n"
            f"Описание:\n{(project.get('description') or 'нет').strip()}"
        ]
        if self.profile:
            parts.append(
                "ФОН ОБ ИСПОЛНИТЕЛЕ (для тона и уровня, НЕ список разрешённых тем):\n"
                f"{self.profile}"
            )
        parts.append(
            "Напиши отклик именно на эту задачу. Если она из области, которой нет "
            "в фоне, всё равно разбери её по существу и предложи план."
        )
        return "\n\n".join(parts)

    async def _complete(self, messages: list[dict[str, str]], temperature: float) -> str:
        last: Exception | None = None

        for attempt in range(1, self.retries + 2):
            started = time.monotonic()
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=600,
                    temperature=temperature,
                    messages=messages,
                )
            except APITimeoutError as err:
                last = err
                logger.warning(
                    "Попытка %s из %s: модель %s не ответила за %.0f с",
                    attempt,
                    self.retries + 1,
                    self.model,
                    self.timeout,
                )
            except APIConnectionError as err:
                last = err
                logger.warning(
                    "Попытка %s из %s: нет связи с %s (%s)",
                    attempt,
                    self.retries + 1,
                    self.base_url,
                    err.__class__.__name__,
                )
            except APIStatusError as err:
                # Ключ, лимиты и опечатка в модели повтором не лечатся.
                logger.error(
                    "CloseRouter ответил %s: %s",
                    err.status_code,
                    str(err)[:200],
                )
                raise
            else:
                logger.info(
                    "Ответ от %s за %.1f с", self.model, time.monotonic() - started
                )
                return (response.choices[0].message.content or "").strip()

            if attempt <= self.retries:
                await asyncio.sleep(2 * attempt)

        raise last  # type: ignore[misc]

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
        except APIStatusError as err:
            self._count("llm_errors")
            self.last_error = f"HTTP {err.status_code}: {str(err)[:120]}"
            return None
        except Exception as err:
            logger.error(
                "Не удалось сгенерировать отклик: %s. Проверьте CLOSEROUTER_BASE_URL, "
                "ключ и доступность сервиса с этого сервера.",
                err.__class__.__name__,
            )
            self._count("llm_errors")
            self.last_error = f"{err.__class__.__name__} при обращении к {self.base_url}"
            return None

        if not raw:
            logger.warning("CloseRouter вернул пустой ответ")
            self._count("llm_errors")
            return None
        if raw.upper().startswith("SKIP"):
            logger.info(
                "SKIP: задача вне профиля, черновик не нужен (%s)",
                (project.get("title") or "")[:60],
            )
            self._count("skips")
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

        if not text:
            return None

        text = ensure_greeting(text, self.greeting)
        report = quality_report(text)
        if report["listy"] or report["too_long"] or report["banned_opener"]:
            logger.info(
                "Черновик слабоват: предложений %s, глаголов подряд %s, шаблонный зачин %s",
                report["sentences"],
                report["verb_run"],
                report["banned_opener"],
            )
        self._count("drafts")
        return text
