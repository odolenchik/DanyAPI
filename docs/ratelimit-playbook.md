# Rate-limit Playbook (для агента)

> Инструкция для агента. Когда пользователь говорит «словил too_frequent» /
> «аккаунт опять заблокировался» / «вот строка из лога» — читай этот файл и действуй
> по шагам, не переоткрывая причину заново.

## Что это такое

DeepSeek блокирует аккаунт за слишком частые запросы. Признак приходит **не HTTP-статусом**,
а SSE-событием `hint` с текстом `Messages too frequent. Try again later.` Пишется в
`hint_error` реконструктора (`danyapi/deepseek/stream.py`).

**`finish_reason` у этого хинта — `rate_limit_reached`** (проверено на живом инциденте
2026-09-12). Ранее предполагалось `too_frequent`; в лог событие тогда попало только потому,
что `_is_rate_limit_hint` сверяет ещё и текст сообщения. **Не убирай текстовые маркеры** —
на одном `finish_reason` детект не держится.

В коде это **намеренно не ретраится**. Блокировка длится минуты, поэтому 5 повторов с
бэкоффом 1→2→4→8 с (23 с суммарно) не помогут, а лишние обращения к уже заблокированному
аккаунту могут продлить блокировку. Вместо ретраев работает **пейсинг** (см. ниже).

Реализация:

| что | где |
|---|---|
| `RATE_LIMIT_HINT_REASONS` / `RATE_LIMIT_HINT_MARKERS` | `danyapi/retry.py` |
| `_is_rate_limit_hint()` / `_log_rate_limit_hint()` | `danyapi/retry.py` |
| `START_WINDOWS` / `START_BUFFER_SEC` | `danyapi/retry.py` |
| `DANYAPI_MIN_REQUEST_INTERVAL` | `danyapi/config.py` → `settings.min_request_interval` |
| `DANYAPI_MIN_END_GAP` | `danyapi/config.py` → `settings.min_end_gap` |
| `wait_turn()` / `seconds_until_ready()` / `note_request_start()` / `note_request_end()` | `danyapi/accounts.py` (`DeepSeekAccount`) |
| точка пейсинга | `danyapi/api/deepseek.py` → `_send_with_auth` (старт), `danyapi/accounts.py` → `account_lock` (конец) |
| вызовы лога | `danyapi/api/streaming.py` (non-stream, stream), `danyapi/api/deepseek.py` (continuation) |

## Пейсинг (что уже сделано)

Пейсинг считает **два** дедлайна и берёт поздний из них:

- `DANYAPI_MIN_REQUEST_INTERVAL` (дефолт **4.0** с, `0` = выключено) — минимальный
  интервал между **стартами** запросов на один аккаунт. Живёт в `_send_with_auth`.
- `DANYAPI_MIN_END_GAP` (дефолт **5.0** с, `0` = выключено) — минимальный интервал между
  **концом** предыдущего запроса и **стартом** следующего. Отмечается в `account_lock`
  (`note_request_end()`), то есть в момент, когда лок отпущен, а тело ответа вычитано.

Один только старт-старт интервал пропускает главный сценарий: клиент, который цепочкой
шлёт длинные запросы. Запрос на ~80 с делает соседние старты далеко отстоящими, а аккаунт
получает следующий запрос через 2–3 с после закрытия потока — это и есть
`Messages too frequent`. Поэтому дедлайн **от конца** обязателен; в логе его видно как
`end_to_start_gap=`.

При инциденте смотри сначала на `end_to_start_gap`: если он мал при `starts_60s=1`,
поднимать надо `DANYAPI_MIN_END_GAP`, а не `DANYAPI_MIN_REQUEST_INTERVAL`.

Порог `MIN_REQUEST_INTERVAL` = 4 с выбран по первому замеру (блокировка на `60s≈19`
запросах, то есть ~1 запрос в 3 с). Запас тонкий, но для сценария длинных запросов он не
релевантен — там работает `MIN_END_GAP`.

