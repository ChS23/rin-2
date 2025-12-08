import json
from pathlib import Path
from datetime import datetime

import structlog
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule
from vkbottle.tools import Keyboard, Text, OpenLink, TemplateElement, template_gen

from src.bot import api

logger = structlog.get_logger("handlers.projects")
labeler = BotLabeler()

DATA_DIR = Path("/app/data")
PROJECTS_FILE = DATA_DIR / "projects.json"


def load_projects() -> dict[str, dict]:
    """Загрузить проекты из файла"""
    if not PROJECTS_FILE.exists():
        return {}
    with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_projects(projects: dict[str, dict]) -> None:
    """Сохранить проекты в файл"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def get_next_id(projects: dict[str, dict]) -> str:
    """Получить следующий ID проекта"""
    if not projects:
        return "1"
    return str(max(int(k) for k in projects.keys()) + 1)


# Состояние для создания проекта (user_id -> step)
creation_state: dict[int, dict] = {}


class InProjectCreation(ABCRule[Message]):
    """Правило: пользователь в процессе создания проекта"""
    async def check(self, event: Message) -> bool:
        return event.from_id in creation_state


@labeler.private_message(text="/проект создать")
async def start_create_project(message: Message):
    """Начать создание проекта (только в ЛС)"""
    user_id = message.from_id
    creation_state[user_id] = {"step": "name"}
    await message.answer(
        "Создание нового проекта\n\n"
        "Шаг 1/4: Введи название проекта:"
    )


@labeler.private_message(InProjectCreation())
async def handle_creation_steps(message: Message):
    """Обработка шагов создания проекта"""
    user_id = message.from_id
    state = creation_state[user_id]
    text = message.text.strip()

    if state["step"] == "name":
        state["name"] = text
        state["step"] = "description"
        await message.answer(
            "Шаг 2/4: Введи описание проекта:"
        )

    elif state["step"] == "description":
        state["description"] = text
        state["step"] = "status"
        await message.answer(
            "Шаг 3/4: Выбери статус:\n"
            "1 — в работе\n"
            "2 — ищу помощь\n"
            "3 — завершён\n\n"
            "Введи номер:"
        )

    elif state["step"] == "status":
        statuses = {"1": "в работе", "2": "ищу помощь", "3": "завершён"}
        if text not in statuses:
            await message.answer("Введи 1, 2 или 3")
            return
        state["status"] = statuses[text]
        state["step"] = "tags"
        await message.answer(
            "Шаг 4/4: Введи теги через запятую (или 'нет' чтобы пропустить):\n"
            "Например: перевод, horror, renpy"
        )

    elif state["step"] == "tags":
        if text.lower() == "нет":
            tags = []
        else:
            tags = [t.strip().lower() for t in text.split(",") if t.strip()]

        # Сохраняем проект
        projects = load_projects()
        project_id = get_next_id(projects)

        projects[project_id] = {
            "name": state["name"],
            "description": state["description"],
            "author_id": user_id,
            "status": state["status"],
            "tags": tags,
            "created_at": datetime.now().strftime("%Y-%m-%d")
        }

        save_projects(projects)
        del creation_state[user_id]

        await logger.ainfo("Проект создан", project_id=project_id, author_id=user_id)
        await message.answer(
            f"Проект #{project_id} создан!\n\n"
            f"Название: {state['name']}\n"
            f"Статус: {state['status']}\n"
            f"Теги: {', '.join(tags) if tags else 'нет'}\n\n"
            f"Посмотреть: /проект {project_id}"
        )


@labeler.private_message(InProjectCreation(), text="/отмена")
async def cancel_creation(message: Message):
    """Отменить создание проекта"""
    user_id = message.from_id
    del creation_state[user_id]
    await message.answer("Создание проекта отменено")


def build_project_carousel(projects_dict: dict[str, dict], limit: int = 10) -> str | None:
    """Создать карусель проектов"""
    if not projects_dict:
        return None

    elements = []
    sorted_projects = sorted(projects_dict.items(), key=lambda x: int(x[0]), reverse=True)[:limit]

    for pid, p in sorted_projects:
        status_emoji = {"в работе": "🔨", "ищу помощь": "🆘", "завершён": "✅"}.get(p["status"], "")

        # Обрезаем описание если слишком длинное
        desc = p["description"][:80] + "..." if len(p["description"]) > 80 else p["description"]

        elements.append(
            TemplateElement(
                title=f"{status_emoji} {p['name']}"[:80],
                description=f"{desc}\n\nТеги: {', '.join(p['tags']) if p['tags'] else 'нет'}"[:160],
                buttons=Keyboard(inline=True)
                    .add(Text(f"Подробнее #{pid}", payload={"cmd": "project", "id": pid}))
                    .get_json()
            )
        )

    return template_gen(*elements) if elements else None


@labeler.message(text="/проекты")
async def list_projects(message: Message):
    """Список всех проектов (карусель)"""
    projects = load_projects()

    if not projects:
        await message.answer("Пока нет ни одного проекта")
        return

    carousel = build_project_carousel(projects)

    if carousel:
        await message.answer("Проекты сообщества:", template=carousel)
    else:
        await message.answer("Не удалось создать карусель")


@labeler.message(text="/проект <project_id>")
async def show_project(message: Message, project_id: str):
    """Показать проект"""
    projects = load_projects()

    if project_id not in projects:
        await message.answer(f"Проект #{project_id} не найден")
        return

    p = projects[project_id]
    users = await api.users.get(user_ids=[p["author_id"]])
    author = users[0] if users else None
    author_name = f"{author.first_name} {author.last_name}" if author else "Неизвестно"

    status_emoji = {"в работе": "🔨", "ищу помощь": "🆘", "завершён": "✅"}.get(p["status"], "")

    text = (
        f"Проект #{project_id}\n\n"
        f"📌 {p['name']}\n\n"
        f"{p['description']}\n\n"
        f"Статус: {status_emoji} {p['status']}\n"
        f"Автор: @id{p['author_id']} ({author_name})\n"
        f"Теги: {', '.join(p['tags']) if p['tags'] else 'нет'}\n"
        f"Создан: {p['created_at']}"
    )

    await message.answer(text)


@labeler.message(text="/мои_проекты")
async def my_projects(message: Message):
    """Мои проекты (карусель)"""
    user_id = message.from_id
    projects = load_projects()

    user_projects = {k: v for k, v in projects.items() if v["author_id"] == user_id}

    if not user_projects:
        await message.answer("У тебя пока нет проектов.\nСоздать: /проект создать (в ЛС с ботом)")
        return

    carousel = build_project_carousel(user_projects)
    if carousel:
        await message.answer("Твои проекты:", template=carousel)
    else:
        # Fallback на текст
        lines = [f"#{pid} {p['name']}" for pid, p in user_projects.items()]
        await message.answer("Твои проекты:\n\n" + "\n".join(lines))


@labeler.message(text="/ищу_помощь")
async def looking_for_help(message: Message):
    """Проекты, которые ищут помощь (карусель)"""
    projects = load_projects()

    help_projects = {k: v for k, v in projects.items() if v["status"] == "ищу помощь"}

    if not help_projects:
        await message.answer("Сейчас никто не ищет помощь")
        return

    carousel = build_project_carousel(help_projects)
    if carousel:
        await message.answer("🆘 Проекты, которые ищут помощь:", template=carousel)
    else:
        lines = [f"#{pid} 🆘 {p['name']}" for pid, p in help_projects.items()]
        await message.answer("Проекты, которые ищут помощь:\n\n" + "\n".join(lines))


@labeler.private_message(text="/проект удалить <project_id>")
async def delete_project(message: Message, project_id: str):
    """Удалить свой проект (только в ЛС)"""
    user_id = message.from_id
    projects = load_projects()

    if project_id not in projects:
        await message.answer(f"Проект #{project_id} не найден")
        return

    if projects[project_id]["author_id"] != user_id:
        await message.answer("Ты можешь удалить только свои проекты")
        return

    name = projects[project_id]["name"]
    del projects[project_id]
    save_projects(projects)

    await logger.ainfo("Проект удалён", project_id=project_id, author_id=user_id)
    await message.answer(f"Проект #{project_id} «{name}» удалён")
