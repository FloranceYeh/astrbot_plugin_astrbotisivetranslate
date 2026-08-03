from __future__ import annotations

import pytest

from astrbot_plugin_astrbotisivetranslate.storage import ReadingStore


@pytest.mark.asyncio
async def test_store_round_trip_and_markdown_export(tmp_path):
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    try:
        await store.create_article(
            "article-1",
            "Test Article",
            automatic=True,
        )
        sequence = await store.add_segment(
            "article-1", "annotate", "Source paragraph", "译文\n\n〔批注〕说明"
        )
        assert sequence == 1

        await store.update_rolling_summary("article-1", "滚动摘要", 1)
        article = await store.finish_article(
            "article-1", "最终摘要", title="Inferred Title"
        )
        assert article is not None
        assert article["status"] == "completed"
        assert article["title"] == "Inferred Title"

        markdown = await store.export_markdown("article-1")
        assert "# Inferred Title" in markdown
        assert "Source paragraph" in markdown
        assert "〔批注〕说明" in markdown
        assert "最终摘要" in markdown
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_article_cascades_to_segments(tmp_path):
    store = ReadingStore(tmp_path / "readings.sqlite3")
    await store.open()
    try:
        await store.create_article("article-2", "Delete Me")
        await store.add_segment("article-2", "translate", "source", "output")
        assert await store.delete_article("article-2") is True
        assert await store.get_article("article-2") is None
        assert await store.get_segments("article-2") == []
    finally:
        await store.close()
