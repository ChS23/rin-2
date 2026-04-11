import os

import redis.asyncio as aioredis
from vkbottle import Bot, API, LoopWrapper

from src.config.settings import get_settings

settings = get_settings()
api = API(token=settings.vk.token)
rdb = aioredis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


def create_bot():
    from src.handlers.checkin import start_scheduler
    from src.handlers.chat.memory import init_rin_self_state

    loop_wrapper = LoopWrapper(
        on_startup=[start_scheduler(), init_rin_self_state()]
    )

    bot = Bot(
        api=api,
        loop_wrapper=loop_wrapper
    )

    from src.handlers import labelers
    for labeler in labelers:
        bot.labeler.load(labeler)

    return bot
