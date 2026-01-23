from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import tarfile
import zipfile
from contextlib import suppress
from datetime import datetime as dt
from pathlib import Path
from urllib.parse import quote

import requests
from asgiref.sync import async_to_sync
from croniter import croniter
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone
from django_github_app.models import Installation
from django_github_app.routing import GitHubRouter
from encrypted_model_fields.fields import EncryptedCharField

from dojo.models import Finding, Product, Test

_repo_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9._/\-]+$",
    message="Only letters, digits, dot, underscore, hyphen and slash are allowed.",
)

# --------- Error/validation messages ----------
ERR_FILEHASH_REQUIRES_SOURCE = "For FILE_HASH version type, source_archive is required."
ERR_VERSION_ALREADY_EXISTS = "This version already exists for the selected project."
ERR_UNSUPPORTED_ARCHIVE = "Unsupported archive format: not a ZIP or TAR.*"

gh = GitHubRouter()


class ScmType(models.TextChoices):
    GITHUB = "GITHUB", "Github"
    GITLAB = "GITLAB", "Gitlab"


class RepositoryInfo(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    type = models.CharField(max_length=64, choices=ScmType.choices, default=ScmType.GITHUB)
    repo_owner = models.CharField(max_length=100, validators=[_repo_part_validator])
    repo_name = models.CharField(max_length=100, validators=[_repo_part_validator])
    base_url = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["repo_owner", "repo_name", "type"])]

    def get_binding(self):
        mapping = {
            ScmType.GITHUB: "github_binding",
            ScmType.GITLAB: "gitlab_binding",
        }
        attr = mapping.get(self.type)
        return getattr(self, attr, None) if attr else None

    @property
    def clone_url(self) -> str:
        binding = self.get_binding()
        if binding:
            url = binding.build_clone_url(self)
            if url:
                return url
        return f"{self.host()}/{self.repo_full}.git"

    @property
    def repo_full(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    def host(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return "https://github.com" if self.type == ScmType.GITHUB else "https://gitlab.com"


class ScmGithubBinding(models.Model):

    """GitHub-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="github_binding")
    installation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    base_api_url = models.CharField(max_length=255, blank=True, default="")  # e.g. https://github.mycorp.com/api/v3

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        # sync option
        logger = logging.getLogger("dojo.aist")
        if not self.installation_id:
            logger.warning("No installation ID for GitHub binding")
            return None
        inst = Installation.objects.filter(installation_id=self.installation_id).first()
        if not inst:
            logger.warning("No installation object for GitHub binding")
            return None
        token = inst.get_access_token()
        return f"{self.host(scm).replace('https://', 'https://x-access-token:' + token + '@')}/{scm.repo_full}.git"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://github.com/owner/repo/blob/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/{scm.repo_full}/blob/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://raw.githubusercontent.com/owner/repo/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        host = self.host(scm)
        if "github.com" in host:
            raw_host = "https://raw.githubusercontent.com"
        else:
            return f"{host.rstrip('/')}/{scm.repo_full}/raw/{ref}/{fp}"
        return f"{raw_host.rstrip('/')}/{scm.repo_full}/{ref}/{fp}"

    def get_auth_headers(self) -> dict[str, str]:
        if not self.installation_id:
            return {}
        inst = Installation.objects.filter(installation_id=self.installation_id).first()
        if not inst:
            return {}
        token = inst.get_access_token()
        return {"Authorization": f"token {token}"}

    def get_project_info(self, scm: RepositoryInfo):
        logger = logging.getLogger("dojo.aist")

        owner = scm.repo_owner
        name = scm.repo_name

        try:
            data = async_to_sync(gh.getitem)(f"/repos/{owner}/{name}")
        except Exception:
            logger.exception("Failed to fetch repo data from GitHubRouter for %s/%s", owner, name)
            return None

        return data


class ScmGitlabBinding(models.Model):

    """GitLab-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="gitlab_binding")
    # just stub
    personal_access_token = EncryptedCharField(max_length=255, blank=True, default="")  # TODO: change to vault
    # or: ci_job_token = models.CharField(...), oauth_app_id, oauth_secret, и т.п.

    def host(self, scm: RepositoryInfo) -> str:
        return scm.host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        token = (self.personal_access_token or "").strip()
        if not token:
            return None
        # GitLab HTTPS clone with PAT:
        # https://oauth2:<PAT>@gitlab.com/owner/repo.git
        return f"{self.host(scm).replace('https://', 'https://oauth2:' + token + '@')}/{scm.repo_full}.git"

    def build_blob_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        # https://gitlab.com/group/repo/-/blob/<ref>/<path>
        fp = path.lstrip("/").replace("\\", "/")
        return f"{self.host(scm).rstrip('/')}/{scm.repo_full}/-/blob/{ref}/{fp}"

    def build_raw_url(self, scm: RepositoryInfo, ref: str, path: str) -> str:
        """Return GitLab API v4 raw-file URL (no redirects, works with PRIVATE-TOKEN)."""
        fp = quote(path.lstrip("/").replace("\\", "/"), safe="")          # encode path
        proj = quote(scm.repo_full, safe="")                               # encode owner/repo
        ref_q = quote(ref or "master", safe="")                            # encode ref
        base = scm.host()                                                 # e.g. https://gitlab.com
        api_base = f"{base}/api/v4"
        return f"{api_base}/projects/{proj}/repository/files/{fp}/raw?ref={ref_q}"

    def get_auth_headers(self) -> dict[str, str]:
        """Return API auth header for GitLab."""
        tok = (self.personal_access_token or "").strip()
        return {"PRIVATE-TOKEN": tok} if tok else {}

    def get_project_info(self, scm: RepositoryInfo):
        logger = logging.getLogger("dojo.aist")
        base = self.host(scm).rstrip("/")
        api_base = f"{base}/api/v4"

        proj = quote(scm.repo_full, safe="")

        url = f"{api_base}/projects/{proj}"

        headers = self.get_auth_headers()

        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=10,
            )
        except Exception:
            logger.exception("Failed to query GitLab API for default branch of %s", scm.repo_full)
            return None

        if resp.status_code != 200:
            logger.warning(
                "GitLab API %s returned %s when requesting default branch for %s",
                url,
                resp.status_code,
                scm.repo_full,
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("GitLab API %s returned non-JSON response for %s", url, scm.repo_full)
            return None
        return data


class PullRequest(models.Model):
    project_version = models.ForeignKey(
        "AISTProjectVersion",
        on_delete=models.CASCADE,
        related_name="pull_requests",
    )

    repository = models.ForeignKey(
        RepositoryInfo,
        on_delete=models.CASCADE,
        related_name="pull_requests",
    )

    pr_number = models.PositiveIntegerField()

    base_ref = models.CharField(max_length=255, blank=True)
    head_ref = models.CharField(max_length=255, blank=True)
    is_from_fork = models.BooleanField(default=False)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project_version", "repository", "pr_number"],
                name="uniq_pr_per_project_version",
            ),
        ]
        indexes = [
            models.Index(fields=["repository", "pr_number"]),
        ]

    def __str__(self):
        return f"{self.repository.repo_full}#{self.pr_number}->PV:{self.project_version_id}"


