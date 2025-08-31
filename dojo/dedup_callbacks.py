"""
Simple optional webhook emitter for deduplication completion.
Wire this from DeduplicationTaskGroup.mark_complete_if_finished().
"""
from __future__ import annotations
import json
import socket
import urllib.request
from typing import Optional

def emit_webhook(url: str, payload: dict, timeout: float = 5.0) -> Optional[str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as e:
        # best-effort notify; don't crash background worker
        print("[dedupe-webhook] failed:", e)
        return None

def emit_on_completion(webhook_url: str, *, test_import_id: int, test_id: int, product_id: int) -> None:
    payload = {
        "event": "deduplication_complete",
        "test_import_id": test_import_id,
        "test_id": test_id,
        "product_id": product_id,
        "host": socket.gethostname(),
    }
    emit_webhook(webhook_url, payload)
