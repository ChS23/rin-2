"""Даталог: полная история чата + каждый вызов модели, по дням в JSONL.

Пишется в /app/data/logs (том bot_data, попадает в бэкап). Append-only, best-effort:
логирование НИКОГДА не должно ронять бота — все ошибки глушатся.

Файлы (дата по МСК):
  chat-YYYY-MM-DD.jsonl    каждое сообщение (все люди + Рин) + метрики её реплик + реплаи ей
  calls-YYYY-MM-DD.jsonl   каждый вызов модели: агент, модель, семплинг, ПОЛНЫЙ промпт, вывод,
                           токены, латентность, TTFT, finish_reason, тул-вызовы, reasoning
  turns-YYYY-MM-DD.jsonl   ход целиком: СТРУКТУРИРОВАННЫЙ контекст (что впрыснули) -> решение
  prompts.jsonl            полный текст системных инструкций, по одной записи на новый хеш

Полный промпт и инструкции хранятся специально: это материал для реплеев/пертурбаций.
Версионирование (code_version + instructions hash) — чтобы поведение можно было привязать
к конкретной версии персоны, иначе тренды по времени необъяснимы.
"""

import contextvars
import datetime
import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path

import orjson

LOGS_DIR = Path("/app/data/logs")
MAX_FIELD = 200_000   # страховка от аномально больших полей

_MSK = datetime.timezone(datetime.timedelta(hours=3))

_RE_LATIN_WORD = re.compile(r'\b[A-Za-z]{3,}\b')
_RE_FOREIGN = re.compile(r'[一-鿿぀-ヿ֐-׿؀-ۿ가-힯]')
_RE_MD = re.compile(r'\*\*|__|`|^\s{0,3}#{1,6}\s', re.MULTILINE)
_RE_EMOJI = re.compile(r'[\U0001F300-\U0001FAFF☀-➿]')

# ── контекст хода: связывает все вызовы модели, порождённые одним событием ──
_turn = contextvars.ContextVar("rin_turn", default=None)

_seen_prompt_hashes: set[str] = set()
_code_version: str | None = None


def _now():
    return datetime.datetime.now(_MSK)


def _clip(v):
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    return s[:MAX_FIELD]


