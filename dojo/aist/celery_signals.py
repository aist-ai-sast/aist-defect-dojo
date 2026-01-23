from __future__ import annotations

import uuid

from celery.signals import worker_process_init
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from dojo.aist.actions import build_one_off_action, get_action_handler
from dojo.aist.models import (
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    PipelineLaunchQueue,
    ProcessedFinding,
    TestDeduplicationProgress,
    VersionType,
)
from dojo.aist.signals import pipeline_status_changed
from dojo.models import Finding, Test
from dojo.signals import finding_deduplicated


@receiver(post_save, sender=AISTProject, dispatch_uid="aistproject_autoversion_master")
def create_default_master_version(sender, instance: AISTProject, created: bool, **kwargs):  # noqa: FBT001
    if not created:
        return

    def _create_if_absent():
        if AISTProjectVersion.objects.filter(project=instance).exists():
            return

        repo = getattr(instance, "repository", None)
        default_branch = "master"

        if repo:
            binding = repo.get_binding()
            if binding is not None:
                project_info = binding.get_project_info(repo)
                default_branch = project_info.get("default_branch", "master")

        AISTProjectVersion.objects.get_or_create(
            project=instance,
            version=default_branch,
            defaults={"version_type": VersionType.GIT_HASH},
        )

    transaction.on_commit(_create_if_absent)


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
        group, group_created = TestDeduplicationProgress.objects.get_or_create(test=instance)
        if group_created:
            now = timezone.now()
            group.started_at = now
            group.last_progress_at = now
            group.save(update_fields=["started_at", "last_progress_at"])
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


def _get_launch_config_id_from_pipeline(pipeline: AISTPipeline) -> int | None:
    launch_config_id = (pipeline.launch_data or {}).get("launch_config_id")
    if launch_config_id:
        return int(launch_config_id)

    queue_item = (
        PipelineLaunchQueue.objects
        .filter(pipeline_id=pipeline.id)
        .order_by("-created")
        .first()
    )
    if queue_item:
        return queue_item.launch_config_id
    return None


def _update_action_run(
    pipeline_id: str,
    *,
    key: str,
    action_type: str,
    trigger_status: str,
    source: str,
    status: str,
    error: str | None = None,
) -> None:
    with transaction.atomic():
        locked = AISTPipeline.objects.select_for_update().filter(id=pipeline_id).first()
        if not locked:
            return
        launch_data = locked.launch_data or {}
        runs = launch_data.get("action_runs") or []
        updated_at = timezone.now().isoformat()

        payload = {
            "key": key,
            "action_type": action_type,
            "trigger_status": trigger_status,
            "source": source,
            "status": status,
            "error": error or "",
            "updated_at": updated_at,
        }

        existing = next((r for r in runs if r.get("key") == key), None)
        if existing:
            existing.update(payload)
        else:
            runs.append(payload)

        launch_data["action_runs"] = runs
        locked.launch_data = launch_data
        locked.save(update_fields=["launch_data", "updated"])


def _mark_one_off_done(pipeline_id: str, action_id: str) -> None:
    with transaction.atomic():
        locked = AISTPipeline.objects.select_for_update().filter(id=pipeline_id).first()
        if not locked:
            return
        launch_data = locked.launch_data or {}
        done_ids = set(launch_data.get("one_off_actions_done") or [])
        done_ids.add(action_id)
        launch_data["one_off_actions_done"] = list(done_ids)
        locked.launch_data = launch_data
        locked.save(update_fields=["launch_data"])


@receiver(pipeline_status_changed)
def on_pipeline_status_changed(sender, pipeline_id=None, old_status=None, new_status=None, **kwargs):
    if not pipeline_id or not new_status:
        return

    pipeline = AISTPipeline.objects.filter(id=pipeline_id).first()
    if not pipeline:
        return

    launch_config_id = _get_launch_config_id_from_pipeline(pipeline)
    if launch_config_id:
        actions = AISTLaunchConfigAction.objects.filter(
            launch_config_id=launch_config_id,
            trigger_status=new_status,
        )
        for action in actions:
            key = f"lc:{action.id}:{new_status}"
            _update_action_run(
                pipeline_id=pipeline_id,
                key=key,
                action_type=action.action_type,
                trigger_status=new_status,
                source="launch_config",
                status="pending",
            )
            handler = get_action_handler(action)
            if not handler:
                _update_action_run(
                    pipeline_id=pipeline_id,
                    key=key,
                    action_type=action.action_type,
                    trigger_status=new_status,
                    source="launch_config",
                    status="failed",
                    error="Handler not found",
                )
                continue
            try:
                handler.run(pipeline=pipeline, new_status=new_status)
                _update_action_run(
                    pipeline_id=pipeline_id,
                    key=key,
                    action_type=action.action_type,
                    trigger_status=new_status,
                    source="launch_config",
                    status="performed",
                )
            except Exception as exc:
                _update_action_run(
                    pipeline_id=pipeline_id,
                    key=key,
                    action_type=action.action_type,
                    trigger_status=new_status,
                    source="launch_config",
                    status="failed",
                    error=str(exc),
                )

    # One-off actions stored on pipeline.launch_data
    locked = AISTPipeline.objects.filter(id=pipeline_id).first()
    if not locked:
        return

    launch_data = locked.launch_data or {}
    one_off_actions = launch_data.get("one_off_actions") or []
    done_ids = set(launch_data.get("one_off_actions_done") or [])

    for action_payload in one_off_actions:
        if not isinstance(action_payload, dict):
            continue
        if action_payload.get("trigger_status") != new_status:
            continue
        action_id = action_payload.get("id")
        if not action_id:
            action_id = uuid.uuid4().hex
            action_payload["id"] = action_id
        if action_id in done_ids:
            continue

        key = f"one_off:{action_id}:{new_status}"
        action_obj = build_one_off_action(action_payload)
        if not action_obj:
            _update_action_run(
                pipeline_id=pipeline_id,
                key=key,
                action_type=action_payload.get("action_type") or "",
                trigger_status=new_status,
                source="one_off",
                status="failed",
                error="Invalid action payload",
            )
            _mark_one_off_done(pipeline_id, action_id)
            continue

        _update_action_run(
            pipeline_id=pipeline_id,
            key=key,
            action_type=action_obj.action_type,
            trigger_status=new_status,
            source="one_off",
            status="pending",
        )
        handler = get_action_handler(action_obj)
        if not handler:
            _update_action_run(
                pipeline_id=pipeline_id,
                key=key,
                action_type=action_obj.action_type,
                trigger_status=new_status,
                source="one_off",
                status="failed",
                error="Handler not found",
            )
            _mark_one_off_done(pipeline_id, action_id)
            continue
        try:
            handler.run(pipeline=locked, new_status=new_status)
            _update_action_run(
                pipeline_id=pipeline_id,
                key=key,
                action_type=action_obj.action_type,
                trigger_status=new_status,
                source="one_off",
                status="performed",
            )
        except Exception as exc:
            _update_action_run(
                pipeline_id=pipeline_id,
                key=key,
                action_type=action_obj.action_type,
                trigger_status=new_status,
                source="one_off",
                status="failed",
                error=str(exc),
            )
        _mark_one_off_done(pipeline_id, action_id)
