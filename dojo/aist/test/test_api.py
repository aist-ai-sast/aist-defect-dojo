# dojo/aist/tests/test_aist_api.py
"""
Django TestCase-based tests for AIST API endpoints.

Covers:
- GET /aist/projects/ (authentication required, payload shape)
- POST /aist/pipeline/start (creates pipeline and triggers Celery task)
- GET  /aist/pipeline/{id} (returns status and response_from_ai)
- Negative: POST /aist/pipeline/start with unknown project -> 404

All comments are in English by request.
"""

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from dojo.aist.models import AISTPipeline, AISTProject, AISTProjectVersion, AISTStatus
from dojo.models import Product, Product_Type, SLA_Configuration


class AISTApiTests(TestCase):

    """Integration-style tests using Django TestCase and DRF's APIClient."""

    def setUp(self) -> None:
        # Create API client and authenticated user
        self.client = APIClient()
        User = get_user_model()
        self.user = User.objects.create(
            username="tester", email="tester@example.com", password="pass",  # noqa: S106
        )
        # Force authenticate as a logged-in user (works with Session/Token/JWT setups)
        self.client.force_authenticate(user=self.user)

        # Minimal product + AIST project to work with
        self.sla = SLA_Configuration.objects.create(
            name="SLA default for tests",
        )
        self.prod_type = Product_Type.objects.create(name="PT for tests")
        self.product = Product.objects.create(
            name="Test Product", description="desc", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["cpp", "python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
        )

    # ---------- Projects listing ----------

    def test_projects_requires_auth(self):
        """Unauthenticated request must be rejected (401/403 depending on global DRF settings)."""
        anon = APIClient()
        url = reverse("dojo_aist_api:project_list")  # /aist/projects/
        resp = anon.get(url)
        self.assertIn(resp.status_code, (401, 403))

    def test_projects_list_ok(self):
        """Authenticated request returns a paginated list of AIST projects with expected fields."""
        url = reverse("dojo_aist_api:project_list")  # /api/v2/aist/projects/
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # Since ListAPIView + global LimitOffsetPagination => paginated dict
        self.assertIsInstance(data, dict)
        self.assertIn("results", data)
        self.assertIsInstance(data["results"], list)

        # Find our project in the results
        row = next((it for it in data["results"] if it["id"] == self.project.id), None)
        self.assertIsNotNone(row, "Current AISTProject not found in results")

        # Serializer fields defined in AISTProjectSerializer
        self.assertEqual(row["product_name"], "Test Product")  # was "nx_open"
        self.assertIn("supported_languages", row)
        self.assertIn("compilable", row)
        self.assertIn("created", row)
        self.assertIn("updated", row)

    # ---------- Pipeline start ----------

    @patch("dojo.aist.api.run_sast_pipeline")
    def test_pipeline_start_happy_path(self, run_task_mock):
        """POST /pipeline/start returns 201 with pipeline id and triggers Celery task."""
        # Mock Celery .delay() to avoid running real task
        async_res = Mock()
        async_res.id = "celery-12345"
        run_task_mock.delay.return_value = async_res

        url = reverse("dojo_aist_api:pipeline_start")  # /aist/pipeline/start
        payload = {"aistproject_id": self.project.id, "project_version": "test", "create_new_version_if_not_exist": True}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, 201)
        pid = resp.json().get("id")
        self.assertTrue(pid)

        # Pipeline must be created in DB
        p = AISTPipeline.objects.get(id=pid)
        self.assertEqual(p.project_id, self.project.id)
        # On bootstrap your code sets FINISHED before task actually begins (matches api.py)
        self.assertEqual(p.status, AISTStatus.FINISHED)

        # AISTProject version must be created in DB
        AISTProjectVersion.objects.get(project=self.project, version="test")

        # Celery task must be triggered with (pipeline_id, params)
        run_task_mock.delay.assert_called_once()
        args, _kwargs = run_task_mock.delay.call_args
        self.assertEqual(args[0], pid)  # pipeline_id
        # NOTE: in current code api.py passes None as params.
        # If you change it to {}, relax this assertion accordingly.
        self.assertIsNone(args[1])

    def test_pipeline_start_404_on_unknown_project(self):
        """POST /pipeline/start with non-existing project must return 404."""
        url = reverse("dojo_aist_api:pipeline_start")
        payload = {"aistproject_id": 999999, "project_version": "test", "create_new_version_if_not_exist": False}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_start_404_on_unknown_version(self):
        """POST /pipeline/start with non-existing version must return 404."""
        url = reverse("dojo_aist_api:pipeline_start")
        payload = {"aistproject_id": self.project.id, "project_version": "lalalala",
                   "create_new_version_if_not_exist": False}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, 404)

    # ---------- Pipeline status ----------

    def test_pipeline_status_returns_response_from_ai(self):
        """
        GET /pipeline/{id} returns status and response_from_ai.

        This asserts the desired contract. If this test fails returning None,
        check that the API uses 'response_from_ai' (not 'response') in the payload.
        """
        p = AISTPipeline.objects.create(
            id="abc12345",
            project=self.project,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
            response_from_ai={"state": "pending", "details": {"x": 1}},
        )
        url = reverse("dojo_aist_api:pipeline_status", kwargs={"pipeline_id": p.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["id"], p.id)
        self.assertEqual(body["status"], AISTStatus.WAITING_RESULT_FROM_AI)
        # Must echo the JSON stored in model.response_from_ai
        self.assertEqual(body["response_from_ai"], {"state": "pending", "details": {"x": 1}})
        self.assertIn("created", body)
        self.assertIn("updated", body)

    def test_project_detail_get_ok(self):
        """GET /projects/{project_id}/ returns a single AISTProject by id."""
        url = reverse("dojo_aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Serializer fields defined in AISTProjectSerializer
        self.assertEqual(body["id"], self.project.id)
        self.assertEqual(body["product_name"], "Test Product")
        self.assertIn("supported_languages", body)
        self.assertIn("compilable", body)
        self.assertIn("created", body)
        self.assertIn("updated", body)

    def test_project_detail_get_404(self):
        """GET /projects/{project_id}/ with non-existing id returns 404."""
        url = reverse("dojo_aist_api:project_detail", kwargs={"project_id": 999999})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_project_delete_requires_auth(self):
        """DELETE /projects/{project_id}/ must require authentication."""
        anon = APIClient()
        url = reverse("dojo_aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = anon.delete(url)
        self.assertIn(resp.status_code, (401, 403))

    def test_project_delete_ok(self):
        """DELETE /projects/{project_id}/ deletes the project and returns 204."""
        url = reverse("dojo_aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        # Ensure the object is gone
        with self.assertRaises(AISTProject.DoesNotExist):
            AISTProject.objects.get(id=self.project.id)
