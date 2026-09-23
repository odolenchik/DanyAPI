# PROGRESS

## Готово

- Пейсинг DeepSeek теперь считает паузу **от конца** предыдущего запроса, а не только от
  старта. Причина инцидента 2026-09-23: клиент шлёт длинные запросы цепочкой (~80 с
  каждый), старт-старт интервал удовлетворён, а аккаунт получает следующий запрос через
  2–3 с после закрытия потока → `Messages too frequent`. Отсюда парадокс
  `starts_60s=1` при блокировке. Новый `DANYAPI_MIN_END_GAP` (дефолт 5 с) отмечается в
  `account_lock` (`note_request_end()`) и берётся как поздний из двух дедлайнов
  (`seconds_until_ready`). В лог добавлено поле `end_to_start_gap` — без него причина
  неотличима от частотной. Тесты: `tests/test_accounts.py` (+8), `tests/test_retry.py`
  (+2), `tests/test_edge_cases.py` (+1). Плейбук `docs/ratelimit-playbook.md` исправлен:
  прежний совет «поднимать MIN_REQUEST_INTERVAL» для этого сценария неверен. Верификация:
  `ruff` + `pytest -q` → 1081 passed, `arch-check` OK.
- Тот же client-injected `<system-reminder>` больше не считается репликой
  пользователя и в остальных местах, где решение принимается по роли сообщения.
  Reminder приходит с `role="user"` (подтверждено wire-логом), поэтому пропуск
  сделан по маркеру `<system`, а не по роли. Починены четыре пути в
  `danyapi/tools/prompt.py`: `_has_history` (считал reminder за ход пользователя),
  `extract_system` (рендерил его в system-блок),
  `build_compact_prompt` (reminder затирал висящий вопрос — здесь важен именно
  `continue`, а не сброс `pending_user`), и history-ветка `build_prompt`
  (reminder попадал в отрендеренную историю). Замер по `DanyAPI/tooltrace.ndjson`
  (5171 промпт): history-веткой шли **8**, reminder в истории — **0**; то есть
  баг редкий, фикс защитный. Тесты: `tests/test_tools.py` (+5), проверены на
  откате (4 падают семантически, показывая reminder как реплику). Верификация:
  `ruff` + `pytest -q` → 1068 passed, `arch-check` OK.

- Ответ пользователя больше не теряется за `<system-reminder>`. Клиент (Kimi)
  дописывает напоминания (TodoList, дата, прерывание хода) отдельным сообщением
  с ролью **user** сразу после реплики пользователя. `extract_last_user`
  (`danyapi/tools/prompt.py`) шёл по сообщениям с конца и возвращал первое
  user/system — то есть сам reminder, а не вопрос; наверх уходил промпт без
  вопроса, и модель отвечала так, будто пользователь ничего не сказал. Замер по
  `DanyAPI/tooltrace.ndjson` (5158 промптов): **413 (8.0%)** состояли из одного
  reminder без текста пользователя. Роли подтверждены по wire-логу клиента:
  `"role":"user", ... "<system-reminder>..."`. Фикс: `extract_last_user`
  пропускает client-injected блоки (по маркеру `<system`) **независимо от роли**,
  `_tail_after_last_user` использует тот же предикат для границы. Тесты:
  `tests/test_tools.py` (+4) — на реальной форме с `role="user"`; проверены на
  откате (падают семантически, возвращая текст reminder вместо вопроса).
  Верификация: `ruff` + `pytest -q` → 1063 passed, `arch-check` OK.

- Вложения из истории больше не перезагружаются на каждом ходу. Раньше
  `_collect_attachments` сканировал **все** сообщения, и картинка из первого
  сообщения загружалась заново на каждом шаге (N ходов = N загрузок, каждая с
  PoW). Теперь при живой сессии сканируются только сообщения после последнего
  ответа ассистента — то есть текущий ввод; остальное уже лежит в сессии
  апстрима. Новые `_history_end_index` и параметр
  `_collect_attachments(..., skip_sent_history=)` (`danyapi/api/attachments.py`),
  вызов в `openai.py:615` передаёт `has_session`. Живой E2E (2 хода с одной
  картинкой): **1 загрузка** вместо 2, модель по-прежнему называет код с картинки
  (`ALICJ5`). Тесты: `tests/test_image_size.py` (+2), проверены на откате.
