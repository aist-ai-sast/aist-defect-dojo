import os
from math import ceil
from typing import Any

from celery import chain, chord, shared_task
from django.db import transaction

from dojo.aist.link_builder import LinkBuilder
from dojo.aist.logging_transport import _install_db_logging, get_redis
from dojo.aist.models import AISTPipeline, AISTStatus
from dojo.models import DojoMeta, Finding, Test

from .dedup import watch_deduplication


@shared_task(bind=True)
def report_enrich_done(self, result: int, pipeline_id: str):
    redis = get_redis()
    key = f"aist:progress:{pipeline_id}:enrich"
    redis.hincrby(key, "done", 1)
    return result


@shared_task(name="dojo.aist.after_upload_enrich_and_watch")
def after_upload_enrich_and_watch(results: list[int],
                                  pipeline_id: str,
                                  test_ids: list[int],
                                  log_level) -> None:
    logger = _install_db_logging(pipeline_id, log_level)
    enriched = sum(int(v or 0) for v in results)

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)

        if test_ids:
            tests = list(Test.objects.filter(id__in=test_ids))
            pipeline.tests.set(tests, clear=True)

        pipeline.status = AISTStatus.WAITING_DEDUPLICATION_TO_FINISH
        pipeline.save(update_fields=["status", "updated"])

    logger.info("Enrichment finished: %s findings enriched. Waiting for deduplication.", enriched)
    res = watch_deduplication.delay(pipeline_id=pipeline_id, log_level=log_level)

    with transaction.atomic():
        pipeline.watch_dedup_task_id = res.id
        pipeline.save(update_fields=["watch_dedup_task_id", "updated"])


@shared_task(bind=False)
def enrich_finding_task(
    finding_id: int,
    trim_path: str,
    project_version_descriptor: dict[str, Any],
) -> int:
    """Enrich a single finding by trimming its file path and attaching a source link."""
    try:
        f = Finding.objects.select_related("test__engagement").get(id=finding_id)
    except Finding.DoesNotExist:
        return 0
    else:
        try:
            file_path = f.file_path or ""
            if trim_path and file_path.startswith(trim_path):
                tp = trim_path if trim_path.endswith("/") else trim_path + "/"
                f.file_path = file_path.replace(tp, "")
                f.save(update_fields=["file_path"])
                file_path = f.file_path

            linker = LinkBuilder(project_version_descriptor)
            link = linker.build(file_path)
            if not link:
                return 0
            acceptable = not linker.contains_excluded_path(link)
            if acceptable:
                DojoMeta.objects.update_or_create(
                    finding=f,
                    name="sourcefile_link",
                    value=link,
                )
            else:
                f.delete()
            return 1

        except Exception:
            return 0


@shared_task(bind=False)
def enrich_finding_batch(
    finding_ids: list[int],
    trim_path: str,
    project_version_descriptor: dict[str, Any],
) -> int:
    processed = 0
    for fid in finding_ids:
        try:
            processed += int(enrich_finding_task.run(fid, trim_path, project_version_descriptor) or 0)
        except Exception:  # noqa: S112
            continue
    return processed


def make_enrich_chord(
    *,
    finding_ids: list[int],
    trim_path: str,
    pipeline_id: str,
    test_ids: list[int],
    log_level: str,
    project_version_descriptor: dict[str, Any],
):
    """
    Build a Celery chord that:
      1) splits findings into K chunks (K ~= number of active workers),
      2) runs one batch task per chunk,
      3) increments progress by the processed count per chunk,
      4) aggregates results in the chord body.

    Returns:
        celery.canvas.Signature: A chord signature ready to dispatch/replace.

    """
    workers = int(os.getenv("DD_CELERY_WORKER_AUTOSCALE_MAX", "4") or 4)
    logger = _install_db_logging(pipeline_id, log_level)
    logger.info(f"Number of workers for enrichment available: {workers}")

    # Edge case: no findings -> return body-only path (caller's code can skip).
    total = len(finding_ids)
    if total == 0:
        return after_upload_enrich_and_watch.s(pipeline_id, test_ids, log_level)

    # 2) Compute number of chunks and perform the split.
    #    We never create more chunks than findings or workers.
    k = max(1, min(workers, total))
    chunk_size = ceil(total / k)
    chunks = [finding_ids[i: i + chunk_size] for i in range(0, total, chunk_size)]

    # 3) Initialize progress in Redis (total = number of findings, done = 0).
    #    report_enrich_done will HINCRBY "done" by the processed count for each chunk.
    redis = get_redis()
    redis.hset(f"aist:progress:{pipeline_id}:enrich", mapping={"total": total, "done": 0})
    logger.info(f"Passing project_version_descriptor {project_version_descriptor}")

    # 4) Build the chord header: one batch chain per chunk.
    header = [
        chain(
            enrich_finding_batch.s(chunk, trim_path, project_version_descriptor),
            report_enrich_done.s(pipeline_id),
        )
        for chunk in chunks
    ]

    # 5) Build the chord body, which already sums the batch results and continues the pipeline.
    body = after_upload_enrich_and_watch.s(pipeline_id, test_ids, log_level)

    # 6) Return the chord signature. The caller can `raise self.replace(sig)`.
    return chord(header, body)
