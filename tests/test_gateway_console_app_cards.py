"""Apps cards: one registration template (backlog 1006, docs/apps.md "Registering an app").

A registered app (apps_manager.AppSpec, apps_desktop.DesktopAppSpec) declares
its card: `mark`, `description`, `illustration` (a file under
console_assets/apps/) and an optional `family`. The overview groups the rows
into `cards[]`: one per family (the browser member is the identity), else one
per app, each with launchers derived from its members' kinds (browser,
desktop, terminal). The console renders those cards with no per-app copy of
its own; a browser launcher carries the globe badge, a desktop launcher the
monitor badge, a terminal launcher the `>_` glyph. The illustrations are
served read-only at /console/assets/apps/<file>, for registered files only.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway import apps_desktop as desk
from abstractgateway import apps_manager as am
from abstractgateway.console import gateway_console_html
from test_gateway_console_offline import _node, _slice_function
from test_r10w5_app_updates import CALLER, _install_web, home, world  # noqa: F401 - fixtures

pytestmark = pytest.mark.basic


# ---------------------------------------------------------------------------
# The registry and the overview's cards
# ---------------------------------------------------------------------------


def _webp_size(data: bytes) -> tuple:
    """(width, height) of a WebP file: VP8 (lossy), VP8L (lossless) or VP8X (extended)."""
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", "not a WebP file"
    chunk = data[12:16]
    if chunk == b"VP8 ":
        assert data[23:26] == b"\x9d\x01\x2a", "bad VP8 frame tag"
        return (int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF)
    if chunk == b"VP8L":
        assert data[20] == 0x2F, "bad VP8L signature"
        b = int.from_bytes(data[21:25], "little")
        return ((b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":
        return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
    raise AssertionError(f"unknown WebP chunk {chunk!r}")


def _card(ov: Dict[str, Any], card_id: str) -> Dict[str, Any]:
    return next(c for c in ov["cards"] if c["id"] == card_id)


def test_every_registered_app_declares_its_card() -> None:
    specs = [*am.APPS, *desk.DESKTOP_APPS]
    for spec in specs:
        assert spec.mark and len(spec.mark) == 2, spec.id
        assert spec.description.strip().endswith("."), f"{spec.id}: the card's description is one whole sentence"
        assert spec.family is None or spec.family in am.APP_FAMILIES, spec.id
    # Every illustration is a plain file name of a served type (registered_illustrations raises otherwise).
    assert set(am.registered_illustrations()) == {s.illustration for s in specs if s.illustration}
    # Every registered illustration ships in the package (768x432 WebP), and a family shares one picture.
    for name, path in am.registered_illustrations().items():
        assert _webp_size(path.read_bytes()) == (768, 432), name
    assert am.APP_BY_ID["assistant-web"].illustration == desk.ASSISTANT.illustration == "assistant.webp"
    # The console holds no per-app copy any more.
    html = gateway_console_html()
    assert "APP_COPY" not in html and "Chat or talk with the Assistant in your browser." not in html


def test_the_assistant_family_is_one_card_with_two_launchers(world) -> None:
    world.net.pypi("0.13.0")
    ov = world.m.overview(caller=CALLER, check_latest=False)
    ids = [c["id"] for c in ov["cards"]]
    assert ids.count("assistant") == 1 and "assistant-web" not in ids
    card = _card(ov, "assistant")
    assert card["family"] == "assistant" and card["name"] == "Assistant"
    # Only the desktop Assistant is installed here: it leads (badge, primary action).
    web = next(a for a in ov["apps"] if a["id"] == "assistant-web")
    assert web["installed"] is False and next(a for a in ov["apps"] if a["id"] == "assistant")["installed"] is True
    assert card["members"] == ["assistant", "assistant-web"]
    assert card["launchers"] == [{"app": "assistant", "kind": "desktop"}, {"app": "assistant-web", "kind": "browser"}]
    assert card["description"] == web["description"] == am.APP_BY_ID["assistant-web"].description
    assert card["mark"] == "As"
    # Code: one card, its browser launcher then its terminal one (when its terminal version exists).
    code = _card(ov, "code")
    assert code["members"] == ["code"] and code["launchers"][0] == {"app": "code", "kind": "browser"}
    assert all(l["kind"] in ("browser", "terminal") for l in code["launchers"])
    # Every row is in exactly one card.
    members = [m for c in ov["cards"] for m in c["members"]]
    assert sorted(members) == sorted(a["id"] for a in ov["apps"])
    # The desktop launcher's words come from the gateway.
    a = next(a for a in ov["apps"] if a["id"] == "assistant")
    assert a["desktop"]["open_label"] in ("Open on this Mac", "Open on this computer")


def test_the_first_installed_member_leads_browser_preferred(world) -> None:
    """The headline describes what this computer has: the browser member when
    it is installed (or when nothing is), else the first installed member."""
    world.net.pypi("0.13.0")
    _install_web(world.m, "assistant-web", "0.1.0")
    card = _card(world.m.overview(caller=CALLER, check_latest=False), "assistant")
    assert card["members"] == ["assistant-web", "assistant"]
    assert card["launchers"] == [{"app": "assistant-web", "kind": "browser"}, {"app": "assistant", "kind": "desktop"}]
    rows = [{"id": "w", "kind": "web", "name": "W", "family": "assistant", "installed": False, "interfaces": [], "description": "w."},
            {"id": "d", "kind": "desktop", "name": "D", "family": "assistant", "installed": False, "interfaces": [], "description": "d."}]
    none = am.app_cards(rows)[0]
    assert none["members"] == ["w", "d"] and none["description"] == "w."  # nothing installed: the browser member
    rows[1]["installed"] = True
    desk_led = am.app_cards(rows)[0]
    assert desk_led["members"] == ["d", "w"] and desk_led["description"] == "w."  # the registry identity keeps the words


def test_without_a_family_the_assistant_is_two_cards_again(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing `family` from the registry gives one card per app: the
    grouping is the registry's, not a special case for the Assistant."""
    apps = tuple(dataclasses.replace(s, family=None) if s.id == "assistant-web" else s for s in am.APPS)
    desktop = tuple(dataclasses.replace(s, family=None) for s in desk.DESKTOP_APPS)
    monkeypatch.setattr(am, "APPS", apps)
    monkeypatch.setattr(desk, "DESKTOP_APPS", desktop)
    monkeypatch.setattr(desk, "DESKTOP_BY_ID", {s.id: s for s in desktop})
    world.net.pypi("0.13.0")
    ov = world.m.overview(caller=CALLER, check_latest=False)
    web, mac = _card(ov, "assistant-web"), _card(ov, "assistant")
    assert web["members"] == ["assistant-web"] and web["name"] == "Assistant (web)" and web["family"] is None
    assert web["launchers"] == [{"app": "assistant-web", "kind": "browser"}]
    assert mac["members"] == ["assistant"] and mac["launchers"] == [{"app": "assistant", "kind": "desktop"}]