class AISTStatus(models.TextChoices):
    SAST_LAUNCHED = "SAST_LAUNCHED", "Launched"
    UPLOADING_RESULTS = "UPLOADING_RESULTS", "Uploading Results"
    FINDING_POSTPROCESSING = "FINDING_POSTPROCESSING", "Finding post-processing"
    WAITING_DEDUPLICATION_TO_FINISH = "WAITING_DEDUPLICATION_TO_FINISH", "Waiting Deduplication To Finish"
    WAITING_CONFIRMATION_TO_PUSH_TO_AI = "WAITING_CONFIRMATION_TO_PUSH_TO_AI", "Waiting Confirmation To Push to AI"
    PUSH_TO_AI = "PUSH_TO_AI", "Push to AI"
    WAITING_RESULT_FROM_AI = "WAITING_RESULT_FROM_AI", "Waiting Result From AI"
    FINISHED = "FINISHED", "Finished"


class Organization(models.Model):

    """
    Simple organization/group entity for AIST projects.
    One organization can have many AISTProject objects.
    """

    created = models.DateTimeField(auto_now_add=True, editable=False)
    updated = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")

    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class AISTProject(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    supported_languages = models.JSONField(default=list, blank=True)
    script_path = models.CharField(max_length=1024)
    compilable = models.BooleanField(default=False)
    profile = models.JSONField(default=dict, blank=True)
    repository = models.OneToOneField(
        RepositoryInfo,
        on_delete=models.CASCADE,
        related_name="project",
        null=True,
        blank=True,
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="projects",
        null=True,
        blank=True,
    )
    ai_default_filter = models.JSONField(null=True, blank=True, default=None)

    def __str__(self) -> str:
        return self.product.name

    def get_excluded_paths(self) -> list[str]:
        excluded_paths = []
        if self.profile:
            excluded_paths.extend(self.profile.get("paths", {}).get("exclude", []))
        return excluded_paths

    def get_launch_schedule(self) -> LaunchSchedule | None:
        try:
            return self.launch_schedule
        except LaunchSchedule.DoesNotExist:
            return None


class VersionType(models.TextChoices):
    GIT_HASH = "GIT_HASH", "Git commit/hash"
    FILE_HASH = "FILE_HASH", "File hash (uploaded archive)"


class AISTProjectVersion(models.Model):
    project = models.ForeignKey(
        AISTProject, on_delete=models.CASCADE, related_name="versions",
    )
    version = models.CharField(max_length=64, db_index=True)
    last_resolved_commit = models.CharField(max_length=40, blank=True, default="")
    last_resolved_at = models.DateTimeField(null=True, blank=True)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    updated = models.DateTimeField(auto_now=True)
    version_type = models.CharField(
        max_length=16,
        choices=VersionType.choices,
        default=VersionType.GIT_HASH,
        db_index=True,
    )

    def _upload_to(self, filename: str) -> str:
        return f"aist_versions/{self.project_id}/{timezone.now():%Y/%m/%d}/{filename}"

    source_archive = models.FileField(upload_to=_upload_to, null=True, blank=True)  # noqa: DJ012
    source_archive_sha256 = models.CharField(max_length=64, blank=True, null=True, default="")

    class Meta:  # noqa: DJ012
        constraints = [
            models.UniqueConstraint(
                fields=["project", "version"],
                name="uniq_project_version_per_project",
            ),
        ]
        ordering = ["-created"]

    def __str__(self):  # noqa: DJ012
        return f"{self.project_id}:{self.version}"

    def save(self, *args, **kwargs):  # noqa: DJ012
        if self.version_type == VersionType.FILE_HASH and not self.version:
            if self.source_archive:
                sha = self._compute_file_sha256()
                self.source_archive_sha256 = sha
                self.version = sha

        super().save(*args, **kwargs)

    def clean(self):
        if self.version_type == VersionType.FILE_HASH:
            if not self.source_archive:
                raise ValidationError(ERR_FILEHASH_REQUIRES_SOURCE)
            v = (self.version or "").strip()
            if v:
                exists = AISTProjectVersion.objects.filter(
                    project=self.project, version=v,
                ).exclude(pk=self.pk).exists()
                if exists:
                    raise ValidationError({"version": ERR_VERSION_ALREADY_EXISTS})

    def as_dict(self):
        return {
            "id": self.pk,
            "version": self.version,
            "type": str(self.version_type),
            "extracted_root": str(self.get_extracted_root()),
        }

    def _compute_file_sha256(self) -> str:
        h = hashlib.sha256()
        for chunk in self.source_archive.chunks():
            h.update(chunk)
        return h.hexdigest()

    def get_extracted_root(self) -> Path:
        """
        Folder, where the extracted archive is located.
        Example: MEDIA_ROOT/aist_versions_extracted/<project_version_id>/
        """
        media_root = Path(getattr(settings, "MEDIA_ROOT", "media"))
        return media_root / "aist_versions_extracted" / str(self.id)

    def _extraction_marker_path(self) -> Path:
        return self.get_extracted_root() / ".extracted.ok"

    def _needs_extraction(self) -> bool:
        marker = self._extraction_marker_path()
        if not marker.exists():
            return True
        try:
            txt = marker.read_text(encoding="utf-8").strip()
        except Exception:
            return True
        return txt != (self.source_archive_sha256 or "")

    def requested_ref(self) -> str:
        # single source of truth: this is what user asked (branch/tag/sha or file hash)
        return (self.version or "").strip()

    def is_git(self) -> bool:
        return self.version_type == VersionType.GIT_HASH

    def ensure_extracted(self) -> Path | None:
        """
        Ensure the uploaded archive is extracted under `get_extracted_root()`.

        - Idempotent: if marker exists and matches current SHA, skip work.
        - Secure extraction: delegates to _safe_extract_* helpers to prevent path traversal.
        - Post-process: if extraction yields exactly one top-level directory, flatten it.
        - Writes `.extracted.ok` containing the archive SHA so we can detect changes.
        """
        from dojo.aist.utils.archive import (  # noqa: PLC0415
            _flatten_single_root_directory,
            _safe_extract_tar_member,
            _safe_extract_zip_member,
        )

        root = self.get_extracted_root()
        root.mkdir(parents=True, exist_ok=True)

        if not self.source_archive:
            return None  # nothing to extract

        # If already extracted and SHA matches, return early
        if not self._needs_extraction():
            return root

        # Clean the extraction directory (except the directory itself)
        for p in root.glob("*"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with suppress(OSError):
                    p.unlink()

        # Read the file via storage (works with non-local backends too)
        with default_storage.open(self.source_archive.name, "rb") as f:
            data = f.read()
        bio = io.BytesIO(data)

        # Detect format and extract securely
        if zipfile.is_zipfile(bio):
            bio.seek(0)
            with zipfile.ZipFile(bio) as zf:
                for member in zf.infolist():
                    _safe_extract_zip_member(zf, member, root)
        else:
            bio.seek(0)
            try:
                with tarfile.open(fileobj=bio, mode="r:*") as tf:
                    for member in tf.getmembers():
                        _safe_extract_tar_member(tf, member, root)
            except tarfile.ReadError:
                raise ValueError(ERR_UNSUPPORTED_ARCHIVE)

        # Flatten "<archive_name>/" level if it is the only top-level entry
        _flatten_single_root_directory(root)

        # Write marker with current SHA to avoid repeated extractions
        (root / ".extracted.ok").write_text(self.source_archive_sha256 or "", encoding="utf-8")

        return root


class AISTPipeline(models.Model):
    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)
    started = models.DateTimeField(auto_now=True)

    id = models.CharField(primary_key=True, max_length=64)

    project = models.ForeignKey(AISTProject, on_delete=models.PROTECT, related_name="aist_pipelines")
    project_version = models.ForeignKey(
        AISTProjectVersion,
        on_delete=models.PROTECT,
        related_name="pipelines",
        db_index=True,
        null=True, blank=True,
    )
    resolved_commit = models.CharField(max_length=40, blank=True, default="")
    status = models.CharField(max_length=64, choices=AISTStatus.choices, default=AISTStatus.FINISHED)

    tests = models.ManyToManyField(Test, related_name="aist_pipelines", blank=True)
    launch_data = models.JSONField(default=dict, blank=True)

    run_task_id = models.CharField(max_length=64, null=True, blank=True)
    watch_dedup_task_id = models.CharField(max_length=64, null=True, blank=True)

    response_from_ai = models.JSONField(default=dict, blank=True)

    pull_request = models.ForeignKey(
        PullRequest,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="pipelines",
    )

    class Meta:
        ordering = ("-created",)

    def __str__(self) -> str:
        return f"SASTPipeline[{self.id}] {self.status}"


class TestDeduplicationProgress(models.Model):

    """Deduplication progress on one Test."""

    test = models.OneToOneField(
        Test, on_delete=models.CASCADE, related_name="dedupe_progress",
    )
    pending_tasks = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    deduplication_complete = models.BooleanField(default=False)
    last_progress_at = models.DateTimeField(null=True, blank=True)
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    reconcile_attempts = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["test", "pending_tasks"])]

    def __str__(self) -> str:
        return f"DeduplicationTaskGroup(test={self.test_id}, remaining={self.pending_tasks})"

    def mark_complete_if_finished(self) -> None:
        if self.pending_tasks == 0 and not self.deduplication_complete:
            self.deduplication_complete = True
            self.completed_at = timezone.now()
            self.save(update_fields=["deduplication_complete", "completed_at"])

    def refresh_pending_tasks(self) -> None:
        with transaction.atomic():
            group = (
                TestDeduplicationProgress.objects
                .select_for_update()
                .get(pk=self.pk)
            )
            now = timezone.now()
            fields_to_update = []

            if group.started_at is None:
                group.started_at = now
                fields_to_update.append("started_at")
            if group.last_progress_at is None:
                group.last_progress_at = now
                fields_to_update.append("last_progress_at")
            # test current findings
            qs_findings = Finding.objects.filter(test_id=group.test_id)

            # pending = findings, for which ProcessedFinding doesn't exist with same test_id and finding_id
            pending_qs = qs_findings.filter(
                ~models.Exists(
                    ProcessedFinding.objects.filter(
                        test_id=group.test_id,
                        finding_id=models.OuterRef("id"),
                    ),
                ),
            )

            pending = pending_qs.count()
            # completed if pending == 0 (even if 0/0)
            is_complete = (pending == 0)

            fields_to_update = []
            if group.pending_tasks != pending:
                group.pending_tasks = pending
                fields_to_update.append("pending_tasks")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")
            if group.deduplication_complete != is_complete:
                group.deduplication_complete = is_complete
                fields_to_update.append("deduplication_complete")
                group.last_progress_at = now
                if "last_progress_at" not in fields_to_update:
                    fields_to_update.append("last_progress_at")

            if fields_to_update:
                group.save(update_fields=fields_to_update)
            Test.objects.filter(id=group.test_id).update(
                deduplication_complete=is_complete,
            )