- Картинки в отпечатке диалога хешируются, а не копируются целиком.
  `_content_fingerprint` (`danyapi/tools/prompt.py`) встраивал весь data-URI
  (11 МБ → 11.5 млн символов строки на каждый запрос). Новый `_image_fingerprint`
  возвращает `image:<sha256>`. Замер: **79 символов вместо 11 534 367**, хеш
  стабилен для одной картинки и различается для разных. Тесты:
  `tests/test_image_size.py` (+2), существующий `test_content_fingerprint_variants`
  обновлён под новое поведение. Верификация обоих: `ruff` + `pytest -q` →
  1058 passed, `arch-check` OK.
- Починена тихая потеря контекста при сбросе мёртвой сессии. Продолжение фикса
  `invalid message id`: в stale-ветке сессия сбрасывалась и создавалась заново, но
  `prompt` уже был посчитан вызывающим и не пересобирался — в пустую сессию уходил
  короткий дельта-промпт. Модель отвечала, ничего не помня, и клиент получал
  HTTP 200. Проверено живьём A/B: до фикса на вопрос о коде модель ответила
  «There is no code stored in this conversation», после — `ZEBRA-7741`.
  `streaming.py` принимает `rebuild_prompt: Callable[[], str]`, и в обеих stale-ветках
  (не-стриминг и стриминг) промпт пересобирается с полной историей; `openai.py`
  передаёт лямбду через `common`. Тесты: `tests/test_retry.py` (+1), проверен на
  откате — падает семантически, не на TypeError. Верификация: `ruff` (3 файла) +
  `pytest -q` → 1054 passed, `arch-check` OK.
- Хвост tool-раунда больше не переотправляется целиком. `build_prompt`
  (`danyapi/tools/prompt.py`) на шагах внутри раунда отправляет только **новейший
  обмен** (последний assistant с tool_calls + ответившие ему tool-результаты) —
  предыдущие шаги уже лежат в сессии апстрима. Новые хелперы `_has_tool_call` и
  `_latest_tool_exchange`; без tool-вызова поведение прежнее. Замер на
  `tooltrace.ndjson` (4660 запросов): 61% запросов — шаги tool-раунда, перерасход
  был **4.5x** (12773 единицы промпта вместо 2860). Живой E2E (2-шаговый раунд,
  сервер на 6660): на шаге 2 ушло 199 символов вместо повторения всего хвоста,
  модель всё равно назвала код из первого обмена (`ALPHA-NXKUYD`). Тесты:
  `tests/test_tools.py` (+2), проверены на откате фикса. Верификация:
  `ruff` + `pytest -q` → 1053 passed, `arch-check` OK.
- Починен второй триггер `invalid message id`: протухший дисковый кэш сессий.
  Кэш (`deepseek-sessions-default.json` и др. в `DANYAPI_CACHE_DIR`) переживает
  перезапуск, и лежащий там `last_message_id` может указывать на сообщение,
  которого на апстриме уже нет. `_deepseek_status` мапил biz-ошибку 26 в **502**,
  а 502 — retryable, поэтому мёртвый id повторялся 5 раз и сессия ломалась
  навсегда (лечилось только `rm` кэша). Добавлен `_is_stale_session_error`
  (`danyapi/retry.py`): ошибка 26 распознаётся как **постоянная**, сессия
  сбрасывается через `_drop_session` и создаётся новая — в обоих путях
  (не-стриминг `streaming.py:557`, стриминг `:859`). Тесты:
  `tests/test_retry.py` (+2), проверены на откате фикса — падают без него.
  Верификация: `ruff` (4 файла) + `pytest -q` → 1051 passed, `arch-check` OK.
- Перепроверен хвост инструментов: **подтверждено живьём, что его можно не слать**.
  Тест: CALL-A — tool-раунд (секрет только в результате инструмента), CALL-B —
  минимальный промпт в ту же сессию без assistant/tool-сообщений, поэтому
  `_render_tool_tail` не вызывается. Модель ответила секретом верно (`RW7L11Z5`),
  в `tooltrace.ndjson` на CALL-B ушло 66 символов вместо 178. По ходу теста
  вскрыт второй баг: `invalid message id` из-за протухшего дискового кэша сессий
  (детали и фикс — в BACKLOG). Оба записаны в BACKLOG.
