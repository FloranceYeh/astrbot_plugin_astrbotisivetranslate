from __future__ import annotations

import json
from pathlib import Path

from astrbot.core.config.astrbot_config import AstrBotConfig


def test_plugin_schema_loads_with_astrbot_config(tmp_path):
    schema_path = Path(__file__).parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    config = AstrBotConfig(
        config_path=str(tmp_path / "plugin_config.json"),
        schema=schema,
    )

    assert config["batching"] == {
        "window_milliseconds": 200,
        "max_requests": 16,
        "max_characters": 12000,
    }
