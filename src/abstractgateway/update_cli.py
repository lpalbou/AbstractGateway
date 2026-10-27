"""`abstractgateway update [--check] [--yes]` — upgrade this AbstractFramework install from a shell.

A shell command for the person at the keyboard, never the gateway process (the web console's
Update and the tray run `self_update`'s background job instead). It reuses that module's
pieces: how this gateway was installed (`detect_install`), the newest release and its pins
(`_fetch_framework_release`: the framework repo's `main` at one commit, its manifest and
its installer script), the sha-checked copy of the installer in `<data dir>/update/`
(`_write_installer_script`) and the exact command (`installer_command`).

What it does, in order:

1. Refuses, with the right instruction, when the AbstractFramework installer did not make this
   install (a source checkout, a container, a plain pip / uv / pipx install).
2. Prints what would change: every package whose installed version differs from the release's
   (the framework, the gateway, the Python packages, the browser apps, the terminal apps),
   then the root changelog sections between the installed release and the newest one.
   `--check` stops here: exit 0 when up to date, 10 when an update exists.
3. Asks once ("Upgrade now? [Y/n]"; `--yes` skips it), then runs the installer in the
   foreground, its output on this terminal: `sh install.sh --yes --no-open --no-modify-path
   --data-dir <this data dir>`. The installer keeps the port, profile and login choices it
   remembers and restarts the gateway service itself. On Windows the PowerShell installer
   runs in a new window, after this command exits (the running `abstractgateway` locks the
   files the installer replaces).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import self_update

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_UPDATE_AVAILABLE = 10

_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\][^\n]*$", re.MULTILINE)


def add_update_subparser(sub: Any) -> None:
    p = sub.add_parser(
        "update",
        help="Upgrade AbstractFramework: show what changes, then run the installer (--check: show only)",
        description=(
            "Upgrade this AbstractFramework install to the newest release. Shows the packages that would "
            "change and the release notes first, asks once, then runs the AbstractFramework installer "
            "here, in the foreground (it keeps your port, profile and login choices and restarts the "
            "gateway). Works for installs made with the one-line installer."
        ),
    )
    p.add_argument("--check", action="store_true", help="Only show what would change; exit 0 when up to date, 10 when an update exists")
    p.add_argument("--yes", "-y", action="store_true", help="Upgrade without asking (required when not run from a terminal)")
    p.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")


# ---------------------------------------------------------------------------
# What is installed, what the release has
# ---------------------------------------------------------------------------


def _installed_app_versions(data_dir: Path, app_ids: List[str]) -> Dict[str, Optional[str]]:
    """The browser apps the gateway installed in this data dir (the apps manager's record)."""
    try:
        from .apps_manager import AppsManager

        am = AppsManager(data_dir)
        return {app_id: am.installed_version(app_id) for app_id in app_ids}
    except Exception:  # noqa: BLE001 - an unreadable apps folder lists no app, never fails the update
        return {app_id: None for app_id in app_ids}


def _installed_crate_versions(data_dir: Path, names: List[str]) -> Dict[str, Optional[str]]:
    """The terminal apps on this computer (the gateway's own folder, PATH, ~/.cargo/bin)."""
    try:
        from .apps_manager import CODE_TUI, GATEWAY_CONSOLE_TUI, AppsManager

        am = AppsManager(data_dir)
        specs = {t.crate: t for t in (GATEWAY_CONSOLE_TUI, CODE_TUI)}
        out: Dict[str, Optional[str]] = {}
        for name in names:
            spec = specs.get(name)
            out[name] = am.tui_status(spec)["version"] if spec is not None else None
        return out
    except Exception:  # noqa: BLE001
        return {name: None for name in names}


def changes_table(
    inst: self_update.InstallInfo,
    rel: Dict[str, Any],
    *,
    python_versions: Optional[Dict[str, str]],
    app_versions: Dict[str, Optional[str]],
    crate_versions: Dict[str, Optional[str]],
) -> List[Dict[str, Any]]:
    """[{name, kind, from, to}] for everything whose installed version differs from the
    release's: the framework, the gateway, the other Python packages of the gateway's
    environment, the browser apps and the terminal apps. A package that is not installed
    (the Assistant, an app never installed) is not listed: the installer leaves it alone."""
    rows: List[Dict[str, Any]] = []
    installed_fw = inst.framework_version or None
    if installed_fw != rel["version"]:
        rows.append({"name": self_update.FRAMEWORK_NAME, "kind": "release", "from": installed_fw, "to": rel["version"]})
    env = {k.lower().replace("_", "-"): v for k, v in (python_versions or {}).items()}
    if not env:
        env = {self_update.DIST_NAME: inst.version}
    for name, latest in sorted((rel.get("python_packages") or {}).items(), key=lambda kv: (kv[0] != self_update.DIST_NAME, kv[0])):
        have = env.get(name)
        if have is not None and have != latest:
            rows.append({"name": name, "kind": "gateway" if name == self_update.DIST_NAME else "python package", "from": have, "to": latest})
    for app_id, latest in sorted((rel.get("npm_apps") or {}).items()):
        have = app_versions.get(app_id)
        if have is not None and have != latest:
            rows.append({"name": app_id, "kind": "browser app", "from": have, "to": latest})
    for name, latest in sorted((rel.get("crates") or {}).items()):
        have = crate_versions.get(name)
        if have is not None and have != latest:
            rows.append({"name": name, "kind": "terminal app", "from": have, "to": latest})
    return rows


def format_changes(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "  (nothing: every package is already at the release's version)"
    width = max(len(r["name"]) for r in rows)
    old_width = max(len(str(r.get("from") or "(none)")) for r in rows)
    new_width = max(len(str(r["to"])) for r in rows)
    lines = []
    for r in rows:
        lines.append(f"  {r['name']:<{width}}  {str(r.get('from') or '(none)'):>{old_width}} -> {str(r['to']):<{new_width}}   {r['kind']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Release notes (the root CHANGELOG.md at the release's commit)
# ---------------------------------------------------------------------------


def changelog_sections(text: str, *, installed: Optional[str], latest: str) -> List[str]:
    """The `## [X.Y.Z] - date` sections of the root changelog for every release newer than
    `installed` up to `latest`, verbatim, newest first (the file's order). No installed
    release recorded: the latest section only."""
    hits = list(_SECTION_RE.finditer(text))
    out: List[str] = []
    for i, m in enumerate(hits):
        version = m.group("version").strip()
        if version.lower() == "unreleased":
            continue
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        if installed:
            newer = self_update.is_newer(version, installed)
            within = self_update.is_newer(version, latest)
            if newer is not True or within is True:
                continue
        elif version != latest:
            continue
        out.append(text[m.start():end].rstrip() + "\n")
    return out


def fetch_release_notes(commit: str, *, installed: Optional[str], latest: str, http_get: Any = None) -> Dict[str, Any]:
    """{ok, sections, url, error}: the root changelog at the release's commit."""
    url = f"{self_update.FRAMEWORK_RAW}/{commit}/{self_update.ROOT_CHANGELOG_PATH}"
    get = http_get or self_update._http_get
    try:
        text = get(url, "text/plain", self_update.CHECK_TIMEOUT_S).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - offline, HTTP error: the notes are optional
        return {"ok": False, "sections": [], "url": url, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "sections": changelog_sections(text, installed=installed, latest=latest), "url": url, "error": None}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _checkout_path() -> Optional[str]:
    direct = self_update._direct_url_info() or {}
    url = str(direct.get("url") or "")
    if url.startswith("file:"):
        return urllib.parse.unquote(urllib.parse.urlparse(url).path) or None
    return None


def refusal_text(inst: self_update.InstallInfo) -> str:
    """One sentence saying why `update` does nothing here, and what to run instead."""
    one_liner = self_update.INSTALLER_ONE_LINER_WINDOWS if sys.platform.startswith("win") else self_update.INSTALLER_ONE_LINER
    if inst.kind == "editable":
        where = _checkout_path() or inst.prefix
        return f"This gateway runs from a checkout at {where}: pull and restart it."
    if inst.kind == "docker":
        return "This gateway runs in a container: pull the newer image and start it again."
    if inst.display_command:
        return (
            f"This gateway was not installed by the AbstractFramework installer ({inst.kind}), so `abstractgateway update` "
            f"does nothing here. To update it: {inst.display_command}. To switch to the installer-managed install, run: {one_liner}"
        )
    why = f" ({inst.reason})" if inst.reason else ""
    return f"This gateway was not installed by the AbstractFramework installer ({inst.kind}){why}. To install it that way, run: {one_liner}"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("", "y", "yes")


def run_installer(command: List[str], *, platform: str = sys.platform, run: Any = None) -> int:
    """Run the installer in the foreground, its output on this terminal; its exit code.

    Windows: the running `abstractgateway` (this process) holds the files the installer
    replaces, so the PowerShell installer starts in a new window and this command exits at
    once (0) so the installer finds nothing locked."""
    if platform.startswith("win"):
        flags = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        (run or subprocess.Popen)(command, creationflags=flags, close_fds=True)
        return EXIT_OK
    return int((run or subprocess.call)(command))


def windows_installer_command(script: Path, data_dir: str) -> List[str]:
    return [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-NoOpen", "-NoModifyPath", "-DataDir", str(data_dir),
    ]


def run_update(
    args: argparse.Namespace,
    *,
    detect: Any = None,
    fetch_release: Any = None,
    http_get: Any = None,
    snapshot: Any = None,
    run: Any = None,
    platform: str = sys.platform,
) -> int:
    from .first_run import local_gateway
    from .firstrun_cli import _apply_data_dir_flag
    from .host_paths import apply_data_dir_default

    _apply_data_dir_flag(getattr(args, "data_dir", None))
    data_dir = apply_data_dir_default().path
    inst = (detect or self_update.detect_install)(data_dir=data_dir, platform=platform)
    if inst.kind != "installer":
        print(refusal_text(inst), file=sys.stderr)
        return EXIT_REFUSED

    installed = inst.framework_version
    have = f"{self_update.FRAMEWORK_NAME} {installed}" if installed else f"{self_update.FRAMEWORK_NAME} (release not recorded) · gateway {inst.version}"
    print(f"Installed: {have}", flush=True)
    print("Checking the newest release...", file=sys.stderr, flush=True)
    rel = (fetch_release or self_update._fetch_framework_release)()
    if not rel.get("ok"):
        print(f"Could not check for a newer release: {rel.get('error') or 'unknown error'}", file=sys.stderr)
        return EXIT_ERROR
    # The same comparison every client makes (and the sha-checked installer kept for the run).
    check = self_update.check_for_update(force=True, info=inst, fetch_release=lambda: rel)
    if check.get("update_available") is None:
        print(f"Could not compare the versions: {check.get('error') or 'unknown error'}", file=sys.stderr)
        return EXIT_ERROR
    latest = str(rel["version"])
    if not check.get("update_available"):
        print(f"Up to date: {self_update.FRAMEWORK_NAME} {latest} (gateway {rel['gateway_version']}) is the newest release.")
        return EXIT_OK

    print(f"Available: {self_update.FRAMEWORK_NAME} {latest} (gateway {rel['gateway_version']})")
    print("")
    print("What changes:")
    python_versions = (snapshot or self_update._env_versions_fresh)(inst.python)
    app_versions = _installed_app_versions(Path(str(inst.data_dir)), list(rel.get("npm_apps") or {}))
    crate_versions = _installed_crate_versions(Path(str(inst.data_dir)), list(rel.get("crates") or {}))
    rows = changes_table(inst, rel, python_versions=python_versions, app_versions=app_versions, crate_versions=crate_versions)
    print(format_changes(rows))
    print("")
    notes = fetch_release_notes(str(rel["commit"]), installed=installed, latest=latest, http_get=http_get)
    if notes["ok"] and notes["sections"]:
        print("Release notes:")
        print("")
        print("\n".join(notes["sections"]).rstrip())
    elif notes["ok"]:
        print(f"Release notes: no entry for {latest} in {notes['url']}")
    else:
        print(f"Release notes: could not fetch them ({notes['error']}); see {notes['url']}", file=sys.stderr)
    print("")
    if args.check:
        print("An update exists (--check: nothing was changed). Run `abstractgateway update` to install it.")
        return EXIT_UPDATE_AVAILABLE

    if not args.yes:
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("Not a terminal: pass --yes to upgrade without asking.", file=sys.stderr)
            return EXIT_REFUSED
        if not _confirm("Upgrade now? [Y/n] "):
            print("Nothing changed.")
            return EXIT_OK

    # Everything the closing line needs, BEFORE the installer replaces this environment.
    console_url = str(local_gateway(Path(str(inst.data_dir)))["url"]).rstrip("/") + "/console"
    ddir = Path(str(inst.data_dir))
    update_dir = ddir / "update"
    if platform.startswith("win"):
        ps_url = f"{self_update.FRAMEWORK_RAW}/{rel['commit']}/{self_update.INSTALLER_SCRIPT_PATH_WINDOWS}"
        try:
            ps_bytes = (http_get or self_update._http_get)(ps_url, "text/plain", self_update.CHECK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            print(f"Could not download the installer ({ps_url}: {type(exc).__name__}: {exc})", file=sys.stderr)
            return EXIT_ERROR
        import hashlib

        script = _write_windows_installer(update_dir, ps_bytes, hashlib.sha256(ps_bytes).hexdigest())
        command = windows_installer_command(script, str(ddir))
        print("Running the AbstractFramework installer in a new PowerShell window; this command exits now so the")
        print("installer can replace the gateway's files. It stops the gateway, updates everything and starts it again.")
        print(f"Afterwards, the console is at {console_url}")
        return run_installer(command, platform=platform, run=run)

    sha = str(check["release"]["installer"]["sha256"])  # the script the check hashed: the one that runs
    script = self_update._write_installer_script(update_dir, bytes(rel["installer_bytes"]), sha)
    command = self_update.installer_command(script, str(ddir), flags=self_update.INSTALLER_CLI_FLAGS)
    print(f"Running the AbstractFramework installer: {' '.join(command)}")
    print("")
    code = run_installer(command, platform=platform, run=run)
    print("")
    if code == 0:
        print(f"Done. Console: {console_url}")
    else:
        print(f"The installer exited with code {code}; its log is the newest install-*.log in {ddir / 'logs'}.", file=sys.stderr)
    return code


def _write_windows_installer(folder: Path, data: bytes, sha256: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"install-{sha256[:12]}.ps1"
    path.write_bytes(data)
    return path
