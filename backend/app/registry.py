import copy
import hashlib
import json
import math
import secrets
import shutil
from pathlib import Path

from pydantic import ValidationError

from .errors import AppError
from .schemas import PARAMETER_MODELS
from .video_modes import check_input_slots, source_info


class Registry:
    def __init__(self, source: Path, data: Path):
        self.tools = {}
        self.versions = {}
        trusted = {}
        for manifest_path in sorted(source.glob("*/*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            trusted[(manifest["id"], manifest["version"])] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            destination = data / "registry" / manifest["id"] / manifest["version"]
            if not destination.exists():
                shutil.copytree(manifest_path.parent, destination)
            saved = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            if saved["template_hash"] != manifest["template_hash"]:
                raise RuntimeError(f"同一个工具版本的模板已改变，请发布新版本：{manifest['id']}")
        for manifest_path in sorted((data / "registry").glob("*/*/manifest.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            known_hash = trusted.get((manifest["id"], manifest["version"]))
            if known_hash is None:
                continue
            if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != known_hash:
                raise RuntimeError(f"工具定义与应用内注册版本不同：{manifest['id']} {manifest['version']}")
            destination = manifest_path.parent
            actual = hashlib.sha256((destination / "workflow.api.json").read_bytes()).hexdigest()
            if actual != manifest["template_hash"]:
                raise RuntimeError(f"工具模板校验失败：{manifest['name']}")
            manifest["template"] = json.loads((destination / "workflow.api.json").read_text(encoding="utf-8"))
            manifest["availability"] = "checking"
            manifest["issues"] = []
            current = self.tools.get(manifest["id"])
            if not current or tuple(map(int, manifest["version"].split("."))) > tuple(
                map(int, current["version"].split("."))
            ):
                self.tools[manifest["id"]] = manifest
            self.versions[(manifest["id"], manifest["version"])] = manifest

    def get(self, tool_id, version=None):
        tool = self.versions.get((tool_id, version)) if version else self.tools.get(tool_id)
        if not tool:
            raise AppError("tool_not_found", "这份工具版本不存在。", 404)
        return tool

    def public(self):
        return [
            {
                k: v
                for k, v in tool.items()
                if k not in ("template", "bindings", "input_bindings", "source_hash", "prefixes")
            }
            for tool in self.tools.values()
        ]

    def inspect(self, info):
        for tool in self.versions.values():
            issues = []
            for kind in tool["required_node_types"]:
                if kind not in info:
                    issues.append(f"缺少节点 {kind}")
            for model in tool["required_models"]:
                node = info.get(model["node_type"], {})
                definition = {
                    **node.get("input", {}).get("required", {}),
                    **node.get("input", {}).get("optional", {}),
                }.get(model["field"])
                if definition:
                    options = (
                        definition[0]
                        if isinstance(definition[0], list)
                        else definition[1].get("options", [])
                        if len(definition) > 1 and isinstance(definition[1], dict)
                        else []
                    )
                    if options and model["value"] not in options:
                        issues.append(f"缺少模型 {model['value']}")
            tool["availability"] = "available" if not issues else "unavailable"
            tool["issues"] = issues

    def offline(self, reason):
        for tool in self.versions.values():
            tool["availability"] = "offline"
            tool["issues"] = [reason]

    @staticmethod
    def resolve_video(params, info, settings):
        if params["aspect_ratio"] == "source" and not info:
            raise AppError("invalid_parameters", "此模式没有画面锚点，请明确视频比例。")
        ratio = (
            info["width"] / info["height"]
            if params["aspect_ratio"] == "source"
            else float(params["aspect_ratio"].split(":")[0]) / float(params["aspect_ratio"].split(":")[1])
        )
        pixels = params["megapixels"] * 1024**2
        width = max(32, round(math.sqrt(pixels * ratio) / 32) * 32)
        height = max(32, round(math.sqrt(pixels / ratio) / 32) * 32)
        if width * height > settings.video_max_pixels:
            raise AppError("resource_limit", "对齐后的实际视频面积超过资源上限。")
        frames = max(5, round(params["duration_seconds"] * 24))
        frames += (5 - frames % 17) % 17
        if frames > settings.video_max_frames:
            raise AppError("resource_limit", "实际视频帧数超过资源上限。")
        return dict(width=width, height=height, resolved_frames=frames, resolved_seconds=frames / 24)

    def normalize(self, tool_id, values, assets, settings, count=1, *, version=None):
        tool = self.get(tool_id, version)
        if tool_id == "image.generate.qwen" and values.get("preset") == "quality":
            values = {"width": 1328, "height": 1328, **values}
        try:
            params = PARAMETER_MODELS[tool_id].model_validate(values).model_dump()
        except ValidationError as exc:
            raise AppError(
                "invalid_parameters",
                "工具参数不符合要求。",
                details={"errors": exc.errors(include_url=False, include_context=False)},
            ) from exc
        try:
            check_input_slots(tool, assets)
        except ValueError as exc:
            raise AppError("input_slots", str(exc)) from exc
        for slot, asset in assets.items():
            if asset["media_type"] != "image" or not asset["media_info"].get("width"):
                raise AppError("input_type", f"{slot}需要一张可读取的图片。")
        if tool["category"] != "generate" and count != 1:
            raise AppError("candidate_count", "编辑、放大和视频每次执行一个结果；可完成后再次操作。")
        dimensions = {}
        if tool["category"] == "generate":
            multiple = 16 if tool_id.endswith("qwen") else 32
            if params["width"] % multiple or params["height"] % multiple:
                raise AppError("dimensions", f"宽高需要是{multiple}的倍数。")
            if params["width"] * params["height"] > settings.image_max_pixels:
                raise AppError("resource_limit", "图片面积超过资源设置上限。")
            dimensions = dict(width=params["width"], height=params["height"])
        elif tool["category"] == "edit":
            info = assets["source_image"]["media_info"]
            factor = min(1, 1280 / max(info["width"], info["height"]))
            width = max(64, round(info["width"] * factor / 16) * 16)
            height = max(64, round(info["height"] * factor / 16) * 16)
            if tool_id.endswith("fast"):
                if (params["output_width"] is None) != (params["output_height"] is None):
                    raise AppError("dimensions", "请同时填写宽度和高度，或同时留空以跟随原图。")
                width = params["output_width"] or width
                height = params["output_height"] or height
                if width % 16 or height % 16:
                    raise AppError("dimensions", "编辑输出宽高需要是16的倍数。")
                params.update(output_width=width, output_height=height)
            dimensions = dict(width=width, height=height)
        elif tool["category"] == "upscale":
            info = assets["source_image"]["media_info"]
            width, height = info["width"], info["height"]
            target_long = round(max(width, height) * params["scale"])
            if target_long > settings.upscale_max_edge:
                raise AppError(
                    "resource_limit",
                    f"按此倍率放大后最长边为{target_long}px，超过当前{settings.upscale_max_edge}px上限。请降低倍率或明确修改资源设置。",
                )
            resolution = max(16, round(min(width, height) * params["scale"] / 2) * 2)
            params.update(resolution=resolution, max_resolution=settings.upscale_max_edge)
            dimensions = dict(width=round(width * params["scale"]), height=round(height * params["scale"]))
        elif tool["category"] == "video":
            info = source_info(tool_id, assets)
            params.update(self.resolve_video(params, info, settings))
            width, height, frames = params["width"], params["height"], params["resolved_frames"]
            dimensions = dict(
                width=width,
                height=height,
                frames=frames,
                duration=frames / 24,
                stretched=bool(
                    info
                    and (width / height != info["width"] / info["height"])
                    and tool_id != "video.generate.minimax_l2v"
                ),
            )
        max_seed = 4294967295 if tool["category"] == "upscale" else 9007199254740991
        seed = params.get("seed")
        if seed is None:
            seed = secrets.randbelow(max_seed - count + 1)
        if seed + count - 1 > max_seed:
            raise AppError("seed_range", "候选种子超出允许范围。")
        params["seed"] = seed
        return params, [seed + i for i in range(count)], dimensions

    def render(self, tool_id, version, params, uploads, execution_id):
        tool = self.get(tool_id, version)
        api = copy.deepcopy(tool["template"])
        for key, (node, field) in tool["bindings"].items():
            api[node]["inputs"][field] = params[key]
        for key, (node, field) in tool.get("input_bindings", {}).items():
            if key in uploads:
                api[node]["inputs"][field] = uploads[key]
            else:
                # Optional references must not leave placeholder LoadImage nodes or live wires behind.
                api.pop(node, None)
                for target, input_name in tool.get("optional_connections", {}).get(key, []):
                    api[target]["inputs"].pop(input_name, None)
        if tool_id == "image.generate.qwen":
            api["11"]["inputs"]["value"] = params["preset"] == "fast"
        for node in tool["prefixes"]:
            api[node]["inputs"]["filename_prefix"] = f"AIVAgent/{execution_id}/result"
        return api
