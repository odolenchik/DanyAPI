# ToolTrace Playbook (для Qwen Code)

> Инструкция для агента. Когда пользователь говорит «есть ошибка по tool use» /
> «Kimi сломал инструмент» / «посмотри логгер» — читай этот файл и действуй по шагам,
> не ковыряясь в коде заново.

## Что это такое

`danyapi/tooltrace.py` — append-only NDJSON-логгер, который на каждый tool-запрос через
DanyAPI пишет **четыре события с общим `request_id`**:

| actor    | что пишется                                            | когда                          |
|----------|--------------------------------------------------------|--------------------------------|
| `client` | model, объявленные `tool_names`, messages_count        | каждый `/v1/chat/completions`   |
| `dany`   | prompt, который реально ушёл в DeepSeek/Qwen           | каждый запрос                   |
| `upstream` | **raw-контент провайдера** (до парсинга) + reasoning | только если были tools          |
| `parse`  | `result` (`ok` / `unparsed` + суффикс проблемы), `issue` | только если были tools        |

Файл: `tooltrace.ndjson` в корне проекта (или путь из `DANYAPI_TOOLTRACE_FILE`).
Логгер **включён по умолчанию** — отдельной командой запускать не нужно.

## Быстрый старт (3 команды)

```bash
cd /home/odolen/ssd_new/DanyAPI

# 1. Сводка: сколько request'ов, полный цикл vs client-only, топ паттернов коверканья
.venv/bin/python scripts/merge_traces.py tooltrace.ndjson

# 2. Последний инцидент в деталях (все 4 события по последнему request_id с issue)
.venv/bin/python - <<'PY'
import json, collections
ev = [json.loads(l) for l in open("tooltrace.ndjson") if l.strip()]
by = collections.defaultdict(dict)
for e in ev: by[e["request_id"]][e["actor"]] = e
bad = [r for r,a in by.items() if a.get("parse",{}).get("issue")]
rid = bad[-1] if bad else None
print("last incident request_id:", rid)
if rid:
    for actor in ("client","dany","upstream","parse"):
        e = by[rid].get(actor)
        print(f"\n=== {actor} ===")
        print(json.dumps(e, ensure_ascii=False)[:800])
PY

# 3. Сырой upstream-контент (что реально вернул DeepSeek/Qwen — источник коверканья)
.venv/bin/python - <<'PY'
import json
ev = [json.loads(l) for l in open("tooltrace.ndjson") if l.strip()]
for e in ev:
    if e["actor"]=="upstream" and e.get("request_id","").startswith("RID_PREFIX"):
        print(e.get("content"))
PY
```

Замените `RID_PREFIX` на первые 8 символов `request_id` из шага 2.

## Диагностика «почему сломался tool call»

Цепочка: **Kimi → DanyAPI (`/v1/chat/completions`) → DeepSeek/Qwen**.
Ошибка «Invalid args for tool» на стороне Kimi почти всегда означает, что парсер Dany
(`danyapi/tools/parse.py::parse_tool_calls`) не распознал ответ провайдера как tool_call.

Алгоритм:

1. **Найди request_id** инцидента (шаг 2 выше). Посмотри `parse.result`:
   - `ok` — парсер счёл всё нормальным, но Kimi всё равно ругается → смотри `upstream.content`,
     возможно коверканье в аргументах (`extra_argument` / `wrong_type`).
   - `unparsed:<issue>` — парсер **не распознал** вызов. `issue` говорит класс:
     `json_malformed`, `no_tool_call`, `unknown_tool`.
   - `ok:extra_argument` / `ok:wrong_type` — распарсилось, но с коверканием (Kimi может отбросить).

