"""The web console's Docs assistant (round 8, R8.3).

The console mounts the kit's DocsAssistantDrawer (panel-chat, through the
islands bundle's `mountDocsAssistant`) — the same component every app mounts.
The conversation, the docs-qa transport, history (one gateway session per
conversation, ADR-0026) and streaming live in the kit (panel-chat
scripts/check_docs_assistant.mjs). The console owns only:
- the source ({app: "gateway"}: the gateway's own llms.txt),
- a GatewayFetch on its own origin (session cookie, CSRF on writes),
- the open state (top-bar `docs` button) and `connected` (signed in).

The shipped functions are cut out of the served console and driven in node
with a fake islands lib and a fake fetch.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.basic


def _source() -> str:
    from abstractgateway.console import gateway_console_html

    return "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))


def _harness(scenario: str) -> list:
    from node_requirement import require_node
    from test_gateway_console_offline import _node, _slice_function

    require_node()
    src = _source()
    consts = []
    for name in ("assistantState", "DOCS_ASSISTANT_SOURCE"):
        m = re.search(r"const " + name + r" = [^\n]*\n", src)
        assert m, f"{name} missing from the console JavaScript"
        consts.append(m.group(0))
    fns = "\n".join(_slice_function(src, n) for n in ("docsAssistantFetch", "docsAssistantProps", "renderDocsAssistant", "toggleAssistant"))
    script = f"""
const mounted = []; const updates = []; const fetches = []; const errors = [];
const console = {{ error: (m) => errors.push(String(m)) }};
const lib = {{ mountDocsAssistant(el, props) {{ mounted.push({{ el, props }}); return {{ update(p) {{ updates.push(p); }} }}; }} }};
const islands = {{ lib }};
const state = {{ principal: {{ id: "admin" }} }};
let islandRenders = 0;
function renderIslands() {{ islandRenders += 1; renderDocsAssistant(); }}
function $(id) {{ return {{ id }}; }}
function csrf() {{ return "tok%20en"; }}
function fetch(url, init) {{ fetches.push({{ url, method: init.method || "GET", csrf: init.headers.get("X-AbstractGateway-CSRF"), credentials: init.credentials, accept: init.headers.get("Accept") }}); return Promise.resolve({{ ok: true }}); }}
{''.join(consts)}
{fns}
{scenario}
"""
    return _node(script)


def test_the_top_bar_button_mounts_the_kit_drawer_once_then_updates_it() -> None:
    out = _harness(
        """
toggleAssistant();
const first = mounted[0];
toggleAssistant();
const closed = updates[updates.length - 1];
first.props.onClose();
console.log; process.stdout.write(JSON.stringify([{
  mounts: mounted.length, el: first.el.id, open: first.props.open, source: first.props.source,
  connected: first.props.connected, suggestions: first.props.suggestions.length, placeholder: first.props.placeholder,
  closedOpen: closed.open, afterOnClose: updates[updates.length - 1].open, errors,
}]) + "\\n");
"""
    )[0]
    assert out["mounts"] == 1, "the island mounts once and is updated afterwards (keep-alive)"
    assert out["el"] == "af-docs-assistant-root"
    assert out["open"] is True and out["closedOpen"] is False and out["afterOnClose"] is False
    assert out["source"] == {"app": "gateway", "name": "AbstractGateway"}
    assert out["connected"] is True
    assert out["suggestions"] >= 1 and out["placeholder"] == "Ask about the gateway…"
    assert out["errors"] == []


def test_the_gateway_fetch_is_same_origin_with_csrf_on_writes_only() -> None:
    out = _harness(
        """
toggleAssistant();
const f = mounted[0].props.fetchGateway;
f("api/gateway/runs/start", { method: "POST", body: "{}" });
f("api/gateway/docs/corpus?app=gateway", { headers: { Accept: "application/json" } });
f("api/gateway/runs/r1/ledger/stream?after=0", { headers: { Accept: "text/event-stream" } });
process.stdout.write(JSON.stringify([fetches]) + "\\n");
"""
    )[0]
    assert out[0] == {"url": "/api/gateway/runs/start", "method": "POST", "csrf": "tok en", "credentials": "same-origin", "accept": None}
    assert out[1]["url"] == "/api/gateway/docs/corpus?app=gateway" and out[1]["csrf"] is None and out[1]["accept"] == "application/json"
    assert out[2]["url"] == "/api/gateway/runs/r1/ledger/stream?after=0" and out[2]["accept"] == "text/event-stream"


def test_signed_out_disables_the_composer_and_a_bundle_without_the_island_says_so() -> None:
    out = _harness(
        """
state.principal = null;
toggleAssistant();
const signedOut = mounted[0].props.connected;
delete lib.mountDocsAssistant; assistantState.handle = null;
toggleAssistant(); toggleAssistant();
process.stdout.write(JSON.stringify([{ signedOut, errors }]) + "\\n");
"""
    )[0]
    assert out["signedOut"] is False
    assert len(out["errors"]) == 1 and "mountDocsAssistant" in out["errors"][0]


def test_the_top_bar_carries_the_docs_slot_not_the_generic_assistant() -> None:
    from test_gateway_console_offline import _slice_function

    props = _slice_function(_source(), "topBarIslandProps")
    assert 'docs: p ? { open: !!assistantState.open, onToggle: () => toggleAssistant(), label: "Docs assistant" } : null' in props
    assert "assistant:" not in props
