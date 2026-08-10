"""THE ONE DOOR to AbstractCore-owned configuration.

TWO ENTRY POINTS, ONE STORE. AbstractCore and AbstractGateway are the two entry
points to the framework. Where AbstractCore holds a configuration value, that
value is the single source of truth and the Gateway holds no copy of it: the
Gateway reads and writes it THROUGH this module, and surfaces it alongside the
configuration that is the Gateway's own.

ONE PATH, NOT TWO (operator ruling 2026-08-01). The store this module resolves
is THE AbstractCore store -- the very path AbstractCore's own configuration
layer resolves by default (`~/.abstractcore/config/abstractcore.json`,
honouring `ABSTRACTCORE_CONFIG_FILE` / `ABSTRACTCORE_CONFIG_DIR`) -- obtained
from AbstractCore's own resolver through
`config_facade.capability_default_config_path`, never re-derived here. The
Gateway used to keep a SECOND base of its own at `<data_dir>/config/
abstractcore.json` whenever user auth was on; that store is RETIRED. It made
`abstractcore config defaults` and the Gateway grid disagree about provider,
model and reasoning on the same machine, which is exactly what the ruling
forbids: "modifying values from the gateway/consoles must just change the value
AT THE SOURCE (in core)". A legacy file is merged into the Core store and
renamed on startup -- see `core_config_migration.py`.

WHAT STAYS LAYERED. Per-USER overlay files (`<user runtime>/config/
abstractcore.json`) are Gateway-proper and unchanged: they sit ON TOP of the one
Core base, and an absent overlay still means "this user overrides nothing", so
the admin-default-inheritance contract holds. The gateway ROOT is not a scope:
an admin write is a write to the source.

PROVIDER PROFILES FOLLOW THE SAME RULING. A provider endpoint profile is
provider configuration, so it is Core-owned: profiles created from EITHER
console land in Core's `provider_profiles` (`provider_endpoint_profiles.py`
backs the gateway-root store onto this seam). Gateway-only scoping remains
possible for per-user overlays alone.

WHAT IS CORE-OWNED. These domains live in AbstractCore's config store
(`~/.abstractcore/config/abstractcore.json`, plus the per-user overlay a
multi-user Gateway layers on top) and are reachable only through the functions
here:

  - capability route defaults -- the default provider/model/base_url for every
    routable capability (`output.text`, `output.image.text_to_image`,
    `output.voice`, `input.voice`, `embedding.text`, ...)
  - the reasoning effort carried on the text-generation route
  - plugin/provider options carried on a route (voice, profile, language, ...)
  - provider API keys held in the Core config file
  - the host's mail connection (the `email` section: IMAP/SMTP host, port,
    username, folder, and the env var a password is read from)
  - the maintenance-triage LLM settings (the `maintenance` section)

WHAT IS GATEWAY-PROPER stays out of this module: endpoint profiles, auth tokens
and principals, the bundle/workflow registry, workspaces, run policies and
retention, and integrations. Those have no AbstractCore representation and the
Gateway is their authority.

THE CONTRACT this module keeps:

  - READ-THROUGH. Every read resolves from the store. `config_signature()`
    fingerprints the files a payload derives from so a caller can tell, for the
    cost of a `stat`, whether a re-read is warranted.
  - THE SEED IS PER INSTALL, NOT PER SCOPE. AbstractCore seeds its recommended
    capability routes into a config file that has never existed, so a fresh
    install works out of the box, and the payload carries `seeded` as the
    provenance of those rows. The Gateway's per-principal stores are OVERLAYS
    on top of that install, and an absent overlay means "this scope overrides
    nothing" -- never "a fresh install of its own". See
    `_load_configured_routes_from_core_config`.
  - WRITE-THROUGH, FIELD-PRESERVING. A write persists to the same store the
    `abstractcore config` CLI and the AbstractCore console-TUI write. A write
    that names only some fields of a route preserves the fields it did not name,
    so setting a provider through one entry point never discards a reasoning
    effort set through the other.
  - SPLIT-SERVER POSTURE. With `ABSTRACTCORE_SERVER_BASE_URL` set, the store
    lives behind an HTTP boundary on the AbstractCore server: reads and writes
    proxy there, `config_signature()` returns ``None`` because there is no file
    to stat, and the write path's push to the live runtime is the freshness
    mechanism. Field preservation holds across that boundary too -- a write
    resolves the merge against the row the server serves and sends the whole
    resolved row, so a remote save keeps exactly what a local save keeps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

# Boundary rule: Gateway imports AbstractRuntime, never AbstractCore directly.
# AbstractCore config management is reached through the Runtime config facade.
# This module is the ONLY Gateway module that may import it.
from abstractruntime.integrations.abstractcore import config_facade

# THE TEXT-GENERATION ROUTE, BY NAME. `output.text` is the canonical read;
# AbstractCore canonicalizes it to the storage key `input.text`, which stays
# readable so a config carrying only the storage key still resolves.
TEXT_ROUTE_KEY = "output.text"
TEXT_ROUTE_STORAGE_KEY = "input.text"
TEXT_ROUTE_KEYS: Tuple[str, ...] = (TEXT_ROUTE_KEY, TEXT_ROUTE_STORAGE_KEY)

# The fields a capability route row carries. `_ROUTE_FIELDS` is what a
# field-preserving write merges over.
_ROUTE_FIELDS: Tuple[str, ...] = ("provider", "model", "base_url", "reasoning")


def gateway_capability_defaults_payload(*, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return execution-host capability defaults through the Gateway control plane.

    Gateway is not the persistence owner for these defaults. In embedded/local
    deployments it edits the local AbstractCore config. In split deployments it
    proxies to the configured AbstractCore server.
    """

    core_base_url = core_server_base_url()
    if core_base_url:
        try:
            payload = _core_server_json("GET", "/config/capability-defaults")
            payload.setdefault("authority", "abstractcore.server")
            payload.setdefault("writable", True)
            payload.setdefault("source", "abstractcore.server")
            return _apply_scoped_core_defaults(payload, base_dir=base_dir)
        except Exception as exc:
            payload = {
                "ok": False,
                "authority": "abstractcore.server",
                "writable": False,
                "routes": [],
                "errors": [str(exc)],
                "config_hint": "Gateway is configured to use a remote AbstractCore server; capability defaults must be read from that execution host.",
            }
            return _apply_scoped_core_defaults(payload, base_dir=base_dir)

    # ONE BASE, ALWAYS THE CORE STORE. Whether user auth is on changes which
    # OVERLAY applies on top, never which store is underneath.
    return _apply_scoped_core_defaults(_local_core_payload(), base_dir=base_dir)


