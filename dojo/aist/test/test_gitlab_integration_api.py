from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from dojo.aist.models import AISTProject, Organization, RepositoryInfo, ScmGitlabBinding, ScmType
from dojo.models import Product, Product_Type, SLA_Configuration


class GitlabIntegrationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)
        SLA_Configuration.objects.bulk_create(
            [SLA_Configuration(id=1, name="SLA default")],
            ignore_conflicts=True,
        )

    def _url(self):
        return reverse("dojo_aist_api:import_project_from_gitlab")

    def _token_url(self, project_id: int):
        return reverse("dojo_aist_api:project_gitlab_token_update", kwargs={"project_id": project_id})

    @staticmethod
    def _resp(status_code, payload=None):
        resp = Mock()
        resp.status_code = status_code
        resp.json.return_value = payload or {}

        def _raise_for_status():
            if status_code >= 400:
                msg = "http error"
                raise Exception(msg)

        resp.raise_for_status = Mock(side_effect=_raise_for_status)
        return resp

    @patch("dojo.aist.api.gitlab_integration._load_analyzers_config")
    @patch("dojo.aist.api.gitlab_integration.requests.get")
    def test_import_gitlab_project_happy_path(self, mock_get, mock_cfg):
        org = Organization.objects.create(name="Org")

        mock_cfg.return_value = Mock(convert_languages=Mock(return_value=["python"]))

        project_payload = {
            "path_with_namespace": "group/my-repo",
            "description": "desc",
            "web_url": "https://gitlab.example.com/group/my-repo",
        }
        langs_payload = {"Python": 80.0, "Go": 20.0}
        mock_get.side_effect = [
            self._resp(200, project_payload),
            self._resp(200, langs_payload),
        ]

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "gitlab_api_token": "token",
                "base_url": "https://gitlab.example.com",
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertIn("aist_project_id", resp.data)

        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(aist_project.organization_id, org.id)
        self.assertEqual(aist_project.repository.type, ScmType.GITLAB)

        repo = RepositoryInfo.objects.get(id=resp.data["repository_id"])
        binding = ScmGitlabBinding.objects.get(scm=repo)
        self.assertEqual(binding.personal_access_token, "token")

    @patch("dojo.aist.api.gitlab_integration.requests.get")
    def test_import_gitlab_project_returns_404(self, mock_get):
        mock_get.return_value = self._resp(404, {})

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 999,
                "gitlab_api_token": "token",
                "base_url": "https://gitlab.example.com",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 404)

    @patch("dojo.aist.api.gitlab_integration._load_analyzers_config")
    @patch("dojo.aist.api.gitlab_integration.requests.get")
    def test_import_gitlab_project_allows_empty_organization(self, mock_get, mock_cfg):
        mock_cfg.return_value = Mock(convert_languages=Mock(return_value=["python"]))

        project_payload = {
            "path_with_namespace": "group/my-repo",
            "description": "desc",
            "web_url": "https://gitlab.example.com/group/my-repo",
        }
        langs_payload = {"Python": 80.0, "Go": 20.0}
        mock_get.side_effect = [
            self._resp(200, project_payload),
            self._resp(200, langs_payload),
        ]

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "gitlab_api_token": "token",
                "base_url": "https://gitlab.example.com",
                "organization_id": "",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertIsNone(aist_project.organization_id)

    def test_import_gitlab_project_requires_token(self):
        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "gitlab_api_token": "",
                "base_url": "https://gitlab.example.com",
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400)

    def test_update_gitlab_token_happy_path(self):
        product_type = Product_Type.objects.create(name="Gitlab Imported")
        product = Product.objects.create(
            name="repo",
            description="desc",
            prod_type=product_type,
            sla_configuration_id=1,
        )
        repo = RepositoryInfo.objects.create(
            type=ScmType.GITLAB,
            repo_owner="group",
            repo_name="repo",
            base_url="https://gitlab.example.com",
        )
        binding = ScmGitlabBinding.objects.create(scm=repo, personal_access_token="old")  # noqa: S106
        project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            script_path="input_projects/default_imported_project_no_built.sh",
            compilable=False,
            profile={},
            repository=repo,
        )

        resp = self.client.post(
            self._token_url(project.id),
            data={"gitlab_api_token": "new-token"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        binding.refresh_from_db()
        self.assertEqual(binding.personal_access_token, "new-token")

    def test_update_gitlab_token_requires_gitlab_repo(self):
        product_type = Product_Type.objects.create(name="Other")
        product = Product.objects.create(
            name="repo",
            description="desc",
            prod_type=product_type,
            sla_configuration_id=1,
        )
        repo = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="group",
            repo_name="repo",
            base_url="https://github.com",
        )
        project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            script_path="input_projects/default_imported_project_no_built.sh",
            compilable=False,
            profile={},
            repository=repo,
        )

        resp = self.client.post(
            self._token_url(project.id),
            data={"gitlab_api_token": "new-token"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)

    def test_update_gitlab_token_requires_token(self):
        product_type = Product_Type.objects.create(name="Gitlab Imported")
        product = Product.objects.create(
            name="repo",
            description="desc",
            prod_type=product_type,
            sla_configuration_id=1,
        )
        repo = RepositoryInfo.objects.create(
            type=ScmType.GITLAB,
            repo_owner="group",
            repo_name="repo",
            base_url="https://gitlab.example.com",
        )
        project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            script_path="input_projects/default_imported_project_no_built.sh",
            compilable=False,
            profile={},
            repository=repo,
        )

        resp = self.client.post(
            self._token_url(project.id),
            data={"gitlab_api_token": ""},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
