from __future__ import annotations

import hashlib
import io
import logging
import shutil
import tarfile
import zipfile
from contextlib import suppress
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone
from django_github_app.models import Installation

from dojo.models import Finding, Product, Test

_repo_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9._-]+$",
    message="Only letters, digits, dot, underscore and hyphen are allowed.",
)

# --------- Error/validation messages (TRY003/EM101) ----------
ERR_FILEHASH_REQUIRES_SOURCE = "For FILE_HASH version type, source_archive is required."
ERR_VERSION_ALREADY_EXISTS = "This version already exists for the selected project."
ERR_UNSUPPORTED_ARCHIVE = "Unsupported archive format: not a ZIP or TAR.*"


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

    def _get_binding(self):
        mapping = {
            ScmType.GITHUB: "github_binding",
            ScmType.GITLAB: "gitlab_binding",
        }
        attr = mapping.get(self.type)
        return getattr(self, attr, None) if attr else None

    @property
    def clone_url(self) -> str:
        binding = self._get_binding()
        if binding:
            url = binding.build_clone_url(self)
            if url:
                return url
        return f"{self._host()}/{self.repo_full}.git"

    @property
    def repo_full(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    def _host(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return "https://github.com" if self.type == ScmType.GITHUB else "https://gitlab.com"


class ScmGithubBinding(models.Model):

    """GitHub-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="github_binding")
    installation_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    base_api_url = models.CharField(max_length=255, blank=True, default="")  # e.g. https://github.mycorp.com/api/v3

    def _host(self, scm: RepositoryInfo) -> str:
        return scm._host()

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
        return f"{self._host(scm).replace('https://', 'https://x-access-token:' + token + '@')}/{scm.repo_full}.git"


class ScmGitlabBinding(models.Model):

    """GitLab-specific binding for ScmInfo."""

    scm = models.OneToOneField(RepositoryInfo, on_delete=models.CASCADE, related_name="gitlab_binding")
    # just stub
    personal_access_token = models.CharField(max_length=255, blank=True, default="")  # TODO: change to vault
    # or: ci_job_token = models.CharField(...), oauth_app_id, oauth_secret, и т.п.

    def _host(self, scm: RepositoryInfo) -> str:
        return scm._host()

    def build_clone_url(self, scm: RepositoryInfo) -> str | None:
        token = (self.personal_access_token or "").strip()
        if not token:
            return None
        # GitLab HTTPS clone with PAT:
        # https://oauth2:<PAT>@gitlab.com/owner/repo.git
        return f"{self._host(scm).replace('https://', 'https://oauth2:' + token + '@')}/{scm.repo_full}.git"


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

    def __str__(self) -> str:
        return self.product.name


class VersionType(models.TextChoices):
    GIT_HASH = "GIT_HASH", "Git commit/hash"
    FILE_HASH = "FILE_HASH", "File hash (uploaded archive)"


class AISTProjectVersion(models.Model):
    project = models.ForeignKey(
        AISTProject, on_delete=models.CASCADE, related_name="versions",
    )
    version = models.CharField(max_length=64, db_index=True)
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
            "extracted_root": self.get_extracted_root(),
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

    def ensure_extracted(self) -> Path | None:
        """
        Ensure the uploaded archive is extracted under `get_extracted_root()`.

        - Idempotent: if marker exists and matches current SHA, skip work.
        - Secure extraction: delegates to _safe_extract_* helpers to prevent path traversal.
        - Post-process: if extraction yields exactly one top-level directory, flatten it.
        - Writes `.extracted.ok` containing the archive SHA so we can detect changes.
        """
        from dojo.aist.utils import (  # noqa: PLC0415
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
    status = models.CharField(max_length=64, choices=AISTStatus.choices, default=AISTStatus.FINISHED)

    tests = models.ManyToManyField(Test, related_name="aist_pipelines", blank=True)
    launch_data = models.JSONField(default=dict, blank=True)
    logs = models.TextField(default="", blank=True)

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

    def append_log(self, line: str) -> None:
        txt = self.logs or ""
        if not line.endswith("\n"):
            line += "\n"
        self.logs = txt + line
        self.save(update_fields=["logs", "updated"])


class TestDeduplicationProgress(models.Model):

    """Deduplication progress on one Test."""

    test = models.OneToOneField(
        Test, on_delete=models.CASCADE, related_name="dedupe_progress",
    )
    pending_tasks = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    deduplication_complete = models.BooleanField(default=False)
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
            if group.deduplication_complete != is_complete:
                group.deduplication_complete = is_complete
                fields_to_update.append("deduplication_complete")

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
