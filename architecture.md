# DanyAPI Architecture

FastAPI proxy that exposes a unified OpenAI-compatible `/v1/chat/completions` endpoint over both DeepSeek and Qwen (Tongyi Qianwen) providers. Manages multiple accounts per provider, handles sessions, context deduplication, retry logic with backoff, tool calls, streaming, and file attachments.

## Module Map

```
danyapi/
├── __init__.py
├── __main__.py              # entry point (uvicorn launch)
├── config.py                # settings from env / .env
├── logging.py               # log level setup
│
├── api/                     # OpenAI-compatible HTTP layer
│   ├── openai.py            # FastAPI app: lifespan, routes, provider dispatch
│   ├── models.py            # pydantic models (ChatMessage, ChatCompletionRequest), model constants
│   ├── attachments.py       # file attachment collection and upload to DeepSeek
│   ├── deepseek.py          # DeepSeek-specific helpers: session prep, auth, retry logic
│   └── streaming.py         # non-streaming + streaming OpenAI response generators (DeepSeek)
│
├── tools/                   # prompt building, tool-call parsing/formatting
│   ├── prompt.py            # build_prompt, context_sequence, render_*, ToolCall
│   ├── parse.py             # DSML regexes, JSON/XML tool-call parsers
│   └── format.py            # format_tool_message, tool_call_deltas (SSE)
│
├── deepseek/                # DeepSeek provider internals
│   ├── client.py            # HTTP client: auth, login, completion, upload_file
│   ├── stream.py            # IncrementalSSE parser, MessageReconstructor
│   └── sse.py               # SSE event types
│
├── qwen/                    # Qwen provider internals
│   ├── api.py               # collect_non_stream + stream_openai (Qwen)
│   ├── client.py            # HTTP client: auth, login, completion, fetch_models
│   ├── accounts.py          # QwenAccount, QwenSessionRegistry
│   └── stream.py            # QwenStreamReconstructor
│
├── zai/                     # z.ai browser-proxy provider (persistent Chrome profile auth)
│   ├── api.py               # collect_non_stream, stream_openai + OpenAI response assembly
│   └── browser.py           # Playwright-driven headed Chrome proxy + incremental SSE parser
│
├── retry.py                 # shared retry constants and helpers (MAX_RETRIES, backoff, _drop_session)
├── tokens.py                # heuristic token estimation (estimate_tokens, count_messages_tokens)
├── usage.py                 # UsageTracker + module singleton for /v1/usage
├── accounts.py              # AccountPool, DeepSeekAccount, ContextIndex, account_lock
├── sessions.py              # SessionRegistry (LRU cache with persistence)
├── store.py                 # JsonStore (file-backed JSON key-value store)
└── pow.py                   # Proof-of-Work manager for DeepSeek auth
```

## Request Flow

```
Client POST /v1/chat/completions
  │
  ├─ openai.py: chat_completions(req)
  │   └─ _resolve_provider(model) → "deepseek" | "qwen"
  │
  ├─ DeepSeek path:
  │   ├─ build_prompt() + context dedup via pool.resolve_context()
  │   ├─ acquire account from AccountPool (session affinity / round-robin)
  │   ├─ streaming.py: _stream_openai or _collect_non_stream
  │   │   ├─ deepseek.py: _prepare_session → obtain/create session
  │   │   ├─ retry loop with backoff (up to MAX_RETRIES=5)
  │   │   ├─ _fresh_pow_headers + _send_with_auth → SSE stream
  │   │   ├─ MessageReconstructor parses SSE events
  │   │   ├─ context continuation via _collect_continuation if input_exceeds_limit
  │   │   └─ emit OpenAI-format response (stream or non-stream)
  │   └─ tool-call parsing: tools/parse.py → format_tool_message / tool_call_deltas
  │
  └─ Qwen path:
      ├─ build_prompt() + context dedup
      ├─ acquire account from qwen_pool
      ├─ qwen/api.py: stream_openai or collect_non_stream
      │   ├─ _prepare_session → obtain/create session (with model_id)
      │   ├─ retry loop with backoff
      │   ├─ QwenStreamReconstructor parses SSE events
      │   └─ emit OpenAI-format response
      └─ tool-call parsing: same tools/ module
```

## Key Design Decisions

