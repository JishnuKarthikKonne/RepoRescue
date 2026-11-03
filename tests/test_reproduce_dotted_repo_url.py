from __future__ import annotations

from reporescue.github.config import parse_repo_from_remote_url


def test_parse_repo_from_remote_url_with_dots_in_repo_name():
    """Reproduces bug where repositories containing dots (e.g. core.utils) fail to parse."""
    https_url = "https://github.com/my-org/core.utils.git"
    assert parse_repo_from_remote_url(https_url) == "my-org/core.utils"

    ssh_url = "git@github.com:my-org/core.utils.git"
    assert parse_repo_from_remote_url(ssh_url) == "my-org/core.utils"