def test_a_family_without_a_title_fails_loudly() -> None:
    rows = [{"id": "x", "kind": "web", "name": "X", "family": "nameless", "interfaces": []}]
    with pytest.raises(KeyError):
        am.app_cards(rows)


def test_illustration_url_only_for_a_registered_file_that_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(am, "CONSOLE_APP_ASSETS_DIR", tmp_path)
    assert am.illustration_url("code.webp") is None  # registered, not there yet: the card shows its mark
    (tmp_path / "code.webp").write_bytes(b"RIFF....WEBPVP8 ")
    url = am.illustration_url("code.webp")
    assert url and re.fullmatch(r"/console/assets/apps/code\.webp\?v=[0-9a-f]{12}", url)
    # The tag is the content's: a new timestamp alone keeps it, new bytes change it.
    import os

    os.utime(tmp_path / "code.webp", ns=(1, 1))
    assert am.illustration_url("code.webp") == url
    (tmp_path / "code.webp").write_bytes(b"RIFF....WEBPVP8 other")
    os.utime(tmp_path / "code.webp", ns=(1, 1))
    assert am.illustration_url("code.webp") != url
    url = am.illustration_url("code.webp")
    (tmp_path / "stray.webp").write_bytes(b"x")
    assert am.illustration_url("stray.webp") is None  # there, but not registered
    assert am.card_fields(am.APP_BY_ID["code"])["illustration_url"] == url
    assert am.card_fields(am.APP_BY_ID["flow"])["illustration_url"] is None


# ---------------------------------------------------------------------------
# The asset route
# ---------------------------------------------------------------------------


@pytest.fixture()
def assets_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t" * 32)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(am, "CONSOLE_APP_ASSETS_DIR", assets)
    from abstractgateway.app import app

    return TestClient(app), assets


