from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from django.test import RequestFactory

# ---- Helpers ----------------------------------------------------------------

class DummyLogger:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass

def _mk_pipeline(**overrides):
    defaults = dict(
        id="pipeline-123",
        status="UNKNOWN",
        updated=None,
        logs="",
        project=SimpleNamespace(product=SimpleNamespace(name="Prod")),
        launch_data={},
        tests=MagicMock(),
        save=MagicMock(),
        refresh_from_db=MagicMock(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)

# ---- after_upload_enrich_and_watch ------------------------------------------

@patch("dojo.aist.tasks.enrich._install_db_logging", return_value=DummyLogger())
@patch("dojo.aist.tasks.enrich.AISTPipeline")
@patch("dojo.aist.tasks.enrich.watch_deduplication")
def _call_after_upload_enrich(mock_watch, mock_model, _mock_log, *,
                              enriched_count_list, pipeline, test_ids, log_level="INFO", params=None):
    from dojo.aist.tasks.enrich import after_upload_enrich_and_watch
    mock_model.objects.select_for_update().get.return_value = pipeline
    mock_res = SimpleNamespace(id="celery-123")
    mock_watch.delay.return_value = mock_res

    after_upload_enrich_and_watch(results=enriched_count_list,
                                  pipeline_id=pipeline.id,
                                  test_ids=test_ids,
                                  log_level=log_level,
                                  params=params or {})
    return mock_watch, pipeline, mock_res

class AfterUploadEnrichTests(TestCase):
    def test_sets_waiting_and_triggers_watcher(self):
        pipeline = _mk_pipeline(status="UPLOADING_RESULTS")
        test_ids = [10, 20, 30]

        mock_watch, pipeline, _ = _call_after_upload_enrich(
            enriched_count_list=[1, 0, 1],
            pipeline=pipeline,
            test_ids=test_ids,
            params={"x": 1},
        )

        self.assertEqual(pipeline.status, "WAITING_DEDUPLICATION_TO_FINISH")
        mock_watch.delay.assert_called_once_with(
            pipeline_id=pipeline.id, log_level="INFO", params={"x": 1}
        )
        pipeline.save.assert_any_call(update_fields=["watch_dedup_task_id", "updated"])

# ---- watch_deduplication ----------------------------------------------------

@patch("dojo.aist.tasks.dedup._install_db_logging", return_value=DummyLogger())
@patch("dojo.aist.tasks.dedup.AISTPipeline")
def _call_watch_dedup(mock_model, _mock_log, *, pipeline):
    from dojo.aist.tasks.dedup import watch_deduplication
    mock_model.objects.get.return_value = pipeline
    watch_deduplication.run(pipeline_id=pipeline.id, log_level="INFO", params={})
    return pipeline

class WatchDeduplicationTests(TestCase):
    def test_no_tests_finishes_immediately(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = False
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        _call_watch_dedup(pipeline=pipeline)

        self.assertEqual(pipeline.status, "FINISHED")
        pipeline.save.assert_any_call(update_fields=["status", "updated"])

    def test_complete_dedup_sets_waiting_confirmation(self):
        tests_mgr = MagicMock()
        tests_mgr.exists.return_value = True
        tests_mgr.filter().count.return_value = 0
        pipeline = _mk_pipeline(status="WAITING_DEDUPLICATION_TO_FINISH", tests=tests_mgr)

        _call_watch_dedup(pipeline=pipeline)

        self.assertEqual(pipeline.status, "WAITING_CONFIRMATION_TO_PUSH_TO_AI")
        pipeline.save.assert_any_call(update_fields=["status", "updated"])

# ---- push_request_to_ai -----------------------------------------------------

class PushRequestToAITests(TestCase):
    @patch("dojo.aist.tasks.ai.requests.post")
    @patch("dojo.aist.tasks.ai._install_db_logging", return_value=DummyLogger())
    @patch("dojo.aist.tasks.ai.AISTPipeline")
    def test_does_not_push_when_not_ready(self, mock_model, _mock_log, mock_post):
        from dojo.aist.tasks.ai import push_request_to_ai
        pipeline = _mk_pipeline(status="WAITING_CONFIRMATION_TO_PUSH_TO_AI")
        mock_model.objects.select_for_update().select_related().get.return_value = pipeline

        push_request_to_ai.run(pipeline_id=pipeline.id, finding_ids=[1, 2], filters={}, log_level="INFO")

        mock_post.assert_not_called()
        self.assertEqual(pipeline.status, "WAITING_CONFIRMATION_TO_PUSH_TO_AI")

    @patch("dojo.aist.tasks.ai.requests.post")
    @patch("dojo.aist.tasks.ai._install_db_logging", return_value=DummyLogger())
    @patch("dojo.aist.tasks.ai.AISTPipeline")
    def test_push_success_transitions_to_waiting_result(self, mock_model, _mock_log, mock_post):
        from dojo.aist.tasks.ai import push_request_to_ai
        pipeline = _mk_pipeline(status="PUSH_TO_AI")
        mock_model.objects.select_for_update().select_related().get.return_value = pipeline

        # Prepare successful HTTP response BEFORE the call
        ok_resp = SimpleNamespace(status_code=202, text="ok", raise_for_status=lambda: None)
        mock_post.return_value = ok_resp

        push_request_to_ai.run(pipeline_id=pipeline.id, finding_ids=[11, 22], filters={"analyzers": ["a"]}, log_level="INFO")

        self.assertEqual(pipeline.status, "WAITING_RESULT_FROM_AI")
        pipeline.save.assert_any_call(update_fields=["status", "updated"])

# ---- send_request_to_ai (view) ----------------------------------------------

class SendRequestToAITests(TestCase):
    @patch("dojo.aist.ai_views._install_db_logging", return_value=DummyLogger())
    @patch("dojo.aist.ai_views.push_request_to_ai")
    @patch("dojo.aist.ai_views.AISTPipeline")
    @patch("dojo.aist.ai_views.Finding")
    def test_send_request_pushes_when_confirmed(self, mock_finding, mock_pipeline_model, mock_push, _mock_log):
        from dojo.aist.ai_views import send_request_to_ai

        rf = RequestFactory()
        body = b'{"pipeline_id":"pipeline-123","finding_ids":[1,2,3],"filters":{"analyzers":["X"]}}'
        req = rf.post("/aist/send-request/pipeline-123/", data=body, content_type="application/json")
        # login_required expects user with is_authenticated=True
        req.user = SimpleNamespace(is_authenticated=True)

        pipeline = _mk_pipeline(status="WAITING_CONFIRMATION_TO_PUSH_TO_AI")
        mock_pipeline_model.objects.select_related().get.return_value = pipeline
        mock_pipeline_model.objects.select_for_update().get.return_value = pipeline

        mock_qs = MagicMock()
        mock_qs.select_related().values_list.return_value = [1, 2, 3]
        mock_finding.objects.filter.return_value = mock_qs

        resp = send_request_to_ai(req, pipeline_id="pipeline-123")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(pipeline.status, "PUSH_TO_AI")
        pipeline.save.assert_any_call(update_fields=["status", "updated"])
        mock_push.delay.assert_called_once_with("pipeline-123", [1, 2, 3], {"analyzers": ["X"]})

# ---- make_enrich_chord progress ---------------------------------------------

@patch("dojo.aist.tasks.enrich.get_redis")
def test_make_enrich_chord_initializes_progress(mock_get_redis: MagicMock):
    """Ensure progress hash is initialized in make_enrich_chord()."""
    from dojo.aist.tasks.enrich import make_enrich_chord
    mock_redis = MagicMock()
    mock_get_redis.return_value = mock_redis

    sig = make_enrich_chord(
        finding_ids=[1, 2, 3],
        repo_url="https://git",
        ref="main",
        trim_path="",
        pipeline_id="pipeline-xyz",
        test_ids=[101],
        log_level="INFO",
        params={},
    )
    assert sig is not None
    mock_redis.hset.assert_called_with("aist:progress:pipeline-xyz:enrich",
                                       mapping={"total": 3, "done": 0})
