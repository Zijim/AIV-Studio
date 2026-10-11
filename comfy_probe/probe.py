"""Read, monitor and preserve this investigation's ComfyUI jobs.

Run with F:/ComfyUI/mcp-env/Scripts/python.exe -X utf8.
No changes are made to ComfyUI configuration or existing workflows.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE = "http://127.0.0.1:8188"
ROOT = Path(__file__).resolve().parent


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get(route):
    r = requests.get(BASE + route, timeout=30)
    r.raise_for_status()
    return r.json()


def collect(prompt_id):
    history = get("/history/" + prompt_id)
    save(ROOT / "records" / (prompt_id + ".history.json"), history)
    entry = history.get(prompt_id)
    if not entry:
        print(json.dumps({"prompt_id": prompt_id, "history": "not_available"}))
        return
    files = []
    seen = set()
    for node_id, values in entry.get("outputs", {}).items():
        for kind, refs in values.items():
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if not isinstance(ref, dict) or "filename" not in ref:
                    continue
                key = (ref.get("type", "output"), ref.get("subfolder", ""), ref["filename"])
                if key in seen:
                    continue
                seen.add(key)
                target = ROOT / "outputs" / prompt_id / key[0] / key[1] / key[2]
                if not target.resolve().is_relative_to((ROOT / "outputs").resolve()):
                    raise ValueError("Output path escapes probe directory")
                try:
                    r = requests.get(BASE + "/view", params={"filename": key[2], "subfolder": key[1], "type": key[0]}, timeout=60)
                    r.raise_for_status()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(r.content)
                    files.append({"node_id": node_id, "kind": kind, "reference": ref, "path": str(target), "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest()})
                except Exception as e:
                    files.append({"node_id": node_id, "reference": ref, "save_error": str(e)})
    messages = entry.get("status", {}).get("messages", [])
    stamps = [(kind, data.get("timestamp")) for kind, data in messages if isinstance(data, dict) and "timestamp" in data]
    duration = (stamps[-1][1] - stamps[0][1]) / 1000 if len(stamps) >= 2 else None
    manifest = {"prompt_id": prompt_id, "status": entry.get("status"), "elapsed_seconds": duration, "files": files}
    save(ROOT / "records" / (prompt_id + ".manifest.json"), manifest)
    print(json.dumps(manifest, ensure_ascii=False))


def upload(path):
    path = Path(path)
    with path.open("rb") as f:
        r = requests.post(BASE + "/upload/image", files={"image": (path.name, f, "image/png")}, data={"type": "input", "subfolder": "AIV_probe_20261011", "overwrite": "false"}, timeout=30)
    r.raise_for_status()
    ref = r.json()
    save(ROOT / "records" / (path.stem + ".upload.json"), ref)
    print(json.dumps(ref, ensure_ascii=False))


def monitor(prompt_id, client_id, seconds):
    import websocket
    ws = websocket.create_connection("ws://127.0.0.1:8188/ws?" + urlencode({"clientId": client_id}), timeout=5)
    end = time.monotonic() + seconds
    log = ROOT / "records" / (prompt_id + ".events.jsonl")
    with log.open("a", encoding="utf-8") as out:
        while time.monotonic() < end:
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                if prompt_id in get("/history/" + prompt_id):
                    break
                continue
            if not isinstance(msg, str):
                continue
            event = json.loads(msg)
            data = event.get("data", {})
            if data.get("prompt_id") not in (None, prompt_id):
                continue
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
            out.flush()
            if event.get("type") in ("executing", "progress", "execution_error", "execution_interrupted", "execution_success"):
                print(json.dumps(event, ensure_ascii=False), flush=True)
            if event.get("type") in ("execution_success", "execution_error", "execution_interrupted"):
                break
    ws.close()
    collect(prompt_id)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["collect", "upload", "monitor", "queue"])
    p.add_argument("value", nargs="?")
    p.add_argument("--client-id", default="AIV-probe-monitor")
    p.add_argument("--seconds", type=int, default=1800)
    args = p.parse_args()
    if args.action == "collect":
        collect(args.value)
    elif args.action == "upload":
        upload(args.value)
    elif args.action == "monitor":
        monitor(args.value, args.client_id, args.seconds)
    else:
        print(json.dumps(get("/queue"), ensure_ascii=False))
