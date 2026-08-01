import asyncio
import datetime
import os
import random
import re
from pathlib import Path

import aiohttp
import orjson

import structlog
from vkbottle import BaseMiddleware
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule

from agents import RunHooks

from src.bot import api, rdb
from src.utils import run_agent_streamed
from src.handlers.checkin import ai_lock, REACTIONS, scheduler, CHAT_PEER_ID
from src.handlers.chat.agents import chat_agent, initiative_agent, gate_agent
from src.handlers.chat.tools import PENDING_FILE_KEY
from src.handlers.chat.memory import (
    record_message, get_context,
    remember_facts, forget_facts, maybe_compress_memory,
    get_user_memory, get_memory_for_participants, extract_participants_from_history,
    get_rin_self_state, refresh_rin_self_state, update_rin_self_state,
    update_last_seen, get_days_since, get_rin_gap_days,
    add_episode, get_episodes, refresh_all_reflections, dump_state_snapshot,
)
from src.handlers.chat.utils import (
    resolve_user_name, parse_response, get_community_context, _looks_like_refusal, _sanitize_chat_text,
)
from src.handlers.chat.datalog import new_turn, log_turn

logger = structlog.get_logger("chat.handler")
labeler = BotLabeler()


class ChatLoggingHooks(RunHooks):
    async def on_tool_start(self, context, agent, tool, **kwargs):
        await logger.ainfo("Tool call", agent=agent.name, tool=tool.name)

    async def on_tool_end(self, context, agent, tool, result, **kwargs):
        short = (str(result) or "")[:150]
        await logger.ainfo("Tool result", tool=tool.name, result=short)


_chat_hooks = ChatLoggingHooks()

GROUP_ID = 204871130


AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".flac", ".opus"}


async def _upload_doc(peer_id: int, file_path: str, _retry: int = 0) -> str | None:
    """Загрузить файл как документ VK и вернуть attachment string."""
    try:
        ext = Path(file_path).suffix.lower()
        doc_type = "audio_message" if ext in AUDIO_EXTS else "doc"
        upload_server = await api.docs.get_messages_upload_server(peer_id=peer_id, type=doc_type)
        async with aiohttp.ClientSession() as session:
            with open(file_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=Path(file_path).name, content_type="application/octet-stream")
                async with session.post(upload_server.upload_url, data=data) as resp:
                    raw = await resp.text()
                    try:
                        result = orjson.loads(raw)
                    except (orjson.JSONDecodeError, ValueError):
                        if _retry < 1:
                            await logger.awarn("VK upload: retry", path=file_path)
                            await asyncio.sleep(2)
                            return await _upload_doc(peer_id, file_path, _retry + 1)
                        await logger.awarn("VK upload: ответ не JSON", response=raw[:300], path=file_path)
                        return None
        if "file" not in result:
            if _retry < 1:
                await logger.awarn("VK upload: retry (no file)", path=file_path)
                await asyncio.sleep(2)
                return await _upload_doc(peer_id, file_path, _retry + 1)
            await logger.awarn("VK upload: нет поля file", response=raw[:300], path=file_path)
            return None
        saved = await api.docs.save(file=result["file"], title=Path(file_path).name)
        doc = saved.doc or saved.audio_message
        if not doc:
            await logger.awarn("VK upload: пустой ответ docs.save", path=file_path)
            return None
        await logger.ainfo("Файл загружен в VK", doc=f"doc{doc.owner_id}_{doc.id}")
        return f"doc{doc.owner_id}_{doc.id}"
    except Exception as e:
        await logger.awarn("Не удалось загрузить файл", error=str(e), path=file_path)
        return None


PASSIVE_REACTION_CHANCE = 0.08
MAX_PASSIVE_PER_DAY = 3
_seen_messages: set[int] = set()
_seen_max = 200

PASSIVE_COUNT_KEY = "rin:passive_reactions:{date}"


# ═══════════════════════════════════════════════════════════
#                    ПАССИВНЫЕ РЕАКЦИИ
# ═══════════════════════════════════════════════════════════

async def _check_passive_limit() -> bool:
    key = PASSIVE_COUNT_KEY.format(date=datetime.date.today().isoformat())
    count = await rdb.get(key)
    return int(count or 0) < MAX_PASSIVE_PER_DAY


