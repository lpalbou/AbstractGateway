"""Mission JJ (2026-09-24): the Entity card's first-run action.

The operator: "for entity, if there are no entities summoned yet, maybe give a
button to create one ... otherwise if i click on the open, i just arrive here
and nothing". So:

- the apps payload carries, for Entity only, `content_summary.entities_count`
  (the entry count of `GET /api/gateway/entities`; `null` when unknown);
- `POST /apps/{id}/open {path}` lands the browser at a SAME-APP path after the
  signed-in handover ("/#new" opens Entity's creation form); anything that
  could leave the app's origin is refused (400 invalid_app_path);
- the console card's one primary button reads "Create your first entity" when
  the count is exactly 0, "Open" otherwise (1+, or unknown).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_manager as am

_TOKEN = "apps-first-run-test-token-0123456789"


class _Offline:
    def __call__(self, req, timeout=None):
        import urllib.error

        raise urllib.error.URLError("offline")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    m = am.AppsManager(tmp_path / "runtime", urlopen=_Offline(), install_allowed=lambda: True)
    m.external_probe = lambda **_kw: {}
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}), m


def _running(m: am.AppsManager, monkeypatch: pytest.MonkeyPatch, app_id: str = "entity", port: int = 18998) -> None:
    real = m.app_row

    def row(spec, **kw):
        out = real(spec, **kw)
        if spec.id == app_id:
            out.update({"running": True, "port": port, "url": f"http://127.0.0.1:{port}/", "status": "running"})
        return out

    monkeypatch.setattr(m, "app_row", row)
    m._update_app_state(app_id, gateway_url="http://127.0.0.1:18823", port=port)


# ---------------------------------------------------------------------------
# The handover path: same app, or refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,want", [(None, "/"), ("", "/"), ("/", "/"), ("/#new", "/#new"), ("/?entity=castor", "/?entity=castor"), ("/a/b?x=1#new", "/a/b?x=1#new")])
def test_handover_path_accepts_same_app_paths(path, want) -> None:
    assert am.handover_path(path) == want


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/",  # network-path reference: another host
        "//evil.example",
        "https://evil.example/#new",
        "http:/evil.example",
        "javascript:alert(1)",
        "evil.example/#new",
        "#new",  # not a path
        "/\\evil.example",  # browsers read the backslash as a slash
        "\\\\evil.example",
        "/ #new",
        "/\t/evil.example",
        "/\n#new",
        "/\x00",
        "/" + "a" * 2048,
    ],
)
def test_handover_path_refuses_anything_that_can_leave_the_app(path) -> None:
    with pytest.raises(am.InvalidAppPath):
        am.handover_path(path)


def test_open_with_a_path_lands_there_after_the_signed_in_handover(env, monkeypatch) -> None:
    client, m = env
    _running(m, monkeypatch)
    r = client.post("/api/gateway/apps/entity/open", json={"path": "/#new"}, headers={"host": "127.0.0.1:18823"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["open_url"].startswith("/apps/handover/")
    assert "new" not in body["open_url"]  # the path is bound to the code, not carried in the link
    browser = TestClient(client.app)
    h = browser.get(body["open_url"], headers={"host": "127.0.0.1:18823"}, follow_redirects=False)
    assert h.status_code == 303
    assert h.headers["location"] == "http://127.0.0.1:18998/#new"
    names = {c.split("=", 1)[0] for c in h.headers.get_list("set-cookie")}
    assert names == {"abstractentity_gateway_url", "abstractentity_gateway_session", "abstractentity_gateway_csrf"}


def test_open_without_a_path_lands_on_the_app_root(env, monkeypatch) -> None:
    client, m = env
    _running(m, monkeypatch)
    body = client.post("/api/gateway/apps/entity/open", json={}, headers={"host": "127.0.0.1:18823"}).json()
    h = TestClient(client.app).get(body["open_url"], headers={"host": "127.0.0.1:18823"}, follow_redirects=False)
    assert h.headers["location"] == "http://127.0.0.1:18998/"


@pytest.mark.parametrize("path", ["//evil.example/#new", "https://evil.example/", "/\\evil.example", "evil.example"])
def test_open_refuses_a_foreign_path_and_mints_nothing(env, monkeypatch, path) -> None:
    client, m = env
    _running(m, monkeypatch)
    r = client.post("/api/gateway/apps/entity/open", json={"path": path}, headers={"host": "127.0.0.1:18823"})
    assert r.status_code == 400, r.text
    assert r.json()["reason"] == "invalid_app_path"
    assert m._handover == {}


def test_the_handover_link_cannot_be_retargeted_from_its_query(env, monkeypatch) -> None:
    client, m = env
    _running(m, monkeypatch)
    body = client.post("/api/gateway/apps/entity/open", json={"path": "/#new"}, headers={"host": "127.0.0.1:18823"}).json()
    h = TestClient(client.app).get(body["open_url"] + "?path=//evil.example/", headers={"host": "127.0.0.1:18823"}, follow_redirects=False)
    assert h.headers["location"] == "http://127.0.0.1:18998/#new"


def test_redeem_keeps_its_three_field_shape_and_the_target_carries_the_path() -> None:
    m = am.AppsManager(Path(tempfile.mkdtemp()), urlopen=_Offline())
    code = m.mint_handover("entity", "p", host="127.0.0.1", path="/#new")
    assert m.redeem_handover_target(code) == ("entity", "p", "127.0.0.1", "/#new")
    code = m.mint_handover("entity", "p", host="127.0.0.1")
    assert m.redeem_handover(code) == ("entity", "p", "127.0.0.1")
    with pytest.raises(am.InvalidAppPath):
        m.mint_handover("entity", "p", host="127.0.0.1", path="//evil.example")


# ---------------------------------------------------------------------------
# content_summary: Entity only, from the same source as GET /entities
# ---------------------------------------------------------------------------


def _installed(m: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "installed_version", lambda app_id: "0.1.0")


@pytest.mark.parametrize("count,want", [(0, 0), (1, 1), (7, 7), (None, None)])
def test_the_entity_row_carries_the_entities_count(env, monkeypatch, count, want) -> None:
    client, m = env
    _installed(m, monkeypatch)
    m.entities_counter = lambda: count
    apps = {a["id"]: a for a in client.get("/api/gateway/apps?latest=false").json()["apps"]}
    assert apps["entity"]["content_summary"] == {"entities_count": want}
    for other in ("observer", "continuum", "code", "flow"):
        assert apps[other]["content_summary"] is None, other  # no invented counts


def test_a_failing_count_is_unknown_not_an_error(env, monkeypatch) -> None:
    client, m = env
    _installed(m, monkeypatch)

    def boom():
        raise RuntimeError("registry unavailable")

    m.entities_counter = boom
    r = client.get("/api/gateway/apps?latest=false")
    assert r.status_code == 200
    assert {a["id"]: a for a in r.json()["apps"]}["entity"]["content_summary"] == {"entities_count": None}


def test_a_not_installed_entity_has_no_summary(env) -> None:
    client, m = env
    m.entities_counter = lambda: 0
    apps = {a["id"]: a for a in client.get("/api/gateway/apps?latest=false").json()["apps"]}
    assert apps["entity"]["installed"] is False and apps["entity"]["content_summary"] is None


def test_an_entity_app_started_outside_counts_only_for_this_gateway(tmp_path: Path) -> None:
    m = am.AppsManager(tmp_path, urlopen=_Offline())
    m.gateway_url = "http://127.0.0.1:18823"
    m.entities_counter = lambda: 0
    spec = am.spec_for("entity")
    for same in ("http://127.0.0.1:18823", "http://localhost:18823/", None):
        m.external_probe = lambda same=same, **_kw: {"entity": am.ExternalApp("entity", 18997, "http://127.0.0.1:18997/", gateway_url=same)}
        m._external_cache = None
        row = m.app_row(spec)
        assert row["source"] == "external" and row["content_summary"] == {"entities_count": 0}, same
    m.external_probe = lambda **_kw: {"entity": am.ExternalApp("entity", 18997, "http://127.0.0.1:18997/", gateway_url="http://127.0.0.1:8080")}
    m._external_cache = None
    assert m.app_row(spec)["content_summary"] == {"entities_count": None}


def test_the_count_is_the_entries_get_entities_lists(tmp_path: Path, monkeypatch) -> None:
    """Same source as the route (`routes.entities._registry()`), same rule as
    `EntityRegistry.list_entities`: a directory with a manifest is listed
    (an unreadable one too, as an error entry); anything else is not."""
    from abstractgateway.entities import MANIFEST_FILENAME, EntityRegistry
    import abstractgateway.routes.entities as entity_routes

    reg = EntityRegistry(data_dir=tmp_path)
    monkeypatch.setattr(entity_routes, "_registry", lambda: reg)
    assert am.gateway_entities_count() == 0 == len(reg.list_entities())  # no entities dir yet
    (tmp_path / "entities" / "castor").mkdir(parents=True)
    (tmp_path / "entities" / "castor" / MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
    (tmp_path / "entities" / "pollux").mkdir()
    (tmp_path / "entities" / "pollux" / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    (tmp_path / "entities" / "half-made").mkdir()  # no manifest: not an entity
    (tmp_path / "entities" / "stray.txt").write_text("x", encoding="utf-8")
    assert am.gateway_entities_count() == 2 == len(reg.list_entities())


def test_the_count_is_unknown_when_the_registry_is_not_reachable(monkeypatch) -> None:
    import abstractgateway.routes.entities as entity_routes

    def boom():
        raise RuntimeError("no gateway service")

    monkeypatch.setattr(entity_routes, "_registry", boom)
    assert am.gateway_entities_count() is None


# ---------------------------------------------------------------------------
# The console card, driven for real in node (the console's own JavaScript)
# ---------------------------------------------------------------------------

_CARD_SCENARIO = r"""
const rowFor = (count, extra) => Object.assign({ id: "entity", name: "Entity", package: "@abstractframework/entity", installed: true, version: "0.1.0",
  running: true, status: "running", actions: ["open", "stop", "logs"], url: "http://127.0.0.1:18998/", interfaces: [{ kind: "web" }],
  content_summary: count === undefined ? null : { entities_count: count } }, extra || {});
