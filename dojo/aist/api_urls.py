from .api import AISTProjectListAPI, ProjectVersionFileBlobAPI, PipelineStartAPI, PipelineAPI, PipelineListAPI, AISTProjectDetailAPI
from django.urls import path

app_name = "dojo_aist_api"
urlpatterns = [
    path("projects/", AISTProjectListAPI.as_view(), name="project_list"),
    path("projects/<int:id>/", AISTProjectDetailAPI.as_view(), name="project_detail"),
    path("pipelines/start", PipelineStartAPI.as_view(), name="pipeline_start"),
    path("pipelines/<str:id>", PipelineAPI.as_view(), name="pipeline_status"),
    path("pipelines/", PipelineListAPI.as_view(), name="pipelines"),
    path(
        "projects_version/<int:id>/files/blob/<path:subpath>",
        ProjectVersionFileBlobAPI.as_view(),
        name="project_version_file_blob",
    ),
]