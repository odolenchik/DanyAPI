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
