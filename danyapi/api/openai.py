from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import tools as toolemu
from ..accounts import AccountPool, AccountPoolBusy, DeepSeekAccount
from ..config import settings
from ..deepseek.client import DeepSeekClient
from ..qwen import api as qwen_api
from ..qwen.accounts import QwenAccount
from ..qwen.client import QwenClient, QwenError
from ..store import JsonStore
from ..tooltrace import ToolTraceLogger
from ..usage import init_tracker
from ..zai import api as zai_api
from ..zai.browser import ZaiBrowser
from ..zai.models import openai_model_list

# Import ChatCompletionRequest early so it's available for route signatures.
# The full re-export block at the bottom brings in every other name used by tests.
from .attachments import MAX_FILE_SIZE
from .models import ChatCompletionRequest, ImageGenerationRequest

log = logging.getLogger("danyapi.api")


def _get_trace() -> ToolTraceLogger:
    """Lazily construct the shared tool-trace logger (no-op if disabled)."""
    cached = getattr(app.state, "tooltrace", None)
    if cached is None:
        path = settings.tooltrace_file or ""
        kwargs = {
            "max_bytes": settings.tooltrace_max_bytes,
            "backup_count": settings.tooltrace_backup_count,
        }
        cached = ToolTraceLogger(path, **kwargs)
        app.state.tooltrace = cached
    return cached


@asynccontextmanager
async def lifespan(app: FastAPI):
    accounts: list[DeepSeekAccount] = []
    qwen_accounts: list[QwenAccount] = []
    zai_browser: ZaiBrowser | None = None
    cache_enabled = settings.cache_enabled
    deepseek_session_store = JsonStore("deepseek-sessions", "default" if cache_enabled else None)
    qwen_session_store = JsonStore("qwen-sessions", "default" if cache_enabled else None)
    deepseek_context_store = JsonStore("deepseek-contexts", "default" if cache_enabled else None)
    qwen_context_store = JsonStore("qwen-contexts", "default" if cache_enabled else None)
    deepseek_affinity_store = JsonStore("deepseek-affinities", "default" if cache_enabled else None)
    qwen_affinity_store = JsonStore("qwen-affinities", "default" if cache_enabled else None)
    try:
        if settings.deepseek_tokens:
            for i, token in enumerate(settings.deepseek_tokens):
                client = DeepSeekClient(token=token, timeout=settings.timeout)
                if not await client.check_auth():
                    log.warning("deepseek token #%d invalid/expired, skipping", i)
                    await client.aclose()
                    continue
                accounts.append(
                    DeepSeekAccount(
                        len(accounts),
                        client,
                        session_cache_size=settings.session_cache_size,
                        ttl=settings.session_ttl,
                        store=deepseek_session_store,
                        min_interval=settings.min_request_interval,
                        burst=settings.request_burst,
                        min_start_gap=settings.min_start_gap,
                    )
                )
            log.info("deepseek accounts ready: %d", len(accounts))
        elif settings.deepseek_email:
            client = DeepSeekClient(timeout=settings.timeout)
            await client.login(
                email=settings.deepseek_email,
                password=settings.deepseek_password,
            )
            accounts.append(DeepSeekAccount(0, client))
            log.info("deepseek login ok, device_id=%s", client.device_id)
        if settings.qwen_tokens:
            for i, token in enumerate(settings.qwen_tokens):
                client = QwenClient(token=token, timeout=settings.timeout)
                if not await client.check_auth():
                    log.warning("qwen token #%d invalid/expired, skipping", i)
                    await client.aclose()
                    continue
                qwen_accounts.append(
                    QwenAccount(
                        len(qwen_accounts),
                        client,
                        session_cache_size=settings.session_cache_size,
                        ttl=settings.session_ttl,
                        store=qwen_session_store,
                    )
                )
            log.info("qwen accounts ready: %d", len(qwen_accounts))
        elif settings.qwen_email:
            client = QwenClient(timeout=settings.timeout)
            await client.login(
                email=settings.qwen_email,
                password=settings.qwen_password,
            )
            qwen_accounts.append(QwenAccount(0, client))
            log.info("qwen login ok")
        if accounts:
            app.state.pool = AccountPool(
                accounts,
                session_cache_size=settings.session_cache_size,
                ttl=settings.session_ttl,
                context_store=deepseek_context_store,
                affinity_store=deepseek_affinity_store,
            )
        else:
            app.state.pool = None
        if qwen_accounts:
            app.state.qwen_pool = AccountPool(
                qwen_accounts,
                label="qwen",
                session_cache_size=settings.session_cache_size,
                ttl=settings.session_ttl,
                context_store=qwen_context_store,
                affinity_store=qwen_affinity_store,
            )
            app.state.qwen_models = await _fetch_qwen_models(qwen_accounts[0].client)
        else:
            app.state.qwen_pool = None
            app.state.qwen_models = []
        if settings.zai_enabled:
            zai_browser = ZaiBrowser(
                token=settings.zai_tokens[0] if settings.zai_tokens else "",
                timeout=settings.timeout,
            )
            app.state.zai_browser = zai_browser
            app.state.zai_models = openai_model_list()
            log.info("zai browser proxy ready (starts lazily, auth=%s)", "token+profile" if settings.zai_tokens else "profile")
        else:
            app.state.zai_browser = None
            app.state.zai_models = []
        if not accounts and not qwen_accounts and zai_browser is None:
            raise RuntimeError("no valid credentials: set DEEPSEEK_TOKENS/DEEPSEEK_EMAIL, QWEN_TOKENS/QWEN_EMAIL, or ZAI_TOKENS/ZAI_TOKEN/DANYAPI_ZAI_ENABLED")
        if settings.usage_enabled:
            app.state.usage = init_tracker(store=JsonStore("usage", "default"), max_records=settings.usage_max_records)
        else:
            app.state.usage = None
        yield
    finally:
        for acct in accounts:
            await acct.client.aclose()
        for acct in qwen_accounts:
            await acct.client.aclose()
        http_client = getattr(app.state, "http_client", None)
        if http_client is not None:
            await http_client.aclose()
        if zai_browser is not None:
            await zai_browser.aclose()