### Provider Dispatch
`_resolve_provider(model)` routes by model name prefix: `qwen*` → Qwen, `glm*`/`zai*` → z.ai, everything else (including `deepseek*`) → DeepSeek. z.ai is a headed-Chrome browser proxy that supports both streaming and non-streaming. It authenticates through a persistent Chrome profile (log in once via OAuth; `ZAI_BROWSER_DIR` overrides the profile path), so `ZAI_TOKENS` is optional — a token, when set, is merely injected as a cookie. The provider is enabled by a non-empty `ZAI_TOKENS`/`ZAI_TOKEN` or explicitly via `DANYAPI_ZAI_ENABLED=1`. All paths share the same `ChatCompletionRequest` schema and return OpenAI-compatible responses.

### Account Pooling
`AccountPool` manages multiple accounts per provider with:
- **Session affinity**: a given session always goes to the account that created it.
- **Context deduplication**: `ContextIndex` matches message fingerprints across sessions so identical conversations reuse existing sessions.
- **Round-robin fallback**: new requests distribute evenly when no session is specified.
- **Concurrency control**: each account has an `asyncio.Semaphore(1)`; `account_lock()` enforces exclusive access with a configurable timeout that raises `AccountPoolBusy` on contention.

### Retry Logic
Shared constants in `retry.py`:
- `MAX_RETRIES = 5`, exponential backoff from 1s to 8s max.
- `RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}`.
- Provider-specific retryable conditions (DeepSeek: busy hints; Qwen: error codes) are handled separately in their respective modules.

### Session Persistence
`SessionRegistry` is an LRU cache backed by `JsonStore` on disk. Sessions survive restarts within TTL. Both DeepSeek and Qwen use the same pattern — Qwen overrides `_create`, `_reuse`, and deserialization to pass `model_id`. Sessions carry two token counters: `accumulated_tokens` (running total reported to the client) and `accounted_tokens` (how much of it has already been recorded in the usage tracker). Both are persisted so a restarted session does not re-count its earlier turns.

### Token Usage Accounting
`usage.py` holds a process-wide `UsageTracker` (singleton via `init_tracker` / `get_tracker` / `reset_tracker`) that accumulates totals, per-model / per-provider / per-user breakdowns, and a bounded `recent` list. State persists through `JsonStore` under the `usage` key and is restored on startup.

`api/openai.py` owns the lifecycle: `lifespan` calls `init_tracker` when `DANYAPI_USAGE_ENABLED` is on and stores it on `app.state.usage`. `GET /v1/usage` returns `tracker.snapshot()` (404 when disabled) and `GET /health` embeds the totals under `"usage"`.

Recording happens in `api/streaming.py` via `_record_deepseek_usage`, called from all four DeepSeek response paths — `_collect_non_stream`, `_stream_openai`, `_compact_retry`, `_compact_retry_stream`. Completion tokens are counted as the *delta* of `session.accumulated_tokens` since the last record, so multi-turn conversations are not double-counted. `prompt_tokens` is a heuristic estimate: `tokens.py` (`estimate_tokens`, `count_messages_tokens`) approximates CJK-aware token counts from the prompt or message list; `_deepseek_usage` uses the same estimator so the number reported to the client matches what the tracker records. Qwen and z.ai are not tracked yet.

### Streaming
Both providers yield OpenAI-compatible SSE chunks (`data: {...}\n\n`). The response generator buffers deltas until content arrives, then flushes in order to avoid sending empty role-only prefixes prematurely. On disconnect or cancellation, the upstream stream is stopped via provider-specific `stop_stream()` calls.

## File Sizes (post-refactor)

| Module | Lines | Role |
|--------|-------|------|
| `tools/parse.py` | 1440 | DSML, JSON, XML tool-call parsing |
| `api/streaming.py` | 1352 | DeepSeek streaming + non-stream response generators |
| `qwen/api.py` | 675 | Qwen streaming + non-stream response generators |
| `api/openai.py` | 566 | FastAPI app, routes, provider dispatch, re-exports |
| `tools/prompt.py` | 529 | Prompt building, context sequence extraction |
| `accounts.py` | 423 | AccountPool, DeepSeekAccount, ContextIndex |
| `api/deepseek.py` | 307 | DeepSeek session/auth/retry helpers |
| `usage.py` | 165 | UsageTracker, `/v1/usage` backing store |
| `sessions.py` | 152 | SessionRegistry (LRU + persistence) |
| `api/models.py` | 131 | Pydantic models, model constants |
| `api/attachments.py` | 108 | File attachment handling |
| `tokens.py` | 66 | Heuristic token estimation |
| `retry.py` | 27 | Shared retry constants and helpers |
| `tools/format.py` | 44 | Tool message formatting for SSE |
