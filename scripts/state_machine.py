from datetime import datetime, timezone


ALLOWED_TRANSITIONS = {
    "todo": {"active", "skipped", "skipped_no_indexer", "failed"},
    "added": {"active", "completed", "skipped_no_indexer", "failed"},
    "active": {"completed", "skipped_no_indexer", "failed"},
    "failed": {"todo", "skipped"},
    "completed": set(),
    "skipped": set(),
    "skipped_no_indexer": set(),
}


def transition_item(item, target, reason="", now=None):
    current = str(item.get("status", "todo") or "todo")
    target = str(target or "").strip()
    if target != current and target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"Invalid queue transition: {current} -> {target}")

    timestamp = now or datetime.now(timezone.utc).isoformat()
    item["status"] = target
    item["stateChangedAt"] = timestamp
    item["lastCheckedAt"] = timestamp
    if reason:
        item["error"] = str(reason)
    elif target in {"active", "completed"}:
        item["error"] = ""
    if target == "failed":
        item["retryCount"] = str(int(item.get("retryCount") or 0) + 1)
    if target in {"completed", "skipped", "skipped_no_indexer"}:
        item["nextRetryAt"] = ""
    return item
