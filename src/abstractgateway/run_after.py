"""What happened after a run (framework ADR-0041 "No hidden effects after a flow").

`GET /api/gateway/runs/{run_id}/after` answers, for one run of the caller's plane:

    {run_id,
     trigger: {automation_id, automation_title, source, text, connection, fired_at,
               arrived: {count, noun, line}} | null,
     run: {started, ended, status, response_excerpt, note},
     tree: [run ids looked at: the run and its sub-runs],
     attention: {record_key, kind, title, raised_at, from, run_id?, prompt_ref?, open?,
                 recipient?, asked?} | null,
     alerts: [ the same shape, every alert the run raised ],
     deliveries: [{record_key, channel, via, device?, device_name?, time, state, reason?}],
     quiet_reason?: "empty title" | "no notify" | "still running" | "not recorded yet"}

Built from records only, never a stream scan:

- the automation's own decision of the occurrence (`automation.completed`, read by its exact
  ledger key `automation:completed:<automation_id>:<index>` = the attention item's record key);
- the alerts the run and its sub-runs (parent_run_id links) published (Notify me, Publish to
  channel with Alert on), found by their deterministic ids
  `attention.raised:publish:<run_id>:<step_id>` in the stream index;
- the waits that parked the run or a sub-run on the person (an approval, a question),
  `attention.raised:<run_id>:<wait_key>:<step_id>`, `open` until their `attention.seen`;
- each delivery's `notification.sent|failed` event, by its deterministic id
  (`notification.<outcome>:<channel>:attention:<record_key>`, email: the outbox key).

`via`: `push` (the gateway's push dispatcher), `outbox` (email), `stream` (an app read the
person's stream and showed it; the console lists the item).

A published alert (Notify me) says whom it was for and where it asked to go: `recipient` ("me")
and `asked`, the channels it chose (`["watch", "phone"]`) or `"all"` (none chosen: every channel),
so a reader sees "asked: watch, phone" beside what was delivered. Its email, when asked, is found
by its outbox key (`notify_me_key(record_key)`).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

TERMINAL = frozenset({"completed", "failed", "cancelled"})
RESPONSE_EXCERPT_MAX = 280
NOTE_MAX = 500
LINE_MAX = 160
# Sub-runs looked at under one run (an agent spawns a few; a runaway tree is cut here).
TREE_MAX = 200


def _status(run: Any) -> str:
    s = getattr(run, "status", None)
    return str(getattr(s, "value", s) or "").strip().lower()


def _meta(run: Any) -> Dict[str, Any]:
    v = getattr(run, "vars", None)
    m = v.get("_meta") if isinstance(v, dict) else None
    return m if isinstance(m, dict) else {}


def _vars(run: Any) -> Dict[str, Any]:
    v = getattr(run, "vars", None)
    return v if isinstance(v, dict) else {}


def _clip(text: Any, limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    if not t:
        return None
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def end_payload(run: Any) -> Dict[str, Any]:
    """The object On Flow End wrote (`response`, `success`, `note`, `notify`...): the run's output,
    or its `result` when the output wraps it (`{success, result}`), the runtime's own rule."""
    out = getattr(run, "output", None)
    out = out if isinstance(out, dict) else {}
    result = out.get("result")
    if isinstance(result, dict) and not any(k in out for k in ("response", "answer", "notify", "note")):
        return result
    return out


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def arrived_summary(source_id: str, run: Any) -> Optional[Dict[str, Any]]:
    """`{count, noun, line}` of what started an occurrence: the typed pins the runtime filled
    (`messages`, `items`, `calls`), else the trigger envelope's payload. None for a schedule or
    a run started by hand (nothing arrived)."""
    v = _vars(run)
    occ = _meta(run).get("occurrence") if isinstance(_meta(run).get("occurrence"), dict) else {}
    env = occ.get("trigger_envelope") if isinstance(occ.get("trigger_envelope"), dict) else {}
    payload = env.get("payload") if isinstance(env.get("payload"), dict) else {}
    source = str(source_id or "")

    def _list(name: str) -> List[Dict[str, Any]]:
        got = v.get(name)
        if not isinstance(got, list):
            got = payload.get(name)
        return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []

    def _count(rows: List[Any]) -> int:
        c = payload.get("count")
        return int(c) if isinstance(c, int) and not isinstance(c, bool) and c >= len(rows) else len(rows)

    if source.startswith("email"):
        rows = _list("messages")
        n = _count(rows)
        first = rows[0] if rows else {}
        subject = str(first.get("subject") or "").strip()
        sender = str(first.get("from_address") or first.get("from") or "").strip()
        line = None
        if subject or sender:
            line = (f"“{subject}”" if subject else "(no subject)") + (f" from {sender}" if sender else "")
            if n > 1:
                line += f" and {n - 1} more"
        return {"count": n, "noun": _plural(n, "new email", "new emails"), "line": _clip(line, LINE_MAX)}
    if source.startswith("feed"):
        rows = _list("items")
        n = _count(rows)
        title = str((rows[0] if rows else {}).get("title") or "").strip()
        line = (f"“{title}”" + (f" and {n - 1} more" if n > 1 else "")) if title else None
        return {"count": n, "noun": _plural(n, "new item", "new items"), "line": _clip(line, LINE_MAX)}
    if source.startswith("webhook"):
        rows = _list("calls")
        n = _count(rows)
        return {"count": n, "noun": _plural(n, "call", "calls"), "line": None}
    return None


