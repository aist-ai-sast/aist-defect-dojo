from django.urls import path

from dojo.aist.api import (
    AISTProjectDetailAPI,
    AISTProjectListAPI,
    LaunchConfigDashboardListAPI,
    LaunchScheduleBulkDisableAPI,
    LaunchScheduleDetailAPI,
    LaunchScheduleListAPI,
    LaunchSchedulePreviewAPI,
    LaunchScheduleRunOnceAPI,
    OrganizationCreateAPI,
    PipelineAPI,
    PipelineLaunchQueueClearDispatchedAPI,
    PipelineLaunchQueueDetailAPI,
    PipelineLaunchQueueListAPI,
    PipelineListAPI,
    PipelineStartAPI,
    ProjectGitlabTokenUpdateAPI,
    ProjectLaunchConfigActionDetailAPI,
    ProjectLaunchConfigActionListCreateAPI,
    ProjectLaunchConfigDetailAPI,
    ProjectLaunchConfigListCreateAPI,
    ProjectLaunchConfigStartAPI,
    ProjectLaunchScheduleUpsertAPI,
    ProjectVersionCreateAPI,
    ProjectVersionFileBlobAPI,
)
from dojo.aist.api.gitlab_integration import ImportProjectFromGitlabAPI

app_name = "dojo_aist_api"
urlpatterns = [
    path("projects/", AISTProjectListAPI.as_view(), name="project_list"),
    path("projects/<int:project_id>/", AISTProjectDetailAPI.as_view(), name="project_detail"),
    path("pipelines/start/", PipelineStartAPI.as_view(), name="pipeline_start"),
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
    path("import_project_from_gitlab/", ImportProjectFromGitlabAPI.as_view(), name="import_project_from_gitlab"),
    path(
        "projects/<int:project_id>/gitlab-token/",
        ProjectGitlabTokenUpdateAPI.as_view(),
        name="project_gitlab_token_update",
    ),
    path(
        "projects/<int:project_id>/launch-configs/",
        ProjectLaunchConfigListCreateAPI.as_view(),
        name="project_launch_config_list_create",
    ),
    path(
        "projects/<int:project_id>/launch-configs/<int:config_id>/",
        ProjectLaunchConfigDetailAPI.as_view(),
        name="project_launch_config_detail",
    ),
    path(
        "projects/<int:project_id>/launch-configs/<int:config_id>/actions/",
        ProjectLaunchConfigActionListCreateAPI.as_view(),
        name="project_launch_config_action_list_create",
    ),
    path(
        "projects/<int:project_id>/launch-configs/<int:config_id>/actions/<int:action_id>/",
        ProjectLaunchConfigActionDetailAPI.as_view(),
        name="project_launch_config_action_detail",
    ),
    path(
        "projects/<int:project_id>/launch-configs/<int:config_id>/start/",
        ProjectLaunchConfigStartAPI.as_view(),
        name="project_launch_config_start",
    ),
    path(
        "launch-configs/",
        LaunchConfigDashboardListAPI.as_view(),
        name="launch_config_dashboard_list",
    ),
    path(
        "projects/<int:project_id>/launch-schedule/",
        ProjectLaunchScheduleUpsertAPI.as_view(),
        name="project_launch_schedule_upsert",
    ),
    path(
        "launch-schedules/",
        LaunchScheduleListAPI.as_view(),
        name="launch_schedule_list",
    ),
    path(
        "launch-schedules/<int:launch_schedule_id>/",
        LaunchScheduleDetailAPI.as_view(),
        name="launch_schedule_detail",
    ),
path("launch-schedules/preview/", LaunchSchedulePreviewAPI.as_view(), name="launch_schedule_preview"),
path("launch-schedules/bulk-disable/", LaunchScheduleBulkDisableAPI.as_view(), name="launch_schedule_bulk_disable"),
path("launch-queue/", PipelineLaunchQueueListAPI.as_view(), name="pipeline_launch_queue_list"),
path("launch-queue/clear-dispatched/", PipelineLaunchQueueClearDispatchedAPI.as_view(), name="pipeline_launch_queue_clear_dispatched"),
path("launch-queue/<int:queue_id>/", PipelineLaunchQueueDetailAPI.as_view(), name="pipeline_launch_queue_detail"),
path(
    "launch-schedules/<int:launch_schedule_id>/run-once/",
    LaunchScheduleRunOnceAPI.as_view(),
    name="launch_schedule_run_once",
),
]
