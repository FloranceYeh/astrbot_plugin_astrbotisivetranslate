from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field

import aiohttp
import pytest

from astrbot_plugin_astrbotisivetranslate.service import (
    AstrBotisiveTranslateService,
)
from astrbot_plugin_astrbotisivetranslate.main import AstrBotisiveTranslatePlugin
from astrbot_plugin_astrbotisivetranslate.storage import ReadingStore


@dataclass
class FakeUsage:
    input: int = 5
    output: int = 3
    total: int = 8


@dataclass
class FakeResponse:
    completion_text: str
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeProvider:
    class Meta:
        id = "default-provider"

    def meta(self):
        return self.Meta()


class FakePersonaManager:
    def get_persona_v3_by_id(self, persona_id):
        if persona_id == "configured-reader":
            return {
                "name": "configured-reader",
                "prompt": "Use the explicitly configured reading persona.",
            }
        return None

    async def get_persona(self, persona_id):
        raise ValueError(persona_id)

    async def resolve_selected_persona(self, **kwargs):
        return (
            "reader",
            {"name": "reader", "prompt": "Speak as a careful literary critic."},
            None,
            False,
        )

    async def get_default_persona_v3(self, umo=None):
        return {"name": "default", "prompt": "Default persona prompt."}


class FakeConversationManager:
    def __init__(self):
        self.pairs = []

    async def get_curr_conversation_id(self, umo):
        return None

    async def get_conversation(self, umo, conversation_id):
        return None

    async def new_conversation(self, umo, title=None):
        return "conversation-1"

    async def add_message_pair(self, **kwargs):
        self.pairs.append(kwargs)


class FakeContext:
    def __init__(self):
        self.calls = []
        self.sent = []
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConversationManager()

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        system_prompt = kwargs.get("system_prompt", "")
        if "final reading note" in system_prompt:
            return FakeResponse(
                "标题：测试文章\n\n## 内容摘要\n摘要\n\n## 核心观点\n观点\n\n"
                "## 重要术语\n术语\n\n## 值得继续讨论\n问题"
            )
        if "compact reading memory" in system_prompt:
            return FakeResponse("滚动摘要")
        return FakeResponse("译文")

    async def get_current_chat_provider_id(self, umo):
        return "default-provider"

    def get_using_provider(self):
        return FakeProvider()

    def get_config(self, umo=None):
        return {"provider_settings": {"default_personality": "reader"}}

    async def send_message(self, umo, message_chain):
        self.sent.append((umo, message_chain))
        return True


class BatchFakeContext(FakeContext):
    async def llm_generate(self, **kwargs):
        system_prompt = kwargs.get("system_prompt", "")
        if "Process the JSON batch" not in system_prompt:
            return await super().llm_generate(**kwargs)
        self.calls.append(kwargs)
        payload = json.loads(kwargs["contexts"][0].content)
        return FakeResponse(
            json.dumps(
                {
                    "items": [
                        {"id": item["id"], "text": f"译文：{item['text']}"}
                        for item in payload["items"]
                    ]
                },
                ensure_ascii=False,
            )
        )


class MalformedBatchContext(FakeContext):
    async def llm_generate(self, **kwargs):
        if "Process the JSON batch" not in kwargs.get("system_prompt", ""):
            return await super().llm_generate(**kwargs)
        self.calls.append(kwargs)
        return FakeResponse("not valid batch JSON")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_config(port: int, *, api_key: str = "secret", host: str = "127.0.0.1"):
    return {
        "admin_umo": "test:FriendMessage:user-1",
        "server": {
            "enabled": True,
            "host": host,
            "port": port,
            "api_key": api_key,
            "requests_per_minute": 100,
            "max_concurrency": 2,
            "request_timeout_seconds": 10,
        },
        "providers": {},
        "batching": {
            "window_milliseconds": 10,
            "max_requests": 16,
            "max_characters": 12000,
        },
        "reading": {
            "capture_standard_requests": True,
            "reading_summary_enabled": True,
            "rolling_summary_trigger_chars": 6000,
            "retention_days": 30,
        },
        "prompts": {"preserve_client_prompts": True},
    }


