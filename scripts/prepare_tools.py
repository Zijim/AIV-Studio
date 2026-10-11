"""Prepare versioned copies of the user's workflows. Never edits sources."""
import copy
import hashlib
import json
from pathlib import Path

from comfy_cli.workflow_to_api import convert_ui_to_api

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"F:\ComfyUI\ComfyUI_windows_portable\ComfyUI\user\default\workflows")
OBJECT_INFO = json.loads((ROOT / "comfy_probe/records/object_info.json").read_text(encoding="utf-8"))


def field(name, label, kind="text", default=None, **kwargs):
    return dict(name=name, label=label, type=kind, default=default, **kwargs)


prompt = field("prompt", "创作描述", "textarea", "", required=True)
negative = field("negative_prompt", "避免出现", "textarea", "")
dimensions = [field("width", "宽度", "number", 1024, min=512, max=1536, step=32), field("height", "高度", "number", 1024, min=512, max=1536, step=32)]
seed = field("seed", "随机种子", "seed", None)
source_input = dict(slot="source_image", label="原图", media_type="image", required=True)
entries = [
    dict(id="image.generate.zimage", name="快速文生图", subtitle="Z-Image Turbo", category="generate", source="基础文生图.json", inputs=[], fields=[prompt, negative, *dimensions, seed], defaults={}, bindings={"prompt": ["3", "text"], "negative_prompt": ["4", "text"], "width": ["5", "width"], "height": ["5", "height"], "seed": ["1", "seed"]}, prefixes=["12"], roles={"12": "final", "8": "preview"}, changes=[]),
    dict(id="image.generate.qwen", name="精细文生图", subtitle="Qwen Image 2512", category="generate", source="文生图qwen.json", inputs=[], fields=[prompt, negative, *dimensions, field("preset", "生成档位", "select", "fast", options=[{"value": "fast", "label": "快速 · 4步"}, {"value": "quality", "label": "质量 · 50步"}]), seed], defaults={}, bindings={"prompt": ["4", "text"], "negative_prompt": ["5", "text"], "width": ["6", "width"], "height": ["6", "height"], "seed": ["18", "seed"]}, prefixes=["21"], roles={"21": "final", "20": "preview"}, changes=["Normalize prompt widgets to strings; preset controls the existing switch."]),
    dict(id="image.edit.qwen.fast", name="图片编辑", subtitle="Qwen Edit · 快速", category="edit", source="图片编辑.json", inputs=[source_input], fields=[field("instruction", "编辑指令", "textarea", "", required=True), negative, field("output_width", "输出宽度", "number", None, min=64, max=1280, step=16), field("output_height", "输出高度", "number", None, min=64, max=1280, step=16), seed], defaults={}, bindings={"instruction": ["3", "prompt"], "negative_prompt": ["4", "prompt"], "output_width": ["13", "width"], "output_height": ["13", "height"], "seed": ["1", "seed"]}, input_bindings={"source_image": ["7", "image"]}, prefixes=["16"], roles={"16": "final", "15": "preview"}, changes=["Fixed latent dimensions are declared parameter bindings; preserve single-image Lightning branch."]),
    dict(id="image.edit.qwen.reference", name="参考图编辑", subtitle="Qwen Edit · 质量", category="edit", source="图片编辑qwen.json", inputs=[source_input, dict(slot="reference_image", label="参考图", media_type="image", required=True)], fields=[field("instruction", "编辑指令", "textarea", "", required=True), negative, seed], defaults={}, bindings={"instruction": ["3", "prompt"], "negative_prompt": ["4", "prompt"], "seed": ["22", "seed"]}, input_bindings={"source_image": ["7", "image"], "reference_image": ["8", "image"]}, prefixes=["24"], roles={"24": "final"}, changes=["Two input images required; preserve existing 40-step quality branch."]),
    dict(id="image.upscale.seedvr2", name="高清放大", subtitle="SeedVR2 · 3B FP8", category="upscale", source="图像高清放大.json", inputs=[source_input], fields=[field("scale", "放大倍率", "number", 2, min=1, max=4, step=0.5), seed], defaults={}, bindings={"resolution": ["5", "resolution"], "max_resolution": ["5", "max_resolution"], "seed": ["5", "seed"]}, input_bindings={"source_image": ["1", "image"]}, prefixes=["6"], roles={"6": "final", "11": "preview"}, changes=[]),
    dict(id="video.generate.minimax_i2v", name="图片生成视频", subtitle="MiniMax H3 · 本地", category="video", source="video_minimax_h3_i2v.json", inputs=[source_input], fields=[prompt, field("duration_seconds", "请求时长（秒）", "number", 5, min=1, max=10, step=1), field("megapixels", "目标像素（MP）", "number", 0.4, min=0.1, max=0.6, step=0.1), field("aspect_ratio", "画面比例", "select", "source", options=[{"value": "source", "label": "保持原图比例"}, {"value": "16:9", "label": "16:9 · 会拉伸输入"}, {"value": "9:16", "label": "9:16 · 会拉伸输入"}, {"value": "1:1", "label": "1:1 · 会拉伸输入"}]), seed], defaults={}, bindings={"prompt": ["105:104", "prompt"], "width": ["105:104", "width"], "height": ["105:104", "height"], "duration_seconds": ["105:111", "value"], "seed": ["105:15", "noise_seed"]}, input_bindings={"source_image": ["114", "image"]}, prefixes=["92"], roles={"92": "final"}, changes=["Expand subgraph without editing definitions; bind width/height as declared constants instead of UI ResolutionSelector."]),
]


for entry in entries:
    original = (SOURCE / entry["source"]).read_bytes()
    ui = json.loads(original.decode("utf-8-sig"))
    api = convert_ui_to_api(copy.deepcopy(ui), OBJECT_INFO)
    if entry["id"] == "image.generate.qwen":
        for node in ("4", "5"):
            api[node]["inputs"]["text"] = ""
        api["11"]["inputs"]["value"] = True
    if entry["category"] == "video":
        api["105:104"]["inputs"]["width"] = 864
        api["105:104"]["inputs"]["height"] = 480
    # Eliminate unconnected legacy/UI helpers from the fixed execution template.
    reachable = set()
    def visit(node_id):
        if node_id in reachable or node_id not in api:
            return
        reachable.add(node_id)
        for value in api[node_id]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) in api:
                visit(str(value[0]))
    for node in entry["roles"]:
        visit(node)
    api = {k: v for k, v in api.items() if k in reachable}
    output = ROOT / "tool_registry" / entry["id"] / "1.0.0"
    output.mkdir(parents=True, exist_ok=True)
    prepared = json.dumps(api, ensure_ascii=False, indent=2).encode("utf-8")
    entry.update(version="1.0.0", local_only=True, source_hash=hashlib.sha256(original).hexdigest(), template_hash=hashlib.sha256(prepared).hexdigest(), required_node_types=sorted({v["class_type"] for v in api.values()}), required_models=[])
    for v in api.values():
        for key, value in v["inputs"].items():
            if key in ("unet_name", "clip_name", "vae_name", "lora_name", "model_name", "model") and isinstance(value, str):
                entry["required_models"].append(dict(node_type=v["class_type"], field=key, value=value))
    (output / "source.workflow.json").write_bytes(original)
    (output / "workflow.api.json").write_bytes(prepared)
    (output / "manifest.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "preparation.json").write_text(json.dumps(dict(source=str(SOURCE / entry["source"]), changes=entry["changes"], source_hash=entry["source_hash"], template_hash=entry["template_hash"]), ensure_ascii=False, indent=2), encoding="utf-8")
    print(entry["id"], len(api), "nodes")
