"""Kwork Radar. Монитор новых проектов с малым числом откликов.

Опрашивает биржу Kwork, отбирает свежие проекты по фильтрам и присылает
их в Telegram вместе с готовым черновиком отклика.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sys
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from kwork import Kwork

from ai import DraftWriter
from config import Settings
from storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("radar")

PROJECT_URL = "https://kwork.ru/projects/{id}/view"
OFFER_URL = "https://kwork.ru/new_offer?project={id}"
DESCRIPTION_LIMIT = 700
TG_LIMIT = 3900


# --------------------------------------------------------------------------- #
# Фильтрация
# --------------------------------------------------------------------------- #
def passes_filters(project: dict[str, Any], settings: Settings) -> tuple[bool, str]:
    offers = project.get("offers") or 0
    if offers > settings.max_offers:
        return False, f"откликов {offers} > {settings.max_offers}"

    price = project.get("price") or 0
    if settings.min_price and price < settings.min_price:
        return False, f"бюджет {price} < {settings.min_price}"
    if settings.max_price and price > settings.max_price:
        return False, f"бюджет {price} > {settings.max_price}"

    haystack = f"{project.get('title') or ''} {project.get('description') or ''}".lower()

    for word in settings.exclude_keywords:
        if word in haystack:
            return False, f"стоп-слово «{word}»"

    if settings.include_keywords and not any(w in haystack for w in settings.include_keywords):
        return False, "нет ни одного ключевого слова"

    return True, ""


# --------------------------------------------------------------------------- #
# Форматирование сообщений
# --------------------------------------------------------------------------- #
def format_card(project: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value or "нет"))

    description = (project.get("description") or "").strip()
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT].rstrip() + "…"

    offers = project.get("offers") or 0
    price = project.get("price") or 0
    hired = project.get("user_hired_percent")
    total = project.get("user_projects_count")
    time_left = project.get("time_left")

    lines = [
        f"🎯 <b>{esc(project.get('title'))}</b>",
        "",
        f"💰 <b>{price:,} ₽</b>".replace(",", " ") + f"  ·  📨 откликов: <b>{offers}</b>",
    ]

    customer = []
    if total is not None:
        customer.append(f"проектов: {total}")
    if hired is not None:
        customer.append(f"найм: {hired}%")
    if customer:
        lines.append(f"👤 {esc(project.get('username'))}: {', '.join(customer)}")

    if time_left:
        lines.append(f"⏳ осталось: {int(time_left) // 3600} ч")

    lines += ["", esc(description) if description else "<i>без описания</i>"]

    text = "\n".join(lines)
    return text[:TG_LIMIT]


def build_keyboard(project_id: int, has_draft: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📄 Проект", url=PROJECT_URL.format(id=project_id)),
            InlineKeyboardButton(text="✍️ Откликнуться", url=OFFER_URL.format(id=project_id)),
        ]
    ]
    if has_draft:
        rows.append(
            [InlineKeyboardButton(text="♻️ Другой вариант", callback_data=f"regen:{project_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_project(
    bot: Bot,
    chat_id: int,
    project: dict[str, Any],
    draft: str | None,
) -> None:
    project_id = project["id"]
    await bot.send_message(
        chat_id,
        format_card(project),
        reply_markup=build_keyboard(project_id, draft is not None),
        disable_web_page_preview=True,
    )
    if draft:
        # <code> в Telegram копируется одним тапом по блоку.
        await bot.send_message(chat_id, f"<code>{html.escape(draft)}</code>")


# --------------------------------------------------------------------------- #
# Опрос биржи
# --------------------------------------------------------------------------- #
async def poll_loop(
    kwork: Kwork,
    bot: Bot,
    storage: Storage,
    writer: DraftWriter,
    settings: Settings,
) -> None:
    first_run = storage.count() == 0
    if first_run and not settings.notify_on_first_run:
        logger.info("Первый запуск: текущая выдача помечается как просмотренная без уведомлений")

    failures = 0

    while True:
        try:
            projects = await kwork.get_projects(
                categories_ids=settings.categories,
                price_from=settings.min_price or None,
                price_to=settings.max_price or None,
                hiring_from=settings.hiring_from or None,
                kworks_filter_to=settings.max_offers,
            )
            failures = 0
        except Exception as err:
            failures += 1
            delay = min(settings.poll_interval * failures, 600)
            logger.warning("Ошибка опроса биржи (%s). Пауза %s с", err, delay)
            await asyncio.sleep(delay)
            continue

        fresh = []
        for item in projects:
            data = item.model_dump()
            if not data.get("id") or storage.is_seen(data["id"]):
                continue
            fresh.append(data)

        # От старых к новым, чтобы порядок в чате был хронологическим.
        fresh.sort(key=lambda p: p["id"])

        for project in fresh:
            storage.mark_seen(project)

            if first_run and not settings.notify_on_first_run:
                continue

            ok, reason = passes_filters(project, settings)
            if not ok:
                logger.info("Пропуск #%s (%s)", project["id"], reason)
                continue

            draft = await writer.draft(project)
            try:
                await send_project(bot, settings.tg_chat_id, project, draft)
                logger.info(
                    "Отправлен #%s «%s» (%s откликов)",
                    project["id"],
                    (project.get("title") or "")[:60],
                    project.get("offers"),
                )
            except TelegramRetryAfter as err:
                await asyncio.sleep(err.retry_after + 1)
                await send_project(bot, settings.tg_chat_id, project, draft)
            except Exception:
                logger.exception("Не удалось отправить #%s", project["id"])

            await asyncio.sleep(1)

        first_run = False
        storage.purge_older_than(30)
        await asyncio.sleep(settings.poll_interval)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
async def show_categories(settings: Settings) -> None:
    """Печатает дерево рубрик с ID для заполнения CATEGORIES в .env."""
    async with Kwork(
        login=settings.kwork_login,
        password=settings.kwork_password,
        phone_last=settings.kwork_phone_last,
        proxy=settings.kwork_proxy,
        timeout=30.0,
        retry_max_attempts=3,
    ) as api:
        for parent in await api.get_categories():
            print(f"\n=== {parent.id}  {parent.name}")
            for sub in parent.subcategories or []:
                print(f"  {sub.id:>6}  {sub.name}")


async def run(settings: Settings) -> None:
    storage = Storage(settings.db_path)
    writer = DraftWriter(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        profile=settings.load_profile(),
        enabled=settings.llm_enabled,
        repair_dashes=settings.repair_dashes,
    )
    bot = Bot(
        token=settings.tg_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    @dp.callback_query(F.data.startswith("regen:"))
    async def regenerate(call: CallbackQuery) -> None:
        project_id = int(call.data.split(":", 1)[1])
        project = storage.get_payload(project_id)
        if not project:
            await call.answer("Проект не найден в базе", show_alert=True)
            return
        await call.answer("Генерирую…")
        draft = await writer.draft(project, temperature=0.95)
        if not draft:
            await call.message.answer("Не получилось сгенерировать вариант")
            return
        await call.message.answer(f"<code>{html.escape(draft)}</code>")

    async with Kwork(
        login=settings.kwork_login,
        password=settings.kwork_password,
        phone_last=settings.kwork_phone_last,
        proxy=settings.kwork_proxy,
        timeout=30.0,
        retry_max_attempts=3,
    ) as kwork:
        me = await kwork.get_me()
        connects = await kwork.get_connects()
        logger.info(
            "Kwork: %s | связок: %s/%s | рубрик в фильтре: %s | интервал: %s c",
            me.username,
            connects.active_connects,
            connects.all_connects,
            len(settings.categories) or "избранные",
            settings.poll_interval,
        )

        poller = asyncio.create_task(poll_loop(kwork, bot, storage, writer, settings))
        try:
            await dp.start_polling(bot, handle_signals=False)
        finally:
            poller.cancel()
            await bot.session.close()
            storage.close()


def main() -> None:
    settings = Settings()
    settings.validate()

    if len(sys.argv) > 1 and sys.argv[1] == "categories":
        asyncio.run(show_categories(settings))
        return

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Остановлено")


if __name__ == "__main__":
    main()