async def _fetch_qwen_models(client: QwenClient) -> list[dict]:
    try:
        raw = await client.fetch_models()
    except QwenError as exc:
        log.warning("qwen models fetch failed, using defaults: %s", exc)
        return QWEN_DEFAULT_MODELS
    models = []
    for model in raw:
        if not isinstance(model, dict) or not model.get("id"):
            continue
        info = model.get("info")
        meta = (info or {}).get("meta") if isinstance(info, dict) else None
        chat_types = (meta or {}).get("chat_type") if isinstance(meta, dict) else None
        chat_types = chat_types or []
        if "t2t" in chat_types:
            model_type = "chat"
        elif "t2i" in chat_types:
            model_type = "image"
        elif "t2v" in chat_types:
            model_type = "video"
        else:
            model_type = "chat"
        models.append(
            {
                "id": model["id"],
                "name": model.get("name") or model["id"],
                "owned_by": "qwen",
                "model_type": model_type,
                "chat_types": chat_types,
            }
        )
    if not models:
        log.warning("qwen models fetch returned nothing, using defaults")
        return QWEN_DEFAULT_MODELS
    return models


IMAGE_SIZE_RE = re.compile(r"^(\d{2,5})\s*[*x\u00d7,]\s*(\d{2,5})$", re.IGNORECASE)
MIN_IMAGE_DIM = 16
MAX_IMAGE_DIM = 8192

# Aspect ratios accepted by the Qwen image models (extra.meta.size).
QWEN_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16")


def _aspect_ratio(dims: tuple[int, int]) -> str:
    """Pick the Qwen-supported aspect ratio closest to the requested dimensions."""
    target = dims[0] / dims[1]
    best = QWEN_IMAGE_RATIOS[0]
    best_delta = None
    for ratio in QWEN_IMAGE_RATIOS:
        width, height = (int(part) for part in ratio.split(":"))
        delta = abs(math.log(target / (width / height)))
        if best_delta is None or delta < best_delta:
            best, best_delta = ratio, delta
    return best


def _parse_image_size(size: str | None) -> tuple[int, int] | None:
    if size is None or not size.strip():
        return None
    match = IMAGE_SIZE_RE.fullmatch(size.strip())
    if match is None:
        raise HTTPException(400, f"invalid size {size!r}: expected WIDTHxHEIGHT (e.g. 1152x2048 or 1152*2048)")
    width, height = int(match.group(1)), int(match.group(2))
    if not (MIN_IMAGE_DIM <= width <= MAX_IMAGE_DIM and MIN_IMAGE_DIM <= height <= MAX_IMAGE_DIM):
        raise HTTPException(400, f"size out of range: both dimensions must be within {MIN_IMAGE_DIM}..{MAX_IMAGE_DIM}")
    return width, height


