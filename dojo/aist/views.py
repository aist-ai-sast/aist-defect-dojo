from __future__ import annotations

import json
import pathlib
import time
from contextlib import suppress

import gitlab
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import close_old_connections, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from dojo.aist.tasks import run_sast_pipeline
from dojo.utils import add_breadcrumb

from .forms import (  # type: ignore[import-not-found]
    AISTPipelineRunForm,
    AISTProjectVersionForm,
    _load_analyzers_config,
    _signature,
)
from .logging_transport import BACKLOG_COUNT, PUBSUB_CHANNEL_TPL, STREAM_KEY, get_pipeline_log_path, get_redis
from .models import AISTPipeline, AISTProject, AISTStatus, Organization, TestDeduplicationProgress
from .utils import _fmt_duration, _qs_without, create_pipeline_object, stop_pipeline

# ---- Error messages -------------------------------------------------
ERR_PIPELINE_NOT_FOUND = "Pipeline not found"
ERR_PROJECT_NOT_FOUND = "Project not found"
ERR_CONFIG_NOT_LOADED = "config not loaded"


@require_http_methods(["GET"])
def pipeline_logs_progressive(request, pipeline_id: str):
    """
    Progressive log API similar to Jenkins/GitLab.
    GET params:
    - start=<int>: byte offset to read from (default 0). Returns data from this offset to EOF.
    - tail=<int>: last N lines to return initially (ignored if start is provided).
    Response headers:
    - X-Log-Size: current file size in bytes (use as next start).
    Body:
    - plain text chunk (UTF-8).
    """
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    path = get_pipeline_log_path(pipeline.id)
    data = ""
    size = 0
    start = request.GET.get("start")
    tail = request.GET.get("tail")

    # parse params
    try:
        start = int(start) if start is not None else None
    except ValueError:
        start = None
    try:
        tail = max(0, int(tail)) if tail is not None else None
    except ValueError:
        tail = None

    if pathlib.Path(path).exists():
        size = pathlib.Path(path).stat().st_size
        # if tail return latest N lines
        if tail:
            # read last N lines
            with pathlib.Path(path).open("rb") as f:
                lines = f.readlines()[-tail:]
            decoded = [ln.decode("utf-8", errors="ignore").rstrip("\r\n") for ln in lines]
            data = "\n".join(decoded)
        elif start is not None:
            # read from bite offset until end
            start = max(0, min(start, size))
            with pathlib.Path(path).open("rb") as f:
                f.seek(start)
                chunk = f.read()
            data = chunk.decode("utf-8", errors="ignore")
        else:
            # by default return all file
            with pathlib.Path(path).open("r", encoding="utf-8", errors="ignore") as f:
                data = f.read()

    resp = HttpResponse(data, content_type="text/plain; charset=utf-8")
    resp["X-Log-Size"] = str(size)
    return resp


