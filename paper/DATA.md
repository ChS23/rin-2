# Данные: что логируется и как с этим работать

Инструментирование включено **01.08.2026** (коммиты `abf8ac2` → `12eeaa5`).
До этой даты систематических данных нет: в Valkey скользящее окно 100 сообщений,
docker-логи не датасет. Всё, что было раньше, — анекдотические наблюдения.

## Где лежит

Контейнер `rin-bot`, каталог **`/app/data/logs/`** (том `rin-2_bot_data`).
Append-only, ротации нет. Объём ~1–2 МБ/день.

**Бэкап:** контейнер `backup` (restic) монтирует `rin-2_bot_data` только на чтение
и каждую ночь в 03:30 льёт в S3 (`s3.firstvds.ru/sergeivolchkov-backups`).

> ⚠️ **Дыра:** том `rin-2_valkey_data` в бэкап **не включён**. Там живут
> `rin:self:state` (легенда), эпизоды, окно истории, лог решений гейта, учёт
> токенов. Файловый датасет это не затрагивает, но живое состояние агента —
> в одной копии.

## Потоки

### `chat-YYYY-MM-DD.jsonl` — каждое сообщение

Все люди + сама Рин.

```
ts, peer_id, from_id, name, is_rin, text, turn_id, code_version
metrics: {chars, words, cyr_ratio, latin_words, foreign_script,
          markdown, emdash, emoji, parens, questions}   # только для её реплик
cmid, reply_to_rin, reply_to_cmid, reply_latency_s, mentions_rin  # вовлечённость
```

`reply_to_rin` / `reply_latency_s` — сигнал вовлечённости: ответили ли агенту и
через сколько. Основной измеримый мост «решение агента → поведение людей».

### `calls-YYYY-MM-DD.jsonl` — каждый вызов модели

Пишется в единственной точке входа всех агентов (`run_agent_streamed`), поэтому
покрывает чат, гейт, рефлексии, self_state, саммари, creative, аудио.

```
ts, agent, model, model_snapshot, code_version, instructions_hash
turn_id, trigger, turn_seq, conversation_id
settings: {temperature, top_p, max_tokens, extra_body}
prompt          # ПОЛНЫЙ — содержит весь впрыснутый контекст
output
tokens_in, tokens_out, requests, cached_tokens, reasoning_tokens
ms, ttft_ms, error, truncated
tools: [{kind, name, args, output}]
reasoning       # если thinking включён
```

Полный промпт хранится намеренно: это материал для реплеев и пертурбаций.

### `turns-YYYY-MM-DD.jsonl` — ход целиком

```
ts, turn_id, trigger, code_version
context:  {user_id, user_name, input, attachments, mood, hour, weekday,
           gap_days, days_since_user, self_state[], episodes[], user_facts[],
           participants_memory, community, chat_context_chars, prompt_chars}
decision: {silent, text, reaction, remember, forget, self_update, episode,
           attachment}   # для проактивных: {act, gate_mode, text, why}
metrics:  {...}          # если что-то сказала
```

Именно **структурированный** контекст, а не строка, — чтобы делать пертурбации
(убрать эпизоды, подменить состояние, переставить порядок).

### `prompts.jsonl` — версии системных промптов

По одной записи на каждый новый хеш инструкций:
`ts, agent, instructions_hash, code_version, instructions` (полный текст).

Нужен, потому что персона правится часто: без этого поведение нельзя привязать к
версии, и любой временной тренд объясняется «тогда поменяли промпт».

## Триггеры (`trigger`)

`chat` · `initiative` · `proactive` · `cron:self_state` · `cron:reflection` ·
`creative`

Все вызовы модели, порождённые одним событием, разделяют `turn_id` —
включая суб-агентов (зрение, аудио, ревью сценария).

## Состояние в Valkey (не в файлах)

| Ключ | Что |
|---|---|
| `rin:self:state` / `:archive` | легенда агента (JSON-список, ≤15) |
| `rin:episodes` | внутряки (≤20) |
| `rin:chat:<peer>:history` / `:summary` | окно истории (100) и саммари |
| `rin:proactive:enabled` | `shadow` \| `live` \| отсутствует = выкл |
| `rin:proactive:shadow` | последние 100 решений гейта |
| `rin:proactive:usage` | токены фичи по дням |
| `rin:creative:paused` | пауза ночных творческих сессий |

Память о людях — файл `/app/data/rin_memory.json`: факты + поле `reflection`
(живая мысль «как Рин видит человека», пересобирается ночью в 03:30).

## Как забрать и анализировать

```bash
# выгрузить логи на Мак
ssh sergei@sergeivolchkov.ru 'docker exec rin-bot tar cz -C /app/data logs' > rin-logs.tgz
tar xzf rin-logs.tgz
```

```python
import pandas as pd, glob
turns = pd.concat([pd.read_json(f, lines=True) for f in glob.glob("logs/turns-*.jsonl")])
calls = pd.concat([pd.read_json(f, lines=True) for f in glob.glob("logs/calls-*.jsonl")])
chat  = pd.concat([pd.read_json(f, lines=True) for f in glob.glob("logs/chat-*.jsonl")])
```

## Известные ограничения логирования

- **`finish_reason` недоступен** — SDK его наружу не отдаёт; обрезка приближается
  флагом `truncated` (tokens_out ≥ 98% от max_tokens).
- **`reasoning`** появляется только там, где thinking включён; на лёгких агентах
  он выключен намеренно (скорость/стоимость).
- **Seed не поддерживается** провайдером → полная побитовая воспроизводимость
  недостижима. Компенсация: k сэмплов на условие + случайный эффект в модели.
- Реакции людей на сообщения агента через VK API не отслеживаются (событие
  реакций не обрабатывается) — вовлечённость меряется по ответам и упоминаниям.

## Псевдонимизация

Логи пишутся с реальными `user_id` и именами. Для любой выгрузки вовне —
заменять на стабильные псевдонимы (`P01`, `P02`, …), тексты сообщений
просматривать на предмет косвенных идентификаторов. Живые логи оставлять как есть.
