"""Проверка генерации отклика без Telegram и без ожидания новых проектов.

    python draft_test.py                    последний проект из базы
    python draft_test.py 2360406            конкретный проект по id
    python draft_test.py --text "Нужно смонтировать ролик на 10 минут"
    python draft_test.py --text "..." -n 3  несколько вариантов подряд
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys

from ai import DraftWriter, SYSTEM_PROMPT
from config import Settings


def load_project(settings: Settings, project_id: int | None) -> dict:
    if not settings.db_path.exists():
        sys.exit(f"Базы нет: {settings.db_path}. Укажи --text вручную.")
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    if project_id:
        row = conn.execute(
            "SELECT payload FROM seen_projects WHERE id = ?", (project_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM seen_projects ORDER BY notified_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        sys.exit("В базе нет подходящего проекта. Укажи --text вручную.")
    return json.loads(row["payload"])


async def main() -> None:
    parser = argparse.ArgumentParser(description="Тест генерации отклика")
    parser.add_argument("project_id", nargs="?", type=int, help="id проекта из базы")
    parser.add_argument("--text", help="текст ТЗ вместо проекта из базы")
    parser.add_argument("--title", default="Тестовая задача")
    parser.add_argument("-n", type=int, default=1, help="сколько вариантов сгенерировать")
    args = parser.parse_args()

    settings = Settings()
    if not settings.llm_api_key:
        sys.exit("CLOSEROUTER_API_KEY пуст, генерировать нечем.")

    if args.text:
        project = {"id": 0, "title": args.title, "description": args.text}
    else:
        project = load_project(settings, args.project_id)

    profile = settings.load_profile()

    print("=" * 70)
    print(f"МОДЕЛЬ:    {settings.llm_model}")
    print(f"ПРИВЕТСТВИЕ: {settings.greeting or 'выключено'}")
    print(f"ПРОМПТ:    {len(SYSTEM_PROMPT)} символов")
    print(f"ЭНДПОИНТ:  {settings.llm_base_url}")
    print(f"ТАЙМАУТ:   {settings.llm_timeout} с, повторов {settings.llm_retries}")
    print(f"ПРОФИЛЬ:   {len(profile)} символов из {settings.profile_path}")
    print("=" * 70)
    print(f"ЗАДАЧА: {project.get('title')}")
    desc = (project.get("description") or "").strip()
    print(desc[:400] + ("..." if len(desc) > 400 else ""))
    print("=" * 70)

    writer = DraftWriter(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        profile=profile,
        enabled=True,
        repair_dashes=settings.repair_dashes,
        greeting=settings.greeting,
        timeout=settings.llm_timeout,
        retries=settings.llm_retries,
    )

    for i in range(args.n):
        draft = await writer.draft(project, temperature=0.7 + 0.15 * i)
        print(f"\n--- ВАРИАНТ {i + 1} ---")
        if draft is None:
            print(f"(пусто: {writer.last_error or 'SKIP, задача вне профиля'})")
            continue
        print(draft)
        checks = {
            "приветствие": draft.lower().startswith(("здравствуй", "добр", "привет")),
            "нет длинных тире": not any(c in draft for c in "—–"),
            "нет цены": not any(w in draft.lower() for w in ("руб", "₽", "цен", "стоим", "бюджет")),
            "нет сроков": not any(w in draft.lower() for w in ("день", "дней", "недел", "срок", "быстро")),
        }
        print("\n" + "  ".join(f"[{'v' if ok else 'x'}] {k}" for k, ok in checks.items()))


if __name__ == "__main__":
    asyncio.run(main())
