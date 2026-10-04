"""Repository catalogue loading with stable, provider-qualified identities."""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from .models import Repository, stable_id


def normalize_repository_url(url: str, allowed_hosts: list[str] | None = None) -> str:
    """Accept public HTTPS repository URLs, never credentials or transport options."""
    allowed = {
        host.lower() for host in (["github.com", "gitlab.com"] if allowed_hosts is None else allowed_hosts)
    }
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in allowed
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Repository URL requires an allowed HTTPS host without credentials or parameters")
    path = parsed.path.rstrip("/").removesuffix(".git").strip("/")
    segments = path.split("/")
    if len(segments) < 2 or (parsed.hostname == "github.com" and len(segments) != 2):
        raise ValueError("Repository URL must identify a namespace and repository")
    if any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in segments):
        raise ValueError("Repository URL contains an invalid repository path")
    return f"https://{parsed.hostname}/{path.lower()}"


def load_repositories(
    path: Path,
    allowed_hosts: list[str] | None = None,
    allow_local: bool = False,
) -> list[Repository]:
    """Load YAML records or the inherited JSON tuple catalogue; deduplicate by URL."""
    raw = json.loads(path.read_text()) if path.suffix == ".json" else yaml.safe_load(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("repositories")
    if not isinstance(raw, list):
        raise ValueError("Repository catalogue must contain a list")
    repositories: dict[str, Repository] = {}
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) in (3, 4, 5):
            entry = dict(zip(("name", "url", "branch", "stars", "preference"), entry, strict=False))
        if not isinstance(entry, dict) or "url" not in entry:
            raise ValueError("Each repository record must specify a URL")
        configured_url = entry["url"]
        if (
            allow_local
            and isinstance(configured_url, str)
            and Path(configured_url).is_absolute()
            and Path(configured_url).is_dir()
        ):
            url = str(Path(configured_url).resolve())
            repo_id = stable_id("local", url)
            default_name = Path(url).name
        else:
            url = normalize_repository_url(configured_url, allowed_hosts)
            parsed = urlsplit(url)
            repo_id = stable_id(parsed.hostname, parsed.path.strip("/"))
            default_name = parsed.path.strip("/")
        repo = Repository(
            id=repo_id,
            name=entry.get("name") or default_name,
            url=url,
            branch=entry.get("branch", "main"),
            stars=entry.get("stars", 0),
            preference=entry.get("preference", 0),
        )
        # Stable first-record precedence preserves intentional local preferences.
        repositories.setdefault(repo_id, repo)
    return list(repositories.values())


class CatalogueError(ValueError):
    """A safe provider error without credentials or response contents."""


class RateLimitError(CatalogueError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"GitHub API rate limited; retry after {retry_after} seconds")


class GitHubCatalogue:
    """Bounded public repository discovery and metadata refresh.

    Rate limits are surfaced to the scheduler rather than sleeping inside a worker.
    https://docs.github.com/en/rest/search/search#search-repositories
    """

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self._token = token
        self._client = client

    def _request(self, path: str, params: dict | None = None) -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        client = self._client or httpx.Client(timeout=30, follow_redirects=False)
        try:
            with client.stream(
                "GET",
                f"https://api.github.com{path}",
                params=params,
                headers=headers,
                follow_redirects=False,
                timeout=30,
            ) as response:
                if response.status_code in (403, 429):
                    try:
                        retry = int(response.headers.get("retry-after", ""))
                    except ValueError:
                        try:
                            reset = int(response.headers.get("x-ratelimit-reset", "0"))
                            retry = max(1, reset - int(time.time()))
                        except ValueError:
                            retry = 60
                    raise RateLimitError(max(1, retry))
                if response.status_code != 200:
                    raise CatalogueError(f"GitHub API request failed with HTTP {response.status_code}")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise CatalogueError("GitHub API response exceeds the byte budget")
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise CatalogueError("GitHub API returned invalid JSON") from None
                if not isinstance(result, dict):
                    raise CatalogueError("GitHub API returned an invalid response shape")
                return result
        except httpx.HTTPError:
            raise CatalogueError("GitHub API transport failed") from None
        finally:
            if self._client is None:
                client.close()

    @staticmethod
    def _repository(item: dict, preference: float = 0) -> Repository:
        try:
            url = normalize_repository_url(item["html_url"], ["github.com"])
            parsed = urlsplit(url)
            return Repository(
                id=stable_id(parsed.hostname, parsed.path.strip("/")),
                name=parsed.path.strip("/"),
                url=url,
                branch=item["default_branch"],
                stars=item.get("stargazers_count", 0),
                preference=preference,
            )
        except (KeyError, TypeError, ValueError):
            raise CatalogueError("GitHub API returned invalid repository metadata") from None

    def discover(self, topic: str = "k8s-at-home", max_pages: int = 5) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,50}", topic) or not 1 <= max_pages <= 10:
            raise CatalogueError("Invalid topic or page budget")
        repositories: dict[str, Repository] = {}
        incomplete = False
        total_count = 0
        for page in range(1, max_pages + 1):
            response = self._request(
                "/search/repositories",
                {
                    "q": f"topic:{topic} fork:false archived:false",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            items = response.get("items")
            if not isinstance(items, list):
                raise CatalogueError("GitHub API returned an invalid search response")
            total_count = response.get("total_count", len(items))
            incomplete |= bool(response.get("incomplete_results"))
            for item in items:
                repo = self._repository(item)
                repositories.setdefault(repo.id, repo)
            if len(items) < 100 or len(repositories) >= total_count:
                break
        return {
            "repositories": list(repositories.values()),
            "total_count": total_count,
            "incomplete": incomplete or len(repositories) < total_count,
        }

    def refresh(self, repo: Repository) -> Repository:
        url = normalize_repository_url(repo.url, ["github.com"])
        metadata = self._request(f"/repos{urlsplit(url).path}")
        updated = self._repository(metadata, preference=repo.preference)
        if updated.id != repo.id:
            raise CatalogueError("Repository moved; update the catalogue explicitly")
        return updated.model_copy(update={"name": repo.name})
