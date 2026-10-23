# PROGRESS

## Готово

- **Пейсинг Qwen и антибот-капча как отдельное состояние.** Симптом: 22.10 Qwen
  отвечал на всё капчей (`FAIL_SYS_USER_VALIDATE` / `RGV587_ERROR` / `punish`),
  DanyAPI крутил 5 повторов на запрос и отдавал голый 502, а `/health` показывал
  `qwen: true, broken: 0` — поломка была невидима мониторингу. За 15:22–19:00
  накопилось 650 ошибок при нуле успешных генераций.
  - **`danyapi/qwen/accounts.py`:** у `QwenAccount` появился пейсинг — серия из
    `DANYAPI_QWEN_BURST` (4) запросов, пауза `DANYAPI_QWEN_MIN_INTERVAL` (3 c)
    плюс джиттер до `..._JITTER` (4 c), после серии отдых
    `DANYAPI_QWEN_BURST_PAUSE` (360 c) плюс джиттер до `..._JITTER` (240 c).
    Джиттер выбирается в `note_request_start` и хранится, иначе каждое чтение
    дедлайна меняло бы паузу. Плюс `mark_captcha` / `captcha_until`:
    отдельное состояние, не auth и не квота.
  - **`danyapi/qwen/api.py`:** `CaptchaChallenge` и `_is_captcha_text`;
    распознавание стоит до `raise_for_status` и до цикла повторов, поэтому
    капча падает за один запрос (было 5). `_pace()` в `_send_completion` —
    единственная точка, через которую идут все запросы к Qwen; способность
    проверяется `inspect.iscoroutinefunction`, чтобы `MagicMock`-двойники не
    принимались за настоящий аккаунт.
  - **`danyapi/accounts.py`:** `_account_state` знает `captcha`; `stats()`
    отдаёт `broken_captcha` и `captcha_resets_in`.
  - **`danyapi/api/openai.py`, `danyapi/config.py`:** проброс настроек пейсинга,
    `DANYAPI_QWEN_CAPTCHA_COOLDOWN` (3600 c, минимум 60).
  - **Тесты:** `tests/test_qwen_pacing.py` (10) — серия, джиттер, `wait_turn`,
    состояние captcha, маркеры, снятие сессии. В `FakeAccount` добавлен
    `mark_captcha`.
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → **1398 passed**. На плате: капча отдаётся за 2.4–3.5 c одним
    запросом, `/health` показывает `broken_captcha` и `captcha_resets_in`.

- **Qwen: браузерный транспорт вместо HTTP.** Симптом: chat.qwen.ai отвечал на
  любой запрос капчей (`FAIL_SYS_USER_VALIDATE` / `RGV587_ERROR` / `punish`),
  причём и текстовые, и картиночные запросы падали в 502. Проверено, что дело не
  в токене и не в IP: httpx, `curl_cffi` с TLS-отпечатком Chrome и даже свой
  `fetch` изнутри страницы получают ту же стену — проходит только запрос, который
  отправляет сам SPA.
  - **Новый модуль `danyapi/qwen/browser.py`:** patchright + persistent-профиль на
    каждый аккаунт. Авторизация — `refresh_token` в cookie на домене `.qwen.ai`
    (не `chat.qwen.ai`: обмен идёт на `auth.qwen.ai`, и cookie с дочернего домена
    туда не уходит, refresh отвечает 401). Отправка — печать в композер и клик по
    `send-button`, ответ ловится `expect_response`, парсится тем же
    `QwenStreamReconstructor`.
  - **Режим изображений:** у Qwen картиночных моделей нет в пикере, они живут в
    отдельном режиме композера `Create Image` (`chat_type: t2i`). Соотношение
    сторон задаётся в сабконфиге (`extra.meta.size`), варианты
    `1:1, 2:3, 3:2, 3:4, 4:3, 16:9, 9:16`; по умолчанию берётся 2:3 — вертикаль
    Pinterest.
  - **`danyapi/qwen/api.py`:** `_BrowserResponse` (httpx-совместимая обёртка, чтобы
    существующие циклы не переписывать), `_real_browser` (проверка через
    `inspect.iscoroutinefunction`, иначе `MagicMock`-аккаунт уводит тесты в
    браузерную ветку), ветки браузера в `_send_completion` и `collect_image`.
  - **Капча:** распознаётся до ретраев (`CaptchaChallenge`), аккаунт уходит в
    отдельное состояние `broken_captcha` с собственным кулдауном; `/health` отдаёт
    `broken_captcha` и `captcha_resets_in`. С `DANYAPI_QWEN_CAPTCHA_WAIT` окно
    остаётся открытым, после решения страница перезагружается, режим и соотношение
    восстанавливаются, запрос повторяется.
  - **Переиспользование чата:** картинки идут в один закреплённый чат на аккаунт
    (`sticky_image_chat`), а не в новый на каждый запрос — у Qwen есть лимит на
    количество новых чатов, и именно его превышение ведёт к суточной блокировке.
  - **Пейсинг:** два режима. HTTP — серия с паузами и длинным отдыхом. Браузер —
    ровный think time (`DANYAPI_QWEN_BROWSER_INTERVAL` 20 с + джиттер до 20 с):
    один запрос и так длится 30–70 с, серии и долгие отдыхи только морили бы
    конвейер голодом. `collect_image` теперь пейсится (раньше пропускал).
  - **Тесты:** `test_qwen_browser.py` (25), `test_qwen_browser_flow.py` (48),
    `test_qwen_api_browser.py` (25), `test_qwen_pacing.py` (18). Playwright
    полностью замокан — браузер не запускается, сеть не используется.
    Покрытие `browser.py`, `qwen/accounts.py`, `config.py` — 100%.
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → **1500 passed**. Живые прогоны: текст 26 с, картинка 63–75 с,
    `n=2` — две картинки 1024×1536 за 135 с в одном чате, повторный запрос
    переиспользует закреплённый чат.

- **Сторож против цикла повторяющихся tool-вызовов (`repeat_loop`).**
  Симптом: сессия шлёт один и тот же вызов раунд за раундом, пока не кончится
  контекст (14 подряд в `a2b6de5d`, 10 в `953c0ca4`); приходилось жать «стоп» в вебе,
  а без этого DeepSeek грозил блокировкой за too frequent.
  - **Новый модуль `danyapi/repeat.py`:** сигнатура вызова (имя + аргументы с
    сортировкой ключей), счётчик живёт в `DeepSeekSession.extra` и переживает
    раунды; любой другой вызов или раунд без вызовов сбрасывает счётчик.
  - **`danyapi/api/streaming.py`:** проверка после `_validate_parsed` в обоих путях
    (non-stream и stream). При срабатывании — ошибка `finish_reason: repeat_loop`,
    в стриме дополнительно `_try_stop_stream`, чтобы аккаунт не жёг лимит.
  - **`danyapi/config.py`:** `DANYAPI_REPEAT_GUARD` (дефолт 1) и
    `DANYAPI_REPEAT_THRESHOLD` (дефолт 5).
  - **`scripts/merge_traces.py`:** отдельная секция `repeat loops` в сводке.
  - **Тесты:** `tests/test_repeat.py` (11, покрытие 100%) + 4 интеграционных в
    `tests/test_retry.py` (стрим, non-stream, выключенный гард, разные вызовы).
  - Документация: `.env.example`, `README.md`, `docs/configuration.html`.
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → **1388 passed**.