def get_logs_content(pipeline: AISTPipeline):
    path = get_pipeline_log_path(pipeline.id)
    if pathlib.Path(path).exists():
        with pathlib.Path(path).open("r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = ""
    return content


@require_http_methods(["GET"])
def pipeline_logs_full(request, pipeline_id: str) -> HttpResponse:
    """Return the entire log file as text/plain."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    content = get_logs_content(pipeline)
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


@require_http_methods(["GET"])
def pipeline_logs_raw(request, pipeline_id: str) -> HttpResponse:
    """Raw log content (same as full) used by 'Copy to clipboard'."""
    return pipeline_logs_full(request, pipeline_id)


@require_http_methods(["GET"])
def pipeline_logs_download(request, pipeline_id: str) -> HttpResponse:
    """Force download of the entire log as a .log file."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    content = get_logs_content(pipeline)
    resp = HttpResponse(content, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="pipeline-{pipeline_id}.log"'
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def project_version_create(request: HttpRequest, project_id: int) -> HttpResponse:
    project = get_object_or_404(AISTProject, id=project_id)

    if request.method == "GET":
        form = AISTProjectVersionForm(initial={"project": project.id})
        return render(request, "dojo/aist/_project_version_form.html", {"form": form, "project": project})

    form = AISTProjectVersionForm(request.POST, request.FILES, initial={"project": project.id})
    if form.is_valid():
        obj = form.save()  # save() sets version = sha256 for FILE_HASH automatically
        return JsonResponse({
            "ok": True,
            "version": {"id": str(obj.id), "label": str(obj)},
        })

    html = render(request, "dojo/aist/_project_version_form.html", {"form": form, "project": project}).content.decode("utf-8")
    return JsonResponse({"ok": False, "html": html}, status=400)


@require_POST
def default_analyzers(request):
    project_id = request.POST.get("project")
    time_class = request.POST.get("time_class_level") or "slow"
    langs = request.POST.getlist("languages") or request.POST.getlist("languages[]")

    proj = AISTProject.objects.filter(id=project_id).first()
    proj_langs = (proj.supported_languages if proj else []) or []
    langs_union = list(set((langs or []) + proj_langs))

    cfg = _load_analyzers_config()
    if not cfg:
        return HttpResponseBadRequest(ERR_CONFIG_NOT_LOADED)  # E111 fixed (indent)

    filtered = cfg.get_filtered_analyzers(
        analyzers_to_run=None,
        max_time_class=time_class,
        non_compile_project=bool(proj and not proj.compilable),
        target_languages=langs_union,
        show_only_parent=True,
    )
    defaults = cfg.get_names(filtered)

    return JsonResponse({
        "defaults": defaults,
        "signature": _signature(project_id, langs_union, time_class),
    })


def pipeline_status_stream(request, pipeline_id: str):
    """SSE endpoint: sends 'status' on status change; finishes with 'done' on FINISHED/FAILED/DELETED."""
    # Quick existence check
    if not AISTPipeline.objects.filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    def event_stream():
        last_status = None
        heartbeat_every = 3  # seconds
        last_heartbeat = 0.0

        try:
            while True:
                # Important for long-lived streams
                close_old_connections()

                # Re-fetch object every loop, don't keep a live instance
                obj = (
                    AISTPipeline.objects
                    .only("id", "status", "updated")
                    .filter(id=pipeline_id)
                    .first()
                )

                if obj is None:
                    # Deleted — inform client and exit
                    yield "event: done\ndata: deleted\n\n"
                    break

                if obj.status != last_status:
                    last_status = obj.status
                    # Proper SSE block: event, data, blank line
                    yield f"event: status\ndata: {last_status}\n\n"

                    if last_status in {
                        getattr(AISTStatus, "FINISHED", "FINISHED"),
                        getattr(AISTStatus, "FAILED", "FAILED"),
                    }:  # PLR6201
                        yield "event: done\ndata: finished\n\n"
                        break

                # Heartbeat so proxies (e.g., Nginx) don't close the connection
                now_ts = time.time()
                if now_ts - last_heartbeat >= heartbeat_every:
                    last_heartbeat = now_ts
                    yield f": heartbeat {int(now_ts)}\n\n"

                time.sleep(1)

        except GeneratorExit:
            # Client closed connection — just exit
            return
        finally:
            close_old_connections()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # important for Nginx to avoid buffering
    return resp


def pipeline_set_status(request, pipeline_id: str):
    if not AISTPipeline.objects.filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    if request.method == "POST":
        with transaction.atomic():
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=pipeline_id)
            )
            pipeline.status = AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI
            pipeline.save(update_fields=["status", "updated"])
    return redirect("dojo_aist:pipeline_detail", pipeline_id=pipeline_id)


