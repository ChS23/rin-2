# Рин

VK-бот для сообщества разработчиков визуальных новелл [da_helper](https://vk.com/da_helper).

Рин — участница чата, инди-разработчица визуальной новеллы "Частота". Общается в чате, отвечает на вопросы, помогает с Ren'Py, и **автономно разрабатывает свою игру** по ночам.

## Архитектура

```
src/
├── bot.py                  # VK API клиент, Valkey
├── handlers/
│   ├── chat/               # Чат-агент (общение в беседе)
│   │   ├── agents.py       # LLM агенты (chat, initiative, music, self_state)
│   │   ├── handler.py      # VK message handlers, middleware
│   │   ├── tools.py        # Инструменты (web_search, generate_image, compose_midi, ...)
│   │   ├── memory.py       # Память о пользователях, контекст чата
│   │   └── utils.py        # Парсинг ответов, resolve имён
│   ├── creative/           # Автономный агент разработки VN
│   │   ├── agent.py        # Creative agent + review + music sub-agents
│   │   ├── handler.py      # Scheduler, DM панель, триггеры
│   │   └── tools.py        # 15 dev-инструментов (файлы, bash, vision, Ren'Py)
│   ├── checkin.py          # Ежедневные чекины
│   ├── event.py            # Приветствие новых участников
│   ├── roles.py            # Роли в сообществе
│   ├── projects.py         # Проекты участников
│   └── board.py            # Доска запросов
├── scripts/
│   └── entry               # Docker entrypoint
└── __main__.py
```

## Стек

- **LLM**: GLM-5.1 (Z.AI) — текст, код, агенты
- **Vision**: GLM-4.6V — анализ изображений
- **Агенты**: [openai-agents SDK](https://github.com/openai/openai-agents-python)
- **VK**: [vkbottle](https://github.com/vkbottle/vkbottle)
- **Хранилище**: Valkey (Redis-совместимый)
- **Музыка**: FluidSynth + opusenc (MIDI -> OGG Opus)
- **Картинки**: Pollinations.ai (бесплатно, без ключа)
- **Игра**: Ren'Py 8.5.2 SDK (lint, compile, web_build)
- **Хостинг**: Docker + nginx sidecar

## Чат-агент

Рин общается в VK-беседе как живой участник. 21 инструмент:

| Категория | Инструменты |
|-----------|------------|
| Файлы | read_file, write_script, edit_file, find_files, grep_files, send_file, move_file |
| Веб | web_search, read_url, download_file |
| Медиа | generate_image (Pollinations), compose_music (FluidSynth MIDI) |
| Vision | analyze_image (GLM-4.6V) |
| Проект | work_on_chastota (триггер creative сессии) |
| VK | create_archive, list_scripts |

## Creative агент

Автономно разрабатывает визуальную новеллу "Частота" на Ren'Py.

### Расписание
- **0:00, 2:00, 4:00** — ночные сессии (по лору Рин работает ночью)
- **Из чата** — `work_on_chastota("задача")` → фоновая сессия
- **ЛС панель** — кнопки: Роадмап, Файлы, Lint, Запустить сессию, Web билд, Статус

### Workflow
```
STORY_BIBLE → STRUCTURE → ROADMAP → разработка → review_story → lint → web_build → git commit
```

### Инструменты (15 + 2 sub-agents)
| Категория | Инструменты |
|-----------|------------|
| Файлы | read_file, write_file, edit_file, find_files, grep_files, delete_file, move_file, bash |
| Веб | web_search, read_url |
| Vision | analyze_image |
| Ассеты | create_image, compose_music (sub-agent) |
| Ren'Py | renpy_lint, renpy_compile, renpy_web_build |
| Ревью | review_story (sub-agent) |

### Документы проекта
```
data/scripts/chastota/
├── STORY_BIBLE.md   # Творческое видение: темы, персонажи, мир, тон
├── STRUCTURE.md     # Карта сцен, ветвления, эмоциональная кривая
├── ROADMAP.md       # Задачи: сделано / в работе / следующее
├── NOTES.md         # Технические заметки, что не работало
└── game/            # Ren'Py проект
    ├── script.rpy, definitions.rpy, options.rpy
    ├── loop1_day1.rpy, loop1_day2_day3.rpy, ...
    ├── screens.rpy, gui.rpy
    ├── images/      # Сгенерированные фоны
    └── audio/       # Сгенерированная музыка
```

## "Частота"

Атмосферная визуальная новелла о радиооператоре на арктической метеостанции, которая слышит голос на мёртвых частотах.

- **Веб-версия**: [chastota.sergeivolchkov.ru](https://chastota.sergeivolchkov.ru)
- **Движок**: Ren'Py 8.5.2
- **Статус**: первый луп написан, веб-билд работает

## Деплой

```bash
# Деплой
cd ~/rin2
git pull
docker compose up -d --build

# Логи
docker logs rin-bot --tail=50

# Запустить creative сессию
docker exec rin-valkey redis-cli SET rin:creative:trigger 1
```

## Конфигурация

`.env`:
```
VK_TOKEN=...
AI_API_KEY=...          # Z.AI API key
AI_BASE_URL=https://api.z.ai/api/coding/paas/v4
AI_MODEL=glm-5.1
FIRECRAWL_API_KEY=...   # Для web_search/read_url
```

`compose.yml` запускает 3 контейнера:
- `rin-bot` — бот + Ren'Py SDK + FluidSynth
- `rin-valkey` — хранилище
- `rin-web` — nginx для веб-версии игры
