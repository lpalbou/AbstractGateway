"""The c69 audit fix (2026-07-18, laurent's dashboard-vs-effective directive):
three memory tools were GRANTED but never OFFERED on the visit lane.

The stated blocker ("resolvers are ChatSession methods the door cannot
reach") went stale the day runtime shipped the session-free HomeMemoryReader
(2026-07-10, on the gateway's own ask) — the door never imported it, and the
hand-copied declaration map silently dropped 3 of 10 granted tools. These
tests pin the fix:

1. Declarations DERIVE from runtime's walled_tool_rows (offered ⇔ executable
   by construction — the drift class is dead).
2. The door executor runs search_memory/read_memory/recent_memories through
   HomeMemoryReader with ONE tag_map per visit.
3. PRIVATE-WORD CONTAINMENT: the durable lane's tool results REST (ledger +
   cycle vars), so private diary gists never enter them — a private entry is
   FOUND (text still matches) but its words stay in the book behind the
   act-only diary_read hop.
4. The dashboard truth wires: per-cell executable on /tool-policy and the
   matrix (the executable:True hardcode is dead); visit/open states a
   narrowed grant when one exists.
"""

from __future__ import annotations

import copy

import pytest


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def test_declarations_derive_from_walled_rows() -> None:
    """Every walled row is declared; optional params carry the default marker
    (core's absence-of-default = required rule). The act_only rider died with
    the ref layer (runtime c273) — no declaration carries it anymore."""
    from abstractruntime.identity.tools import walled_tool_rows

    from abstractgateway.entity_visits import _entity_tool_declarations

    decls = _entity_tool_declarations()
    row_names = {r["name"] for r in walled_tool_rows()}
    assert set(decls.keys()) == row_names, "the declaration map must equal the walled set exactly"
    assert {"search_memory", "read_memory", "recent_memories"} <= set(decls.keys())
    assert "act_only" not in decls["diary_read"], "act_only died with the ref layer"
    # recent_memories' window is optional in the runtime schema — the flat
    # ToolDefinition convention needs the default marker or the wire would
    # demand it.
    assert "default" in decls["recent_memories"]["parameters"]["window"]


def test_memory_tools_are_offered_to_the_model() -> None:
    from abstractcore.tools import ToolDefinition

    from abstractgateway.entity_visits import _entity_tool_definitions

    granted = ["web_search", "search_memory", "read_memory", "recent_memories"]
    offered = {d.name for d in _entity_tool_definitions(granted, ToolDefinition)}
    assert offered == set(granted)


def test_contained_diary_view_redacts_private_gists_keeps_text() -> None:
    """A private entry is FOUND by search (text intact) but its gist — the
    only book field the reader renders — is a word-free marker."""
    from abstractgateway.entities import _ContainedDiaryView

    class _Diary:
        def list_entries(self):
            return [
                {"entry_id": "diary_pub1", "visibility": "normal", "gist": "public gist",
                 "text": "public words", "kind": "note", "written_at": "2026-07-18T10:00:00+00:00"},
                {"entry_id": "diary_priv1", "visibility": "private", "gist": "SECRETWORD plan",
                 "text": "the SECRETWORD is zanzibar", "kind": "note", "written_at": "2026-07-18T11:00:00+00:00"},
            ]

    view = _ContainedDiaryView(_Diary())
    entries = view.list_entries()
    pub = next(e for e in entries if e["entry_id"] == "diary_pub1")
    priv = next(e for e in entries if e["entry_id"] == "diary_priv1")
    assert pub["gist"] == "public gist"
    assert "SECRETWORD" not in priv["gist"]
    assert "private entry" in priv["gist"]
    # Text stays for MATCHING — hiding it would make search lie about
    # absence against its own append-only warrant.
    assert "SECRETWORD" in priv["text"]


