import os
import tomllib
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIV_", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Field(
        default_factory=lambda: Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "AIVAgent" / "data"
    )
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_output_root: str = "F:/ComfyUI/ComfyUI_windows_portable/ComfyUI/output"
    llm_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    reasoning_effort: str = "low"
    max_input_tokens: int = 128 * 1024
    history_summary_threshold_tokens: int = 96 * 1024
    history_recent_tokens: int = 24 * 1024
    max_output_tokens: int = 8 * 1024
    llm_timeout: int = 60
    turn_budget_usd: float = 0.15
    daily_budget_usd: float = 0.50
    minimum_free_bytes: int = 2 * 1024**3
    max_import_file_bytes: int = 2 * 1024**3
    max_outstanding_tasks: int = 10
    image_timeout_seconds: int = 900
    video_timeout_seconds: int = 1800
    auto_free_after_idle_seconds: int = 300
    image_max_pixels: int = 1800000
    upscale_max_edge: int = 3072
    video_max_pixels: int = 630000
    video_max_frames: int = 243


def load_settings() -> Settings:
    config = ROOT / "config.toml"
    values = {}
    if config.exists():
        raw = tomllib.loads(config.read_text(encoding="utf-8-sig"))
        mapping = {
            "app": {"host": "host", "port": "port", "data_dir": "data_dir"},
            "comfyui": {
                "base_url": "comfy_url",
                "output_read_root": "comfy_output_root",
                "max_outstanding_tasks": "max_outstanding_tasks",
                "image_timeout_seconds": "image_timeout_seconds",
                "video_timeout_seconds": "video_timeout_seconds",
                "auto_free_after_idle_seconds": "auto_free_after_idle_seconds",
            },
            "llm": {
                "base_url": "llm_url",
                "model": "llm_model",
                "reasoning_effort": "reasoning_effort",
                "max_input_tokens": "max_input_tokens",
                "history_summary_threshold_tokens": "history_summary_threshold_tokens",
                "history_recent_tokens": "history_recent_tokens",
                "max_output_tokens": "max_output_tokens",
                "timeout_seconds": "llm_timeout",
                "turn_budget_usd": "turn_budget_usd",
                "daily_budget_usd": "daily_budget_usd",
            },
            "storage": {"minimum_free_bytes": "minimum_free_bytes", "max_import_file_bytes": "max_import_file_bytes"},
        }
        for section, fields in mapping.items():
            for src, dst in fields.items():
                value = raw.get(section, {}).get(src)
                if value not in (None, "") and f"AIV_{dst.upper()}" not in os.environ:
                    values[dst] = value
    settings = Settings(**values)
    settings.data_dir = settings.data_dir.resolve()
    for folder in ("blobs", "staging", "previews", "executions", "registry", "logs", "backups"):
        (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
    return settings