2. **Смотри `upstream.content`** (шаг 3) — это сырой ответ модели. Сравни с тем, что ожидает
   парсер (`{"tool_calls": [{"name","arguments"}]}`). Типичные коверканья DeepSeek:
   - **DSML-теги**: `<｜｜DSML｜｜invoke name="...">` вместо JSON → починено в `826baa7`.
   - **Markdown-обёртка + обрезанный JSON** (DeepSeek уткнулся в лимит токенов, блок ```` ```json ````
     не закрыт) → починено в `33d6b34` (last-resort fallback чинит незакрытый JSON).
   - **Лишний ключ** в аргументах (`offset`, `line_offset`) → классификатор ловит как `extra_argument`.
   - **Чужой ключ-обёртка**: `{"calls": [...]}` / `{"tools_calls": [...]}` вместо `tool_calls` →
     починено (`_WRAPPED_CALL_KEYS` в `_extract_wrapped_calls`).
   - **Вложенный `tool_calls`**: элемент массива сам `{"tool_calls": [...]}` → разворачивается рекурсивно.
   - **DSML-инвокация, слитая с inline-JSON**: `invoke name="Bash", "arguments": {...}}]}` без
     `<parameter>`-тегов → чинит `_parse_dsml_fused` (ветка до `_strip_dsml`).
   - **DSML с одной палкой перед `DSML`**: открывающие теги вида `<|DSML||parameter ...>`
     (одна палка слева, две справа) вместо канонических с двумя палками с каждой стороны. `_DSML_SEP` требует
     по две палки с каждой стороны, поэтому такие теги невидимы всем DSML-веткам. Чинит
     `_repair_dsml_separator` — нормализует только битый opener, канонические теги не трогает
     (tooltrace `b1c6a1cc`). Внимание: форма без `<invoke>` (только `parameter`)
     инферит имя инструмента по схеме, поэтому требует `tool_schemas`.
   - **Повтор имени параметра = несколько вызовов**: блок без `<invoke>`, где имя
     аргумента повторяется (4×`parameter name="path"` = 4 вызова Read; 2×`parameter
     name="command"` = 2 вызова Bash). Старый `_parse_dsml_param_only` схлопывал весь
     прогон в один dict (побеждало последнее значение). Теперь прогон разбивается по
     повтору имени (`_parse_dsml_param_only`, tooltrace `24b809c9`, `f4275506`).
   - **Sibling-вызовы, разделённые голым тегом**: блок без `<invoke>` кодирует
     несколько **разных** вызовов, разделённых отдельной строкой ` ` /
     ` ` без слова. У sibling-вызовов имена аргументов не пересекаются
     (Grep: `-n`/`output_mode`/`pattern`; Read: `line_offset`/`n_lines`/`path`),
     поэтому сплит по повтору имени границу не видит и весь блок склеивается в одну
     группу → `unparsed:no_tool_call`. Теперь `_parse_dsml_param_only` считает
     вторым DSML-тегом между соседними параметрами границу вызова (tooltrace
     `0885f2d2`, `8b1c632c`, `0241e65f`).
   - **Sibling-вызовы без разделителя (сплит по схеме)**: тот же блок без
     `<invoke>`, но sibling-вызовы вообще ничем не разделены — только собственным
     close-тегом каждого параметра, а имена аргументов уникальны. DeepSeek
     склеивает параметры разных инструментов в один прогон (Bash `command`/`cwd`/
     `timeout`/…, затем Glob/Grep `path`/`offset`/`head_limit`/`glob`/`type`/`-n`/…).
     Ни повтор имени, ни голый тег границу не видят, `_infer_tool_from_params`
     требует все ключи в одной схеме → ни одна не подходит → весь блок теряется
     (`no_tool_call`). Теперь `_parse_dsml_param_only` разбивает прогон, когда
     очередной параметр оставляет группу без ни одной совместимой схемы
     (`_schema_compatible_tools`, tooltrace `9cf75302`, `59721714`, `be51bb6e`).
     Из `9cf75302` даёт 2 вызова (Bash + Grep) вместо нуля.
   - **Параметры, влитые в закрывающий тег**: хвостовые аргументы идут как `k=v`
     внутри close-тега — `</DSML|| line_offset=170 n_lines=18>`. Их собирает
     `_dsml_close_tag_attrs` (tooltrace `24b809c9`, `50ad2c8a`).
   - **Имя в атрибуте `parameter="X"`**: opener без слова `name=` —
     `<||DSML||parameter="command">`, а значение несёт висячую кавычку
     (`sleep 120"`). `_DSML_PARAM_OPENER_RE` требует `parameter name=`, поэтому
     такие теги невидимы. Матчит `_DSML_PARAM_EQ_OPENER_RE`; висячая кавычка
     срезается (`_parse_dsml_param_only`, tooltrace `32629734`).
   - **Маркер, вклеенный в invoke-opener перед `>`**: имя закрывается лишним DSML-маркером —
     `invoke name="X"` + маркер + `>` вместо канонического `invoke name="X">`.
     `_DSML_INVOKE_RE` ждёт пробел или `>` сразу после имени, поэтому весь блок
     невидим всем DSML-веткам. Нормализует `_repair_dsml_glued_invoke` (tooltrace
     `797cd20e`, `fa460fef`, `c24d8828`).
   - **Маркер, вклеенный в parameter-opener перед `>`**: тот же лишний маркер
     вклеивается и в открывающий тег параметра — `parameter="path"` + маркер + `>`
     вместо канонического `parameter="path">`. И `_DSML_PARAM_OPENER_RE`, и
     `_DSML_PARAM_EQ_OPENER_RE` ждут `>` сразу после кавычки имени, поэтому тег
     невидим всем веткам, а блок падает в `unparsed:no_tool_call`. Нормализует
     `_repair_dsml_glued_param` (tooltrace `4e3abd5c`). Дополнительно
     `_repair_dsml_bare_attributes` раньше принимал легитимный `parameter="X"` за
     голый атрибут с ключом `parameter` и переписывал тег в
     `parameter name="parameter"...`, стирая имя аргумента; теперь ключ `parameter`
     пропускается (tooltrace `4e3abd5c`).
   - **Голый `arguments="<json>"`**: вместо `parameter name=...` на каждый аргумент
     DeepSeek шлёт один тег с атрибутом `arguments`, чьё значение — escaped-JSON объект
     реальных аргументов. Все DSML-ветки ключуются на `parameter name=`, поэтому тег
     невидим. Переписывает в канонический param-тег `_repair_dsml_bare_arguments`, а
     `_unwrap_nested_arguments` затем разворачивает обёртку в реальные ключи (tooltrace
     `797cd20e`, `f9f81609`, `86931c23`, `fa460fef`).
   - **Голые `key="value"`-атрибуты (регрессия `missing_required` + зацикливание)**:
     DeepSeek схлопывает каждый аргумент в голый атрибут на своём DSML-теге — либо
     `key="value"`, либо слитый `parameter name="key="value`. `_DSML_PARAM_RE` требует
     литеральную форму `parameter name="X">`, поэтому аргументы невидимы: вызов
     парсится с ПУСТЫМИ аргументами, клиент отбрасывает его как missing required,
     repair-цикл шлёт промпт снова, а модель повторяет ту же форму — до исчерпания
     лимита. Нормализует в канонический param-тег `_repair_dsml_bare_attributes`
     (`_DSML_BARE_ATTR_RE`; tooltrace `7e2f6788`, `64cd8579`).
   - **Порча валидных `\\`-пар**: старый `_fix_invalid_escapes` матчил второй бэкслеш в `\\|`/`\\(`
     и делал `Invalid \escape` → переписан на посимвольный проход (см. `parse.py`).