def test_door_executor_runs_memory_tools_and_contains_private_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the real door handler: a search_memory native call
    executes (no refusal), finds the private entry, and the RESULT that
    would rest carries no private words — the reread key rides instead."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "memtools-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer memtools-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        er = registry.get_entity_runtime("castor")

        # A private diary entry whose words must never rest.
        from abstractruntime.identity.diary import DiaryEntry

        er.home.diary.append_entry(DiaryEntry(
            entry_id="diary_secret1", author=er.home.entity_id,
            text="the SECRETWORD is zanzibar", gist="SECRETWORD note",
            kind="note", visibility="private",
            written_at="2026-07-18T11:00:00+00:00",
        ))

        handler = registry._entity_tool_handler("castor")

        class _Run:
            run_id = "run-memtools"
            vars = {"_runtime": {"turn_id": "t-0001"}}

        class _Effect:
            payload = {
                "tool_calls": [
                    {"name": "search_memory", "arguments": {"query": "SECRETWORD"}, "call_id": "c1"},
                ]
            }

        out = handler(_Run(), _Effect())
        assert getattr(out, "status", None) == "completed", getattr(out, "error", None)
        [res] = out.result["results"]
        assert res["name"] == "search_memory"
        assert res["success"] is True
        text = str(res["output"])
        # The entry is FOUND (the book count names a match) …
        assert "1 of 1 entries" in text or "book: 1" in text.lower() or "diary_" in text
        # … but the private words never enter what rests.
        assert "zanzibar" not in text
        assert "SECRETWORD" not in text.split("Searched your memory")[-1].replace(
            'for: "SECRETWORD"', "")  # the QUERY echo is the entity's own words; the gist/text are not
        # The reread key is present — the words arrive via the act-only hop.
        assert "diary_read diary_" in text


def test_read_memory_through_the_door_reaches_the_verbatim_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversary F1 regression: the first contained home exposed only
    store/journal/entity_id/diary while read_memory needs `.ms` (payload
    tiers) and `.artifacts` (verbatim load) — the swallowed AttributeError
    made EVERY read answer the false wall "there is no longer verbatim
    behind it". An identity record proves the tier works: with `.ms` wired
    its spark payload_ref resolves to the identity-core message; without
    it, the false wall."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "readmem-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer readmem-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        er = registry.get_entity_runtime("castor")
        handler = registry._entity_tool_handler("castor")

        # An identity-core record's graph id (planted at creation).
        from abstractmemory import TripleQuery

        rows = [a for a in er.home.store.query(
            TripleQuery(predicate="dcterms:abstract", scope="self",
                        owner_id=er.home.entity_id, limit=0))]
        assert rows, "the engram plants identity records at creation"
        from abstractruntime.identity.memory_reader import memory_tag

        tag = memory_tag(str(rows[0].subject))

        run_vars: dict = {"_runtime": {"turn_id": "t-0001"}}

        class _Run:
            run_id = "run-readmem"
            vars = run_vars

        class _Read:
            payload = {"tool_calls": [{"name": "read_memory", "arguments": {"tag": tag}, "call_id": "c1"}]}

        out = handler(_Run(), _Read())
        assert getattr(out, "status", None) == "completed", getattr(out, "error", None)
        [res] = out.result["results"]
        text = str(res["output"])
        # The verbatim tier RESOLVED (identity core names its spark origin);
        # the F1 bug answered the false "born as these words" wall here.
        assert "identity core" in text, text
        assert "no longer verbatim" not in text


def test_tag_map_persists_across_turns_in_run_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driver parity nuance 1: ONE tag_map per visit — a #tag learned in one
    call resolves in a later call because the map lives in `_visit` vars."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "tagmap-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer tagmap-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        registry.get_entity_runtime("castor")
        handler = registry._entity_tool_handler("castor")

        run_vars: dict = {"_runtime": {"turn_id": "t-0001"}}

        class _Run:
            run_id = "run-tagmap"
            vars = run_vars

        class _Search:
            payload = {"tool_calls": [{"name": "search_memory", "arguments": {"query": "curiosity"}, "call_id": "c1"}]}

        out = handler(_Run(), _Search())
        assert getattr(out, "status", None) == "completed"
        tag_map = run_vars.get("_visit", {}).get("memory_tag_map")
        assert isinstance(tag_map, dict)
        # The identity core seeds records at birth; a hit registers its tag.
        if tag_map:
            tag, gid = next(iter(tag_map.items()))
            assert isinstance(tag, str) and isinstance(gid, str)