- **DSML: opener без DSML-разделителя.** `<invoke>` и закрывающие теги DSML-ные, а
  параметр написан обычным XML (`<parameter name="path">`). Аргумент невидим всем
  param-регексам → пустые аргументы → `missing_required` → цикл (tooltrace `42585858`).
  - **`danyapi/tools/parse.py`:** `_repair_dsml_bare_param_opener` /
    `_BARE_PARAM_OPENER_RE`; только при DSML-префиксном `<invoke>`, чтобы не сломать
    чистый XML-путь.
  - **Тест:** `test_dsml_bare_param_opener_inside_dsml_invoke`.
  - Свип по логу: пустых `ok:missing_required` 61 → **41**.
- **DSML: `=` → `:=""` и разделитель → `,=""`.** Форма `name="timeout" :="" 600}`
  (tooltrace `b1240bcd`) не матчится ни одним opener-регексом.
  - **`danyapi/tools/parse.py`:** `_parse_dsml_colon_attrs` как последний резерв
    invoke-ветки; строгий фильтр ключей и значений, обычное `"key":value` — только
    для ключей из схемы и после mangled-формы.
  - **Тест:** `test_dsml_colon_equals_attributes_recovered`.
  - Свип по логу: пустых `ok:missing_required` 41 → **15** (из них 6 — обрезанные
    на 5000 символов ответы).
- **DSML: голое имя атрибута в параметре — `parameter name=` и `=` потеряны.**
  Симптом: тот же цикл, что и выше (10 подряд `ok:missing_required`), но от opener'а
  остаётся только ключ и висячая кавычка — без слова `parameter` и без `=`
  (tooltrace `5186a5ff`, `6ea8d3ab`, `ab9045d8`, сессия `953c0ca4`).
  - **Корневая причина:** ни один param-регекс не ждёт такого тега, аргумент пропадает,
    вызов уходит с пустыми аргументами.
  - **`danyapi/tools/parse.py`:** `_repair_dsml_naked_attributes` /
    `_DSML_NAKED_ATTR_RE` — переписывает в канонический opener, сохраняет
    `string="true"`; вызывается до `_repair_dsml_bare_attributes`.
  - **Тест:** `test_dsml_naked_attribute_name_inside_invoke` (через `parse_tool_calls`,
    т.к. ремонт идёт до DSML-ветки).
  - Проверено свипом по логу: `ok:missing_required` с аргументами 948 → **1039** из 1100.
- **DSML: `parameter="X"` внутри `<invoke>` — зацикливание repair-раундов.**
  Симптом: сессия крутит один и тот же вызов, пока не кончится контекст; в трейсе
  ровный ряд `ok:missing_required` с `issue=Bash.command` каждые ~19 с (14 подряд,
  tooltrace `2baa4153`, `0d19b84c`).
  - **Корневая причина:** invoke-ветка `_parse_dsml` при повторном bounded-скане
    слушала только `_DSML_PARAM_OPENER_RE` (`parameter name="X"`), поэтому
    `parameter="command"` была невидима → вызов уходил как `Bash {}` → клиент
    браковал missing required → repair повторял промпт → модель слала ту же форму.
  - **`danyapi/tools/parse.py`:** скан обеими формами
    (`_DSML_PARAM_OPENER_RE` + `_DSML_PARAM_EQ_OPENER_RE`, сортировка по позиции)
    и срез висячей кавычки для eq-варианта — как в param-only ветке.
  - **Тест:** `test_dsml_param_eq_opener_inside_invoke` в `tests/test_tools.py`;
    проверено прогоном реального `upstream.content` из лога.
  - **`docs/tooltrace-playbook.md`:** новый пункт в каталоге коверканий + отличие
    от `degenerate_repeat` (цикл repair-раундов, а не runaway в одном ответе).
- **DSML: слитый close+open параметра и границы invoke починен (tool-call мангл).**
  Симптом: Kimi падал с «Invalid args for tool», в трейсе `ok:missing_required` —
  имя инструмента есть, аргументы пустые.
  - **Корневая причина:** DeepSeek сцепляет параметры так, что close одного служит
    open следующего (`</…parameter name="path" string="true">` закрывает `pattern`
    и открывает `path`). `_DSML_PARAM_RE` требует отдельный close, поэтому не матчил
    НИ ОДНОГО параметра, `shared_close` оставался `False`, ветка ре-извлечения не
    запускалась (tooltrace `92b6a328`, `793dd6ec`).
  - **`danyapi/tools/parse.py`:**
    - `_DSML_FUSED_CLOSE_OPEN_RE` + `_repair_dsml_fused_close_open` — расщепляет
      слитый close-with-name на канонический close + open; подключено в
      `parse_tool_calls` до `_repair_dsml_bare_attributes`.
    - Граница тела invoke: при отсутствии `</invoke>` (или close за следующим
      `<invoke>`) тело заканчивается на следующем opener — значения больше не
      утекают между вызовами (tooltrace `793dd6ec`, `35bbad80`).
    - Bounded re-scan запускается и когда первый проход не матчил ничего, но
      parameter-opener'ы есть (`not matched_params`).
  - Тест: `tests/test_tools.py::test_parse_dsml_fused_close_open_chained_params`
    (из реального request_id `92b6a328`).
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` + `pytest -q`
    → **1361 passed**.
  - **Осталось:** повтор `path` с `</invoke>`-разделителем (tooltrace `46a56c59ce`)
    и `<arguments>`-носитель без `=` (`d352973b79`) — редкие классы, не починены.

- **Qwen: ротация refresh-токена хранится по каждому аккаунту (переживает рестарт).**
  Симптом: после `systemctl restart danyapi3` Qwen падал в `qwen accounts ready: 0`,
  `/health` → `qwen: false`, хотя до рестарта работали 2 из 3 аккаунтов.
  - **Корневая причина:** credential-store был **одним слотом на весь пул**.
    Единственный `JsonStore("qwen-credentials")` (`danyapi/api/openai.py:67`)
    передавался во все `QwenClient`, а `persist_rotated_token` писал в общие ключи
    `source`/`token` — каждая ротация **перезаписывала** предыдущую, так что из трёх
    аккаунтов на диск попадал только последний. Остальные после рестарта брали из
    `.env` уже отозванные значения и отвергались.
  - **`danyapi/qwen/accounts.py`:** `persist_rotated_token` пишет по ключу
    `_token_fingerprint(configured)` (отпечаток исходного `.env`-токена аккаунта),
    `load_effective_token` читает по тому же ключу. Несколько аккаунтов в одном сторе
    больше не затирают друг друга. Добавлен legacy-фолбэк на старый формат
    `{source, token}` — накопленная запись не теряется при обновлении.
  - Тесты: `tests/test_qwen_credentials.py` — обновлён round-trip под ключ-отпечаток,
    новые `test_multiple_accounts_persist_independently` (регрессия на схлопывание в
    один слот) и `test_load_uses_legacy_single_slot` (фолбэк).
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` + `pytest -q`
    → **1360 passed**.
  - **Осталось:** на плате все три `.env`-токена уже отозваны (проверено живым
    запросом), поэтому Qwen поднимется только после подстановки свежих
    `refresh_token`; главный E2E-критерий (рестарт → снова `accounts ready: N`)
    проверяется после этого.

