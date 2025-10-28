from __future__ import annotations

import importlib
import logging
import os
import shutil
import socket
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from celery import states
from celery.result import AsyncResult
from django.conf import settings
from django.urls import reverse

from .models import AISTPipeline, AISTStatus

_logger = logging.getLogger(__name__)


def _flatten_single_root_directory(root: Path) -> None:
    """If extraction produced a single top-level directory, move its contents up one level and remove it."""
    if not root.exists():
        return

    # ignore marker file when counting entries
    entries = [p for p in root.iterdir() if p.name != ".extracted.ok"]

    # only one entry and it's a directory -> flatten
    if len(entries) == 1 and entries[0].is_dir():
        inner_dir = entries[0]
        # Move children one-by-one (safer than renaming the directory itself)
        for child in inner_dir.iterdir():
            target = root / child.name
            if target.exists():
                # On name collision we overwrite existing files/dirs.
                # If you prefer strict behavior, raise instead of removing.
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            shutil.move(str(child), str(target))
        # remove now-empty directory
        try:
            inner_dir.rmdir()
        except OSError:
            # If not empty for any reason, purge it
            shutil.rmtree(inner_dir, ignore_errors=True)


def _import_sast_pipeline_package():
    pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
    if not pipeline_path or not Path(pipeline_path).is_dir():
        msg = (
            "SAST pipeline code path is not configured or does not exist. "
            "Please set AIST_PIPELINE_CODE_PATH."
        )
        raise RuntimeError(msg)
    if pipeline_path not in sys.path:
        sys.path.append(pipeline_path)


# Must run before importing modules from the external "pipeline" package
_import_sast_pipeline_package()
from pipeline.docker_utils import cleanup_pipeline_containers  # type: ignore[import-not-found]  # noqa: E402


def has_unfinished_pipeline(project_version) -> bool:
    return (
        AISTPipeline.objects.filter(project_version=project_version)
        .exclude(status=AISTStatus.FINISHED)
        .exists()
    )


def create_pipeline_object(aist_project, project_version, pull_request):
    return AISTPipeline.objects.create(
        id=uuid.uuid4().hex[:8],
        project=aist_project,
        project_version=project_version,
        pull_request=pull_request,
        status=AISTStatus.FINISHED,
    )


def _load_analyzers_config():
    _import_sast_pipeline_package()
    return importlib.import_module("pipeline.config_utils").AnalyzersConfigHelper()


def _fmt_duration(start, end):
    if not start or not end:
        return None
    total = int((end - start).total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _qs_without(request, *keys):
    params = request.GET.copy()
    for k in keys:
        params.pop(k, None)
    return urlencode(params, doseq=True)


def _revoke_task(task_id: str | None, *, terminate: bool = True) -> None:
    """Safely revoke a Celery task by its ID if it is still running."""
    if not task_id:
        return
    try:
        result = AsyncResult(task_id)
        if result.state not in states.READY_STATES:
            result.revoke(terminate=terminate)
    except Exception:
        _logger.exception("Failed to revoke Celery task: %s", task_id)


def stop_pipeline(pipeline: AISTPipeline) -> None:
    """
    Stop all Celery tasks associated with an AISTPipeline.

    Revokes both the run and deduplication watcher tasks (if present),
    tears down any related containers, and updates the pipeline status
    to FINISHED. The caller should save() the pipeline afterwards.
    """
    cleanup_pipeline_containers(pipeline.id)

    run_id = getattr(pipeline, "run_task_id", None)
    watch_id = getattr(pipeline, "watch_dedup_task_id", None)
    _revoke_task(run_id)
    _revoke_task(watch_id)

    pipeline.run_task_id = None
    pipeline.watch_dedup_task_id = None
    pipeline.status = AISTStatus.FINISHED


def _best_effort_outbound_ip() -> str:
    """Return the outbound IP chosen by the OS when connecting to a public address (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _is_abs_url(value: str) -> bool:
    try:
        p = urlsplit(value)
        return bool(p.scheme and p.netloc)
    except Exception:
        return False


def _scheme_from_settings_or_request(request):
    """Decide scheme respecting SECURE_SSL_REDIRECT and reverse proxy headers."""
    if getattr(settings, "SECURE_SSL_REDIRECT", False):
        return "https"

    hdr = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
    if request is not None and hdr:
        header_name, expected_value = hdr
        actual = request.META.get(header_name, "")
        if actual.split(",")[0].strip().lower() == expected_value.lower():
            return "https"

    if request is not None and request.is_secure():
        return "https"

    return "http"


def _normalize_base_url(url: str) -> str:
    """Return 'scheme://host[:port]' without path/query/fragment and no trailing slash."""
    p = urlsplit(url)
    scheme = p.scheme or "http"
    netloc = p.netloc or p.path
    if not netloc:
        return ""
    return urlunsplit((scheme, netloc.strip("/"), "", "", "")).rstrip("/")


def get_public_base_url() -> str:
    return getattr(settings, "PUBLIC_BASE_URL", "https://157.90.113.55:8443/")


def build_callback_url(pipeline_id: str) -> str:
    base = get_public_base_url()
    path = reverse("dojo_aist:pipeline_callback", kwargs={"pipeline_id": str(pipeline_id)})
    return urljoin(base + "/", path.lstrip("/"))


def build_project_version_file_blob(project_version_id: int, subpath: str) -> str:
    base = get_public_base_url()
    path = reverse("dojo_aist_api:project_version_file_blob", kwargs={"id": project_version_id, "subpath": subpath})
    return urljoin(base + "/", path.lstrip("/"))


def _safe_join(root: Path, target: str) -> Path:
    target = target.replace("\\", "/")
    joined = (root / target).resolve()
    allowed_prefix = str(root.resolve()) + os.sep
    if not str(joined).startswith(allowed_prefix):
        msg = "Illegal path in archive (path traversal detected)."
        raise ValueError(msg)
    return joined


def _safe_extract_zip_member(zf, member, root: Path) -> None:
    """Extract one member from a ZIP file safely, avoiding path traversal."""
    name = member.filename
    if name.endswith("/"):
        (_safe_join(root, name)).mkdir(parents=True, exist_ok=True)
        return
    out_path = _safe_join(root, name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, Path(out_path).open("wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            dst.write(chunk)


def _safe_extract_tar_member(tf, member, root: Path) -> None:
    """Extract one member from a TAR file safely, avoiding path traversal."""
    if not member.name:
        return
    if member.islnk() or member.issym() or member.ischr() or member.isblk() or member.isfifo():
        return
    out_path = _safe_join(root, member.name + ("/" if member.isdir() else ""))
    if member.isdir():
        out_path.mkdir(parents=True, exist_ok=True)
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = tf.extractfile(member)
    if src is None:
        return
    with src, Path(out_path).open("wb") as dst:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            dst.write(chunk)