- Починен баг `invalid message id`: в не-стриминговом пути `touch_last_message`
  вызывался **до** проверки rate-limit хинта, поэтому в сессию записывался id
  сообщения, которого апстрим не сохранил. Следующий запрос слал его как
  `parent_message_id` → `DeepSeek error 26: invalid message id` → 5 ретраев по
  502, сессия оставалась сломанной. Стриминговый путь (`streaming.py:1199-1242`)
  уже имел правильный порядок; не-стриминг (`streaming.py:693-710`) приведён к
  нему. Регрессионный тест
  `test_non_stream_rate_limit_hint_does_not_record_message_id`
  (`tests/test_retry.py`) проверен на откате фикса — падает без него и проходит
  с ним. Верификация: `ruff` (2 файла) + `pytest -q` → 1049 passed, `arch-check` OK.
- Заведена отдельная рабочая копия DanyAPI в `/home/odolen/ssd_new/DanyAPI3`:
  свой git-репозиторий (ветка `main`, первый коммит `chore: initial import of
  DanyAPI`), свой venv, свой порт `DANYAPI_PORT=6660`, свой кэш
  `DANYAPI_CACHE_DIR=/tmp/danyapi3`. Копия импортирована из
  `/home/odolen/ssd_new/DanyAPI` на коммите `efaf34b` без venv, кэшей и
  untracked-черновиков. Изоляция кэша важна: обе копии по умолчанию писали в общий
  `/tmp/danyapi`, из-за чего usage и сессии двух серверов смешивались (health на
  свежем 6660 показывал чужие 368 запросов; после правки — 0). Живой E2E через
  6660: `deepseek-v4-flash` вернул `DANYAPI3 OK`, usage считает только свои
  запросы. Тесты в копии: `pytest -q` → 1048 passed.
- Включены image-модели Qwen `qwen-image-2.0-pro` / `qwen-image-3.0-pro` в
  `POST /v1/images/generations`. Раньше оба id отдавали 502 `Model not found`.
  Причина: image-id уходил в `models[]`/верхнеуровневый `model`, а upstream Qwen
  выбирает image-модель из `extra.meta.model`. Исправление: `QwenClient.completion`
  принимает `image_model` и кладёт его в `extra.meta.model`; `collect_image`
  создаёт сессию на chat-модели (`IMAGE_CHAT_MODEL = "qwen3.8-max"`,
  `danyapi/qwen/api.py`) и передаёт image-id отдельным параметром; дефолт
  `ImageGenerationRequest.model` → `qwen-image-2.0-pro` (`danyapi/api/models.py`).
  E2E вживую: 2.0 и 3.0 вернули URL картинок, `qwen3.8-omni-flash` и текстовый
  `qwen3.8-max` не сломаны. Верификация: `ruff` (3 файла) + `pytest -q` → 1031 passed.
- `size` в `POST /v1/images/generations` теперь управляет соотношением сторон на
  апстриме, а не только локальной обрезкой. Qwen принимает соотношение (не пиксели)
  в `extra.meta.size`; добавлены `QWEN_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16")`
  и `_aspect_ratio(dims)` (`danyapi/api/openai.py`), который выбирает ближайшее
  соотношение по `abs(log(target/ratio))`. `_image_generations` передаёт результат в
  `qwen_api.collect_image(image_size=...)` → `_send_completion` → `QwenClient.completion`
  (`extra.meta.size`). Локальный resize до точных пикселей сохранён. Тесты:
  `tests/test_image_size.py` (+12), `tests/test_qwen_image.py` (+2). Доки: `README.md`,
  `docs/usage.html` (`ug_images_size`), `docs/i18n.js`. Верификация: `ruff check` +
  `ruff format --check` (4 файла) + `pytest -q` → 1045 passed. Живой E2E на порту 8000
  не прогонялся: там процесс, запущенный до этих правок.
- Задокументирована отправка изображений в vision-модели. README (`### File
  attachments` → `#### Sending an image to a vision model`) и `docs/usage.html`
  (`#vision-image`) получили рабочий JSON-пример и curl с `base64 -w0`, плюс
  оговорки: `url` обязан быть `data:` URI (удалённая ссылка → HTTP 400),
  `image_url` принимает и строку, ту же картинку можно послать через `files`.
  RU-строки в `docs/i18n.js` (`ug_vision_h`, `ug_vision_p`, `ug_vision_curl_p`,
  `ug_vision_hint`). Проверка: все `data-i18n`-ключи HTML определены в i18n.js
  (скриптом, 101/101). Живой E2E: 11 МБ PNG 2340×2862 через `deepseek-v4-vision`
  → HTTP 200 за 14.3 c, осмысленное описание, подтверждённое анализом цветовых
  масс; контрольная картинка с одноразовым кодом прочитана (модель путает `8`/`B`
  на мелком шрифте). Верификация: `pytest -q` → 1045 passed, `arch-check` OK.