- **Qwen: кулдаун по квоте больше не выглядит мёртвым токеном.**
  Симптом: бот присылал тревожное 🔴 «Qwen: все аккаунты отвалились», хотя
  на деле один аккаунт был на суточном кулдауне по квоте и должен вернуться
  сам. Причина: `/health` отдавал только `healthy`/`broken`, а `broken`
  смешивал auth-ошибку (токен мёртв) и временный кулдаун (`quota_until`).
  - **`danyapi/accounts.py`:** классификатор `_account_state()` — 'ok' /
    'quota' (есть `quota_until`) / 'auth'. `AccountPool.stats()` теперь
    считает за один проход и отдаёт новые ключи `broken_quota`, `broken_auth`,
    `quota_resets_in` (секунды до ближайшего возврата). Старые `healthy`/`broken`
    не изменили смысл — обратная совместимость сохранена. Для DeepSeek
    (нет `quota_until`) всё остаётся auth, как раньше.
  - **Бот** (`/opt/orangepi-bot/bot.py`, вне этого репозитория):
    `provider_status` возвращает `(severity, текст)`, `fmt_duration()` даёт
    «~23 ч». Кулдаун → 🟡 «на кулдауне по квоте, вернётся через ~N» (повтор
    только на переходе, без спама), auth → 🔴 «отвалился (auth N из total)».
    Для старого инстанса без новых ключей сохраняется прежнее поведение.
  - Тесты: `test_accounts.py` (+3: разбивка quota/auth, выбор ближайшего
    кулдауна), `test_qwen_api.py` (+1: реальный `QwenAccount` после
    `mark_quota_exhausted`/`mark_broken`). Верификация: `make check`
    (ruff + arch-check) зелёный, `pytest -q` по затронутым файлам 149 passed,
    py3.10 AST OK.
  - На плате 6660: `/health` отдаёт `broken_quota=1, broken_auth=0,
    quota_resets_in≈86400`; `/tokens` → «🟡 Qwen: 1 из 2 на кулдауне по
    квоте, вернётся через ~23 ч»; живой запрос `qwen3.8-max` → 200.
    Ограничение: `quota_until` — монотонное время, после рестарта процесса
    кулдаун сбрасывается (Qwen-учётки создаются заново здоровыми).

- **DSML: неоднозначный одинокий `{path}` резолвится по порядку объявления.**
  Инцидент tooltrace `533f7fa6` (Kimi-код: два `Read` подряд, обе ошибки
  «no_tool_call»). Блок без `<invoke>` из двух `parameter="path"` — `{path}`
  подходит и `Read`, и `ReadMediaFile` (required у обоих = ровно `path`),
  поэтому `_infer_tool_from_params` честно отдавал `None` и весь блок падал в
  `unparsed:no_tool_call`. Важно: прошлый вывод «уже парсится» был артефактом
  проверки с урезанной схемой (только `Read`+`Bash`); с реальным набором
  Kimi (26 инструментов) инцидент воспроизводился на живом процессе.
  - Новый `_resolve_tool_by_order` (`danyapi/tools/parse.py`): в DSML param-only
    пути выигрывает инструмент, объявленный клиентом раньше; схемы с
    невыполненным `__required__` (`Edit`/`Write`) пропускаются, поэтому
    `{path}` → `Read`, а не `Edit`. Глобальный `_infer_tool_from_params`
    сохраняет строгий контракт (тест `..._ambiguous_schemas_skips` не тронут).
  - Репро на живом логе с полным набором Kimi: `533f7fa6` → 2 Read,
    `fd031971` → Read+Read+Bash, `7b1bb070` и `4e3abd5c` тоже зелёные.
  - Расширение: разделитель DSML чинится не только слева. `_DSML_SEP` требует
    две палки с каждой стороны, а DeepSeek шлёт и право-однопалочные, и
    двусторонние. `_repair_dsml_separator` теперь нормализует любую сторону
    (1→2), сохраняя символ палки. Второй паттерн из того же инцидента —
    самозакрытый маркер: лишний `>` перед словом тега прячет opener, его срезает
    `_repair_dsml_marker_gt` (голый закрывающий маркер не трогает). Инцидент
    tooltrace `2a0ddca7` — `browser_snapshot` на 5 аргументов — парсится.
  - Тесты: `test_dsml_param_only_ambiguous_path_resolves_by_declaration_order`,
    `test_dsml_selfclosed_marker_and_double_single_pipe_is_repaired`, обновлён
    `test_dsml_single_pipe_repair_leaves_canonical_tags`.
  - Верификация: `ruff check` + `ruff format --check` + `make check` (arch-check OK)
    + `pytest -q` → **1354 passed**. Плейбук обновлён.

- **DSML: parameter-opener с вклеенным маркером перед `>` + ложный bare-attr.**
  Инцидент tooltrace `4e3abd5c` (Kimi-код, Edit: `must NOT have additional property
  'parameter'`). Два наложившихся бага в `danyapi/tools/parse.py`:
  - DeepSeek закрыл кавычку имени параметра лишним DSML-маркером перед `>`
    (`parameter="path"` + маркер + `>` вместо `parameter="path">`). Оба opener-регекса
    (`_DSML_PARAM_OPENER_RE`, `_DSML_PARAM_EQ_OPENER_RE`) ждут `>` сразу после имени,
    поэтому блок был невидим всем DSML-веткам → `unparsed:no_tool_call`. Новый
    `_repair_dsml_glued_param` (по образцу `_repair_dsml_glued_invoke`) срезает маркер.
  - `_repair_dsml_bare_attributes` принимал легитимный `parameter="X"` за голый
    атрибут с ключом `parameter` и переписывал тег в `parameter name="parameter"...`,
    стирая имя аргумента. Теперь ключ `parameter` пропускается, как `arguments`.
  - Репро на живом логе: `parse_tool_calls` давал NONE; после фикса — 2 вызова `Edit`
    с чистыми `path`/`old_string`/`new_string`.
  - Тот же маркер встречается и в трёх разных написаниях opener'а в одном прогоне
    (с ведущим `<`, с ведущим `</`, вообще без скобки) — tooltrace `7b1bb070`,
    Read на 3 аргумента. `_repair_dsml_glued_param` расширен: ведущая скобка
    опциональна, все три нормализуются в канонический opener.
  - Тесты: `tests/test_tools.py::test_dsml_param_eq_opener_glued_marker_before_gt`
    и `::test_dsml_param_glued_marker_mixed_brackets_one_run`.
  - Верификация: `ruff check` + `ruff format --check` + `make check` (arch-check OK)
    + `pytest -q` → **1352 passed**. Плейбук обновлён (`docs/tooltrace-playbook.md`).