3. **Проверь, воспроизводится ли сейчас**: прогони сырой контент через текущий парсер:
   ```bash
   .venv/bin/python - <<'PY'
   import sys, json; sys.path.insert(0,".")
   from danyapi.tools.parse import parse_tool_calls
   ev = [json.loads(l) for l in open("tooltrace.ndjson") if l.strip()]
   for e in ev:
       if e["actor"]=="upstream" and e.get("request_id","").startswith("RID_PREFIX"):
           res = parse_tool_calls(e["content"], None)
           print("NONE (still broken)" if res is None else f"{len(res[0])} calls parsed OK")
   PY
   ```
   Если `NONE` — парсер всё ещё не ловит этот паттерн → надо чинить `parse.py`.

## Как фиксить парсер

- Код: `danyapi/tools/parse.py`, точка входа `parse_tool_calls()`.
- Порядок веток: DSML (`_parse_dsml`) → JSON-объект → bare array → XML → iter objects →
  last-resort `_loads_lenient` (чинит незакрытый JSON через `_fix_unbalanced_json`).
- Новый формат/паттерн = новая функция-парсер + ветка в `parse_tool_calls` **до** generic-paths,
  если формат не-JSON. JSON-подобное — лучше расширять last-resort fallback.
- Типизация аргументов: смотри `_dsml_param_value` / `_coerce_dsml`.

## Тесты и линтер (обязательно после правок `parse.py`)

```bash
cd /home/odolen/ssd_new/DanyAPI
.venv/bin/python -m pytest tests/test_tools.py -q     # быстрый, ~1с — парсер
.venv/bin/ruff check danyapi/tools/parse.py tests/test_tools.py
# после уверенности:
.venv/bin/python -m pytest -q                          # весь проект (~4-5 мин)
```

Тесты на коверканье живут в `tests/test_tools.py` (DSML — блок «DSML ...», truncation —
`test_parse_tool_calls_*_truncated*`). Новый паттерн → новый тест + пример из реального лога.

## Вырожденный повтор (`degenerate_repeat`)