def _alert_from_completion(completion: Mapping[str, Any], raised: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    att = completion["attention"]
    return {
        "record_key": completion["record_key"],
        "kind": att.get("kind"),
        "title": att.get("title"),
        "raised_at": (raised or {}).get("time") or completion.get("finished_at"),
        # A notify item comes from On Flow End's `notify` (the compatibility path); a failure
        # item from the automation itself.
        "from": "failure" if att.get("kind") == "failure" else "flow_end",
    }


def _alert_from_published(event: Mapping[str, Any]) -> Dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    key = str(data.get("record_key") or "")
    return {
        "record_key": key,
        "kind": data.get("kind") or "notify",
        "title": data.get("title"),
        "raised_at": event.get("time"),
        "from": "notify_me" if data.get("channel") == "me" else "publish",
        "step_id": key.rsplit(":", 1)[-1] if key.count(":") >= 2 else None,
        "recipient": str(data.get("recipient") or "me"),
        "asked": [str(c) for c in data["channels"]] if isinstance(data.get("channels"), list) else "all",
    }


def _alert_from_wait(event: Mapping[str, Any], index: Mapping[str, int]) -> Dict[str, Any]:
    """A run parked on the person (an approval, a question: `record_wait_raised`); `open` until its
    `attention.seen` (the wait was answered, cancelled or timed out) is in the stream."""
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    rid, wait_key, step_id = data.get("run_id"), data.get("wait_key"), data.get("step_id")
    return {
        "record_key": str(data.get("record_key") or ""),
        "kind": data.get("kind"),
        "title": None,
        "raised_at": event.get("time"),
        "from": "wait",
        "run_id": rid,
        "step_id": step_id,
        "prompt_ref": data.get("prompt_ref"),
        "open": f"attention.seen:{rid}:{wait_key}:{step_id}" not in index,
    }


def run_tree(run: Any, run_store: Any, *, limit: int = TREE_MAX) -> List[Any]:
    """The run and its sub-runs (parent_run_id links, the store's children index), breadth first,
    at most `limit` runs."""
    out = [run]
    queue = [str(getattr(run, "run_id", "") or "")]
    seen = set(queue)
    lister = getattr(run_store, "list_children", None)
    while queue and lister is not None and len(out) < limit:
        parent = queue.pop(0)
        try:
            children = lister(parent_run_id=parent) or []
        except Exception:  # noqa: BLE001 - a store without the index answers for the run alone
            children = []
        for child in children:
            cid = str(getattr(child, "run_id", "") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append(child)
            queue.append(cid)
            if len(out) >= limit:
                break
    return out


def _delivery(event: Mapping[str, Any], device_name: Optional[Callable[[str], Optional[str]]]) -> Dict[str, Any]:
    from .events_stream import delivery_route

    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    out: Dict[str, Any] = {
        "record_key": data.get("record_key") or None,
        "channel": data.get("channel"),
        "via": delivery_route(event),
        "time": event.get("time"),
        "state": "failed" if str(event.get("type")) == "notification.failed" else "sent",
    }
    device = data.get("device")
    if isinstance(device, str) and device:
        out["device"] = device
        name = device_name(device) if device_name is not None else None
        if name:
            out["device_name"] = name
    reason = data.get("error") or data.get("cause")
    if out["state"] == "failed" and reason:
        out["reason"] = str(reason)
    return out


def build_after(
    run: Any,
    *,
    run_store: Any,
    ledger_store: Any,
    stream: Any,
    trigger_words: Optional[Callable[[Mapping[str, Any]], Tuple[str, Optional[str]]]] = None,
    device_name: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """The "after the run" record of `run` (see the module docstring). `trigger_words(trigger)`
    gives `(sentence, connection name)` for the automation's trigger; `device_name(id)` the name
    a push device registered with."""
    from .automation_attention import email_notice_keys, occurrence_completion
    from .events_stream import notice_event_ids, published_alert_ids

    rid = str(getattr(run, "run_id", "") or "")
    status = _status(run)
    meta = _meta(run)
    occ = meta.get("occurrence") if isinstance(meta.get("occurrence"), dict) else None
    aid = str((occ or {}).get("automation_id") or "") or None
    index = (occ or {}).get("occurrence_index")
    end = end_payload(run)

    trigger: Optional[Dict[str, Any]] = None
    if aid:
        controller = run_store.load(aid) if run_store is not None else None
        definition = (_meta(controller).get("automation") if controller is not None else None) or {}
        tdef = definition.get("trigger") if isinstance(definition.get("trigger"), dict) else {}
        env = occ.get("trigger_envelope") if isinstance(occ.get("trigger_envelope"), dict) else {}
        source = str(tdef.get("source_id") or env.get("source_id") or "")
        text, connection = (trigger_words(tdef) if (trigger_words is not None and tdef) else ("", None))
        trigger = {
            "automation_id": aid,
            "automation_title": definition.get("title"),
            "occurrence_index": index,
            "source": source or None,
            "text": text or None,
            "connection": connection,
            "fired_at": occ.get("fired_at") or env.get("fired_at"),
            "arrived": arrived_summary(source, run),
        }

    run_block = {
        "run_id": rid,
        "status": status,
        "started": getattr(run, "created_at", None),
        "ended": getattr(run, "updated_at", None) if status in TERMINAL else None,
        "response_excerpt": _clip(end.get("response") if isinstance(end.get("response"), str) else end.get("answer"), RESPONSE_EXCERPT_MAX),
        "note": _clip(end.get("note"), NOTE_MAX),
    }

    from .mail.notifications import idempotency_key as mail_key
    from .mail.notifications import notify_me_key

    index_ids = stream.index_event_ids()
    alerts: List[Dict[str, Any]] = []
    wanted: List[str] = []
    # Email events carry the outbox key, not the record key: outbox key -> its alert's record key.
    email_owner: Dict[str, str] = {}
    completion = occurrence_completion(ledger_store, aid, index) if aid else None
    if completion and completion.get("attention"):
        key = completion["record_key"]
        raised = stream.events_by_ids([f"attention.raised:{key}"], index=index_ids)
        alerts.append(_alert_from_completion(completion, raised[0] if raised else None))
        mails = email_notice_keys(aid, completion["attention"])
        email_owner.update({m: key for m in mails})
        wanted += notice_event_ids(key, email_keys=mails)
    # The run and its sub-runs (an agent's run, a subflow): the alerts they published (Notify me)
    # and the waits that parked them on the person (approvals, questions), each by its id prefix
    # in the stream index (`attention.raised:publish:<run>:`, `attention.raised:<run>:`).
    tree = run_tree(run, run_store) if run_store is not None else [run]
    waits: List[Dict[str, Any]] = []
    for node in tree:
        nid = str(getattr(node, "run_id", "") or "")
        for event in stream.events_by_ids(published_alert_ids(index_ids, nid), index=index_ids):
            alert = _alert_from_published(event)
            if alert["record_key"]:
                alert["run_id"] = nid
                alerts.append(alert)
                mail = notify_me_key(alert["record_key"])
                email_owner[mail] = alert["record_key"]
                wanted += notice_event_ids(alert["record_key"], email_keys=[mail])
        prefix = f"attention.raised:{nid}:"
        wait_ids = sorted((k for k in index_ids if k.startswith(prefix)), key=lambda k: int(index_ids[k]))
        for event in stream.events_by_ids(wait_ids, index=index_ids):
            alert = _alert_from_wait(event, index_ids)
            if alert["record_key"]:
                waits.append(alert)
                data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
                mail = mail_key("approval_needed", nid, data.get("wait_key"))
                email_owner[mail] = alert["record_key"]
                wanted += notice_event_ids(alert["record_key"], email_keys=[mail])
    # An open wait is what the person must act on now: it leads.
    alerts = [a for a in waits if a["open"]] + alerts + [a for a in waits if not a["open"]]

    # "Email me when done" on the run itself (job_finished / job_failed): an effect after the
    # run too, shown as its own alert when its mail was recorded.
    for kind in ("job_finished", "job_failed"):
        mail = mail_key(kind, rid)
        if f"notification.sent:email:{mail}" in index_ids or f"notification.failed:email:{mail}" in index_ids:
            owner = f"email:{kind}:{rid}"
            email_owner[mail] = owner
            alerts.append({"record_key": owner, "kind": "finished" if kind == "job_finished" else "failure", "title": None,
                           "raised_at": None, "from": "email_when_done"})
            wanted += [f"notification.sent:email:{mail}", f"notification.failed:email:{mail}"]

    deliveries = []
    for event in stream.events_by_ids(wanted, index=index_ids):
        d = _delivery(event, device_name)
        data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
        if d["channel"] == "email" and not d["record_key"]:
            d["record_key"] = email_owner.get(str(data.get("notice") or ""))
        deliveries.append(d)
    deliveries.sort(key=lambda d: str(d.get("time") or ""))

    out: Dict[str, Any] = {
        "run_id": rid,
        "trigger": trigger,
        "run": run_block,
        "attention": alerts[0] if alerts else None,
        "alerts": alerts,
        "deliveries": deliveries,
        "tree": [str(getattr(n, "run_id", "") or "") for n in tree],
    }
    if not alerts:
        if status not in TERMINAL:
            out["quiet_reason"] = "still running"
        elif aid and completion is None:
            # The run ended; the automation has not recorded its decision yet.
            out["quiet_reason"] = "not recorded yet"
        elif "notify" in end:
            out["quiet_reason"] = "empty title"
        else:
            out["quiet_reason"] = "no notify"
    return out


__all__ = ["arrived_summary", "build_after", "end_payload", "run_tree"]
