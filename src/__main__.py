import structlog

from src.bot import create_bot

logger = structlog.get_logger("main")


if __name__ == "__main__":
    logger.info("Запуск бота")
    bot = create_bot()
    bot.run_forever()