class ProcessedFinding(models.Model):

    """Set which findings are considered to avoid double decrement"""

    test = models.ForeignKey(Test, on_delete=models.CASCADE)
    finding = models.ForeignKey(Finding, null=True, blank=True,
                                on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test", "finding"],
                name="uniq_processed_test_finding_not_null",
                condition=models.Q(finding__isnull=False),
            ),
        ]
        indexes = [
            # to anti JOIN work fast
            models.Index(fields=["test", "finding"]),
        ]


class AISTAIResponse(models.Model):
    pipeline = models.ForeignKey(
        "AISTPipeline",
        on_delete=models.CASCADE,
        related_name="ai_responses",
        db_index=True,
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created"]  # last one is on top

    def __str__(self):
        return f"AIResponse[{self.pipeline_id}] @ {self.created:%Y-%m-%d %H:%M:%S}"


def _ensure_aware(value: dt) -> dt:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value


class LaunchSchedule(models.Model):
    cron_expression = models.CharField(
        max_length=100,
        help_text="Cron expression in standard 5-field format (e.g. '0 15 * * 1' for Mondays at 15:00).",
    )
    enabled = models.BooleanField(default=True)

    max_concurrent_per_worker = models.PositiveIntegerField(
        default=1,
        help_text="Maximum number of concurrent pipeline runs per worker for this schedule.",
    )

    launch_config = models.OneToOneField(
        "AISTProjectLaunchConfig",
        on_delete=models.CASCADE,
        related_name="launch_schedule",
        help_text="Anchor launch config. Project is derived from launch_config.project.",
    )

    last_run_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last time this schedule launched pipelines.",
    )

    class Meta:
        verbose_name = "Launch Schedule"
        verbose_name_plural = "Launch Schedules"

    def __str__(self) -> str:
        return (f"LaunchSchedule(project={self.launch_config.project_id}, "
                f"launch_config={self.launch_config_id}, cron={self.cron_expression})")

    def get_next_run_time(self, *, now=None):
        """
        Return the most recent scheduled tick time that is <= now (i.e. "due time").

        This avoids missing a tick when the scheduler task runs slightly позднее,
        e.g. tick at 12:55 but Celery beat triggers at 12:56.
        """
        now = now or timezone.now()
        if timezone.is_naive(now):
            now = timezone.make_aware(now, timezone.get_default_timezone())

        # The last scheduled time at or before "now"
        itr = croniter(self.cron_expression, now)
        due_time = itr.get_prev(dt)

        if timezone.is_naive(due_time):
            due_time = timezone.make_aware(due_time, timezone.get_default_timezone())

        return due_time

    def get_next_scheduled_time(self, *, now=None):
        """
        Return the next scheduled tick strictly after now.

        This is a UI/helper method and must not be used to decide "due".
        Scheduler semantics should keep using get_next_run_time() (prev <= now).
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        itr = croniter(self.cron_expression, now)
        nxt = itr.get_next(dt)
        return _ensure_aware(nxt)

    def preview_next_runs(self, *, count: int = 5, now=None) -> list[dt]:
        """
        Return next N scheduled ticks after now (strictly in the future).
        Used by UI preview; backend-only logic.
        """
        now = now or timezone.now()
        now = _ensure_aware(now)

        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 5

        if count < 1:
            return []
        count = min(count, 20)

        itr = croniter(self.cron_expression, now)
        out: list[dt] = []
        for _ in range(count):
            nxt = itr.get_next(dt)
            out.append(_ensure_aware(nxt))
        return out


class PipelineLaunchQueue(models.Model):

    """
    Queued pipeline launch request. Items are created by LaunchSchedule when a cron triggers,
    and later dispatched by the pipeline dispatcher respecting concurrency limits.
    """

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE)
    schedule = models.ForeignKey(
        LaunchSchedule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    launch_config = models.ForeignKey(
        "AISTProjectLaunchConfig",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="launch_queue_items",
        help_text="Launch config used to build pipeline_args snapshot.",
    )
    dispatched = models.BooleanField(default=False, db_index=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    pipeline = models.ForeignKey(
        "AISTPipeline",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="launch_queue_item",
    )

    class Meta:
        ordering = ["created"]

    def __str__(self):
        return f"LaunchQueue(project={self.project_id}, dispatched={self.dispatched})"


class AISTProjectLaunchConfig(models.Model):

    """
    Saved launch configuration ("preset") for a specific AISTProject.

    Stores PipelineArguments-like options excluding:
      - project_id (comes from FK)
      - project_version (chosen at run time)
    """

    project = models.ForeignKey(AISTProject, on_delete=models.CASCADE, related_name="launch_configs")

    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")

    # PipelineArguments-equivalent options (except project_id/project_version)
    params = models.JSONField(default=dict, blank=True)

    is_default = models.BooleanField(default=False)

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="uniq_aist_launch_cfg_name_per_project"),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:{self.name}"


class AISTLaunchConfigAction(models.Model):
    class ActionType(models.TextChoices):
        PUSH_TO_SLACK = "PUSH_TO_SLACK", "push_to_slack"
        SEND_EMAIL = "SEND_EMAIL", "send_email"
        WRITE_LOG = "WRITE_LOG", "write_log"

    launch_config = models.ForeignKey(
        AISTProjectLaunchConfig,
        on_delete=models.CASCADE,
        related_name="actions",
    )
    trigger_status = models.CharField(max_length=64, choices=AISTStatus.choices)
    action_type = models.CharField(max_length=32, choices=ActionType.choices)
    config = models.JSONField(default=dict, blank=True)
    secret_config = EncryptedCharField(max_length=4096, blank=True, default="")

    created = models.DateTimeField(default=timezone.now, editable=False)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["launch_config", "trigger_status", "action_type"],
                name="uniq_aist_launch_cfg_action",
            ),
        ]

    def __str__(self) -> str:
        return f"Action({self.launch_config_id}:{self.action_type}@{self.trigger_status})"

    def get_secret_config(self) -> dict:
        if not self.secret_config:
            return {}
        try:
            return json.loads(self.secret_config)
        except (TypeError, ValueError):
            return {}

    def set_secret_config(self, value: dict | None) -> None:
        data = value or {}
        self.secret_config = json.dumps(data, separators=(",", ":"))