## Сначала включи файловый лог

По умолчанию Dany пишет **только в консоль**, история после перезапуска теряется. Если
пользователь ещё не добавил — попроси добавить в `.env`:

```
DANYAPI_LOG_FILE=./danyapi.log
DANYAPI_LOG_LEVEL=INFO
```

Ротация: `DANYAPI_LOG_MAX_BYTES` (10 МБ), `DANYAPI_LOG_BACKUP_COUNT` (3).

**Про уровень.** Событие пишется через `log.warning`, поэтому достаточно `INFO`. Но `DEBUG`
тоже рабочий — и он удобнее для разбора: даёт `httpx`-запросы и PoW-детали вокруг инцидента.
Цена — лог растёт быстрее и ротации (`danyapi.log.1/.2/.3`) появляются чаще, поэтому во всех
командах ниже используется glob `danyapi.log*`, а не один файл. `httpx`/`httpcore` принудительно
держатся на `WARNING` (`danyapi/logging.py`), так что тело запросов в лог не сыплется; а вот
`uvicorn.access` на `DEBUG` пишет каждый HTTP-запрос — это основной объём.

## Как выглядит событие

```
2026-09-12 10:14:03,221 WARNING danyapi.retry: rate limit hint (too_frequent) where=non-stream account=acct#0 session=92a2ba1d-... request=9e5b3f2e... model=deepseek-v4-pro reason=too_frequent message=Messages too frequent. Try again later. elapsed_ms=843 starts_10s=4 starts_30s=11 starts_60s=23
```

Поля:

| поле | смысл |
|---|---|
| `where` | какой путь словил: `non-stream` / `stream` / `continuation` |
| `account` | `acct#N` — индекс аккаунта в пуле |
| `session` | ключ сессии (у `continuation` — id сессии DeepSeek) |
| `request` | `request_id` запроса |
| `reason` | `finish_reason` из хинта |
| `message` | текст хинта (обрезан до 200 символов) |
| `elapsed_ms` | сколько шёл запрос до сбоя (у `continuation` — `-`) |
| `starts_10s` / `starts_30s` / `starts_60s` | **сколько запросов ушло на этот аккаунт за 10/30/60 с до сбоя** |
| `end_to_start_gap` | секунд между концом предыдущего запроса и стартом этого (`-` = ещё не измерен) |

Ключевые поля — `starts_*` **вместе с** `end_to_start_gap`. Одних `starts_*` мало: при
цепочке длинных запросов они показывают `1`, хотя аккаунт получает запрос через пару
секунд после закрытия предыдущего потока.

## Быстрый старт (3 команды)

> **Важно про ротацию.** При `DANYAPI_LOG_LEVEL=DEBUG` лог растёт быстро, и старые события
> уезжают в `danyapi.log.1`, `.2`, `.3`. Поэтому везде читаем **`danyapi.log*`** (glob
> подхватывает и ротации) — иначе свежий инцидент может оказаться не в активном файле.
> Порядок в glob-выдаче не хронологический: активный файл новее, но идёт не последним —
> сортируй по времени из строки лога, а не по позиции.

```bash
cd /home/odolen/ssd_new/DanyAPI

# 1. Все события rate-limit: время, аккаунт, путь, темп
grep -h 'rate limit hint' danyapi.log* | tail -50

# 2. Сводка по темпу в момент сбоев (по какому окну чаще всего ловим)
grep -ho 'starts_60s=[0-9]*' danyapi.log* | sort | uniq -c | sort -rn | head

# 3. Разбивка по аккаунтам — не один ли токен страдает
grep -h 'rate limit hint' danyapi.log* | grep -o 'account=[^ ]*' | sort | uniq -c | sort -rn
```

`-h` подавляет имена файлов в выводе — иначе строки перемешаются с `danyapi.log.1:`.

## Разбор одного инцидента

