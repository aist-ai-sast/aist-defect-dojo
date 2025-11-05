# dojo/aist/tests/test_aist_api.py
"""
Django TestCase-based tests for AIST API endpoints.

Covers:
- GET /aist/projects/ (authentication required, payload shape)
- POST /aist/pipeline/start (creates pipeline and triggers Celery task)
- GET  /aist/pipeline/{id} (returns status and response_from_ai)
- Negative: POST /aist/pipeline/start with unknown project -> 404

"""
import io
import zipfile
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from dojo.aist.models import AISTPipeline, AISTProject, AISTProjectVersion, AISTStatus, VersionType
from dojo.aist.tasks.enrich import enrich_finding_task
from dojo.models import DojoMeta, Engagement, Finding, Product, Product_Type, SLA_Configuration, Test, Test_Type


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
        # mock celery
        async_res = Mock()
        async_res.id = "celery-12345"
        run_task_mock.delay.return_value = async_res

        # we MUST have a project version now
        pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )

        url = reverse("dojo_aist_api:pipeline_start")
        payload = {"project_version_id": pv.id}
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, 201)

        pid = resp.json().get("id")
        self.assertTrue(pid)

        # pipeline exists
        p = AISTPipeline.objects.get(id=pid)
        self.assertEqual(p.project_id, self.project.id)
        self.assertEqual(p.project_version_id, pv.id)
        self.assertEqual(p.status, AISTStatus.FINISHED)

        # celery called
        run_task_mock.delay.assert_called_once()
        args, _kwargs = run_task_mock.delay.call_args
        self.assertEqual(args[0], pid)
        self.assertIsNone(args[1])

    def test_pipeline_start_404_on_unknown_version(self):
        """POST /pipeline/start with non-existing project_version_id must return 404."""
        url = reverse("dojo_aist_api:pipeline_start")
        payload = {"project_version_id": 999999}
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

    def _make_zip(self, filename: str, content: bytes) -> bytes:
        """Helper to create an in-memory zip archive with a single file."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(filename, content)
        buf.seek(0)
        return buf.read()

    def test_create_version_file_hash_ok(self):
        """FILE_HASH must accept source_archive and return created object."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        # in urls you already have trailing slash, so keep it as is

        archive_bytes = self._make_zip("a.txt", b"content")
        uploaded = SimpleUploadedFile(
            name="src.zip", content=archive_bytes, content_type="application/zip",
        )

        payload = {
            # if VersionType.FILE_HASH.value is enum -> .value, but in your test file
            # you were actually sending name, so do the same:
            "version_type": VersionType.FILE_HASH.value,
            "source_archive": uploaded,
        }

        resp = self.client.post(url, payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        data = resp.json()
        self.assertIn("id", data)
        self.assertEqual(data["project"], self.project.id)
        self.assertEqual(data["version_type"], VersionType.FILE_HASH.value)
        # in your model save() will put sha256 into version if it was FILE_HASH
        self.assertTrue(
            AISTProjectVersion.objects.filter(
                id=data["id"], project=self.project,
            ).exists(),
        )

    def test_create_version_file_hash_missing_archive(self):
        """FILE_HASH without source_archive must return 400 with field error."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        payload = {
            "version_type": VersionType.FILE_HASH.value,
        }
        resp = self.client.post(url, payload)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertIn("source_archive", data)

    def test_create_version_git_hash_ok(self):
        """GIT_HASH must accept 'version' and return it in the response."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        payload = {
            "version_type": VersionType.GIT_HASH,
            "version": "main",
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertIn("id", data)
        self.assertEqual(data["project"], self.project.id)
        self.assertEqual(data["version_type"], VersionType.GIT_HASH)
        self.assertEqual(data["version"], "main")

    def test_create_version_git_hash_missing_version(self):
        """GIT_HASH without version must return 400."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        payload = {
            "version_type": VersionType.GIT_HASH.value,
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        self.assertIn("version", data)

    def test_create_version_duplicate_git_hash(self):
        """Duplicate (project, version) must be rejected by serializer."""
        AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH.value,
            version="dup",
        )

        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        payload = {
            "version_type": VersionType.GIT_HASH.value,
            "version": "dup",
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()

        self.assertTrue(
            "version" in data or "non_field_errors" in data,
            data,
        )

    def test_create_version_404_for_unknown_project(self):
        """Unknown project_id should return 404."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": 999999},
        )
        payload = {
            "version_type": VersionType.GIT_HASH.value,
            "version": "some",
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_version_with_wrong_version_type(self):
        """Invalid version_type must return 400 from ChoiceField."""
        url = reverse(
            "dojo_aist_api:project_version_create",
            kwargs={"project_id": self.project.id},
        )
        payload = {
            "version_type": "WRONG_TYPE",
            "version": "anything",
        }
        resp = self.client.post(url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        data = resp.json()
        # DRF ChoiceField usually returns "“WRONG_TYPE” is not a valid choice."
        self.assertIn("version_type", data)

    def test_enrich_excluded_path_deletes_findings(self):
        """Findings with an excluded path should be deleted by enrich_finding_task."""
        self.project.profile = {"paths": {"exclude": ["skipme"]}}
        self.project.save()

        engagement = Engagement.objects.create(
            product=self.product,
            name="Test Engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        test_type = Test_Type.objects.create(name="AISTTestType")
        test = Test.objects.create(
            engagement=engagement,
            test_type=test_type,
            target_start=timezone.now(),
            target_end=timezone.now(),
        )
        version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH.value,
            version="abc123",
        )

        f_excluded = Finding.objects.create(
            test=test,
            title="Excluded",
            severity="Low",
            description="Should be deleted",
            file_path="src/skipme/file.cpp",
            date=timezone.now(),
            reporter=self.user,
        )
        f_kept = Finding.objects.create(
            test=test,
            title="Kept",
            severity="Low",
            description="Should remain",
            file_path="src/keepme/file.cpp",
            date=timezone.now(),
            reporter=self.user,
        )

        descriptor = {
            "id": version.id,
            "excluded_paths": ["skipme"],
            "type": "FILE_HASH",
        }

        # signature: (finding_id, repo_url, ref, trim_path, descriptor)
        enrich_finding_task.run(
            f_excluded.id,
            "",
            None,
            "",
            descriptor,
        )
        enrich_finding_task.run(
            f_kept.id,
            "",
            None,
            "",
            descriptor,
        )

        self.assertFalse(Finding.objects.filter(id=f_excluded.id).exists())
        self.assertTrue(Finding.objects.filter(id=f_kept.id).exists())

        self.assertTrue(
            DojoMeta.objects.filter(
                finding=f_kept,
                name="sourcefile_link",
            ).exists(),
        )