def test_the_asset_route_serves_registered_files_only(assets_client) -> None:
    client, assets = assets_client
    (assets / "flow.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 flow")
    (assets / "stray.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 stray")
    (assets / "flow.svg").write_text("<svg onload='x()'/>", encoding="utf-8")
    # No credential: public like the console page.
    r = client.get("/console/assets/apps/flow.webp")
    assert r.status_code == 200 and r.content.endswith(b"flow")
    assert r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    for name in ("stray.webp", "flow.svg", "code.webp", "..%2Fflow.webp", "%2E%2E%2Fapps_manager.py", "FLOW.webp"):
        assert client.get(f"/console/assets/apps/{name}").status_code == 404, name
    assert client.get("/console/assets/apps/../../app.py").status_code == 404


# ---------------------------------------------------------------------------
# The console card, rendered in node from the shipped functions
# ---------------------------------------------------------------------------

_CARD_FUNCTIONS = (
    "appLaunchIcon", "appMemberParts", "appCardMarkup", "appBadgeMarkup", "appTuiParts", "appTuiOf", "appNoticeMarkup",
    "appJobActive", "appJobAsProgress", "appPartsMarkup", "appTechSep", "appCmdLine", "appFirstRun", "appLogMarkup",
    "appRowById", "appBrowserOrigin", "appFamilyBadgeMarkup",
)


def _console_source() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))


def _const(source: str, name: str) -> str:
    m = re.search(r"const " + re.escape(name) + r" = \{[^\n]*\};", source)
    assert m, f"{name} not found (one line) in the console JavaScript"
    return m.group(0)


def _badge(running: bool) -> Dict[str, Any]:
    return {"label": "Running" if running else "Stopped", "tone": "ok" if running else "muted", "busy": False,
            "action": "stop" if running else "launch", "enabled": True, "tip": "Running — click to stop" if running else "Stopped — click to start"}


def _rows(*, family: bool) -> List[Dict[str, Any]]:
    """Rows shaped like overview()'s, with the registry's card fields."""
    def fields(spec: Any) -> Dict[str, Any]:
        out = am.card_fields(spec)
        if not family:
            out["family"] = None
        return out

    web = {"id": "assistant-web", "name": "Assistant (web)", "kind": "web", "description": am.APP_BY_ID["assistant-web"].description,
           **fields(am.APP_BY_ID["assistant-web"]), "installed": True, "running": True, "status": "running", "actions": ["open", "stop", "logs"],
           "interfaces": [{"kind": "web"}], "status_control": _badge(True), "package": "@abstractframework/assistant"}
    mac = {"id": "assistant", "name": "Assistant", "kind": "desktop", "description": desk.ASSISTANT.description, **fields(desk.ASSISTANT),
           "installed": True, "running": False, "status": "stopped", "actions": ["open"], "interfaces": [], "status_control": _badge(False),
           "desktop": {"launch_available": True, "launch_blocked": None, "open_label": "Open on this Mac", "where_label": "This Mac",
                       "other_running": {"sentence": "Another Assistant is running: /Applications/AbstractAssistant.app 0.5.0."},
                       "restart_note": "Quit it and open it again to run 0.14.0."}}
    code = {"id": "code", "name": "Code", "kind": "web", "description": am.APP_BY_ID["code"].description, **fields(am.APP_BY_ID["code"]),
            "installed": True, "running": True, "status": "running", "actions": ["open", "stop", "logs"], "status_control": _badge(True),
            "interfaces": [{"kind": "web"}, {"kind": "tui", "installed": True, "launch_available": True, "command": "abstractcode --gateway x"}]}
    return [code, web, mac]


def _render(rows: List[Dict[str, Any]], cards: List[Dict[str, Any]], tech: bool = False) -> Dict[str, str]:
    source = _console_source()
    functions = "\n".join(_slice_function(source, n) for n in _CARD_FUNCTIONS)
    consts = "\n".join(_const(source, n) for n in ("APP_LAUNCH_ICON", "APP_BADGE_PENDING"))
    script = r"""
const esc = (v) => String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const ICONS = { globe: "<svg data-icon=globe></svg>", monitor: "<svg data-icon=monitor></svg>", gear: "<svg data-icon=gear></svg>" };
const state = { principal: { admin: true } };
const appStore = { data: { apps: ROWS, cards: CARDS }, jobs: new Map(), busy: new Set(), pending: new Map(), notices: new Map(), logs: new Map() };
const APP_FIRST_RUN = {}; const APP_SETTINGS_DOORS = {};
function uiShowAdvanced() { return TECH; }
function uiPill(label, tone) { return `<span class="ui-pill tone-${tone}">${label}</span>`; }
function uiProgressMarkup() { return "<progress></progress>"; }
const location = { origin: "http://127.0.0.1:18080" };
CONSTS
FUNCTIONS
const out = {};
for (const c of CARDS) out[c.id] = appCardMarkup(c, CARDS.some((x) => x.illustration_url));
console.log(JSON.stringify([out]));
""".replace("TECH", "true" if tech else "false").replace("ROWS", json.dumps(rows)).replace("CARDS", json.dumps(cards)).replace("CONSTS", consts).replace("FUNCTIONS", functions)
    return _node(script)[0]


