"""Харнесс реплеев: прогон реальных ходов с изменённым контекстом.

Берёт залогированные ходы (`turns-*.jsonl`), применяет к структурированному
контексту заданные пертурбации, пересобирает промпт ТОЙ ЖЕ функцией, что и
боевой путь (`build_chat_prompt`), и прогоняет k сэмплов на условие.

Отсюда берётся статистическая мощность: единица наблюдения — решение модели,
а не человек (людей в сообществе ~12). Сравнения парные внутри хода: каждый ход
сам себе контроль.

Запуск (внутри контейнера):
    docker exec rin-bot python -m src.research.replay --date 2026-08-02 --k 3
    docker exec rin-bot python -m src.research.replay --limit 20 --conditions baseline,no_episodes

БЕЗОПАСНОСТЬ: реплей-агент создаётся БЕЗ тулов — копия чат-агента с теми же
инструкциями и семплингом, но без возможности отправить файл, сгенерировать
картинку или запустить творческую сессию. Иначе эксперимент имел бы побочные
эффекты в реальном чате.
"""

import argparse
import asyncio
import glob
import random
import time
from pathlib import Path

import orjson
from agents import Agent

from src.handlers.chat.agents import chat_agent
from src.handlers.chat.prompt import build_chat_prompt, BLOCK_ORDER
from src.handlers.chat.utils import parse_response, _sanitize_chat_text
from src.handlers.chat.datalog import text_metrics, new_turn, _append, code_version
from src.utils import run_agent_streamed

LOGS_DIR = Path("/app/data/logs")

# Клон чат-агента без тулов — никаких побочных эффектов
replay_agent = Agent(
    model=chat_agent.model,
    name="Рин (реплей)",
    model_settings=chat_agent.model_settings,
    instructions=chat_agent.instructions,
)


# ═══════════════════════ ПЕРТУРБАЦИИ ═══════════════════════

def _drop(field, empty):
    def f(ctx, turn_id):
        ctx[field] = empty
        return ctx, None
    return f


def _shuffle_middle(ctx, turn_id):
    """Перемешать средние блоки, оставив ситуацию первой и сообщение последним.
    Порядок детерминирован по turn_id — эксперимент воспроизводим."""
    middle = [b for b in BLOCK_ORDER if b not in ("situation", "message")]
    rnd = random.Random(f"shuffle:{turn_id}")
    rnd.shuffle(middle)
    return ctx, ["situation"] + middle + ["message"]


