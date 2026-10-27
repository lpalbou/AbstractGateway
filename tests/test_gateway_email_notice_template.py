"""Notification emails a person can read (framework backlog 1028, the template half): one
branded layout for every kind with a plain-text twin, subjects in plain language (never an id),
the wait's facts (the tool's label and arguments, the question and its choices), the time in
the recipient's zone, buttons to the run's page in the console and to the session in
AbstractCode, and ONE email per wait however often the same approval is re-issued."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, connect_body, plane_of, smtp_bodies

pytestmark = pytest.mark.integration

PUBLIC = "https://gateway.example.org"
RUN_ID = "7f3c2a10-1111-4222-8333-944455556666"


def _public_origin(monkeypatch) -> None:
    from abstractgateway import first_run, network_exposure

    monkeypatch.setattr(first_run, "read_serve_record", lambda _: {"console_url": "http://127.0.0.1:8081/console"})
    monkeypatch.setattr(network_exposure, "proxy_env_facts", lambda: {"origins_env": None})
    monkeypatch.setattr(network_exposure, "live_reverse_proxy", lambda: SimpleNamespace(extra_origins=(PUBLIC,), trust_proxy=False))


def _facts(**extra):
    base = {
        "title": "Mail triage",
        "context": [["Workflow", "Mail triage"], ["Started by", "alice"]],
        "started_at": "2026-10-18T13:58:02+00:00",
        "event_at": "2026-10-18T14:05:31+00:00",
        "time_zone": "Europe/Paris",
        "run_id": RUN_ID,
        "session_id": "sess-42",
        "code_app": True,
        "ref": f"run {RUN_ID}",
    }
    base.update(extra)
    return base


def _hrefs(html_body: str) -> list:
    from html.parser import HTMLParser

    class Links(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list = []
            self.srcs: list = []

        def handle_starttag(self, tag, attrs):
            for k, v in attrs:
                if k == "href":
                    self.hrefs.append(v)
                if k == "src":
                    self.srcs.append(v)

    p = Links()
    p.feed(html_body)
    assert p.srcs == [], "an email must not load remote resources"
    return p.hrefs


def test_approval_email_names_the_tool_its_arguments_and_links_to_the_exact_run(monkeypatch) -> None:
    from abstractgateway.mail.notifications import render_notice

    _public_origin(monkeypatch)
    subject, text, html_body = render_notice("approval_needed", _facts(
        wait_kind="tool_approval",
        calls=[{"name": "fetch_url", "arguments": {"url": "https://example.org/invoices", "timeout": 30}}],
    ))
    assert subject == "[AbstractFramework] Mail triage needs your approval: Fetch a web page"
    assert RUN_ID not in subject
    console = f"{PUBLIC}/apps/observer/#run/{RUN_ID}"
    code = f"{PUBLIC}/apps/code/#session=sess-42"
    # The text twin: the facts, the time in the recipient's zone, both links as URLs, the id last.
    assert "Mail triage waits for your approval before running Fetch a web page." in text
    assert "Fetch a web page (fetch_url)" in text and "url: https://example.org/invoices" in text and "timeout: 30" in text
    assert "Started by: alice" in text
    assert "Started: 2026-10-18 13:58 UTC · 15:58 Europe/Paris" in text
    assert "Waiting since: 2026-10-18 14:05 UTC · 16:05 Europe/Paris" in text
    assert f"Open in the console: {console}" in text and f"Open in AbstractCode: {code}" in text
    assert "127.0.0.1" not in text and "/console\n" not in text
    assert text.rstrip().endswith(f"Reference: run {RUN_ID}")
    assert "Replying to this email does nothing." in text
    assert "“Approval needed” is on" in text
    # The HTML: branded, self-contained, explicit colours, the same links.
    hrefs = _hrefs(html_body)
    assert console in hrefs and code in hrefs
    assert hrefs.count(console) == 2 and hrefs.count(code) == 2  # the button and the plain URL
    assert 'bgcolor="#1a1a2e"' in html_body and 'bgcolor="#ffffff"' in html_body and "#e94560" in html_body
    assert "Abstract</span>" in html_body and "Framework</span>" in html_body
    assert "Open in the console</a>" in html_body and "Open in AbstractCode</a>" in html_body
    assert "Fetch a web page" in html_body and "https://example.org/invoices" in html_body
    assert "<h1 style=" in html_body and "Needs your answer" in html_body


def test_question_email_lists_the_question_and_its_choices(monkeypatch) -> None:
    from abstractgateway.mail.notifications import render_notice

    _public_origin(monkeypatch)
    subject, text, html_body = render_notice("approval_needed", _facts(
        title="Research assistant", wait_kind="ask_user",
        prompt="Which folder should I save the report to?", choices=["~/Documents/reports", "~/Desktop"],
    ))
    assert subject == "[AbstractFramework] Research assistant asked you a question"
    assert "Which folder should I save the report to?" in text
    assert "  1. ~/Documents/reports" in text and "  2. ~/Desktop" in text
    assert "Which folder should I save the report to?" in html_body
    assert "<li" in html_body and "~/Documents/reports</li>" in html_body
    assert f"{PUBLIC}/apps/observer/#run/{RUN_ID}" in _hrefs(html_body)


def test_result_and_failure_emails_in_plain_language(monkeypatch) -> None:
    from abstractgateway.mail.notifications import render_notice

    _public_origin(monkeypatch)
    subject, text, html_body = render_notice("job_failed", _facts(title="Nightly build", cause="The tool timed out.", fix="Open the run in the console to see what failed."))
    assert subject == "[AbstractFramework] Nightly build failed"
    assert "Cause: The tool timed out." in text and "Fix: Open the run in the console to see what failed." in text
    assert "The tool timed out." in html_body and "email me when done" in text
    subject, text, html_body = render_notice("job_finished", _facts(title="Nightly build"))
    assert subject == "[AbstractFramework] Nightly build finished" and "When: 2026-10-18 14:05 UTC" in text
    subject, text, html_body = render_notice("automation_result", _facts(
        title="Daily digest", context=[["Automation", "Daily digest"], ["Occurrence", "12"]], session_id="",
        model_title="3 invoices arrived", model_body="Two from ACME.",
    ))
    assert subject == "[AbstractFramework] Daily digest finished"
    assert "Occurrence: 12" in text and "model-authored" in text and "3 invoices arrived" in text
    assert "Open in AbstractCode" not in text and "Open in AbstractCode" not in html_body  # no session
    assert "Two from ACME." in html_body
    subject, text, _ = render_notice("automation_failed", _facts(title="Daily digest", cause="The model endpoint refused the request."))
    assert subject == "[AbstractFramework] Daily digest failed" and "not paused" in text


def test_code_button_only_for_a_session_the_code_app_can_show(monkeypatch) -> None:
    from abstractgateway.mail.notifications import render_notice

    _public_origin(monkeypatch)
    _, text, _ = render_notice("approval_needed", _facts(wait_kind="ask_user", prompt="Go on?", code_app=False))
    assert "Open in the console:" in text and "Open in AbstractCode" not in text
    _, text, _ = render_notice("approval_needed", _facts(wait_kind="ask_user", prompt="Go on?", session_id=""))
    assert "Open in AbstractCode" not in text


def test_links_never_come_from_an_untrusted_origin(monkeypatch) -> None:
    from abstractgateway import first_run, network_exposure
    from abstractgateway.mail.notifications import render_notice

    monkeypatch.setattr(first_run, "read_serve_record", lambda _: {"console_url": "http://127.0.0.1:8081/console"})
    monkeypatch.setattr(network_exposure, "proxy_env_facts", lambda: {"origins_env": None})
    monkeypatch.setattr(network_exposure, "live_reverse_proxy", lambda: SimpleNamespace(extra_origins=(), trust_proxy=False))
    _, text, html_body = render_notice("approval_needed", _facts(wait_kind="ask_user", prompt="Go on?", origin="https://evil.example"))
    assert "evil.example" not in text and "evil.example" not in html_body
    assert f"http://127.0.0.1:8081/apps/observer/#run/{RUN_ID}" in text  # the recorded console address, no public name


def test_when_sentence_carries_utc_and_the_recipient_zone() -> None:
    from abstractgateway.mail.notifications import when_sentence

    assert when_sentence("2026-10-18T14:05:31+00:00", "Europe/Paris") == "2026-10-18 14:05 UTC · 16:05 Europe/Paris"
    assert when_sentence("2026-10-18T23:30:00Z", "Asia/Tokyo") == "2026-10-18 23:30 UTC · 2026-10-19 08:30 Asia/Tokyo"
    assert when_sentence("2026-10-18T14:05:31+00:00", "UTC") == "2026-10-18 14:05 UTC"
    assert when_sentence("2026-10-18T14:05:31+00:00", "Not/AZone") == "2026-10-18 14:05 UTC"
    assert when_sentence("", "Europe/Paris") == ""


def test_recipient_time_zone_is_the_saved_preference(gateway, monkeypatch) -> None:
    from abstractgateway.mail.notifications import recipient_time_zone

    from abstractgateway.account_preferences import write_preferences
    from abstractgateway.users import gateway_data_dir_from_env

    plane = plane_of("alice")
    before = recipient_time_zone(plane)
    assert before and before != "Pacific/Auckland"  # the host's zone until the account saves one
    write_preferences(gateway_data_dir_from_env(), tenant_id=plane.tenant_id, user_id=plane.user_id,
                      changes={"time_zone": "Pacific/Auckland"}, validate=lambda iface, value: None, actor="test")
    assert recipient_time_zone(plane) == "Pacific/Auckland"


def test_tool_labels_read_as_plain_language() -> None:
    from abstractgateway.mail.notifications import tool_label

    assert tool_label("fetch_url") == "Fetch a web page"
    assert tool_label("send_email") == "Send an email"
    assert tool_label("make_coffee") == "Make coffee"
    assert tool_label("") == "a tool"


def test_display_name_prefers_the_run_a_person_started() -> None:
    from abstractruntime.core.models import RunState, RunStatus

    from abstractgateway.mail.notifications import _run_context, _run_title

    root = RunState(run_id="root", workflow_id="visual_react_agent_basic", status=RunStatus.WAITING, current_node="n",
                    vars={"_meta": {"title": "Mail triage"}}, actor_id="person:alice", created_at="2026-10-18T13:58:02+00:00")
    child = RunState(run_id="child", workflow_id="visual_react_agent_basic-agent_0_0_5_81795ea9_node-2", status=RunStatus.WAITING,
                     current_node="n", vars={}, parent_run_id="root")
    store = SimpleNamespace(load=lambda rid: {"root": root, "child": child}.get(rid))
    assert _run_title(child, store) == "Mail triage"
    assert _run_context(child, store) == [["Workflow", "Mail triage"], ["Started by", "alice"]]
    assert _run_title(child, None) == "visual_react_agent_basic-agent_0_0_5_81795ea9_node-2"  # no store: the run's own id


def test_wait_identity_ignores_the_runtime_issuance_counter() -> None:
    from abstractgateway.mail.notifications import wait_identity

    calls = [{"name": "fetch_url", "arguments": {"url": "https://example.org"}}]
    first = {"run_id": "occ-9", "wait_key": "tool_approval:occ-9:node-2:issuance-1", "kind": "tool_approval", "details": calls}
    retry = {**first, "wait_key": "tool_approval:occ-9:node-2:issuance-2"}
    other_tool = {**first, "details": [{"name": "send_email", "arguments": {"to": "bob@example.test"}}]}
    other_node = {**first, "wait_key": "tool_approval:occ-9:node-3:issuance-1"}
    assert wait_identity(first) == wait_identity(retry)
    assert wait_identity(first) != wait_identity(other_tool) and wait_identity(first) != wait_identity(other_node)
    question = {"run_id": "r", "wait_key": "user:r:ask", "kind": "ask_user", "prompt": "Which folder?", "choices": ["a", "b"]}
    assert wait_identity(question) == wait_identity(dict(question))
    assert wait_identity(question) != wait_identity({**question, "prompt": "Which file?"})


def _connect(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text


def _fake_svc(automations):
    run_store = SimpleNamespace(list_runs=lambda status=None, wait_reason=None, limit=100: [])
    return SimpleNamespace(host=SimpleNamespace(run_store=run_store, ledger_store=object(), runtime=None)), {"automations": list(automations), "waits": {}}


def _patch_runtime_sources(monkeypatch, data):
    import abstractruntime.automation_queries as aq
    import abstractruntime.automations.attention as att

    monkeypatch.setattr(aq, "list_automations", lambda run_store, status=None, cursor=None, limit=50: SimpleNamespace(items=list(data["automations"]), next_cursor=None))
    monkeypatch.setattr(att, "list_attention", lambda ledger_store, aid, *, after_seq=0, cursor=None, limit=50: {"items": [], "next_cursor": None})
    monkeypatch.setattr(att, "pending_waits", lambda run_store, aid, limit=20: list(data["waits"].get(aid, [])))


def test_one_email_per_wait_however_often_the_node_is_retried(gateway, imap, smtp, monkeypatch) -> None:
    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    svc, data = _fake_svc([{"automation_id": "auto-1", "title": "Mail triage", "status": "active"}])
    _patch_runtime_sources(monkeypatch, data)
    NotificationCollector(plane, svc).collect()  # baseline
    calls = [{"name": "fetch_url", "arguments": {"url": "https://example.org/invoices"}}]
    data["waits"]["auto-1"] = [{"run_id": "occ-9", "wait_key": "tool_approval:occ-9:node-2:issuance-1", "kind": "tool_approval", "details": calls, "index": 3}]
    assert NotificationCollector(plane, svc).collect()["queued"] == 1
    # The same approval re-issued at the same node (a new runtime wait key each time): nothing new.
    for n in range(2, 9):
        data["waits"]["auto-1"] = [{"run_id": "occ-9", "wait_key": f"tool_approval:occ-9:node-2:issuance-{n}", "kind": "tool_approval", "details": calls, "index": 3}]
        assert NotificationCollector(plane, svc).collect()["queued"] == 0
    # A different wait (another tool batch at that node): one more email.
    data["waits"]["auto-1"] = [{"run_id": "occ-9", "wait_key": "tool_approval:occ-9:node-2:issuance-9", "kind": "tool_approval",
                               "details": [{"name": "send_email", "arguments": {"to": "bob@example.test", "subject": "Invoices"}}], "index": 3}]
    assert NotificationCollector(plane, svc).collect()["queued"] == 1
    NotificationOutbox(plane).deliver()
    mails = sorted(smtp_bodies(smtp), key=lambda m: m["subject"])
    assert [m["subject"] for m in mails] == [
        "[AbstractFramework] Mail triage needs your approval: Fetch a web page",
        "[AbstractFramework] Mail triage needs your approval: Send an email",
    ]
    assert "url: https://example.org/invoices" in mails[0]["text"] and "Occurrence: 3" in mails[0]["text"]
    assert "to: bob@example.test" in mails[1]["text"] and "subject: Invoices" in mails[1]["text"]
    assert all("Reference: automation auto-1, run occ-9" in m["text"] for m in mails)
