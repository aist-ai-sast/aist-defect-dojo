from __future__ import annotations

import json
import uuid
from typing import Any, Dict
from django.core.paginator import Paginator
from django.db.models import Q


from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from .models import TestDeduplicationProgress
from django.db.models import Count


from dojo.utils import add_breadcrumb
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest
from dojo.aist.tasks import run_sast_pipeline

from .forms import AISTPipelineRunForm , _load_analyzers_config, _signature # type: ignore


from .utils import stop_pipeline, _fmt_duration, _qs_without, create_pipeline_object
from .logging_transport import _install_db_logging
import time
from django.http import StreamingHttpResponse, Http404
from django.db import close_old_connections
from django.utils.timezone import now

from .models import AISTPipeline, AISTStatus, AISTProject
from .logging_transport import get_redis, PUBSUB_CHANNEL_TPL, STREAM_KEY, BACKLOG_COUNT
from .forms import AISTProjectVersionForm

@login_required
@require_http_methods(["GET", "POST"])
def project_version_create(request: HttpRequest, project_id: int) -> HttpResponse:
    project = get_object_or_404(AISTProject, id=project_id)

    if request.method == "GET":
        form = AISTProjectVersionForm(initial={"project": project.id})
        return render(request, "dojo/aist/_project_version_form.html", {"form": form, "project": project})

    form = AISTProjectVersionForm(request.POST, request.FILES, initial={"project": project.id})
    if form.is_valid():
        obj = form.save()  # save() сам поставит version = sha256 для FILE_HASH
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
      return HttpResponseBadRequest("config not loaded")

    filtered = cfg.get_filtered_analyzers(
        analyzers_to_run=None,
        max_time_class=time_class,
        non_compile_project=bool(proj and not proj.compilable),
        target_languages=langs_union,
        show_only_parent=True
    )
    defaults = cfg.get_names(filtered)

    return JsonResponse({
        "defaults": defaults,
        "signature": _signature(project_id, langs_union, time_class),
    })

def pipeline_status_stream(request, id: str):
    """
    SSE endpoint: шлёт событие 'status' при смене статуса пайплайна.
    Завершается событием 'done' при FINISHED/FAILED/DELETED.
    """
    # Быстрая проверка, что объект вообще существует на момент старта
    if not AISTPipeline.objects.filter(id=id).exists():
        raise Http404("Pipeline not found")

    def event_stream():
        last_status = None
        heartbeat_every = 3  # сек, раз в N секунд шлём keep-alive комментарий
        last_heartbeat = 0.0

        try:
            while True:
                # В стримах важно периодически закрывать/переподключать старые коннекты
                close_old_connections()

                # Заново читаем объект каждый цикл — не храним «живой» инстанс
                obj = (
                    AISTPipeline.objects
                    .only("id", "status", "updated")
                    .filter(id=id)
                    .first()
                )

                if obj is None:
                    # Объект удалён — сообщаем клиенту и выходим
                    yield "event: done\ndata: deleted\n\n"
                    break

                if obj.status != last_status:
                    last_status = obj.status
                    # Правильный блок SSE: сначала event, затем data, затем пустая строка
                    yield f"event: status\ndata: {last_status}\n\n"

                    if last_status in (
                        getattr(AISTStatus, "FINISHED", "FINISHED"),
                        getattr(AISTStatus, "FAILED", "FAILED"),
                    ):
                        yield "event: done\ndata: finished\n\n"
                        break

                # Heartbeat, чтобы прокси (например, Nginx) не закрывали соединение
                now_ts = time.time()
                if now_ts - last_heartbeat >= heartbeat_every:
                    last_heartbeat = now_ts
                    yield f": heartbeat {int(now_ts)}\n\n"  # комментарий по спецификации SSE

                time.sleep(1)

        except GeneratorExit:
            # Клиент закрыл соединение — просто выходим, без исключений в логах
            return
        finally:
            close_old_connections()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # важно для Nginx, чтобы не буферил стрим
    return resp


def pipeline_set_status(request, id: str):
    if not AISTPipeline.objects.filter(id=id).exists():
        raise Http404("Pipeline not found")

    if request.method == "POST":
        with transaction.atomic():
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=id)
            )

            pipeline.status = AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI
            pipeline.save(update_fields=["status", "updated"])
    return redirect('dojo_aist:pipeline_detail', id=id)


def start_pipeline(request: HttpRequest) -> HttpResponse:
    """Launch a new SAST pipeline or redirect to the active one.

    If there is an existing pipeline that hasn't finished yet the user
    is redirected to its detail page.  Otherwise this view presents a
    form allowing the user to configure and start a new pipeline.  On
    successful submission a new pipeline is created and the Celery
    task is triggered.
    """
    # If there is an active pipeline that hasn't finished yet,
    # immediately redirect the user to its status page.  Only a single
    # pipeline may run at a time.
    active = AISTPipeline.objects.exclude(status=AISTStatus.FINISHED).first()
    if active:
        return redirect('dojo_aist:pipeline_detail', id=active.id)

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
            Q(project__product__name__icontains=q)
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
    add_breadcrumb( title="Start pipeline", top_level=True, request=request)

    if request.method == "POST":
        form = AISTPipelineRunForm(request.POST)
        if form.is_valid():
            params = form.get_params()
            # Generate a unique identifier for this pipeline.  The
            # downstream SAST pipeline uses an 8 character hex string
            # internally, so mirror that here.

            pipeline_id = uuid.uuid4().hex[:8]
            with transaction.atomic():
                p = create_pipeline_object(form.cleaned_data["project"], form.cleaned_data.get("project_version")
                                           or form.cleaned_data["project"].versions.order_by("-created").first(), None)
            # Launch the Celery task and record its id on the pipeline.
            # Storing the task id allows us to revoke it later if the
            # user chooses to stop the pipeline.
            async_result = run_sast_pipeline.delay(p.id, params)
            p.run_task_id = async_result.id
            p.save(update_fields=["run_task_id"])
            return redirect('dojo_aist:pipeline_detail', id=p.id)
    else:
        form = AISTPipelineRunForm()
    return render(request, 'dojo/aist/start.html', {'form': form, 'history_page': page_obj,  # для пагинации
                                                    'history_items': history_items,
                                                    'history_qs': history_qs_str,
                                                    'selected_project': project_id or "",
                                                    'search_query': q, "page_sizes": [10, 20, 50, 100]})

