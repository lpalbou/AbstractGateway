"""kit_release.released_ui_kit reads the TAGGED kit, not a checkout ahead of it."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kit_release import released_ui_kit

pytestmark = pytest.mark.basic


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(repo), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_checkout_ahead_of_the_release_is_read_at_its_tag(tmp_path: Path) -> None:
    repo = tmp_path / "abstractuic"
    (repo / "ui-kit" / "src").mkdir(parents=True)
    (repo / "ui-kit" / "package.json").write_text(json.dumps({"version": "0.1.14"}))
    (repo / "ui-kit" / "src" / "theme.css").write_text("released\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "v0.1.14")
    (repo / "ui-kit" / "src" / "theme.css").write_text("moved ahead\n")
    _git(repo, "commit", "-qam", "ahead")

    released = released_ui_kit(repo / "ui-kit" / "src", "0.1.14", tmp_path / "out")
    assert released is not None
    assert (released / "src" / "theme.css").read_text() == "released\n"
    assert released_ui_kit(repo / "ui-kit", "9.9.9", tmp_path / "none") is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_panel_chat_at_the_tag_is_read_beside_ui_kit(tmp_path: Path) -> None:
    """The islands carry panel-chat (mountSandboxChat): a tag that has it is
    extracted with it, at the tag, next to ui-kit."""
    repo = tmp_path / "abstractuic"
    (repo / "ui-kit" / "src").mkdir(parents=True)
    (repo / "panel-chat" / "src").mkdir(parents=True)
    (repo / "ui-kit" / "package.json").write_text(json.dumps({"version": "0.4.0"}))
    (repo / "ui-kit" / "src" / "theme.css").write_text("released\n")
    (repo / "panel-chat" / "src" / "panel_chat.css").write_text(".pc-released {}\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "v0.4.0")
    (repo / "panel-chat" / "src" / "panel_chat.css").write_text(".pc-ahead {}\n")
    _git(repo, "commit", "-qam", "ahead")

    released = released_ui_kit(repo / "ui-kit" / "src", "0.4.0", tmp_path / "out")
    assert released is not None
    assert (released.parent / "panel-chat" / "src" / "panel_chat.css").read_text() == ".pc-released {}\n"
