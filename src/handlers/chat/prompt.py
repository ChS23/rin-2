"""Единая сборка промпта чата.

Используется И боевым путём (`chat_with_rin`), И харнессом реплеев
(`src/research/replay.py`). Это принципиально: если бы харнесс собирал промпт
своей копией кода, эксперименты воспроизводили бы не то, что реально происходило
в проде, и любые выводы были бы недействительны.

Формат контекста (`ctx`) — ровно то, что пишется в `turns-*.jsonl`.
"""

# Порядок блоков: ситуация → self → чат → другие → community → внутряки →
# факты о собеседнике → сообщение.
# Факты и само сообщение в конце намеренно — recency effect.
BLOCK_ORDER = [
    "situation", "gap", "self_state", "chat_context", "participants",
    "community", "episodes", "user_facts", "message",
]


def _block(name: str, ctx: dict) -> str | None:
    if name == "situation":
        return f"Сейчас: {ctx.get('now_str', '')}. Ты сейчас {ctx.get('mood', '')}."

    if name == "gap":
        gap = ctx.get("gap_days")
        if gap is None or gap < 3:
            return None
        return (
            f"Тебя не было в чате {gap} дней — просто выпала на время, так бывает. "
            "История переписки и саммари, которые ты видишь, — это то, что было ДО твоего исчезновения, старые разговоры, а не свежие. "
            "Вернись легко, как живой человек. НЕ придумывай причин, почему пропала (никаких сдохших ноутов, отключений света, разъездов, стажировок) и не оправдывайся — если не спросят, тему вообще не поднимай. Просто снова тут. Не делай вид, что разговор не прерывался."
        )

    if name == "self_state":
        ss = ctx.get("self_state") or []
        if not ss:
            return None
        return ("Твоё состояние и настроение (фон для тебя, НЕ зачитывай списком):\n"
                + "\n".join(f"- {s}" for s in ss))

    if name == "chat_context":
        return ctx.get("chat_context") or None

    if name == "participants":
        pm = ctx.get("participants_memory")
        return f"Что ты помнишь об участниках разговора:\n{pm}" if pm else None

    if name == "community":
        com = ctx.get("community")
        return f"Инфо о сообществе:\n{com}" if com else None

    if name == "episodes":
        eps = ctx.get("episodes") or []
        if not eps:
            return None
        return ("Ваши реальные внутряки (можешь ненавязчиво сослаться к месту; НЕ выдумывай новых):\n"
                + "\n".join(f"- {e}" for e in eps))

    if name == "user_facts":
        facts = ctx.get("user_facts") or []
        days = ctx.get("days_since_user")
        who = ctx.get("user_name", "")
        if facts:
            out = f"Что ты помнишь о {who}:\n" + "\n".join(f"- {f}" for f in facts)
            if days is not None and days >= 7:
                out += f"\n(Последний раз общались {days} дней назад)"
            return out
        if days is None:
            return f"({who} впервые пишет тебе)"
        if days >= 7:
            return f"({who} не заходил {days} дней)"
        return None

    if name == "message":
        msg = f"{ctx.get('user_name', '')} обращается к тебе: {ctx.get('input', '')}"
        att = ctx.get("attachments")
        if att:
            msg += "\nПрикреплено: " + ", ".join(att)
        return msg

    return None


def build_chat_prompt(ctx: dict, order: list[str] | None = None) -> str:
    """Собрать промпт из структурированного контекста.

    order — можно передать изменённый порядок блоков (для пертурбаций).
    """
    parts = []
    for name in (order or BLOCK_ORDER):
        b = _block(name, ctx)
        if b:
            parts.append(b)
    return "\n\n".join(parts)
