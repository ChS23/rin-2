import json
from pathlib import Path
from datetime import datetime

import structlog
from vkbottle.bot import Message, BotLabeler
from vkbottle.dispatch.rules import ABCRule
from vkbottle.tools import Keyboard, Text, TemplateElement, template_gen

from src.bot import api

logger = structlog.get_logger("handlers.board")
labeler = BotLabeler()

DATA_DIR = Path("/app/data")
BOARD_FILE = DATA_DIR / "board.json"

# Типы запросов
REQUEST_TYPES = {
    "1": ("художник", "🎨"),
    "2": ("сценарист", "✍️"),
    "3": ("озвучка", "🎤"),
    "4": ("программист", "💻"),
    "5": ("переводчик", "🌐"),
    "6": ("композитор", "🎵"),
    "7": ("помощь", "💡"),  # Предлагаю помощь
}

# Типы оплаты
PAYMENT_TYPES = {
    "1": "бесплатно",
    "2": "договорная",
    "3": "оплачиваемо",
}


def load_board() -> dict[str, dict]:
    """Загрузить доску из файла"""
    if not BOARD_FILE.exists():
        return {}
    with open(BOARD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_board(board: dict[str, dict]) -> None:
    """Сохранить доску в файл"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(board, f, ensure_ascii=False, indent=2)


def get_next_id(board: dict[str, dict]) -> str:
    """Получить следующий ID"""
    if not board:
        return "1"
    return str(max(int(k) for k in board.keys()) + 1)


# Состояние для создания запроса
creation_state: dict[int, dict] = {}


class InBoardCreation(ABCRule[Message]):
    """Правило: пользователь в процессе создания запроса"""
    async def check(self, event: Message) -> bool:
        return event.from_id in creation_state


class BoardViewPayload(ABCRule[Message]):
    """Правило: нажата кнопка просмотра запроса"""
    async def check(self, event: Message) -> dict | bool:
        if event.payload:
            import json
            try:
                payload = json.loads(event.payload) if isinstance(event.payload, str) else event.payload
                if payload.get("cmd") == "board_view":
                    return {"request_id": payload.get("id")}
            except (json.JSONDecodeError, AttributeError):
                pass
        return False


def build_board_carousel(requests_dict: dict[str, dict], limit: int = 10) -> str | None:
    """Создать карусель запросов"""
    if not requests_dict:
        return None

    elements = []
    # Сортируем: сначала открытые, потом по дате
    sorted_requests = sorted(
        requests_dict.items(),
        key=lambda x: (x[1].get("closed", False), -int(x[0]))
    )[:limit]

    for rid, r in sorted_requests:
        type_name, emoji = REQUEST_TYPES.get(r["type"], ("другое", "📋"))
        closed = "✅ " if r.get("closed") else ""

        desc = r["description"][:70] + "..." if len(r["description"]) > 70 else r["description"]
        payment = r.get("payment", "не указано")

        elements.append(
            TemplateElement(
                title=f"{closed}{emoji} {r['title']}"[:80],
                description=f"{desc}\n\n💰 {payment}"[:160],
                buttons=Keyboard(inline=True)
                    .add(Text(f"👁 Подробнее", payload={"cmd": "board_view", "id": rid}))
                    .get_json()
            )
        )

    return template_gen(*elements) if elements else None


def build_board_text(requests_dict: dict[str, dict], limit: int = 10) -> str:
    """Текстовый список запросов (fallback)"""
    lines = []
    sorted_requests = sorted(
        requests_dict.items(),
        key=lambda x: (x[1].get("closed", False), -int(x[0]))
    )[:limit]

    for rid, r in sorted_requests:
        type_name, emoji = REQUEST_TYPES.get(r["type"], ("другое", "📋"))
        closed = "✅" if r.get("closed") else ""
        payment = r.get("payment", "")
        lines.append(f"{closed}{emoji} #{rid} {r['title'][:30]} — {payment}")

    return "\n".join(lines)


@labeler.private_message(text="/запрос")
async def start_create_request(message: Message):
    """Начать создание запроса (только в ЛС)"""
    user_id = message.from_id

    types_list = "\n".join([f"{k} — {emoji} {name}" for k, (name, emoji) in REQUEST_TYPES.items()])

    creation_state[user_id] = {"step": "type"}
    await message.answer(
        "Создание запроса на доску\n\n"
        f"Шаг 1/4: Выбери тип:\n{types_list}\n\n"
        "Введи номер:"
    )


@labeler.private_message(InBoardCreation())
async def handle_creation_steps(message: Message):
    """Обработка шагов создания запроса"""
    user_id = message.from_id
    state = creation_state[user_id]
    text = message.text.strip()

    if state["step"] == "type":
        if text not in REQUEST_TYPES:
            await message.answer(f"Введи число от 1 до {len(REQUEST_TYPES)}")
            return
        state["type"] = text
        state["step"] = "title"
        await message.answer("Шаг 2/4: Введи заголовок запроса:")

    elif state["step"] == "title":
        state["title"] = text[:80]
        state["step"] = "description"
        await message.answer(
            "Шаг 3/4: Опиши подробнее что нужно:\n"
            "(требования, сроки, контакты и т.д.)"
        )

    elif state["step"] == "description":
        state["description"] = text
        state["step"] = "payment"
        await message.answer(
            "Шаг 4/4: Условия оплаты:\n"
            "1 — бесплатно\n"
            "2 — договорная\n"
            "3 — оплачиваемо\n\n"
            "Введи номер:"
        )

    elif state["step"] == "payment":
        if text not in PAYMENT_TYPES:
            await message.answer("Введи 1, 2 или 3")
            return

        # Сохраняем запрос
        board = load_board()
        request_id = get_next_id(board)

        type_name, emoji = REQUEST_TYPES[state["type"]]

        board[request_id] = {
            "title": state["title"],
            "description": state["description"],
            "type": state["type"],
            "payment": PAYMENT_TYPES[text],
            "author_id": user_id,
            "created_at": datetime.now().strftime("%Y-%m-%d"),
            "closed": False
        }

        save_board(board)
        del creation_state[user_id]

        await logger.ainfo("Запрос создан", request_id=request_id, author_id=user_id, type=type_name)
        await message.answer(
            f"Запрос #{request_id} создан!\n\n"
            f"{emoji} {type_name}\n"
            f"📌 {state['title']}\n"
            f"💰 {PAYMENT_TYPES[text]}\n\n"
            f"Посмотреть: /запрос {request_id}"
        )


@labeler.private_message(InBoardCreation(), text="/отмена")
async def cancel_creation(message: Message):
    """Отменить создание"""
    user_id = message.from_id
    del creation_state[user_id]
    await message.answer("Создание отменено")


@labeler.message(BoardViewPayload())
async def handle_board_view_button(message: Message, request_id: str):
    """Обработка нажатия кнопки Подробнее"""
    board = load_board()

    if request_id not in board:
        await message.answer(f"Запрос #{request_id} не найден")
        return

    r = board[request_id]
    users = await api.users.get(user_ids=[r["author_id"]])
    author = users[0] if users else None
    author_name = f"{author.first_name} {author.last_name}" if author else "Неизвестно"

    type_name, emoji = REQUEST_TYPES.get(r["type"], ("другое", "📋"))
    closed = "✅ ЗАКРЫТ\n\n" if r.get("closed") else ""

    text = (
        f"{closed}Запрос #{request_id}\n\n"
        f"{emoji} {type_name}\n"
        f"📌 {r['title']}\n\n"
        f"{r['description']}\n\n"
        f"💰 Оплата: {r.get('payment', 'не указано')}\n"
        f"👤 Автор: @id{r['author_id']} ({author_name})\n"
        f"📅 Создан: {r['created_at']}"
    )

    await message.answer(text)


@labeler.message(text="/доска")
async def list_board(message: Message):
    """Показать всю доску (карусель)"""
    board = load_board()

    # Только открытые
    open_requests = {k: v for k, v in board.items() if not v.get("closed")}

    if not open_requests:
        await message.answer("Доска пуста. Создать запрос: /запрос (в ЛС с ботом)")
        return

    carousel = build_board_carousel(open_requests)
    if carousel:
        await message.answer("📋 Доска запросов:", template=carousel)
    else:
        # Fallback на текст
        text = build_board_text(open_requests)
        await message.answer(f"📋 Доска запросов:\n\n{text}\n\nПодробнее: /запрос <номер>")


@labeler.message(text="/доска <filter_type>")
async def list_board_filtered(message: Message, filter_type: str):
    """Доска с фильтром по типу"""
    board = load_board()

    # Ищем тип по названию
    type_id = None
    filter_lower = filter_type.lower()
    for tid, (name, _) in REQUEST_TYPES.items():
        if filter_lower in name.lower():
            type_id = tid
            break

    if not type_id:
        types_list = ", ".join([name for name, _ in REQUEST_TYPES.values()])
        await message.answer(f"Тип не найден. Доступные: {types_list}")
        return

    filtered = {k: v for k, v in board.items() if v["type"] == type_id and not v.get("closed")}

    if not filtered:
        type_name, emoji = REQUEST_TYPES[type_id]
        await message.answer(f"Нет открытых запросов типа {emoji} {type_name}")
        return

    carousel = build_board_carousel(filtered)
    type_name, emoji = REQUEST_TYPES[type_id]
    if carousel:
        await message.answer(f"{emoji} Запросы: {type_name}", template=carousel)
    else:
        lines = [f"#{rid} {r['title']}" for rid, r in filtered.items()]
        await message.answer(f"{emoji} {type_name}:\n\n" + "\n".join(lines))


@labeler.message(text="/запрос <request_id>")
async def show_request(message: Message, request_id: str):
    """Показать запрос"""
    board = load_board()

    if request_id not in board:
        await message.answer(f"Запрос #{request_id} не найден")
        return

    r = board[request_id]
    users = await api.users.get(user_ids=[r["author_id"]])
    author = users[0] if users else None
    author_name = f"{author.first_name} {author.last_name}" if author else "Неизвестно"

    type_name, emoji = REQUEST_TYPES.get(r["type"], ("другое", "📋"))
    closed = "✅ ЗАКРЫТ\n\n" if r.get("closed") else ""

    text = (
        f"{closed}Запрос #{request_id}\n\n"
        f"{emoji} {type_name}\n"
        f"📌 {r['title']}\n\n"
        f"{r['description']}\n\n"
        f"💰 Оплата: {r.get('payment', 'не указано')}\n"
        f"👤 Автор: @id{r['author_id']} ({author_name})\n"
        f"📅 Создан: {r['created_at']}"
    )

    await message.answer(text)


@labeler.message(text="/мои_запросы")
async def my_requests(message: Message):
    """Мои запросы"""
    user_id = message.from_id
    board = load_board()

    user_requests = {k: v for k, v in board.items() if v["author_id"] == user_id}

    if not user_requests:
        await message.answer("У тебя нет запросов.\nСоздать: /запрос (в ЛС с ботом)")
        return

    carousel = build_board_carousel(user_requests)
    if carousel:
        await message.answer("Твои запросы:", template=carousel)
    else:
        lines = []
        for rid, r in user_requests.items():
            _, emoji = REQUEST_TYPES.get(r["type"], ("", "📋"))
            closed = "✅" if r.get("closed") else ""
            lines.append(f"#{rid} {closed}{emoji} {r['title']}")
        await message.answer("Твои запросы:\n\n" + "\n".join(lines))


@labeler.private_message(text="/запрос закрыть <request_id>")
async def close_request(message: Message, request_id: str):
    """Закрыть запрос (нашёл человека)"""
    user_id = message.from_id
    board = load_board()

    if request_id not in board:
        await message.answer(f"Запрос #{request_id} не найден")
        return

    if board[request_id]["author_id"] != user_id:
        await message.answer("Ты можешь закрыть только свои запросы")
        return

    board[request_id]["closed"] = True
    save_board(board)

    await logger.ainfo("Запрос закрыт", request_id=request_id, author_id=user_id)
    await message.answer(f"✅ Запрос #{request_id} закрыт")


@labeler.private_message(text="/запрос удалить <request_id>")
async def delete_request(message: Message, request_id: str):
    """Удалить запрос"""
    user_id = message.from_id
    board = load_board()

    if request_id not in board:
        await message.answer(f"Запрос #{request_id} не найден")
        return

    if board[request_id]["author_id"] != user_id:
        await message.answer("Ты можешь удалить только свои запросы")
        return

    title = board[request_id]["title"]
    del board[request_id]
    save_board(board)

    await logger.ainfo("Запрос удалён", request_id=request_id, author_id=user_id)
    await message.answer(f"Запрос #{request_id} «{title}» удалён")
