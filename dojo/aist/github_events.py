# your_app/events.py
import logging

from asgiref.sync import sync_to_async
from django_github_app.routing import GitHubRouter

from dojo.aist.models import AISTProject, AISTProjectVersion, PullRequest, RepositoryInfo, ScmGithubBinding, ScmType
from dojo.aist.tasks import run_sast_pipeline
from dojo.aist.utils import create_pipeline_object, has_unfinished_pipeline
from dojo.models import Product, Product_Type

from .utils import _load_analyzers_config

gh = GitHubRouter()
logger = logging.getLogger("dojo.aist")


@gh.event("installation", action="created")
@gh.event("installation_repositories", action="added")
async def on_install_created_or_repos_added(event, gh, **_):
    event_type = getattr(event, "event", None)
    action = (event.data or {}).get("action")
    inst_id = ((event.data or {}).get("installation") or {}).get("id")
    logger.info(f"Received installation event: type={event_type}, action={action}, installation_id={inst_id}")

    # 2) Process each added repository
    if event_type == "installation":
        repos_added = [r["full_name"] for r in (event.data or {}).get("repositories", [])]
    else:  # installation_repositories
        repos_added = [r["full_name"] for r in (event.data or {}).get("repositories_added", [])]

    if not repos_added:
        logger.error("No repositories found in installation_repositories.added payload.")
        return

    cfg = _load_analyzers_config()
    product_type, _ = await sync_to_async(Product_Type.objects.get_or_create)(
        name="Github Imported",
    )
    if not cfg:
        logger.error("Could not load analyzers config.")
        return
    for repo_full in repos_added:
        logger.info(f"Processing repository: {repo_full}")
        owner, name = repo_full.split("/", 1)

        try:
            # 3) Fetch repository details from GitHub API (async)
            repo = await gh.getitem(f"/repos/{owner}/{name}")
            languages = await gh.getitem(f"/repos/{owner}/{name}/languages")
            langs = cfg.convert_languages(languages)
            desc = repo.get("description")
            html_url = (repo.get("html_url") or "https://github.com").rstrip("/")
            base_url = html_url.split("/" + owner + "/")[0]  # supports GH Enterprise host
            logger.info(f"Fetched metadata for {repo_full}: languages={langs}, description={desc!r}")
        except Exception:
            logger.exception("Failed to fetch data for %s", repo_full)
            continue

        # 4) Create or update local Product and AISTProject
        product, created_product = await sync_to_async(Product.objects.get_or_create)(
            name=repo_full,
            defaults={"prod_type": product_type, "description": desc or "Empty description. Admin, fix me"},
        )
        logger.info(("Created Product: " if created_product else "Product exists: ") + repo_full)

        repo_info, _created_repo = await sync_to_async(RepositoryInfo.objects.get_or_create)(
            type=ScmType.GITHUB,
            repo_owner=owner,
            repo_name=name,
            defaults={
                "base_url": base_url,
                "repo_url": f"{base_url}/{owner}/{name}.git" if hasattr(RepositoryInfo, "repo_url") else "",
            } if "repo_url" in [f.name for f in RepositoryInfo._meta.fields] else {
                "base_url": base_url,
            },
        )

        _aist_project, created_project = await sync_to_async(AISTProject.objects.get_or_create)(
            product=product,
            defaults={
                "supported_languages": langs,
                "script_path": "INVALID",
                "compilable": False,
                "profile": {},
                "repository": repo_info,
            },
        )
        logger.info(("Created AISTProject for " if created_project else "AISTProject exists for ") + repo_full)

        def _ensure_binding(scm=repo_info, installation_id=inst_id):
            binding, created = ScmGithubBinding.objects.get_or_create(
                scm=scm,
                defaults={"installation_id": installation_id},
            )
            updated = False
            if binding.installation_id != installation_id:
                binding.installation_id = installation_id
                updated = True
            if updated:
                binding.save(update_fields=["installation_id"])
            return binding, created, updated

        _binding, created_bind, updated_bind = await sync_to_async(_ensure_binding)()
        if created_bind:
            logger.info(f"Created ScmGithubBinding for {repo_full} (installation_id={inst_id})")
        elif updated_bind:
            logger.info(f"Updated ScmGithubBinding for {repo_full} (installation_id={inst_id})")
        else:
            logger.info(f"Verified ScmGithubBinding for {repo_full} (installation_id={inst_id})")

    logger.info(f"Finished processing {len(repos_added)} repositories for installation {inst_id}.")


