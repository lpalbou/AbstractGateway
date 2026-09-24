"""The published docs site carries user pages only.

`docs/` also holds the backlog (planning notes). Every folder under `docs/`
must be excluded from the MkDocs build, so a new planning folder cannot reach
the public site unnoticed; user pages live at the top of `docs/`.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _excluded() -> set:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    raw = config.get("exclude_docs") or ""
    return {line.strip().strip("/") for line in raw.splitlines() if line.strip()}


def test_every_docs_folder_is_excluded_from_the_site() -> None:
    folders = {p.name for p in (ROOT / "docs").iterdir() if p.is_dir() and not p.name.startswith(".")}
    assert "backlog" in folders
    assert folders - _excluded() == set()


def test_no_site_page_links_into_an_excluded_folder() -> None:
    excluded = _excluded()
    for page in (ROOT / "docs").glob("*.md"):
        text = page.read_text(encoding="utf-8")
        for folder in excluded:
            assert f"]({folder}/" not in text and f"](./{folder}/" not in text, (page.name, folder)
