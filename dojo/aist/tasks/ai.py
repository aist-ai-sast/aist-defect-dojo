import json
import requests
from celery import shared_task
from typing import Any, Dict, Iterable
from dojo.aist.logging_transport import _install_db_logging
from django.conf import settings
from django.db import transaction
from dojo.aist.models import AISTPipeline, AISTStatus
from dojo.aist.utils import build_callback_url

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
    log = _install_db_logging(pipeline_id, log_level)
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
                return

            project = pipeline.project
            product = getattr(project, "product", None)
            project_name = getattr(product, "name", None) or getattr(project, "project_name", "")

            launch_data = pipeline.launch_data or {}
            languages = _csv(launch_data.get("languages") or [])
            tools = _csv(filters["analyzers"] or [])
            callback_url = build_callback_url(pipeline_id)

            payload: Dict[str, Any] = {
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
            log.error("AI triage POST failed: %s", exc, exc_info=True)
            pipeline.status = AISTStatus.FINISHED
            pipeline.save(update_fields=["status", "updated"])
            return

        pipeline.status = AISTStatus.WAITING_RESULT_FROM_AI
        pipeline.save(update_fields=["status", "updated"])

        log.info("AI triage request accepted: status=%s body=%s", resp.status_code, resp.text[:500])


