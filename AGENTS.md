# AGENTS.md

## 1. Project
DanyAPI — OpenAI-compatible API proxy.
Stack: Python 3.13, FastAPI, httpx, pydantic; no database, session cache via JsonStore (JSON files).
Docs: `architecture.md`, `docs/`.

## 2. Commands
Makefile is the primary entry point; it prefers `.venv/bin/python` when present.
- Setup: `pip install -r requirements.txt -r requirements-dev.txt`
- Run: `python -m danyapi`
- Test: `make check` (lint -> arch-check -> syntax) or `python -m pytest -q`
- Readiness: `make preflight` (env -> deps -> db-ping -> app-check)
- Architecture invariants: `make arch-check` (or `./scripts/arch-check.py`)
- Type/syntax check (mypy not configured): `python -m compileall -q danyapi`
- Lint: `ruff check && ruff format --check`
- Full verification (most important): `ruff check && ruff format --check && python -m pytest -q`
Native PoW solver (Linux/macOS): `gcc -O2 -o danyapi/deepseek/pow_solver danyapi/deepseek/pow_solver.c`

## 3. Hard Architectural Constraints (MUST)
- Do not change the OpenAI-compatible contract of `POST /v1/chat/completions` (request/response shapes) without explicit approval.
- Do not log tokens, passwords, cookies, or `Authorization` headers.
- Do not introduce circular imports between `danyapi/api`, `danyapi/tools`, `danyapi/deepseek`, `danyapi/qwen`, and `danyapi/zai`.
- Do not perform synchronous blocking I/O inside the async request path; use `await`.
- Do not add a new provider except through `_resolve_provider` with tests for both providers.
- Do not use `eval()`, `exec()`, `compile()`, `pickle.loads()`, or `marshal.loads()` in runtime code.
- Do not add raw SQL (`sqlite3`, `psycopg`, `asyncpg`, SQLAlchemy, `.execute()`/`.executemany()`); the project has no database.
- These invariants are enforced by `make arch-check` / `scripts/arch-check.py`; keep it green.

## 4. Definition of Done (DoD)
A feature is done ONLY when the full verification line is green AND the feature's E2E scenario passes.
"Code written" is not done.
Untested changes are not done.
CI enforces 100% coverage; keep tests complete.

## 5. Workflow Rules
- WIP=1: one feature at a time. Do not start a new one until the current one passes DoD.
- No opportunistic refactoring: unrelated issues go to `BACKLOG.md`, leave the code alone.
- Before ending a session, update `PROGRESS.md` (done, in progress, current commit).
- `PROGRESS.md` and `BACKLOG.md` live only in `DanyAPI/` and are written only there, no matter which of the three copies (`DanyAPI/`, `DanyAPI2/`, `DanyAPI3/`) you are working in. Never edit the copies in `DanyAPI2/` or `DanyAPI3/`.
- Documentation is written only in `DanyAPI/`: `README.md`, `architecture.md`, `PROGRESS.md`, `BACKLOG.md`, `AGENTS.md`, `docs/*.md`. The same files in `DanyAPI2/` and `DanyAPI3/` are one-line pointers back here; do not put prose in them.
- Commit only after DoD passes; run the full verification line first.
- Do not bypass tests or lint: fix the failure, never silence it.
- Keep changes scoped to the task; no unrelated cleanup.
- Do not commit `.env`, `pow_solver` binary, or cache files.
- Do not edit `requirements*.txt` without pinning exact versions used by CI.

## 6. Routing
- Adding an endpoint -> `architecture.md`
- Sessions/caching -> `architecture.md` and `danyapi/store.py`
- Writing tests -> `tests/` and `pyproject.toml`
- Tool-call parsing -> `docs/tooltrace-playbook.md`
- Configuration reference -> `docs/configuration.html`
- Provider internals -> `danyapi/deepseek/`, `danyapi/qwen/`, `danyapi/zai/`
- Auth/PoW internals -> `danyapi/pow.py`, `danyapi/deepseek/pow_solver.c`
- Streaming implementation -> `danyapi/api/streaming.py`, `danyapi/qwen/api.py`
- Retry/error handling -> `danyapi/retry.py`, `danyapi/api/deepseek.py`
- Account pooling/concurrency -> `danyapi/accounts.py`
- Readiness checks -> `Makefile`, `scripts/preflight.py`
- Architecture invariants -> `scripts/arch-check.py`