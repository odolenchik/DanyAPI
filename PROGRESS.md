# PROGRESS

## Готово

- **Парсер: `parameters` как синоним `arguments` в одиночном объекте.** DeepSeek
  иногда отдаёт вызов как `{"tool": "nmap", "parameters": {...}}` — с ключом
  `parameters` вместо `arguments`. `_extract_one_call`/`_extract_calls` такой
  объект не распознавали → `unparsed:no_tool_call`. Добавлен `_nested_arguments`:
  берёт `arguments`, а если он пуст — `parameters`; ключ `parameters` исключён из
  `_flat_arguments`, чтобы не утечь в аргументы. Явный `arguments` по-прежнему
  приоритетнее. Из лога починились 4 запроса (`6e02a45f`, `0771891f`, `c905e637`,
  `89c7bf8d`). Форма `{"tool", "why", "goal"}` (без аргументов) остаётся
  `no_tool_call` — это план-документ, не вызов (см. BACKLOG). Тесты:
  `tests/test_tools.py` (+3).

- **Парсер: DSML с одной палкой перед `DSML`.** DeepSeek иногда отдаёт открывающие
  теги вида `<|DSML||parameter ...>` — одна палка слева, две справа, — тогда как
  закрывающие теги канонические. `_DSML_SEP` требует по две палки с каждой стороны,
  поэтому такие вызовы были невидимы всем DSML-веткам и терялись (`parse.result`
  `unparsed`, `issue=None`). Добавлен `_repair_dsml_separator` (regex
  `_DSML_SINGLE_PIPE_OPENER_RE` с negative lookahead): он нормализует только битый
  opener до канонического вида, не трогая корректные теги. Вызывается в начале
  `parse_tool_calls` (tooltrace `b1c6a1cc`; из 6 upstream-событий с таким
  разделителем 5 — реальные вызовы — теперь парсятся, шестое — проза с цитатой в
  бэктиках). Канонический `_DSML_SEP` не ослаблен. Тесты: `tests/test_tools.py`
  (+2: разбор реального контента, сохранность канонических тегов). Playbook
  (`docs/tooltrace-playbook.md`) дополнен новым паттерном.
  Верификация: `ruff check` + `ruff format --check` + `pytest -q` → **1155 passed**,
  `arch-check` OK.

- **Compaction: сохранение текущего хода + суммаризация + один запрос.**
  Переход в новый чат при переполнении контекста
  (`_compact_retry`/`_compact_retry_stream`) был сделан плохо, с прямыми багами:
  - **Текущий вопрос терялся.** `build_compact_prompt` собирал только
    *завершённые* пары user→assistant, поэтому последнее сообщение пользователя
    (ради которого и делается запрос) в новый чат не попадало — модель отвечала
    на старый вопрос. Теперь текущий ход всегда сохраняется (вопрос + для
    tool-раунда сам вызов и результаты).
  - **Не было ограничения размера.** «Компактное» тело собиралось из 8 пар без
    учёта длины и могло превысить лимит (замер: 124k символов при рабочем
    лимите 52k). Теперь тело обрезается до `MAX_APPEND_PROMPT_CHARS`: сначала
    выбрасываются старейшие пары, затем усекаются tool-результаты.
  - **Схема инструментов и JSON-режим терялись** (параметр `tool_schemas` был
    мёртвым). Теперь `build_compact_prompt` принимает raw `tools`/`tool_choice`/
    `response_format` и рендерит их в system-блок; проброшено из `openai.py`
    через `_collect_non_stream`/`_stream_openai`.
  - **Суммаризация вместо обрезки.** Перед переездом старый контекст
    сжимается в резюме моделью на *новой* сессии (`_summarize_for_handoff`,
    инструкция `HANDOFF_SUMMARY_INSTRUCTION`); резюме кладётся в новый чат над
    сохранённой историей, так что факты/решения/пути не теряются. Нет пар для
    суммаризации → сразу обычный компакт.
  - **Один запрос вместо двух.** Раньше system и тело шли двумя сообщениями
    (2 PoW, 2 точки отказа); теперь system+резюме+история+текущий ход уходят
    одним промптом. При использовании сессии под резюме ответ приходит в неё же.
  - Попутно исправлен `_truncate_head`: при лимите меньше маркера результат
    превышал лимит.
  Тесты: `tests/test_tools.py` (+8), `tests/test_api_helpers.py` (+1, обновлён
  один под один запрос), `tests/test_prompt_accounts_coverage.py` (переписан
  под новые семантики). Верификация: `ruff` + `ruff format --check` +
  `pytest -q` → **1153 passed**, `arch-check` OK; `tools/prompt.py` — 100%
  покрытие. Живой E2E: DeepSeek принял входы до 400k символов, поэтому
  `CONTEXT_LENGTH_EXCEEDED` вживую не воспроизвести — путь покрыт мок-тестами,
  которые дёргают именно эти ветки.

