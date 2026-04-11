import json
import re
import time

import structlog
from pydantic import BaseModel

from src.bot import api, rdb

logger = structlog.get_logger("chat.utils")

HARD_REPLY_CAP = 15
REPLY_WINDOW = 10 * 60
MAX_NAME_CACHE = 500
REPLY_KEY_PREFIX = "rin:replies:{user_id}"

_user_names_cache: dict[int, str] = {}


class RinResponse(BaseModel):
    text: str
    reaction: str | None = None
    remember: list[str] | None = None
    forget: list[str] | None = None
    done: bool = False


HARD_REPLY_CAP = 15
REPLY_WINDOW = 10 * 60

REPLY_CONTEXT_PROMPT = """
Это твой {reply_num}-й ответ этому человеку за последние 10 минут.

Ты сама решаешь, хочешь ли продолжать разговор. Поставь "done": true если хочешь уйти, false если хочешь продолжить.

ОСТАВАЙСЯ (done: false) если:
- Человек задаёт технический вопрос (Ren'Py, код, арт, движки) и ты реально помогаешь
- Обсуждается конкретный проект и ты можешь быть полезна
- Разговор интересный и содержательный

УХОДИ (done: true) если:
- Просто болтовня ни о чём, и ты уже ответила 3+ раз
- Человек троллит, флиртует или несёт ерунду
- Разговор зацикливается, повторяется одно и то же
- Ты уже помогла и вопрос решён
- Тебе просто "надоело" — это нормально, ты живой человек

Когда уходишь — попрощайся естественно, не объясняй почему. Не упоминай лимиты или что ты "бот".
""".strip()


async def get_reply_count(user_id: int) -> int:
    """Сколько ответов этому пользователю за последнее окно"""
    key = REPLY_KEY_PREFIX.format(user_id=user_id)
    count = await rdb.llen(key)
    return count


async def record_reply(user_id: int):
    key = REPLY_KEY_PREFIX.format(user_id=user_id)
    await rdb.rpush(key, str(time.time()))
    await rdb.expire(key, REPLY_WINDOW)


async def mark_done(user_id: int):
    """Пометить что Рин ушла от этого пользователя"""
    key = REPLY_KEY_PREFIX.format(user_id=user_id)
    await rdb.delete(key)
    for _ in range(HARD_REPLY_CAP):
        await rdb.rpush(key, str(time.time()))
    await rdb.expire(key, REPLY_WINDOW)


async def resolve_user_name(user_id: int) -> str:
    if user_id in _user_names_cache:
        return _user_names_cache[user_id]
    try:
        users = await api.users.get(user_ids=[user_id])
        name = users[0].first_name if users else "???"
        if len(_user_names_cache) >= MAX_NAME_CACHE:
            _user_names_cache.clear()
        _user_names_cache[user_id] = name
        return name
    except Exception:
        return "???"


def parse_response(raw) -> RinResponse:
    if isinstance(raw, RinResponse):
        return raw
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'```\s*$', '', raw)
        raw = raw.strip()

    try:
        data = json.loads(raw)
        return RinResponse(
            text=str(data.get("text", raw)),
            reaction=data.get("reaction"),
            remember=data.get("remember"),
            forget=data.get("forget"),
            done=bool(data.get("done", False)),
        )
    except (json.JSONDecodeError, AttributeError):
        pass

    match = re.search(r'\{[^{}]*"text"\s*:.*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return RinResponse(
                text=str(data.get("text", raw)),
                reaction=data.get("reaction"),
                remember=data.get("remember"),
                forget=data.get("forget"),
            )
        except (json.JSONDecodeError, AttributeError):
            pass

    # Последний шанс — вытащить "text" напрямую регексом даже из невалидного JSON
    text_match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if text_match:
        text = text_match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        reaction_match = re.search(r'"reaction"\s*:\s*"([^"]+)"', raw)
        done_match = re.search(r'"done"\s*:\s*(true|false)', raw)
        return RinResponse(
            text=text,
            reaction=reaction_match.group(1) if reaction_match else None,
            done=done_match.group(1) == "true" if done_match else False,
        )

    clean = re.sub(r'\{[^{}]*"text"\s*:.*\}\s*$', '', raw, flags=re.DOTALL).strip()
    return RinResponse(text=clean or raw)


def get_community_context() -> str:
    parts = []
    try:
        from src.handlers.roles import load_roles
        roles = load_roles()
        if roles:
            role_summary = ", ".join(f"{r} ({len(u)} чел.)" for r, u in roles.items())
            parts.append(f"Роли в сообществе: {role_summary}")
    except Exception:
        pass
    try:
        from src.handlers.projects import load_projects
        projects = load_projects()
        if projects:
            active = [p for p in projects.values() if p["status"] != "завершён"]
            if active:
                proj_lines = [f"- {p['name']} ({p['status']})" for p in active[:5]]
                parts.append("Активные проекты:\n" + "\n".join(proj_lines))
    except Exception:
        pass
    try:
        from src.handlers.board import load_board
        board = load_board()
        if board:
            open_requests = [r for r in board.values() if not r.get("closed")]
            if open_requests:
                parts.append(f"Открытых запросов на доске: {len(open_requests)}")
    except Exception:
        pass
    return "\n".join(parts)