def _resize_image_bytes(content: bytes, dims: tuple[int, int] | None) -> bytes:
    if dims is None:
        return content
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(content)) as img:
            fmt = img.format or "PNG"
            resized = img.resize(dims, Image.Resampling.LANCZOS)
            if fmt.upper() == "JPEG" and resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            buffer = BytesIO()
            resized.save(buffer, format=fmt)
            return buffer.getvalue()
    except Exception as exc:
        log.warning("image resize to %s failed, returning original: %s", dims, exc)
        return content


def _image_http_client() -> httpx.AsyncClient:
    client = getattr(app.state, "http_client", None)
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=30)
        app.state.http_client = client
    return client


_ASYNC_B64_THRESHOLD = 1 << 20


async def _b64encode(data: bytes) -> str:
    if len(data) > _ASYNC_B64_THRESHOLD:
        data = await asyncio.to_thread(base64.b64encode, data)
    else:
        data = base64.b64encode(data)
    return data.decode("ascii")


async def _image_markdown(data: bytes, content_type: str) -> str:
    return f"![image](data:{content_type or 'image/png'};base64,{await _b64encode(data)})"


async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    data = await file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, f"uploaded file exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit")
    content_type = (file.content_type or "application/octet-stream").split(";", 1)[0].strip() or "application/octet-stream"
    return data, content_type


def _image_edit_req(prompt: str, image_md: str, mask_md: str | None) -> str:
    parts: list[str] = []
    if prompt.strip():
        parts.append(prompt.strip())
    parts.append(image_md)
    if mask_md:
        parts.append(mask_md)
    return "\n".join(parts)


async def _image_pool(request: Request) -> AccountPool:
    pool: AccountPool | None = getattr(app.state, "qwen_pool", None)
    if pool is None:
        raise HTTPException(503, "qwen provider is not configured (required for image generation)")
    return pool


async def _image_generations(req: ImageGenerationRequest, pool: AccountPool) -> dict:
    dims = _parse_image_size(req.size)
    count = max(1, int(getattr(req, "n", 1) or 1))

    account, existing_sid = await _acquire_account(pool, req.session_id)

    want_b64 = req.response_format == "b64_json"
    use_http = want_b64 or dims is not None
    data: list[dict] = []
    usage = None
    result_sid = existing_sid
    hc = _image_http_client()
    download_sem = asyncio.Semaphore(4)

    async def _fetch_image(url: str) -> dict:
        if not use_http:
            return {"url": url}
        async with download_sem:
            try:
                img_resp = await hc.get(url)
                if img_resp.status_code != 200:
                    log.warning("image download failed (%s) for %s, returning url", img_resp.status_code, url)
                    return {"url": url}
                if dims is not None:
                    payload_bytes = await asyncio.to_thread(_resize_image_bytes, img_resp.content, dims)
                else:
                    payload_bytes = img_resp.content
                return {"b64_json": await _b64encode(payload_bytes)}
            except Exception as exc:
                log.warning("image fetch failed for %s, returning url: %s", url, exc)
                return {"url": url}

    try:
        for _ in range(count):
            result = await qwen_api.collect_image(
                account=account,
                pool=pool,
                existing_sid=result_sid,
                lock=account.sem,
                prompt=req.prompt,
                model=req.model,
                model_id=req.model,
                image_size=_aspect_ratio(dims) if dims is not None else None,
                user=req.user,
            )
            result_sid = result.get("session_id") or result_sid
            if result.get("usage"):
                usage = result.get("usage")
            if result["image_urls"]:
                data.extend(await asyncio.gather(*(_fetch_image(url) for url in result["image_urls"])))
    except AccountPoolBusy:
        raise HTTPException(429, "all accounts are busy, try again later") from None

    if not data:
        raise HTTPException(502, "image generation returned no data")

    return {
        "created": int(time.time()),
        "data": data,
        "usage": usage,
        "session_id": result_sid,
    }


app = FastAPI(title="DanyAPI", lifespan=lifespan)


def _pool_stats(pool) -> dict | None:
    if pool is None:
        return None
    try:
        return pool.stats()
    except Exception:
        return None


def _usage_summary() -> dict | None:
    """Totals from the usage tracker, or None when tracking is disabled."""
    tracker = getattr(app.state, "usage", None)
    if tracker is None:
        return None
    try:
        return tracker.snapshot()["totals"]
    except Exception:
        return None