@pytest.mark.asyncio
async def test_non_loopback_listener_requires_api_key(tmp_path):
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        FakeContext(), make_config(8756, api_key="", host="0.0.0.0"), store, _noop
    )
    try:
        assert await service.start() is False
        assert "api_key is required" in service.start_error
    finally:
        await service.stop()
        await store.close()


async def _noop(article):
    return None


@pytest.mark.asyncio
async def test_openai_api_auth_models_persona_and_capture(tmp_path):
    port = free_port()
    context = FakeContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(context, make_config(port), store, _noop)
    assert await service.start() is True
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/v1/models") as response:
                assert response.status == 401

            headers = {"Authorization": "Bearer secret"}
            async with client.get(
                f"http://127.0.0.1:{port}/v1/models", headers=headers
            ) as response:
                payload = await response.json()
                assert response.status == 200
                assert [item["id"] for item in payload["data"]] == [
                    "astrbot-translate",
                    "astrbot-annotate",
                    "astrbot-deep-read",
                ]

            request = {
                "model": "astrbot-annotate",
                "messages": [
                    {"role": "system", "content": "Translate into Chinese."},
                    {"role": "user", "content": "Source paragraph."},
                ],
            }
            async with client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers=headers,
                json=request,
            ) as response:
                payload = await response.json()
                assert response.status == 200
                assert payload["choices"][0]["message"]["content"] == "译文"
                assert payload["usage"]["total_tokens"] == 8

        assert (
            "Speak as a careful literary critic." in context.calls[-1]["system_prompt"]
        )
        assert "Translate into Chinese." in context.calls[-1]["system_prompt"]
        articles = await store.list_articles()
        assert len(articles) == 1
        segments = await store.get_segments(articles[0]["id"])
        assert segments[0]["source_text"] == "Source paragraph."
        assert segments[0]["output_text"] == "译文"
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_streaming_response_ends_with_done(tmp_path):
    port = free_port()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        FakeContext(), make_config(port), store, _noop
    )
    assert await service.start() is True
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Authorization": "Bearer secret"},
                json={
                    "model": "astrbot-translate",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Source"}],
                },
            ) as response:
                body = await response.text()
                assert response.status == 200
                assert "chat.completion.chunk" in body
                assert body.endswith("data: [DONE]\n\n")
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_compatible_concurrent_requests_share_one_provider_call(tmp_path):
    context = BatchFakeContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    config = make_config(free_port())
    config["batching"]["window_milliseconds"] = 20
    service = AstrBotisiveTranslateService(context, config, store, _noop)
    try:
        payloads = [
            {
                "model": "astrbot-translate",
                "messages": [
                    {"role": "system", "content": "Translate into Chinese."},
                    {"role": "user", "content": source},
                ],
            }
            for source in ("First paragraph.", "Second paragraph.", "Third paragraph.")
        ]
        results = await asyncio.gather(
            *(service._generate(payload) for payload in payloads)
        )

        assert len(context.calls) == 1
        assert [result[0] for result in results] == [
            "译文：First paragraph.",
            "译文：Second paragraph.",
            "译文：Third paragraph.",
        ]
        assert sum(result[3]["total_tokens"] for result in results) == 8
        assert "Translate into Chinese." in context.calls[0]["system_prompt"]
        assert (
            "Speak as a careful literary critic." in context.calls[0]["system_prompt"]
        )
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_incompatible_requests_are_not_merged(tmp_path):
    context = BatchFakeContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        context, make_config(free_port()), store, _noop
    )
    try:
        translate, annotate = await asyncio.gather(
            service._generate(
                {
                    "model": "astrbot-translate",
                    "messages": [{"role": "user", "content": "First"}],
                }
            ),
            service._generate(
                {
                    "model": "astrbot-annotate",
                    "messages": [{"role": "user", "content": "Second"}],
                }
            ),
        )

        assert translate[0] == annotate[0] == "译文"
        assert len(context.calls) == 2
        assert all(
            "Process the JSON batch" not in call["system_prompt"]
            for call in context.calls
        )
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_malformed_batch_response_falls_back_to_individual_calls(tmp_path):
    context = MalformedBatchContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        context, make_config(free_port()), store, _noop
    )
    try:
        results = await asyncio.gather(
            *(
                service._generate(
                    {
                        "model": "astrbot-translate",
                        "messages": [{"role": "user", "content": source}],
                    }
                )
                for source in ("First", "Second")
            )
        )

        assert [result[0] for result in results] == ["译文", "译文"]
        assert len(context.calls) == 3
        assert "Process the JSON batch" in context.calls[0]["system_prompt"]
        assert all(
            "Process the JSON batch" not in call["system_prompt"]
            for call in context.calls[1:]
        )
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_finish_generates_and_delivers_once(tmp_path):
    delivered = []

    async def capture_delivery(article):
        delivered.append(article["id"])

    context = FakeContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        context, make_config(free_port()), store, capture_delivery
    )
    try:
        await store.create_article("article-final", "Temporary Title")
        await store.add_segment(
            "article-final", "deep_read", "A source passage", "一段译文"
        )
        first, second = await asyncio.gather(
            service.finish_article("article-final"),
            service.finish_article("article-final"),
        )
        assert first["status"] == second["status"] == "completed"
        assert first["title"] == second["title"] == "测试文章"
        assert delivered == ["article-final"]
        final_calls = [
            call
            for call in context.calls
            if "final reading note" in call.get("system_prompt", "")
        ]
        assert len(final_calls) == 1
        assert "Speak as a careful literary critic." in final_calls[0]["system_prompt"]
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_plugin_delivery_sends_and_injects_summary(tmp_path):
    context = FakeContext()
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    plugin = AstrBotisiveTranslatePlugin.__new__(AstrBotisiveTranslatePlugin)
    plugin.context = context
    plugin.config = {
        "admin_umo": "test:FriendMessage:user-1",
        "reading": {
            "send_summary_to_admin": True,
            "inject_summary_into_context": True,
        },
    }
    plugin.store = store
    try:
        article = await store.create_article("article-deliver", "Reading Title")
        article = await store.finish_article("article-deliver", "Final summary")
        await plugin._deliver_completed_article(article)

        assert len(context.sent) == 1
        assert "Final summary" in context.sent[0][1].get_plain_text()
        assert len(context.conversation_manager.pairs) == 1
        persisted = await store.get_article("article-deliver")
        assert persisted["delivered_at"]
        assert persisted["injected_at"]
        assert persisted["delivery_error"] == ""
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_automatic_session_rolls_over_without_mixing(tmp_path):
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(
        FakeContext(), make_config(free_port()), store, _noop
    )
    try:
        await store.create_article("old-session", "Old Session", automatic=True)
        await store.add_segment("old-session", "translate", "old source", "旧译文")
        await store._connection().execute(
            "UPDATE articles SET updated_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", "old-session"),
        )
        await store._connection().commit()

        new_id = await service._record_translation("new source", "新译文", "translate")
        assert new_id and new_id != "old-session"
        if service.tasks:
            await asyncio.gather(*list(service.tasks))

        old_article = await store.get_article("old-session")
        new_article = await store.get_article(new_id)
        assert old_article["status"] == "completed"
        assert new_article["status"] == "active"
        assert (await store.get_segments(new_id))[0]["source_text"] == "new source"
    finally:
        await service.stop()
        await store.close()


@pytest.mark.asyncio
async def test_configured_persona_overrides_session_persona(tmp_path):
    context = FakeContext()
    config = make_config(free_port())
    config["persona"] = {"persona_id": "configured-reader"}
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    service = AstrBotisiveTranslateService(context, config, store, _noop)
    try:
        await service._generate(
            {
                "model": "astrbot-translate",
                "messages": [{"role": "user", "content": "Source"}],
            }
        )
        system_prompt = context.calls[-1]["system_prompt"]
        assert "Use the explicitly configured reading persona." in system_prompt
        assert "Speak as a careful literary critic." not in system_prompt
        assert service.status()["persona_id"] == "configured-reader"
    finally:
        await store.close()
