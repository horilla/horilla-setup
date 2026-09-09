"""The tool installs and upgrades from the stable branch, not the dev branch.

`hrms-v2` used to build from `dev/v2.0`. That is where work lands first: it
carries migrations and schema changes no release has shipped, so every customer
who ran `horillasetup build hrms-v2` got a checkout ahead of every published
version. The install source is now `2.0`, the branch releases are cut from.

Changing the clone alone would not have been enough. `upgrade` runs a bare
`git pull`, which follows whatever branch the checkout already tracks, so
installs built before the change would have stayed on `dev/v2.0` indefinitely
and the fix would have reached only new installs.
"""

import subprocess

import pytest

from horillasetup.ctl import HORILLA_REPOS, upgrade_project


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_hrms_v2_installs_from_the_stable_branch():
    assert HORILLA_REPOS["hrms-v2"]["branch"] == "2.0"


def test_no_product_installs_from_a_dev_branch():
    for key, cfg in HORILLA_REPOS.items():
        assert not cfg["branch"].startswith("dev/"), f"{key} installs from a dev branch"


@pytest.fixture
def checkout_on_old_branch(tmp_path):
    """A clone tracking dev/v2.0, as pre-change installs are."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "dev/v2.0", cwd=origin)
    git("config", "user.email", "t@t.test", cwd=origin)
    git("config", "user.name", "t", cwd=origin)
    (origin / "f.txt").write_text("dev\n")
    git("add", "-A", cwd=origin)
    git("commit", "-qm", "dev commit", cwd=origin)
    git("branch", "2.0", cwd=origin)

    work = tmp_path / "work"
    git("clone", "-q", "-b", "dev/v2.0", str(origin), str(work), cwd=tmp_path)
    git("config", "user.email", "t@t.test", cwd=work)
    git("config", "user.name", "t", cwd=work)
    return work


def test_upgrade_moves_a_clean_checkout_to_stable(checkout_on_old_branch, monkeypatch):
    monkeypatch.chdir(checkout_on_old_branch)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout_on_old_branch) == "dev/v2.0"

    upgrade_project("hrms-v2")

    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout_on_old_branch) == "2.0"


def test_upgrade_refuses_rather_than_discard_local_changes(
    checkout_on_old_branch, monkeypatch
):
    """
    Switching branches under someone's uncommitted work is not a thing a setup
    tool should do quietly. Refuse, say why, and leave the tree untouched.
    """
    monkeypatch.chdir(checkout_on_old_branch)
    (checkout_on_old_branch / "f.txt").write_text("local edit\n")

    with pytest.raises(SystemExit):
        upgrade_project("hrms-v2")

    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout_on_old_branch) == "dev/v2.0"
    assert (checkout_on_old_branch / "f.txt").read_text() == "local edit\n"