def _buttons(html: str) -> List[str]:
    return re.findall(r"<button[^>]*>.*?</button>", html, flags=re.S)


def test_the_family_card_renders_two_launchers_with_their_badges() -> None:
    rows = _rows(family=True)
    cards = am.app_cards(rows)
    assert [c["id"] for c in cards] == ["code", "assistant"]
    out = _render(rows, cards)
    card = out["assistant"]
    assert card.count('<article class="ui-card ui-app-card') == 1 and 'data-app-family="assistant"' in card
    assert 'data-app-card="family:assistant"' in card and 'data-app-card="assistant"' not in card
    assert '<div class="ui-card__title">Assistant</div>' in card
    sentence = am.APP_BY_ID["assistant-web"].description
    assert f'<div class="ui-card__blurb">{sentence}</div>' in card  # clamped to two lines, no hover tooltip
    row = card[card.index('class="ui-card__actions"'):card.index('class="ui-card__tech"')]
    launchers = [b for b in _buttons(row) if 'data-app-launch="' in b]
    assert [re.search(r'data-app-launch="(\w+)"', b).group(1) for b in launchers] == ["browser", "desktop"]
    web, mac = launchers
    assert "is-primary" in web and ">Open in browser</button>" in web and 'data-app-launch-badge="browser"' in web and "data-icon=globe" in web
    assert "is-ghost" in mac and ">Open on this Mac</button>" in mac and 'data-app-launch-badge="desktop"' in mac and "data-icon=monitor" in mac
    assert "data-icon=globe" not in mac and 'data-app-launch-badge="browser"' not in mac
    # ONE header badge for the family: a segment per member, in member order, each with its launcher's icon.
    head = card[card.index('class="ui-card__head"'):card.index('class="ui-card__blurb"')]
    assert head.count('class="ui-app-badges"') == 1
    assert 'role="group" aria-label="Browser: Running. This Mac: Stopped."' in head
    segs = re.findall(r'<button[^>]*ui-app-seg[^>]*>.*?</button>', head, flags=re.S)
    assert [re.search(r'data-member="([\w-]+)"', g).group(1) for g in segs] == ["assistant-web", "assistant"]
    web_seg, mac_seg = segs
    assert "data-icon=globe" in web_seg and '<span class="ui-app-seg__label">Running</span>' in web_seg
    assert "data-icon=monitor" in mac_seg and '<span class="ui-app-seg__label">Stopped</span>' in mac_seg
    # R11.3 per segment: each stops/starts its own member.
    assert 'data-app-action="badge" data-app="assistant-web"' in web_seg and 'data-badge-action="stop"' in web_seg
    assert 'data-app-action="badge" data-app="assistant"' in mac_seg and 'data-badge-action="launch"' in mac_seg
    assert head.count('class="ui-app-badges__sep"') == 1
    # No badge beside a member's button any more; its notes stay there.
    member = row[row.index('data-app-member="assistant"'):]
    assert "data-app-badge" not in row and "ui-app-seg" not in row
    assert 'data-app-desktop-other="assistant"' in member and 'data-app-desktop-restart="assistant"' in member
    assert len([b for b in _buttons(row) if "is-primary" in b]) == 1
    # The family's illustration (the browser member's registration), served with a version query.
    assert re.search(r'<div class="ui-app-card__art"><img src="/console/assets/apps/assistant\.webp\?v=[0-9a-f]{12}" alt=""', card)


