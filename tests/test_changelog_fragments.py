"""Changelog fragments (scripts/changelog.py): parse, assemble at release, and the PR check."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("changelog_script", ROOT / "scripts" / "changelog.py")
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)


def test_every_fragment_in_the_repository_is_well_formed() -> None:
    for f in cl.fragment_files():
        cl.parse(f.read_text(encoding="utf-8"), name=f.name)


def test_parse_refuses_what_a_release_could_not_place() -> None:
    with pytest.raises(ValueError, match="unknown section"):
        cl.parse("### Improved\n\n- x\n")
    with pytest.raises(ValueError, match="before the first"):
        cl.parse("- x\n### Fixed\n- y\n")
    with pytest.raises(ValueError, match="no entries"):
        cl.parse("### Fixed\n\n")


def test_assemble_groups_sections_keeps_older_releases_and_removes_fragments(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    d.mkdir()
    (d / "README.md").write_text("# not a fragment\n")
    (d / "b.md").write_text("### Fixed\n\n- **b fixed**\n")
    (d / "a.md").write_text("### Fixed\n\n- **a fixed**\n\n### Added\n\n- **a added**\n")
    ch = tmp_path / "CHANGELOG.md"
    ch.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\n### Added\n\n- old\n")
    used = cl.assemble("0.2.0", "2026-10-29", changelog=ch, directory=d)
    text = ch.read_text()
    assert [p.name for p in used] == ["a.md", "b.md"] and not (d / "a.md").exists() and (d / "README.md").exists()
    assert text.index("## [Unreleased]") < text.index("## [0.2.0] - 2026-10-29") < text.index("## [0.1.0]")
    new = text[text.index("## [0.2.0]"):text.index("## [0.1.0]")]
    assert new.index("### Added") < new.index("### Fixed")  # Keep a Changelog order
    assert new.count("### Fixed") == 1 and "- **a fixed**\n- **b fixed**" in new
    assert "- old" in text


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(repo), "PATH": "/usr/bin:/bin"})


def test_check_needs_a_fragment_when_src_changes(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    src = cl.SOURCE_PREFIXES[0].rstrip("/")
    (repo / src).mkdir(parents=True)
    (repo / "changelog.d").mkdir()
    (repo / src / "a.py").write_text("x = 1\n")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    monkeypatch.setattr(cl, "ROOT", repo)
    (repo / src / "a.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "change src")
    assert cl.check("main") == 1
    assert "adds no changelog fragment" in capsys.readouterr().out
    (repo / "changelog.d" / "change.md").write_text("### Changed\n\n- **x is 2**\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fragment")
    assert cl.check("main") == 0
    (repo / "changelog.d" / "bad.md").write_text("### Improved\n\n- x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bad fragment")
    with pytest.raises(ValueError):
        cl.check("main")


def test_check_accepts_the_release_pull_request_that_assembles_the_fragments(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    src = cl.SOURCE_PREFIXES[0].rstrip("/")
    (repo / src).mkdir(parents=True)
    (repo / "changelog.d").mkdir()
    (repo / src / "version.py").write_text('v = "0.1.0"\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    (repo / "changelog.d" / "feature.md").write_text("### Added\n\n- **a feature**\n")
    (repo / "changelog.d" / "other.md").write_text("### Fixed\n\n- **another change**\n")
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n\n### Added\n\n- old\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "release")
    monkeypatch.setattr(cl, "ROOT", repo)
    # A feature PR that deletes someone's fragment and adds a heading is not a release.
    (repo / src / "version.py").write_text('v = "0.1.1"\n')
    (repo / "changelog.d" / "other.md").unlink()
    (repo / "CHANGELOG.md").write_text((repo / "CHANGELOG.md").read_text().replace("## [Unreleased]\n", "## [Unreleased]\n\n## [9.9.9] - 2026-11-02\n\n- hand edit\n"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "not a release")
    assert cl.check("main") == 1
    assert "CHANGELOG.md was edited" in capsys.readouterr().out
    # The version's heading with a fragment left behind is still not the release assembly.
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')
    _git(repo, "commit", "-qam", "version 9.9.9, feature.md still there")
    assert cl.check("main") == 1
    capsys.readouterr()
    # The release step: assemble writes the dated section of the pyproject version and deletes every fragment.
    _git(repo, "checkout", "-q", "main", "--", "CHANGELOG.md", "changelog.d")
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.2.0"\n')
    cl.assemble("0.2.0", "2026-11-02", changelog=repo / "CHANGELOG.md", directory=repo / "changelog.d")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "release 0.2.0")
    assert cl.check("main") == 0
    assert "release assembly of 0.2.0" in capsys.readouterr().out


def test_check_accepts_a_patch_release_from_a_base_with_no_fragment(tmp_path: Path, monkeypatch, capsys) -> None:
    """Everything was folded by the previous release; the next release has nothing to delete. It is a
    release when it raises the version and adds that version's dated section; not otherwise."""
    repo = tmp_path / "repo"
    src = cl.SOURCE_PREFIXES[0].rstrip("/")
    (repo / src).mkdir(parents=True)
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / "README.md").write_text("fragments\n")
    (repo / src / "app.py").write_text('version = "0.1.0"\n')
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.2.0"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n\n### Added\n\n- old\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "release")
    monkeypatch.setattr(cl, "ROOT", repo)
    # A hand edit with no version bump is not a release.
    (repo / src / "app.py").write_text('version = "0.2.0"\n')
    (repo / "CHANGELOG.md").write_text((repo / "CHANGELOG.md").read_text().replace("## [Unreleased]\n", "## [Unreleased]\n\n## [0.2.1] - 2026-01-02\n\n### Fixed\n\n- x\n"))
    _git(repo, "commit", "-qam", "hand edit")
    assert cl.check("main") == 1
    capsys.readouterr()
    # The version raised to the section's version: the release.
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.2.1"\n')
    _git(repo, "commit", "-qam", "release 0.2.1")
    assert cl.check("main") == 0
    assert "release assembly of 0.2.1" in capsys.readouterr().out
    # A fragment left in changelog.d/ is never a release.
    (repo / "changelog.d" / "left.md").write_text("### Fixed\n\n- y\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fragment left")
    assert cl.check("main") != 0 or "release assembly" not in capsys.readouterr().out