@app.get("/health")
async def health() -> dict:
    pool = getattr(app.state, "pool", None)
    qwen_pool = getattr(app.state, "qwen_pool", None)
    zai_browser = getattr(app.state, "zai_browser", None)
    return {
        "status": "ok",
        "deepseek": pool is not None,
        "qwen": qwen_pool is not None,
        "zai": zai_browser is not None,
        "deepseek_stats": _pool_stats(pool),
        "qwen_stats": _pool_stats(qwen_pool),
        "usage": _usage_summary(),
    }


@app.get("/v1/usage")
async def usage_stats() -> dict:
    tracker = getattr(app.state, "usage", None)
    if tracker is None:
        raise HTTPException(404, "usage tracking is disabled")
    return tracker.snapshot()


@app.get("/v1/models")
async def list_models() -> dict:
    models = [
        {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": "deepseek",
            "model_type": model_type,
        }
        for name, model_type in MODEL_TYPE_BY_NAME.items()
    ]
    qwen_models: list[dict] = getattr(app.state, "qwen_models", [])
    for model in qwen_models:
        models.append(
            {
                "id": model["id"],
                "object": "model",
                "created": 0,
                "owned_by": model.get("owned_by", "qwen"),
                "name": model.get("name"),
                "model_type": model.get("model_type", "chat"),
            }
        )
    zai_models: list[dict] = getattr(app.state, "zai_models", [])
    for model in zai_models:
        models.append(
            {
                "id": model["id"],
                "object": "model",
                "created": 0,
                "owned_by": model.get("owned_by", "zai"),
                "name": model.get("name"),
                "model_type": model.get("model_type", "chat"),
            }
        )
    return {"object": "list", "data": models}


def _resolve_provider(model: str) -> str:
    if model.startswith("qwen"):
        return "qwen"
    if model in MODEL_TYPE_BY_NAME or model.startswith("deepseek"):
        return "deepseek"
    if model.startswith(("glm", "zai")):
        return "zai"
    raise HTTPException(404, f"Unknown model: {model}")


def _resolve_thinking(req: ChatCompletionRequest, *, default: bool) -> bool:
    """Resolve thinking flag from client request.

    Priority: explicit `thinking` field > `reasoning_effort` (Kimi) > provider default.
    Kimi sends reasoning_effort="low"|"medium"|"high"; any non-"off" value means enabled.
    """
    if req.thinking is not None:
        return req.thinking
    reasoning_effort = getattr(req, "reasoning_effort", None)
    if reasoning_effort is not None:
        return reasoning_effort != "off"
    return default


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Any:
    provider = _resolve_provider(req.model)
    if provider == "qwen":
        return await _chat_completions_qwen(req)
    if provider == "zai":
        return await _chat_completions_zai(req)
    return await _chat_completions_deepseek(req)


@app.post("/v1/images/generations")
async def image_generations(req: ImageGenerationRequest, request: Request) -> dict:
    pool = await _image_pool(request)
    return await _image_generations(req, pool)


@app.post("/v1/images/edits")
async def image_edits(
    request: Request,
    image: Annotated[UploadFile, File()],
    prompt: Annotated[str, Form()] = "",
    mask: Annotated[UploadFile | None, File()] = None,
    model: Annotated[str, Form()] = "qwen3.8-omni-flash",
    n: Annotated[int, Form()] = 1,
    size: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "url",
    user: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = None,
) -> dict:
    pool = await _image_pool(request)
    image_data, image_type = await _read_upload(image)
    edited = _image_edit_req(prompt, await _image_markdown(image_data, image_type), None)
    if mask is not None:
        mask_data, mask_type = await _read_upload(mask)
        edited = f"{edited}\n{await _image_markdown(mask_data, mask_type)}"
    req = ImageGenerationRequest(
        model=model,
        prompt=edited,
        n=n,
        size=size,
        response_format=response_format,
        session_id=session_id,
        user=user,
    )
    return await _image_generations(req, pool)


@app.post("/v1/images/variations")
async def image_variations(
    request: Request,
    image: Annotated[UploadFile, File()],
    model: Annotated[str, Form()] = "qwen3.8-omni-flash",
    n: Annotated[int, Form()] = 1,
    size: Annotated[str | None, Form()] = None,
    response_format: Annotated[str, Form()] = "url",
    user: Annotated[str | None, Form()] = None,
    session_id: Annotated[str | None, Form()] = None,
) -> dict:
    pool = await _image_pool(request)
    image_data, image_type = await _read_upload(image)
    req = ImageGenerationRequest(
        model=model,
        prompt=await _image_markdown(image_data, image_type),
        n=n,
        size=size,
        response_format=response_format,
        session_id=session_id,
        user=user,
    )
    return await _image_generations(req, pool)


