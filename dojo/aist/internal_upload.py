from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from dojo.models import Development_Environment
from celery.result import ResultSet
from .utils import _import_sast_pipeline_package
from .logging_transport import _install_db_logging

from dojo.importers.default_importer import DefaultImporter
from dojo.models import (
    Engagement,
    Product,
    Product_Type,
    Test,
    Finding,
)

_import_sast_pipeline_package()
from pipeline.defect_dojo.repo_info import RepoParams, read_repo_params # type: ignore
from pipeline.config_utils import AnalyzersConfigHelper # type: ignore

@dataclass
class ImportConfig:
    """Configuration options loaded from the defectdojo.yaml file.

    Only fields relevant to the internal importer are defined here. The
    original configuration includes other options (URL, SSL verification)
    which are only applicable when using the REST API. These are
    intentionally omitted.
    """

    minimum_severity: str = "Info"
    name_mode: str = "analyzer-sha"
    engagement_status: str = "In Progress"

@dataclass
class ImportResult:
    """Result returned by :func:`upload_results_internal`.

    ``engagement_id`` and ``test_id`` allow callers to reference the
    imported objects. ``imported_findings`` records how many findings
    were created in DefectDojo and ``enriched_count`` counts the
    number of findings for which a source file link was successfully
    attached. ``raw`` contains the underlying test object for
    introspection by advanced callers.
    """

    engagement_id: int
    engagement_name: str
    test_id: Optional[int]
    imported_findings: int
    enriched_count: int
    raw: Test


def derive_engagement_name(analyzer_name: str, branch: Optional[str], commit: Optional[str], name_mode: str) -> str:
    """Derive an engagement name from analyser and repository information.

    The naming scheme mirrors the logic in :meth:`SastPipelineDDClient.derive_engagement_name`.

    :param analyzer_name: Name of the analyser/tool used to produce the report.
    :param branch: Current Git branch or tag if available.
    :param commit: Full commit hash if available.
    :param name_mode: Naming mode configured in defectdojo.yaml.
    :returns: A string suitable for use as an Engagement name.
    """
    short_sha = (commit or "")[:8] if commit else None
    if name_mode == "analyzer":
        return analyzer_name
    if name_mode == "analyzer-branch":
        return "-".join([x for x in [analyzer_name, branch] if x])
    # Default: analyzer-sha
    return "-".join([x for x in [analyzer_name, short_sha] if x]) or analyzer_name


def resolve_scan_type(analyzer: dict[str, Any]) -> str:
    ot = (analyzer.get("output_type") or "SARIF").strip()
    if ot.lower() in ("xml", "generic-xml"):
        return "Generic XML Import"
    return ot


def get_or_create_product(product_name: str) -> Product:
    """Retrieve or create a Product.

    A default product type will be created if necessary. The product is
    lazily initialised to avoid unnecessary queries. Administrators
    should ensure that the service account running Celery has permission
    to create products and engagements if creation is expected.
    """
    prod_type_name = getattr(settings, "DEFAULT_PRODUCT_TYPE_NAME", "Default")
    prod_type, _ = Product_Type.objects.get_or_create(name=prod_type_name)
    product, _ = Product.objects.get_or_create(
        name=product_name,
        defaults={
            "description": "Created automatically during report import",
            "prod_type": prod_type,
        },
    )
    return product


def ensure_engagement(product: Product, name: str, repo_params: RepoParams, status: str) -> Engagement:
    """Get or create an Engagement with repository metadata.

    If an engagement with the same name exists its repository metadata
    (source_code_management_uri, branch_tag, commit_hash) is updated
    when different. Otherwise a new engagement is created spanning
    today to tomorrow with the provided status.
    """
    engagement = Engagement.objects.filter(product=product, name=name).first()
    if engagement:
        updated = False
        if repo_params.repo_url and engagement.source_code_management_uri != repo_params.repo_url:
            engagement.source_code_management_uri = repo_params.repo_url
            updated = True
        if repo_params.branch_tag and engagement.branch_tag != repo_params.branch_tag:
            engagement.branch_tag = repo_params.branch_tag
            updated = True
        if repo_params.commit_hash and engagement.commit_hash != repo_params.commit_hash:
            engagement.commit_hash = repo_params.commit_hash
            updated = True
        if status and engagement.status != status:
            engagement.status = status
            updated = True
        if updated:
            engagement.save(update_fields=["source_code_management_uri", "branch_tag", "commit_hash", "status"])
        return engagement
    # Create new engagement
    today = date.today()
    tomorrow = today + timedelta(days=1)
    engagement = Engagement.objects.create(
        name=name,
        product=product,
        engagement_type="CI/CD",
        target_start=today,
        target_end=tomorrow,
        status=status,
        source_code_management_uri=repo_params.repo_url or None,
        branch_tag=repo_params.branch_tag,
        commit_hash=repo_params.commit_hash,
    )
    return engagement


