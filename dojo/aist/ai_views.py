import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_POST
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from dojo.aist.tasks import push_request_to_ai
from dojo.models import Finding, Test

from .logging_transport import _install_db_logging
from .models import AISTAIResponse, AISTPipeline, AISTStatus


def _severity_rank_case():
    """
    Return a Django Case/When expression that ranks severities for ordering.
    Critical(0) < High(1) < Medium(2) < Low(3) < Info(4)
    """
    return Case(
        When(severity__iexact="Critical", then=Value(0)),
        When(severity__iexact="High", then=Value(1)),
        When(severity__iexact="Medium", then=Value(2)),
        When(severity__iexact="Low", then=Value(3)),
        When(severity__iexact="Informational", then=Value(4)),
        When(severity__iexact="Info", then=Value(4)),
        default=Value(9),
        output_field=IntegerField(),
    )


@login_required
@require_GET
def product_analyzers_json(request, product_id: int):
    """
    Return distinct analyzers (Test.test_type.name) that produced findings for a Product.
    This is grounded in DefectDojo's models via Finding -> Test -> Test_Type.
    """
    # Limit to analyzers that actually have findings for this product.
    names_qs = (Finding.objects
                .filter(test__engagement__product_id=product_id)
                .select_related("test__test_type")
                .values_list("test__test_type__name", flat=True)
                .distinct())

    analyzers = []
    used_analyzers_name = set()
    for name in names_qs:
        if not name:
            continue
        if name in used_analyzers_name:
            continue
        used_analyzers_name.add(name)
        analyzers.append({
            "key": slugify(name),
            "display": name,
        })
    return JsonResponse({"analyzers": analyzers})


@login_required
@require_GET
def search_findings_json(request):
    """
    Search findings by product with optional filters:
    - analyzers: CSV of slugs/names of Test_Type involved
    - cwe: CSV of integers
    - query: case-insensitive search in title/description/file_path
    Results are ordered by severity (Critical first) then by date (newest first).
    """
    product = request.GET.get("product")
    if not product:
        return HttpResponseBadRequest("product is required")
    try:
        product_id = int(product)
    except ValueError:
        return HttpResponseBadRequest("product must be int")

    qs = (Finding.objects
          .filter(test__engagement__product_id=product_id, active=True)
          .select_related("test__test_type"))

    # analyzers filter
    analyzers = request.GET.get("analyzers", "").strip()
    if analyzers:
        raw_keys = [x.strip() for x in analyzers.split(",") if x.strip()]
        if raw_keys:
            # Build a map of name -> slug and match by either
            all_types = (Test.objects
                         .filter(engagement__product_id=product_id)
                         .select_related("test_type")
                         .values_list("test_type__name", flat=True)
                         .distinct())

            keep_names: list[str] = []
            keyset_lower = {k.lower() for k in raw_keys}
            for name in all_types:
                s = slugify(name)
                if s in raw_keys or name in raw_keys or name.lower() in keyset_lower:
                    keep_names.append(name)
            if keep_names:
                qs = qs.filter(test__test_type__name__in=keep_names)

    # cwe filter
    cwe_csv = request.GET.get("cwe", "").strip()
    if cwe_csv:
        cwes = []
        for x in cwe_csv.split(","):
            cwe = x.strip()
            if cwe.isdigit():
                cwes.append(int(cwe))
        if cwes:
            qs = qs.filter(cwe__in=cwes)

    # free-text query
    query = request.GET.get("query", "").strip()
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(description__icontains=query) | Q(file_path__icontains=query))

    # ordering
    qs = qs.annotate(sev_rank=_severity_rank_case()).order_by("sev_rank", "-date", "-id")

    # hard cap
    try:
        limit = int(request.GET.get("limit", "150"))
    except ValueError:
        limit = 150
    limit = max(1, min(limit, 1000))

    results = []
    for f in qs[:limit]:
        analyzer_name = f.test.test_type.name if (hasattr(f, "test") and f.test and f.test.test_type) else None
        results.append({
            "id": f.id,
            "severity": (f.severity or "").upper(),
            "title": f.title,
            "cwe": [f.cwe] if f.cwe else [],
            "analyzer": analyzer_name,
            "analyzer_display": analyzer_name,
            "file_path": getattr(f, "file_path", None),
            "line": getattr(f, "line", None),
            "created": f.date.isoformat() if getattr(f, "date", None) else None,
        })

    return JsonResponse({"results": results})


@login_required
@require_POST
def send_request_to_ai(request, pipeline_id: str):
    """
    Extend/implement the endpoint to accept a curated list of finding IDs.

    Expected JSON body:
    {
      "pipeline_id": "<uuid|string>",
      "finding_ids": [1,2,3],
      "filters": {...}  # optional, for audit/debug
    }

    Security: we verify that all findings belong to the same Product as the pipeline's project.
    Then delegate to your internal sender (if you already had one).
    """
    try:
        pipeline = AISTPipeline.objects.select_related("project__product").get(id=pipeline_id)
    except AISTPipeline.DoesNotExist:
        return HttpResponseBadRequest("Unknown pipeline")

    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        data = {}

    ids = data.get("finding_ids") or []
    if not isinstance(ids, list) or not all(str(x).isdigit() for x in ids):
        return HttpResponseBadRequest("finding_ids must be a list of integers")

    ids_int = [int(x) for x in ids]
    product = pipeline.project.product

    allowed_qs = Finding.objects.filter(id__in=ids_int, test__engagement__product=product).select_related("test__test_type")
    found_ids = list(allowed_qs.values_list("id", flat=True))

    filters = data.get("filters") or {}

    if not found_ids:
        return HttpResponseBadRequest("No valid findings for this pipeline/product")

    try:
        logger = _install_db_logging(pipeline_id)
        with transaction.atomic():
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=pipeline_id)
            )
            if pipeline.status != AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI:
                logger.error("Attempt to push to AI before receiving confirmation")
                return JsonResponse({"error": "Attempt to push to AI before receiving confirmation"}, status=400)

            pipeline.status = AISTStatus.PUSH_TO_AI
            pipeline.save(update_fields=["status", "updated"])

        push_request_to_ai.delay(pipeline_id, ids_int, filters)
    except Exception as exc:
        # Return partial error but keep HTTP 200 if you prefer UI to proceed.
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    return JsonResponse({"ok": True, "count": len(found_ids)})


@login_required
@require_POST
def delete_ai_response(request, pipeline_id: str, response_id: int):
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    resp = get_object_or_404(pipeline.ai_responses, id=response_id)
    resp.delete()
    return redirect("dojo_aist:pipeline_detail", pipeline_id=pipeline.id)


@api_view(["POST"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def pipeline_callback(request, pipeline_id: str):
    try:
        get_object_or_404(AISTPipeline, id=pipeline_id)
        response_from_ai = request.data
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    errors = response_from_ai.pop("errors", None)
    logger = _install_db_logging(pipeline_id)
    if errors:
        logger.error(errors)

    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .get(id=pipeline_id)
        )
        AISTAIResponse.objects.create(pipeline=pipeline, payload=response_from_ai)
        pipeline.status = AISTStatus.FINISHED
        pipeline.save(update_fields=["status", "updated"])

    return Response({"ok": True})