def pipeline_list(request):
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "FINISHED").upper()  # FINISHED | ALL
    per_page = int(request.GET.get("page_size") or 20)

    qs = (AISTPipeline.objects
          .select_related("project__product")
          .order_by("-updated"))
    if status != "ALL":
        qs = qs.filter(status=AISTStatus.FINISHED)

    if project_id:
        qs = qs.filter(project_id=project_id)
    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q)
        )

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "created": p.created,
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
    } for p in page_obj.object_list]

    qs_str = _qs_without(request, "page")

    projects = AISTProject.objects.select_related("product").order_by("product__name")
    add_breadcrumb(title="Pipeline History", top_level=True, request=request)
    return render(
        request,
        'dojo/aist/pipeline_list.html',
        {
            "page_obj": page_obj,
            "items": items,
            "qs": qs_str,
            "selected_project": project_id or "",
            "search_query": q,
            "status": status,
            "projects": projects,
        }
    )


def pipeline_detail(request, id: str):
    """
    Display the status and logs for a pipeline.
    Adds actions (Stop/Delete) and connects SSE client to stream logs.
    """
    pipeline = get_object_or_404(AISTPipeline, id=id)
    if request.headers.get("X-Partial") == "status":
        return render(request, "dojo/aist/_pipeline_status_container.html", {"pipeline": pipeline})

    add_breadcrumb(parent=pipeline, title="Pipeline Detail", top_level=False, request=request)
    return render(request, "dojo/aist/pipeline_detail.html", {"pipeline": pipeline})


@login_required
@require_http_methods(["POST"])
def stop_pipeline_view(request, id: str):
    """
    POST-only endpoint to stop a running pipeline (Celery revoke).
    Sets FINISHED regardless of current state to keep UI consistent.
    """
    pipeline = get_object_or_404(AISTPipeline, id=id)
    with transaction.atomic():
        stop_pipeline(pipeline)
        pipeline.save(update_fields=["status"])

    return redirect("dojo_aist:pipeline_detail", id=pipeline.id)

@login_required
@require_http_methods(["GET", "POST"])
def delete_pipeline_view(request, id: str):
    """
    Delete a pipeline after confirmation (POST). GET returns a confirm view.
    """
    pipeline = get_object_or_404(AISTPipeline, id=id)
    if request.method == "POST":
        pipeline.delete()
        return redirect("dojo_aist:start_pipeline")
    add_breadcrumb(parent=pipeline, title="Delete pipeline", top_level=False, request=request)
    return render(request, "dojo/aist/confirm_delete.html", {"pipeline": pipeline})



@csrf_exempt  # SSE does not need CSRF for GET; keep it simple
@require_http_methods(["GET"])
def stream_logs_sse(request, id: str):
    """
    Server-Sent Events endpoint that streams new log lines for a pipeline.
    Reads from DB; emits only new tail bytes every poll tick.
    """
    pipeline = get_object_or_404(AISTPipeline, id=id)

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
    return f"data: {payload}\n\n".encode("utf-8")


def _sse_comment(comment: str) -> bytes:
    """Format an SSE comment line (useful as heartbeat)."""
    return f": {comment}\n\n".encode("utf-8")


def _stream_last_lines_from_redis_stream(r, pipeline_id: str, limit: int):
    """
    Send initial backlog from Redis Stream (last `limit` items) filtered by pipeline_id.
    Uses XREVRANGE for 'tail'-like behavior then reverses to chronological order.
    """
    try:
        # XREVRANGE stream + - COUNT N  -> newest first
        entries = r.xrevrange(STREAM_KEY, max="+", min="-", count=limit) or []
        # reverse to oldest -> newest for nicer UI
        for entry_id, fields in reversed(entries):
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
def stream_logs_sse_redis_based(request: HttpRequest, id: str) -> HttpResponse:
    """
    SSE endpoint for pipeline logs.
    1) Replays last N lines from Redis Stream for quick backlog.
    2) Subscribes to Redis Pub/Sub and streams new lines immediately.
    """
    # Validate pipeline
    try:
        AISTPipeline.objects.only("id").get(id=id)
    except AISTPipeline.DoesNotExist:
        raise Http404("Pipeline not found")

    r = get_redis()
    channel = PUBSUB_CHANNEL_TPL.format(pipeline_id=id)

    def event_stream():
        # 1) initial backlog from Redis Stream
        yield from _stream_last_lines_from_redis_stream(r, id, BACKLOG_COUNT)

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
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception:
                pass

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    # Avoid buffering by proxies/servers
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"    # for nginx
    return resp

def deduplication_progress_json(request, id: str):
    """
    Return deduplication progress for a pipeline as JSON (per Test and overall).
    Progress counts findings, not just tests with a boolean flag.
    """
    pipeline = get_object_or_404(AISTPipeline, id=id)

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
        raise Http404("Project not found")

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
def pipeline_enrich_progress_sse(request, id: str):
    redis = get_redis()
    key = f"aist:progress:{id}:enrich"

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