const neighbour = { id: "flow", name: "Flow", package: "@abstractframework/flow", installed: true, version: "0.1.0", running: true, status: "running",
  actions: ["open", "stop", "logs"], url: "http://127.0.0.1:18996/", interfaces: [{ kind: "web" }], content_summary: { entities_count: 0 } };
let appsPayload = null;
const posts = [];
const assigned = [];
location.assign = (u) => assigned.push(u);
context.fetch = async (path, options = {}) => {
  const method = options.method || "GET";
  if (path === "/api/gateway/me") return res(200, { ok: true, principal: { user_id: "admin", roles: ["admin"], admin: true } });
  if (path === "/api/gateway/host/first-run") return res(200, { ok: true, completed: true });
  if (path.startsWith("/api/gateway/apps?")) return res(200, { ok: true, gateway_url: "http://127.0.0.1:18823", install_allowed: true, registry: { reachable: true }, runtime: { node: { available: true } }, apps: appsPayload });
  if (path === "/api/gateway/apps/entity/open" && method === "POST") { posts.push(JSON.parse(String(options.body || "{}"))); return res(200, { ok: true, open_url: "/apps/handover/c0de" }); }
  if (path === "/api/gateway/apps/entity/launch" && method === "POST") return res(200, { ok: true, app: rowFor(0) });
  return res(200, {});
};
vm.runInContext(source, context);
await settle();
try { vm.runInContext("state.principal = state.principal || { admin: true };", context); } catch (e) { console.log("STATE " + e.message); }
const out = {};
const card = (html, id) => { const parts = html.split('<article class="ui-card ui-app-card').slice(1); return parts.find((c) => c.includes('data-app-card="' + id + '"')) || ""; };
const actionRow = (c) => c.slice(c.indexOf('class="ui-card__actions"'), c.indexOf('class="ui-card__tech"'));
for (const [key, apps] of Object.entries({
  zero: [rowFor(0), neighbour], one: [rowFor(1), neighbour], unknown: [rowFor(null), neighbour], absent: [rowFor(undefined), neighbour],
  stoppedZero: [rowFor(0, { running: false, status: "stopped", actions: ["launch", "logs"] }), neighbour],
})) {
  appsPayload = apps;
  const host = el("cards-" + key);
  context.mountAppCards("t-" + key, host);
  await settle();
  out[key] = { entity: actionRow(card(host.innerHTML, "entity")), flow: actionRow(card(host.innerHTML, "flow")) };
  context.unmountAppCards("t-" + key);
}
appsPayload = [rowFor(0), neighbour];
context.mountAppCards("t-click", el("cards-click"));
await settle();
await context.appAction("open", "entity", { dataset: { appPath: "/#new" } }); await settle();
await context.appAction("open", "entity", { dataset: {} }); await settle();
out.posts = posts;
out.assigned = assigned;
console.log("RESULT " + JSON.stringify(out));
"""


def _drive_card() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to drive the console card")
    import re

    from abstractgateway.console import gateway_console_html
    from tests.test_gateway_console_first_run import _HARNESS, WIZARD_IDS

    marker = "vm.runInContext(source, context);"
    assert marker in _HARNESS, "the console node harness changed shape (tests/test_gateway_console_first_run.py)"
    prefix = _HARNESS[: _HARNESS.index(marker)]
    html = gateway_console_html()
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    harness = (
        prefix.replace("__SOURCE__", json.dumps("\n".join(scripts)))
        .replace("__SCENARIO__", json.dumps({"name": "apps-first-run", "hash": "", "completed": True}))
        .replace("__CLASSES__", json.dumps({k: "" for k in WIZARD_IDS}))
        + _CARD_SCENARIO
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = f.name
    proc = subprocess.run([node, path], capture_output=True, text=True, check=False, timeout=60)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-4000:]
    return json.loads(line[len("RESULT "):])


def _primary(row_html: str) -> str:
    import re

    buttons = re.findall(r"<button[^>]*>[^<]*</button>", row_html)
    primary = [b for b in buttons if "is-primary" in b]
    assert len(primary) == 1, row_html  # GG's rule: one primary action per card
    return primary[0]


def test_console_card_says_create_your_first_entity_only_at_zero() -> None:
    out = _drive_card()
    zero = _primary(out["zero"]["entity"])
    assert ">Create your first entity</button>" in zero and 'data-app-path="/#new"' in zero and 'data-app-action="open"' in zero
    stopped = _primary(out["stoppedZero"]["entity"])
    assert ">Create your first entity</button>" in stopped and 'data-app-path="/#new"' in stopped
    for key in ("one", "unknown", "absent"):
        p = _primary(out[key]["entity"])
        assert ">Open</button>" in p and "data-app-path" not in p, (key, p)
    # Keyed by app: another app's summary never turns its button into Entity's.
    for key in ("zero", "one"):
        p = _primary(out[key]["flow"])
        assert ">Open</button>" in p and "data-app-path" not in p


def test_console_card_click_sends_the_path_through_the_handover() -> None:
    out = _drive_card()
    assert out["posts"] == [{"path": "/#new"}, {}]
    assert out["assigned"] == ["/apps/handover/c0de", "/apps/handover/c0de"]