def code_version() -> str:
    """SHA версии кода — чтобы привязать поведение к версии промптов/персоны."""
    global _code_version
    if _code_version is None:
        v = os.getenv("GIT_SHA", "").strip()
        if not v:
            try:
                v = subprocess.run(
                    ["git", "-C", "/app", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
            except Exception:
                v = ""
        _code_version = v or "unknown"
    return _code_version


def _append(kind: str, rec: dict):
    """Дописать строку в дневной JSONL. Никогда не бросает."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        suffix = "" if kind == "prompts" else f"-{_now().strftime('%Y-%m-%d')}"
        with open(LOGS_DIR / f"{kind}{suffix}.jsonl", "ab") as f:
            f.write(orjson.dumps(rec) + b"\n")
    except Exception:
        pass


# ═══════════════════════════ ХОД (trace) ═══════════════════════════

def new_turn(trigger: str, conversation_id=None, meta: dict | None = None) -> str:
    """Начать новый ход: все последующие вызовы модели свяжутся с ним."""
    tid = uuid.uuid4().hex[:12]
    _turn.set({"turn_id": tid, "trigger": trigger, "conversation_id": conversation_id,
               "seq": 0, "meta": meta or {}})
    return tid


def _turn_fields() -> dict:
    t = _turn.get()
    if not t:
        return {"turn_id": None, "trigger": None, "conversation_id": None, "turn_seq": 0}
    t["seq"] += 1
    return {"turn_id": t["turn_id"], "trigger": t["trigger"],
            "conversation_id": t["conversation_id"], "turn_seq": t["seq"]}


def current_turn_id():
    t = _turn.get()
    return t["turn_id"] if t else None


# ═══════════════════════ МЕТРИКИ (без LLM) ═══════════════════════

def text_metrics(text: str) -> dict:
    """Детерминированные метрики текста — объективно и бесплатно."""
    t = text or ""
    letters = [c for c in t if c.isalpha()]
    cyr = sum(1 for c in letters if 'а' <= c.lower() <= 'я' or c.lower() == 'ё')
    return {
        "chars": len(t),
        "words": len(t.split()),
        "cyr_ratio": round(cyr / len(letters), 3) if letters else None,
        "latin_words": len(_RE_LATIN_WORD.findall(t)),
        "foreign_script": bool(_RE_FOREIGN.search(t)),
        "markdown": bool(_RE_MD.search(t)),
        "emdash": "—" in t,
        "emoji": len(_RE_EMOJI.findall(t)),
        "parens": t.count(")"),
        "questions": t.count("?"),
    }


# ═══════════ РАСПАКОВКА ВНУТРЕННОСТЕЙ ОТВЕТА (best-effort) ═══════════

def _to_dict(obj):
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    try:
        return dict(vars(obj))
    except Exception:
        return {}


def _find_keys(obj, keys: set[str], depth: int = 0, found: dict | None = None) -> dict:
    """Рекурсивно ищет интересующие ключи в вложенных структурах SDK."""
    if found is None:
        found = {}
    if depth > 6 or len(found) >= len(keys):
        return found
    try:
        if not isinstance(obj, (dict, list, tuple)):
            obj = _to_dict(obj)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and k not in found and v not in (None, "", [], {}):
                    found[k] = v
                if isinstance(v, (dict, list, tuple)) or hasattr(v, "__dict__"):
                    _find_keys(v, keys, depth + 1, found)
        elif isinstance(obj, (list, tuple)):
            for v in obj[:20]:
                _find_keys(v, keys, depth + 1, found)
    except Exception:
        pass
    return found


def _response_meta(result) -> dict:
    """finish_reason, reasoning, snapshot модели, детали токенов — если SDK их отдаёт."""
    meta = {}
    try:
        raws = getattr(result, "raw_responses", None) or []
        f = _find_keys(raws, {"finish_reason", "stop_reason", "reasoning_content",
                              "reasoning", "cached_tokens", "reasoning_tokens", "model"})
        if f.get("finish_reason") or f.get("stop_reason"):
            meta["finish_reason"] = str(f.get("finish_reason") or f.get("stop_reason"))
        rc = f.get("reasoning_content") or f.get("reasoning")
        if rc:
            meta["reasoning"] = _clip(rc)
        for k in ("cached_tokens", "reasoning_tokens"):
            if isinstance(f.get(k), int):
                meta[k] = f[k]
        if isinstance(f.get("model"), str):
            meta["model_snapshot"] = f["model"]
    except Exception:
        pass
    return meta


def _tool_calls(result) -> list:
    """Тул-вызовы хода: имя, аргументы, результат."""
    tools = []
    try:
        for it in (getattr(result, "new_items", None) or [])[:60]:
            t = str(getattr(it, "type", ""))
            if "tool" not in t:
                continue
            d = _to_dict(getattr(it, "raw_item", None) or it)
            fn = d.get("function") if isinstance(d.get("function"), dict) else {}
            tools.append({
                "kind": t,
                "name": d.get("name") or fn.get("name"),
                "args": _clip(d.get("arguments") or fn.get("arguments"))[:4000] if (d.get("arguments") or fn.get("arguments")) else None,
                "output": _clip(d.get("output"))[:4000] if d.get("output") else None,
            })
    except Exception:
        pass
    return tools


def _prompt_hash(agent) -> str | None:
    """Хеш системных инструкций + разовый дамп полного текста при новой версии."""
    try:
        instr = getattr(agent, "instructions", None)
        if not isinstance(instr, str) or not instr:
            return None
        h = hashlib.sha256(instr.encode("utf-8")).hexdigest()[:12]
        if h not in _seen_prompt_hashes:
            _seen_prompt_hashes.add(h)
            _append("prompts", {
                "ts": _now().isoformat(),
                "agent": getattr(agent, "name", "?"),
                "instructions_hash": h,
                "code_version": code_version(),
                "instructions": _clip(instr),
            })
        return h
    except Exception:
        return None


# ═══════════════════════════ ЗАПИСИ ═══════════════════════════

def log_message(peer_id: int, from_id: int, name: str, text: str, is_rin: bool,
                extra: dict | None = None):
    """Каждое сообщение в чате — от всех людей и от Рин."""
    rec = {
        "ts": _now().isoformat(),
        "peer_id": peer_id,
        "from_id": from_id,
        "name": name,
        "is_rin": is_rin,
        "text": _clip(text),
        "turn_id": current_turn_id(),
        "code_version": code_version(),
    }
    if is_rin:
        rec["metrics"] = text_metrics(text)
    if extra:
        rec.update(extra)     # reply_to_rin, reply_latency_s, cmid и т.п.
    _append("chat", rec)


def log_call(agent, prompt, result, ms: int, error: str | None = None,
             ttft_ms: int | None = None):
    """Каждый вызов модели: полный промпт + инструкции(хеш) + вывод + токены + внутренности."""
    rec = {"ts": _now().isoformat(), "agent": getattr(agent, "name", "?"),
           "ms": ms, "ttft_ms": ttft_ms, "error": error,
           "code_version": code_version()}
    rec.update(_turn_fields())
    rec["instructions_hash"] = _prompt_hash(agent)
    try:
        rec["model"] = getattr(getattr(agent, "model", None), "model", None)
    except Exception:
        rec["model"] = None
    try:
        s = getattr(agent, "model_settings", None)
        rec["settings"] = {
            "temperature": getattr(s, "temperature", None),
            "top_p": getattr(s, "top_p", None),
            "max_tokens": getattr(s, "max_tokens", None),
            "extra_body": getattr(s, "extra_body", None),
        }
    except Exception:
        pass
    rec["prompt"] = _clip(prompt)
    try:
        rec["output"] = _clip(getattr(result, "final_output", None))
    except Exception:
        rec["output"] = None
    try:
        u = result.context_wrapper.usage
        rec["tokens_in"] = int(getattr(u, "input_tokens", 0) or 0)
        rec["tokens_out"] = int(getattr(u, "output_tokens", 0) or 0)
        rec["requests"] = int(getattr(u, "requests", 0) or 0)
    except Exception:
        pass
    if result is not None:
        rec.update(_response_meta(result))
        # finish_reason SDK наружу не отдаёт — приближаем обрезку по лимиту токенов
        try:
            mt = (rec.get("settings") or {}).get("max_tokens")
            if mt and rec.get("tokens_out") and rec["tokens_out"] >= mt * 0.98:
                rec["truncated"] = True
        except Exception:
            pass
        tools = _tool_calls(result)
        if tools:
            rec["tools"] = tools
    _append("calls", rec)


def log_turn(trigger: str, context: dict, decision: dict):
    """Ход целиком: какой контекст впрыснули -> какое решение приняла.

    context — СТРУКТУРИРОВАННО (self_state, эпизоды, рефлексии, память, саммари, mood…),
    чтобы потом можно было делать пертурбации (убрать эпизоды / подменить состояние).
    """
    rec = {
        "ts": _now().isoformat(),
        "turn_id": current_turn_id(),
        "trigger": trigger,
        "code_version": code_version(),
        "context": context,
        "decision": decision,
    }
    if decision.get("text"):
        rec["metrics"] = text_metrics(decision["text"])
    _append("turns", rec)