- **Qwen: ротация refresh-токена теперь сохраняется + здоровье аккаунта видно.**
  Диагноз подтверждён по логу: refresh-токены Qwen «протухают» за ~5.5 ч вместо
  30 дней, потому что сервер **изредка ротирует** refresh-токен при обмене, а
  DanyAPI читал из ответа только `access_token` — новый `refresh_token` терялся.
  Старый работал ещё в grace-окне, затем Qwen отвечал `Token is revoked`.
  - **Железное доказательство** (лог `danyapi3`, 2026-10-07 15:21:02):
    `qwen refresh_token was ROTATED by the server (old=... jti=9e938906
    new=... jti=83c115de ...); the configured value is now stale`.
    Через 39 мин (16:00) старый `9e938906` отозван — `Token is revoked`.
    Это был частичный патч в `DanyAPI3`, который ротацию только **логировал**.
  - **Ротация редкая, не на каждый вызов.** 6 обменов подряд свежим токеном
    вернули **тот же** `jti` побайтово — так что воспроизводить её «в лоб»
    бесполезно; триггер, вероятно, окно/возраст токена. Живой прогон нового
    кода: `POST /v1/chat/completions` qwen3.8-max → HTTP 200, `pong` за 7.3 с;
    стор `qwen-credentials` пуст (ротации в этом прогоне не случилось — норма).
  - `danyapi/qwen/client.py` — `refresh_access_token` вызывает новый
    `_adopt_refresh_token(biz.get("refresh_token"))`: при смене значения логирует
    `ROTATED (old=... new=...)` через `_token_fingerprint` (type+jti+exp, без
    секрета) и зовёт колбэк `token_persist`. Логи успеха/отказа приведены к
    единому формату; отказ читает `x-actual-status-code` (auth-хост отвечает 200
    даже при отказе).
  - `danyapi/qwen/accounts.py` — `load_effective_token` / `persist_rotated_token`
    поверх `JsonStore("qwen-credentials")`, привязка к SHA-256 исходного
    `.env`-токена (правка `.env` перекрывает сохранённый).
  - `danyapi/api/openai.py` — читает эффективный токен при старте и передаёт
    `token_persist` в `QwenClient`; создан `qwen-credentials` стор.
  - `danyapi/qwen/api.py` — `_send_completion` теперь принимает `account` и ловит
    `QwenError` → `_handle_account_error` + 401. Раньше исключение летело наружу
    как голый 500, `mark_broken()` не звался, и `/health` показывал
    `healthy: 1` при мёртвом токене (мониторинг-бот был слеп).
  - Тесты: `test_qwen_client.py` (+4 ротация), `test_qwen_credentials.py` (новый,
    +9), `test_qwen_api.py` (+2 QwenError→mark_broken/502, обновлены `_send` и
    http-error тесты под новую сигнатуру).
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → **1350 passed** (`qwen/client.py` — 100%).
  - Деплой: 4 файла + тесты синхронизированы в `DanyAPI3` и на плату
    (бэкапы `.qwen-rotation-bak-*`), кэши старого аккаунта убраны, сервис
    перезапущен (PID 92778). Лог подтверждает новый формат:
    `qwen refresh refused (Unauthorized): credential=type=refresh_token
    jti=9e938906 ... actual=401`, `/health` честно отдаёт `qwen: False`.
  - **Осталось:** вставить свежий refresh-токен и убедиться, что при следующей
    ротации новый `jti` реально сохраняется в `qwen-credentials` и переживает
    рестарт.

- **DeepSeek: гонка сети на старте + оживление отвалившихся токенов.**
  Симптом: при ребуте платы eth0 поднимается на ~4 с позже старта юнита,
  `check_auth()` уходил в сеть, получал транспортную ошибку и токен
  отбрасывался как «мёртвый» → `deepseek accounts ready: 0` до ручного
  перезапуска. Причина была двойная.
  - **Транспортная ошибка выглядела как мёртвый токен.**
    `danyapi/deepseek/client.py`: `check_auth()` возвращал bool, ловя
    `httpx.HTTPError` → `False`. Теперь возвращает enum `AuthCheck`
    (`OK` / `REJECTED` / `UNREACHABLE`); битый JSON тоже `UNREACHABLE`.
  - **Auth-ошибка убивала аккаунт навсегда.** `danyapi/accounts.py`:
    `mark_broken()` был необратим. Добавлены `broken_at`/`last_revive_at`,
    новый `revive()`; `is_available()` по-прежнему `not broken`, но флаг
    снимается после успешной перепроверки.
  - **Старт** (`danyapi/api/openai.py`): `REJECTED` больше не выбрасывается —
    аккаунт остаётся в пуле как broken; `UNREACHABLE` даёт рабочий аккаунт
    (оптимистично) — это и чинит гонку. Следствие: если все токены отвергнуты,
    сервер СТАРТУЕТ, а не падает с «no valid credentials».
  - **Фоновый цикл** `_revive_sweep`/`_revive_broken_accounts`: broken-аккаунт
    перепроверяется каждые `DANYAPI_DEEPSEEK_REVIVE_INTERVAL` (900 с); лежащий
    дольше `DANYAPI_DEEPSEEK_REVIVE_SLOW_AFTER` (7200 с) — раз в
    `DANYAPI_DEEPSEEK_REVIVE_SLOW_INTERVAL` (1800 с). Минимум 60, мусор → дефолт.
    Запускается в lifespan, отменяется в finally.
  - **Плата:** drop-in `deploy/systemd/danyapi3.service.d/override.conf` с
    `ExecStartPre` — ждёт реального DNS до 60 с (`exit 0` в любом случае).
    Установлен в `/etc/systemd/system/danyapi3.service.d/`.
  - Тесты: `test_deepseek_client.py` (enum, 4 случая), `test_accounts.py`
    (+5), `test_config.py` (+4), новый `test_revive.py` (8), `test_api_helpers.py`
    (`_patch_creds` мокает `AuthCheck.OK`; REJECTED/UNREACHABLE-старт).
    Верификация: `ruff check` + `ruff format --check` + `make check` + `pytest -q`
    → 1336 passed (во всех трёх репозиториях), py3.10 AST OK.
    На плате 6660: `accounts ready: 1`, живой запрос 200; 8000 — живой запрос 200.

- **Парсер DSML: три паттерна коверканья + защита от вырожденного повтора.**
  Из разбора `tooltrace.ndjson` (DanyAPI + DanyAPI2, ~14k upstream-событий).
  - **Вклеенный маркер в invoke-opener**: `invoke name="X"` + лишний DSML-маркер + `>`
    вместо канонического `invoke name="X">`. `_DSML_INVOKE_RE` такое не видит → блок
    терялся. Нормализует `_repair_dsml_glued_invoke`.
  - **Голый `arguments="<json>"`**: вместо `parameter name=...` на каждый аргумент —
    один тег с атрибутом `arguments` и escaped-JSON значением. Переписывает
    `_repair_dsml_bare_arguments`, а `_unwrap_nested_arguments` разворачивает обёртку.
  - **Голые `key="value"`-атрибуты**: аргументы схлопнуты в голые атрибуты
    (`new_string="..."`, `parameter name="key="...`). Парсилось с пустыми аргументами →
    `missing_required` → repair-цикл до лимита. Нормализует `_repair_dsml_bare_attributes`.
  - **Защита от вырожденного повтора** (`degenerate_repeat`): на длинном многозаходном
    ходу DeepSeek теряет EOS и повторяет пустой контейнер `calls>` сотни раз до лимита.
    Детектор `is_degenerate_repeat`/`degenerate_repeat_count` (прогон подряд пустых
    контейнеров, порог 20; свип 53/60, 0 ложных на 14k). Стрим обрывается + `stop_stream`.
    Env: `DANYAPI_DEGENERATE_GUARD` (on), `DANYAPI_DEGENERATE_THRESHOLD` (20). Видно в
    сводке `merge_traces.py` отдельным блоком. Ограничение: сэмплинг веб-API DeepSeek
    не настраивается, это борьба с последствием, не с причиной.
  - Тесты: `test_tools.py` (+9), `test_config.py` (+3), `test_retry.py` (+1). README,
    `docs/configuration.html`, `docs/i18n.js`, `.env.example`, playbook обновлены.
    Верификация: `ruff check` + `ruff format --check` + `arch-check` + `pytest -q` → 1318 passed.

