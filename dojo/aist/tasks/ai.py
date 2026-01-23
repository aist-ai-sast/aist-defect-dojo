import json
from collections.abc import Iterable
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction

from dojo.aist.ai_filter import apply_ai_filter
from dojo.aist.logging_transport import install_pipeline_logging
from dojo.aist.models import AISTPipeline, AISTStatus
from dojo.aist.utils.pipeline import finish_pipeline, set_pipeline_status
from dojo.aist.utils.urls import build_callback_url
from dojo.models import Finding


def _csv(items: Iterable[Any]) -> str:
    seen = set()
    result: list[str] = []
    for it in items or []:
        s = str(it).strip()
        if not s:
            continue
        if s not in seen:
            seen.add(s)
            result.append(s)
    return ", ".join(result)


@shared_task(bind=True)
def push_request_to_ai(self, pipeline_id: str, finding_ids, filters, log_level="INFO") -> None:
    log = install_pipeline_logging(pipeline_id, log_level)
    webhook_url = getattr(
        settings,
        "AIST_AI_TRIAGE_WEBHOOK_URL",
        "https://flaming.app.n8n.cloud/webhook-test/triage-sast",
    )

    webhook_timeout = getattr(settings, "AIST_AI_TRIAGE_REQUEST_TIMEOUT", 10)
    triage_secret = getattr(settings, "AIST_AI_TRIAGE_SECRET", None)

    with transaction.atomic():
        try:
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .select_related("project__product")
                .get(id=pipeline_id)
            )

            if pipeline.status != AISTStatus.PUSH_TO_AI:
                log.error("Attempt to push to AI before pipeline ready to Push it")
                finish_pipeline(pipeline)
                return

            project = pipeline.project
            product = getattr(project, "product", None)
            project_name = getattr(product, "name", None) or getattr(project, "project_name", "")

            launch_data = pipeline.launch_data or {}
            languages = _csv(launch_data.get("languages") or [])
            tools = _csv(filters["analyzers"] or [])
            callback_url = build_callback_url(pipeline_id)

            payload: dict[str, Any] = {
                "project": {
                    "name": project_name,
                    "description": getattr(project, "description", "") or "",
                    "languages": languages,
                    "cwe": getattr(filters, "cwe", "") or "",
                    "tools": tools,
                    "findings": finding_ids,
                },
                "pipeline_id": str(pipeline.id),
                "callback_url": callback_url,
            }
            headers = {"Content-Type": "application/json"}
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if triage_secret:
                headers["X-AIST-Signature"] = triage_secret
            log.info("Sending AI triage request: url=%s payload=%s", webhook_url, payload)
            resp = requests.post(webhook_url, data=body_bytes, headers=headers, timeout=webhook_timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.exception("AI triage POST failed with exception %s", exc)
            finish_pipeline(pipeline)
            return

        set_pipeline_status(pipeline, AISTStatus.WAITING_RESULT_FROM_AI)

        log.info("AI triage request accepted: status=%s body=%s", resp.status_code, resp.text[:500])


@shared_task(name="dojo.aist.auto_push_to_ai_if_configured")
def auto_push_to_ai_if_configured(pipeline_id: str):
    logger = install_pipeline_logging(pipeline_id)

    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .prefetch_related("tests")
            .get(id=pipeline_id)
        )

        if pipeline.status != AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI:
            logger.error("Attempt to collect findings for AI before pipeline ready to Push it")
            finish_pipeline(pipeline)
            return

        ai = (pipeline.launch_data or {}).get("ai") or {}
        snap = ai.get("filter_snapshot")

        if not snap or not ai:
            logger.warning("AUTO_DEFAULT: Filter snapshot not configured")
            finish_pipeline(pipeline)
            return

        qs = Finding.objects.filter(test__in=pipeline.tests.all(), active=True)
        qs = apply_ai_filter(qs, snap)

        limit = int((snap or {}).get("limit") or None)

        if limit is None:
            logger.warning("AUTO_DEFAULT: Filter limit is None")
            finish_pipeline(pipeline)
            return

        finding_ids = list(qs.values_list("id", flat=True)[:limit])

        if not finding_ids:
            logger.warning("AUTO_DEFAULT: filter matched 0 findings")
            finish_pipeline(pipeline)
            return

    set_pipeline_status(pipeline, AISTStatus.PUSH_TO_AI)
    push_request_to_ai.delay(pipeline_id, finding_ids, {"filter": snap})
