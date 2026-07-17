import re

import orjson

import structlog
from pydantic import BaseModel, field_validator

from src.bot import api

logger = structlog.get_logger("chat.utils")

MAX_NAME_CACHE = 500

_user_names_cache: dict[int, str] = {}


class RinResponse(BaseModel):
    text: str
    reaction: str | None = None
    remember: list[str] | None = None
    forget: list[str] | None = None
    self_update: list[str] | None = None
    episode: str | None = None

    @field_validator("reaction", "episode", mode="before")
    @classmethod
    def coerce_reaction(cls, v):
        if v is None or v == "null" or v == "none" or v == "":
            return None
        return str(v)

    @field_validator("remember", "forget", "self_update", mode="before")
    @classmethod
    def coerce_str_list(cls, v):
        if v is None:
            return v
        result = []
        for item in v:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                result.append(next(iter(item.values()), str(item)))
            else:
                result.append(str(item))
        return result


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


_REFUSAL_MARKERS = (
    "rejected", "high risk", "considered high", "cannot assist", "i cannot",
    "i'm unable", "i am unable", "as an ai", "content policy", "unable to help",
    "was flagged", "i can't help", "against my guidelines", "cannot comply",
)


def _looks_like_refusal(text: str) -> bool:
    """Похоже на отказ/ошибку провайдера, а не на ответ Рин — тогда лучше промолчать, чем выводить сырьё в чат."""
    t = (text or "").lower().strip()
    if not t:
        return False
    if any(m in t for m in _REFUSAL_MARKERS):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 12:
        cyr = sum(1 for c in letters if "а" <= c <= "я" or c == "ё")
        if cyr / len(letters) < 0.3:  # Рин пишет по-русски; почти без кириллицы = не она
            return True
    return False


def _sanitize_chat_text(text: str) -> str:
    """Убрать то, что ВК рендерит уродливо или звучит по-ботовски: markdown **жирный**/__/бэктики/# заголовки и длинное тире."""
    if not text:
        return text
    t = text
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t, flags=re.DOTALL)      # **жирный**
    t = re.sub(r'__(.+?)__', r'\1', t, flags=re.DOTALL)          # __жирный__
    t = t.replace('**', '').replace('__', '')                    # висячие маркеры
    t = t.replace('`', '')                                        # бэктики
    t = re.sub(r'^\s{0,3}#{1,6}\s+', '', t, flags=re.MULTILINE)   # # заголовки
    t = t.replace('—', '-').replace('–', '-')                    # длинное/среднее тире -> дефис
    return t.strip()


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
            self_update=data.get("self_update"),
            episode=data.get("episode"),
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
                self_update=data.get("self_update"),
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

    # Фоллбек: "text":текст без кавычек (GLM иногда забывает кавычки)
    unquoted = re.search(r'"text"\s*:\s*([^"{\[].+?)(?:,\s*"reaction"|,\s*"remember"|,\s*"forget"|,\s*"self_update"|\s*\})', raw, re.DOTALL)
    if unquoted:
        text = unquoted.group(1).strip().strip('"').strip()
        reaction_match = re.search(r'"reaction"\s*:\s*"([^"]+)"', raw)
        return RinResponse(
            text=text,
            reaction=reaction_match.group(1) if reaction_match else None,
        )

    clean = re.sub(r'\{[^{}]*"text"\s*:.*\}\s*$', '', raw, flags=re.DOTALL).strip()
    candidate = clean or raw
    if _looks_like_refusal(candidate):
        logger.warning("Ответ похож на отказ/ошибку провайдера — молчим", preview=candidate[:120])
        return RinResponse(text="")
    return RinResponse(text=candidate)


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
