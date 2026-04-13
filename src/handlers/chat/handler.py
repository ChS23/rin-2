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

from agents import Runner, RunHooks

from src.bot import api, rdb
from src.handlers.checkin import ai_lock, REACTIONS, scheduler, CHAT_PEER_ID
from src.handlers.chat.agents import chat_agent, initiative_agent
from src.handlers.chat.tools import PENDING_FILE_KEY
from src.handlers.chat.memory import (
    record_message, get_context,
    remember_facts, forget_facts, maybe_compress_memory,
    get_user_memory, get_memory_for_ids, extract_user_ids_from_history,
    get_rin_self_state, refresh_rin_self_state, update_rin_self_state,
    update_last_seen, get_days_since,
)
from src.handlers.chat.utils import (
    resolve_user_name, parse_response, get_community_context,
)

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
                await record_message(msg.peer_id, msg.from_id, text, resolve_user_name)
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


class MentionsBot(ABCRule[Message]):
    async def check(self, event: Message) -> bool:
        if not event.peer_id > 2000000000:
            return False
        if event.reply_message and event.reply_message.from_id == -GROUP_ID:
            return True
        if event.fwd_messages:
            for fwd in event.fwd_messages:
                if fwd.from_id == -GROUP_ID:
                    return True
        mention_patterns = [
            f"[club{GROUP_ID}|",
            "@rinchan_bot",
            f"@club{GROUP_ID}",
        ]
        text_lower = (event.text or "").lower()
        return any(p.lower() in text_lower for p in mention_patterns)


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
async def chat_with_rin(message: Message):
    text = message.text or ""
    text = re.sub(r'\[club\d+\|[^\]]*\]', '', text).strip()
    text = re.sub(r'@rinchan_bot', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'@club\d+', '', text, flags=re.IGNORECASE).strip()

    if not text:
        text = "привет"

    user_name = await resolve_user_name(message.from_id)

    now = datetime.datetime.now()
    context = await get_context(message.peer_id)
    user_facts = get_user_memory(message.from_id)
    community = get_community_context()
    self_state = await get_rin_self_state()
    days_since = await get_days_since(message.from_id)

    # Фильтруем память — только участники из последних сообщений
    recent_messages = await rdb.lrange(f"rin:chat:{message.peer_id}:history", -30, -1)
    chat_user_ids = extract_user_ids_from_history(recent_messages)
    chat_user_ids.add(str(message.from_id))
    relevant_memory = get_memory_for_ids(chat_user_ids, exclude_uid=message.from_id)

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
    if self_state:
        prompt_parts.append("Твой текущий прогресс и состояние:\n" + "\n".join(f"- {s}" for s in self_state))
    if context:
        prompt_parts.append(context)
    if relevant_memory:
        prompt_parts.append(f"Что ты помнишь об участниках разговора:\n{relevant_memory}")
    if community:
        prompt_parts.append(f"Инфо о сообществе:\n{community}")
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

    try:
        async with ai_lock:
            await rdb.delete(PENDING_FILE_KEY)
            result = await asyncio.wait_for(Runner.run(chat_agent, prompt, hooks=_chat_hooks), timeout=600)
    except asyncio.TimeoutError:
        await logger.aerror("Таймаут AI в чате")
        return
    except Exception as e:
        await logger.aerror("Ошибка AI в чате", error=str(e))
        return

    file_path = await rdb.getdel(PENDING_FILE_KEY)
    attachment = None
    upload_failed = False
    if file_path:
        attachment = await _upload_doc(message.peer_id, file_path)
        if not attachment:
            upload_failed = True

    r = parse_response(result.final_output)
    if upload_failed:
        r.text = (r.text or "") + "\n\n(чот вк не грузит файл, попробуйте позже)"
        await logger.awarn("Upload failed, добавлено уведомление", path=file_path)

    if not r.text:
        await update_last_seen(message.from_id)
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

@scheduler.scheduled_job(trigger="cron", hour=4, minute=0)
async def rin_self_state_update():
    """Обновляет собственное состояние Рин на основе истории чата за день"""
    try:
        await refresh_rin_self_state(CHAT_PEER_ID)
    except Exception as e:
        await logger.aerror("Ошибка обновления состояния Рин", error=str(e))


@scheduler.scheduled_job(trigger="cron", hour=13, minute=30)
async def rin_initiative():
    context = await get_context(CHAT_PEER_ID)
    self_state = await get_rin_self_state()

    # Для инициативы — память о людях из недавнего чата
    recent = await rdb.lrange(f"rin:chat:{CHAT_PEER_ID}:history", -30, -1)
    chat_user_ids = extract_user_ids_from_history(recent)
    relevant_memory = get_memory_for_ids(chat_user_ids)

    prompt_parts = [f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A')}"]
    if self_state:
        prompt_parts.append("Твой текущий прогресс и состояние:\n" + "\n".join(f"- {s}" for s in self_state))
    if relevant_memory:
        prompt_parts.append(f"Что ты помнишь об участниках:\n{relevant_memory}")
    if context:
        prompt_parts.append(context)
    prompt_parts.append("Напиши что-нибудь в чат от себя.")

    prompt = "\n\n".join(prompt_parts)

    try:
        async with ai_lock:
            result = await Runner.run(initiative_agent, prompt)

        text = result.final_output.strip().strip('"')

        await api.messages.send(
            peer_ids=[CHAT_PEER_ID],
            message=text,
            random_id=random.getrandbits(31),
        )
        await record_message(CHAT_PEER_ID, -GROUP_ID, text, resolve_user_name)
        await logger.ainfo("Рин написала сама", text=text)
    except Exception as e:
        await logger.aerror("Ошибка инициативы Рин", error=str(e))
