from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.config.astrbot_config import AstrBotConfig

from .service import ApiError, AstrBotisiveTranslateService
from .storage import ReadingStore

PLUGIN_NAME = "astrbot_plugin_astrbotisivetranslate"


class AstrBotisiveTranslatePlugin(Star):
    """Power Immersive Translate with AstrBotisive Translate."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """Initialize plugin components.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration.
        """
        super().__init__(context, config)
        self.context = context
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = ReadingStore(self.data_dir / "readings.sqlite3")
        self.service = AstrBotisiveTranslateService(
            context, config, self.store, self._deliver_completed_article
        )

    async def initialize(self) -> None:
        """Open storage and start the OpenAI-compatible listener."""
        await self.store.open()
        removed = await self.store.purge_older_than(
            int(self._reading_settings().get("retention_days", 30))
        )
        if removed:
            logger.info(
                "[AstrBot式翻译] Purged %s expired articles on startup", removed
            )
        if not self._admin_umo():
            logger.warning(
                "[AstrBot式翻译] admin_umo is empty: translation remains available, "
                "but capture, summaries, delivery, context injection, and commands are disabled"
            )
        await self.service.start()

    async def terminate(self) -> None:
        """Stop the listener and close SQLite cleanly."""
        await self.service.stop()
        await self.store.close()
        logger.info("[AstrBot式翻译] Plugin stopped")

    def _reading_settings(self) -> dict[str, Any]:
        value = self.config.get("reading", {})
        return value if isinstance(value, dict) else {}

    def _admin_umo(self) -> str:
        return str(self.config.get("admin_umo", "") or "").strip()

    def _command_denial(self, event: AstrMessageEvent) -> str:
        admin_umo = self._admin_umo()
        if not admin_umo:
            return "插件尚未配置 admin_umo；当前仅提供基础翻译，阅读记录命令已禁用。"
        if str(event.unified_msg_origin) != admin_umo:
            return "此命令仅允许配置的 admin_umo 会话使用。"
        return ""

    async def _latest_article_id(self, article_id: str) -> str | None:
        candidate = str(article_id or "").strip()
        if candidate:
            return candidate
        articles = await self.store.list_articles(1)
        return str(articles[0]["id"]) if articles else None

    async def _deliver_completed_article(self, article: dict[str, Any]) -> None:
        reading = self._reading_settings()
        umo = self._admin_umo()
        if not umo:
            return
        summary = str(article.get("final_summary") or "")
        title = str(article.get("title") or "未命名阅读")
        article_id = str(article["id"])
        delivered = False
        injected = False
        errors: list[str] = []

        if bool(reading.get("send_summary_to_admin", True)):
            try:
                delivered = await self.context.send_message(
                    umo,
                    MessageChain().message(
                        f"阅读已结束：{title}\n\n{summary}\n\n阅读 ID：{article_id}"
                    ),
                )
                if not delivered:
                    errors.append("platform adapter was not found")
            except Exception as exc:
                errors.append(f"message delivery failed: {exc}")
                logger.exception(
                    "[AstrBot式翻译] Summary delivery failed article=%s", article_id
                )

        if bool(reading.get("inject_summary_into_context", True)):
            try:
                manager = self.context.conversation_manager
                conversation_id = await manager.get_curr_conversation_id(umo)
                if not conversation_id:
                    conversation_id = await manager.new_conversation(
                        umo, title="AstrBot式阅读"
                    )
                await manager.add_message_pair(
                    cid=conversation_id,
                    user_message=UserMessageSegment(
                        content=[
                            TextPart(
                                text=(
                                    "[AstrBot式翻译阅读上下文]\n"
                                    f"我刚读完《{title}》。以下是本次阅读记录，后续讨论请以它为依据；"
                                    "若记录没有覆盖原文细节，请明确说明。\n\n"
                                    f"{summary}\n\n阅读 ID：{article_id}"
                                )
                            )
                        ]
                    ),
                    assistant_message=AssistantMessageSegment(
                        content=[
                            TextPart(text="已记录这次阅读，可以继续讨论文章内容。")
                        ]
                    ),
                )
                injected = True
            except Exception as exc:
                errors.append(f"context injection failed: {exc}")
                logger.exception(
                    "[AstrBot式翻译] Context injection failed article=%s", article_id
                )

        await self.store.mark_delivery(
            article_id,
            delivered=delivered,
            injected=injected,
            error="; ".join(errors),
        )

    @filter.command_group("ait")
    def astrbotisive_translate_commands(self):
        """Manage AstrBotisive Translate reading records."""
        pass

    @astrbotisive_translate_commands.command("status")
    async def command_status(self, event: AstrMessageEvent):
        """Show listener and capture status.

        Args:
            event: Current message event.

        Yields:
            Status text or an authorization error.
        """
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        status = self.service.status()
        listener_state = "运行中" if status["started"] else "未运行"
        capture_state = "开启" if status["capture"] else "关闭"
        details = (
            f"HTTP 服务：{listener_state}\n"
            f"监听地址：{status['host']}:{status['port']}\n"
            f"翻译请求合并窗口：{status['batch_window_milliseconds']} ms\n"
            f"标准请求自动归档：{capture_state}\n"
            f"翻译人格：{status['persona_id']}\n"
            f"admin_umo：{self._admin_umo()}"
        )
        if status["error"]:
            details += f"\n启动错误：{status['error']}"
        yield event.plain_result(details)

    @astrbotisive_translate_commands.command("articles")
    async def command_articles(self, event: AstrMessageEvent):
        """List recent reading sessions."""
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        articles = await self.store.list_articles(10)
        if not articles:
            yield event.plain_result("还没有阅读记录。")
            return
        lines = ["最近阅读："]
        for article in articles:
            state = "已完成" if article["status"] == "completed" else "阅读中"
            lines.append(
                f"- {article['title']} [{state}]\n"
                f"  ID: {article['id']}，{article['segment_count']} 段"
            )
        yield event.plain_result("\n".join(lines))

    @astrbotisive_translate_commands.command("summary")
    async def command_summary(self, event: AstrMessageEvent, article_id: str = ""):
        """Show the latest or selected reading summary."""
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        resolved = await self._latest_article_id(article_id)
        if not resolved or not (article := await self.store.get_article(resolved)):
            yield event.plain_result("找不到阅读记录。")
            return
        summary = article["final_summary"] or article["rolling_summary"]
        if not summary:
            summary = (
                "摘要生成功能已关闭。"
                if not bool(
                    self._reading_settings().get("reading_summary_enabled", True)
                )
                else "尚未生成摘要；可使用 /ait finish 结束阅读并生成。"
            )
        yield event.plain_result(
            f"{article['title']}\n阅读 ID：{article['id']}\n\n{summary}"
        )

    @astrbotisive_translate_commands.command("finish")
    async def command_finish(self, event: AstrMessageEvent, article_id: str = ""):
        """Finish the latest or selected reading session."""
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        resolved = await self._latest_article_id(article_id)
        if not resolved:
            yield event.plain_result("还没有可以结束的阅读记录。")
            return
        try:
            article = await self.service.finish_article(resolved)
        except ApiError as exc:
            yield event.plain_result(f"结束阅读失败：{exc.message}")
            return
        result = (
            "摘要已保存"
            if article["final_summary"]
            else "摘要生成已关闭，仅保存阅读记录"
        )
        yield event.plain_result(
            f"已结束阅读《{article['title']}》，{result}。\n阅读 ID：{resolved}"
        )

    @astrbotisive_translate_commands.command("export")
    async def command_export(self, event: AstrMessageEvent, article_id: str = ""):
        """Export the latest or selected reading record as Markdown."""
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        resolved = await self._latest_article_id(article_id)
        if not resolved:
            yield event.plain_result("还没有可以导出的阅读记录。")
            return
        try:
            markdown = await self.store.export_markdown(resolved)
            article = await self.store.get_article(resolved)
        except KeyError:
            yield event.plain_result("找不到阅读记录。")
            return
        safe_title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", article["title"])
        safe_title = safe_title.strip("._")[:60] or "reading"
        export_dir = Path(self.data_dir) / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"{safe_title}_{resolved[:8]}.md"
        export_path.write_text(markdown, encoding="utf-8")
        yield event.chain_result(
            [File(name=export_path.name, file=str(export_path.resolve()))]
        )

    @astrbotisive_translate_commands.command("forget")
    async def command_forget(self, event: AstrMessageEvent, article_id: str = ""):
        """Delete the latest or selected reading record."""
        if denial := self._command_denial(event):
            yield event.plain_result(denial)
            return
        resolved = await self._latest_article_id(article_id)
        if not resolved or not await self.store.delete_article(resolved):
            yield event.plain_result("找不到阅读记录。")
            return
        yield event.plain_result(f"已删除阅读记录：{resolved}")
