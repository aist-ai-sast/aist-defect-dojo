import time
from urllib.parse import urljoin, urlparse

import requests
from django.urls import reverse

from .utils import get_public_base_url


class LinkBuilder:

    """Build source links for GitHub/GitLab/Bitbucket; verify remote file existence (handles 429)."""

    def __init__(self, version_descriptor):
        self.is_local_files = version_descriptor.get("type", "FILE_HASH") == "FILE_HASH"
        self.version_id = version_descriptor.get("id", "")
        self.excluded_path = version_descriptor.get("excluded_paths", [])

    @staticmethod
    def _scm_type(repo_url: str) -> str:
        host = urlparse(repo_url).netloc.lower()
        if "github" in host:
            return "github"
        if "gitlab" in host:
            return "gitlab"
        if "bitbucket.org" in host:
            return "bitbucket-cloud"
        if "bitbucket" in host:
            return "bitbucket-server"
        if "gitea" in host:
            return "gitea"
        if "codeberg" in host:
            return "codeberg"
        if "dev.azure.com" in host or "visualstudio.com" in host:
            return "azure"
        return "generic"

    @staticmethod
    def build_project_version_file_blob(project_version_id: int, subpath: str) -> str:
        base = get_public_base_url()
        path = reverse("dojo_aist_api:project_version_file_blob",
                       kwargs={"project_version_id": project_version_id, "subpath": subpath})
        return urljoin(base + "/", path.lstrip("/"))

    @staticmethod
    def build_raw_url(repo_url: str, ref: str, subpath: str) -> str:
        """
        Build a raw file URL for the given SCM (GitHub/GitLab/etc).
        This function converts a repo URL + ref + path into a direct raw download link.
        """
        lb = LinkBuilder({"id": 0})
        scm = lb._scm_type(repo_url)
        fp = subpath.lstrip("/").replace("\\", "/")

        if scm == "github":
            return f"{repo_url.rstrip('/')}/raw/{ref}/{fp}"
        if scm == "gitlab":
            return f"{repo_url.rstrip('/')}/-/raw/{ref}/{fp}"
        if scm == "bitbucket-cloud":
            return f"{repo_url.rstrip('/')}/raw/{ref}/{fp}"
        if scm == "bitbucket-server":
            return f"{repo_url.rstrip('/')}/raw/{ref}/{fp}"
        if scm in {"gitea", "codeberg"}:
            return f"{repo_url.rstrip('/')}/raw/{ref}/{fp}"
        if scm == "azure":
            return f"{repo_url.rstrip('/')}/?path=/{fp}&version=GC{ref}&api-version=7.0&download=true"
        return f"{repo_url.rstrip('/')}/raw/{ref}/{fp}"

    def get_public_redirect_blob_url(self, repo_url: str, ref, file_path: str):
        scm = self._scm_type(repo_url)
        ref = ref or "master"
        file_path = file_path.replace("file://", "")
        fp = file_path.lstrip("/")
        if scm == "github":
            return f"{repo_url.rstrip('/')}/blob/{ref}/{fp}"
        if scm == "gitlab":
            return f"{repo_url.rstrip('/')}/-/blob/{ref}/{fp}"
        if scm == "bitbucket-cloud":
            return f"{repo_url.rstrip('/')}/src/{ref}/{fp}"
        if scm == "bitbucket-server":
            return f"{repo_url.rstrip('/')}/browse/{fp}?at={ref}"
        if scm in {"gitea", "codeberg"}:
            return f"{repo_url.rstrip('/')}/src/{ref}/{fp}"
        if scm == "azure":
            return f"{repo_url.rstrip('/')}/?path=/{fp}&version=GC{ref}"
        return f"{repo_url.rstrip('/')}/blob/{ref}/{fp}"

    # for all build proxy aist url
    # when aistproject created it always creates initial version master
    # the proxy if it see it's git_hash redirect to blob and pat is not None get the content by itself with token and returns it

    def build(self, file_path: str) -> str | None:
        if not file_path:
            return None

        file_path = file_path.replace("file://", "")
        fp = file_path.lstrip("/")
        return LinkBuilder.build_project_version_file_blob(self.version_id, fp)

    def contains_excluded_path(self, url: str) -> bool:
        return any(path in url for path in self.excluded_path)

    def remote_link_exists(self, url: str, timeout: int = 5, max_retries: int = 3) -> bool | None:
        """Return True if GET 200/3xx, False if 404, None for other errors. Retries on 429."""
        if self.is_local_files:
            return True

        try:
            r = requests.get(url, allow_redirects=True, timeout=timeout)
        except requests.RequestException:
            return None
        else:
            if r.status_code == 429 and max_retries > 0:
                retry = int(r.headers.get("Retry-After", "1"))
                time.sleep(retry)
                return self.remote_link_exists(url, timeout, max_retries - 1)
            if r.status_code == 200:
                return True
            if 300 <= r.status_code < 400:
                return True
            if r.status_code == 404:
                return False
            return None
