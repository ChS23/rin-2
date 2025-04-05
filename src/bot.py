from vkbottle import Bot, API, LoopWrapper

from src.config.settings import get_settings

settings = get_settings()
api = API(token=settings.vk.token)

def create_bot():
    from src.handlers.checkin import start_scheduler
    
    loop_wrapper = LoopWrapper(
        on_startup=[start_scheduler()]
    )

    bot = Bot(
        api=api,
        loop_wrapper=loop_wrapper
    )

    from src.handlers import labelers
    for labeler in labelers:
        bot.labeler.load(labeler)
    
    return bot
