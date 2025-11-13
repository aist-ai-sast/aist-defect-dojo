# dojo/aist/api.py
from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path

import requests
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseServerError
from django.shortcuts import get_object_or_404
from django.utils.encoding import iri_to_uri
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.aist.tasks import run_sast_pipeline

from .link_builder import LinkBuilder
from .models import AISTPipeline, AISTProject, AISTProjectVersion, AISTStatus, VersionType
from .utils import _import_sast_pipeline_package, create_pipeline_object, get_project_build_path

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import read_repo_params  # type: ignore[import-not-found]  # noqa: E402

# ----------------------------
# Module-level error messages
# ----------------------------
ERR_FILE_NOT_FOUND_IN_ARCHIVE = "File not found in version archive"


class PipelineStartRequestSerializer(serializers.Serializer):
    project_version_id = serializers.IntegerField(required=True)


class PipelineResponseSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()
    response_from_ai = serializers.JSONField(allow_null=True)
    created = serializers.DateTimeField()
    updated = serializers.DateTimeField()


class PipelineStartAPI(APIView):

    """Start a new AIST pipeline."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PipelineStartRequestSerializer,
        responses={
            201: OpenApiResponse(PipelineResponseSerializer, description="Pipeline created"),
            404: OpenApiResponse(description="Project version not found"),
        },
        examples=[
            OpenApiExample(
                "Start by version id",
                value={"project_version_id": 123},
                request_only=True,
            ),
        ],
        tags=["aist"],
        summary="Start pipeline",
        description="Creates and starts AIST Pipeline for the given existing AISTProjectVersion.",
    )
    def post(self, request, *args, **kwargs) -> Response:
        # validate body
        serializer = PipelineStartRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pv_id = serializer.validated_data["project_version_id"]
        # we take project from version to avoid double inputs
        project_version = get_object_or_404(AISTProjectVersion, pk=pv_id)
        project = project_version.project

        # create pipeline in transaction
        with transaction.atomic():
            p = create_pipeline_object(project, project_version, None)

        async_result = run_sast_pipeline.delay(p.id, None)
        p.run_task_id = async_result.id
        p.save(update_fields=["run_task_id"])

        out = PipelineResponseSerializer(
            {"id": p.id, "status": p.status, "response_from_ai": p.response_from_ai, "created": p.created,
             "updated": p.updated})
        return Response(out.data, status=status.HTTP_201_CREATED)


class PipelineListAPI(generics.ListAPIView):

    """Paginated list of pipelines with simple filtering."""

    permission_classes = [IsAuthenticated]
    serializer_class = PipelineResponseSerializer

    @extend_schema(
        tags=["aist"],
        summary="List pipelines",
        description=(
            "Returns a paginated list of AIST pipelines. "
            "Filters: project_id, status, created_gte/lte (ISO8601). "
            "Ordering: created, -created, updated, -updated."
        ),
        parameters=[
            OpenApiParameter(name="project_id", location=OpenApiParameter.QUERY, description="Filter by AISTProject id", required=False, type=int),
            OpenApiParameter(name="status", location=OpenApiParameter.QUERY, description="Filter by status (string/choice)", required=False, type=str),
            OpenApiParameter(name="created_gte", location=OpenApiParameter.QUERY, description="Created >= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="created_lte", location=OpenApiParameter.QUERY, description="Created <= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="ordering", location=OpenApiParameter.QUERY, description="created | -created | updated | -updated", required=False, type=str),
            # Параметры пагинации из LimitOffsetPagination:
            OpenApiParameter(name="limit", location=OpenApiParameter.QUERY, required=False, type=int),
            OpenApiParameter(name="offset", location=OpenApiParameter.QUERY, required=False, type=int),
        ],
        responses={200: PipelineResponseSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            AISTPipeline.objects
            .select_related("project", "project_version")
            .all()
            .order_by("-created")
        )
        qp = self.request.query_params

        project_id = qp.get("project_id")
        status = qp.get("status")
        created_gte = qp.get("created_gte")
        created_lte = qp.get("created_lte")
        ordering = qp.get("ordering")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if status:
            qs = qs.filter(status=status)
        if created_gte:
            qs = qs.filter(created__gte=created_gte)
        if created_lte:
            qs = qs.filter(created__lte=created_lte)
        if ordering in {"created", "-created", "updated", "-updated"}:
            qs = qs.order_by(ordering)

        return qs


class PipelineAPI(APIView):

    """Retrieve or delete a pipeline by id."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: PipelineResponseSerializer, 404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Get pipeline status",
        description="Returns pipeline status and AI response.",
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTPipeline, id=pipeline_id)
        data = {
            "id": p.id,
            "status": p.status,
            "response_from_ai": p.response_from_ai,
            "created": p.created,
            "updated": p.updated,
        }
        out = PipelineResponseSerializer(data)
        return Response(out.data, status=status.HTTP_200_OK)

    @extend_schema(
        responses={204: OpenApiResponse(description="Pipeline deleted"),
                   400: OpenApiResponse(description="Cannot delete pipeline"),
                   404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Delete pipeline",
        description="Deletes the specified AISTPipeline by id.",
    )
    def delete(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTPipeline, id=pipeline_id)
        if p.status != AISTStatus.FINISHED:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTProjectSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = AISTProject
        fields = ["id", "product_name", "supported_languages", "compilable", "created", "updated", "repository"]


class AISTProjectListAPI(generics.ListAPIView):

    """List all current AISTProjects."""

    queryset = AISTProject.objects.select_related("product").all().order_by("created")
    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List all AISTProjects",
        description="Returns all existing AISTProject records with their metadata.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class AISTProjectDetailAPI(generics.RetrieveDestroyAPIView):
    queryset = AISTProject.objects.select_related("product").all()
    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    @extend_schema(
        responses={204: OpenApiResponse(description="AIST project deleted"), 404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Delete AIST project",
        description="Deletes the specified AISTProject by id.",
    )
    def delete(self, request, project_id: int, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTProject, id=project_id)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        responses={404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Get AIST project",
        description="Get the specified AISTProject by id.",
    )
    def get(self, request, project_id: int, *args, **kwargs) -> Response:
        project = get_object_or_404(AISTProject, id=project_id)
        serializer = AISTProjectSerializer(project)
        return Response(serializer.data, status=status.HTTP_200_OK)


class _NoBodySerializer(serializers.Serializer):

    """Empty serializer used to satisfy schema generation for APIView-like endpoints."""


class ProjectVersionFileBlobAPI(generics.GenericAPIView):

    """
    GET /projects_version/<id>/files/blob/<path:subpath>
    Returns the specified file from project version.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = _NoBodySerializer

    @extend_schema(
        tags=["aist"],
        summary="Get file from extracted project version archive",
        description=(
            "Returns the raw bytes of a file located **inside** the extracted archive of the specified "
            "AIST project version. If the archive hasn't been extracted yet, it will be extracted once."
        ),
        parameters=[
            OpenApiParameter(
                name="project_version_id",
                location=OpenApiParameter.PATH,
                description="AISTProjectVersion ID",
                required=True,
                type=int,
            ),
            OpenApiParameter(
                name="subpath",
                location=OpenApiParameter.PATH,
                description="Relative path inside the extracted archive (e.g. `src/main.py`)",
                required=True,
                type=str,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpenApiTypes.BINARY,
                description="Raw file content (binary stream)",
            ),
            404: OpenApiResponse(description="Project version or file not found"),
        },
    )
    def _return_remote_bytes(self, url: str, filename: str, extra_headers: dict | None = None):
        """Download the file from a remote URL and return as HttpResponse."""
        headers = dict(extra_headers or {})
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)

        if response.status_code == 404:
            raise Http404("File not found in remote repository")
        response.raise_for_status()

        content_type, _ = guess_type(filename)
        content_type = content_type or "application/octet-stream"

        resp = HttpResponse(response.content, content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(filename)}"'
        return resp

    @staticmethod
    def _return_local_file(project_version, subpath):
        root = project_version.ensure_extracted()
        if root is None:
            raise Http404(ERR_FILE_NOT_FOUND_IN_ARCHIVE)

        safe_rel = subpath.lstrip("/").replace("\\", "/")
        file_path = (root / safe_rel).resolve()
        if not file_path.exists() or not file_path.is_file():
            raise Http404(ERR_FILE_NOT_FOUND_IN_ARCHIVE)

        content_type, _ = guess_type(str(file_path))
        content_type = content_type or "application/octet-stream"
        resp = FileResponse(file_path.open("rb"), content_type=content_type)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(file_path.name)}"'
        return resp

    def get(self, request, project_version_id: int, subpath: str, *args, **kwargs):
        project_version = get_object_or_404(AISTProjectVersion, pk=project_version_id)

        # --- Case 1: Local FILE_HASH (from extracted archive) ---
        if project_version.version_type == VersionType.FILE_HASH:
            return self._return_local_file(project_version, subpath)

        # --- Case 2: Git-based version (GIT_HASH) ---
        ref = (project_version.version or "master").strip()
        repo_obj = getattr(project_version.project, "repository", None)

        link_builder = LinkBuilder({"id": project_version.id})
        if repo_obj:
            # Try using existing SCM binding (GitHub/GitLab)
            binding = repo_obj.get_binding()
            if binding:
                raw_url = binding.build_raw_url(repo_obj, ref, subpath)
                headers = binding.get_auth_headers() or {}
                return self._return_remote_bytes(raw_url, Path(subpath).name, headers)

            # Fallback to public blob/raw URL if no binding configured
            raw_url = link_builder.build_raw_url(repo_obj.host(), ref, subpath)
            return self._return_remote_bytes(raw_url, Path(subpath).name, {})

        # --- Case 3: No repository_info, use local build path + repo_info ---
        try:
            repo_path = get_project_build_path(project_version.project.product.name,
                                               project_version.version or "default")
        except RuntimeError:
            return HttpResponseServerError()

        # Read Git metadata from local repo
        params = read_repo_params(str(repo_path))
        raw_url = link_builder.build_raw_url(params.repo_url, params.branch_tag or ref, subpath)
        return self._return_remote_bytes(raw_url, Path(subpath).name, {})


class AISTProjectVersionCreateSerializer(serializers.ModelSerializer):

    """
    Serializer for creating AISTProjectVersion instances via API.
    Performs the same validations as AISTProjectVersionForm:
    - For FILE_HASH requires `source_archive`
    - For GIT_HASH requires `version`
    - Ensures the combination (project, version) is unique
    """

    id = serializers.IntegerField(read_only=True)
    project = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = AISTProjectVersion
        fields = ("id", "project", "version_type", "version", "source_archive")
        extra_kwargs = {
            "version": {"required": False, "allow_blank": True},
            "source_archive": {"required": False},
        }

    def validate(self, attrs):
        project = self.context.get("project")
        if project is None:
            raise serializers.ValidationError({"project": "Project is required."})

        version_type = attrs.get("version_type")
        version = attrs.get("version") or ""
        source_archive = attrs.get("source_archive")

        if version_type == VersionType.FILE_HASH and not source_archive:
            raise serializers.ValidationError(
                {"source_archive": "This field is required for FILE_HASH versions."},
            )

        if version_type == VersionType.GIT_HASH and not version:
            raise serializers.ValidationError(
                {"version": "This field is required for GIT_HASH versions."},
            )

        if version:
            exists = AISTProjectVersion.objects.filter(
                project=project, version=version,
            ).exists()
            if exists:
                raise serializers.ValidationError(
                    {"version": "This version already exists for this project."},
                )

        attrs["project"] = project
        return attrs

    def create(self, validated_data):
        # for FILE_HASH without explicit version the model will set sha256 in save()
        return AISTProjectVersion.objects.create(**validated_data)


class ProjectVersionCreateAPI(APIView):

    """API endpoint for creating AISTProjectVersion instances."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        methods=["post"],
        request=AISTProjectVersionCreateSerializer,
        responses={
            201: OpenApiResponse(
                AISTProjectVersionCreateSerializer,
                description="Project version created successfully",
            ),
            400: OpenApiResponse(
                description="Validation failed",
            ),
            404: OpenApiResponse(
                description="Project not found",
            ),
        },
    )
    def post(self, request, project_id):
        project = get_object_or_404(AISTProject, pk=project_id)

        serializer = AISTProjectVersionCreateSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        version = serializer.save()

        out = AISTProjectVersionCreateSerializer(instance=version, context={"project": project})
        return Response(out.data, status=status.HTTP_201_CREATED)