@gh.event("pull_request", action="opened")
@gh.event("pull_request", action="synchronize")
async def on_pr_event(event, gh, **_):
    """Handle PR open/update events asynchronously."""
    action = (event.data or {}).get("action")
    logger.info(f"Received pull_request event with action: {action}")

    repo_payload = (event.data or {}).get("repository") or {}
    repo_full = repo_payload.get("full_name")
    if not repo_full:
        logger.error("Missing repository.full_name in pull_request payload.")
        return
    owner, name = repo_full.split("/", 1)

    pr = (event.data or {}).get("pull_request") or {}
    pr_number = pr.get("number")
    head = pr.get("head") or {}
    base = pr.get("base") or {}

    head_sha = (head.get("sha") or "").strip()
    head_ref = (head.get("ref") or "").strip()
    base_ref = (base.get("ref") or "").strip()
    is_from_fork = (head.get("repo") or {}).get("full_name") != repo_full

    logger.info(
        f"PR metadata: repo={repo_full}, pr_number={pr_number}, "
        f"head_sha={head_sha[:7]}, head_ref={head_ref}, base_ref={base_ref}, from_fork={is_from_fork}",
    )

    # ORM (async-safe)
    try:
        product = await sync_to_async(Product.objects.get)(name=repo_full)
        aist_project = await sync_to_async(AISTProject.objects.get)(product=product)
    except Product.DoesNotExist:
        logger.error(f"Product not found for repository: {repo_full}")
        return
    except AISTProject.DoesNotExist:
        logger.error(f"AISTProject not found for repository: {repo_full}")
        return

    pv, created = await sync_to_async(AISTProjectVersion.objects.get_or_create)(
        project=aist_project,
        version=head_sha,
    )
    if created:
        logger.info(f"Created new AISTProjectVersion: {head_sha}")

    if await sync_to_async(has_unfinished_pipeline)(pv):
        logger.warning(f"Pull request {pr_number} already has an unfinished pipeline. Skipping.")
        return

    html_url = (repo_payload.get("html_url") or "https://github.com").rstrip("/")
    base_url = html_url.split("/" + owner + "/")[0]

    try:
        repo_info = await sync_to_async(RepositoryInfo.objects.get)(
            type=ScmType.GITHUB,
            repo_owner=owner,
            repo_name=name,
            defaults={"base_url": base_url},
        )
        aist_project = await sync_to_async(AISTProject.objects.get)(product=product)
    except RepositoryInfo.DoesNotExist:
        logger.error(f"RepositoryInfo not found for repository: {repo_full}")
        return

    pr_ref, created_pr = await sync_to_async(PullRequest.objects.update_or_create)(
        project_version=pv,
        repository=repo_info,
        pr_number=pr_number,
        defaults={
            "base_ref": base_ref,
            "head_ref": head_ref,
            "is_from_fork": is_from_fork,
        },
    )
    if created_pr:
        logger.info(f"Created PullRequest record: #{pr_number}")
    else:
        logger.info(f"Updated PullRequest record: #{pr_number}")

    params = {"pr_launch": True}

    # Create pipeline and enqueue Celery task
    pipeline = await sync_to_async(create_pipeline_object)(aist_project, pv, pr_ref)
    async_result = run_sast_pipeline.delay(pipeline.id, params)
    pipeline.run_task_id = async_result.id
    await sync_to_async(pipeline.save)(update_fields=["run_task_id"])

    logger.info(
        f"Pipeline enqueued for PR #{pr_number}: pipeline_id={pipeline.id}, task_id={async_result.id}",
    )
