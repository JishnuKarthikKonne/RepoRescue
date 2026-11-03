from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from reporescue.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubValidationError,
    MockGitHubClient,
)
from reporescue.github.config import GitHubConfig
from reporescue.github.models import GitHubPRRequest


def test_github_client_create_draft_pr_success():
    cfg = GitHubConfig(
        token="ghp_test_secret_token_123",
        repository="owner/repo",
        base_branch="main",
    )
    client = GitHubClient(cfg)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {
            "id": 101,
            "number": 42,
            "url": "https://api.github.com/repos/owner/repo/pulls/42",
            "html_url": "https://github.com/owner/repo/pull/42",
            "title": "[RepoRescue] Fix checkout bug",
            "draft": True,
            "head": {"sha": "abc1234567890"},
            "created_at": "2026-11-03T12:00:00Z",
        }
    ).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        req = GitHubPRRequest(
            task_id="task-123",
            title="[RepoRescue] Fix checkout bug",
            body="### Summary\nFix applied.",
            head_branch="reporescue/task-123",
            base_branch="main",
            draft=True,
        )
        res = client.create_draft_pr(req)

        assert res.pr_number == 42
        assert res.html_url == "https://github.com/owner/repo/pull/42"
        assert res.draft is True
        assert res.is_existing is False

        # Inspect request
        http_req = mock_urlopen.call_args[0][0]
        assert (
            http_req.get_full_url() == "https://api.github.com/repos/owner/repo/pulls"
        )
        assert http_req.headers["Authorization"] == "Bearer ghp_test_secret_token_123"
        assert http_req.headers["Accept"] == "application/vnd.github+json"

        sent_body = json.loads(http_req.data.decode("utf-8"))
        assert sent_body["draft"] is True
        assert sent_body["head"] == "reporescue/task-123"


def test_github_client_auth_failure_masks_token():
    secret_token = "ghp_ultra_secret_credentials_999"
    cfg = GitHubConfig(token=secret_token, repository="owner/repo")
    client = GitHubClient(cfg)

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/pulls",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(read=lambda: f"Bad credentials for token {secret_token}".encode()),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        req = GitHubPRRequest(
            task_id="task-123",
            title="Title",
            body="Body",
            head_branch="head",
        )
        with pytest.raises(GitHubAuthError) as exc_info:
            client.create_draft_pr(req)

        err_text = str(exc_info.value)
        assert secret_token not in err_text
        assert "***" in err_text


def test_github_client_not_found():
    cfg = GitHubConfig(token="token", repository="owner/missing-repo")
    client = GitHubClient(cfg)

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/missing-repo/pulls",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"message": "Not Found"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        req = GitHubPRRequest(task_id="t1", title="T", body="B", head_branch="h")
        with pytest.raises(GitHubNotFoundError):
            client.create_draft_pr(req)


def test_github_client_rate_limit():
    cfg = GitHubConfig(token="token", repository="owner/repo")
    client = GitHubClient(cfg)

    mock_hdrs = MagicMock()
    mock_hdrs.get.return_value = "0"  # x-ratelimit-remaining == 0

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/pulls",
        code=403,
        msg="Forbidden",
        hdrs=mock_hdrs,
        fp=MagicMock(read=lambda: b'{"message": "API rate limit exceeded"}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        req = GitHubPRRequest(task_id="t1", title="T", body="B", head_branch="h")
        with pytest.raises(GitHubRateLimitError):
            client.create_draft_pr(req)


def test_mock_github_client_lifecycle():
    client = MockGitHubClient()

    # 1. Check repo
    repo = client.check_auth_and_repo()
    assert repo["permissions"]["push"] is True

    # 2. Create PR
    req = GitHubPRRequest(
        task_id="task-mock",
        title="[RepoRescue] Mock fix",
        body="Mock body",
        head_branch="reporescue/task-mock",
        draft=True,
    )
    res = client.create_draft_pr(req)
    assert res.pr_number == 1
    assert "pull/1" in res.html_url
    assert res.draft is True

    # 3. Existing PR idempotency
    existing = client.find_existing_pr("reporescue/task-mock")
    assert existing is not None
    assert existing.pr_number == 1

    # Attempt duplicate creation returns existing PR
    res2 = client.create_draft_pr(req)
    assert res2.pr_number == 1

    # 4. Invariant check: draft=False must be rejected
    bad_req = GitHubPRRequest(
        task_id="task-mock2",
        title="Title",
        body="Body",
        head_branch="branch2",
        draft=False,
    )
    with pytest.raises(GitHubValidationError):
        client.create_draft_pr(bad_req)


def test_preflight_git_receive_pack_http_success(tmp_path):
    cfg = GitHubConfig(
        token="ghp_test_token",
        repository="owner/repo",
        token_username="testuser",
        source_repository_path=tmp_path,
    )
    client = GitHubClient(cfg)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.getcode.return_value = 200
    mock_resp.headers = {"Content-Type": "application/x-git-receive-pack-advertisement"}
    mock_resp.read.return_value = b"001e# service=git-receive-pack\n0000"
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch.object(
            client, "check_auth_and_repo", return_value={"permissions": {"push": True}}
        ),
        patch.object(client, "_make_request", return_value={"login": "testuser"}),
        patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
    ):
        res = client.preflight_git_receive_pack()
        assert res.api_authenticated is True
        assert res.api_push_permission is True
        assert res.git_smart_http_authorized is True
        assert res.status_code == 200
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert "git-receive-pack" in req.get_full_url()