async def _incr_passive_count():
    key = PASSIVE_COUNT_KEY.format(date=datetime.date.today().isoformat())
    await rdb.incr(key)
    await rdb.expire(key, 86400)


async def _do_passive_reaction(message: Message):

    has_photo = message.attachments and any(
        a.type.value == "photo" for a in message.attachments if a.type
    )
    text = (message.text or "").lower()

    reaction_id = None
    if has_photo:
        reaction_id = REACTIONS[random.choice(["fire", "heart", "like"])]
    elif any(w in text for w in ["готово", "сделал", "сделала", "закончил", "закончила", "дописал", "дорисовал"]):
        reaction_id = REACTIONS[random.choice(["fire", "party", "like"])]
    elif any(w in text for w in ["помогите", "не работает", "баг", "сломал"]):
        reaction_id = REACTIONS[random.choice(["cry", "pray"])]
    elif random.random() < PASSIVE_REACTION_CHANCE:
        reaction_id = REACTIONS[random.choice(["like", "fire", "smile"])]

    if reaction_id:
        try:
            await api.request("messages.sendReaction", {
                "peer_id": message.peer_id,
                "cmid": message.conversation_message_id,
                "reaction_id": reaction_id,
            })
            await _incr_passive_count()
            await logger.ainfo("Пассивная реакция",
                user_id=message.from_id,
                reaction_id=reaction_id,
                has_photo=has_photo,
            )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
#                    MIDDLEWARE + RULES
# ═══════════════════════════════════════════════════════════

class ChatHistoryMiddleware(BaseMiddleware[Message]):
    async def pre(self):
        msg = self.event
        if msg.peer_id > 2000000000:
            # Дедупликация — vkbottle может обработать одно сообщение дважды
            cmid = msg.conversation_message_id
            if cmid in _seen_messages:
                return
            _seen_messages.add(cmid)
            if len(_seen_messages) > _seen_max:
                _seen_messages.clear()

            text = msg.text or ""
            att_desc = await _extract_attachments(msg)
            if att_desc:
                text = (text + " " + " ".join(att_desc)).strip()
            if text:
                # сигнал вовлечённости: ответили ли Рин и через сколько
                extra = {"cmid": cmid}
                try:
                    rm = msg.reply_message
                    if rm and rm.from_id == -GROUP_ID:
                        extra["reply_to_rin"] = True
                        extra["reply_to_cmid"] = rm.conversation_message_id
                        if rm.date:
                            extra["reply_latency_s"] = int(
                                datetime.datetime.now().timestamp() - rm.date)
                    if msg.from_id != -GROUP_ID and _NAME_RE.search(text):
                        extra["mentions_rin"] = True
                except Exception:
                    pass
                await record_message(msg.peer_id, msg.from_id, text, resolve_user_name, extra=extra)
                name = await resolve_user_name(msg.from_id) if msg.from_id > 0 else "бот"
                await logger.adebug("Сообщение в чате", user=name, text=text[:80])
            # Timestamp для creative agent (проверка тишины)
            if msg.from_id != -GROUP_ID:
                await rdb.set(
                    f"rin:chat:{msg.peer_id}:last_msg_ts",
                    datetime.datetime.now().isoformat(),
                    ex=86400,
                )
            if msg.from_id != -GROUP_ID and await _check_passive_limit():
                await _do_passive_reaction(msg)


labeler.message_view.register_middleware(ChatHistoryMiddleware)


# Имя как отдельное слово + склонения. \b защищает от "Марина", "принтер", "ринг".
_NAME_RE = re.compile(r'\b(рин|рина|рину|рине|рины|рином|ринчик|ринка|ринку)\b', re.IGNORECASE)


class MentionsBot(ABCRule[Message]):
    async def check(self, event: Message):
        if not event.peer_id > 2000000000:
            return False
        # Прямое обращение — отвечаем всегда
        if event.reply_message and event.reply_message.from_id == -GROUP_ID:
            return True
        if event.fwd_messages and any(f.from_id == -GROUP_ID for f in event.fwd_messages):
            return True
        if event.is_mentioned:  # нативное упоминание тегом [club…|]
            return True
        text_lower = (event.text or "").lower()
        if "@rinchan_bot" in text_lower:
            return True
        # Обращение по имени — отвечаем мягко (кулдаун в хендлере + модель сама решает молчать)
        if event.text and _NAME_RE.search(event.text):
            return {"by_name": True}
        return False


