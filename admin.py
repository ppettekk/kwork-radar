"""Панель управления в Telegram. Всё делается кнопками, команд минимум."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from runtime import FIELDS, Runtime, show
from storage import Storage

logger = logging.getLogger(__name__)
router = Router()

Screen = tuple[str, InlineKeyboardMarkup]


class Edit(StatesGroup):
    """Ожидание значения, введённого текстом после кнопки «Ввести вручную»."""

    waiting = State()


@dataclass
class Ctx:
    runtime: Runtime
    storage: Storage
    writer: Any = None
    started_at: float = field(default_factory=time.monotonic)
    kwork: Any = None
    last_poll: float = 0.0
    last_error: str = ""


BOT_COMMANDS = [
    BotCommand(command="menu", description="Панель управления"),
]


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# --------------------------------------------------------------------------- #
# Главный экран
# --------------------------------------------------------------------------- #
def menu_screen(ctx: Ctx) -> Screen:
    rt = ctx.runtime
    ago = f"{int(time.monotonic() - ctx.last_poll)} с назад" if ctx.last_poll else "ещё не было"

    text = (
        f"{'⏸' if rt.paused else '▶️'} <b>Kwork Radar: "
        f"{'на паузе' if rt.paused else 'работает'}</b>\n\n"
        f"Черновики ИИ: {'включены' if rt.llm_enabled else 'выключены'}"
        f"{', сразу с проектом' if rt.auto_draft else ', по кнопке'}\n"
        f"Опрос каждые {rt.poll_interval} с, последний {ago}\n"
        f"Фильтр: до {rt.max_offers} откликов, бюджет от {rt.min_price or 0} ₽\n"
        f"Рубрики: {show('categories', rt.categories)}"
    )
    if ctx.last_error:
        text += f"\n\n⚠️ Последняя ошибка: {html.escape(ctx.last_error[:120])}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("▶️ Продолжить" if rt.paused else "⏸ Пауза", "adm:t:paused"),
            btn("🤖 ИИ выкл" if rt.llm_enabled else "🤖 ИИ вкл", "adm:t:llm_enabled"),
        ],
        [btn(
            "🪄 Черновик по кнопке" if rt.auto_draft else "🪄 Черновик сразу",
            "adm:t:auto_draft",
        )],
        [btn("📊 Статистика", "adm:stats"), btn("⚙️ Параметры", "adm:params")],
        [btn("🔄 Обновить", "adm:menu")],
    ])
    return text, keyboard


# --------------------------------------------------------------------------- #
# Статистика
# --------------------------------------------------------------------------- #
async def stats_screen(ctx: Ctx) -> Screen:
    c = ctx.storage.counters()
    p = ctx.storage.project_stats()

    lines = [
        "📊 <b>Статистика</b>\n",
        f"В работе: {human_time(time.monotonic() - ctx.started_at)}",
        f"Опросов биржи: {c.get('polls', 0)}"
        + (f", ошибок: {c.get('poll_errors', 0)}" if c.get("poll_errors") else ""),
        "",
        "<b>Проекты</b>",
        f"Просмотрено: {p['total']}, за сутки {p['seen_day']}",
        f"Отправлено: {p['notified']}, за сутки {p['notified_day']}, "
        f"за неделю {p['notified_week']}",
        f"Отсеяно фильтрами: {c.get('filtered', 0)}",
    ]
    if p["avg_price"]:
        lines.append(
            f"Средний бюджет отправленных: {int(p['avg_price']):,} ₽".replace(",", " ")
        )

    lines += ["", "<b>Черновики</b>", f"Сгенерировано: {c.get('drafts', 0)}"]
    if c.get("skips"):
        lines.append(f"Пропущено как SKIP: {c['skips']}")
    if c.get("llm_errors"):
        lines.append(f"Ошибок ИИ: {c['llm_errors']}")
    if ctx.writer is not None and getattr(ctx.writer, "last_error", ""):
        lines.append(f"Последняя: <i>{html.escape(ctx.writer.last_error[:120])}</i>")

    if ctx.kwork is not None:
        try:
            connects = await ctx.kwork.get_connects()
            lines += ["", f"Связок на Kwork: {connects.active_connects}/{connects.all_connects}"]
        except Exception:
            logger.warning("Не удалось получить связки")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Обновить", "adm:stats"), btn("◀️ Назад", "adm:menu")],
        [btn("🧹 Обнулить счётчики", "adm:zero")],
    ])
    return "\n".join(lines), keyboard


# --------------------------------------------------------------------------- #
# Параметры: список и карточка одного параметра
# --------------------------------------------------------------------------- #
def params_screen(ctx: Ctx) -> Screen:
    rows, pair = [], []
    for key, fld, value, overridden in ctx.runtime.items():
        if fld.kind == "bool":
            continue  # переключаются с главного экрана
        mark = "•" if overridden else ""
        pair.append(btn(f"{mark}{fld.label}", f"adm:p:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("◀️ Назад", "adm:menu")])

    lines = ["⚙️ <b>Параметры</b>", "<i>точка = изменено через бота</i>\n"]
    for key, fld, value, overridden in ctx.runtime.items():
        if fld.kind == "bool":
            continue
        lines.append(f"{fld.label}: <b>{show(key, value)}</b>")

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def param_screen(ctx: Ctx, key: str, note: str = "") -> Screen:
    fld = FIELDS[key]
    value = ctx.runtime.get(key)
    overridden = ctx.runtime.is_overridden(key)

    text = (
        f"⚙️ <b>{fld.label}</b>\n\n"
        f"Сейчас: <b>{show(key, value)}</b>\n"
        f"<i>{fld.hint}</i>"
    )
    if overridden:
        text += "\n\nИзменено через бота, кнопка сброса вернёт значение из .env."
    if note:
        text += f"\n\n{note}"

    rows: list[list[InlineKeyboardButton]] = []

    if fld.steps:
        rows.append([
            btn(f"{d:+d}".replace("+", "＋").replace("-", "−"), f"adm:d:{key}:{d}")
            for d in fld.steps
        ])
    if fld.presets:
        presets = list(fld.presets)
        for i in range(0, len(presets), 2 if fld.kind == "str" else 4):
            chunk = presets[i:i + (2 if fld.kind == "str" else 4)]
            rows.append([
                btn(p.split("/")[-1] if fld.kind == "str" else p, f"adm:v:{key}:{p}")
                for p in chunk
            ])

    extra = [btn("✏️ Ввести вручную", f"adm:in:{key}")]
    if fld.kind in {"list_int", "list_str"}:
        extra.append(btn("🧹 Очистить", f"adm:clr:{key}"))
    rows.append(extra)

    last = [btn("◀️ К параметрам", "adm:params")]
    if overridden:
        last.insert(0, btn("↩️ Сбросить", f"adm:rst:{key}"))
    rows.append(last)

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Отрисовка
# --------------------------------------------------------------------------- #
async def edit(call: CallbackQuery, screen: Screen) -> None:
    text, keyboard = screen
    try:
        await call.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as err:
        if "message is not modified" not in str(err):
            raise


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #
@router.message(Command("menu", "start"))
async def cmd_menu(message: Message, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = menu_screen(ctx)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


# --------------------------------------------------------------------------- #
# Кнопки
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "adm:menu")
async def cb_menu(call: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await edit(call, menu_screen(ctx))


@router.callback_query(F.data == "adm:stats")
async def cb_stats(call: CallbackQuery, ctx: Ctx) -> None:
    await call.answer()
    await edit(call, await stats_screen(ctx))


@router.callback_query(F.data == "adm:zero")
async def cb_zero(call: CallbackQuery, ctx: Ctx) -> None:
    ctx.storage.reset_counters()
    await call.answer("Счётчики обнулены")
    await edit(call, await stats_screen(ctx))


@router.callback_query(F.data == "adm:params")
async def cb_params(call: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await edit(call, params_screen(ctx))


@router.callback_query(F.data.startswith("adm:p:"))
async def cb_param(call: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    await state.clear()
    key = call.data.split(":", 2)[2]
    await call.answer()
    await edit(call, param_screen(ctx, key))


@router.callback_query(F.data.startswith("adm:t:"))
async def cb_toggle(call: CallbackQuery, ctx: Ctx) -> None:
    key = call.data.split(":", 2)[2]
    value = ctx.runtime.toggle(key)
    await call.answer(f"{FIELDS[key].label}: {show(key, value)}")
    await edit(call, menu_screen(ctx))


@router.callback_query(F.data.startswith("adm:d:"))
async def cb_delta(call: CallbackQuery, ctx: Ctx) -> None:
    _, _, key, delta = call.data.split(":", 3)
    value = ctx.runtime.bump(key, int(delta))
    await call.answer(f"{show(key, value)}")
    await edit(call, param_screen(ctx, key))


@router.callback_query(F.data.startswith("adm:v:"))
async def cb_value(call: CallbackQuery, ctx: Ctx) -> None:
    _, _, key, raw = call.data.split(":", 3)
    try:
        value = ctx.runtime.set(key, raw)
    except ValueError as err:
        await call.answer(str(err), show_alert=True)
        return
    await call.answer(f"{show(key, value)}")
    await edit(call, param_screen(ctx, key))


@router.callback_query(F.data.startswith("adm:clr:"))
async def cb_clear(call: CallbackQuery, ctx: Ctx) -> None:
    key = call.data.split(":", 3)[2]
    ctx.runtime.clear(key)
    await call.answer("Очищено")
    await edit(call, param_screen(ctx, key))


@router.callback_query(F.data.startswith("adm:rst:"))
async def cb_reset(call: CallbackQuery, ctx: Ctx) -> None:
    key = call.data.split(":", 3)[2]
    value = ctx.runtime.reset(key)
    await call.answer(f"Из .env: {show(key, value)}")
    await edit(call, param_screen(ctx, key))


# --------------------------------------------------------------------------- #
# Ручной ввод
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith("adm:in:"))
async def cb_input(call: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    key = call.data.split(":", 3)[2]
    fld = FIELDS[key]
    await state.set_state(Edit.waiting)
    await state.update_data(key=key)
    await call.answer()
    await edit(call, (
        f"✏️ <b>{fld.label}</b>\n\n"
        f"Сейчас: <b>{show(key, ctx.runtime.get(key))}</b>\n"
        f"<i>{fld.hint}</i>\n\n"
        "Пришлите новое значение одним сообщением.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Отмена", f"adm:p:{key}")]]),
    ))


@router.message(Edit.waiting)
async def on_input(message: Message, ctx: Ctx, state: FSMContext) -> None:
    key = (await state.get_data()).get("key")
    if key not in FIELDS:
        await state.clear()
        return

    try:
        value = ctx.runtime.set(key, message.text or "")
    except ValueError as err:
        text, keyboard = param_screen(ctx, key, note=f"⚠️ Не подходит: {err}")
        await message.answer(text, reply_markup=keyboard)
        return

    await state.clear()
    text, keyboard = param_screen(
        ctx, key, note=f"✅ Сохранено: <b>{show(key, value)}</b>"
    )
    await message.answer(text, reply_markup=keyboard)
