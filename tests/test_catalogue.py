import json

import httpx
import pytest

from k8s_explorer.catalogue import (
    CatalogueError,
    GitHubCatalogue,
    RateLimitError,
    load_repositories,
    normalize_repository_url,
)


def test_yaml_and_inherited_json_stable_dedup(tmp_path):
    yaml = tmp_path / "repositories.yaml"
    yaml.write_text("""repositories:
  - name: Example/Repo
    url: https://github.com/Example/Repo.git
    branch: master
    stars: 42
    preference: 2
  - url: https://github.com/example/repo/
  - url: https://gitlab.com/group/nested/repo
""")
    inherited = tmp_path / "repos.json"
    inherited.write_text(json.dumps([["example/repo", "https://github.com/example/repo", "main", 5]]))
    repos = load_repositories(yaml)
    assert len(repos) == 2
    assert repos[0].id == load_repositories(inherited)[0].id
    assert repos[0].stars == 42 and repos[0].preference == 2
    assert repos[0].branch == "master"
    assert repos[1].name == "group/nested/repo"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo",
        "https://token@github.com/example/repo",
        "https://github.com/example/repo#secret",
        "https://github.com/example/repo?token=secret",
        "https://github.com:8080/example/repo",
        "https://github.com/example/../repo",
        "https://github.com/example/%2e%2e",
        "https://github.com/example/repo/tree/main",
        "ssh://github.com/example/repo",
        "https://example.invalid/example/repo",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        normalize_repository_url(url)


def test_empty_catalogue_and_invalid_shape(tmp_path):
    path = tmp_path / "repositories.yaml"
    path.write_text("repositories: []\n")
    assert load_repositories(path) == []
    path.write_text("repositories: wrong\n")
    with pytest.raises(ValueError):
        load_repositories(path)
    with pytest.raises(ValueError):
        normalize_repository_url("https://github.com/example/repo", [])


def test_local_catalogue_requires_explicit_opt_in(tmp_path):
    directory = tmp_path / "repository"
    directory.mkdir()
    path = tmp_path / "repos.json"
    path.write_text(json.dumps([{"url": str(directory), "name": "local"}]))
    with pytest.raises(ValueError):
        load_repositories(path)
    repo = load_repositories(path, allow_local=True)[0]
    assert repo.url == str(directory.resolve()) and repo.name == "local"
    path.write_text(json.dumps([{"url": "https://gitlab.example/group/nested/repo"}]))
    with pytest.raises(ValueError):
        load_repositories(path)
    assert load_repositories(path, allowed_hosts=["gitlab.example"])[0].name == "group/nested/repo"


def test_discovery_pagination_budget_and_refresh(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer private"
        if request.url.path == "/search/repositories":
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "total_count": 101,
                    "incomplete_results": False,
                    "items": [
                        {
                            "html_url": f"https://github.com/example/repo-{i}",
                            "default_branch": "main",
                            "stargazers_count": i,
                        }
                        for i in (range(100) if page == 1 else range(100, 101))
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "html_url": "https://github.com/example/repo-0",
                "default_branch": "master",
                "stargazers_count": 99,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalogue = GitHubCatalogue(token="private", client=client)
        partial = catalogue.discover(max_pages=1)
        assert partial["incomplete"] and len(partial["repositories"]) == 100
        complete = catalogue.discover(max_pages=2)
        assert not complete["incomplete"] and len(complete["repositories"]) == 101
        repo = complete["repositories"][0].model_copy(update={"preference": 5})
        updated = catalogue.refresh(repo)
        assert updated.stars == 99 and updated.branch == "master" and updated.preference == 5
    assert len(calls) == 4


def test_rate_limits_and_provider_errors_do_not_expose_credentials():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "42"}, text="private"),
        )
    ) as client:
        with pytest.raises(RateLimitError) as error:
            GitHubCatalogue(token="private", client=client).discover()
        assert error.value.retry_after == 42 and "private" not in str(error.value)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "https://evil.invalid/private"}),
        )
    ) as client:
        with pytest.raises(CatalogueError, match="HTTP 302"):
            GitHubCatalogue(token="private", client=client).discover()
    with pytest.raises(CatalogueError):
        GitHubCatalogue().discover("topic injection", max_pages=1)
