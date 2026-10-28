import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.errors import AppError
from app.task_identity import execution_identity

ROOT = Path(__file__).resolve().parents[2]


def tool_definition(tool_id):
    folder = ROOT / "tool_registry" / tool_id / "1.0.0"
    tool = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    tool["template"] = json.loads((folder / "workflow.api.json").read_text(encoding="utf-8"))
    return tool


def execution(tool, **values):
    return SimpleNamespace(
        **{
            "tool_id": tool["id"],
            "tool_version": tool["version"],
            "executed_payload": None,
            "submitted_payload": None,
            "history": None,
            "parameters": {},
            "configuration": {"template_hash": tool["template_hash"]},
            **values,
        }
    )


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tool = tool_definition("image.generate.zimage")
        self.registry = SimpleNamespace(get=lambda tool_id, version: self.tool)

    def names(self, identity):
        return [model["name"] for model in identity["models"]]

    def test_executed_models_override_submitted_and_current_defaults(self):
        graph = copy.deepcopy(self.tool["template"])
        graph["2"]["inputs"]["unet_name"] = "historical-model.safetensors"
        identity = execution_identity(
            execution(
                self.tool,
                executed_payload=graph,
                submitted_payload={"prompt": self.tool["template"]},
            ),
            self.registry,
        )
        self.assertEqual(identity["tool_name"], "快速文生图")
        self.assertEqual(identity["model_source"], "executed")
        self.assertEqual(identity["models"][0], {"role": "main", "name": "historical-model.safetensors"})
        self.assertNotIn("z_image_turbo_bf16.safetensors", self.names(identity))

    def test_saved_history_without_executed_payload_is_supported(self):
        identity = execution_identity(
            execution(
                self.tool,
                history={
                    "prompt": [None, None, self.tool["template"]],
                    "outputs": {"12": {}},
                },
            ),
            self.registry,
        )
        self.assertEqual(identity["model_source"], "executed")
        self.assertIn("z_image_turbo_bf16.safetensors", self.names(identity))

    def test_submission_is_labelled_separately(self):
        identity = execution_identity(
            execution(
                self.tool,
                submitted_payload={"prompt": self.tool["template"]},
            ),
            self.registry,
        )
        self.assertEqual(identity["model_source"], "submitted")
        self.assertEqual(len(identity["models"]), 3)

    def test_unsubmitted_qwen_preset_does_not_list_inactive_lora_or_modify_template(self):
        tool = tool_definition("image.generate.qwen")
        original = copy.deepcopy(tool)
        registry = SimpleNamespace(get=lambda *_: tool)
        fast = execution_identity(execution(tool, parameters={"preset": "fast"}), registry)
        quality = execution_identity(execution(tool, parameters={"preset": "quality"}), registry)
        self.assertEqual(fast["model_source"], "planned")
        self.assertEqual(quality["model_source"], "planned")
        self.assertTrue(any(m["role"] == "lora" for m in fast["models"]))
        self.assertFalse(any(m["role"] == "lora" for m in quality["models"]))
        self.assertEqual(tool, original)

    def test_actual_qwen_quality_branch_excludes_lightning_lora(self):
        tool = tool_definition("image.generate.qwen")
        graph = copy.deepcopy(tool["template"])
        graph["11"]["inputs"]["value"] = False
        identity = execution_identity(execution(tool, executed_payload=graph), SimpleNamespace(get=lambda *_: tool))
        self.assertEqual(identity["model_source"], "executed")
        self.assertFalse(any(m["role"] == "lora" for m in identity["models"]))

    def test_missing_evidence_is_not_replaced_with_current_defaults(self):
        for config in ({}, {"template_hash": "different-version"}):
            identity = execution_identity(execution(self.tool, configuration=config), self.registry)
            self.assertEqual(identity["models"], [])
            self.assertEqual(identity["model_source"], "unavailable")

    def test_missing_tool_version_still_uses_saved_history(self):
        def missing(*_):
            raise AppError("tool_not_found", "工具版本不存在", 404)

        identity = execution_identity(
            execution(
                self.tool,
                history={
                    "prompt": [None, None, self.tool["template"]],
                    "outputs": {"12": {}},
                },
            ),
            SimpleNamespace(get=missing),
        )
        self.assertEqual(identity["tool_name"], self.tool["id"])
        self.assertEqual(identity["model_source"], "executed")
        self.assertIn("z_image_turbo_bf16.safetensors", self.names(identity))

    def test_pinned_tool_version_is_used_for_name(self):
        def get(tool_id, version):
            self.assertEqual((tool_id, version), (self.tool["id"], "1.0.0"))
            return self.tool

        execution_identity(execution(self.tool), SimpleNamespace(get=get))

    def test_all_registered_tools_report_main_and_supporting_models(self):
        for manifest in (ROOT / "tool_registry").glob("*/*/manifest.json"):
            tool = tool_definition(manifest.parent.parent.name)
            with self.subTest(tool=tool["id"]):
                identity = execution_identity(
                    execution(tool, executed_payload=tool["template"]), SimpleNamespace(get=lambda *_: tool)
                )
                self.assertTrue(any(m["role"] == "main" for m in identity["models"]))
                self.assertTrue(any(m["role"] == "vae" for m in identity["models"]))

    def test_disabled_lora_is_not_reported(self):
        tool = tool_definition("image.edit.qwen.fast")
        graph = copy.deepcopy(tool["template"])
        graph["10"]["inputs"]["strength_model"] = 0
        identity = execution_identity(execution(tool, executed_payload=graph), SimpleNamespace(get=lambda *_: tool))
        self.assertFalse(any(m["role"] == "lora" for m in identity["models"]))


if __name__ == "__main__":
    unittest.main()
