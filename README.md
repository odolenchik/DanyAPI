# DanyAPI

OpenAI-compatible HTTP API built on Python + FastAPI. Instead of using paid provider APIs, DanyAPI talks to the internal APIs of the free web clients [chat.deepseek.com](https://chat.deepseek.com) and [chat.qwen.ai](https://chat.qwen.ai) using server-side accounts. API users need no keys — all requests are made by the server accounts.

## Features

- OpenAI-compatible `POST /v1/chat/completions` endpoint
- Streaming and non-streaming responses (SSE chunks in `data: {...}\n\n` form)
- Two providers behind one API: DeepSeek and Qwen (Tongyi Qianwen)
- Multiple accounts per provider with session affinity, context deduplication, and concurrency control
- Tool/function calling with parsing of DSML, JSON, and XML tool calls
- File attachments: image `data:` URIs and base64 files (DeepSeek only)
- Session persistence across restarts via on-disk LRU cache
- Retry logic with exponential backoff and provider-specific retryable conditions
- Health check and model listing endpoints

## How it works

Clients send standard OpenAI-style chat completion requests. DanyAPI authenticates against the internal APIs of the free web clients using credentials stored on the server, maintains chat sessions, and translates responses back into the OpenAI format. Model name prefix selects the provider: `qwen*` goes to Qwen, everything else (including `deepseek*`) goes to DeepSeek.

## Requirements

- Python 3.13+
- A DeepSeek and/or Qwen account or bearer token
- (Recommended, Linux/macOS) `gcc` or `clang` to build the native Proof-of-Work solver

## Installation

### Quick start with the setup wizard

Clone the repository and run the interactive setup. It creates `.env` from `.env.example`, optionally installs dependencies, collects and validates provider credentials, and can create a launcher shortcut.

```bash
git clone https://github.com/FANATFANATA/DanyAPI.git
cd DanyAPI

# Linux/macOS
./docs/setup.sh

# Windows
docs\setup.bat

# or directly, on any platform
python docs/setup.py
```

Start the server:

```bash
python -m danyapi
```

To start through the auto-update launcher instead:

```bash
python docs/start.py
```

By default the server listens on `0.0.0.0:8000`.

### Manual setup

```bash
git clone https://github.com/FANATFANATA/DanyAPI.git
cd DanyAPI

cp .env.example .env
# edit .env and add DeepSeek and/or Qwen credentials

pip install -r requirements.txt

# Linux/macOS: build the native PoW solver (recommended)
gcc -O2 -o danyapi/deepseek/pow_solver danyapi/deepseek/pow_solver.c

python -m danyapi
```

A pure-Python PoW fallback is available when the native binary is absent, but the native solver is recommended for production.

### Docker

```bash
docker build -t danyapi .
docker run --env-file .env -p 8000:8000 danyapi
```

## Configuration

All settings are read from environment variables, usually provided through `.env`. The `.env.example` file documents the common ones; the full set is listed below.

### Credentials

| Variable | Description |
|----------|-------------|
| `DEEPSEEK_TOKENS` | One or more DeepSeek bearer tokens, comma-separated |
| `DEEPSEEK_TOKEN` | Single DeepSeek bearer token (used only if `DEEPSEEK_TOKENS` is empty) |
| `DEEPSEEK_EMAIL` | DeepSeek login fallback (used only if no tokens are set) |
| `DEEPSEEK_PASSWORD` | Password for DeepSeek email login |
| `QWEN_TOKENS` | One or more Qwen bearer tokens, comma-separated |
| `QWEN_TOKEN` | Single Qwen bearer token (used only if `QWEN_TOKENS` is empty) |
| `QWEN_EMAIL` | Qwen login fallback (used only if no tokens are set) |
| `QWEN_PASSWORD` | Password for Qwen email login |

At least one valid DeepSeek or Qwen credential is required; otherwise the server refuses to start.

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `DANYAPI_HOST` | `0.0.0.0` | Address the API server binds to |
| `DANYAPI_PORT` | `8000` | Port the API server listens on |
| `DANYAPI_TIMEOUT` | `60` | Upstream request timeout in seconds |
| `DANYAPI_ACQUIRE_TIMEOUT` | empty | Seconds to wait for a free account before returning `429` (empty = wait forever) |
| `DANYAPI_SESSION_CACHE_SIZE` | `128` | Max server-side chats cached per provider (LRU) |
| `DANYAPI_SESSION_TTL_SECONDS` | `3600` | Seconds an unused session/context stays reusable (0 = never expire) |
| `DANYAPI_CACHE_DIR` | empty | On-disk cache directory (default: system temp dir, e.g. `%TEMP%\danyapi`) |
| `DANYAPI_CACHE_DISABLED` | empty | Set to `1`/`true`/`yes`/`on` to disable the on-disk cache |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `DANYAPI_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DANYAPI_LOG_FILE` | empty | Log file path (empty = console only) |
| `DANYAPI_LOG_MAX_BYTES` | `10485760` | Max log file size in bytes before rotation |
| `DANYAPI_LOG_BACKUP_COUNT` | `3` | Number of rotated log files to keep |

### Request behaviour

| Variable | Default | Description |
|----------|---------|-------------|
| `DANYAPI_HUMAN_DELAY_MIN` | `0.5` | Minimum delay in seconds before sending a request |
| `DANYAPI_HUMAN_DELAY_MAX` | `3.0` | Maximum delay in seconds before sending a request |

### Tool-call trace (diagnostics)

| Variable | Default | Description |
|----------|---------|-------------|
| `DANYAPI_TOOLTRACE_FILE` | `<log dir or .>/tooltrace.ndjson` | NDJSON tool-call trace log (empty = disabled) |
| `DANYAPI_TOOLTRACE_MAX_BYTES` | `52428800` | Max trace file size in bytes before rotation |
| `DANYAPI_TOOLTRACE_BACKUP_COUNT` | `10` | Number of rotated trace files to keep |

### Launcher only

`DANYAPI_AUTO_UPDATE` (`1` by default) is read by `docs/start.py`, not by the server itself. It makes the launcher check for and install the latest GitHub release before starting the server. Set it to `0` to disable auto-update.

## API

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completion (streaming or non-streaming) |
| `GET` | `/v1/models` | List available models |
| `GET` | `/health` | Health check and account pool status |

### Models

DeepSeek models are defined statically:

- `deepseek-v4-flash` (default)
- `deepseek-v4-pro` (expert; no file attachments)
- `deepseek-v4-vision` (vision)

Qwen models are fetched from the provider at startup. If the fetch fails, the server falls back to `qwen3.8-max`, `qwen3.7-plus`, and `qwen3.7-max`. Fetch the current list from `GET /v1/models`.

### Image generation

`POST /v1/images/generations` generates images through a Qwen account:

```bash
curl http://localhost:8000/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model": "qwen-image-2.0-pro", "prompt": "a red apple on a wooden table", "n": 1, "response_format": "url"}'
```

Available models are `qwen-image-2.0-pro` (default) and `qwen-image-3.0-pro`; Qwen text models that advertise text-to-image (for example `qwen3.8-omni-flash`) also work. Accepted fields: `model`, `prompt`, `n` (1-4), `size` (e.g. `1024x1024`), `response_format` (`url` or `b64_json`), `session_id`, `user`. Requests need a Qwen account - a DeepSeek-only setup returns HTTP 503. `b64_json` or an explicit `size` makes the server download and re-encode the image, which is slower.

### Example request

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

Streaming: add `"stream": true`. The response uses `text/event-stream` with OpenAI-style `data: {...}` chunks.

### Request fields

`POST /v1/chat/completions` accepts the following JSON fields:

- `model` — provider model name (default `deepseek-v4-flash`)
- `messages` — list of chat messages (`role`, `content`, `tool_calls`, `tool_call_id`, `name`)
- `stream` — stream the response (`false` by default)
- `temperature`, `top_p`
- `thinking` — enable thinking mode
- `search` — enable web search
- `session_id` — pin the conversation to a specific server-side session
- `user` — end-user identifier used for context deduplication
- `files` — list of `{name, content, content_type}` where `content` is base64
- `tools`, `tool_choice`, `parallel_tool_calls`
- `response_format`
- `stream_options` — e.g. `{"include_usage": true}`

### File attachments

Attachments are supported for DeepSeek models (not `deepseek-v4-pro`):

- Vision/image content via `image_url` with a base64 `data:` URI inside a message `content` list.
- Arbitrary files via the `files` array (`content` is base64).

Limits: at most 50 files per request and 100 MB per file.

#### Sending an image to a vision model

Use `deepseek-v4-vision` (images only) or `deepseek-v4-flash` (images and text files).
Pass the image as an `image_url` part whose `url` is a base64 `data:` URI:

```json
{
  "model": "deepseek-v4-vision",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "Describe this image."},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,<base64>"}}
    ]
  }]
}
```

A runnable curl that base64-encodes a local file:

```bash
B64=$(base64 -w0 photo.png)
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"deepseek-v4-vision\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Describe this image.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$B64\"}}]}]}"
```

- `url` must be a `data:` URI; a remote `http(s)://` link is rejected with HTTP 400.
- `image_url` also accepts a plain string instead of `{"url": ...}`.
- The same image can be sent through `files` as `{name, content (base64), content_type: "image/png"}`.
- The 100 MB per-file limit applies, so large photos work; DanyAPI decodes the base64, uploads the bytes upstream and references them in the completion.

### Tool calls

Tools use the OpenAI function-calling schema. DanyAPI builds the prompt, sends it upstream, and parses the provider's response — which may come back as DSML, JSON, or XML — into standard OpenAI `tool_calls`.

## Sessions and caching

Sessions are cached per account using an LRU registry and persisted as JSON files on disk. This lets a client continue a conversation across server restarts within the TTL. When `session_id` is omitted, DanyAPI tries to match an existing session by context fingerprint, so identical conversations can reuse an existing chat.

## Development

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Build the native PoW solver (Linux/macOS):

```bash
clang -O2 -o danyapi/deepseek/pow_solver danyapi/deepseek/pow_solver.c
```

Run tests and lint:

```bash
python -m pytest -q
ruff check
ruff format --check
```

CI enforces 100% test coverage and runs `ruff check` plus `ruff format --check`.

## Project structure

```
danyapi/
├── api/          # FastAPI app, models, attachments, DeepSeek streaming/helpers
├── tools/        # prompt building, tool-call parsing and formatting
├── deepseek/     # DeepSeek HTTP client, SSE parsing, stream reconstruction
├── qwen/         # Qwen HTTP client, accounts, SSE parsing, API generators
├── accounts.py   # AccountPool, DeepSeekAccount, ContextIndex, account lock
├── sessions.py   # SessionRegistry (LRU + persistence)
├── store.py      # JsonStore (file-backed JSON key-value store)
├── pow.py        # Proof-of-Work manager for DeepSeek auth
├── retry.py      # shared retry constants and helpers
├── config.py     # settings from env / .env
├── logging.py    # log level setup
├── tooltrace.py  # NDJSON tool-call trace logger
└── __main__.py   # uvicorn entry point

docs/             # documentation site, setup/start scripts
tests/            # pytest test suite
```

## Documentation

- `architecture.md` — architecture overview and request flow
- `docs/` — browsable documentation site (`index.html`, `overview.html`, `setup.html`, `configuration.html`, `usage.html`, `internals.html`)
- `docs/tooltrace-playbook.md` — notes on the tool-call trace format

## Disclaimer

DanyAPI is an unofficial proxy that relies on the internal APIs of free web clients. These APIs are not public contracts and may change or break at any time.

## Contacts

[Creator](https://t.me/DanyaVoredom) · [Telegram channel](https://t.me/DanyAPIFree) · [Website](https://fanatfanata.github.io/DanyAPI/)