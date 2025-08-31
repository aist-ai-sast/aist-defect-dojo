# dojo/aist/api.py
from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, generics
from rest_framework.response import Response
from pathlib import Path
from django.http import FileResponse, Http404
from mimetypes import guess_type
from django.utils.encoding import iri_to_uri
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, OpenApiParameter

from .models import AISTPipeline, AISTProject, AISTStatus, AISTProjectVersion
from dojo.aist.tasks import run_sast_pipeline
from .utils import create_pipeline_object
from drf_spectacular.types import OpenApiTypes


class PipelineStartRequestSerializer(serializers.Serializer):
    """Minimal payload to start a pipeline."""
    aistproject_id = serializers.IntegerField(required=True)
    project_version = serializers.CharField(required=True)
    create_new_version_if_not_exist = serializers.BooleanField(required=True)


class PipelineStartResponseSerializer(serializers.Serializer):
    id = serializers.CharField()


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
            201: OpenApiResponse(PipelineStartResponseSerializer, description="Pipeline created"),
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Project or project version not found"),
        },
        examples=[
            OpenApiExample(
                "Basic start",
                value={"aistproject_id": 42, "project_version": "master", "create_new_version_if_not_exist": True},
                request_only=True,
            )
        ],
        tags=["aist"],
        summary="Start pipeline",
        description=(
            "Creates and starts AIST Pipeline for the given AISTProject with default parameters."
        ),
    )
    def post(self, request, *args, **kwargs) -> Response:
        serializer = PipelineStartRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = serializer.validated_data["aistproject_id"]
        project = get_object_or_404(AISTProject, pk=project_id)

        project_version_hash = serializer.validated_data["project_version"]
        create_new_version = serializer.validated_data["create_new_version_if_not_exist"]

        if create_new_version:
            project_version, _created = AISTProjectVersion.objects.get_or_create(
                project=project, version=project_version_hash)
        else:
            project_version = get_object_or_404(AISTProjectVersion, project=project, version=project_version_hash)

        with transaction.atomic():
            p = create_pipeline_object(project, project_version, None)

        async_result = run_sast_pipeline.delay(p.id, None)
        p.run_task_id = async_result.id
        p.save(update_fields=["run_task_id"])

        out = PipelineStartResponseSerializer({"id": p.id})
        return Response(out.data, status=status.HTTP_201_CREATED)

class PipelineListAPI(generics.ListAPIView):
    """
    Paginated list of pipelines with simple filtering.
    """
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
    def get(self, request, id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTPipeline, id=id)
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
    def delete(self, request, id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTPipeline, id=id)
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
    """
    List all current AISTProjects.
    """
    queryset = AISTProject.objects.select_related("product").all().order_by("created")
    serializer_class = AISTProjectSerializer
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List all AISTProjects",
        description="Returns all existing AISTProject records with their metadata."
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
    def delete(self, request, id: int, *args, **kwargs) -> Response:
        p = get_object_or_404(AISTProject, id=id)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        responses={404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Get AIST project",
        description="Get the specified AISTProject by id.",
    )
    def get(self, request, id: int, *args, **kwargs) -> Response:
        project = get_object_or_404(AISTProject, id=id)
        serializer = AISTProjectSerializer(project)
        return Response(serializer.data, status=status.HTTP_200_OK)

class ProjectVersionFileBlobAPI(APIView):
    """
    GET /projects_version/<id>/files/blob/<path:subpath>
    Returns the specified file from project version.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Get file from extracted project version archive",
        description=(
            "Returns the raw bytes of a file located **inside** the extracted archive of the specified "
            "AIST project version. If the archive hasn't been extracted yet, it will be extracted once."
        ),
        parameters=[
            OpenApiParameter(
                name="id",
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

    def get(self, request, id: int, subpath: str, *args, **kwargs):
        pv = get_object_or_404(AISTProjectVersion, pk=id)

        # Generate one-time extraction
        root = pv.ensure_extracted()

        if root is None:
            raise Http404("File not found in version archive")


        safe_rel = subpath.lstrip("/").replace("\\", "/")
        file_path = (root / safe_rel).resolve()

        if not file_path.exists() or not file_path.is_file():
            raise Http404("File not found in version archive")


        ctype, _ = guess_type(str(file_path))
        ctype = ctype or "application/octet-stream"

        resp = FileResponse(open(file_path, "rb"), content_type=ctype)
        resp["Content-Disposition"] = f'inline; filename="{iri_to_uri(file_path.name)}"'
        return resp