```bash
.venv/bin/python - <<'PY'
import glob, re
LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?rate limit hint \((?P<reason>[^)]*)\) "
    r"where=(?P<where>\S+) account=(?P<account>\S+) "
    r"session=(?P<session>\S+) request=(?P<request>\S+) model=(?P<model>\S+) "
    r"reason=(?P<reason2>\S+) message=(?P<message>.*?) elapsed_ms=(?P<elapsed>\S+) "
    r"starts_10s=(?P<s10>\d+) starts_30s=(?P<s30>\d+) starts_60s=(?P<s60>\d+)"
)
events = []
for path in glob.glob("danyapi.log*"):
    for line in open(path, encoding="utf-8", errors="replace"):
        m = LINE.search(line)
        if m:
            events.append(m.groupdict())
# Сортируем по времени из строки лога, а не по имени файла — ротации не в хронологическом порядке.
events.sort(key=lambda e: e["ts"])

print(f"всего событий: {len(events)}")
for e in events[-10:]:
    print(f"  {e['ts']}  {e['where']:<12} {e['account']:<8} "
          f"10s={e['s10']:>3} 30s={e['s30']:>3} 60s={e['s60']:>3} "
          f"elapsed={e['elapsed']}ms  request={e['request'][:8]}")

# При каком темпе ломается: разброс стартов в каждом окне среди сбоев
for key in ("s10", "s30", "s60"):
    vals = sorted(int(e[key]) for e in events)
    if vals:
        print(f"{key}: min={vals[0]} медиана={vals[len(vals)//2]} max={vals[-1]}")
PY
```

Читай результат так: **минимальные** `starts_*` среди сбоев — это нижняя граница того, что
сервер уже не терпит. Порог пейсинга ставится с запасом ниже неё.

## Что делать с цифрами

Пейсинг **уже реализован** — это `DANYAPI_MIN_REQUEST_INTERVAL` (см. раздел «Пейсинг» выше),
порог по умолчанию 4 с.

Задача этого раздела — проверить, хватает ли текущего порога, и при необходимости поднять его.

Ориентир для старт-старт пейсинга: если при `starts_60s = 18..23` ловим блокировку
стабильно, а при `starts_60s <= 10` не ловим ни разу — рабочий порог где-то между,
ставить ближе к нижнему краю.

Но если `end_to_start_gap` в момент сбоя мал (единицы секунд) при `starts_60s = 1..5`,
это другой сценарий: аккаунт долбят не частотой стартов, а отсутствием паузы после
закрытия потока. Порог старт-старт тут ни при чём — поднимать `DANYAPI_MIN_END_GAP`.

**Не выводи порог из одного события.** Нужно хотя бы 5–10 сбоев, иначе это угадывание.
Если событий мало — скажи пользователю, что данных недостаточно, и что нужно погонять под
нагрузкой ещё. Первый замер (2026-09-12) дал ровно один эпизод, и это прямо оговорено.

Если инциденты повторяются при включённом пейсинге — поднимай **тот** интервал, который
отвечает за наблюдаемый сценарий: `MIN_END_GAP` (5 → 8 → 12 с) при малом
`end_to_start_gap`, `MIN_REQUEST_INTERVAL` (4 → 6 → 8 с) при высоком `starts_60s`. Прогоняй
нагрузочный тест заново. Не ищи причину в другом месте, пока не проверен порог.

## Чего НЕ делать

- **Не добавляй `too_frequent` в `RETRYABLE_FINISH_REASONS`.** Это выглядит как очевидный
  фикс, но при блокировке в минуты он только долбит уже наказанный аккаунт. Отсутствие
  там `too_frequent` — намеренное решение, а не недосмотр.
- **Не увеличивай `MAX_RETRIES` / бэкофф** — та же причина.
- **Пейсинг живёт в `_send_with_auth`**, не в `note_request_start`. Вызов `wait_turn()` +
  `note_request_start()` стоит в одной точке (`danyapi/api/deepseek.py`), через которую
  проходят все пять путей отправки. Не разносите это по вызывающим местам и не добавляйте
  второй планировщик — дублирование сломает учёт дедлайна.
