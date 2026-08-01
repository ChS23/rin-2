"""Даталог: полная история чата + каждый вызов модели, по дням в JSONL.

Пишется в /app/data/logs (том bot_data, попадает в бэкап). Append-only, best-effort:
логирование НИКОГДА не должно ронять бота — все ошибки глушатся.

Файлы (дата по МСК):
  chat-YYYY-MM-DD.jsonl   — каждое сообщение в чате (все люди + Рин), с метриками для её реплик
  calls-YYYY-MM-DD.jsonl  — каждый вызов модели: агент, модель, ПОЛНЫЙ промпт, вывод, токены, латентность

Полный промпт нужен специально: он содержит весь контекст (self_state, эпизоды,
рефлексии, память об участниках, саммари, mood) — это материал для реплеев/пертурбаций.
"""

import datetime
import re
from pathlib import Path

import orjson

LOGS_DIR = Path("/app/data/logs")
MAX_FIELD = 200_000   # страховка от аномально больших полей

_MSK = datetime.timezone(datetime.timedelta(hours=3))

_RE_LATIN_WORD = re.compile(r'\b[A-Za-z]{3,}\b')
_RE_FOREIGN = re.compile(r'[一-鿿぀-ヿ֐-׿؀-ۿ가-힯]')
_RE_MD = re.compile(r'\*\*|__|`|^\s{0,3}#{1,6}\s', re.MULTILINE)
_RE_EMOJI = re.compile(r'[\U0001F300-\U0001FAFF☀-➿]')


def _now():
    return datetime.datetime.now(_MSK)


def _clip(v):
    s = "" if v is None else str(v)
    return s[:MAX_FIELD]


def _append(kind: str, rec: dict):
    """Дописать строку в дневной JSONL. Никогда не бросает."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = LOGS_DIR / f"{kind}-{_now().strftime('%Y-%m-%d')}.jsonl"
        with open(path, "ab") as f:
            f.write(orjson.dumps(rec) + b"\n")
    except Exception:
        pass


def text_metrics(text: str) -> dict:
    """Детерминированные метрики текста — без LLM, объективно и бесплатно."""
    t = text or ""
    letters = [c for c in t if c.isalpha()]
    cyr = sum(1 for c in letters if 'а' <= c.lower() <= 'я' or c.lower() == 'ё')
    return {
        "chars": len(t),
        "words": len(t.split()),
        "cyr_ratio": round(cyr / len(letters), 3) if letters else None,
        "latin_words": len(_RE_LATIN_WORD.findall(t)),   # утечки англ. слов
        "foreign_script": bool(_RE_FOREIGN.search(t)),    # CJK/иврит/арабица/хангыль
        "markdown": bool(_RE_MD.search(t)),
        "emdash": "—" in t,
        "emoji": len(_RE_EMOJI.findall(t)),
        "parens": t.count(")"),                           # её стилевой маркер вместо эмодзи
        "questions": t.count("?"),
    }


def log_message(peer_id: int, from_id: int, name: str, text: str, is_rin: bool):
    """Каждое сообщение в чате — от всех людей и от Рин."""
    rec = {
        "ts": _now().isoformat(),
        "peer_id": peer_id,
        "from_id": from_id,
        "name": name,
        "is_rin": is_rin,
        "text": _clip(text),
    }
    if is_rin:
        rec["metrics"] = text_metrics(text)
    _append("chat", rec)


def log_call(agent, prompt, result, ms: int, error: str | None = None):
    """Каждый вызов модели: полный промпт + вывод + токены + латентность."""
    rec = {
        "ts": _now().isoformat(),
        "agent": getattr(agent, "name", "?"),
        "ms": ms,
        "error": error,
    }
    try:
        rec["model"] = getattr(getattr(agent, "model", None), "model", None)
    except Exception:
        rec["model"] = None
    try:
        ms_ = getattr(agent, "model_settings", None)
        rec["settings"] = {
            "temperature": getattr(ms_, "temperature", None),
            "top_p": getattr(ms_, "top_p", None),
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
    except Exception:
        pass
    _append("calls", rec)