def test_terminal_and_desktop_launchers_carry_no_browser_badge() -> None:
    rows = _rows(family=True)
    out = _render(rows, am.app_cards(rows))
    code = out["code"]
    row = code[code.index('class="ui-card__actions"'):code.index('class="ui-card__tech"')]
    buttons = _buttons(row)
    tui = next(b for b in buttons if 'data-app-action="tui-open"' in b)
    assert "&gt;_" in tui and "data-icon=globe" not in tui and "data-app-launch-badge" not in tui
    web = next(b for b in buttons if 'data-app-action="open"' in b)
    assert 'data-app-launch-badge="browser"' in web
    assert buttons.index(web) < buttons.index(tui)  # Open in browser, then Open in Terminal


def test_without_the_family_the_console_renders_two_assistant_cards() -> None:
    rows = _rows(family=False)
    cards = am.app_cards(rows)
    assert [c["id"] for c in cards] == ["code", "assistant-web", "assistant"]
    out = _render(rows, cards)
    assert '<div class="ui-card__title">Assistant (web)</div>' in out["assistant-web"]
    assert "data-app-member" not in out["assistant-web"] and "data-app-member" not in out["assistant"]
    mac = [b for b in _buttons(out["assistant"]) if 'data-app-launch="desktop"' in b]
    assert len(mac) == 1 and "is-primary" in mac[0]  # alone, the desktop app is its card's primary launcher
    # A single-member card keeps its plain badge (no segments, no group).
    for key, app_id in (("assistant", "assistant"), ("assistant-web", "assistant-web")):
        head = out[key][out[key].index('class="ui-card__head"'):out[key].index('class="ui-card__blurb"')]
        assert "ui-app-badges" not in head and "ui-app-seg" not in head and "data-member=" not in head
        assert f'ui-app-badge" data-app-action="badge" data-app="{app_id}"' in head
    code = out["code"][out["code"].index('class="ui-card__head"'):out["code"].index('class="ui-card__blurb"')]
    assert 'class="ui-pill tone-ok ui-app-badge" data-app-action="badge" data-app="code"' in code and "ui-app-badges" not in code


def test_an_illustration_shows_and_its_neighbours_show_their_mark() -> None:
    rows = _rows(family=True)
    cards = am.app_cards(rows)
    cards[0]["illustration_url"] = None
    cards[1]["illustration_url"] = "/console/assets/apps/assistant.webp?v=0123456789ab"
    out = _render(rows, cards)
    assert '<img src="/console/assets/apps/assistant.webp?v=0123456789ab" alt="" width="768" height="432"' in out["assistant"]
    assert '<span class="ui-app-card__artmark" aria-hidden="true">Co</span>' in out["code"] and "<img" not in out["code"]


def test_with_no_illustration_at_all_the_art_slot_stays_empty() -> None:
    rows = _rows(family=True)
    cards = [dict(c, illustration_url=None) for c in am.app_cards(rows)]
    out = _render(rows, cards)
    for html in out.values():
        assert '<div class="ui-app-card__art"></div>' in html and "<img" not in html and "ui-app-card__artmark" not in html


def test_a_family_card_names_what_each_install_installs_and_labels_each_technical_block() -> None:
    rows = _rows(family=True)
    for r in rows:
        if r["id"] in ("assistant-web", "assistant"):
            r.update(installed=False, running=False, status="not_installed", actions=["install"], status_control=dict(_badge(False), label="Not installed", tip=None))
    next(r for r in rows if r["id"] == "assistant")["desktop"]["install_command"] = "uv pip install abstractassistant"
    out = _render(rows, am.app_cards(rows), tech=True)
    card = out["assistant"]
    labels = [re.sub(r"<[^>]+>", "", b).strip() for b in _buttons(card) if 'data-app-action="install"' in b]
    assert labels == ["Install browser app", "Install desktop app"], labels  # never a bare Install on a shared card
    assert ">Browser app</span>" in card and ">Desktop app</span>" in card
    assert card.index(">Browser app</span>") < card.index(">Desktop app</span>")
    # A single-app card keeps the plain Install and no member label.
    rows[0].update(installed=False, running=False, status="not_installed", actions=["install"], interfaces=[{"kind": "web"}])
    code = _render(rows, am.app_cards(rows), tech=True)["code"]
    assert [re.sub(r"<[^>]+>", "", b).strip() for b in _buttons(code) if 'data-app-action="install"' in b] == ["Install"]
    assert "Browser app</span>" not in code


def test_the_error_fallback_has_no_app_list_of_its_own() -> None:
    source = _console_source()
    view = _slice_function(source, "appViewMarkup")
    assert "FIRST_RUN_APPS" not in source
    assert "This gateway cannot manage apps right now." in view