async def _acquire_account(pool: AccountPool, session_id: str | None):
    try:
        return await pool.acquire(session_id, settings.acquire_timeout)
    except AccountPoolBusy:
        raise HTTPException(429, "all accounts are busy, try again later") from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _trace_client_event(req: ChatCompletionRequest, request_id: str) -> ToolTraceLogger:
    trace = _get_trace()
    trace.client(
        request_id,
        session_id=req.session_id,
        model=req.model,
        tools=getattr(req, "tools", None),
        tool_choice=getattr(req, "tool_choice", None),
        messages_count=len(req.messages or []),
    )
    return trace


async def _chat_completions_deepseek(req: ChatCompletionRequest) -> Any:
    pool: AccountPool = app.state.pool
    if pool is None:
        raise HTTPException(503, "deepseek provider is not configured")

    request_id = uuid.uuid4().hex
    trace = _trace_client_event(req, request_id)

    model_type = _resolve_model(req.model)
    thinking = _resolve_thinking(req, default=model_type == "expert")
    search = bool(req.search) and model_type == "default"

    context_seq = toolemu.context_sequence(req.messages, user=getattr(req, "user", None))
    if req.session_id:
        account, existing_sid = await _acquire_account(pool, req.session_id)
    else:
        cached_sid = pool.resolve_context(context_seq) if context_seq else None
        account, existing_sid = await _acquire_account(pool, cached_sid)

    # If the resolved session was evicted from local LRU, build a full prompt
    # (system + tools + history) so the recreated session gets complete context.
    has_session = existing_sid is not None and account.sessions.get(existing_sid) is not None

    try:
        prompt, tool_mode = toolemu.build_prompt(
            req.messages,
            getattr(req, "tools", None),
            getattr(req, "tool_choice", None),
            has_session,
            getattr(req, "response_format", None),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # A live session already holds the attachments from earlier turns, so only
    # the current turn's input needs uploading (see _history_end_index).
    attachments = _collect_attachments(req, skip_sent_history=has_session)
    _validate_attachments(attachments, model_type)
    ref_file_ids = None
    if attachments:
        ref_file_ids = await _upload_attachments(account, attachments, model_type, thinking)

    common = {
        "account": account,
        "pool": pool,
        "existing_sid": existing_sid,
        "prompt": prompt,
        "model": req.model,
        "model_type": model_type,
        "thinking": thinking,
        "search": search,
        "ref_file_ids": ref_file_ids,
        "tool_schemas": toolemu.tool_schema_map(getattr(req, "tools", None)),
        "tool_mode": tool_mode,
        "include_usage": _include_usage(req),
        "context_seq": context_seq,
        "request_id": request_id,
        "trace": trace,
        "messages": req.messages,
        "user": getattr(req, "user", None),
        # Rebuild the prompt with full history if the session dies mid-flight
        # (stale parent pointer): the fresh session has no context, so a delta
        # prompt would arrive with nothing behind it.
        "rebuild_prompt": lambda: toolemu.build_prompt(
            req.messages,
            getattr(req, "tools", None),
            getattr(req, "tool_choice", None),
            False,
            getattr(req, "response_format", None),
        )[0],
    }
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": request_id}
    if req.stream:
        return StreamingResponse(
            _stream_guard(_stream_openai(lock=account.sem, **common), req.model),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        result = await _collect_non_stream(lock=account.sem, **common)
    except AccountPoolBusy:
        raise HTTPException(429, "all accounts are busy, try again later") from None
    if isinstance(result, dict):
        result["x_request_id"] = request_id
    return result


async def _chat_completions_qwen(req: ChatCompletionRequest) -> Any:
    pool: AccountPool = app.state.qwen_pool
    if pool is None:
        raise HTTPException(503, "qwen provider is not configured")

    request_id = uuid.uuid4().hex
    trace = _trace_client_event(req, request_id)

    thinking = _resolve_thinking(req, default=True)
    search = bool(req.search)

    context_seq = toolemu.context_sequence(req.messages, user=getattr(req, "user", None))
    if req.session_id:
        account, existing_sid = await _acquire_account(pool, req.session_id)
    else:
        cached_sid = pool.resolve_context(context_seq) if context_seq else None
        account, existing_sid = await _acquire_account(pool, cached_sid)

    # If the resolved session was evicted from local LRU, build a full prompt
    # (system + tools + history) so the recreated session gets complete context.
    has_session = existing_sid is not None and account.sessions.get(existing_sid) is not None

    try:
        prompt, tool_mode = toolemu.build_prompt(
            req.messages,
            getattr(req, "tools", None),
            getattr(req, "tool_choice", None),
            has_session,
            getattr(req, "response_format", None),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    common = {
        "account": account,
        "pool": pool,
        "existing_sid": existing_sid,
        "prompt": prompt,
        "model": req.model,
        "model_id": req.model,
        "thinking": thinking,
        "search": search,
        "tool_schemas": toolemu.tool_schema_map(getattr(req, "tools", None)),
        "tool_mode": tool_mode,
        "include_usage": _include_usage(req),
        "context_seq": context_seq,
        "request_id": request_id,
        "trace": trace,
    }
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": request_id}
    if req.stream:
        return StreamingResponse(
            _stream_guard(qwen_api.stream_openai(lock=account.sem, **common), req.model),
            media_type="text/event-stream",
            headers=headers,
        )

    try:
        result = await qwen_api.collect_non_stream(lock=account.sem, **common)
    except AccountPoolBusy:
        raise HTTPException(429, "all accounts are busy, try again later") from None
    if isinstance(result, dict):
        result["x_request_id"] = request_id
    return result


async def _chat_completions_zai(req: ChatCompletionRequest) -> Any:
    browser: ZaiBrowser | None = getattr(app.state, "zai_browser", None)
    if browser is None:
        raise HTTPException(503, "zai provider is not configured")

    request_id = uuid.uuid4().hex
    trace = _trace_client_event(req, request_id)
    try:
        prompt, tool_mode = toolemu.build_prompt(
            req.messages,
            getattr(req, "tools", None),
            getattr(req, "tool_choice", None),
            False,
            getattr(req, "response_format", None),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    trace.dany(request_id, prompt=prompt, model=req.model)

    tool_schemas = toolemu.tool_schema_map(getattr(req, "tools", None))

    if req.stream:
        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": request_id}
        return StreamingResponse(
            _stream_guard(
                zai_api.stream_openai(
                    browser=browser,
                    prompt=prompt,
                    model=req.model,
                    include_usage=_include_usage(req),
                    tool_mode=tool_mode,
                    tool_schemas=tool_schemas,
                    request_id=request_id,
                ),
                req.model,
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    result = await zai_api.collect_non_stream(
        browser=browser,
        prompt=prompt,
        model=req.model,
        tool_mode=tool_mode,
        tool_schemas=tool_schemas,
        request_id=request_id,
    )
    if isinstance(result, dict):
        result["x_request_id"] = request_id
    return result


# ---------------------------------------------------------------------------
# Re-exports: submodules of this package (models / attachments / deepseek /
# streaming). Kept here so `danyapi.api.openai` exposes the same names as
# before the split, and module-level monkeypatching in tests keeps working.
# ---------------------------------------------------------------------------

from .attachments import (  # noqa: E402,F401
    MAX_FILES_PER_REQUEST,
    Attachment,
    _collect_attachments,
    _fresh_pow_upload_headers,
    _split_data_uri,
    _upload_attachments,
    _validate_attachments,
)
from .deepseek import (  # noqa: E402,F401
    DEEPSEEK_AUTH_ERROR_CODES,
    MAX_RETRIES,
    RETRY_BACKOFF_MAX_SEC,
    RETRY_BACKOFF_SEC,
    RETRYABLE_FINISH_REASONS,
    RETRYABLE_HTTP_STATUSES,
    _busy_error_body,
    _collect_continuation,
    _deepseek_error_detail,
    _deepseek_status,
    _drop_session,
    _fresh_pow_headers,
    _handle_account_error,
    _human_delay,
    _input_exceeds_hint_from_http,
    _is_context_limit,
    _is_input_exceeds_limit,
    _is_retryable_hint,
    _is_retryable_http,
    _prepare_session,
    _send_completion,
    _send_with_auth,
    _try_stop_stream,
)
from .models import (  # noqa: E402,F401
    MODEL_TYPE_BY_NAME,
    QWEN_DEFAULT_MODELS,
    ChatMessage,
    FileSpec,
    _deepseek_usage,
    _finish_reason,
    _include_usage,
    _resolve_model,
)
from .streaming import (  # noqa: E402,F401
    _collect_non_stream,
    _record_deepseek_usage,
    _sse,
    _stream_guard,
    _stream_openai,
)

# Tool-trace helpers (_get_trace, _trace_client_event) are defined above and
# remain importable from this module for tests that monkeypatch the logger.