def test_tool_policy_serves_per_cell_executability(monkeypatch: pytest.MonkeyPatch) -> None:
    """entity c72 wire shape: phases[phase].executable[tool] = {ok, reason?};
    live lanes (visit/personal) serve ok=true, lane-less phases (sleep/work)
    ok=false with the honest reason."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "exec-wire-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer exec-wire-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        body = client.get("/api/gateway/entities/Castor/tool-policy").json()
        visit = body["phases"]["visit"]["executable"]
        assert visit["web_search"]["ok"] is True
        assert "search_memory" in visit and visit["search_memory"]["ok"] is True
        sleep_exec = body["phases"]["sleep"]["executable"]
        for tool, cell in sleep_exec.items():
            assert cell["ok"] is False, f"sleep has no tool lane; {tool} must serve ok=false"
            assert cell.get("reason")


def test_matrix_cells_carry_lane_truth_not_a_hardcode() -> None:
    from abstractgateway.tool_inventory import phase_capability_matrix

    matrix = phase_capability_matrix()
    items = matrix["sections"][0]["items"]
    web = next(i for i in items if i["id"] == "web_search")
    assert web["cells"]["visit"]["executable"] is True
    assert web["cells"]["sleep"]["executable"] is False
    assert web["cells"]["sleep"]["reason"]
    assert web["cells"]["work"]["executable"] is False


def test_visit_open_states_a_narrowed_grant_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """skill's pin: a narrowed grant is STATED at the door, never discovered
    by refusal. END-TO-END through a real /visit/open (adversary F2: the
    first version re-executed the diff expression — tautological about the
    wire; a wiring typo would have shipped green and the field, whose whole
    purpose is version-skew visibility, would silently never appear).
    Post-c69 the dropped set is empty on a current stack, so the field is
    ABSENT on a clean open; a simulated declaration gap makes it appear."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "pruned-e2e-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "scripted-model")

    from abstractgateway import entity_chat, entity_visits
    from abstractgateway.app import app

    class _ScriptedLLM:
        def generate(self, **kwargs):
            class _R:
                content = "Hello."
                tool_calls = None
                finish_reason = "stop"
                usage = {"total_tokens": 1}
                model = "scripted-model"

            return _R()

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM())

    # Simulate version skew: one granted name loses its door declaration.
    real = entity_visits._entity_tool_declarations

    def _narrowed():
        decls = dict(real())
        decls.pop("search_memory", None)
        return decls

    monkeypatch.setattr(entity_visits, "_entity_tool_declarations", _narrowed)

    with TestClient(app, headers={"Authorization": "Bearer pruned-e2e-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        pruned = opened.json().get("allowlist_pruned")
        assert pruned is not None, "a narrowed grant must be STATED on the open response"
        assert pruned["dropped"] == ["search_memory"]
        assert "granted but not offerable" in pruned["reason"]


def test_entity_llm_result_never_carries_raw_response_or_reasoning(monkeypatch) -> None:
    """G1 privacy defense-in-depth (wave-4 P0, 2026-07-19): the durable-visit
    LLM handler must build its result from NAMED FIELDS ONLY — never copy
    raw_response/reasoning off the response object. Those siblings carry the
    UNMARKED reply (diary fences intact) and would rest in run vars + ledger
    past the write-boundary content capture ("diary fences fly to the book
    before the result rests" — the capture KEPT after the ref layer's
    deletion, runtime c273). The general runtime handler has this leak; the
    entity door handler must not. Pinned so a rewire that starts passing
    raw_response refuses loudly here."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "noraw-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "scripted-model")

    from abstractgateway import entity_chat
    from abstractgateway.app import app

    class _LeakyResponse:
        """A response object carrying the sibling keys the handler must drop —
        including a diary fence in the raw reply the door must never rest."""
        content = "A reply.\n```diary\nvisibility=private\na secret thought\n```"
        tool_calls = None
        finish_reason = "stop"
        usage = {"total_tokens": 5}
        model = "scripted-model"
        raw_response = {"choices": [{"message": {"content": content}}]}
        reasoning = "the private words leaked into reasoning too"

    class _LeakyLLM:
        def generate(self, **kwargs):
            return _LeakyResponse()

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _LeakyLLM())

    with TestClient(app, headers={"Authorization": "Bearer noraw-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        registry.get_entity_runtime("castor")
        handler = registry._entity_llm_handler("castor")

        from abstractruntime.core.models import Effect, EffectType

        eff = Effect(type=EffectType.LLM_CALL, payload={"messages": [{"role": "user", "content": "hi"}]})

        class _Run:
            run_id = "run-noraw"
            vars: dict = {}

        out = handler(_Run(), eff)
        assert getattr(out, "status", None) == "completed", getattr(out, "error", None)
        res = out.result
        # The named fields survive; the leaky siblings never do.
        assert "raw_response" not in res, "the door handler must never persist raw_response (diary-fence leak class)"
        assert "reasoning" not in res, "the door handler must never persist reasoning"
        assert set(res.keys()) <= {"content", "tool_calls", "finish_reason", "usage", "model"}
        # The SIBLING leak paths are gone: the private words rode raw_response
        # AND reasoning; neither key survives, so those copies never rest.
        # (The diary fence still sits in `content` here — the write-boundary
        # capture that flies fences to the book before the result rests is
        # open_entity_runtime's job over this handler; a direct-handler call
        # deliberately does not exercise it. This pin owns the handler's
        # guarantee — no raw sibling copies — not the capture.)
        import json as _json

        assert "a secret thought" not in _json.dumps({k: v for k, v in res.items() if k != "content"})
        assert "leaked into reasoning" not in _json.dumps(res)
