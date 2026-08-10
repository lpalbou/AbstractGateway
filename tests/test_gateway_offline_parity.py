"""Offline-operation parity between the gateway console-TUI and the web console.

THE LAW UNDER TEST
------------------
The framework must be fully configurable with zero internet, against LOCAL
inference servers. Two surfaces write the SAME AbstractCore store
(``~/.abstractcore/config/abstractcore.json``): the Rust console-TUI
(``console-tui/``) and the browser console (``abstractgateway/console.py``).
An operator who can configure a route in one must be able to configure it in
the other, and neither may leave a transient "Loading..." label as a terminal
state when the network is gone.

The console-TUI is the PARITY BAR because it is the surface that demonstrably
worked offline. Its mechanism, verbatim from the source:

  * ``console-tui/src/api.rs:172-181`` -- two pooled ureq agents,
    ``timeout_connect(5s)`` + ``timeout_read(60s)``, and a ``slow_agent`` at
    ``timeout_read(300s)`` for calls that legitimately run for minutes. NOTHING
    is unbounded.
  * ``console-tui/src/ui/routes.rs:1100-1215`` -- the model field has FOUR
    terminal states, and two of them are a FREE-TEXT lane: ``Loadable::Failed``
    renders "discovery failed - type the model id" plus the verbatim error plus
    an explicit "Retry model discovery" button; ``Ready(empty)`` renders
    "no discoverable models - type the model id". Discovery failure therefore
    never blocks configuration.
  * ``console-tui/src/ui/routes.rs:905-912`` -- a FAILED cache entry is
    re-fetched on the next provider pick ("a Failed entry left alone would make
    a transient blip permanent for the session"). Failure is never sticky.

Tests below lock the web console against that bar. Tests that encode a parity
gap the web console does not yet meet are marked ``xfail(strict=True)`` so the
suite stays honest in both directions: red if a fixed gap regresses, XPASS the
moment the gap is closed.

Written by the parity auditor; read-only on all product source.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from abstractgateway.console import gateway_console_html

# --------------------------------------------------------------------------
# Sources under audit
# --------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]
_TUI_API_RS = _REPO / "console-tui" / "src" / "api.rs"
_TUI_ROUTES_RS = _REPO / "console-tui" / "src" / "ui" / "routes.rs"


def _console_js() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts, "the console page must carry inline <script> blocks"
    return "\n".join(scripts)


def _tui_src(path: Path) -> str:
    if not path.exists():  # sdist / wheel checkouts ship no Rust tree
        pytest.skip(f"console-tui source not present: {path}")
    return path.read_text(encoding="utf-8")


def _block_from(js: str, open_at: int) -> str:
    """The brace-balanced block starting at the ``{`` at ``open_at``."""
    depth = 0
    for j in range(open_at, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[open_at : j + 1]
    raise AssertionError(f"unbalanced braces at offset {open_at}")


def _fn_body(js: str, name: str) -> str:
    """The source of one top-level ``function name(...)`` by brace matching.

    The parameter list is skipped by paren balance first -- ``function
    api(path, options = {})`` carries a brace INSIDE the signature, and naive
    "first brace after the name" matching returns that empty default instead of
    the body.
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", js)
    assert m, f"{name}() not found in the console JavaScript"
    depth = 0
    k = m.end() - 1
    while k < len(js):
        if js[k] == "(":
            depth += 1
        elif js[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return _block_from(js, js.index("{", k))


def _handler_body(js: str, selector: str, event: str) -> str:
    """The source of a ``$("id").onchange = ...`` style handler."""
    pat = r'\$\("' + re.escape(selector) + r'"\)\.' + re.escape(event) + r"\s*=\s*"
    m = re.search(pat, js)
    assert m, f'$("{selector}").{event} handler not found'
    i = js.index("{", m.end())
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i : j + 1]
    raise AssertionError(f"unbalanced braces in {selector}.{event}")


# --------------------------------------------------------------------------
# 1. The TUI's resilience mechanism -- the bar itself must not rot
# --------------------------------------------------------------------------


def test_tui_http_agents_are_bounded_with_a_slow_lane() -> None:
    """Neither TUI agent may be unbounded, and the slow lane must exist."""
    src = _tui_src(_TUI_API_RS)
    assert "timeout_connect(Duration::from_secs(5))" in src
    assert "timeout_read(Duration::from_secs(60))" in src, "default read budget"
    assert "timeout_read(Duration::from_secs(300))" in src, "slow_agent read budget"
    assert "slow_agent" in src


def test_tui_route_editor_keeps_a_free_text_lane_when_discovery_fails() -> None:
    """Discovery failure must never remove the operator's ability to type."""
    src = _tui_src(_TUI_ROUTES_RS)
    assert "discovery failed — type the model id" in src
    assert "no discoverable models — type the model id" in src
    assert "Retry model discovery" in src
    assert "model id for that provider" in src, "CUSTOM provider row's model lane"


def test_tui_retries_a_failed_model_cache_entry() -> None:
    """A failed entry must be re-fetched, never latched for the session."""
    src = _tui_src(_TUI_ROUTES_RS)
    assert "Loadable::Failed(_)" in src, "failed entries are detected for re-load"


# --------------------------------------------------------------------------
# 2. Web console: every gateway call is bounded (the offline incident)
# --------------------------------------------------------------------------


def test_web_api_helper_bounds_every_request() -> None:
    """``fetch()`` has no default timeout; the helper must supply one.

    Without this a blackholed upstream leaves the promise pending forever and
    whatever "Loading..." label the caller set becomes permanent -- the
    2026-08-02 offline incident.
    """
    body = _fn_body(_console_js(), "api")
    assert "AbortController" in body, "api() must be abortable"
    assert "signal" in body, "the controller must be wired into fetch()"
    assert "setTimeout" in body and "abort()" in body, "a deadline must fire the abort"
    assert "clearTimeout" in body, "the timer must not outlive the request"


def test_web_api_timeout_budgets_match_the_tui_agents() -> None:
    """60s default / 300s slow -- the console-TUI's two ureq agents."""
    js = _console_js()
    assert "API_TIMEOUT_MS = 60000" in js, "default budget must equal the TUI's 60s read"
    assert "API_SLOW_TIMEOUT_MS = 300000" in js, "slow budget must equal the TUI's 300s read"


def test_web_abort_names_which_failure_happened() -> None:
    """A timeout and an unreachable gateway are two different truths.

    The TUI keeps them apart in ``ApiErrorKind``; the browser's opaque
    "Failed to fetch" teaches nothing, so the helper must relabel.
    """
    body = _fn_body(_console_js(), "api")
    assert "AbortError" in body
    assert "timed out" in body.lower()
    assert "unreachable" in body.lower()


# --------------------------------------------------------------------------
# 3. Web console: no transient label may become terminal
# --------------------------------------------------------------------------

_SPINNER_OWNERS = [
    # (function name, the transient label it sets)
    ("loadDefaultModels", "Loading models..."),
    ("loadDefaultVoices", "Loading voices..."),
    ("loadEntityVoiceModels", "Loading models..."),
    ("loadEntityVoiceVoices", "Loading voices..."),
    ("loadSandboxModels", "Loading models..."),
    ("discoverEndpointModels", "Discovering models from endpoint..."),
]


@pytest.mark.parametrize("fn,label", _SPINNER_OWNERS)
def test_web_spinner_owner_has_a_terminal_failure_path(fn: str, label: str) -> None:
    """The function that SETS a transient label must also clear it on failure.

    Ownership matters: a caller-side catch only covers the callers that
    remembered one. ``$("modal-default-provider").onchange`` did not, which is
    how "Loading models..." became permanent offline.
    """
    body = _fn_body(_console_js(), fn)
    assert label in body, f"{fn}() is expected to set {label!r}"
    assert "catch" in body, (
        f"{fn}() sets {label!r} but has no catch -- a rejected request leaves "
        f"that label on screen forever"
    )


def test_web_provider_change_handler_cannot_strand_the_model_select() -> None:
    """The retry gesture the modal offers must itself be failure-safe.

    Either the handler catches, or every ``load*`` it calls owns its own
    terminal state (the fix that shipped). One of the two must hold.
    """
    js = _console_js()
    handler = _handler_body(js, "modal-default-provider", "onchange")
    called = [n for n in ("loadDefaultModels", "loadDefaultVoices") if n in handler]
    assert called, "the handler is expected to reload the model/voice catalogs"
    if "catch" in handler:
        return
    for name in called:
        assert "catch" in _fn_body(js, name), (
            f"{name}() is awaited by an uncaught provider-change handler and "
            f"does not own its own terminal state"
        )


def test_web_model_cache_evicts_rejected_promises() -> None:
    """A cached REJECTED promise re-throws forever without touching the network.

    Measured 2026-08-02: after the network came back the retry issued no
    request at all; only clearing the cache healed it. The TUI's equivalent
    explicitly re-loads ``Loadable::Failed`` entries.
    """
    body = _fn_body(_console_js(), "fetchDefaultModels")
    assert "state.providerModels.set(cacheKey, promise)" in body, (
        "this test guards the in-flight-promise cache; the shape changed"
    )
    assert re.search(r"\.catch\(\s*\(\s*\)\s*=>", body), (
        "the cached promise must carry a rejection handler that evicts the key"
    )
    assert "delete(cacheKey)" in body, "a failed entry must be evicted, not latched"


# --------------------------------------------------------------------------
# 4. Web console: long calls must not be cut by the default budget
# --------------------------------------------------------------------------

# Endpoints the console-TUI routes through its 300s slow_agent (api.rs), plus
# the media lanes the TUI has no equivalent for. A 60s cap on any of these is a
# REGRESSION introduced by bounding api(): the gateway keeps working while the
# browser reports a timeout.
_MUST_BE_SLOW = [
    "/models/download",
    "/sandbox/generate",
    "/voice/tts",
    "/images/generate",
    "/videos/generate",
    "/music/generate",
]


def _api_call_sites(js: str):
    """(line_no, full call text) for every ``api(...)`` call, by paren balance."""
    for m in re.finditer(r"(?<![\w.$])api\(", js):
        depth = 0
        k = m.end() - 1
        while k < len(js):
            if js[k] == "(":
                depth += 1
            elif js[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        yield js.count("\n", 0, m.start()) + 1, js[m.start() : k + 1]


@pytest.mark.parametrize("endpoint", _MUST_BE_SLOW)
def test_web_long_running_calls_use_the_slow_budget(endpoint: str) -> None:
    """Every api() call to a minutes-long endpoint must pass ``slow: true``.

    Bounding api() at 60s is what makes this load-bearing: before the timeout
    these calls were unbounded and simply worked. A missed ``slow`` now aborts
    a generation the gateway is still running.
    """
    offenders = [
        f"js line {ln}: {call[:120]}"
        for ln, call in _api_call_sites(_console_js())
        # only the call that STARTS the work; the job-status GET beside it is
        # a poll and must keep the short budget.
        if endpoint in call and 'method: "POST"' in call and "slow: true" not in call
    ]
    assert not offenders, (
        f"{endpoint} is called through api() without slow:true -> capped at 60s "
        f"while the gateway keeps generating. Offenders: {offenders}"
    )


def test_web_sandbox_media_generation_uses_the_slow_budget() -> None:
    """The sandbox's media lane dispatches through a VARIABLE endpoint.

    ``runSandbox`` builds ``endpoint`` (images/videos/music/tts) and then calls
    ``api(endpoint, ...)``, so a literal-path scan cannot see it. Image and
    video generation on the seeded local defaults (mlx-gen flux.2 / wan2.2)
    runs for MINUTES; a 60s abort is a hard regression.

    Was RED on 2026-08-02: this call site was the one media lane the timeout fix
    did not mark, precisely because a literal-path scan cannot see a computed
    endpoint. Fixed with one token -- ``{ slow: true, method: "POST", ... }``.
    """
    body = _fn_body(_console_js(), "runSandbox")
    assert "/images/generate" in body, "this test guards runSandbox's media lane"
    unslowed = [
        call[:140]
        for _, call in _api_call_sites(body)
        if re.match(r"api\(\s*endpoint\b", call) and "slow: true" not in call
    ]
    assert not unslowed, (
        "runSandbox dispatches image/video/music/tts generation through "
        f"api(endpoint, ...) without slow:true: {unslowed}"
    )


# --------------------------------------------------------------------------
# 5. Shared-store inheritance: both surfaces name the same authority
# --------------------------------------------------------------------------


def test_web_console_surfaces_the_core_store_authority_line() -> None:
    js = _console_js()
    assert "config_file" in js, "the console must read the payload's config_file"
    assert "authority" in js
    assert "shared with AbstractCore" in js


def test_tui_surfaces_the_core_store_authority_line() -> None:
    store = _tui_src(_REPO / "console-tui" / "src" / "store.rs")
    assert 'b(v, "writable")' in store, "the TUI must parse the payload's writable flag"
    assert 's(v, "authority")' in store, "the TUI must parse the payload's authority"
    routes = _tui_src(_TUI_ROUTES_RS)
    assert "route store {}" in routes, "the routes screen must render the authority line"


# --------------------------------------------------------------------------
# 6. THE PARITY GAPS THAT WERE FOUND HERE -- all three now CLOSED
#
# Each was written first as a failing assertion against the shipped console and
# is kept as a regression guard: the gap it names cost the operator a working
# offline surface once, and the cheapest way to reopen it is to "tidy" one of
# these lanes away.
# --------------------------------------------------------------------------


def test_web_route_modal_offers_a_free_text_model_lane_offline() -> None:
    """CLOSED (was P0): the operator's literal complaint — configurable offline
    in the TUI, not in the web console. The model <select> can only offer
    *discovered* values, so with no reachable provider it was a disabled control
    and saveDefault() refused. The console-TUI degrades to a free-text lane
    (routes.rs:1163-1213) and stays savable; the web console now does too."""
    html = gateway_console_html()
    js = _console_js()
    assert 'id="modal-default-model-custom"' in html, "no free-text model lane in the route modal"

    # The lane opens on BOTH offline shapes: a failed probe and an empty catalog.
    loader = _fn_body(js, "loadDefaultModels")
    assert 'setCustomLane("modal-default-model-custom", true, selected)' in loader, (
        "lane never opens on discovery failure"
    )
    assert 'setCustomLane("modal-default-model-custom", !models.length, selected)' in loader, (
        "lane never opens on an empty catalog"
    )
    # ...and stays shut when the catalog is healthy, so a good catalog still
    # steers the operator to real values instead of inviting typos.
    assert 'setCustomLane("modal-default-model-custom", false)' in loader

    # The typed value must be READ, else the lane is decoration -- and read by
    # the same accessor the loaders use, else it is a field that accepts typing
    # and changes nothing until Save (which then refuses for want of a model).
    assert 'customLaneValue("modal-default-model-custom")' in _fn_body(js, "activeDefaultModel")
    assert "activeDefaultModel()" in _fn_body(js, "saveDefault")
    assert "Select a discovered provider and model before saving." not in js, (
        "the old refusal is still reachable — offline saves would still be blocked"
    )


def test_web_route_modal_can_edit_base_url_and_options() -> None:
    """CLOSED (was P1): the console-TUI's route editor has always carried a
    'base URL' field and an 'options (JSON)' field and sent both on every save
    (ui/routes.rs:1517-1522); the web console had neither and could only merge a
    picked voice into options. The API accepted both all along
    (routes/gateway.py:23464-23467) -- it was a surface gap, not an API limit.
    Offline, base_url is how a route is pointed at a local inference server on a
    non-default port."""
    html = gateway_console_html()
    js = _console_js()
    assert 'id="modal-default-base-url"' in html
    assert 'id="modal-default-options"' in html

    # Editable on screen but discarded on save would be worse than absent, so
    # an EDIT must travel -- including an emptying edit, which is how an
    # override is CLEARED. It is the untouched field that must stay unnamed:
    # the modal prefills from the last grid render and never re-reads the row,
    # so echoing one back rolls a newer `abstractcore config` value away.
    save = _fn_body(js, "saveDefault")
    assert "if (baseUrlText !== String(prefill.base_url" in save
    assert "if (optionsEdited) body.options = options;" in save
    assert "state.defaultModalPrefill" in _fn_body(js, "openDefaultModal"), (
        "nothing records what the operator was shown, so no edit can be detected"
    )
    # A typo must not be swallowed into a silently-successful save.
    assert "Options is not valid JSON" in js


def test_web_console_renders_every_capability_route_the_tui_does() -> None:
    """CLOSED (was P2): visibleCapabilityDefaultRow() filtered every scene3d
    route out of the web grid -- 24 rows in the TUI vs 20 in the web console off
    the SAME payload -- so a scene3d route written by the TUI could be neither
    seen nor cleared from here. It was hidden because the modal could not
    configure it (no scene3d discovery endpoint); the free-text provider and
    model lanes make "nothing to discover" a supported state instead."""
    js = _console_js()
    body = _fn_body(js, "visibleCapabilityDefaultRow")
    # Assert the FILTER, not the prose: the explanation of why scene3d is no
    # longer excluded naturally mentions scene3d.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )
    assert "scene3d" not in code, "scene3d rows are still filtered out of the web grid"
    assert "return true;" in code
    # The lanes that make an undiscoverable row configurable must both exist.
    assert 'id="modal-default-provider-custom"' in gateway_console_html()
    assert 'id="modal-default-model-custom"' in gateway_console_html()

# --------------------------------------------------------------------------
# 7. Live API-level parity (skipped unless a gateway is reachable)
# --------------------------------------------------------------------------

_GW = os.environ.get("PARITY_GATEWAY_URL", "http://127.0.0.1:8080")
_TOKEN = os.environ.get("PARITY_GATEWAY_TOKEN", "")


def _live(path: str, timeout: float = 20.0):
    req = urllib.request.Request(_GW + path, method="GET")
    req.add_header("Accept", "application/json")
    if _TOKEN:
        req.add_header("Authorization", "Bearer " + _TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A GATEWAY THAT ANSWERS IS A REACHABLE GATEWAY. HTTPError subclasses
        # URLError, so folding it into the skip below reported "no reachable
        # gateway" for a 401 -- every assertion in this section skipped itself
        # on the one machine where it could have run, and the whole live block
        # was unfalsifiable. Refusal and absence are different sentences.
        if not _TOKEN:
            pytest.skip(
                f"{_GW} answered HTTP {exc.code}: set PARITY_GATEWAY_TOKEN to run the live checks"
            )
        raise AssertionError(
            f"{_GW}{path} answered HTTP {exc.code} with PARITY_GATEWAY_TOKEN set -- "
            f"the token is rejected or lacks the scope these payloads need: {exc}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        pytest.skip(f"no reachable gateway at {_GW}: {exc}")


@pytest.mark.parametrize(
    "path",
    ["/api/gateway/config/capability-defaults", "/api/gateway/config/provider-endpoint-profiles"],
)
def test_live_config_payloads_name_the_one_store(path: str) -> None:
    """Both surfaces read these payloads; both must be told the same authority."""
    payload = _live(path)
    for field in ("config_file", "authority", "writable"):
        assert field in payload, f"{path} must carry {field}"
    assert str(payload["config_file"]).endswith("abstractcore.json")
    assert payload["authority"] == "abstractcore.local"


def test_live_unreachable_provider_discovery_is_bounded_and_honest() -> None:
    """Offline discovery must answer, not hang, and must not fake an empty catalog.

    A provider profile whose base_url cannot be reached must come back as a
    bounded 200 with an empty ``models`` list -- the shape both surfaces
    already know how to render.
    """
    profiles = _live("/api/gateway/config/provider-endpoint-profiles")
    unreachable = None
    for prof in profiles.get("profiles", []):
        base = str(prof.get("base_url") or "")
        if base.startswith("http://x/") or "203.0.113." in base or ":1/v1" in base:
            unreachable = prof
            break
    if unreachable is None:
        pytest.skip("no deliberately-unreachable endpoint profile configured")
    virtual = unreachable.get("virtual_provider") or f"endpoint:{unreachable['id']}"
    payload = _live(
        "/api/gateway/discovery/providers/"
        + urllib.parse.quote(str(virtual), safe="")
        + "/models?capability_route=output.text",
        timeout=90.0,
    )
    assert payload.get("models") == []
    assert payload.get("available") is False
    assert payload.get("route_available") is True, (
        "an unreachable endpoint is still a CONFIGURED route -- the surfaces "
        "must be able to tell 'cannot reach it' from 'not configured'"
    )