def test_preflight_git_receive_pack_http_403_denied():
    cfg = GitHubConfig(
        token="ghp_readonly_token",
        repository="owner/repo",
        token_username="testuser",
    )
    client = GitHubClient(cfg)

    http_error = urllib.error.HTTPError(
        url="https://github.com/owner/repo.git/info/refs?service=git-receive-pack",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=MagicMock(read=lambda: b"Permission denied to push"),
    )

    with (
        patch.object(
            client, "check_auth_and_repo", return_value={"permissions": {"push": True}}
        ),
        patch.object(client, "_make_request", return_value={"login": "testuser"}),
        patch("urllib.request.urlopen", side_effect=http_error),
    ):
        res = client.preflight_git_receive_pack()
        assert res.api_authenticated is True
        assert res.api_push_permission is True  # REST API reports True
        assert res.git_smart_http_authorized is False  # But Git push fails with 403!
        assert res.status_code == 403
        assert "403" in res.error_message


def test_preflight_git_receive_pack_dry_run_fails_with_permission_denied(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    cfg = GitHubConfig(
        token="ghp_test_token",
        repository="owner/repo",
        token_username="testuser",
        source_repository_path=tmp_path,
    )
    client = GitHubClient(cfg)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.getcode.return_value = 200
    mock_resp.headers = {"Content-Type": "application/x-git-receive-pack-advertisement"}
    mock_resp.read.return_value = b"001e# service=git-receive-pack\n0000"
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch.object(
            client, "check_auth_and_repo", return_value={"permissions": {"push": True}}
        ),
        patch.object(client, "_make_request", return_value={"login": "testuser"}),
        patch("urllib.request.urlopen", return_value=mock_resp),
        patch("reporescue.runner.run_command") as mock_run_cmd,
    ):
        mock_run_cmd.return_value = MagicMock(
            passed=False,
            stderr="fatal: remote: Permission to owner/repo.git denied to testuser.",
            stdout="",
        )
        res = client.preflight_git_receive_pack()
        assert res.api_authenticated is True
        assert res.git_smart_http_authorized is False
        assert res.status_code == 403
        assert "Permission to owner/repo.git denied to testuser" in res.error_message


def test_github_rest_403_blocks_preflight():
    """5. GitHub REST 403 blocks preflight and reports non-authorized."""
    cfg = GitHubConfig(token="ghp_bad_role_token", repository="owner/repo")
    client = GitHubClient(cfg)

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=MagicMock(
            read=lambda: b'{"message": "Must have push access to repository"}'
        ),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        res = client.preflight_git_receive_pack()
        assert res.api_authenticated is False
        assert res.api_push_permission is False
        assert res.git_smart_http_authorized is False


def test_github_invalid_token_401_raises_auth_error():
    """4. Invalid token (HTTP 401) raises GitHubAuthError and masks token."""
    secret_token = "ghp_invalid_expired_token_000"
    cfg = GitHubConfig(token=secret_token, repository="owner/repo")
    client = GitHubClient(cfg)

    http_error = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(
            read=lambda: f'{{"message": "Bad credentials for {secret_token}"}}'.encode()
        ),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(GitHubAuthError) as exc_info:
            client.check_auth_and_repo()
        assert secret_token not in str(exc_info.value)
        assert "***" in str(exc_info.value)


def test_github_config_credential_source_gcm_override(monkeypatch):
    """8. GITHUB_CREDENTIAL_SOURCE=gcm forces GCM even if GITHUB_TOKEN is present."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_ambient_shadowing_token")
    monkeypatch.setenv("GITHUB_CREDENTIAL_SOURCE", "gcm")

    with patch(
        "reporescue.github.config.resolve_gcm_token",
        return_value=("JishnuKarthikKonne", "gho_valid_gcm_token"),
    ):
        cfg = GitHubConfig.from_env()
        assert cfg.token == "gho_valid_gcm_token"
        assert cfg.token_source == "gcm"
        assert cfg.token_username == "JishnuKarthikKonne"


def test_github_config_no_credentials_available(monkeypatch):
    """9. When no credential exists in env or GCM, is_configured is False."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GITHUB_CREDENTIAL_SOURCE", "env")

    cfg = GitHubConfig.from_env()
    assert cfg.token is None
    assert cfg.token_source is None
    assert cfg.is_configured is False
    errors = cfg.validate()
    assert any("token is missing" in e.lower() for e in errors)
