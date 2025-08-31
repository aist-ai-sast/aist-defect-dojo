import time
from dojo.aist.models import AISTPipeline, AISTStatus,TestDeduplicationProgress
from celery import shared_task
from dojo.aist.logging_transport import _install_db_logging
from django.db import transaction, OperationalError
from dojo.models import Test

@shared_task(bind=True)
def watch_deduplication(self, pipeline_id: str, log_level) -> None:
    """Monitor deduplication progress and finalise the pipeline.

    This task polls the ``Test`` model to determine when all imported
    tests have completed deduplication.  Once deduplication is
    finished the pipeline is marked as awaiting AI results and then
    immediately set to finished. Long running polling inside a
    Celery task is acceptable here because we perform a short sleep
    between checks and exit when complete.
    """
    pipeline = AISTPipeline.objects.get(id=pipeline_id)

    logger = _install_db_logging(pipeline_id, log_level)
    if not pipeline.tests.exists():
        pipeline.status = AISTStatus.FINISHED
        pipeline.save(update_fields=['status', 'updated'])
        logger.warning("No tests to wait")
        return
    # Poll until all deduplication flags are true
    try:
        while True:
                # Reload pipeline to capture any manual status change
                pipeline.refresh_from_db()
                # Exit early if user or another task finished the pipeline
                if pipeline.status == AISTStatus.FINISHED:
                    return
                # Query for tests still undergoing deduplication
                remaining = pipeline.tests.filter(deduplication_complete=False).count()
                if remaining > 0:
                    # Sleep for 10 seconds before checking again
                    time.sleep(3)
                    continue
                # TODO: set only if flag waiting for confirmation is set
                pipeline.status = AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI
                pipeline.save(update_fields=["status", "updated"])
                return

    except Exception as exc:
        logger.error("Exception while waiting for deduplication to finish: %s", exc)


@shared_task(
    name="dojo.aist.reconcile_deduplication",
    rate_limit="5/s",                    # throttle to protect the DB
    autoretry_for=(OperationalError,),   # retry on transient DB issues
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def reconcile_deduplication(batch_size: int = 200, max_runtime_s: int = 50) -> dict:
    """
    Periodically reconciles deduplication state for Tests whose deduplication is not complete.
    To handle concurrent runs safely, we take a row-level lock on the progress row and skip
    already-locked rows (skip_locked=True).
    """
    start = time.time()
    processed = 0

    # Single read to fetch candidate Tests (with the FK preloaded for convenience).
    tests = (
        Test.objects
        .filter(deduplication_complete=False)
        .select_related("dedupe_progress")
        .order_by("updated" if hasattr(Test, "updated") else "id")[:batch_size]
    )

    for test in tests:
        if time.time() - start > max_runtime_s:
            break

        try:
            prog = test.dedupe_progress
        except TestDeduplicationProgress.DoesNotExist:
            prog = None

        if prog is None:
            continue  # no progress row yet — a later run can create/handle it

        # Strict per-item reconciliation with row lock on the progress record.
        with transaction.atomic():
            # Lock just this progress row; if another worker already locked it,
            # skip it now to avoid contention and try others.
            locked_prog = (
                TestDeduplicationProgress.objects
                .select_for_update(skip_locked=True)
                .get(pk=prog.pk)
            )

            # Idempotent operation: derives state from DB, safe to run multiple times.
            locked_prog.refresh_pending_tasks()
            processed += 1
    return {
        "processed": processed,
        "batch_size": batch_size,
        "duration_s": round(time.time() - start, 3),
    }