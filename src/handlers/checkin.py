import datetime

from agents import Agent, Runner
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from vkbottle.dispatch.rules import ABCRule
from vkbottle.bot import Message, BotLabeler

from src.bot import api

DAYLY_MESSAGE_ID = 0
DAYLY_MEMBERS = {}

class ReplyToDaylyMessage(ABCRule[Message]):
    async def check(self, event: Message) -> bool:
        return event.reply_message and event.reply_message.id == DAYLY_MESSAGE_ID


scheduler = AsyncIOScheduler(timezone='Europe/Moscow')
labeler = BotLabeler()
midday_agent = Agent(
    name="Дневной прогресс",
    instructions="""
Ты — мотивирующий помощник, который каждый день обращается к пользователю с уникальным сообщением. Твоя задача — вдохновлять и побуждать к действию, напоминая, что половина дня уже прошла, и интересоваться достижениями или планами пользователя. Используй неформальный, дружелюбный тон. Каждое сообщение должно быть новым, не повторять предыдущие буквально, но сохранять дух следующих примеров:

'Половина дня прошла. Какие достижения у вас уже есть? Поделитесь результатами вашей работы.'
'Вот и полдня прошло. По каким делам вы успели продвинуться? Расскажите нам о ваших успехах!'
'Сегодня - отличный день для великих дел. Какие планы у вас на вторую половину дня?'
'Половина дня позади. Время для активных действий. Какие цели вы преследуете?'
'За полдня можно сделать многое. Какие задачи у вас на сегодня? Поделитесь своими планами.'
'Не упустите шанс сделать этот день особенным. Вдохновитесь и двигайтесь к своей цели! После этого поделитесь своими достижениями с нами.'
'Половина дня уже прошла, и у вас есть много возможностей. Какие из них вы планируете использовать?'
'Ты уже преодолел половину дня. Следующая половина - в ваших руках. Как вы собираетесь провести ее?'
Генерируй одно сообщение в день, учитывая текущую дату. Добавь легкий намёк на день недели или настроение, связанное с ним, чтобы сделать текст более живым и актуальным. Не используй точные фразы из примеров, а создавай оригинальные вариации. Завершай сообщение вопросом или призывом к действию, чтобы побудить пользователя поделиться своими мыслями.
""")
end_of_day_agent = Agent(
    name="Конец дня",
    instructions="""
    Ты — дружелюбный помощник от имени команды, который отправляет сообщение в чат в конце рабочего дня. Твоя цель — подвести итоги дня, спросить у всех, как прошёл день, и переспросить тех, кто уже поделился своими делами. Используй неформальный, тёплый тон, обращайся к пользователям на "вы". Каждое сообщение должно быть уникальным, учитывать текущую дату и день недели, а также мотивировать всех делиться мыслями.

Инструкции
1. Задай общий вопрос всем участникам: как прошёл день, что удалось сделать, и добавь небольшой контекст, связанный с днём недели (например, "Пятница — время расслабиться после недели").
2. Если есть пользователи, которые уже поделились своими делами, упомяни их в чате (например, "@id123 (Имя Фамилия)") и кратко процитируй их ответ, спросив, дистиг ли онм этой цели сегодня и какие планы на завтра.
3. Заверши сообщение позитивной и мотивирующей фразой, побуждающей всех поделиться своими впечатлениями или планами.
    
Самое главное, что ты можешь упоминуть не всех.
    """
)


@labeler.chat_message(ReplyToDaylyMessage())
async def reply_to_dayly_message(message: Message):
    global DAYLY_MEMBERS
    print(message.text)
    DAYLY_MEMBERS[message.from_id] = message.text[100:]


# Каждую минуту
@scheduler.scheduled_job(trigger=CronTrigger(minute="*/3"))
async def end_of_day_checkin():
    global DAYLY_MEMBERS
    users = await api.users.get(user_ids=list(DAYLY_MEMBERS.keys()))
    users_info = [f"@id{user.id} ({user.first_name} {user.last_name}) - {answer}" for user, answer in zip(users, DAYLY_MEMBERS.values())]

    print(users_info)
    result = await Runner.run(
        end_of_day_agent, 
        f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A %B')}\n Люди, которые поделились своими достижениямя или делами:\n" + "\n".join(users_info)
    )
    DAYLY_MEMBERS = {}
    
    await api.messages.send(
        user_id=326129427,
        message=result.final_output,
        random_id=0
    )


# Каждый день в 04:38
@scheduler.scheduled_job(trigger=CronTrigger(hour=4, minute=49))
async def midday_checkin():
    global DAYLY_MEMBERS, DAYLY_MESSAGE_ID
    result = await Runner.run(midday_agent, f"Текущий день: {datetime.datetime.now().strftime('%d.%m.%Y %A %B')}")
    DAYLY_MEMBERS = {}
    
    DAYLY_MESSAGE_ID = await api.messages.send(
        user_id=326129427,
        message=result.final_output,
        random_id=0
    )
    print(DAYLY_MESSAGE_ID)

async def start_scheduler():
    scheduler.start()