- **Qwen: причина отказа токена теперь видна.** Старт молча выкидывал отклонённый
  credential и отдавал наружу только `qwen provider is not configured`, так что
  отозванный refresh-токен выглядел как «протух за пару часов».
  - `danyapi/qwen/client.py` — `QwenClient` запоминает причину последнего сбоя
    (`_last_error`, property `last_auth_error`); успех её очищает.
  - `danyapi/api/openai.py` — лог отказа токена содержит причину, при отказе всех
    токенов пишется `log.error` и `app.state.qwen_disabled_reason`; 503 для chat и
    images добавляет причину к тексту.
  - Тесты: `tests/test_qwen_client.py` (+8), `tests/test_api_helpers.py`,
    `tests/test_images_api.py`.
- **Миграция Qwen на фронтенд 0.3.12 (новая схема авторизации).** Сделана как
  отдельная фича после отката; схема разобрана вживую и проверена E2E.
  - Суть: `0.3.12` больше не принимает статичный токен как bearer. Рабочий
    `access_token` (~15 мин) получают обменом credential'а через
    `GET https://auth.qwen.ai/api/v2/auths/refresh`. Отдельный auth-хост,
    пути без `/api/v1`.
  - `danyapi/qwen/client.py` — `WEB_VERSION` → `0.3.12`; добавлены
    `AUTH_URL`, `jwt_claims()`, `token_is_expired()`, `new_auth_client()`,
    `refresh_access_token()`, `ensure_access_token()`. Credential уходит
    cookie с именем по типу токена (`refresh_token` для refresh-токена,
    `token` для legacy) — `_source_cookie_name`. Обязательные заголовки
    `timezone` (иначе `Invalid request header`) и
    `x-request-origin` (иначе `Missing origin`). `_post`/`_get` идут через
    новый `_send`: refresh по `exp` с запасом 60 с и ретрай один раз на 401;
    `completion()` (стрим) делает то же для `build_request`.
  - `login()` переведён на `auth.qwen.ai` и принимает
    `refresh_token`/`access_token`/`token`.
  - `docs/setup.py`, `docs/internals.html`, `docs/i18n.js` — версия и описание
    auth-потока обновлены.
  - Тесты: `tests/test_qwen_client.py` переписан под новую схему (+33 кейса) —
    refresh, имя cookie по типу, обязательные заголовки, ретрай на 401,
    single-flight по лок-ре-чеку; `qwen/client.py` — 100%.
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → 1290 passed. E2E вживую с refresh-токеном: chat создан,
    `POST /api/v2/chat/completions` → 200, `pong` получен.
  - **Внимание по credential'ам:** legacy-токены из `.env` дают 200 на
    `0.2.83`, но как источник refresh на `0.3.12` отвечают
    `Token is revoked`. Нужен именно `refresh_token`. Подробности — в
    «Провал при актуализации» ниже.