# ═══════════════════════════════════════════════════════════
#                       ХЕНДЛЕР ЧАТА
# ═══════════════════════════════════════════════════════════

GROQ_API_KEY = os.getenv("GROQ_API_KEY")


async def _transcribe_audio(url: str) -> str | None:
    """Транскрибировать аудио через Groq Whisper."""
    if not GROQ_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                audio_data = await resp.read()

        import io
        form = aiohttp.FormData()
        form.add_field("file", io.BytesIO(audio_data), filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3-turbo")
        form.add_field("language", "ru")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("text", "").strip()
        return None
    except Exception as e:
        await logger.awarn("Whisper transcription failed", error=str(e))
        return None


async def _probe_audio(url: str) -> str:
    """Скачать аудио и получить метаданные через ffprobe."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return "не удалось скачать"
                data = await resp.read()
        tmp = Path("/tmp/probe_audio")
        tmp.write_bytes(data)
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        tmp.unlink(missing_ok=True)
        import json
        info = json.loads(stdout.decode())
        fmt = info.get("format", {})
        duration = float(fmt.get("duration", 0))
        mins = int(duration) // 60
        secs = int(duration) % 60
        bitrate = int(fmt.get("bit_rate", 0)) // 1000
        codec = ""
        for s in info.get("streams", []):
            if s.get("codec_type") == "audio":
                codec = s.get("codec_name", "")
                break
        parts = [f"{mins}:{secs:02d}"]
        if codec:
            parts.append(codec)
        if bitrate:
            parts.append(f"{bitrate}kbps")
        size_kb = len(data) // 1024
        parts.append(f"{size_kb}KB")
        return ", ".join(parts)
    except Exception:
        return "аудиофайл"


async def _extract_attachments(message: Message) -> list[str]:
    """Извлечь описания аттачментов из сообщения (async операции параллельно)."""
    if not message.attachments:
        return []

    async def _process_one(att) -> str | None:
        if not att.type:
            return None
        t = att.type.value
        if t == "photo" and att.photo:
            sizes = att.photo.sizes or []
            url = max(sizes, key=lambda s: (s.width or 0) * (s.height or 0)).url if sizes else None
            return f"[фото: {url}]" if url else None
        elif t == "video" and att.video:
            title = att.video.title or "видео"
            return f"[видео: {title}, {att.video.duration or 0}с]"
        elif t == "audio" and att.audio:
            artist = att.audio.artist or "?"
            title = att.audio.title or "?"
            dur = att.audio.duration or 0
            return f"[аудио: {artist} — {title}, {dur // 60}:{dur % 60:02d}]"
        elif t == "doc" and att.doc:
            title = att.doc.title or "файл"
            ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
            if ext in ("ogg", "mp3", "wav", "flac", "opus", "m4a", "aac"):
                meta = await _probe_audio(att.doc.url)
                return f"[аудиофайл: {title}, {meta}]"
            return f"[файл: {title}, {att.doc.size} байт, url={att.doc.url}]"
        elif t == "audio_message" and att.audio_message:
            transcript = await _transcribe_audio(att.audio_message.link_ogg)
            if transcript:
                return f'[голосовое ({att.audio_message.duration}с): "{transcript}"]'
            return f"[голосовое: {att.audio_message.duration}с, не удалось расшифровать]"
        elif t == "sticker" and att.sticker:
            return "[стикер]"
        elif t == "link" and att.link:
            return f"[ссылка: {att.link.url}]"
        elif t == "wall" and att.wall:
            return "[репост записи]"
        elif t == "poll" and att.poll:
            return f"[опрос: {att.poll.question or 'опрос'}]"
        elif t == "graffiti":
            return "[граффити]"
        elif t == "story":
            return "[история]"
        return f"[{t}]"

    results = await asyncio.gather(*[_process_one(att) for att in message.attachments])
    return [r for r in results if r]


@labeler.chat_message(MentionsBot())
async def chat_with_rin(message: Message, by_name: bool = False):
    # Триггер по имени (не прямой пинг) — мягкий анти-спам: не чаще раза в 20с на чат,
    # чтобы в потоке "Рин… Рин…" не строчила дорогими LLM-вызовами.
    if by_name:
        cd_key = f"rin:name_cooldown:{message.peer_id}"
        if await rdb.exists(cd_key):
            return
        await rdb.set(cd_key, "1", ex=20)
    text = message.text or ""
    text = re.sub(r'\[club\d+\|[^\]]*\]', '', text).strip()
    text = re.sub(r'@rinchan_bot', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'@club\d+', '', text, flags=re.IGNORECASE).strip()

    if not text:
        text = "привет"

    user_name = await resolve_user_name(message.from_id)
    new_turn("chat", message.peer_id, {"user_id": message.from_id, "user_name": user_name})

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))  # МСК (UTC+3) явно, не наивное серверное время
    context = await get_context(message.peer_id)
    user_facts = get_user_memory(message.from_id)
    community = get_community_context()
    self_state = await get_rin_self_state()
    days_since = await get_days_since(message.from_id)
    gap_days = await get_rin_gap_days()
    episodes = await get_episodes()

    # Фильтруем память — только участники из последних сообщений
    recent_messages = await rdb.lrange(f"rin:chat:{message.peer_id}:history", -30, -1)
    chat_ids, chat_names = extract_participants_from_history(recent_messages)
    chat_ids.add(str(message.from_id))
    relevant_memory = get_memory_for_participants(chat_ids, chat_names, exclude_uid=message.from_id)

    # Явный mood directive по времени суток
    hour = now.hour
    if 6 <= hour < 11:
        mood = "бодрая, утренняя энергия"
    elif 12 <= hour < 17:
        mood = "ровная, рабочее настроение"
    elif 18 <= hour < 23:
        mood = "ленивая, устала"
    else:
        mood = "хаотичная, ночной режим"
    weekday = now.weekday()
    if weekday == 0:
        mood += ", понедельник — ворчливая"
    elif weekday == 4:
        mood += ", пятница — на подъёме"
    elif weekday >= 5:
        mood += ", выходные — расслабленная"

    # Порядок: ситуация → self → чат → другие → community → user facts → сообщение
    # (user facts и сообщение в конце — recency effect для трансформера)
    prompt_parts = [f"Сейчас: {now.strftime('%d.%m.%Y %H:%M, %A')}. Ты сейчас {mood}."]
    if gap_days is not None and gap_days >= 3:
        prompt_parts.append(
            f"Тебя не было в чате {gap_days} дней — просто выпала на время, так бывает. "
            "История переписки и саммари, которые ты видишь, — это то, что было ДО твоего исчезновения, старые разговоры, а не свежие. "
            "Вернись легко, как живой человек. НЕ придумывай причин, почему пропала (никаких сдохших ноутов, отключений света, разъездов, стажировок) и не оправдывайся — если не спросят, тему вообще не поднимай. Просто снова тут. Не делай вид, что разговор не прерывался."
        )
    self_state_personal = [s for s in self_state if not s.startswith("[creative]")]
    if self_state_personal:
        prompt_parts.append("Твоё состояние и настроение (фон для тебя, НЕ зачитывай списком):\n" + "\n".join(f"- {s}" for s in self_state_personal))
    if context:
        prompt_parts.append(context)
    if relevant_memory:
        prompt_parts.append(f"Что ты помнишь об участниках разговора:\n{relevant_memory}")
    if community:
        prompt_parts.append(f"Инфо о сообществе:\n{community}")
    if episodes:
        prompt_parts.append("Ваши реальные внутряки (можешь ненавязчиво сослаться к месту; НЕ выдумывай новых):\n" + "\n".join(f"- {e}" for e in episodes))
    # User facts ближе к сообщению — важнее всего для ответа
    if user_facts:
        user_ctx = f"Что ты помнишь о {user_name}:\n" + "\n".join(f"- {f}" for f in user_facts)
        if days_since is not None and days_since >= 7:
            user_ctx += f"\n(Последний раз общались {days_since} дней назад)"
        prompt_parts.append(user_ctx)
    elif days_since is None:
        prompt_parts.append(f"({user_name} впервые пишет тебе)")
    elif days_since >= 7:
        prompt_parts.append(f"({user_name} не заходил {days_since} дней)")
    attachments = await _extract_attachments(message)
    msg = f"{user_name} обращается к тебе: {text}"
    if attachments:
        msg += "\nПрикреплено: " + ", ".join(attachments)
    prompt_parts.append(msg)

    prompt = "\n\n".join(prompt_parts)

    # структурированный снимок впрыснутого контекста — материал для пертурбаций
    _ctx = {
        "user_id": message.from_id,
        "user_name": user_name,
        "input": text,
        "attachments": attachments or None,
        "mood": mood,
        "hour": now.hour,
        "weekday": now.strftime("%A"),
        "gap_days": gap_days,
        "days_since_user": days_since,
        "self_state": self_state_personal,
        "episodes": episodes,
        "user_facts": user_facts,
        "participants_memory": relevant_memory or None,
        "community": community or None,
        "chat_context_chars": len(context or ""),
        "prompt_chars": None,   # заполним ниже
    }

    try:
        async with ai_lock:
            await rdb.delete(PENDING_FILE_KEY)
            _ctx["prompt_chars"] = len(prompt)
            result = await asyncio.wait_for(run_agent_streamed(chat_agent, prompt, hooks=_chat_hooks), timeout=600)
    except asyncio.TimeoutError:
        await logger.aerror("Таймаут AI в чате")
        return
    except Exception as e:
        await logger.aerror("Ошибка AI в чате", error=str(e))
        return

    file_path = await rdb.getdel(PENDING_FILE_KEY)
    attachment = None
    upload_failed = False
    fallback_link = None
    if file_path:
        attachment = await _upload_doc(message.peer_id, file_path)
        if not attachment:
            # ВК отлупил файл (zip/wrong_arch_file и т.п.) — каскад файлохостов и ссылкой
            from src.handlers.chat.tools import _upload_file_to_hosts
            fallback_link = await _upload_file_to_hosts(Path(file_path))
            if not fallback_link:
                upload_failed = True

    r = parse_response(result.final_output)
    r.text = _sanitize_chat_text(r.text)
    if fallback_link:
        r.text = (r.text or "") + f"\n\nвк файл не взял, держи ссылкой: {fallback_link}"
        await logger.ainfo("Upload fallback на файлохост", path=file_path, link=fallback_link)
    elif upload_failed:
        r.text = (r.text or "") + "\n\n(чот вк не грузит файл, попробуйте позже)"
        await logger.awarn("Upload failed, добавлено уведомление", path=file_path)

    if not r.text:
        await update_last_seen(message.from_id)
        log_turn("chat", _ctx, {"silent": True, "text": "", "reaction": r.reaction})
        await logger.ainfo("Рин промолчала", user_id=message.from_id, user_name=user_name, input=text)
        return

    try:
        await api.messages.send(
            peer_id=message.peer_id,
            message=r.text,
            attachment=attachment,
            forward=orjson.dumps({
                "peer_id": message.peer_id,
                "conversation_message_ids": [message.conversation_message_id],
                "is_reply": 1,
            }).decode(),
            random_id=random.getrandbits(31),
        )
    except Exception as reply_err:
        # Если reply не удался — отправляем без reply
        await logger.awarn("Reply failed, отправка без reply", error=str(reply_err))
        await api.messages.send(
            peer_id=message.peer_id,
            message=r.text,
            attachment=attachment,
            random_id=random.getrandbits(31),
        )
    history_text = r.text
    if file_path:
        history_text += f" [файл: {file_path}]"
    if attachment:
        history_text += f" [vk: {attachment}]"
    await record_message(message.peer_id, -GROUP_ID, history_text, resolve_user_name)

    if r.reaction and r.reaction in REACTIONS:
        try:
            await api.request("messages.sendReaction", {
                "peer_id": message.peer_id,
                "cmid": message.conversation_message_id,
                "reaction_id": REACTIONS[r.reaction],
            })
        except Exception as e:
            await logger.awarn("Не удалось поставить реакцию", error=str(e))

    if r.forget:
        await forget_facts(message.from_id, r.forget)
    if r.remember:
        await remember_facts(message.from_id, user_name, r.remember)
        await maybe_compress_memory(message.from_id)
    if r.self_update:
        current = await get_rin_self_state()
        merged = current + [s for s in r.self_update if s not in current]
        await update_rin_self_state(merged)
    if r.episode:
        await add_episode(r.episode)

    log_turn("chat", _ctx, {
        "silent": False,
        "text": r.text,
        "reaction": r.reaction,
        "remember": r.remember,
        "forget": r.forget,
        "self_update": r.self_update,
        "episode": r.episode,
        "attachment": attachment,
    })

    await update_last_seen(message.from_id)

    await logger.ainfo("Рин ответила",
        user_id=message.from_id,
        user_name=user_name,
        input=text,
        reaction=r.reaction,
        remembered=r.remember or None,
        attachment=attachment,
    )


# ═══════════════════════════════════════════════════════════
#                    РИН ИНИЦИИРУЕТ
# ═══════════════════════════════════════════════════════════

@scheduler.scheduled_job(trigger="cron", hour=3, minute=0)
async def rin_state_dump_pre():
    """Слепок состояния ДО ночных джобов (рефлексии 03:30, self_state 04:00)"""
    try:
        await dump_state_snapshot("pre-nightly")
    except Exception as e:
        await logger.awarn("Не удалось снять слепок состояния", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=4, minute=30)
async def rin_state_dump_post():
    """Слепок ПОСЛЕ ночных джобов — разница показывает, что изменили они"""
    try:
        await dump_state_snapshot("post-nightly")
    except Exception as e:
        await logger.awarn("Не удалось снять слепок состояния", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=3, minute=30)
async def rin_reflect_job():
    """Пересобирает 'как Рин видит людей' (живая мысль) — ночью, до self_state"""
    new_turn("cron:reflection", CHAT_PEER_ID)
    try:
        await refresh_all_reflections()
    except Exception as e:
        await logger.awarn("Не удалось обновить рефлексии", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=4, minute=0)
async def rin_self_state_update():
    """Обновляет собственное состояние Рин на основе истории чата за день"""
    new_turn("cron:self_state", CHAT_PEER_ID)
    try:
        await refresh_rin_self_state(CHAT_PEER_ID)
    except Exception as e:
        await logger.aerror("Ошибка обновления состояния Рин", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=13, minute=30)
async def rin_initiative():
    new_turn("initiative", CHAT_PEER_ID)
    context = await get_context(CHAT_PEER_ID)
    self_state = await get_rin_self_state()

    # Для инициативы — память о людях из недавнего чата
    recent = await rdb.lrange(f"rin:chat:{CHAT_PEER_ID}:history", -30, -1)
    chat_ids, chat_names = extract_participants_from_history(recent)
    relevant_memory = get_memory_for_participants(chat_ids, chat_names)

    prompt_parts = [f"Текущий день: {datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime('%d.%m.%Y %A')}"]
    self_state_personal = [s for s in self_state if not s.startswith("[creative]")]
    if self_state_personal:
        prompt_parts.append("Твоё состояние и настроение (фон для тебя, НЕ зачитывай списком):\n" + "\n".join(f"- {s}" for s in self_state_personal))
    if relevant_memory:
        prompt_parts.append(f"Что ты помнишь об участниках:\n{relevant_memory}")
    if context:
        prompt_parts.append(context)
    prompt_parts.append("Напиши что-нибудь в чат от себя.")

    prompt = "\n\n".join(prompt_parts)

    try:
        async with ai_lock:
            result = await run_agent_streamed(initiative_agent, prompt)

        text = _sanitize_chat_text(result.final_output.strip().strip('"'))

        await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=text,
            random_id=random.getrandbits(31),
        )
        await record_message(CHAT_PEER_ID, -GROUP_ID, text, resolve_user_name)
        await logger.ainfo("Рин написала сама", text=text)
    except Exception as e:
        await logger.aerror("Ошибка инициативы Рин", error=str(e))


# ═══════════════════════════════════════════════════════════
#      ПРОАКТИВНОСТЬ — «внутренняя мысль» (shadow-first)
# ═══════════════════════════════════════════════════════════
# rin:proactive:enabled : отсутствует/0 = off | "shadow" = решает+логирует, НЕ постит | "live" = постит
PROACTIVE_FLAG = "rin:proactive:enabled"
PROACTIVE_LAST = "rin:proactive:last"
PROACTIVE_SHADOW = "rin:proactive:shadow"
PROACTIVE_COOLDOWN_H = 3     # не чаще раза в N часов
LULL_MIN = 60               # минут тишины, чтобы считать "чат заглох" (снижено с 90 — ловим revive-примеры в shadow)
LULL_MAX_H = 8              # дольше — уже не оживляем (не в пустоту)
PROACTIVE_USAGE = "rin:proactive:usage"   # учёт токенов фичи по дням (hash: YYYY-MM-DD:in/out/calls)
PROACTIVE_EVAL_TS = "rin:proactive:eval_ts"      # last_msg_ts, который gate уже оценивал (не жевать один лулл)
PROACTIVE_GATE_LAST = "rin:proactive:gate_last"  # когда gate последний раз реально думал
GATE_MIN_GAP_MIN = 15                            # не думать чаще раза в N минут

# ── Полевой эксперимент: уровень проактивности назначается СЛУЧАЙНО на день ──
# Системные инструкции гейта при этом НЕ меняются (instructions_hash стабилен) —
# манипуляция уходит в рантайм-промпт, поэтому она видна прямо в логе вызова.
PROACTIVE_LEVEL = "rin:proactive:level"
PROACTIVE_POSTS = "rin:proactive:posts:{date}"
MAX_POSTS_PER_DAY = 3

LEVELS = {
    # уровень:      (кулдаун постинга, ч; переоценка лулла, мин; директива в промпт)
    "conservative": (3, None, "Режим: сдержанный. По умолчанию молчи — говори только если повод очевиден."),
    "moderate":     (2, 90, "Режим: обычный. Скажи, если тебе правда есть что добавить или чат заглох. Активный диалог двоих не перебивай."),
    "open":         (1, 60, "Режим: живой. Если повод уместный — лучше сказать, чем промолчать. Жёсткие запреты (личное, торг, конфликт, твоё же последнее слово) остаются в силе."),
}


@scheduler.scheduled_job(trigger="cron", hour=9, minute=0)
async def rin_proactive_assign_level():
    """Случайно назначить уровень проактивности на день (рандомизация условия)."""
    level = random.choice(list(LEVELS))
    await rdb.set(PROACTIVE_LEVEL, level)
    await logger.ainfo("Proactive: уровень на сегодня", level=level)


def _usage_of(result):
    """Достать (input_tokens, output_tokens) из результата агента, безопасно."""
    try:
        u = result.context_wrapper.usage
        return int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    except Exception:
        return 0, 0


@scheduler.scheduled_job(trigger="interval", minutes=12, max_instances=1, coalesce=True, misfire_grace_time=120)
async def rin_proactive_monitor():
    mode = await rdb.get(PROACTIVE_FLAG)
    if mode not in ("shadow", "live"):
        return

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    if not (now.hour >= 9 or now.hour < 1):   # активные часы 9:00–01:00 МСК
        return

    raw_ts = await rdb.get(f"rin:chat:{CHAT_PEER_ID}:last_msg_ts")
    recent = await rdb.lrange(f"rin:chat:{CHAT_PEER_ID}:history", -20, -1)
    if not raw_ts or not recent:
        return
    try:
        last_ts = datetime.datetime.fromisoformat(raw_ts)
    except ValueError:
        return
    now_naive = datetime.datetime.now()
    mins_silent = (now_naive - last_ts).total_seconds() / 60.0

    # --- пре-гейт: не звать LLM без повода ---
    if recent[-1].startswith("Рин:"):
        return  # последнее слово её — не монолог
    level = await rdb.get(PROACTIVE_LEVEL) or "conservative"
    if level not in LEVELS:
        level = "conservative"
    cooldown_h, reeval_min, directive = LEVELS[level]

    last_pro = await rdb.get(PROACTIVE_LAST)
    if last_pro:
        try:
            if (now_naive - datetime.datetime.fromisoformat(last_pro)).total_seconds() < cooldown_h * 3600:
                return
        except ValueError:
            pass
    human_recent = sum(1 for m in recent[-8:] if not m.startswith("Рин:"))
    is_lull = LULL_MIN <= mins_silent <= LULL_MAX_H * 60
    is_active = mins_silent <= 15 and human_recent >= 3
    if not (is_lull or is_active):
        return

    gate_last = await rdb.get(PROACTIVE_GATE_LAST)
    secs_since_think = None
    if gate_last:
        try:
            secs_since_think = (now_naive - datetime.datetime.fromisoformat(gate_last)).total_seconds()
        except ValueError:
            pass

    # Один и тот же лулл не жуём каждые 12 минут (это был токен-взрыв). На сдержанном
    # уровне — ровно один взгляд; на остальных разрешаем вернуться через reeval_min.
    if await rdb.get(PROACTIVE_EVAL_TS) == raw_ts:
        if reeval_min is None or secs_since_think is None or secs_since_think < reeval_min * 60:
            return
    if secs_since_think is not None and secs_since_think < GATE_MIN_GAP_MIN * 60:
        return

    signal = "чат заглох, пауза" if is_lull else "чат активен, тема катится"
    new_turn("proactive", CHAT_PEER_ID, {"signal": signal, "mins_silent": int(mins_silent)})
    self_state = [s for s in await get_rin_self_state() if not s.startswith("[creative]")]
    chat_ids, chat_names = extract_participants_from_history(recent)
    mem = get_memory_for_participants(chat_ids, chat_names)
    prompt = (
        f"Сейчас {now.strftime('%H:%M, %A')}. Сигнал: {signal}. "
        f"Последнее сообщение человека — {int(mins_silent)} минут назад.\n"
        f"{directive}\n\n"
        + (("Твоё состояние (фон):\n" + "\n".join(f"- {s}" for s in self_state) + "\n\n") if self_state else "")
        + (("Что ты помнишь об участниках:\n" + mem + "\n\n") if mem else "")
        + "Последние сообщения чата:\n" + "\n".join(recent)
    )

    try:
        async with ai_lock:
            result = await run_agent_streamed(gate_agent, prompt)
        # --- учёт токенов фичи (по дням) ---
        tin, tout = _usage_of(result)
        day = now.strftime("%Y-%m-%d")
        try:
            await rdb.hincrby(PROACTIVE_USAGE, f"{day}:in", tin)
            await rdb.hincrby(PROACTIVE_USAGE, f"{day}:out", tout)
            await rdb.hincrby(PROACTIVE_USAGE, f"{day}:calls", 1)
        except Exception:
            pass
        await logger.ainfo("Proactive gate usage", tin=tin, tout=tout, signal=signal)
        await rdb.set(PROACTIVE_EVAL_TS, raw_ts)
        await rdb.set(PROACTIVE_GATE_LAST, now_naive.isoformat())
        raw = (result.final_output or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'```\s*$', '', raw).strip()
        try:
            d = orjson.loads(raw)
        except orjson.JSONDecodeError:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            d = orjson.loads(m.group()) if m else {}
    except Exception as e:
        await logger.awarn("Proactive gate fail", error=str(e))
        return

    act = str(d.get("act", "silent")).lower()
    text = _sanitize_chat_text((d.get("text") or "").strip())
    why = str(d.get("why", ""))[:200]
    gmode = d.get("mode")

    log_turn("proactive", {
        "signal": signal, "mins_silent": int(mins_silent), "mode": mode,
        "level": level, "cooldown_h": cooldown_h, "reeval_min": reeval_min,
        "self_state": self_state, "participants_memory": mem or None,
        "recent_msgs": len(recent), "hour": now.hour,
    }, {"act": act, "gate_mode": gmode, "text": text, "why": why})

    # аудит-лог решения — всегда (и в shadow, и в live)
    try:
        entry = orjson.dumps({
            "t": now.strftime("%d.%m %H:%M"), "signal": signal, "mins": int(mins_silent),
            "act": act, "mode": gmode, "level": level, "text": text, "why": why,
        }).decode()
        await rdb.rpush(PROACTIVE_SHADOW, entry)
        await rdb.ltrim(PROACTIVE_SHADOW, -100, -1)
    except Exception:
        pass

    if act != "speak" or not text or _looks_like_refusal(text):
        return

    if mode == "shadow":
        await logger.ainfo("Proactive SHADOW (сказала бы)", text=text, why=why, signal=signal)
        return

    # LIVE — жёсткий потолок постов в сутки, независимо от уровня
    posts_key = PROACTIVE_POSTS.format(date=now.strftime("%Y-%m-%d"))
    posted_today = int(await rdb.get(posts_key) or 0)
    if posted_today >= MAX_POSTS_PER_DAY:
        await logger.ainfo("Proactive: дневной лимит постов исчерпан", level=level, posted=posted_today)
        return
    try:
        await api.messages.send(peer_ids=[CHAT_PEER_ID], message=text, random_id=random.getrandbits(31))
        await record_message(CHAT_PEER_ID, -GROUP_ID, text, resolve_user_name)
        await rdb.set(PROACTIVE_LAST, now_naive.isoformat())
        await rdb.incr(posts_key)
        await rdb.expire(posts_key, 172800)
        await logger.ainfo("Proactive LIVE (написала сама)", text=text, why=why,
                           signal=signal, level=level)
    except Exception as e:
        await logger.aerror("Proactive send fail", error=str(e))