def start_pipeline(request: HttpRequest) -> HttpResponse:
    """
    Launch a new SAST pipeline or redirect to the active one.

    If there is an existing pipeline that hasn't finished yet the user
    is redirected to its detail page. Otherwise this view presents a
    form allowing the user to configure and start a new pipeline. On
    successful submission a new pipeline is created and the Celery
    task is triggered.
    """
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    history_qs = (
        AISTPipeline.objects
        .filter(status=AISTStatus.FINISHED)
        .select_related("project__product")
    )
    if project_id:
        history_qs = history_qs.filter(project_id=project_id)
    if q:
        history_qs = history_qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q),
        )

    history_qs = history_qs.order_by("-updated")

    per_page = int(request.GET.get("page_size") or 8)
    paginator = Paginator(history_qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    history_items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
    } for p in page_obj.object_list]

    history_qs_str = _qs_without(request, "page")
    add_breadcrumb(title="Start pipeline", top_level=True, request=request)

    if request.method == "POST":
        form = AISTPipelineRunForm(request.POST)
        if form.is_valid():
            params = form.get_params()

            with transaction.atomic():
                p = create_pipeline_object(
                    form.cleaned_data["project"],
                    form.cleaned_data.get("project_version")
                    or form.cleaned_data["project"].versions.order_by("-created").first(),
                    None,
                )
            # Launch the Celery task and record its id on the pipeline.
            async_result = run_sast_pipeline.delay(p.id, params)
            p.run_task_id = async_result.id
            p.save(update_fields=["run_task_id"])
            return redirect("dojo_aist:pipeline_detail", pipeline_id=p.id)
    else:
        form = AISTPipelineRunForm()
    return render(
        request,
        "dojo/aist/start.html",
        {
            "form": form,
            "history_page": page_obj,  # for pagination
            "history_items": history_items,
            "history_qs": history_qs_str,
            "selected_project": project_id or "",
            "search_query": q,
            "page_sizes": [10, 20, 50, 100],
        },
    )


def pipeline_list(request):
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    status = (request.GET.get("status") or "ALL").upper()  # ALL | FINISHED
    per_page = int(request.GET.get("page_size") or 20)

    qs = (
        AISTPipeline.objects
        .select_related("project__product", "project_version")
        .order_by("-updated")
    )

    if status == "FINISHED":
        qs = qs.filter(status=AISTStatus.FINISHED)

    if project_id:
        qs = qs.filter(project_id=project_id)
    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q),
        )

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "project_version": getattr(p.project_version, "version", None),
        "created": p.created,
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
        # Active = anything that is not FINISHED
        "is_active": p.status != AISTStatus.FINISHED,
    } for p in page_obj.object_list]

    qs_str = _qs_without(request, "page")

    projects = AISTProject.objects.select_related("product").order_by("product__name")
    add_breadcrumb(title="Pipeline History", top_level=True, request=request)
    return render(
        request,
        "dojo/aist/pipeline_list.html",
        {
            "page_obj": page_obj,
            "items": items,
            "qs": qs_str,
            "selected_project": project_id or "",
            "search_query": q,
            "status": status,
            "projects": projects,
        },
    )


def pipeline_detail(request, pipeline_id: str):
    """Display the status and logs for a pipeline. Adds actions (Stop/Delete) and connects SSE client to stream logs."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    if request.headers.get("X-Partial") == "status":
        return render(request, "dojo/aist/_pipeline_status_container.html", {"pipeline": pipeline})

    add_breadcrumb(parent=pipeline, title="Pipeline Detail", top_level=False, request=request)
    return render(request, "dojo/aist/pipeline_detail.html", {"pipeline": pipeline})


@login_required
@require_http_methods(["POST"])
def stop_pipeline_view(request, pipeline_id: str):
    """POST-only endpoint to stop a running pipeline (Celery revoke). Sets FINISHED regardless of current state to keep UI consistent."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    stop_pipeline(pipeline)
    return redirect("dojo_aist:pipeline_detail", pipeline_id=pipeline.id)


@login_required
@require_http_methods(["GET", "POST"])
def delete_pipeline_view(request, pipeline_id: str):
    """Delete a pipeline after confirmation (POST). GET returns a confirm view."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    if request.method == "POST":
        pipeline.delete()
        return redirect("dojo_aist:start_pipeline")
    add_breadcrumb(parent=pipeline, title="Delete pipeline", top_level=False, request=request)
    return render(request, "dojo/aist/confirm_delete.html", {"pipeline": pipeline})


@csrf_exempt  # SSE does not need CSRF for GET; keep it simple
@require_http_methods(["GET"])
def stream_logs_sse(request, pipeline_id: str):
    """Server-Sent Events endpoint that streams new log lines for a pipeline. Reads from DB; emits only new tail bytes every poll tick."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)

    def event_stream():
        last_len = 0
        # Simple polling loop. Replace with channels/redis pub-sub if desired.
        for _ in range(60 * 60 * 12):  # up to ~12h
            p = AISTPipeline.objects.filter(id=pipeline.id).only("logs", "status").first()
            if not p:
                break
            data = p.logs or ""
            if len(data) > last_len:
                chunk = data[last_len:]
                last_len = len(data)
                # SSE frame
                yield f"data: {chunk}\n\n"
            if p.status == AISTStatus.FINISHED:
                yield "event: done\ndata: FINISHED\n\n"
                break
            time.sleep(0.3)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream; charset=utf-8")
    resp["Cache-Control"] = "no-cache, no-transform"
    resp["X-Accel-Buffering"] = "no"
    return resp


