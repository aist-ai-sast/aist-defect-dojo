from __future__ import annotations

from celery.signals import worker_process_init
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from dojo.models import Finding, Test
from dojo.signals import finding_deduplicated

from .models import ProcessedFinding, TestDeduplicationProgress


@worker_process_init.connect
def setup_logging_on_worker(**kwargs):
    pass
    # install handler once per worker process
    # install_global_redis_log_handler()


@receiver(finding_deduplicated)
def on_finding_deduplicated(sender, finding_id=None, test=None, **kwargs):
    # Nothing to do if finding_id is missing
    if not finding_id:
        return

    # Prefer to extract test_id from the provided test object
    test_id = getattr(test, "id", None)

    if not test_id:
        return

    def do_create_processed_finding():
        if not Test.objects.filter(id=test_id).exists():
            return
        ProcessedFinding.objects.get_or_create(
            test_id=test_id,
            finding_id=finding_id,
        )

    # Creating of ProcessedFinding and group update after commit current transaction
    transaction.on_commit(do_create_processed_finding)

    def do_refresh():
        try:
            group = TestDeduplicationProgress.objects.get(test_id=test_id)
        except TestDeduplicationProgress.DoesNotExist:
            return
        group.refresh_pending_tasks()

    transaction.on_commit(do_refresh)

# Additional signals to manage dynamic refresh of pending tasks


@receiver(post_save, sender=Test)
def create_dedup_group_on_test_save(sender, instance, created, **kwargs):
    """
    Ensure that a DeduplicationTaskGroup exists for every Test.

    When a new Test is created the group is initialised and its pending
    tasks are computed. For existing tests this signal can also be
    triggered via bulk operations (e.g. updating fields) so we refresh
    the counters to stay in sync.
    """

    def do_refresh():
        group, _ = TestDeduplicationProgress.objects.get_or_create(test=instance)
        # Always recompute pending tasks to pick up any missing or newly added findings.
        group.refresh_pending_tasks()

    transaction.on_commit(do_refresh)


@receiver(post_save, sender=Finding)
def refresh_on_finding_save(sender, instance, created, **kwargs):
    """
    Recalculate pending tasks when a Finding is created or updated.

    We refresh the DeduplicationTaskGroup for the finding's test to account
    for new findings or changes to the test association.
    """
    test_id = instance.test_id
    if not test_id:
        return

    if not Test.objects.filter(id=test_id).exists():
        return

    def do_refresh():
        group, _ = TestDeduplicationProgress.objects.get_or_create(test_id=test_id)
        group.refresh_pending_tasks()

    transaction.on_commit(do_refresh)


@receiver(post_delete, sender=Finding)
def refresh_on_finding_delete(sender, instance, **kwargs):
    """
    Update pending tasks when a Finding is deleted.

    Deleted findings should no longer contribute to the total number of tasks.
    """
    test_id = instance.test_id
    if not test_id or not Test.objects.filter(id=test_id).exists():
        return

    def do_refresh():
        try:
            group = TestDeduplicationProgress.objects.get(test_id=test_id)
        except TestDeduplicationProgress.DoesNotExist:
            return
        group.refresh_pending_tasks()

    transaction.on_commit(do_refresh)
