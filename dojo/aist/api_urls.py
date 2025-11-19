from django.urls import path

from .api import (
    AISTProjectDetailAPI,
    AISTProjectListAPI,
    OrganizationCreateAPI,
    PipelineAPI,
    PipelineListAPI,
    PipelineStartAPI,
    ProjectVersionCreateAPI,
    ProjectVersionFileBlobAPI,
)
from .gitlab_integration_api import ImportProjectFromGitlabAPI

app_name = "dojo_aist_api"
urlpatterns = [
    path("projects/", AISTProjectListAPI.as_view(), name="project_list"),
    path("projects/<int:project_id>/", AISTProjectDetailAPI.as_view(), name="project_detail"),
    path("pipelines/start", PipelineStartAPI.as_view(), name="pipeline_start"),
    path("pipelines/<str:pipeline_id>", PipelineAPI.as_view(), name="pipeline_status"),
    path("pipelines/", PipelineListAPI.as_view(), name="pipelines"),
    path(
        "organizations/",
        OrganizationCreateAPI.as_view(),
        name="organization_create",
    ),
    path(
        "projects_version/<int:project_version_id>/files/blob/<path:subpath>",
        ProjectVersionFileBlobAPI.as_view(),
        name="project_version_file_blob",
    ),
    path(
        "projects/<int:project_id>/versions/create/",
        ProjectVersionCreateAPI.as_view(),
        name="project_version_create",
    ),
    path("import_project_from_gitlab", ImportProjectFromGitlabAPI.as_view(), name="import_project_from_gitlab"),
]