Отдельный класс инцидента, **не** связанный с парсингом: на длинном многозаходном ходу
DeepSeek иногда теряет EOS и повторяет пустой контейнер ` ` сотни раз подряд,
пока не упрётся в лимит вывода. В чате это выглядит как «зацикливание до исчерпания лимита».

Как отличить от обычного коверканья:
- в `upstream.content` идут **десятки подряд** маркеров `calls>` с пустым телом между ними;
- валидный вызов (если был) уже в начале ответа — парсер его извлекает, хвост отбрасывает;
- в логе контент обрезан ровно на 5000 символов (`_truncate(content, 5000)`), потому что
  реальный ответ длиннее; это признак того, что модель не остановилась сама.

Что делает Dany (`danyapi/api/streaming.py`):
- детектор `is_degenerate_repeat` / `degenerate_repeat_count` (`danyapi/tools/parse.py`) считает
  **длиннейший прогон подряд** пустых контейнеров (структурный признак, не вероятностный);
- порог `DANYAPI_DEGENERATE_THRESHOLD` (дефолт 20 — легитимные ответы дают максимум ~7);
- при срабатывании во время стрима поток **обрывается** (`break`) и вызывается `stop_stream`
  на стороне DeepSeek — модель не дожёвывает хвост до лимита;
- в `parse.result` пишется `degenerate_repeat` (виден в сводке `merge_traces.py`);
- `DANYAPI_DEGENERATE_GUARD=0` выключает обрыв (только логирование).

Это **не** лечит причину (сэмплинг веб-API DeepSeek не настраивается — в теле
`/api/v0/chat/completion` нет `temperature`/`penalties`/`stop`). Мера борется с последствием:
не даёт модели дожевать вырожденный хвост. Наблюдаемая корреляция (60 событий): риск растёт
с числом tool-вызовов в предыдущем ходу (≥3 → ~2%, ≥8 → ~17%), то есть профилактика —
сокращать контекст tool-раунда.

## Коммит-конвенция

Один коммит на один паттерн коверканья, формат:
```
feat(tools): parse <формат> tool calls          # новый формат (DSML)
fix(tools): repair <описание> in <место>        # фикс существующего пути
```
В body — ссылка на `request_id` из лога и что изменилось в поведении.

## Env-переменные логгера

| переменная                        | дефолт                    | смысл                          |
|-----------------------------------|---------------------------|--------------------------------|
| `DANYAPI_TOOLTRACE_FILE`          | `<dir(log_file)>/tooltrace.ndjson` (или `./tooltrace.ndjson`) | путь; пустое = логгер off |
| `DANYAPI_TOOLTRACE_MAX_BYTES`     | 52428800 (50 МБ)          | размер файла до ротации        |
| `DANYAPI_TOOLTRACE_BACKUP_COUNT`  | 10                        | сколько backup-копий хранить   |

Путь **относительный** при дефолте — зависит от cwd запуска Dany. Фиксируйте абсолютным, если
запускаете через systemd/docker. Ротация: `tooltrace.ndjson` → `.1` … `.N`.

## Env-переменные защиты от вырожденного повтора

| переменная                        | дефолт | смысл                                                    |
|-----------------------------------|--------|----------------------------------------------------------|
| `DANYAPI_DEGENERATE_GUARD`        | 1      | обрывать стрим при вырожденном повторе (0 = только лог)  |
| `DANYAPI_DEGENERATE_THRESHOLD`    | 20     | сколько подряд пустых контейнеров считать вырождением    |

## Связь с Kimi (этап 2, пока не готов)

Dany возвращает заголовок `X-Request-Id` в ответе — это и есть `request_id` из лога.
Когда добавят Kimi-side патч (`httpClient.ts`, чтение `x-trace-id`), смок будет точным:
один request_id ↔ один запрос Kimi ↔ 4 события Dany. Пока этого нет, сопоставляй по времени
(`ts`) и содержимому prompt из `dany`.

## Чеклист «есть новая ошибка tool use»

- [ ] Прочитал этот файл.
- [ ] Запустил шаг 1 (сводка) — увидел новый request_id с issue?
- [ ] Вытащил `upstream.content` по этому request_id (шаг 3).
- [ ] Определил класс коверканья (DSML / truncation / extra_arg / другое).
- [ ] Прогнал контент через текущий `parse_tool_calls` — парсится?
- [ ] Если нет → чиню `parse.py`, добавляю тест с этим примером, гоняю `pytest tests/test_tools.py`.
- [ ] Коммичу по конвенции выше.
