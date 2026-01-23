from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from dojo.aist.forms import (
    AISTLaunchConfigForm,
    AISTProjectVersionForm,
    _load_analyzers_config,
    _signature,
)
from dojo.aist.models import AISTLaunchConfigAction, AISTProject, AISTStatus, Organization
from dojo.aist.views._common import ERR_CONFIG_NOT_LOADED, ERR_PROJECT_NOT_FOUND
from dojo.utils import add_breadcrumb


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
        },
        )

    html = render(request, "dojo/aist/_project_version_form.html", {"form": form, "project": project}).content.decode(
        "utf-8",
        )
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
    },
    )


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
    },
    )


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
        .prefetch_related("projects__product", "projects__repository")
        .order_by("name")
    )

    # Projects that are not assigned to any organization -> "Others" section.
    unassigned_projects = (
        AISTProject.objects
        .select_related("product", "repository")
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
            "launch_config_form": AISTLaunchConfigForm(),
            "aist_status_choices": AISTStatus.choices,
            "aist_action_types": AISTLaunchConfigAction.ActionType.choices,
        },
    )
