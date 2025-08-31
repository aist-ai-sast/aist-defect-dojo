# dojo/aist/logging_transport.py

import weakref
from django.db import transaction

from .models import AISTPipeline
import logging, json, time, redis
from functools import lru_cache
from logging import LoggerAdapter
from django.conf import settings

REDIS_URL = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
PUBSUB_CHANNEL_TPL = "aist:pipeline:{pipeline_id}:logs"
STREAM_KEY = "aist:logs"
BACKLOG_COUNT = 200

def get_redis():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


class StaticPipelineIdFilter(logging.Filter):
    """Inject fixed pipeline_id in every record if missing."""
    def __init__(self, pipeline_id: str) -> None:
        super().__init__()
        self.pipeline_id = str(pipeline_id)

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "pipeline_id"):
            record.pipeline_id = self.pipeline_id
        return True

class RedisLogHandler(logging.Handler):
    """Publish log records to Redis Pub/Sub and Stream."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            pid = getattr(record, "pipeline_id", None)
            payload = {
                "ts": time.time(),
                "level": record.levelname,
                "message": self.format(record),
                "pipeline_id": pid,
            }
            js = json.dumps(payload, ensure_ascii=False)
            r = get_redis()
            if pid:
                r.publish(PUBSUB_CHANNEL_TPL.format(pipeline_id=pid), js)
            r.xadd(STREAM_KEY, {
                "pipeline_id": pid or "",
                "level": payload["level"],
                "message": payload["message"],
                "ts": str(payload["ts"]),
            }, maxlen=100_000, approximate=True)
        except Exception:
            # never break main flow on logging error
            pass

def _attach_pipeline_filters(pipeline_id: str):
    """
    Set filter to 'dojo.aist' and 'pipeline',
    return detach(), which resets filter
    """
    f = StaticPipelineIdFilter(pipeline_id)
    aist_log = logging.getLogger("dojo.aist")
    pkg_log  = logging.getLogger("pipeline")

    aist_log.addFilter(f)
    pkg_log.addFilter(f)

    def detach() -> None:
        # try to detach
        try: aist_log.removeFilter(f)
        except Exception: pass
        try: pkg_log.removeFilter(f)
        except Exception: pass

    return detach

@lru_cache(maxsize=1)
def install_global_redis_log_handler(level=logging.INFO) -> None:
    """Install a single RedisLogHandler for this process."""
    root = logging.getLogger("dojo.aist")
    root.setLevel(level)
    if not any(isinstance(h, RedisLogHandler) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler = RedisLogHandler()
        handler.setLevel(level)
        handler.setFormatter(fmt)
        root.addHandler(handler)

    package_log = logging.getLogger("pipeline")
    package_log.setLevel(level)
    if not any(isinstance(h, RedisLogHandler) for h in package_log.handlers):
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler = RedisLogHandler()
        handler.setLevel(level)
        handler.setFormatter(fmt)
        package_log.addHandler(handler)

@lru_cache(maxsize=1024)
def get_pipeline_logger(pipeline_id: str) -> LoggerAdapter:
    """Return a logger that injects pipeline_id into all records."""
    base = logging.getLogger(f"dojo.aist.pipeline.{pipeline_id}")
    base.propagate = True
    return LoggerAdapter(base, {"pipeline_id": pipeline_id})

# def _install_db_logging(pipeline_id: str, level=logging.INFO):
#     install_global_redis_log_handler(level)
#     detach = _attach_pipeline_filters(pipeline_id)
#     return get_pipeline_logger(pipeline_id), detach


class PipelineScopedLogger:
    def __init__(self, pipeline_id: str, level=logging.INFO):
        install_global_redis_log_handler(level)
        self._detach = _attach_pipeline_filters(pipeline_id)
        self.logger = get_pipeline_logger(pipeline_id)
        self._closed = False

    def __enter__(self):
        return self.logger

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if not self._closed:
            try:
                self._detach()
            finally:
                self._closed = True

    def __del__(self):
        self.close()

# TODO: change to working Reddis solution, for now it's not working

class DatabaseLogHandler(logging.Handler):
    """
    Logging handler that writes log records into the AISTPipeline.logs field.

    This allows anything that logs through Python's logging to be displayed
    in the pipeline UI in near-real-time via the SSE endpoint.
    """
    def __init__(self, pipeline_id: str):
        super().__init__()
        self.pipeline_id = pipeline_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with transaction.atomic():
                p = AISTPipeline.objects.select_for_update().get(id=self.pipeline_id)
                p.append_log(msg)
        except Exception:
            # Never break the main flow due to logging issues
            pass

def _install_db_logging(pipeline_id: str, level=logging.INFO):
    """Connect BD -handler for all required loggers ко всем нужным логгерам (root и 'pipeline')."""
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    dbh = DatabaseLogHandler(pipeline_id)
    dbh.setLevel(level)
    dbh.setFormatter(fmt)

    plog = logging.getLogger("pipeline")
    plog.setLevel(level)
    plog.propagate = True
    if not any(isinstance(h, DatabaseLogHandler) and getattr(h, "pipeline_id", None) == pipeline_id
               for h in plog.handlers):
        plog.addHandler(dbh)

    root = logging.getLogger("dojo.aist.pipeline.{pipeline_id}")
    root.setLevel(level)
    if not any(isinstance(h, DatabaseLogHandler) and getattr(h, "pipeline_id", None) == pipeline_id
               for h in root.handlers):
        root.addHandler(dbh)
    return root
