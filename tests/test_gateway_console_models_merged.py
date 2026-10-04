"""Models page, round 5 (R5.2): one page, condensed cards.

Drives the REAL view code (``console_ui.CONSOLE_UI_JS`` + ``console_catalog.CATALOG_JS``)
in a node VM (the harness of ``test_gateway_console_catalog``) and pins:

- the downloaded models the catalog does not know (``GET /models/installed`` rows
  no downloaded catalog artifact accounts for) are plain rows under "Not in the
  catalog" in the SAME list, with All / Downloaded, never Not downloaded, a
  capability or Hugging Face mode;
- a model the catalog does know is never listed twice;
- the chip counts and the "N of M models · K artifacts" line include them;
- the same trash icon, confirmation and engine-native delete route;
- the condensed card: header on one line (title, facts, tags, badges inside the
  header), one row per artifact (engine · id · quant · size · status chips ·
  actions), the recommended marker as a small accent dot, 44 px targets.
"""

from __future__ import annotations

import re

from abstractgateway.console import gateway_console_html
from abstractgateway.console_catalog import CATALOG_CSS
from test_gateway_console_catalog import _catalog, _run

import pytest

pytestmark = pytest.mark.basic

EXTRA_OLLAMA = {"provider": "ollama", "artifact": "my-own-model:7b", "quant": "q4_k_m", "size_bytes": 4_000_000_000, "loaded": False,
                "location": "/Users/x/.ollama", "catalog_id": None, "deletable": True, "delete_blockers": []}
EXTRA_HF = {"provider": "huggingface", "artifact": "someone/uncatalogued", "quant": None, "size_bytes": None, "loaded": None,
            "location": None, "catalog_id": None, "deletable": True, "delete_blockers": []}


def _fixture() -> tuple[dict, dict, str]:
    cat = _catalog(12)
    covered = cat["rows"][0]["artifacts"][0]
    assert covered["presence"]["status"] == "installed" and covered["provider"] == "mlx"
    installed = {
        "schema": "models_installed_v1",
        "rows": [dict(EXTRA_OLLAMA), dict(EXTRA_HF),
                 {"provider": "mlx", "artifact": covered["artifact"], "quant": "4bit", "size_bytes": 1, "catalog_id": "model-0"}],
        "errors": {},
    }
    return cat, installed, f"mlx/{covered['artifact']}"


def _chip_n(html: str, group: str, value: str) -> int:
    m = re.search(rf'data-mc-filter="{group}" data-mc-value="{value}"[^>]*>[^<]*<span class="mc-chip__n">(\d+)</span>', html)
    assert m, (group, value)
    return int(m.group(1))


def test_not_in_the_catalog_rows_join_the_one_list_with_right_counts() -> None:
    cat, installed, covered_key = _fixture()
    n = len(cat["rows"])
    total = sum(len(r["artifacts"]) for r in cat["rows"])
    downloaded = sum(1 for r in cat["rows"] for a in r["artifacts"] if a["presence"]["status"] == "installed")
    out = _run(cat, "merged", installed=installed)

    html = out["html"]
    assert html.count('data-mc-extra-row="1"') == 2 and "Not in the catalog</h4>" in html
    assert 'data-mc-art="ollama/my-own-model:7b"' in html and 'data-mc-art="huggingface/someone/uncatalogued"' in html
    assert html.count(f'data-mc-art="{covered_key}"') == 1  # the catalog's own row, never a second one
    assert out["initial"]["count"] == [n, n, total + 2]

    assert out["downloaded"]["count"] == [downloaded and len({r["id"] for r in cat["rows"] if any(a["presence"]["status"] == "installed" for a in r["artifacts"])}), n, downloaded + 2]
    assert _chip_n(out["downloadedHtml"], "status", "downloaded") == downloaded + 2
    assert out["downloadedHtml"].count('data-mc-extra-row="1"') == 2
    assert "Not in the catalog" not in out["notDownloadedHtml"] and out["notDownloadedHtml"].count('data-mc-extra-row="1"') == 0
    assert _chip_n(out["notDownloadedHtml"], "status", "downloaded") == downloaded + 2  # counts stay right on every chip

    assert out["ollamaHtml"].count('data-mc-extra-row="1"') == 1 and 'data-mc-art="ollama/my-own-model:7b"' in out["ollamaHtml"]
    assert 'data-mc-filter="provider" data-mc-value="huggingface"' in out["html"]  # its provider gets a chip
    assert out["visionHtml"].count('data-mc-extra-row="1"') == 0
    # A search that only an extra row matches shows it, not the empty state.
    assert out["search"]["count"] == [0, n, 1] and 'data-mc-empty="1"' not in out["searchHtml"]
    assert out["searchHtml"].count('data-mc-extra-row="1"') == 1


