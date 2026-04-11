import re

import orjson

import structlog
from pydantic import BaseModel

from src.bot import api

logger = structlog.get_logger("chat.utils")

MAX_NAME_CACHE = 500

_user_names_cache: dict[int, str] = {}


class RinResponse(BaseModel):
    text: str
    reaction: str | None = None
    remember: list[str] | None = None
    forget: list[str] | None = None


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
        data = orjson.loads(raw)
        return RinResponse(
            text=str(data.get("text", raw)),
            reaction=data.get("reaction"),
            remember=data.get("remember"),
            forget=data.get("forget"),
        )
    except (orjson.JSONDecodeError, AttributeError):
        pass

    match = re.search(r'\{[^{}]*"text"\s*:.*\}', raw, re.DOTALL)
    if match:
        try:
            data = orjson.loads(match.group())
            return RinResponse(
                text=str(data.get("text", raw)),
                reaction=data.get("reaction"),
                remember=data.get("remember"),
                forget=data.get("forget"),
            )
        except (orjson.JSONDecodeError, AttributeError):
            pass

    text_match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if text_match:
        text = text_match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        reaction_match = re.search(r'"reaction"\s*:\s*"([^"]+)"', raw)
        return RinResponse(
            text=text,
            reaction=reaction_match.group(1) if reaction_match else None,
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