- **Qwen: reattach + sticky-чат для генерации картинок (анти-блок).**
  Причина суточного блока генерации: в `_image_generations` не было ни context
  dedup, ни reattach, поэтому при `session_id=None` **каждый** запрос картинки
  создавал новый чат, а Qwen за это ограничивает и в итоге блокирует генерацию.
  - **Sticky-чат на пару «аккаунт + image-модель».** `QwenAccount` держит
    привязку в `qwen-sticky` JsonStore (`sticky_image_chat` /
    `set_sticky_image_chat`); `collect_image` без `session_id` переиспользует
    закреплённый чат (явный `session_id` уважается и не перезаписывает pin).
  - **Reattach для Qwen.** `QwenClient.fetch_chat` (`GET /api/v2/chats/{id}`),
    `QwenSessionRegistry._attach` восстанавливает `last_response_id` из
    `history.currentId` (фолбэк — id последнего assistant). Чат-тип учтён:
    чат возобновляется только при совпадении `chat_type` (`t2t`/`t2i`), поэтому
    текстовый чат не продолжается image-запросом. Image-сессии создаются
    `chat_type="t2i"`.
  - Reattach-хук добавлен и в Qwen chat-путь (`_chat_completions_qwen`),
    зеркально DeepSeek (до `build_prompt`).
  - Диагностика вживую: у image-чатов (t2i) история и `history.currentId`
    работают так же, как у t2t (чат с 66 сообщениями); `create_chat` принимает
    `chat_type` и создаёт t2i-чат; `response_id` из стрима == `id` assistant в
    истории == `history.currentId`; продолжение по одному chat_id сохраняет
    контекст. Прежний тезис «у Qwen нет API истории» **опровергнут**.
  Тесты: `tests/test_sessions.py` (+9 Qwen attach), `tests/test_edge_cases.py`
  (+3 sticky), `tests/test_qwen_image.py` (+3 sticky/reattach). Верификация:
  `ruff` + `ruff format --check` + `pytest -q` → **1143 passed**, `arch-check` OK.
  E2E (порт 6720): два image-запроса без `session_id` → **1** `create chat`,
  sticky-файл указывает на тот же чат; reboot (удалены `qwen-sessions`, оставлен
  `qwen-sticky`) → генерация вернула картинку, `reattached: true`, **0**
  `create chat`.

- **Возврат к тому же чату после потери локальной записи (reattach).**
  Раньше `session_id` == upstream `chat_session_id`, но при промахе локального
  реестра (`SessionRegistry.obtain`) создавался **новый** чат: LRU-вытеснение,
  TTL, перезапуск или очищенный temp-кэш рвали связку, а чат на стороне
  DeepSeek оставался жив.
  - `SessionRegistry.attach()` + провайдерный хук `_attach()`
    (`danyapi/sessions.py`): при промахе реестра пробует восстановить сессию.
  - `DeepSeekSessionRegistry` (`danyapi/accounts.py`): `_attach` тянет
    `history_messages`, берёт **max(message_id)** как `last_message_id` и
    `accumulated_token_usage` как восстановленный счётчик, ставит
    `reattached=True`. Ошибка/пустая история → `None` (мягкий фолбэк на новый чат).
  - `api/openai.py` вызывает reattach **до** `build_prompt`, поэтому при успехе
    `has_session=True` и уходит дельта-промпт, а не вся история; сессия
    регистрируется в пуле (`pool.register`).
  - `_attach` выставляет `accounted_tokens = accumulated_tokens`, чтобы первый
    ход после возврата не пересчитывал всю историю в usage.
  - Долговечная привязка session→account: `account_for_session` при TTL чистит
    только in-memory запись и падает в персистентный `JsonStore`
    (`_affinity_lookup`), поэтому сессия и после рестарта маршрутизируется на
    аккаунт-владелец чата. Сбрасывается только при broken-аккаунте или `forget()`.
  - Диагностика на живом DeepSeek: поле id сообщения — `message_id` (не `id`),
    порядок брать по max, `last_message_id`/токены восстанавливаются, чужой UUID
    → biz_code 1 «invalid chat session id», не-UUID → biz_code 400; оба ловятся.
  Тесты: `tests/test_sessions.py` (+14), `tests/test_accounts.py` (+2, обновлён
  один под новые TTL-семантики). Верификация: `ruff` + `ruff format --check` +
  `pytest -q` → **1129 passed**, `arch-check` OK. E2E (порт 6712): создать чат →
  убить процесс → удалить `deepseek-sessions-default.json`, оставить
  `deepseek-affinities-default.json` → перезапуск → запрос с тем же
  `session_id` → тот же chat id, кодовое слово сохранено, в логе нет
  `create session`.