def import_scan_via_default_importer(
    engagement: Engagement,
    scan_type: str,
    report_path: str,
    test_title: str,
    repo_params: RepoParams,
    minimum_severity: str,
) -> Tuple[Test, List[Finding]]:
    """Import a scan using :class:`DefaultImporter` and return the Test and findings.

    This helper encapsulates the boilerplate required to instantiate
    DefaultImporter with the correct options for the pipeline use case.
    Findings are collected from the created test to allow for
    enrichment and deletion.
    """
    # Determine a user to act as lead; fall back to the first user
    User = get_user_model()
    lead = User.objects.order_by("id").first()
    scan_date = timezone.now()
    environment = Development_Environment.objects.get_or_create(name="Development")[0]
    importer = DefaultImporter(
        engagement=engagement,
        test_title=test_title,
        scan_type=scan_type,
        scan_date=scan_date,
        minimum_severity=minimum_severity,
        active=True,
        verified=False,
        lead=lead,
        environment=environment,
        service=None,
        version=None,
        branch_tag=repo_params.branch_tag,
        build_id=repo_params.commit_hash or None,
        commit_hash=repo_params.commit_hash,
        api_scan_configuration=None,
        group_by=None,
        create_finding_groups_for_all_findings=False,
        deduplication_on_engagement=False,
        auto_create_context=True,
        user=lead,
    )
    with open(report_path, "rb") as f:
        test_obj, *_rest = importer.process_scan(f)
    # Collect findings associated with the test
    findings = list(Finding.objects.filter(test=test_obj))
    return test_obj, findings


def upload_report_internal(
        analyzer_name: str,
        product_name: str,
        scan_type: str,
        report_path: str,
        repo_params: RepoParams,
        trim_path: str,
        cfg: ImportConfig,
        pipeline_id: str,
        log_level: str
) -> ImportResult:
    """Replicate the behaviour of :meth:`SastPipelineDDClient.upload_report` without REST.

    This function orchestrates product/engagement creation, scan import
    and post‑processing (file path trimming, link enrichment and deletion
    of invalid findings). It is intended to be called from
    :func:`upload_results_internal`.
    """

    # Ensure product and engagement exist
    product = get_or_create_product(product_name)
    eng_name = derive_engagement_name(analyzer_name, repo_params.branch_tag, repo_params.commit_hash, cfg.name_mode)
    engagement = ensure_engagement(product, eng_name, repo_params, cfg.engagement_status)
    logger = _install_db_logging(pipeline_id, log_level)
    logger.info("Using engagement '%s' (id=%s)", eng_name, engagement.id)
    # Import
    test_obj, findings = import_scan_via_default_importer(
        engagement=engagement,
        scan_type=scan_type,
        report_path=report_path,
        test_title=f"{analyzer_name} {scan_type}",
        repo_params=repo_params,
        minimum_severity=cfg.minimum_severity,
    )
    imported_count = len(findings)
    logger.info("Import finished for %s, %d findings", analyzer_name, imported_count)

    # Collect finding IDs for later enrichment
    return ImportResult(
        engagement_id=engagement.id,
        engagement_name=eng_name,
        test_id=test_obj.id if test_obj else None,
        imported_findings=imported_count,
        enriched_count=0,          # will be updated in enrichment callback
        raw=test_obj,
    )


def upload_results_internal(
        analyzers_cfg_path: str,
        output_dir: str,
        product_name: str,
        repo_path: str,
        trim_path: str,
        pipeline_id: str,
        log_level: str
) -> List[ImportResult]:

    cfg = ImportConfig()
    repo_dir = repo_path
    logger = _install_db_logging(pipeline_id, log_level)
    try:
        repo_params = read_repo_params(repo_dir)
    except Exception as exc:
        logger.warning("Failed to read repository info from %s: %s", repo_dir, exc)
        # TODO: fill me with some info for local sorces
        repo_params = RepoParams(commit_hash=None, branch_tag=None, repo_url=None, scm_type=None, local_path=repo_dir)
    # Build list of analysers
    analyzers_cfg = AnalyzersConfigHelper(analyzers_cfg_path)
    if not analyzers_cfg:
        return []
    analyzers = analyzers_cfg.get_analyzers()

    results: List[ImportResult] = []
    for analyzer in analyzers:
        name = analyzer.get("name") or "unknown"
        report_name = analyzers_cfg.get_analyzer_result_file_name(analyzer)
        report_path = os.path.join(output_dir, report_name)
        if not os.path.exists(report_path):
            logger.error("No result on expected path %s for analyzer %s", report_path, name)
            continue
        scan_type = resolve_scan_type(analyzer)
        logger.info("Processing report: %s (analyzer=%s, scan_type=%s)", report_path, name, scan_type)
        try:
            res = upload_report_internal(
                analyzer_name=name,
                product_name=product_name,
                scan_type=scan_type,
                report_path=report_path,
                repo_params=repo_params,
                trim_path=trim_path,
                cfg=cfg,
                pipeline_id=pipeline_id,
                log_level=log_level
            )
            results.append(res)
        except Exception as exc:
            logger.error("Error during uploading report %s: %s", report_path, exc)
    return results