def _truncate_history(ctx, turn_id):
    """Обрезать окно истории вдвое (саммари остаётся)."""
    cc = ctx.get("chat_context")
    if cc:
        lines = cc.split("\n")
        ctx["chat_context"] = "\n".join(lines[: max(1, len(lines) // 2)])
    return ctx, None


CONDITIONS = {
    "baseline":         lambda ctx, t: (ctx, None),
    "no_episodes":      _drop("episodes", []),
    "no_self_state":    _drop("self_state", []),
    "no_participants":  _drop("participants_memory", None),
    "no_user_facts":    _drop("user_facts", []),
    "no_chat_context":  _drop("chat_context", None),
    "no_community":     _drop("community", None),
    "no_mood":          _drop("mood", ""),
    "shuffle_order":    _shuffle_middle,
    "truncate_history": _truncate_history,
}


# ═══════════════════════ ЗАГРУЗКА ХОДОВ ═══════════════════════

def load_turns(date: str | None, limit: int | None, trigger: str = "chat") -> list[dict]:
    pattern = f"turns-{date}.jsonl" if date else "turns-*.jsonl"
    turns = []
    for path in sorted(glob.glob(str(LOGS_DIR / pattern))):
        with open(path, "rb") as f:
            for line in f:
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if rec.get("trigger") != trigger:
                    continue
                if not rec.get("context", {}).get("input"):
                    continue
                turns.append(rec)
    if limit:
        turns = turns[-limit:]
    return turns


# ═══════════════════════ ПРОГОН ═══════════════════════

async def run_one(turn: dict, cond_name: str, sample: int, run_id: str, sem: asyncio.Semaphore) -> dict:
    ctx = orjson.loads(orjson.dumps(turn["context"]))   # глубокая копия
    turn_id = turn.get("turn_id") or "?"
    ctx, order = CONDITIONS[cond_name](ctx, turn_id)
    prompt = build_chat_prompt(ctx, order)

    rec = {
        "run_id": run_id,
        "turn_id": turn_id,
        "condition": cond_name,
        "sample": sample,
        "code_version": code_version(),
        "prompt_chars": len(prompt),
        "orig_silent": bool(turn.get("decision", {}).get("silent")),
        "orig_text": turn.get("decision", {}).get("text"),
    }
    async with sem:
        started = time.monotonic()
        try:
            result = await run_agent_streamed(replay_agent, prompt)
            raw = result.final_output
            rec["ms"] = int((time.monotonic() - started) * 1000)
            try:
                u = result.context_wrapper.usage
                rec["tokens_in"] = int(getattr(u, "input_tokens", 0) or 0)
                rec["tokens_out"] = int(getattr(u, "output_tokens", 0) or 0)
            except Exception:
                pass
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            _append(f"replay-{run_id}", rec)
            return rec

    r = parse_response(raw)
    r.text = _sanitize_chat_text(r.text)
    rec["silent"] = not bool(r.text)
    rec["text"] = r.text
    rec["reaction"] = r.reaction
    rec["remember"] = r.remember
    rec["self_update"] = r.self_update
    rec["episode"] = r.episode
    if r.text:
        rec["metrics"] = text_metrics(r.text)
    _append(f"replay-{run_id}", rec)
    return rec


async def main():
    ap = argparse.ArgumentParser(description="Реплей ходов с пертурбациями контекста")
    ap.add_argument("--date", help="YYYY-MM-DD; по умолчанию все дни")
    ap.add_argument("--limit", type=int, help="взять только последние N ходов")
    ap.add_argument("--k", type=int, default=3, help="сэмплов на условие (temp>0 → нужен повтор)")
    ap.add_argument("--conditions", default=",".join(CONDITIONS),
                    help="через запятую: " + ", ".join(CONDITIONS))
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    conds = [c.strip() for c in args.conditions.split(",") if c.strip()]
    bad = [c for c in conds if c not in CONDITIONS]
    if bad:
        raise SystemExit(f"неизвестные условия: {bad}\nдоступны: {list(CONDITIONS)}")

    turns = load_turns(args.date, args.limit)
    if not turns:
        raise SystemExit("ходов не найдено — логи ещё не накопились?")

    run_id = args.run_id or f"{int(time.time())}"
    total = len(turns) * len(conds) * args.k
    print(f"ходов: {len(turns)} × условий: {len(conds)} × сэмплов: {args.k} = {total} прогонов")
    print(f"run_id: {run_id} → {LOGS_DIR}/replay-{run_id}.jsonl")

    new_turn(f"replay:{run_id}")   # чтобы вызовы в calls-*.jsonl были помечены
    sem = asyncio.Semaphore(args.concurrency)
    tasks = [
        run_one(t, c, s, run_id, sem)
        for t in turns for c in conds for s in range(args.k)
    ]
    done = 0
    results = []
    for fut in asyncio.as_completed(tasks):
        results.append(await fut)
        done += 1
        if done % 10 == 0 or done == total:
            print(f"  {done}/{total}")

    # сводка: доля молчания по условиям
    print("\nдоля молчания по условиям:")
    for c in conds:
        rs = [r for r in results if r["condition"] == c and "error" not in r]
        if rs:
            silent = sum(1 for r in rs if r.get("silent"))
            print(f"  {c:<18} {silent}/{len(rs)} = {silent / len(rs):.0%}")
    errs = sum(1 for r in results if "error" in r)
    if errs:
        print(f"\nошибок: {errs}")


if __name__ == "__main__":
    asyncio.run(main())