- **Актуализация трёх веб-фронтов (deepseek / qwen / z.ai).** Сверено вживую
  (curl + Playwright, включая скриншоты UI z.ai);
  - `danyapi/deepseek/client.py` — `x-client-version` `2.3.0` → `2.5.0`
    (текущее значение шлёт сам chat.deepseek.com).
  - `danyapi/qwen/client.py` — `WEB_VERSION` был поднят до `0.3.12` без
    проверки токенов и откачен, затем переведён по-настоящему (см. выше).
  - `danyapi/zai/browser.py` — новый `_dismiss_modals()`: онбординг/«model
    upgraded» модалка z.ai рисует полноэкранный бэкдроп выше композера и
    глотает клики (submit молча не срабатывает), при этом накрывает собственную
    кнопку закрытия — поэтому Escape, а при неудаче удаление узла оверлея
    (`div[data-dialog-overlay], ._modal-overlay`). Вызывается в `start()`,
    `_select_model()` и перед каждым `fill`+`click` в `complete()` /
    `complete_stream()` (модель может быть уже выбрана — тогда навигации нет).
  - `docs/setup.py`, `docs/internals.html`, `docs/i18n.js` — версии приведены
    в соответствие.
  - z.ai: `GLM-5.2` в реестр **не** добавлялся (по решению владельца);
    `ZAI_BROWSER_DIR` не трогался — сеанс z.ai предполагается один.
  - Тесты: `tests/test_zai_browser_coverage.py` — 7 кейсов на `_dismiss_modals`
    (нет страницы, нет оверлея, Escape сработал, откат к удалению узлов, сбой
    подсчёта, сбой Escape, сбой удаления); `zai/browser.py` — 100%.
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` + `pytest -q`
    → 1265 passed. E2E вживую: `ZaiBrowser.complete()` на копии профиля вернул
    `pong` (usage 35), в логе `zai modal detected (1 overlay(s)), dismissing`.
- **Картинки Qwen: `size` уходит на верхний уровень тела запроса.** Прежний код
  клал соотношение в `extra.meta.size`, и Qwen его игнорировал: на `2:3`
  возвращался квадрат 2048×2048. Проверено вживую — то же значение на верхнем
  уровне, `body["size"]`, даёт запрошенные 1696×2528. `meta["size"]` убран.
  Тесты: `tests/test_qwen_client.py` — `test_completion_puts_image_size_at_top_level`
  (значение на верхнем уровне, в `meta` его нет) и
  `test_completion_omits_size_when_unset` (поле не появляется, когда размер не
  задан). Верификация: `ruff check` + `ruff format --check` + `pytest -q` →
  1258 passed, `arch-check` OK. Коммит `6551b05`. Живой процесс на порту 8000 не
  перезапускался: правка вступит в силу после рестарта.
- **`deepseek-v4-pro` (expert) принимает изображения.** Прежде expert отклонял
  любые вложения (`deepseek-v4-pro does not support file attachments`), теперь
  принимает картинки — та же проверка, что у vision. Добавлен
  `MODEL_NAME_BY_TYPE` (`danyapi/api/models.py`), имя модели в тексте ошибки
  берётся из него, а не хардкодится. Тесты: `tests/test_retry.py` —
  `test_pro_rejects_all_files` переименован в `test_pro_rejects_text_files`,
  добавлен `test_pro_accepts_images`. Верификация: `ruff check` +
  `ruff format --check` + `pytest -q` → 1258 passed, `arch-check` OK. Коммит
  `059dfed`.
- **Соотношения 2:3 и 3:2 для картинок Qwen (пины Pinterest).** `QWEN_IMAGE_RATIOS`
  (`danyapi/api/openai.py`) знал только `1:1`, `3:4`, `4:3`, `16:9`, `9:16`,
  поэтому запрос `size=1024x1536` (2:3) уезжал в ближайшее `3:4`: модель рисовала
  3:4, а `_resize_image_bytes` затем растягивал результат до ровных 1024×1536 —
  картинку сжимало по горизонтали и вытягивало по вертикали. Добавлены `2:3` и
  `3:2`, теперь 2:3 остаётся 2:3 и локальный resize пропорций не ломает. Тесты:
  `tests/test_image_size.py` — новые кейсы в таблице `_aspect_ratio` (200×300 →
  `2:3`, 300×200 → `3:2`, 1024×1536 → `2:3`, 1536×1024 → `3:2`; прежде 300×200 и
  200×300 ожидали `4:3`/`3:4`) плюс `test_aspect_ratio_keeps_two_by_three_for_pin_size`.
  Доки: `docs/usage.html` (`ug_images_size`), `docs/i18n.js`. Верификация:
  `ruff check` + `ruff format --check` + `pytest -q` → 1255 passed. Живой процесс
  на порту 8000 не перезапускался: правка вступит в силу после рестарта.
- **Автовыбор thinking по сложности хода (тесты + верификация).** Сама эвристика
  уже лежала в рабочей копии: `_looks_hard`/`_thinking_text`/`_resolve_thinking`
  (`danyapi/api/openai.py:504-554`) и настройки `thinking_heuristic`,
  `thinking_min_prompt_chars` (2000), `thinking_min_messages` (8)
  (`danyapi/config.py:162-176`, env `DANYAPI_THINKING_HEURISTIC`,
  `DANYAPI_THINKING_MIN_PROMPT_CHARS`, `DANYAPI_THINKING_MIN_MESSAGES`; off по
  умолчанию). Приоритет прежний: явное `thinking` → `reasoning_effort` →
  эвристика (если включена) → дефолт провайдера; формат
  `/v1/chat/completions` не менялся.

  **Правило переработано (сигналы складываются в счёт).** Изначально
  `_looks_hard` был набором OR-условий, и одно из них — голый `tools` — делало
  фичу бесполезной для агентных клиентов: Kimi Code CLI шлёт `tools` почти на
  каждом ходу (61% трафика — шаги tool-раунда), поэтому thinking включался
  всегда. Плюс `tool_choice` не различал режимы: `"none"` (инструменты
  запрещены) и `"required"` (вызов принудительный, решения нет) тоже давали
  «думать». Теперь сигналы дают очки: сильный (длинный промпт ИЛИ
  ошибка/стектрейс/ключевое слово) = 2, слабый (длинная история, авто-tool
  раунд) = 1, порог = 2. Голый `tools` больше не сигнал; засчитывается только
  `tools` + `tool_choice` `auto`/не задан, и это слабый сигнал, который сам по
  себе порога не берёт. `tool_choice` `none`/`required`/именованная функция —
  ноль. Хелпер `_tool_signal`. Веса и порог вынесены в env (см. ниже), а не
  зашиты константами.

  **Веса и порог — через env.** `thinking_strong_weight` /
  `thinking_weak_weight` / `thinking_score_threshold` в `Settings`
  (`danyapi/config.py`), читаются хелпером `_int_env` (нечисловое значение →
  дефолт, значение ниже 1 → clamp). Env: `DANYAPI_THINKING_STRONG_WEIGHT` (2),
  `DANYAPI_THINKING_WEAK_WEIGHT` (1), `DANYAPI_THINKING_SCORE_THRESHOLD` (2).
  `_looks_hard`/`_tool_signal` берут их из `settings`, поэтому баланс крутится
  без правки кода. Задокументированы в `.env.example`.

  Закрыта тестовая таблица из BACKLOG: новый
  `tests/test_thinking_heuristic.py` (33 теста) — явные поля перебивают
  эвристику в обе стороны, `reasoning_effort="off"`/`"high"`, сильные сигналы
  поодиночке (длина, ошибка/стектрейс, англ. и рус. ключевые слова), слабые
  поодиночке НЕ включают thinking (голый `tools`, `auto`, длинная история), все
  режимы `tool_choice` (`none`/`required`/именованная/`auto` без `tools`),
  комбинации слабых сигналов дают thinking, сигналы «не думать» (короткий/
  простой промпт, пороги на единицу ниже), просматривается только последний
  user-ход, мультимодальный/не-текстовый контент, независимость от
  `search`-гейта, регрессия при выключенном флаге. Плюс 4 route-level E2E
  через `_chat_completions_deepseek`
  (стаб `_collect_non_stream`, как в `tests/test_retry.py`): простой и сложный
  ход без `thinking` дают `thinking=False`/`True`, выключенный флаг — дефолт
  провайдера, явное `thinking` по-прежнему главнее. `tests/test_config.py`
  (+10: флаг off/on-варианты, дефолты порогов и весов, чтение env, фолбэк на
  битых значениях, floor=1). Верификация: `ruff check` + `ruff format --check` +
  `pytest -q` → **1252 passed**, `arch-check` OK; покрытие
  `danyapi/config.py` 100%, `danyapi/api/openai.py` 99% (оставшиеся 4
  непокрытые строки — не эта фича).

- **Retry на нераспарсенный tool-call (parse-fail), DeepSeek.** Раньше при
  `parse_tool_calls → None` Dany отдавал сырой текст как обычный ответ; существующий
  `_repair_tool_calls` срабатывал только когда парсинг прошёл, а валидация упала.
  Теперь parse-фейл тоже ретраится один раз: `_repair_unparsed`
  (`danyapi/api/streaming.py`) под гейтом `should_retry_unparsed` — `tool_choice`
  `required`/именованная функция ретраит всегда; `auto`/не задан — только при
  `DANYAPI_TOOLCALL_RETRY=1` **и** вердикте классификатора `json_malformed`; обычная
  проза (`no_tool_call`) не ретраится никогда. Промпт parse-фейла —
  `build_unparsed_repair_prompt` (`danyapi/tools/validate.py`). Общий транспорт
  обоих ретраев вынесен в `_retry_tool_call`; успешный parse-fail-ретрай помечается
  `continuation`-событием в tooltrace. Проведено во все 4 сайта парсинга DeepSeek
  (non-stream, stream, оба compact-пути). Env-флаг off по умолчанию. Тесты:
  `tests/test_tools_api.py` (+6), `tests/test_validate.py` (+2),
  `tests/test_config.py` (+2). Верификация: `ruff check` + `ruff format --check` +
  `pytest -q` → **1204 passed**, `arch-check` OK.

- **Парсер: sibling-вызовы без разделителя в param-only блоке (сплит по схеме).**
  Блок DSML без `<invoke>` (только `<parameter>`) склеивал параметры **разных**
  инструментов в один прогон, причём sibling-вызовы не разделены ничем, кроме
  собственного close-тега каждого параметра, а имена аргументов уникальны: Bash
  (`command`/`cwd`/`description`/`run_in_background`/`timeout`/`disable_timeout`),
  затем Glob/Grep (`path`/`offset`/`head_limit`/`glob`/`type`/`-n`/…). Ни сплит по
  повтору имени, ни сплит по голому тегу границу не видели; `_infer_tool_from_params`
  требует все ключи в одной схеме → ни одна не подходит → весь блок терялся как
  `unparsed:no_tool_call` (tooltrace `9cf75302`, `59721714`, `be51bb6e`). Теперь
  `_parse_dsml_param_only` разбивает прогон, когда очередной параметр оставляет
  группу без ни одной совместимой схемы (helper `_schema_compatible_tools`).
  Из лога `9cf75302` даёт 2 вызова (Bash + Grep) вместо нуля. Тесты:
  `tests/test_tools.py` (+1, реальный пример). Playbook дополнен. Верификация:
  `ruff check` + `ruff format --check` + `pytest -q` → **1189 passed**, `arch-check` OK.

- **Python 3.10: `except asyncio.TimeoutError` вместо встроенного `TimeoutError`.**
  На плате (Ubuntu 22.04) Python 3.10, где `asyncio.TimeoutError` — отдельный класс,
  а не алиас встроенного `TimeoutError` (унификация только с 3.11). Перехват
  встроенного потому не ловил таймауты `asyncio.wait_for` в трёх местах:
  `accounts.py` (acquire пула с `max_wait`), `zai/api.py` (браузерный complete → 504)
  и `zai/browser.py` (стрим-поллинг не прерывался по дедлайну). Переведено на
  `asyncio.TimeoutError` с `# noqa: UP041` — ruff считает его алиасом, но на 3.10
  это не так. Тесты не трогались: `ruff check` + `ruff format --check` +
  `pytest -q` → **1186 passed**, `arch-check` OK. Те же правки вручную внесены в
  копию на плате.

