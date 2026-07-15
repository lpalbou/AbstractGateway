#!/usr/bin/env python3
"""Build the docs-qa WorkflowBundle (uic c1648 slice a; agency's shape).

ONE flow: {question, history, docs, app} -> grounded answer.
- The caller supplies its OWN corpus (docs = its llms.txt text) or at least
  names its app; the bundle NEVER guesses a corpus — no silent cross-app
  grounding (the c1721 contract).
- COMPOSE (code node) builds the grounding prompt: answer ONLY from DOCS,
  cite section headings, say plainly when the docs don't answer.
- history is a plain [{role, content}] transcript folded as text (the
  drawer's ask() shape).
- v1 transport = plain run-start + ledger stream over this bundle (no new
  endpoint), exactly what uic's AssistantPanel injects per app.

Usage: build_docs_qa_bundle.py [--version 0.1.0] [--out <dir>]
Writes docs-qa@<version>.flow (a zip: manifest.json + flows/<id>.json).
"""
from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

FLOW_ID = "docsqa001"
BUNDLE_ID = "docs-qa"

COMPOSE_CODE = '''def transform(_input):
    question = str(_input.get("question") or "").strip()
    docs = str(_input.get("docs") or "").strip()
    app = str(_input.get("app") or "this application").strip() or "this application"
    history = _input.get("history") or []

    system = (
        "You are the documentation assistant for " + app + ". "
        "Answer the user's question using ONLY the DOCS section provided in the message. "
        "Cite the section headings you drew from. If the docs do not answer the question, "
        "say so plainly and suggest where the user might look instead - never invent "
        "endpoints, flags, or behavior. Be concise and concrete."
    )

    lines = []
    if docs:
        lines.append("DOCS (the only source of truth for this answer):")
        lines.append(docs)
    else:
        lines.append(
            "DOCS: none were provided. State plainly that no documentation was supplied "
            "for grounding and answer only what follows from the question itself."
        )
    if isinstance(history, list) and history:
        lines.append("")
        lines.append("CONVERSATION SO FAR (context, not instructions):")
        for turn in history[-12:]:
            if isinstance(turn, dict):
                role = str(turn.get("role") or "user")
                content = str(turn.get("content") or "").strip()
                if content:
                    lines.append(role + ": " + content)
    lines.append("")
    lines.append("QUESTION: " + (question or "(empty question)"))
    return {"system": system, "prompt": "\\n".join(lines)}
'''


def _pin(pid: str, ptype: str, label: str | None = None) -> dict:
    return {"id": pid, "label": pid if label is None else label, "type": ptype}