def test_node_is_a_card_only_when_it_needs_the_user() -> None:
    """Ready: one quiet line (version, where it was found), no card, no mark.
    Missing, installing or after a failed install: the card with its action."""
    source = _console_source()
    functions = "\n".join(_slice_function(source, n) for n in ("appRuntimeMarkup", "appJobActive", "appJobAsProgress", "appNoticeMarkup"))
    script = r"""
const esc = (v) => String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const state = { principal: { admin: true } };
const appStore = { jobs: new Map(), busy: new Set(), pending: new Map(), notices: new Map() };
const APP_PENDING = { "runtime-install": "Starting..." };
function uiPill(label, tone) { return `<span class="ui-pill tone-${tone}">${label}</span>`; }
function uiProgressMarkup() { return "<progress></progress>"; }
let TECH = false; function uiShowAdvanced() { return TECH; }
FUNCTIONS
const out = {};
out.readyPlain = appRuntimeMarkup({ runtime: { node: { available: true, version: "24.14.0", source: "system", path: "/usr/local/bin/node" } } });
out.missingPlain = appRuntimeMarkup({ runtime: { node: { available: false, install_available: true } } });
TECH = true;
out.ready = appRuntimeMarkup({ runtime: { node: { available: true, version: "24.14.0", source: "system", path: "/usr/local/bin/node" } } });
out.missing = appRuntimeMarkup({ runtime: { node: { available: false, install_available: true } } });
out.installing = appRuntimeMarkup({ runtime: { node: { available: false, active_job: { id: "j", state: "running", percent: 40 } } } });
appStore.notices.set("__node__", { tone: "err", text: "Node.js did not install." });
out.failed = appRuntimeMarkup({ runtime: { node: { available: false, install_available: true } } });
console.log(JSON.stringify([out]));
""".replace("FUNCTIONS", functions)
    out = _node(script)[0]
    assert out["readyPlain"] == ""  # ready, Technical details off: nothing at all
    assert '<article class="ui-card ui-runtime-row' in out["missingPlain"]  # it needs the user: always the card
    ready = out["ready"]
    assert "<article" not in ready and "ui-card" not in ready and "ui-mark" not in ready
    assert 'data-runtime-state="ready"' in ready and "Node.js 24.14.0 · found on this computer" in ready
    assert "/usr/local/bin/node" in ready
    for key in ("missing", "installing", "failed"):
        assert '<article class="ui-card ui-runtime-row' in out[key], key
    assert 'data-app-action="runtime-install"' in out["missing"] and ">Install Node.js</button>" in out["missing"]
    assert "<progress>" in out["installing"] and 'data-app-action="cancel"' in out["installing"]
    assert "Node.js did not install." in out["failed"] and ">Install Node.js</button>" in out["failed"]  # the reason, and Install again


def test_card_styles_clamp_the_description_and_fade_the_illustration() -> None:
    from abstractgateway.console_ui import CONSOLE_UI_CSS

    blurb = re.search(r"\.ui-app-card \.ui-card__blurb \{([^}]*)\}", CONSOLE_UI_CSS)
    assert blurb and "-webkit-line-clamp: 2" in blurb.group(1) and "min-height: 2.9em" in blurb.group(1)
    fade = re.search(r"\.ui-app-card__art::after \{([^}]*)\}", CONSOLE_UI_CSS)
    assert fade and "linear-gradient(to bottom, transparent, var(--bg-card" in fade.group(1)
    assert ".ui-card__blurb.is-oneline" not in CONSOLE_UI_CSS


def test_the_apps_toolbar_is_one_line_of_icon_buttons() -> None:
    source = _console_source()
    view = _slice_function(source, "appViewMarkup")
    assert "Apps open in your browser" not in source and "already signed in to this gateway" not in view
    assert 'data-app-action="refresh" aria-label="Refresh" data-af-tip="Check installed apps and updates again"' in view
    assert "${ICONS.refresh}" in view and '"Check again"' not in view
    card = _slice_function(source, "appCardMarkup")
    assert "ui-app-card__chip" not in source and 'class="ui-card__blurb" title=' not in card


def test_registry_descriptions_fit_the_two_line_ceiling() -> None:
    """Two lines of the card at the 3-column desktop width hold ~96
    characters: every description stays within 92, so none is cut ("…")."""
    from abstractgateway.apps_desktop import DESKTOP_APPS

    for spec in [*am.APPS, *DESKTOP_APPS]:
        assert len(spec.description) <= 92, (spec.id, len(spec.description))
