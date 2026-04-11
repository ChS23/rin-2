import json
import re
import time

import structlog
from pydantic import BaseModel

from src.bot import api

logger = structlog.get_logger("chat.utils")

REPLY_LIMIT = 5
REPLY_WINDOW = 10 * 60
MAX_NAME_CACHE = 500

_user_reply_times: dict[int, list[float]] = {}
_user_names_cache: dict[int, str] = {}


class RinResponse(BaseModel):
    text: str
    reaction: str | None = None
    remember: list[str] | None = None
    forget: list[str] | None = None


REPLY_LIMIT_PROMPT = """
Когда тебе пишет один и тот же человек подряд, ты не обязана отвечать бесконечно — ты живая, у тебя свои дела.

Сейчас тебе осталось {remaining} ответов этому человеку (из 5 за 10 минут).
- 4-5: общайся как обычно.
- 2-3: отвечай короче, можешь отвлекаться, терять интерес.
- 1: сворачивай разговор естественно — "ладн, мне пора", "пойду", "потом доболтаем".
- 0: это твоя последняя реплика. Попрощайся по-своему и уходи. Не объясняй лимиты — просто уходи как живой человек.

Не упоминай счётчик, лимиты или то, что ты "бот". Ты просто устала или тебе стало скучно — это нормально.
""".strip()


def get_remaining_replies(user_id: int) -> int:
    now = time.time()
    times = _user_reply_times.get(user_id, [])
    times = [t for t in times if now - t < REPLY_WINDOW]
    _user_reply_times[user_id] = times
    return max(0, REPLY_LIMIT - len(times))


def record_reply(user_id: int):
    if user_id not in _user_reply_times:
        _user_reply_times[user_id] = []
    _user_reply_times[user_id].append(time.time())


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
