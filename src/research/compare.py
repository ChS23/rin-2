"""Сравнение прогонов реплея — контрольный прогон перед сменой модели или промпта.

    python -m src.research.compare RUN_A RUN_B [RUN_C ...]

Для каждого прогона: доля молчания, длина, язык, markdown, эмодзи, утечки «кухни»
(свои файлы, модели, правила) и задержка. Плюс парное сравнение по одним и тем же
ходам: сколько ходов прогон B «хуже» A по утечкам кухни.
"""
import glob
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import orjson

LOGS_DIR = Path("/app/data/logs")

# Кухня: её файлы, устройство памяти, модели и правила — то, что не выносят в чат.
KITCHEN_RE = re.compile(
    r"notes\.md|roadmap|story_bible|\.md\b|досье|провайдер|промпт|инструкци|harness|"
    r"\bмодел(ь|и|ью|ька|ьку|ьке)\b|поколени|апгрейд|нельзя (ставить )?тир|перечитаю (память|заметки)",
    re.IGNORECASE)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def load(run_id: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(str(LOGS_DIR / f"replay-{run_id}*.jsonl"))):
        with open(path, "rb") as f:
            rows += [orjson.loads(line) for line in f if line.strip()]
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    spoke = [r for r in ok if r.get("text")]
    m = [r.get("metrics") or {} for r in spoke]
    ms = [r["ms"] for r in ok if r.get("ms")]
    return {
        "прогонов": len(rows),
        "ошибок": len(rows) - len(ok),
        "молчание": 1 - len(spoke) / max(len(ok), 1),
        "символов": st.mean(x.get("chars", 0) for x in m) if m else 0,
        "кириллица": st.mean(x.get("cyr_ratio", 0) for x in m) if m else 0,
        "лат.слов": st.mean(x.get("latin_words", 0) for x in m) if m else 0,
        "markdown": sum(bool(x.get("markdown")) for x in m) / max(len(m), 1),
        "эмодзи": sum(bool(EMOJI_RE.search(r["text"])) for r in spoke) / max(len(spoke), 1),
        "кухня": sum(bool(KITCHEN_RE.search(r["text"])) for r in spoke) / max(len(spoke), 1),
        "мс, медиана": st.median(ms) if ms else 0,
    }


def main(runs: list[str]):
    data = {r: load(r) for r in runs}
    for r, rows in data.items():
        if not rows:
            raise SystemExit(f"прогон {r} не найден в {LOGS_DIR}")
    cols = list(summarize(next(iter(data.values()))).keys())
    width = max(len(r) for r in runs) + 2
    print("прогон".ljust(width) + "".join(c.rjust(12) for c in cols))
    for r, rows in data.items():
        s = summarize(rows)
        cells = []
        for c in cols:
            v = s[c]
            if c in ("молчание", "markdown", "эмодзи", "кухня"):
                cells.append(f"{v:.0%}".rjust(12))
            elif isinstance(v, float):
                cells.append(f"{v:.2f}".rjust(12) if v < 10 else f"{v:.0f}".rjust(12))
            else:
                cells.append(str(v).rjust(12))
        print(r.ljust(width) + "".join(cells))

    # парно по ходам: у кого кухня протекает там, где у первого прогона нет
    base = runs[0]
    by_turn = defaultdict(dict)
    for r, rows in data.items():
        for row in rows:
            if row.get("text"):
                by_turn[row["turn_id"]].setdefault(r, []).append(bool(KITCHEN_RE.search(row["text"])))
    print(f"\nкухня по ходам (доля сэмплов с утечкой), база — {base}:")
    for tid, per in sorted(by_turn.items()):
        if any(any(v) for v in per.values()):
            print(f"  {tid}: " + "  ".join(f"{r}={sum(v)}/{len(v)}" for r, v in per.items()))

    print("\nпримеры (первый сэмпл каждого хода, где прогоны расходятся по кухне):")
    shown = 0
    for tid, per in by_turn.items():
        if len({any(v) for v in per.values()}) > 1 and shown < 5:
            shown += 1
            print(f"  ход {tid}:")
            for r, rows in data.items():
                t = next((x["text"] for x in rows if x.get("turn_id") == tid and x.get("text")), "")
                print(f"    {r}: {t[:150]}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    main(sys.argv[1:])