- **Парсер ловит tool-вызов с неквотированным ключом (`<{tool_calls: [...]}>`).**
  «Холостые» вызовы инструментов: DeepSeek иногда теряет кавычки вокруг ключа
  `tool_calls` и оборачивает объект в угловые скобки — `<{tool_calls: [{...}]}>`.
  Ни один repair-путь (`_normalize_single_quotes`, `_strip_trailing_commas`,
  `_fix_invalid_escapes`, `_fix_unbalanced_json`) голый ключ не чинил, поэтому
  `parse_tool_calls` возвращал `None`, и весь JSON уходил клиенту как обычный
  `content` — вызов «не срабатывал». Добавлен `_quote_bare_keys`
  (`danyapi/tools/parse.py`): кавычит идентификатор только там, где ожидается
  ключ (после `{` или `,`), и никогда внутри строки — URL/проза не задеты.
  Подключён в `_loads_lenient` (в т.ч. в комбинации с `_fix_invalid_escapes`).
  Уголки отдельно не чинятся: `_balanced_json` игнорирует `<`/`>` вокруг `{...}`.
  Проверено на реальном контенте из лога (rid `93870405f7794e73b8cd7f71065372e2`,
  426 симв.): было `NONE` → стало 3 вызова, `classify: ok`.
  Тесты: `tests/test_tools.py` (+6) — angle-wrapper, без обёртки, вложенные
  голые ключи, неизменность валидного JSON, URL-проза, строковые значения.
  Верификация: `ruff` + `ruff format --check` + `pytest -q` → **1115 passed**,
  `arch-check` OK.

- **Фаза D переработки tool use: валидация по JSON Schema + repair-цикл.**
  Раньше парсер `danyapi/tools/parse.py` принимал любой распарсенный вызов без
  проверки против схемы клиента — коверканье (`extra_argument`, `wrong_type`,
  отсутствие required-поля) уходило клиенту как есть, и Kimi ругался
  «Invalid args for tool». Добавлен единый валидатор `danyapi/tools/validate.py`
  (`type_matches` / `validate_call` / `validate_calls` / `summarize` /
  `build_repair_prompt`), а диагностический `tooltrace._check_calls` теперь
  делегирует в него — раньше логика типов дублировалась и могла разойтись.
  Новое: `missing_required` (по ключу `__required__` из `tool_schema_map`).
  При провале валидации основной путь делает **одну** repair-попытку в той же
  сессии (`_repair_tool_calls` в `streaming.py`, через обобщённый
  `_collect_continuation(prompt=...)`); сбой repair не роняет запрос —
  сохраняется исходный результат. Подключено во все три точки DeepSeek
  (non-stream, основной stream, compact-retry). `tool_schema_map` и `parse.py`
  не трогались — нулевой риск для 242 существующих тестов парсера.
  Тесты: `tests/test_validate.py` (+21), `tests/test_tools_api.py` (+5).
  Верификация: `ruff` + `ruff format --check` + `pytest -q` → **1109 passed**,
  `arch-check` OK. E2E через живой сервер (порт 6670): non-stream и stream
  вернули валидные tool_calls без утечки мусора в `content`; `tooltrace`
  `parse.result=ok`, `issue=None`.
  Осталось (Фаза B): инкрементальный разбор потока вместо буферизации
  `content_buf`; Фаза C — точечно, только если D не хватит.

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

- Полный CI-гейт `pytest --cov-fail-under=100` падает: покрытие ~97% — дефицит
  рефакторинга в `api/streaming.py` (~102 строки), `tools/parse.py` (~52) и др.,
  не относится к текущим фиксам (локальный `pytest -q` → 1143 passed).
  Пять coverage-сюит (`tests/test_*_coverage.py`) подняли покрытие с 94% до 97%.
- `ruff format --check` ранее падал на `scripts/smoke_tooltrace.py` и
  `tests/test_tooltrace.py`; оба отформатированы, сейчас формат-гейт зелёный.

## Текущий commit

В работе (не закоммичено): compaction — сохранение текущего хода, суммаризация
старого контекста, один запрос вместо двух; см. «Готово» выше.

Последний коммит: `test: add targeted coverage suites and a CLAUDE.md agent guide`
(`3568633`). До него: `feat(sessions): reattach lost chats and pin one chat per image model`
(`33ad32b`).

Следующее: добить покрытие до 100% для CI-гейта — начать с `api/streaming.py`
(см. «Известные проблемы»).
