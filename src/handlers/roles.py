import json
import re
from pathlib import Path

import structlog
from vkbottle.bot import Message, BotLabeler

from src.bot import api

logger = structlog.get_logger("handlers.roles")
labeler = BotLabeler()

DATA_DIR = Path("/app/data")
ROLES_FILE = DATA_DIR / "roles.json"


def load_roles() -> dict[str, list[int]]:
    """Загрузить роли из файла"""
    if not ROLES_FILE.exists():
        return {}
    with open(ROLES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_roles(roles: dict[str, list[int]]) -> None:
    """Сохранить роли в файл"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ROLES_FILE, "w", encoding="utf-8") as f:
        json.dump(roles, f, ensure_ascii=False, indent=2)


# Паттерн для проверки валидного названия роли (только буквы, цифры, пробелы, дефисы)
VALID_ROLE_PATTERN = re.compile(r'^[\w\s\-]+$', re.UNICODE)


def normalize_role(role: str) -> str | None:
    """Нормализовать название роли (убрать окончания). Вернёт None если роль невалидна."""
    role = role.lower().strip()

    # Проверяем на запрещённые символы (теги, ссылки и т.д.)
    if '@' in role or '[' in role or ']' in role or 'id' in role and any(c.isdigit() for c in role):
        return None

    # Проверяем что роль содержит только допустимые символы
    if not VALID_ROLE_PATTERN.match(role):
        return None

    # Ограничиваем длину
    if len(role) > 30:
        return None

    # Убираем типичные окончания множественного числа
    if role.endswith("ы") or role.endswith("и"):
        role = role[:-1]
    return role


@labeler.message(text="/роль <role>")
async def manage_role(message: Message, role: str):
    """Добавить или убрать роль"""
    user_id = message.from_id
    roles = load_roles()

    if role.startswith("-"):
        # Убрать роль
        role_name = normalize_role(role[1:])
        if role_name is None:
            await message.answer("Невалидное название роли")
            return
        if role_name in roles and user_id in roles[role_name]:
            roles[role_name].remove(user_id)
            if not roles[role_name]:
                del roles[role_name]
            save_roles(roles)
            await message.answer(f"Роль «{role_name}» убрана")
            await logger.ainfo("Роль убрана", user_id=user_id, role=role_name)
        else:
            await message.answer(f"У тебя нет роли «{role_name}»")
    else:
        # Добавить роль
        role_name = normalize_role(role)
        if role_name is None:
            await message.answer("Невалидное название роли (без @, ссылок, макс 30 символов)")
            return
        if role_name not in roles:
            roles[role_name] = []
        if user_id not in roles[role_name]:
            roles[role_name].append(user_id)
            save_roles(roles)
            await message.answer(f"Роль «{role_name}» добавлена")
            await logger.ainfo("Роль добавлена", user_id=user_id, role=role_name)
        else:
            await message.answer(f"У тебя уже есть роль «{role_name}»")


@labeler.message(text="/роли")
async def list_my_roles(message: Message):
    """Показать свои роли"""
    user_id = message.from_id
    roles = load_roles()

    user_roles = [role for role, users in roles.items() if user_id in users]

    if user_roles:
        await message.answer(f"Твои роли: {', '.join(user_roles)}")
    else:
        await message.answer("У тебя пока нет ролей. Добавь через /роль <название>")


@labeler.message(text="/тег <role>")
async def tag_role(message: Message, role: str):
    """Тегнуть всех с ролью"""
    role_name = normalize_role(role)
    if role_name is None:
        await message.answer("Невалидное название роли")
        return

    roles = load_roles()

    if role_name not in roles or not roles[role_name]:
        await message.answer(f"Никто не записан в роль «{role_name}»")
        return

    user_ids = roles[role_name]
    users = await api.users.get(user_ids=user_ids)

    mentions = [f"@id{user.id} ({user.first_name})" for user in users]
    await message.answer(f"📢 {role_name}: {', '.join(mentions)}")
    await logger.ainfo("Тег роли", role=role_name, count=len(user_ids))


@labeler.message(text="/все_роли")
async def list_all_roles(message: Message):
    """Показать все роли и количество людей"""
    roles = load_roles()

    if not roles:
        await message.answer("Пока нет ни одной роли")
        return

    lines = [f"• {role}: {len(users)} чел." for role, users in roles.items()]
    await message.answer("Все роли:\n" + "\n".join(lines))