def capability_defaults_config_signature(*, base_dir: Optional[Path] = None) -> Optional[tuple]:
    """A cheap (path, mtime_ns, size) fingerprint of every FILE the capability
    defaults payload is derived from. ``None`` when the payload is not
    file-backed (a split AbstractCore server owns it).

    ONE STORE, TWO WRITERS. The Gateway's PUT/DELETE routes are not the only
    supported way to set a default: `abstractcore config set-default <route>
    --provider ... --model ...` and AbstractCore's console-TUI write the very
    same file directly -- that IS entry point (a) of the operator's ruling. The
    execution host pushes a COPY of this payload into the live runtime, and
    once it has, `resolve_capability_default_route` no longer falls through to
    disk for ANY route: unconfigured rows arrive as an explicit
    ``source: "not_configured"``, which short-circuits the config-file read it
    would otherwise do. So without a freshness check an out-of-band write is
    invisible to the running host until the next Gateway write or a restart --
    the two entry points would disagree, which is exactly what the ruling
    forbids.

    A `stat` reads no content, so the host can afford one per run (~24us
    measured for the whole signature); the payload is only re-derived when a
    file actually changed. It is deliberately NOT a TTL: a TTL pays the parse
    on a timer whether or not anything moved, and still lies for the length of
    the window.

    RESOLVING THE PATHS MUST NOT COST A PARSE. Locating the AbstractCore store
    through the LOADING facade call cost a full read of it -- 133us of JSON
    parse plus env application, on every run -- so the check that exists to
    avoid re-reading the file re-read it first. `capability_default_config_path`
    answers the same question at stat cost (~24us for the whole signature).
    """

    paths: list[Path] = []

    def _add(value: Any) -> None:
        if not value:
            return
        try:
            candidate = Path(str(value)).expanduser()
        except Exception:
            return
        if not any(_same_path(candidate, seen) for seen in paths):
            paths.append(candidate)

    if core_server_base_url():
        # The store lives behind an HTTP boundary; there is no file to stat and
        # a per-run GET is the starvation lever this whole design avoids. The
        # write routes' push stays the freshness mechanism there.
        return None

    try:
        # The PATH resolver, not the loading one: this runs per run, and asking
        # for the location by building a manager re-read and re-parsed the very
        # file the fingerprint exists to avoid re-reading.
        _add(config_facade.capability_default_config_path())
    except Exception:
        pass
    _add(_scoped_core_config_path(base_dir))

    signature: list[tuple] = []
    for path in paths:
        try:
            st = path.stat()
            signature.append((str(path), st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            # Absent is a state too: creating the file must count as a change.
            signature.append((str(path), None, None))
        except Exception:
            signature.append((str(path), "unreadable", None))
    return tuple(signature)


def save_gateway_capability_default(
    kind: str,
    modality: Optional[str] = None,
    *,
    task: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    reasoning: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist one capability route through AbstractCore, preserving unnamed fields.

    AbstractCore stores a route as a whole row, so a write that named only
    `provider` and `model` would clear every other field of that row. The
    Gateway console edits provider/model and leaves reasoning to the
    `abstractcore config set-default --reasoning` entry point, so a save must
    merge over the stored row rather than replace it -- otherwise the two entry
    points overwrite each other and the store stops being one truth.

    Pass a field explicitly to change it; pass `""` to clear it.
    """

    if modality is None:
        kind, modality = _split_route(kind)
        if "." in str(modality):
            modality, task = str(modality).split(".", 1)

    body = {
        "provider": _clean(provider),
        "model": _clean(model),
        "base_url": _clean(base_url),
        "reasoning": _clean(reasoning),
        "options": dict(options or {}) if isinstance(options, dict) else {},
    }

    scoped_config_path = _writable_scoped_core_config_path(base_dir)
    if scoped_config_path is not None:
        merged = _merge_over_stored_route(
            body,
            kind,
            modality,
            task=task,
            config_file=scoped_config_path,
            provider=provider,
            model=model,
            base_url=base_url,
            reasoning=reasoning,
            options=options,
        )
        _save_core_config_route(
            scoped_config_path,
            kind,
            modality,
            task=task,
            provider=merged["provider"],
            model=merged["model"],
            base_url=merged["base_url"],
            reasoning=merged["reasoning"],
            options=merged["options"],
        )
        return gateway_capability_defaults_payload(base_dir=base_dir)

    if core_server_base_url():
        # THE SPLIT STORE IS STILL ONE STORE. The row lives behind an HTTP
        # boundary, so the merge is resolved here against what the server
        # currently serves and the fully-resolved row is what goes over the
        # wire: a field this save did not name arrives carrying its stored
        # value, and a cleared field arrives as `""`. A remote save then
        # preserves exactly what a local save preserves, whichever merge rule
        # the AbstractCore server on the far side happens to apply.
        merged = _merge_over_stored_route(
            body,
            kind,
            modality,
            task=task,
            config_file=None,
            provider=provider,
            model=model,
            base_url=base_url,
            reasoning=reasoning,
            options=options,
        )
        remote_body = {field: (merged.get(field) or "") for field in _ROUTE_FIELDS}
        remote_body["options"] = dict(merged.get("options") or {})
        suffix = f"/{task}" if _clean(task) else ""
        payload = _core_server_json("PUT", f"/config/capability-defaults/{kind}/{modality}{suffix}", remote_body)
        payload.setdefault("authority", "abstractcore.server")
        payload.setdefault("writable", True)
        return payload

    merged = _merge_over_stored_route(
        body,
        kind,
        modality,
        task=task,
        config_file=None,
        provider=provider,
        model=model,
        base_url=base_url,
        reasoning=reasoning,
        options=options,
    )
    if not config_facade.set_capability_default(
        kind,
        modality,
        task=task,
        provider=merged["provider"],
        model=merged["model"],
        base_url=merged["base_url"],
        reasoning=merged["reasoning"],
        options=merged["options"],
    ):
        raise ValueError(f"Failed to set capability default {kind}.{modality}")
    return _local_core_payload()


def _merge_over_stored_route(
    body: Dict[str, Any],
    kind: str,
    modality: str,
    *,
    task: Optional[str],
    config_file: Optional[Path],
    provider: Any,
    model: Any,
    base_url: Any,
    reasoning: Any,
    options: Any,
) -> Dict[str, Any]:
    """Fill the fields a save did not name from the currently stored route.

    A field the caller passed -- including an explicit empty string, which means
    "clear it" -- always wins. Only ``None`` (the field was not named at all)
    falls back to the stored value.
    """

    named = {
        "provider": provider is not None,
        "model": model is not None,
        "base_url": base_url is not None,
        "reasoning": reasoning is not None,
    }
    if all(named.values()) and options is not None:
        return dict(body)

    stored = _stored_route_row(kind, modality, task=task, config_file=config_file)
    merged = dict(body)
    for field in _ROUTE_FIELDS:
        if not named[field]:
            merged[field] = _clean(stored.get(field))
    if options is None:
        stored_options = stored.get("options")
        merged["options"] = dict(stored_options) if isinstance(stored_options, dict) else {}
    return merged


def _stored_route_row(
    kind: str,
    modality: str,
    *,
    task: Optional[str],
    config_file: Optional[Path],
) -> Dict[str, Any]:
    """The route row as AbstractCore currently stores it, or `{}`.

    Only a row AbstractCore actually persists for THIS route counts. A row the
    store derives from another route -- `input.image` answered by `input.text`,
    say -- is marked `covered_by` and is skipped: merging over a derivation
    would persist it, turning coverage that follows the text route into a frozen
    copy of whatever it said at save time.
    """

    suffix = f".{_clean(task)}" if _clean(task) else ""
    wanted = f"{str(kind).strip().lower()}.{str(modality).strip().lower()}{suffix}"
    try:
        if config_file is not None:
            rows = config_facade.list_capability_defaults(config_file=config_file, apply_env=False)
        elif core_server_base_url():
            # Split deployment: the store the write lands in is the server's,
            # so the row the write merges over must come from the server too.
            payload = _core_server_json("GET", "/config/capability-defaults")
            rows = payload.get("routes") if isinstance(payload, dict) else None
            rows = rows if isinstance(rows, list) else []
        else:
            rows = config_facade.list_capability_defaults()
    except Exception as exc:
        # `{}` MEANS "no such row", NOT "could not look". The caller merges the
        # save over whatever comes back, so answering `{}` to a failed READ makes
        # every field the save does not name look absent — and the merge then
        # clears settings nobody touched. A full disk is enough to trigger it
        # (AbstractCore now raises OSError rather than degrading to defaults, and
        # this handler used to swallow that too).
        #
        # There is no safe merge over an unknown baseline, so refuse.
        raise RuntimeError(
            f"could not read the stored route {wanted} before saving it: {exc}. "
            "Refusing to merge over an unknown baseline — nothing was written."
        ) from exc
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("key") or "").strip().lower() != wanted:
            continue
        if str(row.get("source") or "").strip() == "not_configured":
            return {}
        if row.get("covered_by"):
            return {}
        return dict(row)
    return {}


def clear_gateway_capability_default(
    kind: str,
    modality: Optional[str] = None,
    *,
    task: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if modality is None:
        kind, modality = _split_route(kind)
        if "." in str(modality):
            modality, task = str(modality).split(".", 1)

    scoped_config_path = _writable_scoped_core_config_path(base_dir)
    if scoped_config_path is not None:
        _clear_core_config_route(scoped_config_path, kind, modality, task=task)
        return gateway_capability_defaults_payload(base_dir=base_dir)

    if core_server_base_url():
        suffix = f"/{task}" if _clean(task) else ""
        payload = _core_server_json("DELETE", f"/config/capability-defaults/{kind}/{modality}{suffix}")
        payload.setdefault("authority", "abstractcore.server")
        payload.setdefault("writable", True)
        return payload

    if not config_facade.clear_capability_default(kind, modality, task=task):
        suffix = f".{task}" if _clean(task) else ""
        raise ValueError(f"Failed to clear capability default {kind}.{modality}{suffix}")
    return _local_core_payload()


def apply_recommended_gateway_capability_defaults(
    *,
    only: Optional[list] = None,
    force: bool = False,
    dry_run: bool = False,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply AbstractCore's recommended capability routes to the store the
    console edits, and return the report alongside the refreshed grid.

    ONE ACTION, ONE DECISION-MAKER. Which routes get written, which are kept
    because the operator configured them differently, and which already match
    is AbstractCore's call (`apply_recommended_capability_defaults`); the
    Gateway only chooses WHICH STORE -- the same choice every other write here
    makes: the per-principal/runtime overlay when user auth is on, otherwise
    the install store.

    NOT SUPPORTED over the split-server boundary: the AbstractCore server would
    have to expose the action itself, and quietly applying it to the Gateway's
    own (unused) local store instead would write to the wrong machine.
    """

    if core_server_base_url():
        raise RuntimeError(
            "Capability defaults live on a remote AbstractCore server; run "
            "`abstractcore config apply-recommended` on that execution host."
        )

    scoped_config_path = _writable_scoped_core_config_path(base_dir)
    report = config_facade.apply_recommended_capability_defaults(
        only=list(only) if only else None,
        force=bool(force),
        dry_run=bool(dry_run),
        config_file=scoped_config_path,
        apply_env=scoped_config_path is None,
    )
    payload = dict(gateway_capability_defaults_payload(base_dir=base_dir))
    payload["applied_recommended"] = report
    return payload


def capability_default_rows(*, base_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Every capability route the store carries, keyed by route key."""

    payload = gateway_capability_defaults_payload(base_dir=base_dir)
    rows = payload.get("routes") if isinstance(payload, dict) else None
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        if key and key not in out:
            out[key] = dict(row)
    return out


def text_route_provider_warnings(
    kind: str,
    modality: Optional[str] = None,
    *,
    task: Optional[str] = None,
    provider: Optional[str] = None,
) -> list[str]:
    """Warn (never refuse) when a TEXT route names a provider Core cannot build.

    ACCEPT-AND-WARN, deliberately. Refusing unknown names would break every
    plugin/media backend (`mlx-gen`, `supertonic`, `faster-whisper`, ...) and
    every endpoint profile added after this process started, and a config store
    that rejects values its own runtime accepts is worse than one that does not
    check. But a TEXT route is different: AbstractCore has a closed registry
    for those, so an unrecognized name there is almost always a typo the
    operator should learn about NOW rather than at the first run.

    Silence is the failure mode of choice: an unavailable registry, a non-text
    route, or an `endpoint:<profile>` reference all return `[]`.
    """

    name = _clean_lower(provider)
    if not name:
        return []
    try:
        route_kind, route_modality = (kind, modality) if modality is not None else _split_route(kind)
    except Exception:
        return []
    route_key = f"{str(route_kind).strip().lower()}.{str(route_modality).strip().lower()}"
    if task or route_key not in TEXT_ROUTE_KEYS:
        return []
    if name.startswith("endpoint:"):
        # A profile reference; profiles are Gateway/Core data, not registry ids.
        return []
    try:
        known = [str(p).strip().lower() for p in config_facade.list_llm_provider_names()]
    except Exception:
        return []
    if not known or name in known:
        return []
    return [
        f"Provider {name!r} is not a known AbstractCore text provider "
        f"(available: {', '.join(sorted(known))}). It was saved, but text runs using this "
        "default will fail until it names a real provider or an existing endpoint profile "
        "(endpoint:<id>)."
    ]


def text_default(*, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The execution host's text-generation default, read BY NAME.

    Returns `{"provider", "model", "reasoning", "source", "key"}` with `None`
    for anything the store does not carry. The canonical key `output.text`
    answers first and the storage key `input.text` second, so a config that
    carries only the storage key still resolves; `source` names which key
    answered, so a run's evidence says where its default came from.
    """

    out: Dict[str, Any] = {"provider": None, "model": None, "reasoning": None, "source": None, "key": None}
    try:
        payload = gateway_capability_defaults_payload(base_dir=base_dir)
    except Exception:
        return out
    rows = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return out
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _row_key(row)
        if key and key not in by_key:
            by_key[key] = row
    for wanted in TEXT_ROUTE_KEYS:
        row = by_key.get(wanted)
        if not isinstance(row, dict):
            continue
        if str(row.get("source") or "").strip() == "not_configured":
            continue
        provider = _clean_lower(row.get("provider"))
        model = _clean(row.get("model"))
        reasoning = _clean_lower(row.get("reasoning"))
        if not (provider or model or reasoning):
            continue
        origin = str(row.get("source") or payload.get("authority") or "abstractcore_config")
        return {
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "source": f"{origin}:{wanted}",
            "key": wanted,
        }
    return out


def reasoning_default(*, base_dir: Optional[Path] = None) -> Optional[str]:
    """The reasoning effort configured on the text-generation route, or `None`.

    `None` means no effort is configured, and the execution host sends no
    reasoning parameter at all.
    """

    return text_default(base_dir=base_dir).get("reasoning")


def capability_default_specs() -> Dict[str, Dict[str, Any]]:
    """The catalog of routable capabilities AbstractCore knows about."""

    return config_facade.capability_default_specs()


# ---------------------------------------------------------------------------
# Model WEIGHTS -- the same seam, one question further down
# ---------------------------------------------------------------------------
#
# A capability route says WHICH model; these say whether that model's weights
# are on the execution host and how to fetch them when they are not. It belongs
# behind this door for the same reason the routes do: AbstractCore owns the
# answer (`abstractcore.config.model_materializer`), the Gateway surfaces it,
# and a second opinion -- a Gateway-side `shutil.which("ollama")` -- is exactly
# the drift this seam exists to prevent.
#
# WHY THE PAYLOAD IS BUILT FROM `gateway_capability_defaults_payload` AND NOT
# FROM A SECOND CONFIG READ: the Gateway resolves routes across three scopes
# (install, gateway runtime, principal). Annotating a fresh local-config read
# would answer about a DIFFERENT store than the grid renders, so the
# availability column and the provider/model columns would disagree on exactly
# the multi-user deployments where it matters most.


def gateway_model_availability_payload(*, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The capability grid, annotated with local weight availability.

    Every route row gains `availability` (installed / absent / unknown /
    not_applicable, with the evidence that produced it) and, where the served
    model id is not the download reference, `download_artifact`. `recommended`
    carries the fresh-install set so a console can say "2 of 3 present".

    READ-ONLY AND CHEAP BY CONTRACT: probes never download and never contact a
    model hub. They do talk to localhost daemons with short timeouts, so a
    caller runs this off the event loop.
    """

    payload = gateway_capability_defaults_payload(base_dir=base_dir)
    routes = payload.get("routes") if isinstance(payload, dict) else None
    rows = routes if isinstance(routes, list) else []
    out: Dict[str, Any] = {
        "ok": bool(payload.get("ok", True)) if isinstance(payload, dict) else True,
        "version": 1,
        "authority": payload.get("authority") if isinstance(payload, dict) else None,
        "source": payload.get("source") if isinstance(payload, dict) else None,
        "config_file": payload.get("config_file") if isinstance(payload, dict) else None,
        "routes": [],
        "recommended": {},
        "errors": list(payload.get("errors") or []) if isinstance(payload, dict) else [],
    }
    if isinstance(payload, dict) and payload.get("seeded"):
        out["seeded"] = payload["seeded"]
    # ONE PAYLOAD IS ONE SNAPSHOT. The rows and the recommended summary are two
    # calls that describe the same machine at the same moment; a sweep makes
    # them read each provider's library ONCE, so they cannot disagree and a
    # wedged `lms ls` costs its timeout once instead of once per probe.
    with config_facade.model_presence_sweep():
        try:
            out["routes"] = config_facade.annotate_model_availability(rows)
        except Exception as exc:
            # A failed probe must never take the grid down: the routes still
            # serve, the availability column simply has nothing to say.
            out["routes"] = [dict(row) for row in rows if isinstance(row, dict)]
            out["errors"].append(f"model availability probe failed: {exc}")
            out["ok"] = False
        try:
            out["recommended"] = config_facade.recommended_model_plan()
        except Exception as exc:
            out["errors"].append(f"recommended model probe failed: {exc}")
    return out


def recommended_core_model_downloads() -> list[Dict[str, str]]:
    """`{route, provider, artifact}` for the recommended fresh-install set."""

    try:
        return list(config_facade.recommended_model_downloads())
    except Exception:
        return []


def core_model_download(
    provider: str,
    artifact: str,
    *,
    progress_cb: Any = None,
    base_url: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run AbstractCore's materializer for ONE artifact. Blocking, explicit.

    Never called on a request path: the Gateway's job runner
    (`model_downloads.py`) owns the thread, the single-flight and the progress
    buffer; this is only the door the work goes through.
    """

    return config_facade.download_model_artifact(
        provider,
        artifact,
        progress_cb=progress_cb,
        base_url=base_url,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Provider endpoint profiles -- provider config, therefore Core-owned
# ---------------------------------------------------------------------------
#
# THE RULING (2026-08-01): a profile the operator creates from EITHER console is
# provider configuration, so it lands in Core's `provider_profiles`. The Gateway
# keeps the hosting columns (`scope`, `capabilities`) on the same Core row
# instead of a second file, and per-USER overlays remain the one place a
# Gateway-only profile can live. `provider_endpoint_profiles.py` is the Gateway
# layer over these three functions.


def list_core_provider_profiles() -> list[Dict[str, Any]]:
    """Every profile in the Core store, with secrets, or `[]`.

    Never raises: a store the Gateway cannot read must degrade to "no profiles
    configured", exactly as an absent file did.
    """

    try:
        rows = config_facade.list_provider_profiles()
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def save_core_provider_profile(profile_id: str, **fields: Any) -> Dict[str, Any]:
    """Create or update a profile in the Core store. Raises on a rejected value."""

    return dict(config_facade.set_provider_profile(profile_id, **fields))


def delete_core_provider_profile(profile_id: str) -> bool:
    """Delete a profile from the Core store."""

    try:
        return bool(config_facade.delete_provider_profile(profile_id))
    except Exception:
        return False


def core_config_file(*, config_file: Optional[Path] = None, apply_env: bool = True) -> str:
    """The AbstractCore config file path a read or write would resolve to."""

    if config_file is not None:
        return config_facade.capability_default_config_file(config_file=config_file, apply_env=apply_env)
    return config_facade.capability_default_config_file(apply_env=apply_env)


def read_core_config_api_key(config_file: Any, attr: str) -> str:
    """A provider API key held in an AbstractCore config file.

    Never applies environment side effects and returns `""` rather than raising
    when the file or attribute is absent.
    """

    return config_facade.read_config_api_key(config_file, attr)


def core_email_settings() -> Dict[str, Any]:
    """AbstractCore's stored mail connection, or `{}`.

    AbstractCore holds the host's IMAP/SMTP settings in its `email` config
    section and its own mail tools resolve from it. Gateway features that reach
    the same mailbox read it here rather than requiring the operator to state
    the same host twice, once per entry point. Passwords never travel: the
    section names the environment variable a password is read from.

    Never raises; an unreadable or absent config reads as `{}`.
    """

    try:
        settings = config_facade.read_email_settings()
    except Exception:
        return {}
    return dict(settings) if isinstance(settings, dict) else {}


def core_maintenance_settings() -> Dict[str, Any]:
    """AbstractCore's stored maintenance-triage LLM settings, or `{}`.

    AbstractCore holds the triage assistant's provider settings in its
    `maintenance` config section. The Gateway runs the same assistant, so it
    reads that store rather than asking the operator to state the model, base
    URL and limits a second time under Gateway environment names.

    Never raises; an unreadable or absent config reads as `{}`.
    """

    try:
        settings = config_facade.read_maintenance_settings()
    except Exception:
        return {}
    return dict(settings) if isinstance(settings, dict) else {}


def _row_key(row: Dict[str, Any]) -> str:
    """A route row's key, rebuilt from kind/modality when the row omits it."""

    key = str(row.get("key") or "").strip().lower()
    if key:
        return key
    kind = str(row.get("kind") or row.get("direction") or "").strip().lower()
    modality = str(row.get("modality") or "").strip().lower()
    if not kind or not modality:
        return ""
    # `task` on a spec row is descriptive ("text_generation"), not a route
    # segment; the 3-part image/video/scene3d routes ship an explicit `key`.
    return f"{kind}.{modality}"


def _clean_lower(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def _unreadable_store_errors(config_file: Any) -> list[str]:
    """`[]`, or one loud line when the store exists and does not parse.

    A CORRUPT STORE MUST NOT LOOK LIKE A FRESH INSTALL. AbstractCore already
    refuses to destroy an unparseable config -- it backs the file up and falls
    back to defaults for the session (`manager._load_config`) -- but that
    fallback reaches this payload as every route `not_configured`, which is
    byte-for-byte what a brand-new install looks like. The operator would then
    be told to configure a default they configured months ago, and the next
    save would write defaults over the (recoverable) file.

    So the control plane says it. Best-effort and never raises: a payload that
    could not run its own diagnostic still serves the routes.
    """
    if not config_file:
        return []
    try:
        path = Path(str(config_file)).expanduser()
        if not path.is_file():
            return []
        json.loads(path.read_text(encoding="utf-8"))
        return []
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        backups = []
        try:
            backups = sorted(p.name for p in path.parent.glob(path.name + ".corrupt-*.bak"))
        except Exception:
            backups = []
        recovery = f" A copy was preserved as {backups[-1]}." if backups else ""
        return [
            f"The AbstractCore config store at {path} could not be parsed ({exc}); "
            "capability defaults below are DEFAULTS, not what you configured."
            f"{recovery} Fix or restore the file before saving - a save overwrites it."
        ]
    except Exception:
        return []


def _seed_marker(**kwargs: Any) -> Optional[str]:
    """`"recommended-v1"` when this store's routes came from the fresh-install
    seed, else ``None``. Provenance only -- see
    `config_facade.capability_defaults_seed_marker`. Surfaced on the payload so
    a console can label the three seeded rows "recommended" instead of letting
    them read as values the operator chose."""
    try:
        return config_facade.capability_defaults_seed_marker(**kwargs)
    except Exception:
        return None


def _local_core_payload() -> Dict[str, Any]:
    config_file = config_facade.capability_default_config_file()
    payload = {
        "ok": True,
        "version": 1,
        "authority": "abstractcore.local",
        "writable": True,
        "source": "abstractcore.local",
        "config_file": config_file,
        "routes": config_facade.list_capability_defaults(),
        "errors": _unreadable_store_errors(config_file),
    }
    seeded = _seed_marker()
    if seeded:
        payload["seeded"] = seeded
    return payload


def runtime_core_config_file(base_dir: Optional[Path]) -> Optional[Path]:
    """The AbstractCore config file a RUNTIME built for this scope should read.

    The per-user overlay when this scope has one, otherwise THE Core store.
    Handing a runtime `<data_dir>/config/abstractcore.json` is how the second
    store reached the execution path: a run then resolved its provider from a
    file the `abstractcore` CLI never opens.
    """

    return _scoped_core_config_path(base_dir) or _core_store_path()


def core_store_path() -> Optional[Path]:
    """THE Core store path, for a caller that needs the FILE (a migration).

    Public because the retirement of the second store has to name both paths;
    everything else in the Gateway should ask this module for values, not for
    the file they live in.
    """

    return _core_store_path()


def default_core_config_document() -> Dict[str, Any]:
    """AbstractCore's untouched config document -- the baseline of a store merge."""

    return dict(config_facade.default_config_document())


def merge_core_config_documents(
    baseline: Optional[Dict[str, Any]],
    mine: Dict[str, Any],
    disk: Dict[str, Any],
) -> Dict[str, Any]:
    """AbstractCore's OWN three-way store merge. The Gateway restates no rules."""

    return dict(config_facade.merge_config_documents(baseline, mine, disk))


def _core_store_path() -> Optional[Path]:
    """THE Core store, resolved by AbstractCore's own resolver. Never re-derived.

    A second resolution -- even one that "looks the same" -- is how the two
    entry points came to serve two files. `capability_default_config_path`
    forwards to `abstractcore.config.manager.resolve_config_file`, so
    `ABSTRACTCORE_CONFIG_FILE` / `ABSTRACTCORE_CONFIG_DIR` and the default
    `~/.abstractcore/config/abstractcore.json` mean here exactly what they mean
    to `abstractcore config defaults`.
    """

    try:
        return Path(str(config_facade.capability_default_config_path())).expanduser()
    except Exception:
        return None


def _scoped_core_config_path(base_dir: Optional[Path]) -> Optional[Path]:
    """The per-USER overlay for this scope, or ``None`` for "the Core store".

    An overlay exists only for a per-user runtime under a user-auth Gateway.
    The gateway ROOT is deliberately not a scope: an admin editing a default
    edits the source, which is the whole of the operator's ruling. `None` here
    is what routes a read or a write to the Core store.
    """

    if base_dir is None:
        return None
    try:
        from .users import gateway_data_dir_from_env, gateway_user_auth_enabled

        if not gateway_user_auth_enabled():
            return None
        if _same_path(Path(base_dir), gateway_data_dir_from_env()):
            return None
        return _core_config_path_for_base_dir(base_dir)
    except Exception:
        return None


def _writable_scoped_core_config_path(base_dir: Optional[Path]) -> Optional[Path]:
    return _scoped_core_config_path(base_dir)


def _core_config_path_for_base_dir(base_dir: Path) -> Path:
    try:
        return Path(base_dir).expanduser().resolve() / "config" / "abstractcore.json"
    except Exception:
        return Path(base_dir) / "config" / "abstractcore.json"


def _save_core_config_route(
    path: Path,
    kind: str,
    modality: str,
    *,
    task: Optional[str] = None,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    reasoning: Optional[str] = None,
    options: Dict[str, Any],
) -> None:
    if not config_facade.set_capability_default(
        kind,
        modality,
        task=task,
        provider=provider,
        model=model,
        base_url=base_url,
        reasoning=reasoning,
        options=options,
        config_file=path,
        apply_env=False,
    ):
        # Belt and braces. AbstractCore raises `CapabilityDefaultWriteError`
        # (a ValueError, so the PUT route's handler turns it into a 400 with
        # the reason attached) rather than returning a reason-free False. This
        # branch stays for a store implementation that only says "no".
        suffix = f".{task}" if _clean(task) else ""
        raise ValueError(f"Failed to set capability default {kind}.{modality}{suffix}")


def _clear_core_config_route(path: Path, kind: str, modality: str, *, task: Optional[str] = None) -> None:
    if not config_facade.clear_capability_default(kind, modality, task=task, config_file=path, apply_env=False):
        suffix = f".{task}" if _clean(task) else ""
        raise ValueError(f"Failed to clear capability default {kind}.{modality}{suffix}")


def _same_path(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def _load_configured_routes_from_core_config(path: Path) -> Dict[str, Dict[str, Any]]:
    """The routes ONE SCOPE overrides with. An absent scope file overrides with nothing.

    THE SEED IS PER INSTALL, NOT PER SCOPE. AbstractCore seeds its recommended
    capability routes when a config file has never existed there (operator
    ruling 2026-08-01) so a fresh install works out of the box. This function
    is not an install read: it is the OVERLAY read that answers "does this
    scope -- this principal, this gateway runtime -- override the store below
    it?", and there the only honest answer for a file that does not exist is
    "no". Seeding here would make every newly created user silently shadow the
    operator's gateway-wide default with the framework recommendation, and the
    admin would have no way to set a default that new users inherit. The
    install-level read (`_local_core_payload`) still
    gets the seed, so a fresh gateway still works out of the box and its users
    still inherit the recommendation through the normal inheritance path.
    """
    try:
        if not Path(path).expanduser().exists():
            return {}
    except Exception:
        return {}
    try:
        routes: Dict[str, Dict[str, Any]] = {}
        for row in config_facade.list_capability_defaults(config_file=path, apply_env=False):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if key and bool(row.get("configured")):
                routes[key] = dict(row)
        return routes
    except Exception:
        return {}


def _apply_core_config_routes(payload: Dict[str, Any], *, config_path: Path, source: str) -> tuple[Dict[str, Any], bool]:
    configured_routes = _load_configured_routes_from_core_config(config_path)
    if not configured_routes:
        return payload, False

    try:
        specs = config_facade.capability_default_specs()
    except Exception:
        specs = {}

    existing_rows = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if key:
            rows_by_key[key] = dict(row)
    for key, route in configured_routes.items():
        row = dict(specs.get(key, {"key": key}))
        row.update(route)
        row["key"] = key
        parts = [part for part in key.split(".") if part]
        if len(parts) >= 2:
            row.setdefault("kind", parts[0])
            row.setdefault("modality", parts[1])
            if len(parts) >= 3:
                row.setdefault("task", parts[2])
        row["configured"] = True
        row["source"] = source
        rows_by_key[key] = row

    payload = dict(payload)
    payload["routes"] = [rows_by_key[key] for key in sorted(rows_by_key)]
    return payload, bool(configured_routes)


def _apply_scoped_core_defaults(payload: Dict[str, Any], *, base_dir: Optional[Path]) -> Dict[str, Any]:
    """Layer THIS USER's overlay over the one Core base. One layer, not two."""

    principal_path = _scoped_core_config_path(base_dir)
    if principal_path is None:
        return payload

    payload = dict(payload)
    payload.setdefault("principal_config_file", str(principal_path))
    payload, principal_applied = _apply_core_config_routes(
        payload,
        config_path=principal_path,
        source="abstractcore.runtime",
    )
    payload["principal_defaults"] = bool(principal_applied)
    payload["ok"] = bool(payload.get("ok", True))
    payload["writable"] = True
    if principal_applied:
        payload["authority"] = "abstractcore.runtime"
        payload["source"] = "abstractcore.runtime"
    return payload


def _core_server_json(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = core_server_base_url()
    if not base:
        raise RuntimeError("ABSTRACTCORE_SERVER_BASE_URL is not configured")
    url = core_server_url(base, path)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = core_server_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AbstractCore config route returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"AbstractCore config route unavailable: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("AbstractCore config route returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AbstractCore config route returned a non-object payload")
    return payload


def core_server_base_url() -> str:
    raw = os.getenv("ABSTRACTCORE_SERVER_BASE_URL")
    return raw.strip().rstrip("/") if isinstance(raw, str) and raw.strip() else ""


def core_server_token() -> str:
    for name in (
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTCORE_AUTH_TOKEN",
        "ABSTRACTCORE_SERVER_API_KEY",
    ):
        raw = os.getenv(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def core_server_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    clean_path = "/" + path.lstrip("/")
    if urllib.parse.urlsplit(base).path.rstrip("/").endswith("/v1"):
        return f"{base}{clean_path}"
    return f"{base}/v1{clean_path}"


def _core_server_base_url() -> str:
    return core_server_base_url()


def _core_server_token() -> str:
    return core_server_token()


def _core_server_url(base: str, path: str) -> str:
    return core_server_url(base, path)


def _split_route(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "." in raw:
        left, right = raw.split(".", 1)
        return left, right
    if ":" in raw:
        left, right = raw.split(":", 1)
        return left, right
    raise ValueError("Capability route must be written as kind.modality, for example output.text.")


def _clean(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None