def _sse_data(payload: str) -> bytes:
    """Format a single SSE 'message' event."""
    return f"data: {payload}\n\n".encode()


def _sse_comment(comment: str) -> bytes:
    """Format an SSE comment line (useful as heartbeat)."""
    return f": {comment}\n\n".encode()


def _stream_last_lines_from_redis_stream(r, pipeline_id: str, limit: int):
    """
    Send initial backlog from Redis Stream (last `limit` items) filtered by pipeline_id.
    Uses XREVRANGE for 'tail'-like behavior then reverses to chronological order.
    """
    try:
        # XREVRANGE stream + - COUNT N  -> newest first
        entries = r.xrevrange(STREAM_KEY, max="+", min="-", count=limit) or []
        # reverse to oldest -> newest for nicer UI
        for _entry_id, fields in reversed(entries):  # B007
            pid = (fields or {}).get("pipeline_id")
            msg = (fields or {}).get("message")
            lvl = (fields or {}).get("level")
            if not pid or pid != pipeline_id or not msg:
                continue
            line = f"{lvl} {msg}" if lvl else msg
            yield _sse_data(line)
    except Exception:
        # Do not break SSE if Redis is temporarily unavailable
        return


@csrf_exempt
@require_http_methods(["GET"])
def stream_logs_sse_redis_based(request: HttpRequest, pipeline_id: str) -> HttpResponse:
    """
    SSE endpoint for pipeline logs.
    1) Replays last N lines from Redis Stream for quick backlog.
    2) Subscribes to Redis Pub/Sub and streams new lines immediately.
    """
    # Validate pipeline
    try:
        AISTPipeline.objects.only("id").get(id=pipeline_id)
    except AISTPipeline.DoesNotExist:
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    r = get_redis()
    channel = PUBSUB_CHANNEL_TPL.format(pipeline_id=pipeline_id)

    def event_stream():
        # 1) initial backlog from Redis Stream
        yield from _stream_last_lines_from_redis_stream(r, pipeline_id, BACKLOG_COUNT)

        # 2) subscribe for live updates
        pubsub = r.pubsub()
        pubsub.subscribe(channel)

        last_ping = time.monotonic()
        try:
            # Notify client that SSE is alive
            yield _sse_comment("connected")

            for msg in pubsub.listen():
                now = time.monotonic()
                # heartbeat every ~25s
                if now - last_ping > 25:
                    yield _sse_comment("ping")
                    last_ping = now

                if msg.get("type") != "message":
                    continue

                try:
                    data = json.loads(msg["data"])
                    txt = f'{data.get("level") or ""} {data.get("message") or ""}'.strip()
                    if txt:
                        yield _sse_data(txt)
                except Exception:
                    # If payload not JSON, try raw data
                    raw = msg.get("data")
                    if isinstance(raw, str) and raw:
                        yield _sse_data(raw)
        finally:
            with suppress(Exception):  # S110
                pubsub.unsubscribe(channel)
                pubsub.close()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    # Avoid buffering by proxies/servers
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"    # for nginx
    return resp