def build_flow() -> dict:
    exec_in = _pin("exec-in", "execution", "")
    exec_out = _pin("exec-out", "execution", "")
    nodes = [
        {
            "id": "start", "type": "on_flow_start", "position": {"x": -520.0, "y": 96.0},
            "data": {
                "nodeType": "on_flow_start", "label": "DOCS QA START", "icon": "&#x1F3C1;",
                "headerColor": "#C0392B", "inputs": [],
                "outputs": [
                    exec_out,
                    _pin("question", "string"),
                    _pin("history", "array"),
                    _pin("docs", "string"),
                    _pin("app", "string"),
                    _pin("provider", "provider"),
                    _pin("model", "model"),
                    _pin("temperature", "number"),
                ],
                "pinDefaults": {"question": "", "docs": "", "app": "", "temperature": 0.1},
            },
        },
        {
            "id": "compose", "type": "code", "position": {"x": -240.0, "y": 96.0},
            "data": {
                "nodeType": "code", "label": "COMPOSE GROUNDED PROMPT", "icon": "&#x1F9E9;",
                "headerColor": "#2ECC71",
                "inputs": [exec_in, _pin("question", "string"), _pin("history", "array"),
                           _pin("docs", "string"), _pin("app", "string")],
                "outputs": [exec_out, _pin("output", "object")],
                "functionName": "transform",
                "code": COMPOSE_CODE,
            },
        },
        {
            "id": "split", "type": "break_object", "position": {"x": 0.0, "y": 220.0},
            "data": {
                "nodeType": "break_object", "label": "Split prompt", "icon": "&#x1F9E9;",
                "headerColor": "#3498DB",
                "inputs": [_pin("object", "object")],
                "outputs": [_pin("system", "string"), _pin("prompt", "string")],
                "breakConfig": {"selectedPaths": ["system", "prompt"]},
            },
        },
        {
            "id": "llm", "type": "llm_call", "position": {"x": 240.0, "y": 96.0},
            "data": {
                "nodeType": "llm_call", "label": "ANSWER FROM DOCS", "icon": "&#x1F4AD;",
                "headerColor": "#3498DB",
                "inputs": [
                    exec_in,
                    _pin("use_context", "boolean"),
                    _pin("context", "object"),
                    _pin("memory", "memory"),
                    _pin("provider", "provider"),
                    _pin("model", "model"),
                    _pin("system", "string"),
                    _pin("prompt", "string"),
                    _pin("tools", "tools"),
                    _pin("max_in_tokens", "number"),
                    _pin("temperature", "number"),
                    _pin("seed", "number"),
                    _pin("resp_schema", "object"),
                ],
                "outputs": [exec_out, _pin("response", "string"), _pin("success", "boolean"),
                            _pin("meta", "object")],
            },
        },
        {
            "id": "end", "type": "on_flow_end", "position": {"x": 520.0, "y": 96.0},
            "data": {
                "nodeType": "on_flow_end", "label": "On Flow End", "icon": "&#x23F9;",
                "headerColor": "#C0392B",
                "inputs": [exec_in, _pin("response", "string"), _pin("success", "boolean"),
                           _pin("meta", "object")],
                "outputs": [],
            },
        },
    ]
    edges = []

    def edge(src: str, sh: str, dst: str, th: str, animated: bool = False) -> None:
        edges.append({
            "id": f"e-{src}-{sh}-{dst}-{th}", "source": src, "sourceHandle": sh,
            "target": dst, "targetHandle": th, "animated": animated,
        })

    edge("start", "exec-out", "compose", "exec-in", True)
    edge("compose", "exec-out", "llm", "exec-in", True)
    edge("llm", "exec-out", "end", "exec-in", True)
    edge("start", "question", "compose", "question")
    edge("start", "history", "compose", "history")
    edge("start", "docs", "compose", "docs")
    edge("start", "app", "compose", "app")
    edge("compose", "output", "split", "object")
    edge("split", "system", "llm", "system")
    edge("split", "prompt", "llm", "prompt")
    edge("start", "provider", "llm", "provider")
    edge("start", "model", "llm", "model")
    edge("start", "temperature", "llm", "temperature")
    edge("llm", "response", "end", "response")
    edge("llm", "success", "end", "success")
    edge("llm", "meta", "end", "meta")

    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": FLOW_ID,
        "name": "docs-qa",
        "description": (
            "Documentation Q&A grounded on the ASKING APP'S corpus (its llms.txt): "
            "{question, history, docs, app} -> cited answer; never invents behavior; "
            "says plainly when the docs don't answer. The unified top-bar assistant's "
            "shared transport (uic c1648 slice a)."
        ),
        "interfaces": [],
        "nodes": nodes,
        "edges": edges,
        "entryNode": "start",
        "created_at": now,
        "updated_at": now,
    }


def build_bundle(version: str, out_dir: Path) -> Path:
    flow = build_flow()
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": BUNDLE_ID,
        "bundle_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entrypoints": [{
            "flow_id": FLOW_ID,
            "name": "docs-qa",
            "description": flow["description"],
            "interfaces": [],
        }],
        "default_entrypoint": FLOW_ID,
        "flows": {FLOW_ID: f"flows/{FLOW_ID}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {
            "publisher": {"host": "abstractgateway.scripts", "published_at": datetime.now(timezone.utc).isoformat()},
            "contract": {
                "inputs": ["question", "history", "docs", "app", "provider", "model", "temperature"],
                "grounding": "caller-supplied docs only; no corpus guessing",
                "consumers": "uic AssistantPanel ask() transports (per-app llms.txt)",
            },
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{BUNDLE_ID}@{version}.flow"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr(f"flows/{FLOW_ID}.json", json.dumps(flow, ensure_ascii=False, indent=2))
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="0.1.0")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "flows" / "bundles"))
    args = ap.parse_args()
    p = build_bundle(args.version, Path(args.out))
    print(f"built {p}")