- **Парсер: sibling-вызовы в param-only блоке, разделённые голым тегом.**
  Блок DSML без `<invoke>` (только `<parameter>`) может кодировать **несколько
  разных** вызовов, разделённых отдельной строкой ` ` / ` `
  без слова (`parameter`/`calls`/`invoke`). Существующий сплиттер делил группы
  только по **повтору имени** параметра, а у sibling-вызовов имена аргументов не
  пересекаются (Grep: `-n`/`output_mode`/`pattern`; Read: `line_offset`/`n_lines`/
  `path`), поэтому граница не распознавалась: все аргументы склеивались в одну
  группу, ни одна схема не подходила → `unparsed:no_tool_call` (tooltrace
  `0885f2d2`, `8b1c632c`, `0241e65f`). Теперь `_parse_dsml_param_only` считает
  вторым DSML-тегом в регионе между соседними параметрами границу вызова. Из
  лога `0885f2d2` даёт 3 корректных вызова (Read + Grep + Grep), `8b1c632c` и
  `0241e65f` — по 2 (Grep + Read). Тесты: `tests/test_tools.py` (+2, реальные
  примеры). Playbook дополнен. Верификация: `ruff check` + `ruff format --check` +
  `pytest -q` → **1185 passed**, `arch-check` OK.

- **Парсер: имя параметра в атрибуте `parameter="X"`.** DeepSeek иногда отдаёт
  opener без слова `name=` — `parameter="command"` вместо `parameter
  name="command"` — а значение несёт висячую кавычку (`sleep 120"`).
  `_DSML_PARAM_OPENER_RE` требует `parameter name=`, поэтому такие теги были
  невидимы всем DSML-веткам и блок терялся (tooltrace `32629734`). Добавлен
  `_DSML_PARAM_EQ_OPENER_RE` (отдельный паттерн, канонический не тронут),
  `_parse_dsml_param_only` сканирует оба вида; висячая кавычка срезается только у
  нового варианта. Тесты: `tests/test_tools.py` (+3). Playbook дополнен.
  Верификация: `ruff check` + `ruff format --check` + `pytest -q` → **1170 passed**,
  `arch-check` OK.

- **Парсер: повтор имени параметра = несколько вызовов + аргументы в close-теге.**
  Блок DSML без `<invoke>` (только `<parameter>`) может кодировать **несколько**
  вызовов: DeepSeek повторяет имя аргумента по разу на вызов (tooltrace `24b809c9`:
  4×`parameter name="path"` = 4 чтения Read; `f4275506`: 2×`parameter name="command"`
  = 2 вызова Bash). Старый `_parse_dsml_param_only` схлопывал весь прогон в один
  dict — побеждало последнее значение, а одиночный `{path}` ещё и не давал
  однозначно определить инструмент (Read/Edit/Write) → `no_tool_call`. Теперь
  прогон разбивается по повтору имени, а хвостовые аргументы, влитые в закрывающий
  тег (хвост `line_offset=170 n_lines=18` внутри close-тега), собирает `_dsml_close_tag_attrs`.
  Имя инструмента резолвится на каждый вызов; неоднозначные вызовы наследуют
  единственное разрешённое имя блока, если схема принимает их ключи. Из лога
  `24b809c9` теперь даёт 4 корректных Read-вызова, `f4275506` — 2 Bash. Тесты:
  `tests/test_tools.py` (+4). Верификация: `ruff check` + `ruff format --check` +
  `pytest -q` → **1162 passed**, `arch-check` OK.

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

- **Изоляция дискового кэша между инстансами (45 повторных загрузок картинок).**
  Симптом: после рестарта DanyAPI2 на 9000 «продолженный» диалог заново загрузил
  всю историю изображений — 45 загрузок за 26 секунд на один запрос.
  - **Корневая причина:** обе копии (DanyAPI на 8000 и DanyAPI2 на 9000) писали
    кэш в один каталог `/tmp/danyapi`. `JsonStore._write` перезаписывает файл
    целиком, поэтому работает «кто последний, тот и прав»: один процесс молча
    теряет сессии и отпечатки контекста другого. Каждый ход промахивался мимо
    upstream-чата, диалог пересобирался с нуля и история уходила на повторную
    загрузку.
  - **`danyapi/store.py`:** `default_cache_subdir()` → `danyapi-<settings.port>`;
    `cache_root()` использует его вместо общего `danyapi`. Явный
    `DANYAPI_CACHE_DIR` по-прежнему имеет приоритет.
  - **Тесты:** `tests/test_store.py` — `test_cache_root_falls_back_to_temp`
    изолирован через `monkeypatch` на `tempfile.gettempdir`; новые
    `test_cache_root_is_per_instance` (8000 ≠ 9000) и
    `test_cache_root_override_wins_over_port`. `tests/conftest.py` — autouse
    фикстура `isolated_cache` уводит `settings.cache_dir` в `tmp_path`, иначе
    тесты (usage-трекер, lifespan через `TestClient`) писали в боевой кэш.
  - **Доки:** `.env.example`, `docs/configuration.html`, `docs/i18n.js` (ru+en).
  - Верификация: `ruff check` + `ruff format --check` + `arch-check` +
    `pytest -q` → **1369 passed**; после прогона каталоги в `/tmp` не создаются.
    Синхронизировано в `DanyAPI2` (`store.py` + оба тест-файла), там тоже
    1369 passed.
  - Живые инстансы 8000/9000 перезапущены и уже пишут в свои каталоги
    `/tmp/danyapi-8000` и `/tmp/danyapi-9000`.
  - **Не сделано:** изменения не закоммичены. Кэш-состояние обоих инстансов при
    проверке пересоздавалось (каталоги удалялись, чтобы поймать утечку в тестах),
    сессии/контексты в кэше потеряны — на данные это не влияет.