- **Не трогай `_human_delay()`** в надежде на пейсинг: он стоит **до** `account_lock`
  (`streaming.py`), поэтому параллельные запросы спят одновременно и всё равно уходят на
  один аккаунт пачкой. Пейсинг (`DANYAPI_MIN_REQUEST_INTERVAL`) работает внутри лока и
  как раз решает эту проблему.

## Проверка, что логирование живо

Если `grep 'rate limit hint'` ничего не даёт, а пользователь уверен, что блокировка была:

1. Проверь, что `DANYAPI_LOG_FILE` выставлен и процесс перезапущен.
2. Проверь, что порог уровня не выше WARNING: `DANYAPI_LOG_LEVEL=INFO` достаточно
   (событие пишется через `log.warning`).
3. Прогони тесты, покрывающие путь:
   ```bash
   .venv/bin/python -m pytest tests/test_retry.py tests/test_accounts.py -q
   ```
   Тесты `test_non_stream_rate_limit_hint_logs_and_raises_429`,
   `test_stream_rate_limit_hint_logs_and_emits_error`,
   `test_log_rate_limit_hint_reports_start_counts` подтверждают, что строка пишется.
4. Если события не пишутся вообще, а блокировка была — возможно, изменилась формулировка.
   Проверь сырой хинт: `_is_rate_limit_hint` ловит как по `finish_reason`
   (`RATE_LIMIT_HINT_REASONS`), так и по подстрокам `too frequent` / `rate limit` /
   `too many requests` (`RATE_LIMIT_HINT_MARKERS`). Новая формулировка → добавить маркер
   в соответствующий набор.

## Тесты и линтер (после любых правок)

```bash
cd /home/odolen/ssd_new/DanyAPI
.venv/bin/python -m pytest tests/test_accounts.py tests/test_retry.py tests/test_edge_cases.py -q   # быстро
.venv/bin/ruff check danyapi tests && .venv/bin/ruff format --check danyapi tests
.venv/bin/python scripts/arch-check.py
.venv/bin/python -m pytest -q                                              # весь проект
```

У `DeepSeekAccount` есть `__slots__` — при добавлении полей не забудь про них, иначе упадёт
на `AttributeError`. Фейки аккаунтов в `tests/test_retry.py`, `tests/test_tools_api.py`,
`tests/test_api_helpers.py` должны иметь `wait_turn`, `note_request_start`,
`recent_request_count` и `label` — иначе тесты сломаются на вызове из `_send_with_auth`.
Фейк в `tests/test_qwen_api.py` пейсинга не касается и его не требует.

## Чеклист «словили блокировку при включённом пейсинге»

- [ ] Прочитал этот файл.
- [ ] Убедился, что `DANYAPI_LOG_FILE` выставлен и логи есть.
- [ ] Собрал события: сколько их, по каким аккаунтам, в каком `where`.
- [ ] Посмотрел распределение `starts_60s` (и 10s/30s) в момент сбоев.
- [ ] Посмотрел `end_to_start_gap` — не мал ли он при `starts_60s=1`.
- [ ] Проверил `finish_reason` — он может быть `rate_limit_reached`, а не `too_frequent`.
- [ ] Событий ≥ 5? Если нет — сказать пользователю, что данных мало, и не гадать с порогом.
- [ ] Поднял нужный интервал: `DANYAPI_MIN_END_GAP` (5 → 8 → 12 с) при малом gap,
      `DANYAPI_MIN_REQUEST_INTERVAL` (4 → 6 → 8 с) при высоком `starts_60s`.
- [ ] Убедился, что прогон чистый (без чужого трафика через Dany), иначе цифры грязные.
- [ ] Прогнал полную проверку и обновил `PROGRESS.md`.