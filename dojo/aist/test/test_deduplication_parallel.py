
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.utils import timezone

from dojo.aist.models import ProcessedFinding, TestDeduplicationProgress
from dojo.models import Engagement, Finding, Product, Product_Type, SLA_Configuration, Test, Test_Type


class ConcurrentDeduplicationTest(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create(
            username="tester",
            email="tester@example.com",
            password="x",  # noqa: S106
        )
        # Create basic DefectDojo objects
        self.sla = SLA_Configuration.objects.create(
            name="SLA default for tests",
        )
        self.prod_type = Product_Type.objects.create(name="PT for tests")
        product = Product.objects.create(
            name="Test Product", description="desc", prod_type=self.prod_type, sla_configuration_id=self.sla.id,
        )
        engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=product,
        )
        test_type = Test_Type.objects.create(name="SAST")
        self.test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        # Deduplication progress should exist
        self.progress = TestDeduplicationProgress.objects.get(test=self.test)

    def test_deleted_processed_do_not_fake_complete(self):
        findings = [
            Finding.objects.create(test=self.test, title=f"A{i}", severity="High", date=timezone.now(), reporter=self.user)
            for i in range(100)
        ]
        # set 60 as not processed
        for f in findings[:60]:
            ProcessedFinding.objects.get_or_create(test_id=self.test.id, finding_id=f.id)

        # delete them
        for f in findings[:60]:
            f.delete()

        # left 40 finding without Processed finding
        self.progress.refresh_pending_tasks()
        self.progress.refresh_from_db()
        self.assertEqual(self.progress.pending_tasks, 40)
        self.assertFalse(self.progress.deduplication_complete)
