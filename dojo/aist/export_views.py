import csv
from io import BytesIO, StringIO

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from openpyxl import Workbook

from .models import AISTPipeline


def _build_ai_export_rows(
    pipeline: AISTPipeline,
    ai_payload: dict,
    ignore_false_positives,
) -> list[dict]:
    """
    Normalize AI payload into a flat list of findings suitable for tabular export.

    The function:
    - merges all list-like collections from payload["results"] (e.g. true_positives, uncertainly)
    - maps nested originalFinding.* fields into flat columns
    - optionally filters out items with falsePositive == True
    - keeps impactScore in each row for sorting, but does not expose it as a visible column
    """
    results = ai_payload.get("results") or {}
    findings_raw: list[dict] = []

    if isinstance(results, dict):
        for value in results.values():
            if isinstance(value, list):
                findings_raw.extend([item for item in value if isinstance(item, dict)])
    elif isinstance(results, list):
        findings_raw = [item for item in results if isinstance(item, dict)]

    rows: list[dict] = []
    for item in findings_raw:
        original = item.get("originalFinding") or {}

        # Project version is expected to come from AI response when available;
        # we fall back to the pipeline's project_version label.
        project_version_label = (
            item.get("projectVersion")
            or getattr(pipeline.project_version, "version", None)
            or ""
        )

        row = {
            "title": item.get("title") or "",
            "project_version": project_version_label,
            "cwe": original.get("cwe") or "",
            "file": original.get("file") or "",
            "line": original.get("line") or "",
            # "description" is taken from AI explanation; adjust if your schema differs.
            "description": item.get("reasoning") or "",
            # "code_snippet" is taken from originalFinding.snippet when available.
            "code_snippet": original.get("snippet") or "",
            "false_positive": bool(item.get("falsePositive")),
            "impactScore": item.get("impactScore") or 0,
        }

        if ignore_false_positives and row["false_positive"]:
            continue

        rows.append(row)

    # Sort by impactScore descending: highest impact first.
    rows.sort(key=lambda r: r.get("impactScore") or 0, reverse=True)
    return rows


def _export_ai_results_csv(
    pipeline: AISTPipeline,
    columns: list[str],
    header_map: dict[str, str],
    rows: list[dict],
) -> HttpResponse:
    """
    Export normalized AI rows as a CSV file.

    The CSV is UTF-8 encoded and uses comma as a delimiter.
    """
    buffer = StringIO()
    writer = csv.writer(buffer)

    writer.writerow([header_map[c] for c in columns])
    for row in rows:
        writer.writerow([row.get(c, "") for c in columns])

    resp = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="aist_ai_results_{pipeline.id}.csv"'
    return resp


def _export_ai_results_excel(
    pipeline: AISTPipeline,
    columns: list[str],
    header_map: dict[str, str],
    rows: list[dict],
) -> HttpResponse:
    """
    Export normalized AI rows as an XLSX file.

    If openpyxl is not installed, falls back to CSV export.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "AI results"

    ws.append([header_map[c] for c in columns])
    for row in rows:
        ws.append([row.get(c, "") for c in columns])

    buffer = BytesIO()
    wb.save(buffer)

    resp = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="aist_ai_results_{pipeline.id}.xlsx"'
    return resp


@login_required
@require_http_methods(["POST"])
def export_ai_results(request: HttpRequest, pipeline_id: str) -> HttpResponse:
    """
    Export the latest AI response for a pipeline as a tabular file.

    All heavy lifting (parsing AI payload, sorting by impactScore, filtering
    false positives and limiting the number of findings) happens on the backend.
    """
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)

    # Use the most recent AI response for this pipeline.
    ai_response = (
        pipeline.ai_responses
        .order_by("-created")
        .first()
    )
    if not ai_response or not ai_response.payload:
        return HttpResponseBadRequest("No AI responses available for export.")

    payload = ai_response.payload or {}

    fmt = (request.POST.get("format") or "csv").lower()

    selected_columns = request.POST.getlist("columns") or [
        "title",
        "project_version",
        "cwe",
        "file",
        "line",
        "description",
        "code_snippet",
    ]

    ignore_fp = (request.POST.get("ignore_false_positives") or "1").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    export_all = (request.POST.get("export_all") or "").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }

    max_findings_raw = (request.POST.get("max_findings") or "").strip()
    max_findings: int | None
    try:
        max_findings_val = int(max_findings_raw) if max_findings_raw else None
        max_findings = max_findings_val if max_findings_val and max_findings_val > 0 else None
    except ValueError:
        max_findings = None

    rows = _build_ai_export_rows(pipeline, payload, ignore_false_positives=ignore_fp)

    if not rows:
        return HttpResponseBadRequest("No findings matched the selected filters.")

    if not export_all and max_findings is not None:
        rows = rows[:max_findings]

    # Ensure that "false_positive" column is present when we include false positives.
    if not ignore_fp and "false_positive" not in selected_columns:
        selected_columns.append("false_positive")

    # Normalize and validate requested columns.
    valid_columns = {
        "title",
        "project_version",
        "cwe",
        "file",
        "line",
        "description",
        "code_snippet",
        "false_positive",
    }
    final_columns: list[str] = []
    seen: set[str] = set()
    for col in selected_columns:
        if col in valid_columns and col not in seen:
            seen.add(col)
            final_columns.append(col)

    if not final_columns:
        # Sensible default if user somehow unchecks everything.
        final_columns = ["title", "project_version", "cwe", "file", "line"]

    header_map = {
        "title": "Title",
        "project_version": "Project version",
        "cwe": "CWE",
        "file": "File",
        "line": "Line",
        "description": "Description",
        "code_snippet": "Code snippet",
        "false_positive": "False positive",
    }

    if fmt == "csv":
        return _export_ai_results_csv(pipeline, final_columns, header_map, rows)
    if fmt in {"xlsx", "excel", "xls"}:
        return _export_ai_results_excel(pipeline, final_columns, header_map, rows)
    if fmt == "pdf":
        # Placeholder: implement a real PDF table export (e.g. using ReportLab) if needed.
        return HttpResponseBadRequest("PDF export is not implemented yet.")

    return HttpResponseBadRequest(f"Unsupported export format: {fmt}")
