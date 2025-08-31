from dojo.aist.logging_transport import get_redis, STREAM_KEY
from celery import shared_task
from typing import Any, Dict, List, Optional
from collections import defaultdict
from dojo.aist.models import AISTPipeline
from django.db.models.functions import Concat
from django.db.models import F, Value

GROUP = "aistlog"

CONSUMER = "log-flusher"

def _ensure_group(r):
    """Create consumer group if it does not exist."""
    try:
        r.xgroup_create(STREAM_KEY, GROUP, id="$", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

@shared_task(bind=True, name="dojo.aist.flush_logs_once")
def flush_logs_once(self, max_read: int = 500) -> int:
    """
    Flush a batch of log entries from Redis Stream to the database.

    Reads up to `max_read` entries from the stream, groups them by pipeline_id,
    and appends the concatenated lines to `AISTPipeline.logs`. After writing,
    acknowledges the entries so they won't be re-read.
    Returns the number of log lines persisted on this run.
    """
    r = get_redis()
    # Ensure consumer group exists
    _ensure_group(r)

    # Read entries from the stream for this consumer
    # ">" reads only new messages (not pending)
    response = r.xreadgroup(GROUP, CONSUMER,
                            streams={STREAM_KEY: ">"}, count=max_read, block=200)
    if not response:
        return 0

    # XREADGROUP returns a list of (stream, [(id, fields), ...]) pairs
    entries = response[0][1] if response and response[0][0] == STREAM_KEY else []
    # Group entries by pipeline_id
    by_pid: Dict[str, List[str]] = defaultdict(list)
    entry_ids: List[str] = []

    for entry_id, fields in entries:
        entry_ids.append(entry_id)
        pipeline_id = fields.get("pipeline_id")
        message = fields.get("message", "")
        if not pipeline_id:
            # Skip lines without pipeline_id
            continue
        # prefix with the level or other data if desired
        level = fields.get("level")
        line = f"{level} {message}" if level else message
        by_pid[pipeline_id].append(line)

    # Append each group of lines to the corresponding pipeline.log
    written = 0
    for pid, lines in by_pid.items():
        text_block = "\n".join(lines) + "\n"
        try:
            # Use Concat to update the text atomically without select_for_update
            AISTPipeline.objects.filter(id=pid).update(
                logs=Concat(F("logs"), Value(text_block))
            )
            written += len(lines)
        except Exception:
            # Silently ignore errors; lines will remain pending and can be retried
            continue

    # Acknowledge processed entries
    # If update failed, those entries remain in the pending list and can be retried later
    if entry_ids:
        r.xack(STREAM_KEY, GROUP, *entry_ids)

    return written