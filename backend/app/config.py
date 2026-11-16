import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIV_", extra="ignore")
    host: str = "127.0.0.1"
    port: int = 8765
    public_origin: str = ""
    public_writes_per_minute: int = Field(default=30, ge=1, le=300)
    public_max_request_bytes: int = Field(default=90 * 1024**2, ge=1024, le=90 * 1024**2)
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
    vision_max_images: int = Field(default=6, ge=1, le=12)
    vision_max_edge: int = Field(default=1536, ge=512, le=2048)
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
    agent_max_executions: int = Field(default=12, ge=1, le=100)
    agent_max_issue_reworks: int = Field(default=2, ge=0, le=10)
    agent_max_model_requests: int = Field(default=40, ge=1, le=200)
    agent_max_tool_calls: int = Field(default=120, ge=1, le=1000)
    agent_max_no_progress: int = Field(default=3, ge=1, le=20)
    agent_active_seconds: int = Field(default=1800, ge=30, le=14400)
    agent_media_seconds: int = Field(default=60, ge=5, le=180)
    agent_media_max_frames: int = Field(default=1800, ge=30, le=18000)
    agent_max_composes: int = Field(default=4, ge=1, le=50)
    agent_compose_max_frames: int = Field(default=720, ge=24, le=2880)
    agent_compose_seconds: int = Field(default=300, ge=30, le=1800)

    @property
    def uses_opencode_go(self) -> bool:
        parsed = urlsplit(self.llm_url)
        return (
            parsed.scheme == "https"
            and parsed.netloc.lower() in ("opencode.ai", "opencode.ai:443")
            and parsed.path.rstrip("/") == "/inference/go/openai/v1"
            and not parsed.query
            and not parsed.fragment
        )

    @field_validator("public_origin")
    @classmethod
    def validate_public_origin(cls, value):
        if not value:
            return ""
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("public_origin 必须是无路径的公网HTTPS域名。")
        return f"https://{parsed.hostname.lower()}"


def load_settings() -> Settings:
    config = ROOT / "config.toml"
    values = {}
    if config.exists():
        raw = tomllib.loads(config.read_text(encoding="utf-8-sig"))
        mapping = {
            "app": {"host": "host", "port": "port", "data_dir": "data_dir"},
            "sharing": {
                "public_origin": "public_origin",
                "writes_per_minute": "public_writes_per_minute",
                "max_request_bytes": "public_max_request_bytes",
            },
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
                "vision_max_images": "vision_max_images",
                "vision_max_edge": "vision_max_edge",
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
        for key, value in raw.get("agent", {}).items():
            target = "agent_" + key
            if target in Settings.model_fields and f"AIV_{target.upper()}" not in os.environ:
                values[target] = value
    settings = Settings(**values)
    settings.data_dir = settings.data_dir.resolve()
    for folder in ("blobs", "staging", "previews", "executions", "registry", "logs", "backups"):
        (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
    return settings