- **Пустой ответ DeepSeek больше не уходит клиенту как успех (зависание Kimi на
  цензуре).** Симптом: `ReadMediaFile` показал изображение, DeepSeek его
  отклонил, Kimi Code завис — в логе DanyAPI2 (порт 9000) 153 загрузки одной и
  той же картинки, интервалы 13→20→40 с.
  - **Корневая причина 1:** отказ приходит обычным `toast` без
    `type: "error"` («Content violates terms. Message not sent.»), а
    `MessageReconstructor.handle` сохранял только `type == "error"` → текст
    терялся, `hint_error` пуст.
  - **Корневая причина 2:** пустой ответ (ни `content`, ни `reasoning`, ни
    `hint_error`) отдавался как обычный `finish_reason: "stop"` с пустым
    `content` (non-stream 200 / stream `stop`). Клиент видел «ничего не
    сгенерировано» и повторял запрос — отсюда бесконечный цикл, ретраи
    прокси тут ни при чём (апстрим отвечал за 0.5 с).
  - `deepseek/stream.py`: новый `MessageReconstructor.notice` — любой
    toast/hint с текстом сохраняется независимо от `type`; `extend_with`
    его переносит.
  - `retry.py`: `blank_answer_reason(rec)` (текст отказа → `CONTENT_FILTER` →
    generic «empty response») и `blank_answer_finish_reason(rec)`
    (`content_filter` / reason из hint / `error`).
  - `api/streaming.py`: guard в обоих путях — non-stream → `HTTPException(502,
    reason)`, stream → SSE-чанк с `error.message` и `finish_reason`, затем
    `[DONE]`. Пустой ответ никогда не считается успехом; отказ не ретраится.
  - Тесты: `tests/test_retry.py` +6 (`REFUSAL_NOTICE_SSE`,
    `CONTENT_FILTER_STATUS_SSE`, `EMPTY_SSE`). Негативная проверка: без фикса
    5 из 6 падают.
  - Верификация: `ruff check` + `ruff format --check` + `pytest -q` →
    **1367 passed**, `arch-check` OK. Синхронизировано в `DanyAPI2`
    (вместе с search-фиксом; там тоже 1367 passed). Живой прогон на 9001:
    non-stream `pong` 200, stream 11 чанков + `[DONE]`.
  - **Не сделано:** ваш процесс на 9000 не перезапускался — фикс вступит в
    силу после рестарта DanyAPI2. E2E на реально отклонённом изображении не
    воспроизводился (не открывал картинку); отказ покрыт SSE-фикстурами из
    инцидента.

- DeepSeek web search разрешён для `deepseek-v4-pro`: гейт в
  `api/openai.py` (`_chat_completions_deepseek`) заменён с `model_type == "default"`
  на `model_type in SEARCH_CAPABLE_MODEL_TYPES` (новый кортеж в `api/models.py`:
  `default`, `expert`; `vision` без поиска). Обновлены `tests/test_retry.py`
  (pro теперь `search=True`), `README.md`, `docs/overview.html`,
  `docs/usage.html`, `docs/i18n.js`. Верификация: `ruff check` +
  `ruff format --check` + `pytest -q` → 1361 passed, `arch-check` OK.
  E2E вживую: non-stream (`search: true` + `deepseek-v4-pro` → 3 свежих
  заголовка HN со ссылками, `finish_reason: stop`) и stream (703 `data:`-чанка,
  маркеры `[citation:N]`, `[DONE]`) на порту 8765.
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

## Провал при актуализации (2026-10-05)

Задача «актуализировать версии фронтов» была выполнена **частично неверно**.

**Что сделано неправильно.** `WEB_VERSION` для Qwen был поднят с `0.2.83` до
`0.3.12` на основании только того, что живой фронт шлёт именно это значение.
Не была проведена проверка «а работают ли наши токены с новой версией». Они не
работают: с `0.3.12` Qwen отдаёт `401 Unauthorized`. Из-за этого Qwen был
сломан. Ошибка обнаружилась по факту («токены протухли?») и была
диагностирована.

**Что проверено теперь.** Один и тот же живой токен, меняется только заголовок
`version`: `0.2.99` → 200, `0.3.0` → 401. Граница между `0.2.99` и `0.3.0`.
Живые токены (legacy 209 б, refresh 297 б) на `0.3.12` дают 401 оба. Дело не в
токенах — `0.3.12` требует новой схемы авторизации.

**Как исправлено:** сначала `WEB_VERSION` возвращён на `0.2.83`, затем сделана
настоящая миграция на `0.3.12` (см. «Готово» выше) — с разбором новой схемы
и E2E-проверкой. DeepSeek `2.5.0` оставлен — проверено, что работает и с
`2.3.0`, и с `2.5.0`.

**Что важно знать про credential'ы.** Legacy-токены из `.env` на `0.2.83`
дают `200`, но как источник refresh на `0.3.12` отвечают `Token is revoked`.
Рабочий источник — `refresh_token` (cookie `refresh_token`, ~30 дней).
Его и нужно класть в `QWEN_TOKENS`.

**Урок:** подстановка значения, подсмотренного у живого фронта, — это гипотеза,
а не факт. Критерий «фронт так делает» недостаточен; нужна проверка «наш токен
с этим значением проходит аутентификацию» ДО коммита.

## Известные проблемы (см. BACKLOG.md)

- Полный CI-гейт `pytest --cov-fail-under=100` падает: покрытие ~97% — дефицит
  рефакторинга в `api/streaming.py` (~102 строки), `tools/parse.py` (~52) и др.,
  не относится к текущим фиксам (локальный `pytest -q` → 1252 passed).
  Пять coverage-сюит (`tests/test_*_coverage.py`) подняли покрытие с 94% до 97%.
- `ruff format --check` ранее падал на `scripts/smoke_tooltrace.py` и
  `tests/test_tooltrace.py`; оба отформатированы, сейчас формат-гейт зелёный.

## Текущий commit

Копии `DanyAPI2` и `DanyAPI3` — без `.git`; при синхронизации кода их надо
приводить к `DanyAPI` вручную (`.env` у каждого свой и не трогается).

Состояние на `65bb91a`: все три копии по коду идентичны (`danyapi/`, `tests/`,
`docs/`, `scripts/`, `deploy/`, корневые файлы). Различается только `.env`
(порт 8000 / 9000 / 6660 и, у `DanyAPI3`, явный `DANYAPI_CACHE_DIR=/tmp/danyapi3`)
и наличие `.venv`. Проверка — `diff -rq` без `.venv`/`.git`/`.env`; тесты
1369 passed в каждой.

Последний коммит: см. `git log -1`. До него: `fix(qwen): revert WEB_VERSION to 0.2.83`
и `chore(providers): sync web client versions with the live frontends`;
ранее `feat(images): accept image attachments for deepseek-v4-pro (expert)`
(`059dfed`), `fix(images): send Qwen image size at the top level of the body`
(`6551b05`), `fix(images): accept 2:3 and 3:2 aspect ratios for Qwen` (`f98dc51`).

Следующее: добить покрытие до 100% для CI-гейта — начать с `api/streaming.py`
(см. «Известные проблемы»).
