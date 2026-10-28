"""Describe the pinned tool and models from the execution's own workflow evidence."""

from .errors import AppError

MODEL_ROLES = {
    "unet_name": "main",
    "ckpt_name": "main",
    "model_name": "main",
    "clip_name": "encoder",
    "clip_name1": "encoder",
    "clip_name2": "encoder",
    "vae_name": "vae",
    "lora_name": "lora",
}


def workflow_graph(value):
    return (
        value
        if isinstance(value, dict) and any(isinstance(node, dict) and "class_type" in node for node in value.values())
        else None
    )


def active_nodes(graph, roots):
    """Follow output dependencies, resolving the registered Qwen preset's lazy switch."""
    visited = set()

    def visit(node_id):
        node_id = str(node_id)
        if node_id in visited or node_id not in graph:
            return
        visited.add(node_id)
        node = graph[node_id]
        if not isinstance(node, dict):
            return
        inputs = node.get("inputs", {})
        if node.get("class_type") == "ComfySwitchNode":
            switch = inputs.get("switch")
            if isinstance(switch, list) and len(switch) == 2:
                switch_node = graph.get(str(switch[0]), {})
                switch = switch_node.get("inputs", {}).get("value")
            if isinstance(switch, bool):
                inputs = {"selected": inputs.get("on_true" if switch else "on_false")}
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                visit(value[0])

    for root in roots:
        visit(root)
    return visited


def execution_identity(execution, registry):
    try:
        tool = registry.get(execution.tool_id, execution.tool_version)
    except AppError:
        tool = {}
    graph = workflow_graph(execution.executed_payload)
    history = execution.history or {}
    prompt = history.get("prompt", [])
    if not graph and isinstance(prompt, list) and len(prompt) > 2:
        graph = workflow_graph(prompt[2])
    source = "executed" if graph else "unavailable"
    if not graph:
        graph = workflow_graph((execution.submitted_payload or {}).get("prompt"))
        if graph:
            source = "submitted"
    if (
        not graph
        and tool.get("template_hash")
        and (execution.configuration.get("template_hash") == tool["template_hash"])
    ):
        graph = workflow_graph(tool.get("template"))
        if graph:
            source = "planned"
            # Do not modify the registered template while resolving an unsubmitted plan.
            if execution.tool_id == "image.generate.qwen":
                graph = {
                    **graph,
                    "11": {
                        "class_type": "PrimitiveBoolean",
                        "inputs": {
                            "value": execution.parameters.get("preset", "fast") == "fast",
                        },
                    },
                }
    models = []
    if graph:
        roots = list((history.get("outputs") or {}).keys()) or list(tool.get("roles", {}))
        # History's output-node list is available even if a tool version is no longer installed.
        selected = active_nodes(graph, roots) if roots else set(graph)
        seen = set()
        for node_id, node in graph.items():
            if node_id not in selected or not isinstance(node, dict):
                continue
            node_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            # A zero-strength LoRA does not contribute to the generated result.
            if "Lora" in node_type and inputs.get("strength_model") == 0 and inputs.get("strength_clip", 0) == 0:
                continue
            for field, value in inputs.items():
                role = MODEL_ROLES.get(field)
                if field == "model" and node_type in {"SeedVR2LoadDiTModel", "SeedVR2LoadVAEModel"}:
                    role = "vae" if "VAE" in node_type else "main"
                if not role or not isinstance(value, str) or not value.strip() or (role, value) in seen:
                    continue
                seen.add((role, value))
                models.append({"role": role, "name": value})
    models.sort(key=lambda model: {"main": 0, "lora": 1, "encoder": 2, "vae": 3}[model["role"]])
    return {"tool_name": tool.get("name", execution.tool_id), "models": models, "model_source": source}