def test_an_extra_row_has_the_same_trash_confirmation_and_delete_route() -> None:
    cat, installed, _ = _fixture()
    out = _run(cat, "merged", installed=installed)
    row = out["html"].split('data-mc-art="ollama/my-own-model:7b"')[1].split("</li>")[0]
    assert 'data-mc-action="delete-ask"' in row and 'aria-label="Delete" title="Delete"' in row and 'data-icon="trash"' in row
    assert ">Downloaded<" in row and "q4_k_m" in row and "Use as default" not in row
    confirm = out["confirmHtml"].split('data-mc-art="ollama/my-own-model:7b"')[1].split("</li>")[0]
    assert 'data-mc-del-confirm="ollama/my-own-model:7b"' in confirm and "from this computer. Files only" in confirm
    assert out["deletes"] == [
        {"provider": "ollama", "artifact": "my-own-model:7b", "dry_run": True},
        {"provider": "ollama", "artifact": "my-own-model:7b", "dry_run": False},
    ]
    after = out["afterDeleteHtml"]
    assert 'data-mc-art="ollama/my-own-model:7b"' not in after and "Deleted my-own-model:7b." in after and "freed." in after
    assert after.count('data-mc-extra-row="1"') == 1


def test_an_unlisted_installed_answer_is_said_not_hidden() -> None:
    cat, _, _ = _fixture()
    out = _run(cat, "merged", installed=None)
    assert 'data-mc-installed-error="1"' in out["downloadedHtml"] and "connection refused" in out["downloadedHtml"]


def test_the_condensed_card_and_row() -> None:
    cat, installed, _ = _fixture()
    html = _run(cat, "merged", installed=installed)["html"]
    card = html.split('data-mc-model="model-3"')[1].split("</article>")[0]
    head = card.split("<header")[1].split("</header>")[0]
    # One header line: title, facts, tags and badges all inside the header.
    assert 'class="mc-card__title"' in head and 'class="mc-card__meta"' in head and 'class="mc-tags"' in head and ">Starter<" in head
    assert "4B params" in head and "apache-2.0" in head
    assert "ui-mark" not in card
    rows = card.split('<li class="mc-art')[1:]
    assert len(rows) == 3
    order = ["mc-art__id", "mc-art__prov", "mc-art__quant", "mc-art__size", "mc-art__chips", "mc-art__action"]
    for r in rows:
        at = [r.index(f'class="{c}"') for c in order]
        assert at == sorted(at), r
        chips = r.split('class="mc-art__chips">')[1].split("</div>")[0]
        assert chips.count('class="ui-pill') == 2  # weights + fit
    primary = [r for r in rows if r.startswith(' is-primary"')]
    assert len(primary) == 1 and 'class="mc-rec" role="img" aria-label="Recommended for this computer"' in primary[0]
    assert "Recommended for this computer</span>" not in card  # a dot, not a text line
    # One grid line per row on a wide card, 44 px targets.
    assert 'grid-template-areas: "prov id quant size chips action" "job job job job job job"' in CATALOG_CSS
    assert re.search(r"\.mc-art \{[^}]*min-height: 44px", CATALOG_CSS)
    assert re.search(r"\.mc-art__action \.ui-btn \{[^}]*min-height: 44px", CATALOG_CSS)
    assert re.search(r"\.mc-art__action \.ui-btn\.mc-del \{[^}]*width: 44px", CATALOG_CSS)


def test_the_page_has_no_second_list() -> None:
    html = gateway_console_html()
    panel = html[html.index('id="tab-catalog"') : html.index('id="tab-apps"')]
    assert "On this computer" not in panel and "catalog-core-root" not in panel
