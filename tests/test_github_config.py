from __future__ import annotations

import pytest

from reporescue.github.config import GitHubConfig, parse_repo_from_remote_url


def test_parse_repo_from_remote_url():
    # HTTPS variants
    assert (
        parse_repo_from_remote_url("https://github.com/octocat/Hello-World.git")
        == "octocat/Hello-World"
    )
    assert (
        parse_repo_from_remote_url("https://github.com/octocat/Hello-World")
        == "octocat/Hello-World"
    )
    assert (
        parse_repo_from_remote_url(
            "https://x-access-token:ghp_12345@github.com/owner/repo.git"
        )
        == "owner/repo"
    )

    # SSH variants
    assert parse_repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"
    assert (
        parse_repo_from_remote_url("ssh://git@github.com/owner/repo.git")
        == "owner/repo"
    )

    # Invalid / non-GitHub URLs
    assert parse_repo_from_remote_url("") is None
    assert parse_repo_from_remote_url("invalid-url") is None
    assert parse_repo_from_remote_url(None) is None  # type: ignore[arg-type]


def test_github_config_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_1234567890")
    monkeypatch.setenv("GITHUB_REPOSITORY", "my-org/my-project")
    monkeypatch.setenv("GITHUB_BASE_BRANCH", "develop")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com/")
    monkeypatch.setenv("GITHUB_REPO_ALLOWLIST", "my-org/my-project, other-org/repo")

    cfg = GitHubConfig.from_env()
    assert cfg.token == "ghp_secret_token_1234567890"
    assert cfg.repository == "my-org/my-project"
    assert cfg.base_branch == "develop"
    assert cfg.api_url == "https://api.github.com"
    assert cfg.draft is True
    assert cfg.repository_allowlist == ["my-org/my-project", "other-org/repo"]
    assert cfg.is_configured is True
    assert cfg.validate() == []


def test_github_config_gh_token_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_fallback_token_12345")
    monkeypatch.setenv("REPORESCUE_GITHUB_REPO", "owner/fallback-repo")

    cfg = GitHubConfig.from_env()
    assert cfg.token == "ghp_fallback_token_12345"
    assert cfg.repository == "owner/fallback-repo"


def test_github_config_repository_precedence_github_repo_over_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_test_123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "primary-org/primary-repo")
    monkeypatch.setenv("REPORESCUE_GITHUB_REPO", "secondary-org/secondary-repo")

    cfg = GitHubConfig.from_env()
    assert cfg.repository == "primary-org/primary-repo"


def test_github_config_token_precedence_github_token_over_gh_token(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_primary_token_999")
    monkeypatch.setenv("GH_TOKEN", "ghp_secondary_token_111")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")

    cfg = GitHubConfig.from_env()
    assert cfg.token == "ghp_primary_token_999"


def test_github_config_repository_fallback_to_detected_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("REPORESCUE_GITHUB_REPO", raising=False)

    # When no explicit repository is set in env, from_env uses cwd git remote origin
    cfg = GitHubConfig.from_env()
    assert cfg.repository == "JishnuKarthikKonne/RepoRescue"


def test_github_config_validation_errors():
    # Missing token and repository
    cfg = GitHubConfig(token="", repository="")
    errors = cfg.validate()
    assert any("token is missing" in e for e in errors)
    assert any("repository is missing" in e for e in errors)

    # Invalid repo format
    cfg_bad_repo = GitHubConfig(token="token", repository="not_a_valid_format")
    errors2 = cfg_bad_repo.validate()
    assert any("Invalid GitHub repository format" in e for e in errors2)

    # Allowlist rejection
    cfg_allowlist = GitHubConfig(
        token="token",
        repository="unauthorized/repo",
        repository_allowlist=["authorized/repo"],
    )
    errors3 = cfg_allowlist.validate()
    assert any("not permitted by GITHUB_REPO_ALLOWLIST" in e for e in errors3)


def test_github_config_masked_token():
    cfg_none = GitHubConfig()
    assert cfg_none.masked_token == "<none>"

    cfg_short = GitHubConfig(token="12345678")
    assert cfg_short.masked_token == "***"

    cfg_long = GitHubConfig(token="ghp_1234567890abcdef")
    assert cfg_long.masked_token == "ghp_...cdef"
    assert "1234567890" not in cfg_long.masked_token