def deduplication_progress_json(request, pipeline_id: str):
    """Return deduplication progress for a pipeline as JSON (per Test and overall). Progress counts findings, not just tests with a boolean flag."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)

    tests = (
        pipeline.tests
        .select_related("engagement")
        .annotate(total_findings=Count("finding", distinct=True))
        .order_by("id")
    )

    tests_payload = []
    overall_total = 0
    overall_processed = 0

    for t in tests:
        # Ensure progress row exists and is refreshed if needed
        prog, _ = TestDeduplicationProgress.objects.get_or_create(test=t)
        # `pending_tasks` = findings total - processed; we keep `refresh_pending_tasks()` the SSOT.
        prog.refresh_pending_tasks()

        total = getattr(t, "total_findings", 0)
        pending = prog.pending_tasks
        processed = max(total - pending, 0)
        pct = 100 if total == 0 else int(processed * 100 / total)

        overall_total += total
        overall_processed += processed

        tests_payload.append({
            "test_id": t.id,
            "test_name": getattr(t, "title", None) or f"Test #{t.id}",
            "total_findings": total,
            "processed": processed,
            "pending": pending,
            "percent": pct,
            "completed": bool(prog.deduplication_complete),
        })

    overall_pct = 100 if overall_total == 0 else int(overall_processed * 100 / overall_total)

    return JsonResponse({
        "status": pipeline.status,
        "overall": {
            "total_findings": overall_total,
            "processed": overall_processed,
            "pending": max(overall_total - overall_processed, 0),
            "percent": overall_pct,
        },
        "tests": tests_payload,
    })


@csrf_exempt
def project_meta(request, pk: int):
    try:
        p = AISTProject.objects.get(pk=pk)
    except AISTProject.DoesNotExist:
        raise Http404(ERR_PROJECT_NOT_FOUND)

    versions = [
        {"id": str(v.id), "label": str(v)}
        for v in p.versions.all()
    ]
    return JsonResponse({
        "supported_languages": p.supported_languages or [],
        "versions": versions,
    })


@csrf_exempt
@require_http_methods(["GET"])
def pipeline_enrich_progress_sse(request, pipeline_id: str):
    redis = get_redis()
    key = f"aist:progress:{pipeline_id}:enrich"

    def event_stream():
        last = None
        last_ping = time.monotonic()
        while True:
            try:
                total, done = redis.hmget(key, "total", "done")
            except Exception:
                total, done = 0, 0
            total = int(total or 0)
            done = int(done or 0)

            payload = {
                "total": total,
                "done": done,
                "percent": (100 if total == 0 else int(done * 100 / total)),
            }

            now = (payload["total"], payload["done"])
            if now != last:
                yield f"data: {json.dumps(payload)}\n\n"
                last = now

            # heartbeat so proxy doesn't close connection
            if time.monotonic() - last_ping > 25:
                yield ": ping\n\n"
                last_ping = time.monotonic()

            if total and done >= total:
                yield "event: done\ndata: ok\n\n"
                break

            time.sleep(1)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@require_http_methods(["POST"])
def aist_project_update_view(request: HttpRequest, project_id: int) -> HttpResponse:
    """
    Update editable fields of a single AISTProject.

    Expected POST fields:
    - script_path: str (required)
    - supported_languages: comma-separated string, e.g. "python, c++, java"
    - compilable: "on" / missing (checkbox)
    - profile: JSON string representing an object (optional)
    - organization: optional organization id (int) or empty for no organization
    """
    project = get_object_or_404(AISTProject, id=project_id)

    script_path = (request.POST.get("script_path") or "").strip()
    compilable = request.POST.get("compilable") == "on"
    supported_languages_raw = (request.POST.get("supported_languages") or "").strip()
    profile_raw = (request.POST.get("profile") or "").strip()
    organization_raw = (request.POST.get("organization") or "").strip()

    errors: dict[str, str] = {}

    if not script_path:
        errors["script_path"] = "Script path is required."

    # Parse supported_languages from comma-separated string.
    cfg = _load_analyzers_config()
    if not cfg:
        return HttpResponseBadRequest(ERR_CONFIG_NOT_LOADED)

    if supported_languages_raw:
        languages = cfg.convert_languages(
            [
                x.strip()
                for x in supported_languages_raw.split(",")
                if x.strip()
            ],
        )
    else:
        languages = []

    # Parse profile JSON; keep validation on the server.
    profile: dict | list | None
    if not profile_raw:
        profile = {}
    else:
        try:
            profile = json.loads(profile_raw)
        except json.JSONDecodeError:
            errors["profile"] = "Profile must be a valid JSON value."
            profile = None

    # For now we only allow JSON objects or empty profile for better UX.
    if profile is not None and not isinstance(profile, dict):
        errors["profile"] = 'Profile must be a JSON object (e.g. {"paths": {"exclude": []}}).'

    # Parse organization (optional)
    organization = None
    if organization_raw:
        try:
            org_id = int(organization_raw)
            organization = Organization.objects.get(id=org_id)
        except (ValueError, Organization.DoesNotExist):
            errors["organization"] = "Selected organization does not exist."

    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)

    project.script_path = script_path
    project.compilable = compilable
    project.supported_languages = languages
    project.profile = profile or {}
    project.organization = organization
    project.save(
        update_fields=[
            "script_path",
            "compilable",
            "supported_languages",
            "profile",
            "organization",
            "updated",
        ],
    )

    return JsonResponse(
        {
            "ok": True,
            "project": {
                "id": project.id,
                "product_name": getattr(project.product, "name", str(project.id)),
                "script_path": project.script_path,
                "compilable": project.compilable,
                "supported_languages": project.supported_languages,
                "profile": project.profile,
                "organization_id": project.organization_id,
                "organization_name": getattr(project.organization, "name", None),
            },
        },
    )


@login_required
@require_http_methods(["GET"])
def aist_project_list_view(request: HttpRequest) -> HttpResponse:
    """
    Management screen for AISTProject objects, grouped by Organization.

    Notes:
    - One Organization can have many AISTProject objects.
    - Projects without an Organization are shown under the "Others" group.
    - Only fields that are safe to edit from UI are exposed:
      * script_path
      * supported_languages
      * compilable
      * profile

    """
    # Organizations with their projects prefetched to avoid N+1 queries.
    organizations = (
        Organization.objects
        .prefetch_related("projects__product")
        .order_by("name")
    )

    # Projects that are not assigned to any organization -> "Others" section.
    unassigned_projects = (
        AISTProject.objects
        .select_related("product")
        .filter(organization__isnull=True)
        .order_by("product__name", "id")
    )

    add_breadcrumb(title="AIST Projects", top_level=True, request=request)
    return render(
        request,
        "dojo/aist/projects.html",
        {
            "organizations": organizations,
            "unassigned_projects": unassigned_projects,
        },
    )


@login_required
@require_POST
def gitlab_projects_list(request: HttpRequest) -> JsonResponse:
    """
    Return a lightweight list of projects from a GitLab instance.

    Token and URL are NOT stored anywhere, they are used only for this request.
    """
    gitlab_url = (request.POST.get("gitlab_url") or "").strip()
    gitlab_token = (request.POST.get("gitlab_token") or "").strip()

    if not gitlab_url or not gitlab_token:
        return JsonResponse(
            {"ok": False, "error": "GitLab URL and token are required."},
            status=400,
        )

    try:
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
        # Explicit auth to fail fast if token/URL are invalid.
        gl.auth()
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "error": "Unable to authenticate with GitLab. "
                         "Please check URL and personal access token.",
            },
            status=400,
        )

    projects: list[dict] = []
    try:
        # Keep it lightweight: first page, last active projects on top.
        gl_projects = gl.projects.list(
            all=True,
            per_page=100,
            order_by="last_activity_at",
            sort="desc",
        )
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Failed to fetch projects list from GitLab."},
            status=400,
        )

    for pr in gl_projects:
        language = ""
        # Try to detect the dominant language via /projects/:id/languages
        with suppress(Exception):
            langs = pr.languages()
            if isinstance(langs, dict) and langs:
                language = max(langs, key=langs.get)

        projects.append(
            {
                "id": pr.id,
                "name": getattr(pr, "name", "") or "",
                "path_with_namespace": getattr(pr, "path_with_namespace", "") or "",
                "description": getattr(pr, "description", "") or "",
                "web_url": getattr(pr, "web_url", "") or "",
                "default_branch": getattr(pr, "default_branch", "") or "",
                "visibility": getattr(pr, "visibility", "") or "",
                "language": language or "",
            },
        )

    return JsonResponse({"ok": True, "projects": projects})
