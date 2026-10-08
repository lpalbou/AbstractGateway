"""What a publish/promote/upload did to the services that run workflows (R16.2).

A workflow change is served by swapping the workflow registry on each affected
service's EXISTING runtime (`registry_swap`). A service's runtime is rebuilt only when
the new workflows need something it was started without (`service_reload`), and every
service only when explicitly asked (`full_rebuild`). `describe_workflow_reload` turns the
per-service results of `WorkflowBundleGatewayHost.reload_bundles_from_disk` into the
`reload` object every publish/promote/upload response carries, with one sentence that
says what happened in plain words.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

RELOAD_KINDS = ("registry_swap", "service_reload", "full_rebuild")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def describe_workflow_reload(results: Iterable[Dict[str, Any]], *, duration_ms: int) -> Dict[str, Any]:
    """Aggregate per-service reload results into `{ok, kind, services, duration_ms, sentence}`.

    Each result is a host reload result carrying `reload: {kind, duration_ms, reason,
    changed}` plus `service` (a label such as `default:default`), or `{ok: False, error}`.
    `services` lists the services the reload touched (changed or rebuilt); services whose
    workflows did not move are only counted (`unchanged_services`).
    """
    rows = [dict(r) for r in results]
    touched: List[Dict[str, Any]] = []
    unchanged = 0
    failed: List[Dict[str, Any]] = []
    for r in rows:
        info = r.get("reload") if isinstance(r.get("reload"), dict) else {}
        entry: Dict[str, Any] = {
            "service": str(r.get("service") or ""),
            "kind": str(info.get("kind") or ""),
            "duration_ms": int(info.get("duration_ms") or 0),
            "ok": bool(r.get("ok", True)),
        }
        if info.get("reason"):
            entry["reason"] = str(info["reason"])
        if "count" in r:
            entry["bundle_count"] = int(r.get("count") or 0)
        if r.get("skipped_count"):
            entry["skipped_count"] = int(r["skipped_count"])
        if r.get("pinned_runs"):
            entry["pinned_runs"] = int(r["pinned_runs"])
        if r.get("warnings"):
            entry["warnings"] = list(r["warnings"])
        if not entry["ok"]:
            entry["error"] = str(r.get("error") or "reload failed")
            failed.append(entry)
            touched.append(entry)
            continue
        if entry["kind"] == "registry_swap" and info.get("changed") is False:
            unchanged += 1
            continue
        touched.append(entry)

    kinds = {e["kind"] for e in touched if e["ok"]}
    if "full_rebuild" in kinds:
        kind = "full_rebuild"
    elif "service_reload" in kinds:
        kind = "service_reload"
    else:
        kind = "registry_swap"

    ms = int(duration_ms)
    swapped = [e for e in touched if e["ok"] and e["kind"] == "registry_swap"]
    rebuilt = [e for e in touched if e["ok"] and e["kind"] in ("service_reload", "full_rebuild")]
    if kind == "full_rebuild":
        sentence = (
            f"Full rebuild of {_plural(len(rebuilt), 'service')} in {ms} ms, as requested; "
            "their loaded models and prompt caches start empty."
        )
    elif kind == "service_reload":
        names = ", ".join(e["service"] or "a service" for e in rebuilt)
        reason = rebuilt[0].get("reason") or "its workflows need something it was started without"
        sentence = f"Rebuilt {names} in {ms} ms because {reason}; its loaded models and prompt caches start empty."
        if swapped:
            sentence += f" {_plural(len(swapped), 'other service')} updated in place."
    elif swapped:
        sentence = (
            f"Workflows updated in place on {_plural(len(swapped), 'service')} in {ms} ms. Nothing was "
            "restarted: loaded models, prompt caches and running runs were not touched."
        )
    else:
        sentence = "Nothing changed on disk; no service was touched."
    if failed:
        sentence += f" {_plural(len(failed), 'service')} could not reload: " + "; ".join(
            f"{e['service'] or 'a service'}: {e['error']}" for e in failed
        ) + "."

    return {
        "ok": not failed,
        "kind": kind,
        "services": touched,
        "unchanged_services": unchanged,
        "duration_ms": ms,
        "sentence": sentence,
    }
