"""Immutable, local writing guides, bound to published workflow versions."""

import hashlib
import json
import re
from pathlib import Path

from .config import ROOT
from .errors import AppError
from .schemas import PARAMETER_MODELS

TOOL_MODES = {
    "image.generate.zimage": "image_generate",
    "image.generate.qwen": "image_generate",
    "image.edit.qwen.fast": "image_edit_single",
    "image.edit.qwen.reference": "image_edit_reference",
    "image.upscale.seedvr2": "image_upscale",
    "video.generate.minimax_i2v": "video_i2va",
}


def content_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class GuideRegistry:
    def __init__(self, data_dir: Path, root: Path | None = None):
        self.root = (root or ROOT / "prompt_guides").resolve()
        self.guides, self.issues = {}, {}
        try:
            bindings = json.loads((self.root / "bindings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bindings = []
        seen = set()
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict):
                continue
            key = (binding.get("tool_id"), binding.get("tool_version"), binding.get("task_mode"))
            try:
                if key in seen:
                    raise ValueError("同一工具模式存在重复指引绑定")
                seen.add(key)
                guide_id, version = binding["guide_id"], binding["guide_version"]
                if not re.fullmatch(r"[a-z0-9_.-]+", guide_id) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                    raise ValueError("指引身份格式错误")
                folder = f"{guide_id}/{version}"
                manifest = json.loads((self.root / folder / "manifest.json").read_text(encoding="utf-8"))
                if (manifest["id"], manifest["version"], manifest["task_mode"]) != (guide_id, version, key[2]):
                    raise ValueError("指引身份与绑定不一致")
                files = {}
                for name, expected in manifest["files"].items():
                    path = (self.root / name).resolve()
                    if not path.is_relative_to(self.root) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise ValueError("指引内容校验失败")
                    files[name] = path.read_text(encoding="utf-8")
                text = "\n\n".join(files[name] for name in [*manifest["shared"], f"{folder}/guide.md"])
                examples = json.loads(files[f"{folder}/examples.json"])
                snapshot = {"binding": binding, "manifest": manifest, "files": files}
                effective_hash = content_hash(snapshot)
                destination = data_dir / "prompt_guides" / guide_id / version / f"{effective_hash}.json"
                encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.read_text(encoding="utf-8") != encoded:
                    raise ValueError("已保存的指引快照内容已改变")
                if not destination.exists():
                    destination.write_text(encoded, encoding="utf-8")
                self.guides[key] = {**manifest, "text": text, "examples": examples, "effective_hash": effective_hash}
            except (OSError, ValueError, KeyError) as exc:
                self.guides.pop(key, None)
                self.issues[key] = f"专用写作指引不可用：{exc}"

    def get(self, tool_id, version="1.0.0", mode=None):
        key = (tool_id, version, mode or TOOL_MODES.get(tool_id))
        if key not in self.guides:
            raise AppError("guide_unavailable", self.issues.get(key, "此工具尚未配置适用的专用写作指引。"), 503)
        return self.guides[key]

    def cards(self, registry, settings):
        cards = []
        for tool in registry.public():
            try:
                guide = self.get(tool["id"], tool["version"])
                issue = None
            except AppError as exc:
                guide, issue = None, exc.message
            cards.append(
                {
                    "id": tool["id"],
                    "version": tool["version"],
                    "name": tool["name"],
                    "category": tool["category"],
                    "availability": tool["availability"],
                    "assistant_available": guide is not None and tool["availability"] == "available",
                    "guide_issue": issue,
                    "task_mode": TOOL_MODES.get(tool["id"]),
                    "description": guide["description"] if guide else "此工具当前没有可用的写作指引。",
                    "use_cases": guide["use_cases"] if guide else [],
                    "limitations": guide["limitations"] if guide else [],
                    "inputs": tool["inputs"],
                    "fields": tool["fields"],
                    "parameter_schema": PARAMETER_MODELS[tool["id"]].model_json_schema(),
                    "resource_limits": {
                        "image_max_pixels": settings.image_max_pixels,
                        "video_max_pixels": settings.video_max_pixels,
                        "video_max_frames": settings.video_max_frames,
                        "upscale_max_edge": settings.upscale_max_edge,
                    },
                }
            )
        return cards
