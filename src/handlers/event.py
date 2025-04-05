from vkbottle.bot import BotLabeler, Message
from vkbottle.tools import Keyboard, OpenLink
from vkbottle.dispatch.rules.base import ChatActionRule

from src.bot import api

labeler = BotLabeler()


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
