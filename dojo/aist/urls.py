from django.urls import path
from django_github_app.views import AsyncWebhookView

from . import ai_views, views

app_name = "dojo_aist"
urlpatterns = [
    path("start", views.start_pipeline, name="start_pipeline"),
    path("pipelines/<str:pipeline_id>/", views.pipeline_detail, name="pipeline_detail"),
    path("pipelines/<str:pipeline_id>/stop/", views.stop_pipeline_view, name="pipeline_stop"),

    path("products/<int:product_id>/analyzers.json", ai_views.product_analyzers_json, name="product_analyzers_json"),
    path("findings/search.json", ai_views.search_findings_json, name="search_findings_json"),
    path("pipelines/<str:pipeline_id>/send_request_to_ai", ai_views.send_request_to_ai, name="send_request_to_ai"),
    path("pipelines/<str:pipeline_id>/callback/", ai_views.pipeline_callback, name="pipeline_callback"),
    path("pipelines/<str:pipeline_id>/ai-response/<int:response_id>/delete/",
         ai_views.delete_ai_response,
         name="delete_ai_response"),

    path("pipelines/<str:pipeline_id>/delete/", views.delete_pipeline_view, name="pipeline_delete"),
    path("pipelines/<str:pipeline_id>/logs/stream/", views.stream_logs_sse, name="pipeline_logs_stream"),
    path("pipelines/<str:pipeline_id>/progress/deduplication", views.deduplication_progress_json, name="deduplication_progress"),

    path("pipeline/<str:pipeline_id>/status/stream/", views.pipeline_status_stream, name="pipeline_status_stream"),
    path("aist/default-analyzers/", views.default_analyzers, name="default_analyzers"),
    path("pipelines/", views.pipeline_list, name="pipeline_list"),
    path("pipelines/<str:pipeline_id>/set_status_push_to_ai", views.pipeline_set_status, name="pipeline_set_status"),  # TODO: make generic
    path("projects/<int:pk>/meta.json", views.project_meta, name="project_meta"),
    path("pipeline/<str:pipeline_id>/progress/enrichment", views.pipeline_enrich_progress_sse, name="pipeline_enrich_progress"),
    path("projects/<int:project_id>/versions/create/", views.project_version_create, name="project_version_create"),

    # Github hooks
    path("github_hook/", AsyncWebhookView.as_view()),
]
