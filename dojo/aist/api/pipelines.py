from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from dojo.aist.ai_filter import get_required_ai_filter_for_start
from dojo.aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from dojo.aist.models import AISTPipeline, AISTProjectVersion, AISTStatus
from dojo.aist.pipeline_args import PipelineArguments
from dojo.aist.tasks import run_sast_pipeline
from dojo.aist.utils.pipeline import create_pipeline_object, has_unfinished_pipeline


class PipelineStartRequestSerializer(serializers.Serializer):
    project_version_id = serializers.IntegerField(required=True)
    ai_filter = serializers.JSONField(required=False, allow_null=True)


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
            405: OpenApiResponse(description="There is already a running pipeline for this project version"),
        },
        examples=[
            OpenApiExample(
                "Start by version id",
                value={
                    "limit": 50,
                    "project_version_id": 123,
                    "ai_filter": {"severity": [
                        {"comparison": "EQUALS", "value": "High"},
                        {"comparison": "EQUALS", "value": "Critical"},
                    ]},
                },
                request_only=True,
            ),
        ],
        tags=["aist"],
        summary="Start pipeline",
        description="Creates and starts AIST Pipeline for the given existing AISTProjectVersion.",
    )
    def post(self, request, *args, **kwargs) -> Response:
        if api_settings.URL_FORMAT_OVERRIDE:
            setattr(request, api_settings.URL_FORMAT_OVERRIDE, None)

        # validate body
        serializer = PipelineStartRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pv_id = serializer.validated_data["project_version_id"]
        # we take project from version to avoid double inputs
        project_version = get_object_or_404(AISTProjectVersion, pk=pv_id)
        project = project_version.project
        provided_ai_filter = serializer.validated_data.get("ai_filter", None)

        if has_unfinished_pipeline(project_version):
            return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

        # AI filter: use default if exists, otherwise require explicit ai_filter

        try:
            _scope, normalized_filter = get_required_ai_filter_for_start(
                project=project,
                provided_filter=provided_ai_filter,
            )
        except ValueError as e:

            return Response(
                {"ai_filter": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = {
            "ai_mode": "AUTO_DEFAULT",
            "ai_filter_snapshot": normalized_filter,
            # keep API behavior stable: no analyzers override here (same as before)
            "analyzers": [],
            "selected_languages": [],
            "rebuild_images": False,
            "log_level": "INFO",
            "time_class_level": None,
            "project_version": project_version.as_dict(),
        }

        params = PipelineArguments.normalize_params(project=project, raw_params=raw)

        # create pipeline in transaction
        with transaction.atomic():
            p = create_pipeline_object(project, project_version, None)

        async_result = run_sast_pipeline.delay(p.id, params)
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
            # Pagination params from LimitOffsetPagination:
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