- Починен измеритель tool-call'ов: `tooltrace.classify` завышал долю сбоев примерно
  в 5 раз. Он делал `json.loads` на весь текст ответа и любой не-чистый-JSON валил в
  `json_malformed`, хотя настоящий `parse_tool_calls` такие ответы разбирает (проза +
  вызов, несколько JSON-объектов подряд, голый массив, починенная обрезка). Добавлена
  `_classify_via_parser`: в не-JSON ветке (кроме DSML) вердикт берётся у настоящего
  парсера, и только если он вернул `None` — `json_malformed`. На накопленном
  `tooltrace.ndjson` (4044 upstream-события) разметка изменилась с
  `ok:json_malformed 2188` на `json_malformed 9`; `ok` вырос с 1149 до 3614. Реальные
  сбои разбора — единицы, а не половина трафика. DSML-диагностика (`extra_argument`
  на `file_path`/`offset`) не тронута. Тесты: `tests/test_classify.py` (+3). Верификация:
  `ruff check` + `ruff format --check` (2 файла) + `pytest -q` → 1048 passed, `arch-check` OK.
- Починил падение стрима из-за незащищённого `_loads_lenient` в `parse_tool_calls`
  (`danyapi/tools/parse.py`). В last-resort ветке (prose + закрытый ```json-фенс с
  невалидным телом) `_loads_lenient` вызывался без `try/except`, а он намеренно бросает
  `ValueError("invalid json")`. Исключение улетало из парсера и роняло стриминговый ответ
  (`Exception in ASGI application`, HTTP 500) вместо фолбэка на обычный текст. Контракт
  `parse_tool_calls` — возвращать `None`, а не бросать (вызывающий проверяет `is not None`).
  Аудит остальных call-site'ов `_loads_lenient`/`json.loads` — все обёрнуты. Регрессионный
  тест в `tests/test_tools.py`. Верификация: `ruff` + `arch-check` + `pytest -q` → 873 passed.
- Разобрал `tooltrace.ndjson` (5593 request'а, 390 `unparsed`) и научил парсер
  (`danyapi/tools/parse.py`) обрабатывать пять классов коверканья, ранее падавших в
  `unparsed:json_malformed` (было 0/22 распознаваемых, стало 11/22 на выборке инцидентов):
  - **Порча валидных `\\`-пар** (`_fix_invalid_escapes`). Регексп `\\([^...])` матчил
    второй бэкслеш в уже валидной паре (`\\|`, `\\(`) и превращал 2 в 3 → `Invalid
    \escape`, из-за чего last-resort ветка ронял весь вызов. Переписан на посимвольный
    проход, который потребляет `\\` как единицу (request_ids 85c42f56, b9031792, 17d61eff).
  - **Чужие ключи-обёртки**: `{"calls": [...]}` / `{"tools_calls": [...]}` вместо
    `tool_calls`. `_extract_wrapped_calls` принимает набор `_WRAPPED_CALL_KEYS`
    (tool_calls / tools_calls / calls / function_calls), а `classify()` в `tooltrace.py`
    синхронизирован, чтобы такие payload'ы не помечались как `no_tool_call`.
  - **Вложенный `tool_calls`**: элемент массива сам является `{"tool_calls": [...]}` —
    теперь рекурсивно разворачивается (request_id c204a696).
  - **DSML-инвокация, слитая с inline-JSON**: `invoke name="Bash", "arguments": {...}}]}`
    без `<parameter>`-тегов; обычные opener-регекспы требуют `>` сразу после имени и такой
    вызов пропускали. Новый `_parse_dsml_fused` + ветка в `parse_tool_calls` **до**
    `_strip_dsml` (request_id 3ab1fd2b).
  - Оставшиеся нераспознаваемые случаи — разовые порчи с неэкранированной кавычкой внутри
    строки (границу строки однозначно не восстановить) и чистая проза (`no_tool_call`).
  - Регрессионные тесты в `tests/test_tools.py` (5 новых, с реальными request_id).
    Верификация: `ruff check` + `ruff format --check` + `pytest -q` → 872 passed,
    `arch-check` OK.
- Портирован учёт расхода токенов DeepSeek из `origin/dev`:
  - `danyapi/usage.py` — `UsageTracker` (totals / by_model / by_provider / by_user / recent),
    модульный singleton (`init_tracker`/`get_tracker`/`reset_tracker`/`record_usage`),
    персистентность через `JsonStore` (ключ `usage`).
  - `danyapi/tokens.py` — эвристическая оценка токенов (`estimate_tokens`,
    `count_message_tokens`, `count_messages_tokens`), CJK-aware.
  - `GET /v1/usage` (404 при выключенном трекере) и поле `usage` в `GET /health`.
  - Настройки `DANYAPI_USAGE_ENABLED` (по умолчанию 1) и `DANYAPI_USAGE_MAX_RECORDS` (1000).
  - Запись в 4 точках DeepSeek-пути: `_collect_non_stream`, `_stream_openai`,
    `_compact_retry`, `_compact_retry_stream` (через `_record_deepseek_usage`).
    `completion_tokens` считается **дельтой** `session.accumulated_tokens`, поэтому
    multi-turn диалоги не задваиваются; на `DeepSeekSession` добавлено поле
    `accounted_tokens`, которое персистится в `SessionRegistry`.
  - `prompt_tokens` в `_deepseek_usage` теперь оценивается тем же `estimate_tokens`,
    что и в трекере (раньше было жёстко 0).
  - Qwen и z.ai в учёт не входят — осознанно, как и в origin.
  - Верификация: `ruff check` + `ruff format --check` + `pytest -q` → 848 passed,
    `arch-check` OK.
- Починил нормализацию аргументов TodoList в `danyapi/tools/parse.py`. DeepSeek называет
  массив задач `items` (вместо схемного `todos`) и текстовое поле каждого элемента
  `content` (вместо `title`), из-за чего клиент отбрасывал вызов как extra property.
  Добавлен топ-уровневый алиас `items -> todos` в `_ARG_ALIASES`, а нормализация элементов
  TodoList (`_normalize_todo_items`) теперь переименовывает и `content -> title`.
  Регрессионный тест в `tests/test_tools.py` (request_id 86a22871). Верификация:
  `ruff` + `pytest -q` → 775 passed, `arch-check` OK.
- Добавлен провайдер z.ai (`danyapi/zai/`): браузерный прокси через Playwright,
  конфиг `ZAI_TOKENS`/`ZAI_TOKEN`, роутинг по префиксам `glm*`/`zai*`. Аутентификация —
  persistent Chrome-профиль (см. подробности ниже).
- Рефакторинг: `danyapi/tools.py` → пакет `danyapi/tools/` (prompt/parse/format).
- Рефакторинг API: `danyapi/api/openai.py` разделён на `models`/`attachments`/`deepseek`/`streaming`.
- Вернул `validate_tool_calls` в `ChatMessage` (`danyapi/api/models.py`) + юнит-тесты
  `tests/test_models.py` (все ветки валидатора). Цель — вернуть входную валидацию
  структуры `tool_calls`, утерянную при переносе моделей из `origin/main`.
- Починил диагностику DSML-коверканья в `danyapi/tooltrace.py::classify`: DSML-ветка
  раньше сразу возвращала `ok`, не проверяя параметры, из-за чего реальные ошибки
  схемы (`file_path` вместо `path`, `offset`/`limit` вместо `line_offset`/`n_lines`,
  `content`/`completed` вместо `title`/`done`) не помечались как `extra_argument`/
  `wrong_type`. Теперь DSML-вызов декодируется `_parse_dsml` и прогоняется через общий
  `_check_calls`. Тесты в `tests/test_classify.py` (19, включая кейсы из реального
  лога `tooltrace.ndjson`). Верификация: `ruff` + `pytest -q` → 735 passed.
- Починил алиасы параметров Bash/Edit/Write в DSML-парсере (`danyapi/tools/parse.py`).
  DeepSeek шлёт `cmd`/`desc` для Bash и `file_path` для Edit/Write вместо схемных
  `command`/`description`/`path`; Kimi отбрасывал вызов как extra property. Введён общий
  маппинг `_ARG_ALIASES` + `_normalize_tool_args`, применяемый во всех путях парсинга через
  `_normalize_calls`. Регрессионные тесты в `tests/test_tools.py` (request_id dc4ee19c).
  Заодно закомментировал незакоммиченный WIP-вызов несуществующего
  `_parse_parameter_tool_calls`, ронявшего 30 тестов (`NameError`). Верификация:
  `ruff` + `pytest -q` → 753 passed, `arch-check` OK.
- Добавлен парсер «parameter-style» вызова в `danyapi/tools/parse.py`. DeepSeek v4-pro
  иногда шлёт `<tool_calls><parameter name="name">Read</parameter><parameter .../></...>` —
  без внутреннего `<invoke>`, имя инструмента приходит как отдельный параметр. Этот формат
  не был ни JSON, ни XML-`<invoke>`, ни DSML и падал в `unparsed:json_malformed`. Новый
  `_parse_parameter_tool_calls` группирует соседние `<parameter>` теги (разрыв с любой
  меткой отделяет вызовы), параметр `name="name"` = имя инструмента, остальные — аргументы;
  обрабатывает и удвоенный вёрстак `<tool_calls><tool_calls>`. Ветка включена в
  `parse_tool_calls` между bare-array и XML-путями. Регрессионные тесты в
  `tests/test_tools.py` (request_id 26f9caa2: plain, nested, multi-param, no-name→prose,
  surrounding prose). Верификация: `ruff` + `pytest -q` → 758 passed, `arch-check` OK.
- Добавлен парсер «tag-name» вызова в `danyapi/tools/parse.py`. DeepSeek v4-pro иногда
  шлёт `<tool_calls><Bash>…</Bash></tool_calls>` — имя инструмента приходит как беглый
  элементный тег, а не через `<invoke name=...>` или параметр. Этот формат падал в
  `unparsed`. Новый `_parse_tagname_tool_calls` внутри каждого блока `<tool_calls>`
  превращает каждый `<Name>…</Name>` (кроме зарезервированных `parameter`/`invoke` и т.п.)
  в ToolCall; дочерние теги (`<command>`, `<path>`…) — аргументы через общий legacy-children
  экстрактор, вложенные opener'ы внутри обработанного элемента не считаются инструментами.
  Обрабатывает обрыв потока (отсутствие `</Name>`/`</tool_calls>` из-за 1000-символьной
  обрезки лога) — восстанавливает хвостовые аргументы; ветка уступает классическому
  `<invoke name=...>`, если тот присутствует. Ветка включена в `parse_tool_calls` между
  parameter-style и generic XML. Регрессионные тесты в `tests/test_tools.py` (request_id
  f706e842/e10ca604: single, multiple, child-tags-not-tools, truncated stream, defer-to-invoke,
  surrounding prose). Верификация: `ruff` + `pytest -q` → 764 passed, `arch-check` OK.
- Добавлен стриминг для z.ai (убран `501` в `_chat_completions_zai`). Новый
  `zai_api.stream_openai` отдаёт OpenAI `chat.completion.chunk` (role → reasoning/content
  дельты → finish → usage → `[DONE]`) идентично Qwen/DeepSeek; инкрементальный парсер
  `_IncrementalZaiSSE` переиспользуется и в batch-пути `parse_sse_body`.
- Провайдер z.ai доведён до рабочего состояния (E2E на живом профиле):
  - **Включение без токена.** Аутентификация идёт через persistent Chrome-профиль
    (`~/.danyapi/zai-browser`, переопределяется `ZAI_BROWSER_DIR`), поэтому `ZAI_TOKENS`
    больше не обязателен. Новый флаг `DANYAPI_ZAI_ENABLED` (`danyapi/config.py`,
    `settings.zai_enabled`) поднимает провайдер без токена; при наличии токена он лишь
    инъектится cookie. `lifespan` в `danyapi/api/openai.py` переведён на `zai_enabled`.
  - **Починен стриминг.** Прежний транспорт через CDP `Network.dataReceived` не работал:
    Chrome 152 в этом событии отдаёт только `dataLength`, без поля `data`, а тело ответа
    сжато Brotli (`Content-Encoding: br`). Оба фактора делали байты нечитаемыми
    (`UnicodeDecodeError`). Заменено на перехват `window.fetch` внутри страницы
    (`FETCH_TAP_SCRIPT` в `danyapi/zai/browser.py`): тело ответа `tee`-ится, браузер сам
    распаковывает Brotli, декодированный текст через `expose_binding` (`__zaiChunk`/
    `__zaiEnd`) попадает в `asyncio.Queue`. Мёртвый `_decode_cdp_data` и CDP-сессия удалены.
  - Убрано неиспользуемое поле `self._browser` и осиротевший импорт `Browser`; докстринги
    `browser.py`, `architecture.md` приведены в соответствие (стриминг поддержан, капча не
    «на каждый запрос»).
  - `ZAI_BROWSER_DIR` и `DANYAPI_ZAI_ENABLED` задокументированы в `.env.example`.
  - E2E проверено вживую: non-stream (`content='pong'`, usage 27/31/58) и stream
    (15 чанков, reasoning + content + usage + `[DONE]`, `curl exit 0`) через
    `POST /v1/chat/completions` на порту 8765.
  - Тесты: `tests/test_config.py` (+6 на `zai_enabled`), `tests/test_zai.py`
    (колбэки стрим-очереди вместо `_decode_cdp_data`). Верификация: `ruff check` +
    `ruff format --check` + `pytest -q` → 862 passed, `arch-check` OK.

## В работе

- z.ai: выбор модели по `req.model`. Добавлен реестр `danyapi/zai/models.py`
  (`ZAI_MODELS`, `resolve_zai_model`, `openai_model_list`) с wire-id, которые
  фронтенд кладёт в тело запроса: `glm-5.3` → `glm-5.3`, `glm-5.3-flash` →
  `x-preview-l` (внутренний превью-id). `ZAI_DEFAULT_MODELS` заменён на реестр.
  `ZaiBrowser._select_model` применяет модель на каждый запрос: если пришедший
  wire-id отличается от уже выбранного, пишет его в `localStorage.selectedModels`
  и возвращается на `chat.z.ai/` (свежий чат). chat.z.ai привязывает модель к
  беседе и блокирует пикер (`disabled`) на `/c/<id>`, поэтому смена модели = новый
  чат; при повторном запросе той же моделью перезагрузки нет. Проверено вживую:
  подряд `glm-5.3`, затем `glm-5.3-flash` на одном процессе дают разные модели,
  в логе обе строки `zai model selected`. Тест: `tests/test_zai.py`
  (`test_select_model_applies_on_first_call_and_switches`).
- z.ai: tool calls реализованы по образцу Qwen-ветки. `build_prompt` теперь
  возвращает `(prompt, tool_mode)`; в `collect_non_stream` и `stream_openai`
  добавлены `tool_mode`/`tool_schemas`, контент парсится через `tools.parse_tool_calls`,
  при успехе отдаётся `tools.format_tool_message` / `tools.tool_call_deltas` и
  `finish_reason="tool_calls"`; в стриме контент буферизуется до конца, при неудаче
  парсинга буфер уходит обычным `content`. `_chat_completions_zai` пробрасывает
  `tool_schemas` из `req.tools`. Тесты: `tests/test_zai.py` (+4). E2E (порт 8765):
  non-stream `Read` → `finish_reason=tool_calls`; stream `Glob` → 16 чанков,
  `tool_calls=['Glob']`. Верификация: `ruff check` + `ruff format --check` +
  `pytest -q` → 866 passed, `arch-check` OK.
- z.ai: осталось (опционально) решение, нужен ли учёт токенов z.ai в
  `UsageTracker`.
- Ожидается решение по `validate_role`.

## Известные проблемы (см. BACKLOG.md)

- Полный CI-гейт `pytest --cov-fail-under=100` падает: покрытие ~96% — дефицит
  рефакторинга в `streaming.py`, `tooltrace.py`, `prompt.py` и др., не относится к
  текущим фиксам (локальный `pytest -q` → 848 passed).
- `ruff format --check` ранее падал на `scripts/smoke_tooltrace.py` и
  `tests/test_tooltrace.py`; оба отформатированы, сейчас формат-гейт зелёный.

## Текущий commit

Закоммичено: `perf(api): stop re-uploading attachments and hashing whole images` —
`danyapi/api/attachments.py`, `danyapi/api/openai.py`, `danyapi/tools/prompt.py`,
`tests/test_image_size.py`, `tests/test_tools.py`, `PROGRESS.md`. Предыдущий
коммит: `docs(backlog): note that trace.dany misses a rebuilt prompt`.
