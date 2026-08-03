from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiohttp import web

from astrbot.core import logger
from astrbot.core.agent.message import Message

from .prompts import (
    BATCH_SYSTEM_PROMPT,
    FINAL_SUMMARY_PROMPT,
    MODE_PROMPTS,
    MODELS,
    ROLLING_SUMMARY_PROMPT,
)
from .storage import ReadingStore

CompletionCallback = Callable[[dict[str, Any]], Awaitable[None]]
GenerationResult = tuple[str, str, str, dict[str, int]]


@dataclass(slots=True)
class BatchRequest:
    """One compatible translation request waiting for a batch."""

    mode: str
    provider_id: str
    contexts: list[Message]
    client_system: list[str]
    source_text: str
    kwargs: dict[str, Any]
    future: asyncio.Future[GenerationResult]


class BatchResponseError(Exception):
    """Indicate that a provider response cannot be mapped back to batch items."""


class ApiError(Exception):
    """Represent an OpenAI-compatible HTTP error."""

    def __init__(self, status: int, message: str, code: str) -> None:
        """Initialize an API error.

        Args:
            status: HTTP status code.
            message: Public error message.
            code: Stable machine-readable code.
        """
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class AstrBotisiveTranslateService:
    """Expose AstrBot providers through the AstrBotisive Translate API."""

    def __init__(
        self,
        context: Any,
        config: Any,
        store: ReadingStore,
        on_article_completed: CompletionCallback,
    ) -> None:
        """Initialize service state.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration object.
            store: Persistent reading store.
            on_article_completed: Callback for delivery and context injection.
        """
        self.context = context
        self.config = config
        self.store = store
        self.on_article_completed = on_article_completed
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.background_task: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.start_error = ""
        self._request_times: deque[float] = deque()
        self._capture_lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()
        self._batch_pending: list[BatchRequest] = []
        self._batch_flush_task: asyncio.Task | None = None
        self._batch_futures: set[asyncio.Future[GenerationResult]] = set()
        self._summary_locks: dict[str, asyncio.Lock] = {}
        self._finish_locks: dict[str, asyncio.Lock] = {}
        server = self._section("server")
        self._concurrency = asyncio.Semaphore(
            max(1, min(int(server.get("max_concurrency", 4)), 32))
        )

    def _section(self, name: str) -> dict[str, Any]:
        value = self.config.get(name, {}) if hasattr(self.config, "get") else {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _is_loopback(host: str) -> bool:
        return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=204)
            return self._with_cors(request, response)

        if request.path != "/health":
            server = self._section("server")
            api_key = str(server.get("api_key", "") or "")
            if api_key:
                authorization = request.headers.get("Authorization", "")
                supplied = (
                    authorization[7:]
                    if authorization.lower().startswith("bearer ")
                    else ""
                )
                if not supplied or not hmac.compare_digest(supplied, api_key):
                    return self._with_cors(
                        request,
                        self._error_response(
                            401, "Invalid or missing API key", "invalid_api_key"
                        ),
                    )

            now = time.monotonic()
            limit = max(1, int(server.get("requests_per_minute", 120)))
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()
            if len(self._request_times) >= limit:
                return self._with_cors(
                    request,
                    self._error_response(429, "Rate limit exceeded", "rate_limit"),
                )
            self._request_times.append(now)

        try:
            response = await handler(request)
        except ApiError as exc:
            response = self._error_response(exc.status, exc.message, exc.code)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("[AstrBot式翻译] HTTP request failed")
            response = self._error_response(
                500, "Internal server error", "internal_error"
            )
        return self._with_cors(request, response)

    def _with_cors(
        self, request: web.Request, response: web.StreamResponse
    ) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        allowed = self._section("server").get("allowed_origins", [])
        if not isinstance(allowed, list):
            allowed = []
        if origin and ("*" in allowed or origin in allowed):
            response.headers["Access-Control-Allow-Origin"] = (
                "*" if "*" in allowed else origin
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Vary"] = "Origin"
        return response

    @staticmethod
    def _error_response(status: int, message: str, code: str) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error" if status < 500 else "server_error",
                    "param": None,
                    "code": code,
                }
            },
            status=status,
        )

    async def start(self) -> bool:
        """Start the configured HTTP listener.

        Returns:
            Whether the listener started successfully.
        """
        server = self._section("server")
        if not bool(server.get("enabled", True)):
            self.start_error = "HTTP service is disabled"
            return False
        host = str(server.get("host", "127.0.0.1") or "127.0.0.1").strip()
        port = int(server.get("port", 8756))
        api_key = str(server.get("api_key", "") or "")
        if not 1 <= port <= 65535:
            self.start_error = f"invalid port: {port}"
            return False
        if not self._is_loopback(host) and not api_key:
            self.start_error = "api_key is required for non-loopback listeners"
            logger.error("[AstrBot式翻译] %s", self.start_error)
            return False

        app = web.Application(
            middlewares=[self._middleware],
            client_max_size=max(
                16 * 1024,
                min(int(server.get("max_request_bytes", 1048576)), 16 * 1024 * 1024),
            ),
        )
        app.add_routes(
            [
                web.get("/health", self._health),
                web.get("/v1/models", self._models),
                web.post("/v1/chat/completions", self._chat_completions),
            ]
        )
        try:
            self.runner = web.AppRunner(app, access_log=None)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, host, port)
            await self.site.start()
        except Exception as exc:
            self.start_error = str(exc)
            logger.exception("[AstrBot式翻译] HTTP service failed to start")
            if self.runner is not None:
                await self.runner.cleanup()
            self.runner = None
            self.site = None
            return False

        self.started = True
        self.start_error = ""
        self.background_task = asyncio.create_task(
            self._maintenance_loop(), name="immersive_translate_maintenance"
        )
        logger.info("[AstrBot式翻译] OpenAI API listening on http://%s:%s", host, port)
        return True

    async def stop(self) -> None:
        """Stop background work and release the HTTP listener."""
        self.started = False
        if self._batch_flush_task is not None:
            self._batch_flush_task.cancel()
            await asyncio.gather(self._batch_flush_task, return_exceptions=True)
            self._batch_flush_task = None
        for future in list(self._batch_futures):
            if not future.done():
                future.cancel()
        self._batch_pending.clear()
        if self.background_task is not None:
            self.background_task.cancel()
            await asyncio.gather(self.background_task, return_exceptions=True)
            self.background_task = None
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None

    async def _health(self, request: web.Request) -> web.Response:
        server = self._section("server")
        return web.json_response(
            {
                "status": "ok" if self.started else "stopped",
                "service": "astrbotisive-translate",
                "version": "1.1.0",
                "admin_umo_configured": bool(self._admin_umo()),
                "capture_standard_requests": bool(
                    self._section("reading").get("capture_standard_requests", True)
                ),
                "batch_window_milliseconds": max(
                    0,
                    int(self._section("batching").get("window_milliseconds", 200)),
                ),
                "listener": f"{server.get('host', '127.0.0.1')}:{server.get('port', 8756)}",
            }
        )

    async def _models(self, request: web.Request) -> web.Response:
        now = int(time.time())
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": now,
                        "owned_by": "astrbot",
                    }
                    for model in MODELS
                ],
            }
        )

    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest, UnicodeDecodeError) as exc:
            raise ApiError(
                400, "Request body must be valid JSON", "invalid_json"
            ) from exc
        if not isinstance(payload, dict):
            raise ApiError(400, "Request body must be a JSON object", "invalid_body")
        return payload

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    raise ApiError(
                        400,
                        "Only text message content is supported",
                        "unsupported_content",
                    )
                text = part.get("text")
                if not isinstance(text, str):
                    raise ApiError(400, "Invalid text content", "invalid_messages")
                parts.append(text)
            return "\n".join(parts)
        raise ApiError(400, "Message content must be text", "invalid_messages")

    def _validate_messages(
        self, payload: dict[str, Any]
    ) -> tuple[list[Message], list[str], str]:
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ApiError(
                400, "messages must be a non-empty array", "invalid_messages"
            )
        if len(raw_messages) > 100:
            raise ApiError(400, "Too many messages", "invalid_messages")
        contexts: list[Message] = []
        system_messages: list[str] = []
        source_text = ""
        for item in raw_messages:
            if not isinstance(item, dict):
                raise ApiError(400, "Invalid message object", "invalid_messages")
            role = item.get("role")
            if role not in {"system", "user", "assistant"}:
                raise ApiError(
                    400, f"Unsupported message role: {role}", "invalid_messages"
                )
            text = self._text_content(item.get("content"))
            if role == "system":
                system_messages.append(text)
                continue
            contexts.append(Message(role=role, content=text))
            if role == "user":
                source_text = text
        if not source_text:
            raise ApiError(
                400, "At least one user message is required", "invalid_messages"
            )
        return contexts, system_messages, source_text

    def _mode(self, model: Any) -> tuple[str, str]:
        model_id = str(model or "astrbot-translate")
        mode = MODELS.get(model_id)
        if mode is None:
            raise ApiError(
                400,
                f"Unknown model '{model_id}'. Use one of: {', '.join(MODELS)}",
                "model_not_found",
            )
        return model_id, mode

    async def _provider_id(self, mode: str) -> str:
        providers = self._section("providers")
        configured = str(providers.get(f"{mode}_provider_id", "") or "").strip()
        if configured:
            return configured
        umo = self._admin_umo()
        try:
            if umo:
                return await self.context.get_current_chat_provider_id(umo)
        except Exception:
            logger.warning(
                "[AstrBot式翻译] Cannot resolve the provider for admin_umo; using default"
            )
        provider = self.context.get_using_provider()
        if provider is None:
            raise ApiError(
                503, "No AstrBot chat provider is available", "provider_missing"
            )
        return provider.meta().id

    async def _persona_prompt(self) -> str:
        """Resolve the persona currently selected for the administrator session."""
        manager = self.context.persona_manager
        configured_persona_id = str(
            self._section("persona").get("persona_id", "") or ""
        ).strip()
        if configured_persona_id:
            if configured_persona_id == "[%None]":
                return ""
            try:
                persona = manager.get_persona_v3_by_id(configured_persona_id)
                if persona:
                    return str(persona.get("prompt", "") or "").strip()
                persona = await manager.get_persona(configured_persona_id)
                prompt = str(getattr(persona, "system_prompt", "") or "").strip()
                if prompt:
                    return prompt
            except Exception as exc:
                logger.warning(
                    "[AstrBot式翻译] Configured persona %s is unavailable; "
                    "falling back to the session persona: %s",
                    configured_persona_id,
                    exc,
                )
        umo = self._admin_umo()
        if umo:
            try:
                conversation_persona_id = None
                conversation_id = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        umo
                    )
                )
                if conversation_id:
                    conversation = (
                        await self.context.conversation_manager.get_conversation(
                            umo, conversation_id
                        )
                    )
                    conversation_persona_id = getattr(conversation, "persona_id", None)
                config = self.context.get_config(umo=umo)
                provider_settings = config.get("provider_settings", {})
                persona_id, persona, _, _ = await manager.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=conversation_persona_id,
                    platform_name=umo.split(":", 1)[0],
                    provider_settings=provider_settings,
                )
                if persona:
                    return str(persona.get("prompt", "") or "").strip()
                if persona_id == "[%None]":
                    return ""
            except Exception as exc:
                logger.warning(
                    "[AstrBot式翻译] Failed to resolve admin persona; using default: %s",
                    exc,
                )
        try:
            persona = await manager.get_default_persona_v3(umo=umo or None)
            return str(persona.get("prompt", "") or "").strip()
        except Exception as exc:
            logger.warning("[AstrBot式翻译] Failed to resolve default persona: %s", exc)
            return ""

    async def _system_prompt(self, mode: str, client_system: list[str]) -> str:
        prompts = self._section("prompts")
        template = MODE_PROMPTS[mode].format(
            annotation_count=max(1, min(int(prompts.get("annotation_count", 2)), 5)),
            annotation_max_chars=max(
                50, min(int(prompts.get("annotation_max_chars", 240)), 2000)
            ),
            deep_read_max_chars=max(
                100, min(int(prompts.get("deep_read_max_chars", 600)), 4000)
            ),
        )
        if bool(prompts.get("preserve_client_prompts", True)) and client_system:
            template += "\n\n<Client translation settings>\n"
            template += "\n".join(client_system)
            template += "\n</Client translation settings>"
        extra = str(prompts.get(f"{mode}_extra_prompt", "") or "").strip()
        if extra:
            template += f"\n\n<Administrator rules>\n{extra}\n</Administrator rules>"
        persona_prompt = await self._persona_prompt()
        if persona_prompt:
            template += (
                "\n\nApply the following AstrBot persona's voice, preferences, and judgment "
                "where compatible. Translation fidelity and the output format above remain mandatory."
                f"\n<AstrBot persona>\n{persona_prompt}\n</AstrBot persona>"
            )
        return template

    @staticmethod
    def _generation_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        for key in ("temperature", "top_p", "max_tokens"):
            if isinstance(payload.get(key), int | float):
                kwargs[key] = payload[key]
        return kwargs

    @staticmethod
    def _is_batchable(payload: dict[str, Any]) -> bool:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return False
        non_system = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") != "system"
        ]
        return len(non_system) == 1 and non_system[0].get("role") == "user"

    @staticmethod
    def _response_usage(response: Any) -> dict[str, int]:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        response_usage = getattr(response, "usage", None)
        if response_usage is not None:
            usage = {
                "prompt_tokens": int(getattr(response_usage, "input", 0) or 0),
                "completion_tokens": int(getattr(response_usage, "output", 0) or 0),
                "total_tokens": int(getattr(response_usage, "total", 0) or 0),
            }
        return usage

    async def _call_provider(
        self,
        *,
        provider_id: str,
        mode: str,
        contexts: list[Message],
        system_prompt: str,
        kwargs: dict[str, Any],
    ) -> Any:
        timeout = max(
            10,
            min(
                int(self._section("server").get("request_timeout_seconds", 120)),
                600,
            ),
        )
        try:
            await asyncio.wait_for(self._concurrency.acquire(), timeout=5)
        except TimeoutError as exc:
            raise ApiError(429, "Server is busy", "server_busy") from exc
        try:
            return await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    **kwargs,
                ),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise ApiError(504, "Upstream model timed out", "upstream_timeout") from exc
        except ApiError:
            raise
        except Exception as exc:
            logger.exception(
                "[AstrBot式翻译] Provider call failed provider=%s mode=%s",
                provider_id,
                mode,
            )
            raise ApiError(
                502, f"AstrBot provider failed: {exc}", "provider_error"
            ) from exc
        finally:
            self._concurrency.release()

    async def _generate_direct(self, request: BatchRequest) -> GenerationResult:
        response = await self._call_provider(
            provider_id=request.provider_id,
            mode=request.mode,
            contexts=request.contexts,
            system_prompt=await self._system_prompt(
                request.mode, request.client_system
            ),
            kwargs=request.kwargs,
        )
        text = str(response.completion_text or "").strip()
        if not text:
            raise ApiError(
                502, "AstrBot provider returned empty text", "empty_response"
            )
        return (
            text,
            request.source_text,
            request.mode,
            self._response_usage(response),
        )

    @staticmethod
    def _batch_key(request: BatchRequest) -> tuple[Any, ...]:
        return (
            request.mode,
            request.provider_id,
            tuple(request.client_system),
            tuple(sorted(request.kwargs.items())),
        )

    async def _enqueue_batch(self, request: BatchRequest) -> GenerationResult:
        batching = self._section("batching")
        window_ms = max(0, min(int(batching.get("window_milliseconds", 200)), 5000))
        if window_ms <= 0:
            return await self._generate_direct(request)

        async with self._batch_lock:
            self._batch_pending.append(request)
            self._batch_futures.add(request.future)
            request.future.add_done_callback(self._batch_futures.discard)
            if self._batch_flush_task is None or self._batch_flush_task.done():
                self._batch_flush_task = asyncio.create_task(
                    self._flush_batches(window_ms / 1000),
                    name="astrbotisive_translate_batch_flush",
                )
        return await request.future

    async def _flush_batches(self, window_seconds: float) -> None:
        await asyncio.sleep(window_seconds)
        current_task = asyncio.current_task()
        async with self._batch_lock:
            pending, self._batch_pending = self._batch_pending, []
            if self._batch_flush_task is current_task:
                self._batch_flush_task = None

        groups: dict[tuple[Any, ...], list[BatchRequest]] = {}
        for request in pending:
            if not request.future.cancelled():
                groups.setdefault(self._batch_key(request), []).append(request)

        batching = self._section("batching")
        max_requests = max(1, min(int(batching.get("max_requests", 16)), 64))
        max_chars = max(500, min(int(batching.get("max_characters", 12000)), 100000))
        for group in groups.values():
            chunk: list[BatchRequest] = []
            chunk_chars = 0
            for request in group:
                request_chars = len(request.source_text)
                if chunk and (
                    len(chunk) >= max_requests
                    or chunk_chars + request_chars > max_chars
                ):
                    self._schedule(self._process_batch(chunk))
                    chunk = []
                    chunk_chars = 0
                chunk.append(request)
                chunk_chars += request_chars
            if chunk:
                self._schedule(self._process_batch(chunk))

    async def _process_batch(self, requests: list[BatchRequest]) -> None:
        requests = [request for request in requests if not request.future.cancelled()]
        if not requests:
            return
        try:
            if len(requests) == 1:
                results: list[GenerationResult | BaseException] = [
                    await self._generate_direct(requests[0])
                ]
            else:
                try:
                    results = list(await self._generate_batch(requests))
                except BatchResponseError as exc:
                    logger.warning(
                        "[AstrBot式翻译] Cannot split a batch of %s; retrying separately: %s",
                        len(requests),
                        exc,
                    )
                    results = await self._generate_individually(requests)
        except asyncio.CancelledError:
            for request in requests:
                if not request.future.done():
                    request.future.cancel()
            raise
        except Exception as exc:
            results = [exc] * len(requests)

        for request, result in zip(requests, results, strict=True):
            if request.future.done():
                continue
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    request.future.cancel()
                else:
                    request.future.set_exception(result)
            else:
                request.future.set_result(result)

    async def _generate_individually(
        self, requests: list[BatchRequest]
    ) -> list[GenerationResult | BaseException]:
        concurrency = max(
            1,
            min(
                int(self._section("server").get("max_concurrency", 4)),
                32,
            ),
        )
        results: list[GenerationResult | BaseException] = []
        for start in range(0, len(requests), concurrency):
            chunk = requests[start : start + concurrency]
            results.extend(
                await asyncio.gather(
                    *(self._generate_direct(request) for request in chunk),
                    return_exceptions=True,
                )
            )
        return results

    async def _generate_batch(
        self, requests: list[BatchRequest]
    ) -> list[GenerationResult]:
        first = requests[0]
        batch_payload = {
            "items": [
                {"id": str(index), "text": request.source_text}
                for index, request in enumerate(requests)
            ]
        }
        kwargs = dict(first.kwargs)
        if isinstance(kwargs.get("max_tokens"), int):
            kwargs["max_tokens"] = min(int(kwargs["max_tokens"]) * len(requests), 32768)
        response = await self._call_provider(
            provider_id=first.provider_id,
            mode=first.mode,
            contexts=[
                Message(
                    role="user",
                    content=json.dumps(
                        batch_payload, ensure_ascii=False, separators=(",", ":")
                    ),
                )
            ],
            system_prompt=(
                f"{await self._system_prompt(first.mode, first.client_system)}\n\n"
                f"{BATCH_SYSTEM_PROMPT}"
            ),
            kwargs=kwargs,
        )
        outputs = self._parse_batch_response(
            str(response.completion_text or ""), len(requests)
        )
        logger.debug(
            "[AstrBot式翻译] Combined %s requests mode=%s provider=%s",
            len(requests),
            first.mode,
            first.provider_id,
        )
        usages = self._split_usage(self._response_usage(response), len(requests))
        return [
            (output, request.source_text, request.mode, usage)
            for request, output, usage in zip(requests, outputs, usages, strict=True)
        ]

    @staticmethod
    def _parse_batch_response(text: str, expected_count: int) -> list[str]:
        candidate = text.strip()
        fenced = re.fullmatch(
            r"```(?:json)?\s*(.*?)\s*```",
            candidate,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            candidate = fenced.group(1)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise BatchResponseError("response is not valid JSON") from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != expected_count:
            raise BatchResponseError("response item count does not match the request")
        outputs: list[str] = []
        for index, item in enumerate(items):
            if (
                not isinstance(item, dict)
                or item.get("id") != str(index)
                or not isinstance(item.get("text"), str)
                or not item["text"].strip()
            ):
                raise BatchResponseError(f"invalid response item {index}")
            outputs.append(item["text"].strip())
        return outputs

    @staticmethod
    def _split_usage(usage: dict[str, int], count: int) -> list[dict[str, int]]:
        results = [
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            for _ in range(count)
        ]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            quotient, remainder = divmod(max(0, usage[key]), count)
            for index in range(count):
                results[index][key] = quotient + (1 if index < remainder else 0)
        return results

    async def _generate(self, payload: dict[str, Any]) -> GenerationResult:
        _, mode = self._mode(payload.get("model"))
        contexts, client_system, source_text = self._validate_messages(payload)
        request = BatchRequest(
            mode=mode,
            provider_id=await self._provider_id(mode),
            contexts=contexts,
            client_system=client_system,
            source_text=source_text,
            kwargs=self._generation_kwargs(payload),
            future=asyncio.get_running_loop().create_future(),
        )
        if not self._is_batchable(payload):
            return await self._generate_direct(request)
        return await self._enqueue_batch(request)

    async def _chat_completions(self, request: web.Request) -> web.StreamResponse:
        payload = await self._body(request)
        text, source_text, mode, usage = await self._generate(payload)
        if self._admin_umo():
            await self._record_translation(source_text, text, mode)
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        model_id = str(payload.get("model") or "astrbot-translate")
        created = int(time.time())
        if bool(payload.get("stream", False)):
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            self._with_cors(request, response)
            await response.prepare(request)
            chunks = [
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {"index": 0, "delta": {"content": text}, "finish_reason": None}
                    ],
                },
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            for chunk in chunks:
                await response.write(
                    f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                )
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response
        return web.json_response(
            {
                "id": response_id,
                "object": "chat.completion",
                "created": created,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
        )

    def _admin_umo(self) -> str:
        return str(self.config.get("admin_umo", "") or "").strip()

    async def _record_translation(
        self,
        source_text: str,
        output_text: str,
        mode: str,
    ) -> str | None:
        reading = self._section("reading")
        if bool(reading.get("capture_standard_requests", True)):
            async with self._capture_lock:
                article = await self.store.get_active_automatic()
                if article is not None:
                    last_updated = datetime.fromisoformat(str(article["updated_at"]))
                    idle_minutes = max(
                        1, int(reading.get("reading_idle_timeout_minutes", 30))
                    )
                    if (
                        datetime.now(last_updated.tzinfo) - last_updated
                    ).total_seconds() >= idle_minutes * 60:
                        self._schedule(self.finish_article(str(article["id"])))
                        article = None
                if article is None:
                    article_id = uuid.uuid4().hex
                    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
                    article = await self.store.create_article(
                        article_id,
                        f"自动阅读 {now}",
                        automatic=True,
                    )
                else:
                    article_id = str(article["id"])
                finish_lock = self._finish_locks.setdefault(article_id, asyncio.Lock())
                async with finish_lock:
                    article = await self.store.get_article(article_id)
                    if article is None or article["status"] != "active":
                        article_id = uuid.uuid4().hex
                        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
                        article = await self.store.create_article(
                            article_id,
                            f"自动阅读 {now}",
                            automatic=True,
                        )
                    sequence = await self.store.add_segment(
                        article_id, mode, source_text, output_text
                    )
        else:
            return None
        article = await self.store.get_article(article_id)
        if article is not None:
            trigger_chars = max(
                500, int(reading.get("rolling_summary_trigger_chars", 6000))
            )
            segments = await self.store.get_segments(
                article_id, after_sequence=int(article["summarized_sequence"])
            )
            pending_chars = sum(len(item["source_text"]) for item in segments)
            if (
                bool(reading.get("reading_summary_enabled", True))
                and pending_chars >= trigger_chars
            ):
                self._schedule(self._update_rolling_summary(article_id, force=False))
        logger.debug(
            "[AstrBot式翻译] Captured article=%s sequence=%s mode=%s",
            article_id,
            sequence,
            mode,
        )
        return article_id

    def _schedule(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _summary_provider_id(self) -> str:
        configured = str(
            self._section("providers").get("summary_provider_id", "") or ""
        ).strip()
        if configured:
            return configured
        return await self._provider_id("deep_read")

    async def _update_rolling_summary(self, article_id: str, *, force: bool) -> str:
        lock = self._summary_locks.setdefault(article_id, asyncio.Lock())
        async with lock:
            article = await self.store.get_article(article_id)
            if article is None:
                raise ApiError(404, "Article not found", "article_not_found")
            segments = await self.store.get_segments(
                article_id, after_sequence=int(article["summarized_sequence"])
            )
            if not segments:
                return str(article["rolling_summary"])
            reading = self._section("reading")
            pending_chars = sum(len(item["source_text"]) for item in segments)
            trigger = max(500, int(reading.get("rolling_summary_trigger_chars", 6000)))
            if not force and pending_chars < trigger:
                return str(article["rolling_summary"])
            input_limit = max(
                2000, min(int(reading.get("summary_input_max_chars", 24000)), 100000)
            )
            passages = "\n\n".join(
                f"[{item['sequence']}] {item['source_text']}" for item in segments
            )[-input_limit:]
            language = str(reading.get("summary_language", "中文") or "中文")
            max_chars = max(
                300, min(int(reading.get("rolling_summary_max_chars", 1600)), 8000)
            )
            prompt = (
                f"摘要语言：{language}\n\n现有摘要：\n"
                f"{article['rolling_summary'] or '（无）'}\n\n新增原文：\n{passages}"
            )
            try:
                summary_system_prompt = ROLLING_SUMMARY_PROMPT.format(
                    max_chars=max_chars
                )
                persona_prompt = await self._persona_prompt()
                if persona_prompt:
                    summary_system_prompt += (
                        "\n\nUse this AstrBot persona's perspective and voice without changing facts:"
                        f"\n<AstrBot persona>\n{persona_prompt}\n</AstrBot persona>"
                    )
                response = await self.context.llm_generate(
                    chat_provider_id=await self._summary_provider_id(),
                    prompt=prompt,
                    system_prompt=summary_system_prompt,
                    temperature=0.2,
                )
            except Exception as exc:
                logger.warning(
                    "[AstrBot式翻译] Rolling summary failed article=%s: %s",
                    article_id,
                    exc,
                )
                if force:
                    return str(article["rolling_summary"])
                return ""
            summary = str(response.completion_text or "").strip()
            if summary:
                await self.store.update_rolling_summary(
                    article_id, summary, int(segments[-1]["sequence"])
                )
            return summary

    async def finish_article(self, article_id: str) -> dict[str, Any]:
        """Generate the final note, complete the session, and trigger delivery."""
        lock = self._finish_locks.setdefault(article_id, asyncio.Lock())
        async with lock:
            return await self._finish_article_locked(article_id)

    async def _finish_article_locked(self, article_id: str) -> dict[str, Any]:
        """Complete one article while its finish lock is held."""
        article = await self.store.get_article(article_id)
        if article is None:
            raise ApiError(404, "Article not found", "article_not_found")
        if article["status"] == "completed":
            return article
        if not bool(self._section("reading").get("reading_summary_enabled", True)):
            completed = await self.store.finish_article(article_id, "")
            if completed is None:
                raise ApiError(404, "Article not found", "article_not_found")
            return completed
        if int(article["segment_count"]) <= 0:
            completed = await self.store.finish_article(
                article_id, "本次阅读没有已保存的文本片段。"
            )
            if completed is None:
                raise ApiError(404, "Article not found", "article_not_found")
            return completed

        await self._update_rolling_summary(article_id, force=True)
        article = await self.store.get_article(article_id)
        if article is None:
            raise ApiError(404, "Article not found", "article_not_found")
        segments = await self.store.get_segments(article_id)
        reading = self._section("reading")
        input_limit = max(
            2000, min(int(reading.get("summary_input_max_chars", 24000)), 100000)
        )
        recent_passages = "\n\n".join(
            f"[{item['sequence']}] {item['source_text']}" for item in segments[-50:]
        )[-input_limit:]
        max_chars = max(
            500, min(int(reading.get("final_summary_max_chars", 3000)), 12000)
        )
        language = str(reading.get("summary_language", "中文") or "中文")
        prompt = (
            f"摘要语言：{language}\n已有阅读记忆：\n"
            f"{article['rolling_summary'] or '（无）'}\n\n近期原文：\n{recent_passages}"
        )
        try:
            summary_system_prompt = FINAL_SUMMARY_PROMPT.format(max_chars=max_chars)
            persona_prompt = await self._persona_prompt()
            if persona_prompt:
                summary_system_prompt += (
                    "\n\nUse this AstrBot persona's perspective and voice without changing facts:"
                    f"\n<AstrBot persona>\n{persona_prompt}\n</AstrBot persona>"
                )
            response = await self.context.llm_generate(
                chat_provider_id=await self._summary_provider_id(),
                prompt=prompt,
                system_prompt=summary_system_prompt,
                temperature=0.2,
            )
        except Exception as exc:
            logger.exception(
                "[AstrBot式翻译] Final summary failed article=%s", article_id
            )
            raise ApiError(
                502, f"Summary provider failed: {exc}", "summary_failed"
            ) from exc
        summary = str(response.completion_text or "").strip()
        if not summary:
            raise ApiError(
                502, "Summary provider returned empty text", "summary_failed"
            )
        title_match = re.match(
            r"^标题\s*[:：]\s*(.+)$", summary.splitlines()[0].strip()
        )
        inferred_title = title_match.group(1).strip()[:120] if title_match else None
        completed = await self.store.finish_article(
            article_id, summary, title=inferred_title
        )
        if completed is None:
            raise ApiError(404, "Article not found", "article_not_found")
        try:
            await self.on_article_completed(completed)
        except Exception:
            logger.exception(
                "[AstrBot式翻译] Completion delivery callback failed article=%s",
                article_id,
            )
        return completed

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                reading = self._section("reading")
                idle_minutes = max(
                    1, int(reading.get("reading_idle_timeout_minutes", 30))
                )
                for article in await self.store.stale_active_articles(idle_minutes):
                    try:
                        await self.finish_article(str(article["id"]))
                    except Exception as exc:
                        logger.warning(
                            "[AstrBot式翻译] Idle finalization failed article=%s: %s",
                            article["id"],
                            exc,
                        )
                removed = await self.store.purge_older_than(
                    int(reading.get("retention_days", 30))
                )
                if removed:
                    logger.info("[AstrBot式翻译] Purged %s expired articles", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[AstrBot式翻译] Maintenance loop failed")

    def status(self) -> dict[str, Any]:
        """Return command-friendly runtime status."""
        server = self._section("server")
        batching = self._section("batching")
        return {
            "started": self.started,
            "error": self.start_error,
            "host": str(server.get("host", "127.0.0.1")),
            "port": int(server.get("port", 8756)),
            "admin_umo_configured": bool(self._admin_umo()),
            "persona_id": str(
                self._section("persona").get("persona_id", "") or "跟随会话人格"
            ),
            "capture": bool(
                self._section("reading").get("capture_standard_requests", True)
            ),
            "batch_window_milliseconds": max(
                0, min(int(batching.get("window_milliseconds", 200)), 5000)
            ),
        }
