# Ландшафт литературы (2025–2026)

Что уже сделано, где именно свободная ниша и каким будет главное возражение
рецензента по каждому вопросу.

---

<a name="rq1"></a>
## RQ1. Отравление памяти агента

### Существующая литература — почти вся про атаки

- [From Untrusted Input to Trusted Memory: A Systematic Study of Memory Poisoning
  Attacks in LLM Agents](https://arxiv.org/html/2606.04329v1)
- [Plant, Persist, Trigger: Sleeper Attack on LLM Agents](https://arxiv.org/pdf/2605.28201)
- [MemAudit: Post-hoc Auditing of Poisoned Agent Memory](https://arxiv.org/pdf/2605.23723)
- [Forensic Trajectory Signatures for Agent Memory Poisoning Detection](https://arxiv.org/pdf/2606.30566)
- [MEMSAD: Gradient-Coupled Anomaly Detection for Memory Poisoning](https://arxiv.org/pdf/2605.03482)
- Обзор темы: [Persistent Memory Poisoning](https://www.emergentmind.com/topics/persistent-memory-poisoning)

Общая рамка: **злоумышленник** внедряет запись, она живёт неограниченно и
срабатывает на каждом семантически близком запросе в будущих сессиях.

Важная деталь внутри этой литературы, близкая к нашему случаю: агенты **сами
усиливают** ложные записи — контент не инспектируется перед записью в память, а
цикл самоулучшения «считает валидным всё, что выполнилось без ошибки».

### Смежная литература — про обучение, не про память

- [LLM Web Dynamics: Tracing Model Collapse in a Network of LLMs](https://arxiv.org/pdf/2506.15690)
- [A Theoretical Perspective: How to Prevent Model Collapse in Self-consuming
  Training Loops](https://arxiv.org/html/2502.18865)
- [When AI Reviews Its Own Code: Recursive Self-Training Collapse](https://arxiv.org/html/2606.28438v1)

Ключевые понятия: MAD (Model Autophagy Disorder), ранний и поздний коллапс,
«≥5% реальных данных предотвращает долгосрочный коллапс». Но это **веса**, а не
память агента.

### Свободная ниша

**Эндогенное отравление памяти: без злоумышленника, на уровне памяти (не весов),
в реальном многомесячном деплое.**

Что даём мы и чего нет ни у кого:
1. живой механизм с наблюдаемым симптомом (сага про ноутбук, недели повторов);
2. деплой в человеческом сообществе, а не синтетическая атака;
3. абляция починки (фильтр по происхождению, консервативная экстракция,
   температура), а не только детекция;
4. сшивка двух литератур, которые сейчас не разговаривают друг с другом.

### Главное возражение рецензента

> «Memory poisoning уже описан».

**Ответ:** описан как **состязательный**. Наш случай — доброкачественный и
самопорождённый: систему никто не атакует, она травит себя штатной работой
механизма самообновления. Это другой threat model и другие меры (гигиена
происхождения вместо детекции атак).

---

## RQ2. Проактивность и право голоса в групповом чате

### Существующая литература

- [Proactive Conversational Agents with Inner Thoughts (CHI 2025)](https://arxiv.org/html/2501.00383)
  — пять стадий: триггер → извлечение → формирование мысли → оценка → участие.
  Наш гейт по сути сжатая версия; удобно как прямая точка сравнения.
- [Adaptive Turn-Taking for Real-time Multi-Party Voice Agents](https://arxiv.org/html/2606.13544)
  — ModeratorLM, turn-taking, обусловленный явно назначенной ролью.
- [Evaluating LLMs for Addressee, Turn-change, and Next Speaker Prediction in
  Meetings](https://arxiv.org/pdf/2606.17542) — **дообученные LLM предсказывают
  следующего говорящего не лучше случайного**, если роль не назначена явно.
  Хороший факт для введения.
- [Redefining Proactivity for Information Seeking Dialogue](https://arxiv.org/pdf/2410.15297)
- [Evaluating LLM-based Agents for Multi-Turn Conversations: A Survey](https://arxiv.org/pdf/2503.22458)

### Свободная ниша

Всё перечисленное — лаборатория, бенчмарки, голосовые митинги, короткие сессии.
Нет: **персона-агента, живущего месяцами в настоящем сообществе, с залогированными
решениями speak/silent и реальными исходами вовлечённости.**

Два бонуса, которых в литературе нет:
- **негативный результат**: сверхконсервативный гейт не заговаривает никогда
  (0 постов за 5 суток при 6–9 решениях в день) — режим отказа, который никто
  не описывает;
- **shadow-first как метод** безопасной калибровки проактивности до боевого
  включения (методологический вклад).

---

## RQ3. Абляции контекста

- [Decision-Aware Memory Cards: Counterfactual-Inspired Context Selection and
  Compression for Tool-Using LLM Agents](https://arxiv.org/html/2606.08151) — ближайшее
- [PersonaAgent: Bridging Memory and Action for Personalized LLM Agents](https://arxiv.org/pdf/2506.06254)

Методология отработана, но почти всегда на **качестве выполнения задач**
(«убрали примеры → 0% успеха»). Абляции **социального** контекста, влияющие на
**социальные** решения, — свежее, но на отдельную статью не тянет.

---

## Деплои «в дикой природе» — с чем нас будут сравнивать

- [Social Simulacra in the Wild: AI Agent Communities on Moltbook](https://arxiv.org/html/2603.16128)
  — тысячи автономных агентов, самоорганизующихся в сообщества. **Популяция целиком
  агентская.**
- [Gender Dynamics and Homophily in a Social Network of LLM Agents](https://arxiv.org/pdf/2602.02606)
  — Chirper.ai, платформа **только из ботов**.
- [Adopt ≠ Adapt: Longitudinal Analyses of LLM Conversations in the Wild](https://arxiv.org/pdf/2605.29018)
  — лонгитюд, но **агрегированные** логи человек-LLM.
- [The HCI Aspects of Public Deployment of Research Chatbots](https://arxiv.org/pdf/2306.04765)

**Ниша:** один персона-агент внутри **человеческого** сообщества, длительно, к тому
же **создающий артефакты** (выпустил игру, в которую играли участники). Ни
полностью агентские популяции, ни агрегированные логи этого не покрывают.

---

## Дрейф персоны (фон для введения)

- [Persona Drift (обзор темы)](https://www.emergentmind.com/topics/persona-drift)
- [Examining Identity Drift in Conversations of LLM Agents](https://arxiv.org/html/2412.00804v2)
- [Drift No More? Context Equilibria in Multi-Turn LLM Interactions](https://arxiv.org/pdf/2510.07777)
- [PsyMem: психологическое выравнивание и явный контроль памяти для ролевых LLM](https://arxiv.org/pdf/2505.12814)

Полезный факт: дрейф сильнее всего в **терапевтических и философских** ветках —
что совпало с наблюдением, как агент «уплыл» в ночном споре про симуляцию.

---

## Методология оценки

- [Agreement Metrics for LLM-as-Judge Evaluation: What to Report and Why](https://arxiv.org/html/2606.00093)
- [Reliability without Validity: Large-Scale Evaluation of LLM-as-a-Judge Models](https://arxiv.org/html/2606.19544)
- [PersonaEval: Are LLM Evaluators Human Enough to Judge Role-Play?](https://www.researchgate.net/publication/394488337)

Требования, которые предъявят:
- метрики **с поправкой на случайность** (каппа); сырой exact-match завышает
  различающую способность на десятки процентных пунктов;
- валидация судьи на человеческой разметке минимум на двух разных структурах меток;
- согласованность и смещение измерять **вместе** (высокая повторяемость может
  маскировать детерминизм, нарушающий позиционную инвариантность);
- у судей есть **agreeableness bias** — склонность подтверждать (высокий TP,
  низкий TN), поэтому самооценку агента как метрику использовать нельзя.

## Стандарты логирования

- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/blog/2026/genai-observability/)
- [MLflow: GenAI semconv](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/genai-semconv/)
- [Agent observability: complete guide 2026](https://www.braintrust.dev/articles/agent-observability-complete-guide-2026)
- [Why LLM Evaluation Results Aren't Reproducible](https://www.promptlayer.com/blog/why-llm-evaluation-results-arent-reproducible-and-what-to-do-about-it/)

Наше логирование строилось по этим требованиям — см. [DATA.md](DATA.md).
