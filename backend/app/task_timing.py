"""Read execution times without counting queue waits or history collection time."""

import math
from datetime import datetime, timezone

TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted", "unknown"}
END_EVENTS = {"execution_success", "execution_error", "execution_interrupted"}


def event_time(value):
    """ComfyUI event timestamps are Unix milliseconds, including in saved history."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat() if value > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


def history_times(history):
    starts, finishes = [], []
    for message in (history or {}).get("status", {}).get("messages", []):
        if not isinstance(message, (list, tuple)) or len(message) != 2 or not isinstance(message[1], dict):
            continue
        kind, data = message
        timestamp = event_time(data.get("timestamp"))
        if timestamp and kind == "execution_start":
            starts.append(timestamp)
        elif timestamp and kind in END_EVENTS:
            finishes.append(timestamp)
    start = min(starts) if starts else None
    finish = max(finishes) if finishes else None
    if start and finish and finish < start:
        finish = None
    return start, finish


def execution_timing(execution):
    start, finish = history_times(execution.history)
    source = "history" if start and finish else "observed"
    start = start or execution.started_at
    terminal = execution.execution_state in TERMINAL_STATES
    # For lost evidence, finished_at is the time we stopped reconciling, not proof of completion.
    finish = finish or (
        execution.finished_at if terminal and execution.execution_state not in {"unknown", "interrupted"} else None
    )
    running = bool(start and not finish and not terminal)
    seconds = None
    if start and (finish or running):
        seconds = max(
            0,
            (datetime.fromisoformat(finish) if finish else datetime.now(timezone.utc)).timestamp()
            - datetime.fromisoformat(start).timestamp(),
        )
    # An execution cancelled/rejected before submission has consumed no generation time.
    never_started = (
        not start
        and not execution.submitted_payload
        and not execution.history
        and (
            execution.execution_state in {"pending", "blocked", "waiting_external", "preparing", "cancelled", "failed"}
        )
    )
    if never_started:
        seconds = 0
    return {
        "started_at": start,
        "finished_at": finish,
        "elapsed_seconds": seconds,
        "is_running": running,
        "source": source if start else "unavailable",
        "incomplete": (terminal and seconds is None) or execution.execution_state in {"unknown", "interrupted"},
    }
