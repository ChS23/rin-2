from vkbottle.bot import BotLabeler, Message
from vkbottle.tools import Keyboard, Text, OpenLink
from vkbottle.dispatch.rules.base import ChatActionRule

from src.bot import api

labeler = BotLabeler()


# ═══════════════════════════════════════════════════════════
#                         СПРАВКА
# ═══════════════════════════════════════════════════════════

HELP_MAIN = """
🤖 Рин — бот сообщества визуальных новелл

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋 /помощь — эта справка
📋 /помощь роли — система ролей
📋 /помощь проекты — доска проектов
📋 /помощь доска — доска запросов

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔥 Быстрые команды:
• /роль <название> — добавить роль
• /тег <роль> — позвать людей с ролью
• /проекты — посмотреть проекты
• /доска — доска поиска людей
""".strip()

HELP_ROLES = """
👥 СИСТЕМА РОЛЕЙ

Роли — способ найти людей по навыкам.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝 Управление:
• /роль <название> — добавить себе роль
• /роль -<название> — убрать роль
• /роли — посмотреть свои роли

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔍 Поиск:
• /тег <роль> — позвать всех с ролью
• /все_роли — список всех ролей

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 Примеры:
/роль художник
/роль сценарист
/роль программист
/тег художники
""".strip()

HELP_PROJECTS = """
📁 ДОСКА ПРОЕКТОВ

Покажи свой проект сообществу!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝 Создание (в ЛС с ботом):
• /проект создать — создать проект

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔍 Просмотр:
• /проекты — все проекты
• /проект <номер> — подробнее
• /мои_проекты — твои проекты
• /ищу_помощь — кто ищет помощь

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🗑 Управление (в ЛС):
• /проект удалить <номер>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 Статусы:
🔨 в работе | 🆘 ищу помощь | ✅ завершён
""".strip()

HELP_BOARD = """
📋 ДОСКА ЗАПРОСОВ

Найди людей для своего проекта!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📝 Создание (в ЛС с ботом):
• /запрос — создать запрос

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔍 Просмотр:
• /доска — все запросы
• /доска <тип> — фильтр по типу
• /запрос <номер> — подробнее
• /мои_запросы — твои запросы

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🗑 Управление (в ЛС):
• /запрос закрыть <номер>
• /запрос удалить <номер>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 Типы запросов:
🎨 художник | ✍️ сценарист | 🎤 озвучка
💻 программист | 🌐 переводчик | 🎵 композитор
💡 помощь (предлагаю свои услуги)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💰 Оплата: бесплатно / договорная / оплачиваемо
""".strip()


@labeler.message(text=["/помощь", "/help", "/команды", "/start"])
async def help_main(message: Message):
    """Главная справка"""
    keyboard = (
        Keyboard(inline=True)
        .add(Text("👥 Роли", payload={"help": "roles"}))
        .add(Text("📁 Проекты", payload={"help": "projects"}))
        .row()
        .add(Text("📋 Доска", payload={"help": "board"}))
    )
    await message.answer(HELP_MAIN, keyboard=keyboard)


@labeler.message(text=["/помощь роли", "/помощь роль", "/help roles"])
async def help_roles(message: Message):
    """Справка по ролям"""
    await message.answer(HELP_ROLES)


@labeler.message(text=["/помощь проекты", "/помощь проект", "/help projects"])
async def help_projects(message: Message):
    """Справка по проектам"""
    await message.answer(HELP_PROJECTS)


@labeler.message(text=["/помощь доска", "/помощь запрос", "/help board"])
async def help_board(message: Message):
    """Справка по доске запросов"""
    await message.answer(HELP_BOARD)


@labeler.message(payload={"help": "roles"})
async def help_roles_button(message: Message):
    await message.answer(HELP_ROLES)


@labeler.message(payload={"help": "projects"})
async def help_projects_button(message: Message):
    await message.answer(HELP_PROJECTS)


@labeler.message(payload={"help": "board"})
async def help_board_button(message: Message):
    await message.answer(HELP_BOARD)


@labeler.chat_message(ChatActionRule(chat_action_types=["chat_invite_user", "chat_invite_user_by_link"]))
async def invite_event_handler(message: Message):
    users = await api.users.get(user_ids=message.from_id)
    # Добро пожаловать в беседу, @id{} ({}) . \n Посмотри закреп и чувствуй себя как дома^^
    welcome_message = ""
    if message.from_id:
        welcome_message = (
            "Добро пожаловать в беседу "
            f"@id{message.from_id} ({users[0].first_name} {users[0].last_name})"
            "\n Посмотри закреп и чувствуй себя как дома^^"
        )
    else:
        welcome_message = "Добро пожаловать в беседу\nПосмотри закреп и чувствуй себя как дома^^"
        
    await message.answer(
        message=welcome_message,
        keyboard=(
            Keyboard(
                one_time=False,
                inline=True
            ).add(OpenLink("https://vk.com/da_helper", "Наш паблик"))
        ),
        attachment="photo-195811361_457239398",
    )
