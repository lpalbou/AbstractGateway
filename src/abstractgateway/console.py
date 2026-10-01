from __future__ import annotations

import functools
import html
import json
import re
import socket
from typing import Any, Dict, Tuple


def _script_json(value: Any) -> str:
    """JSON that is safe inside an inline <script> (no `</script>` break-out)."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def _core_console_parts() -> Dict[str, Any]:
    """AbstractCore's Models/Engines screens, fetched once per render.

    The screens are AbstractCore's own (`abstractcore.console.web`), reached
    through the one seam (`core_config`) like every other Core surface: the
    Gateway embeds them, it never re-implements them. When the installed
    AbstractCore predates them (or is missing) the tabs render a card that
    names the version needed instead -- an optional feature, not an error.
    """
    from . import core_config

    try:
        models = core_config.core_console_fragment("models")
        engines = core_config.core_console_fragment("engines")
        return {
            "available": True,
            "models_html": str(models.get("html") or ""),
            "engines_html": str(engines.get("html") or ""),
            # js/css are identical for both kinds: included ONCE.
            "js": str(models.get("js") or ""),
            "css": str(models.get("css") or ""),
            "message": "",
        }
    except Exception as exc:  # CoreTooOld, RuntimeError (Core missing), anything else
        try:
            support = core_config.core_models_engines_support()
        except Exception:
            support = {}
        installed = support.get("abstractcore_version") if isinstance(support, dict) else None
        required = (support.get("required") if isinstance(support, dict) else None) or "2.17.0"
        if isinstance(exc, NotImplementedError) or installed:
            have = f"This gateway has abstractcore {installed}." if installed else "This gateway's abstractcore is older."
        else:
            have = f"The screens could not be loaded ({type(exc).__name__}: {exc})."
        return {
            "available": False,
            "models_html": "",
            "engines_html": "",
            "js": "",
            "css": "",
            "installed": installed,
            "required": required,
            "message": f"Models and Engines require abstractcore \u2265 {required}. {have}",
            "upgrade": f'pip install -U "abstractcore>={required}"',
        }


def _core_console_unavailable_card(parts: Dict[str, Any], what: str) -> str:
    return (
        '<section class="core-console-unavailable" data-core-console="unavailable">'
        f"<h2>{html.escape(what)}</h2>"
        f'<p class="message warn">{html.escape(str(parts.get("message") or ""))}</p>'
        f'<p>Upgrade on the gateway host, then restart it: <code>{html.escape(str(parts.get("upgrade") or ""))}</code></p>'
        "</section>"
    )


# ---------------------------------------------------------------------------
# Source comments stay in the source
# ---------------------------------------------------------------------------
#
# The console's HTML, CSS and JS carry maintainer comments (design reasons,
# review findings, dates). They explain the code to whoever edits it and mean
# nothing to a browser, so the page is served without them. Only whole
# comments are removed: an HTML `<!-- ... -->`, a CSS `/* ... */`, and a JS
# comment that starts its own line (`// ...`, or a `/* ... */` block). A JS
# comment after code on the same line is left alone, because telling it apart
# from `//` inside a string or a regex needs a JS parser. The splice
# placeholders (`<!--__NAME__-->`, `/*__NAME__*/`) are kept.

_PLACEHOLDER_HTML = re.compile(r"<!--__[A-Z_]+__-->")
_PLACEHOLDER_BLOCK = re.compile(r"/\*__[A-Z_]+__\*/")
_HTML_COMMENT = re.compile(r"<!--(?!__[A-Z_]+__-->).*?-->", re.S)
_CSS_COMMENT = re.compile(r"/\*(?!__[A-Z_]+__\*/).*?\*/", re.S)
_BLANK_RUN = re.compile(r"\n(?:[ \t]*\n)+")
_SCRIPT_BLOCK = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", re.S)
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.S)


def _strip_css_comments(css: str) -> str:
    return _BLANK_RUN.sub("\n", _CSS_COMMENT.sub("", css))


def _strip_js_line_comments(js: str) -> str:
    out = []
    in_block = False
    for line in js.splitlines(keepends=True):
        text = line.strip()
        if in_block:
            if "*/" in line:
                in_block = False
                rest = line.split("*/", 1)[1]
                if rest.strip():
                    out.append(rest)
            continue
        if text.startswith("//"):
            continue
        if text.startswith("/*") and not _PLACEHOLDER_BLOCK.fullmatch(text):
            if "*/" not in text[2:]:
                in_block = True
                continue
            rest = text[2:].split("*/", 1)[1]
            if rest.strip():
                out.append(line[: len(line) - len(line.lstrip())] + rest.strip() + "\n")
            continue
        out.append(line)
    return "".join(out)


def _strip_html_comments(page: str) -> str:
    """HTML comments outside scripts, CSS comments in styles, JS line comments in scripts."""

    def markup(chunk: str) -> str:
        def style(m: "re.Match[str]") -> str:
            return m.group(1) + _strip_css_comments(m.group(2)) + m.group(3)

        pieces = []
        last = 0
        for m in _STYLE_BLOCK.finditer(chunk):
            pieces.append(_HTML_COMMENT.sub("", chunk[last:m.start()]))
            pieces.append(style(m))
            last = m.end()
        pieces.append(_HTML_COMMENT.sub("", chunk[last:]))
        return _BLANK_RUN.sub("\n", "".join(pieces))

    pieces = []
    last = 0
    for m in _SCRIPT_BLOCK.finditer(page):
        pieces.append(markup(page[last:m.start()]))
        pieces.append(m.group(1) + _strip_js_line_comments(m.group(2)) + m.group(3))
        last = m.end()
    pieces.append(markup(page[last:]))
    return "".join(pieces)


@functools.lru_cache(maxsize=1)
def _console_owned_sources() -> Tuple[str, str, str, str]:
    """(template, theme CSS, UI CSS, UI JS) without their source comments."""
    from .console_catalog import CATALOG_CSS, CATALOG_JS
    from .console_skills_mcp import SKILLS_MCP_CSS, SKILLS_MCP_JS
    from .console_themes import KIT_THEME_CSS
    from .console_ui import CONSOLE_UI_CSS, CONSOLE_UI_JS

    return (
        _strip_html_comments(_CONSOLE_HTML_TEMPLATE),
        _strip_css_comments(KIT_THEME_CSS),
        _strip_css_comments(CONSOLE_UI_CSS + CATALOG_CSS + SKILLS_MCP_CSS),
        _strip_js_line_comments(CONSOLE_UI_JS + CATALOG_JS + SKILLS_MCP_JS),
    )


def console_about_config() -> Dict[str, Any]:
    """The console's About facts, computed by the SERVING gateway at render
    time: `version` = the abstractgateway version this process runs (the
    `abstractgateway` field of GET /about), `rows` = the gateway-version
    rows of that same payload, formatted ONCE by AbstractCore's
    `gateway_version_rows` (the Python twin of ui-kit `gatewayVersionRows`,
    contract A-9 -- the islands bundle does not export the TS helper). A
    failure is ONE visible "Gateway: unavailable (<reason>)" row, never an
    empty About."""
    import abstractgateway

    try:
        from abstractcore.utils.identity import gateway_version_rows
    except Exception as exc:  # noqa: BLE001 - an old core must be named, not hidden
        return {
            "version": str(getattr(abstractgateway, "__version__", "") or "version not reported"),
            "rows": [["Gateway", f"unavailable (the installed AbstractCore has no identity module: {exc})"]],
        }
    try:
        from .routes.gateway import about_payload

        payload = about_payload()
        rows = gateway_version_rows(payload)
        version = str(payload.get("abstractgateway") or "")
    except Exception as exc:  # noqa: BLE001 - one visible row, never an empty About
        rows = gateway_version_rows(None, f"{type(exc).__name__}: {exc}")
        version = ""
    if not version:
        version = str(getattr(abstractgateway, "__version__", "") or "version not reported")
    return {"version": version, "rows": [[str(k), str(v)] for k, v in rows]}


def gateway_console_html() -> str:
    """The served console page. Theme CSS + the theme list are spliced from
    `console_themes.py` — the generated verbatim copy of the abstractuic
    kit's theme.css/THEME_SPECS (console_theme_sync; uic card 0023) — so the
    console serves exactly the framework's themes, never a local fork.

    AbstractCore's Models/Engines screens are spliced LAST, on placeholders
    that are pure `str.replace` tokens (the template is never `.format`-ed),
    so nothing in the fragment -- braces, `$`, a `</script>` in a string --
    can re-enter the template or end the host script early."""
    from .console_islands import ISLANDS_CSS, ISLANDS_JS
    from .console_themes import KIT_THEME_SPECS

    template, theme_css, ui_css, ui_js = _console_owned_sources()

    parts = _core_console_parts()
    config = {
        "available": bool(parts["available"]),
        "hostName": socket.gethostname() or "gateway host",
        "message": parts.get("message") or "",
        "upgrade": parts.get("upgrade") or "",
        "installed": parts.get("installed"),
    }
    if parts["available"]:
        catalog_body = parts["models_html"]
        engines_body = parts["engines_html"]
        script = (
            '<script id="abstractcore-console-js">\n'
            + parts["js"].replace("</script", "<\\/script")
            + "\n</script>"
        )
        css = parts["css"]
    else:
        catalog_body = _core_console_unavailable_card(parts, "Models")
        engines_body = _core_console_unavailable_card(parts, "Engines")
        script = ""
        css = ""
    page = (
        template
        .replace("/*__KIT_THEME_CSS__*/", theme_css)
        .replace("__KIT_THEME_SPECS_JSON__", json.dumps(KIT_THEME_SPECS, ensure_ascii=False))
        .replace("__CORE_CONSOLE_CONFIG_JSON__", _script_json(config))
        .replace("__GATEWAY_ABOUT_JSON__", _script_json(console_about_config()))
    )
    # Fragment content last, each placeholder exactly once. The layer and the
    # kit islands go first (they are console-owned); `<script`/`</` inside the
    # React bundle are hex-escaped (valid in any JS string/regex context) so
    # nothing in it can close or open a script element.
    islands_js = (
        ISLANDS_JS.replace("<script", "\\x3Cscript").replace("</script", "\\x3C/script").replace("<!--", "\\x3C!--")
    )
    for token, value in (
        ("/*__AF_KIT_CSS__*/", ISLANDS_CSS.replace("</style", "<\\/style")),
        # The model catalog cards (console_catalog.py) ride with the UI layer:
        # same sheet, same script scope.
        ("/*__CONSOLE_UI_CSS__*/", ui_css),
        ("/*__CONSOLE_UI_JS__*/", ui_js),
        ("/*__AF_CONSOLE_ISLANDS_JS__*/", islands_js),
        ("/*__ABSTRACTCORE_FRAGMENT_CSS__*/", css.replace("</style", "<\\/style")),
        ("<!--__ABSTRACTCORE_CATALOG_HTML__-->", catalog_body),
        ("<!--__ABSTRACTCORE_ENGINES_HTML__-->", engines_body),
        ("<!--__ABSTRACTCORE_FRAGMENT_SCRIPT__-->", script),
    ):
        head, sep, tail = page.partition(token)
        if not sep:
            raise RuntimeError(f"console template lost its {token} placeholder")
        page = head + value + tail
    return page


_CONSOLE_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
  <meta name="theme-color" content="#1a1a2e">
  <title>AbstractGateway Console</title>
  <!-- abstractuic ui-kit component CSS (console_islands.py, generated from the
       kit's theme.css minus the per-theme blocks console_themes.py carries):
       the islands below render the kit's real components with the kit's
       own styles. The console's own sheet follows and may refine layout. -->
  <style id="af-kit-css">/*__AF_KIT_CSS__*/</style>
  <style>
	    /* DESIGN TOKENS (the console looks like
	       every AbstractFramework app): the token values below are the abstractuic
	       ui-kit's :root VERBATIM (theme.css — the palette flow/observer/
	       continuum share). The console's historical var names (--panel,
	       --line, --button, ...) become ALIASES onto the charter tokens so
	       every existing rule inherits the family look without a rewrite. */
	    :root {
	      color-scheme: dark;
	      /* Charter tokens (ui-kit theme.css values, verbatim) */
	      --bg-primary: #1a1a2e;
	      --bg-secondary: #16213e;
	      --bg-tertiary: #0f3460;
	      --text-primary: #eee;
	      --text-secondary: #aaa;
	      --text-muted: #666;
	      --accent: #e94560;
	      --success: #27ae60;
	      --warning: #f39c12;
	      --error: #e74c3c;
	      --info: #60a5fa;
	      --accent-subtle: rgba(233, 69, 96, 0.12);
	      --info-subtle: rgba(96, 165, 250, 0.12);
	      --ui-surface-1: rgba(0, 0, 0, 0.16);
	      --ui-surface-2: rgba(255, 255, 255, 0.06);
	      --ui-surface-3: rgba(0, 0, 0, 0.25);
	      --ui-border-1: rgba(255, 255, 255, 0.10);
	      --ui-border-2: rgba(255, 255, 255, 0.14);
	      --ui-shadow-1: 0 10px 30px rgba(0, 0, 0, 0.35);
	      /* Console aliases (historical names -> charter tokens). DERIVED
	         (color-mix), never hand-tuned per theme: every alias re-resolves
	         against whichever kit theme block is active, so all 21 kit themes
	         style the console without per-theme console CSS (this replaced
	         a hand-tuned 6-theme fork). */
	      --bg: var(--bg-primary);
	      --panel: var(--bg-secondary);
	      --panel-2: color-mix(in srgb, var(--bg-secondary) 92%, black);
	      --panel-3: var(--bg-tertiary);
	      --line: var(--bg-tertiary);
	      --line-soft: var(--ui-border-1);
	      --text: var(--text-primary);
	      --muted: var(--text-secondary);
	      --subtle: color-mix(in srgb, var(--text-secondary) 60%, var(--text-muted));
	      --accent-2: var(--info);
	      --danger: var(--error);
	      --danger-2: color-mix(in srgb, var(--error) 76%, black);
	      --ok: var(--success);
	      --warn: var(--warning);
	      --cyan: var(--info);
	      /* Primary actions wear the family accent (continuum button.primary);
	         secondary actions sit on the tertiary surface. */
	      --button: var(--accent);
	      --button-2: var(--bg-tertiary);
	      --shadow: var(--ui-shadow-1);
	      --font-scale: 1;
	      --header-density: 1;
	      --font-base: calc(14px * var(--font-scale));
	      --font-sm: calc(12px * var(--font-scale));
	      --font-xs: calc(11px * var(--font-scale));
	      --font-caption: calc(11px * var(--font-scale));
	      --accent-primary: var(--accent);
	      --font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
	      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
	      --font-size-xs: var(--font-xs);
	      --font-size-sm: var(--font-sm);
	      --font-size-md: calc(13px * var(--font-scale));
	      --font-size-base: var(--font-base);
	      --font-size-lg: calc(16px * var(--font-scale));
	      --font-size-xl: calc(18px * var(--font-scale));
	      --radius-sm: 4px;
	      --radius-md: 8px;
	      --radius-lg: 10px;
	    }
	    /* Touch floor (DESIGN-v2 §0 item 11, round-1 leftover "13 px on touch"): the
	       kit's own (pointer: coarse) block runs BEFORE this sheet, and the :root block
	       above redefines the small scale steps after it, so on a phone every helper,
	       note and table cell set in --font-size-sm/-md rendered at 12-13 px. The
	       floor sits on the TOKENS (not on a list of selectors): reading text is never
	       below 14 px on touch; the xs step (pills, eyebrow captions) never below 13. */
	    @media (pointer: coarse) {
	      :root {
	        --font-sm: max(14px, calc(12px * var(--font-scale)));
	        --font-xs: max(13px, calc(11px * var(--font-scale)));
	        --font-size-md: max(14px, calc(13px * var(--font-scale)));
	        --font-caption: max(14px, calc(11px * var(--font-scale)));
	      }
	    }
	    /* Per-theme token blocks: the abstractuic kit's theme.css VERBATIM
	       (all 21 themes), generated by console_theme_sync — the console's
	       aliases above derive everything else per theme. Never hand-edit a
	       theme here; edit the kit and re-run the sync (uic card 0023). */
/*__KIT_THEME_CSS__*/
	    * { box-sizing: border-box; }
	    /* No iOS/Android text inflation (DESIGN §2): the kit sheet carries it
	       too; repeated here so the console never depends on the islands. */
	    html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
	    html, body { height: 100%; }
	    body {
	      margin: 0;
	      background: var(--bg);
	      color: var(--text);
	      font: var(--font-base)/1.5 var(--font-sans);
	      -webkit-font-smoothing: antialiased;
	      overflow: hidden;
	      /* The shell scrolls inside; pull-to-refresh must not drag it. */
	      overscroll-behavior-y: none;
	    }
	    body.font-sm { --font-scale: .92; }
	    body.font-lg { --font-scale: 1.08; }
	    body.header-compact { --header-density: .84; }
	    body.header-large { --header-density: 1.18; }
	    /* ---- FAMILY SHELL (.shell_* — continuum/observer's redesigned layout
	       vocabulary, styles.css shell block): left sidebar + slim header,
	       content scrolls internally. ---- */
	    /* --vh-full (kit token: 100dvh where supported): 100vh is taller than
	       the visible area on iOS, which hid the bottom of every tab. */
	    .shell { display: flex; flex-direction: row; height: var(--vh-full, 100vh); min-width: 0; }
	    .shell_sidebar {
	      flex: 0 0 196px;
	      display: flex;
	      flex-direction: column;
	      background: var(--bg-secondary);
	      border-right: 1px solid var(--bg-tertiary);
	      min-height: 0;
	    }
	    body:not(.signed-in) .shell_sidebar { display: none; }
	    .shell_brand { display: flex; align-items: center; gap: 8px; padding: 14px 14px 10px; min-width: 0; }
	    .shell_brand_mark { font-size: var(--font-size-xl); line-height: 1; color: var(--accent); font-weight: 700; }
	    .shell_brand_name { font-weight: 700; letter-spacing: .3px; white-space: nowrap; font-size: var(--font-size-md); }
	    .shell_nav { display: flex; flex-direction: column; gap: 2px; padding: 6px 8px; flex: 1 1 auto; min-height: 0; overflow-y: auto; }
	    .shell_nav_icon { display: inline-flex; flex: 0 0 auto; width: 16px; justify-content: center; font-size: var(--font-size-md); opacity: .8; }
	    .shell_nav_label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	    /* Groups (DESIGN-v2 §1): the kit's .af-nav-group / __caption carry the look
	       (small uppercase muted eyebrow); the console only spaces them. */
	    .shell_nav_group + .shell_nav_group { margin-top: 12px; }
	    .shell_nav_caption { padding: 4px 10px 2px; }
	    .shell_sidebar_foot { display: flex; flex-direction: column; align-items: stretch; gap: 8px; }
	    .shell_sidebar_foot button.shell_setup { justify-content: flex-start; gap: 10px; font-size: var(--font-size-md); }
	    .shell_main { flex: 1 1 auto; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	    .shell_header {
	      flex: 0 0 auto;
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 12px;
	      min-height: calc(52px * var(--header-density));
	      padding: max(8px, var(--safe-top, 0px)) max(16px, var(--safe-right, 0px)) 8px max(16px, var(--safe-left, 0px));
	      border-bottom: 1px solid var(--bg-tertiary);
	      background: var(--bg-secondary);
	      /* The top-right cluster wraps under the title instead of pushing the
	         page wider than the phone (it was 418 px wide at 375). */
	      flex-wrap: wrap;
	      row-gap: 6px;
	      min-width: 0;
	    }
	    .shell_header > .af-topbar-island, .shell_header > .af-topbar { margin-left: auto; min-width: 0; max-width: 100%; }
	    .shell_header .af-topbar { flex-wrap: wrap; justify-content: flex-end; row-gap: 6px; }
	    @media (max-width: 767.98px) {
	      .shell_header { gap: 8px 10px; }
	      #page-subtitle { display: none; }
	    }
	    @media (max-height: 500px) {
	      /* Phone landscape: one thin header row. */
	      .shell_header { min-height: 0; padding-top: max(4px, var(--safe-top, 0px)); padding-bottom: 4px; flex-wrap: nowrap; }
	      #page-subtitle { display: none; }
	    }
	    .shell_header_titles { min-width: 0; flex: 1 1 auto; }
	    .shell_content { flex: 1 1 auto; min-height: 0; min-width: 0; overflow-y: auto; overscroll-behavior: contain; }
	    /* Sidebar -> drawer below md (DESIGN §5.2). The nav toggle and the
	       backdrop exist only for this range; above it the sidebar is the
	       docked column it always was. */
	    .shell_nav_toggle, .shell_nav_backdrop { display: none; }
	    .shell_nav_close { display: none; }
	    @media (max-width: 1023.98px) {
	      .shell_sidebar {
	        position: fixed; top: 0; bottom: 0; left: 0; z-index: var(--z-drawer, 900);
	        width: min(288px, 86vw);
	        padding-top: var(--safe-top, 0px); padding-bottom: var(--safe-bottom, 0px); padding-left: var(--safe-left, 0px);
	        box-shadow: var(--shadow);
	        transform: translateX(-105%);
	        visibility: hidden;
	        transition: transform 180ms ease, visibility 0s linear 180ms;
	      }
	      body.nav-open .shell_sidebar { transform: none; visibility: visible; transition: transform 180ms ease; }
	      body.signed-in .shell_nav_toggle { display: inline-flex; }
	      body.nav-open .shell_nav_backdrop {
	        display: block; position: fixed; inset: 0; z-index: calc(var(--z-drawer, 900) - 1);
	        background: rgba(0, 0, 0, .45);
	      }
	      .shell_nav_close { display: inline-flex; margin-left: auto; }
	      .tab-button.shell_nav_item { min-height: var(--tap-min, 32px); }
	    }
	    @media (max-width: 1023.98px) and (prefers-reduced-motion: reduce) {
	      .shell_sidebar, body.nav-open .shell_sidebar { transition: none; }
	    }
	    .shell_nav_toggle, .shell_nav_close {
	      align-items: center; justify-content: center; flex: 0 0 auto;
	      width: var(--tap-min, 32px); height: var(--tap-min, 32px); min-height: 0; padding: 0;
	      border: 1px solid var(--line); border-radius: var(--radius-md);
	      background: var(--panel-2); color: var(--text); font-size: 16px; line-height: 1;
	    }
	    .shell_nav_close { border: 0; background: transparent; color: var(--subtle); font-size: 20px; }
    /* Headings scale with the Appearance font-size control (hardcoded
       px never scaled) and cap at weight 650 —
       when everything is 800, nothing leads. */
    h1 { font-size: calc(17px * var(--font-scale)); margin: 0; letter-spacing: -.01em; font-weight: 650; }
    h2 { font-size: calc(15px * var(--font-scale)); margin: 0; letter-spacing: -.01em; font-weight: 600; }
    h3 { font-size: calc(13px * var(--font-scale)); margin: 0; letter-spacing: -.01em; font-weight: 600; }
    p { margin: 0; }
	    main { width: 100%; max-width: 1560px; margin: 0 auto; padding: 20px 22px 28px; }
	    @media (max-width: 767.98px) {
	      main { padding: var(--gutter, 16px) max(var(--gutter, 16px), var(--safe-right, 0px)) max(28px, var(--safe-bottom, 0px)) max(var(--gutter, 16px), var(--safe-left, 0px)); }
	      section { padding: 14px; }
	    }
	    #page-title { font-size: var(--font-size-lg); font-weight: 700; }
    .brand-subtitle { color: var(--subtle); font-size: var(--font-size-sm); margin-top: 1px; }
    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      min-width: 0;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); display: inline-block; }
    .dot.ok { background: var(--ok); }
    .dot.bad { background: var(--danger); }
    body.signed-in #status-dot { display: none; } /* the account pill carries the green dot; two dots read wrong */
    /* --- .af-topbar / .af-drawer: abstractuic ui-kit CSS PUBLIC API, vendored
       for this server-rendered page (ui-kit/src/theme.css is the source of
       truth; class names + markup shape are the contract, visual tokens are
       mapped onto the console's own variables). --- */
    .af-topbar { display: flex; align-items: center; gap: 8px; }
    .af-topbar__btn {
      display: inline-flex; align-items: center; justify-content: center;
      width: 30px; height: 30px; padding: 0;
      border: 1px solid var(--line); border-radius: var(--radius-md);
      background: var(--panel-2); color: var(--muted); cursor: pointer; font-size: 14px;
    }
    .af-topbar__btn:hover { color: var(--text); border-color: var(--border, var(--line)); }
    .af-topbar__btn.is-active {
      color: var(--text);
      background: color-mix(in srgb, var(--accent) 22%, var(--panel-2));
      border-color: color-mix(in srgb, var(--accent) 55%, transparent);
    }
    .af-topbar__pill {
      display: inline-flex; align-items: center; gap: 7px; min-height: 30px; padding: 4px 12px;
      border: 1px solid var(--line); border-radius: 999px;
      background: var(--panel-2); color: var(--text);
      font: inherit; font-size: var(--font-sm); font-weight: 600; cursor: pointer; white-space: nowrap;
    }
    .af-topbar__pill:hover:not(:disabled) { border-color: var(--border, var(--line)); filter: brightness(1.08); }
    .af-topbar__pill:disabled { opacity: .6; cursor: progress; }
    .af-topbar__dot { width: 8px; height: 8px; border-radius: 999px; flex: 0 0 auto; }
    .af-topbar__dot--connected { background: var(--ok); }
    .af-topbar__dot--disconnected { background: var(--danger); }
    .af-topbar__dot--loading { background: var(--warn); }
    .af-drawer {
      position: fixed; top: 0; right: 0; bottom: 0; z-index: 900;
      display: flex; flex-direction: column; max-width: 100vw;
      border-left: 1px solid var(--line); background: var(--panel);
      box-shadow: var(--shadow); color: var(--text); font-family: var(--font-sans);
    }
    /* Full width below sm with the notch/home-indicator insets (DESIGN §5.2;
       the kit's own .af-drawer rule, mirrored for this vendored markup). */
    .af-drawer { height: var(--vh-full, 100vh); padding-top: var(--safe-top, 0px); padding-bottom: var(--safe-bottom, 0px); padding-right: var(--safe-right, 0px); }
    @media (max-width: 767.98px) { .af-drawer { width: 100vw !important; border-left: 0; padding-left: var(--safe-left, 0px); } }
    .af-drawer__header {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 12px 14px; border-bottom: 1px solid var(--line);
    }
    .af-drawer__title { font-size: var(--font-base); font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .af-drawer__header-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
    .af-drawer__close {
      display: inline-flex; align-items: center; justify-content: center;
      width: 26px; height: 26px; padding: 0; border: 0; border-radius: var(--radius-sm);
      background: transparent; color: var(--subtle); font-size: 18px; line-height: 1; cursor: pointer;
    }
    .af-drawer__close:hover { color: var(--text); background: var(--panel-2); }
    .af-drawer__body { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: auto; overscroll-behavior: contain; }
    @media (pointer: coarse) { .af-drawer__close { width: var(--tap-min, 44px); height: var(--tap-min, 44px); } }
    /* Console assistant drawer internals (console-owned, not kit API) */
    .assistant-messages { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; gap: 10px; padding: 14px; }
    .assistant-msg { border: 1px solid var(--line); border-radius: var(--radius-md); padding: 9px 11px; font-size: var(--font-sm); line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
    .assistant-msg.user { background: var(--info-subtle); align-self: flex-end; max-width: 88%; }
    .assistant-msg.assistant { background: var(--panel-2); align-self: flex-start; max-width: 94%; }
    .assistant-msg.error { background: color-mix(in srgb, var(--danger) 14%, var(--panel-2)); border-color: color-mix(in srgb, var(--danger) 45%, var(--line)); }
    .assistant-msg.pending { color: var(--subtle); font-style: italic; }
    .assistant-note { color: var(--subtle); font-size: var(--font-xs); padding: 0 14px 6px; }
    .assistant-composer { display: flex; gap: 8px; padding: 10px 14px 14px; border-top: 1px solid var(--line); }
    .assistant-composer textarea { flex: 1; resize: vertical; min-height: 44px; max-height: 160px; }
    .assistant-composer { padding-bottom: max(14px, var(--safe-bottom, 0px)); }
		    .workspace-shell { display: grid; gap: 16px; }
		    /* Sidebar nav rows (the continuum .shell_nav_item recipe) — the
		       tab-button class + ids survive so the wiring and tests hold. */
		    .tab-button.shell_nav_item {
		      display: flex;
		      align-items: center;
		      gap: 10px;
		      padding: 8px 10px;
		      min-height: 0;
		      border: none;
		      border-radius: var(--radius-md);
		      background: transparent;
		      color: var(--text-secondary);
		      cursor: pointer;
		      text-align: left;
		      font-size: var(--font-size-md);
		      font-weight: 500;
		      min-width: 0;
		      justify-content: flex-start;
		    }
	    .tab-button.shell_nav_item:hover {
	      background: rgba(148, 163, 184, 0.1);
	      color: var(--text);
	      filter: none;
	    }
	    /* The active row must survive a mouse pass: hover is wash-only,
	       active carries weight + the stronger wash. */
	    .tab-button.shell_nav_item.active {
	      background: rgba(148, 163, 184, 0.16);
	      color: var(--text);
	      font-weight: 600;
	    }
	    .tab-panel { display: none; }
	    .tab-panel.active { display: block; }
	    .tab-grid {
	      display: grid;
	      grid-template-columns: minmax(330px, 430px) minmax(0, 1fr);
	      gap: 18px;
	      align-items: start;
	    }
	    .tab-grid-wide { grid-template-columns: minmax(0, 1fr); }
	    .tab-stack { display: grid; gap: 16px; }
	    .session-summary {
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 12px;
	      flex-wrap: wrap;
	      padding: 10px 12px;
	      margin-bottom: 14px;
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-md);
	      background: color-mix(in srgb, var(--text) 3%, transparent);
	      color: var(--muted);
	    }
	    /* Cards are flat; depth belongs to overlays (
	       every section wore a modal's 48px shadow — a page of floating slabs). */
	    section {
	      border: 1px solid var(--line-soft);
	      background: var(--panel);
	      border-radius: var(--radius-md);
	      padding: 16px;
	      box-shadow: var(--shadow-card, 0 1px 2px rgba(0, 0, 0, .10));
    }
    .section-head { display: flex; align-items: start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .section-title { display: flex; align-items: center; gap: 9px; }
    .section-icon {
      width: 28px;
      height: 28px;
      display: inline-grid;
      place-items: center;
      border: 1px solid var(--line-soft);
      border-radius: var(--radius-md);
      color: var(--accent);
      background: var(--accent-subtle);
      font-size: 14px;
      flex: 0 0 auto;
    }
    .section-note { color: var(--muted); font-size: var(--font-size-sm); max-width: 620px; }
    /* AUTHORITY LINE (one store, one owner). A panel that edits a
       store it does not own must say so PERSISTENTLY — not in a toast the
       operator dismissed, and not only after a failure. It sits under the
       panel note, quieter than the note (it is provenance, not
       instruction), and names the file so the answer to "what did I just
       change" is on screen. Wider than .section-note because a config
       path is long and must not wrap mid-path. */
    .authority-note { color: var(--subtle); font-size: var(--font-size-xs); max-width: 900px; margin-top: 4px; overflow-wrap: anywhere; min-width: 0; }
    .authority-note code { padding: 1px 5px; font-size: var(--font-size-xs); overflow-wrap: anywhere; word-break: break-all; white-space: normal; }
    .authority-note.authority-readonly { color: var(--warn); }
    label {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
      color: var(--muted);
      /* 600, not 800 — labels must not compete (the kit's own reverted lesson). */
      font-weight: 600;
      text-transform: uppercase;
      font-size: var(--font-caption);
      letter-spacing: .04em;
    }
    /* Inputs: the family recipe (continuum styles.css) — surface-3 body,
       radius 10, accent focus ring with the subtle glow. */
    input, select, textarea {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--ui-border-1);
      border-radius: var(--radius-lg);
      background: var(--ui-surface-3);
      color: var(--text-primary);
      padding: 8px 10px;
      font: inherit;
      transition: border-color .15s, box-shadow .15s, background .15s;
    }
    select {
      min-height: 34px;
      line-height: 1.2;
      padding: 6px 30px 6px 9px;
    }
    select:not([multiple]) {
      appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, var(--text-secondary) 50%),
        linear-gradient(135deg, var(--text-secondary) 50%, transparent 50%);
      background-position:
        calc(100% - 14px) 50%,
        calc(100% - 9px) 50%;
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
    }
    textarea { min-height: 76px; resize: vertical; }
    input[type="checkbox"], input[type="radio"] { width: auto; min-height: auto; }
    /* Theme-aware focus ring (the old ring was
       hardcoded cyan and stayed cyan in every theme). */
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--info);
      box-shadow: 0 0 0 3px var(--info-subtle);
    }
    button:focus-visible { outline: 2px solid color-mix(in srgb, var(--accent) 65%, transparent); outline-offset: 1px; }
    .field-help { color: var(--subtle); font-size: var(--font-size-sm); line-height: 1.35; text-transform: none; letter-spacing: 0; font-weight: 600; }
    .inline { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }
    .inline > label { flex: 1 1 150px; }
    button {
      border: 0;
      border-radius: var(--radius-md);
      min-height: 36px;
      padding: 8px 12px;
      background: var(--button);
      color: white;
      font-weight: 600;
      font-size: var(--font-size-md);
      cursor: pointer;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease, filter 120ms ease;
    }
    /* Interaction states existed NOWHERE on the page's own buttons (so
       every click felt dead) — the ui-kit's measured 120ms
       polish block, applied. */
    button:hover:not(:disabled) { filter: brightness(1.08); }
    button:active:not(:disabled) { filter: brightness(0.94); }
    @media (prefers-reduced-motion: reduce) { button { transition: none; } }
	    button.secondary { background: var(--button-2); color: var(--text); }
    /* Tinted danger, not solid maroon (a table of
       filled red pills = alarm fatigue; red weight belongs to confirm-gated
       acts, carried by the inset ring + tint, readable in both themes). */
    button.danger {
      background: color-mix(in srgb, var(--danger) 14%, var(--panel-2));
      color: var(--text);
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--danger) 45%, transparent);
    }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .button-icon { font-size: 14px; line-height: 1; display: inline-flex; align-items: center; }
    /* Registry SVGs (card 015 wave 3): stroke icons inherit currentColor so
       every button/chip state tints its glyph for free. */
    .button-icon svg, .section-icon svg, .chip-icon svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
    .section-icon svg { width: 17px; height: 17px; }
    .chip-icon { display: inline-flex; align-items: center; margin-right: 5px; }
    .chip-icon svg { width: 12px; height: 12px; }
    table { width: 100%; border-collapse: collapse; }
    th { border-bottom: 1px solid var(--ui-border-2); }
    td { border-bottom: 1px solid var(--line-soft); }
    th, td { padding: 9px 10px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: var(--font-size-xs); text-transform: uppercase; letter-spacing: .04em; }
    /* Polarity-safe hover wash (the white literal
       was invisible on the light theme). */
    tbody tr:hover { background: color-mix(in srgb, var(--text) 4%, transparent); }
    code { background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 2px 6px; }
    .muted { color: var(--muted); }
    .message { margin-top: 12px; color: var(--muted); overflow-wrap: anywhere; }
    /* Error text derives from the danger token (the old #ff9caf pale pink was
       unreadable on white). */
    .message.error { color: color-mix(in srgb, var(--danger) 72%, var(--text)); }
    .message.ok { color: var(--ok); }
    .issued { margin: 12px 0; border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent); background: color-mix(in srgb, var(--accent) 8%, transparent); border-radius: var(--radius-md); padding: 10px; overflow-wrap: anywhere; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	    .hidden { display: none !important; }
	    body:not(.signed-in) .console-shell {
	      display: grid;
	      place-items: center;
	      padding-block: clamp(24px, 7vh, 72px);
	    }
	    body:not(.signed-in) .session-only { display: none !important; }
	    body.signed-in #login-section { display: none !important; }
	    /* ONE chip recipe (the kit's measured AA color-mix
	       derivation, mapped over every pill family — class names and state
	       words kept as they were; families now differ only in their state
	       color variables, never in shape/typography). */
	    .pill, .badge, .state-pill, .entity-chip, .entity-warn-pill, .entity-live-badge {
	      display: inline-flex; align-items: center; gap: 6px;
	      border: 1px solid var(--line); border-radius: 999px;
	      padding: 2px 8px; color: var(--muted);
	      font-size: var(--font-size-sm); white-space: nowrap;
	    }
	    .pill { padding: 3px 8px; margin: 0 6px 6px 0; }
	    .state-pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--success) 34%, transparent); background: color-mix(in srgb, var(--success) 8%, transparent); }
	    .state-pill.off { color: var(--warn); border-color: color-mix(in srgb, var(--warning) 35%, transparent); background: color-mix(in srgb, var(--warning) 8%, transparent); }
	    .state-pill.covered { color: var(--cyan); border-color: color-mix(in srgb, var(--info) 36%, transparent); background: color-mix(in srgb, var(--info) 8%, transparent); }
	    .capability-derived td { color: var(--muted); }
	    .actions { display: flex; gap: 6px; flex-wrap: nowrap; align-items: center; }
    th:last-child, td:last-child { width: 1%; white-space: nowrap; }
    /* In-table actions are GHOSTS (filled tertiary
       pills at table density turned every table into a wall of blue —
       continuum's in-table .btn recipe). Standalone secondaries keep the
       filled look. */
    .actions button.secondary { background: var(--ui-surface-2); border: 1px solid var(--ui-border-1); color: var(--text-primary); }
    .actions button.secondary:hover:not(:disabled) { background: rgba(255, 255, 255, .08); border-color: var(--ui-border-2); filter: none; }
    .actions button.danger { color: var(--error); }
    .actions select { width: auto; min-width: 150px; }
    .empty { color: var(--muted); padding: 12px 8px; }
    .danger-text { color: color-mix(in srgb, var(--danger) 72%, var(--text)); }
	    /* ---- Summoned Entities ---- */
	    .entity-chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 10px; }
	    /* Chip family deltas only — the shared .chip recipe above owns shape. */
	    .entity-chip { padding: 3px 10px; background: color-mix(in srgb, var(--text) 3%, transparent); }
	    .entity-chip-locked { color: var(--text-secondary); border-color: color-mix(in srgb, var(--accent) 30%, transparent); background: var(--accent-subtle); }
	    .entity-advanced { border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 10px 12px; margin: 10px 0; background: color-mix(in srgb, var(--text) 2%, transparent); }
	    .entity-advanced summary { cursor: pointer; color: var(--muted); font-size: var(--font-size-md); }
	    .entity-config-block { margin: 12px 0 4px; }
	    .entity-config-group {
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-md);
	      padding: 12px 14px;
	      margin: 12px 0;
	      background: var(--ui-surface-1);
	    }
	    .entity-config-group-danger { border-color: color-mix(in srgb, var(--danger) 30%, transparent); }
	    .entity-config-group .entity-config-title { margin-top: 0; }
	    .entity-config-title { font-size: var(--font-size-md); margin: 10px 0 6px; }
	    .entity-config-hint { color: var(--muted); font-weight: 400; font-size: var(--font-size-sm); }
	    .entity-danger-title { color: color-mix(in srgb, var(--danger) 72%, var(--text)); }
	    .entity-matrix { overflow-x: auto; margin: 8px 0; }
	    .entity-matrix-table { border-collapse: collapse; min-width: 420px; }
	    .entity-matrix-table th, .entity-matrix-table td { border: 1px solid var(--line); padding: 5px 10px; font-size: var(--font-size-sm); text-align: center; }
	    .entity-matrix-table tbody th { text-align: left; font-weight: 500; color: var(--text); }
	    .entity-matrix-table thead th { color: var(--muted); text-transform: capitalize; }
	    .entity-subtabs { display: flex; gap: 4px; flex-wrap: wrap; margin: 10px 0; padding-bottom: 6px; border-bottom: 1px solid var(--line-soft); }
	    .entity-subtab { background: transparent; color: var(--text-secondary); border-radius: var(--radius-md); padding: 6px 12px; font-size: var(--font-size-sm); }
	    .entity-subtab:hover { background: rgba(148, 163, 184, 0.1); color: var(--text); filter: none; }
	    .entity-subtab.active { color: var(--text); background: rgba(148, 163, 184, 0.16); font-weight: 600; outline: none; }
	    /* Runtimes master-detail:
	       the master table is HEIGHT-BOUNDED so the detail pane below it is
	       always on screen — the load-bearing half of the redesign (an
	       unbounded list is exactly the fold math that failed on the
	       entities tab and produced the "infinite scroll" complaint).
	       max-height (not height): three runtimes never render an empty
	       scroll box. Sticky thead needs the section's own opaque ground
	       (--panel) or scrolled rows read through the header text. */
	    .table-scroll { max-height: 40vh; overflow-y: auto; }
	    .table-scroll thead th { position: sticky; top: 0; background: var(--panel); z-index: 1; }
	    tr.row-selectable { cursor: pointer; }
	    tr.row-selected td { background: rgba(148, 163, 184, 0.12); }
	    .entity-subpanel { padding: 4px 0 8px; }
	    .entity-overview { display: grid; gap: 4px; margin-bottom: 10px; }
	    .entity-kv { display: flex; gap: 10px; font-size: var(--font-size-md); }
	    .entity-kv-key { color: var(--muted); min-width: 110px; flex: 0 0 auto; white-space: nowrap; }
	    .entity-kv-val { color: var(--text); word-break: break-all; }
	    .entity-btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; }
	    .entity-checkbox { display: inline-flex; align-items: center; gap: 6px; font-size: var(--font-size-sm); color: var(--muted); text-transform: none; letter-spacing: 0; font-weight: 500; margin-bottom: 0; }
	    /* State truth surface ("crystal clear visually,
	       reflects the REAL state"): pills + a push button whose color, text
	       AND border all derive from server truth — text always carries the
	       state word so color is never the sole channel. */
	    .entity-live-line { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 4px 0 10px; font-size: var(--font-size-md); }
	    .entity-live-badge { padding: 3px 12px; font-weight: 600; border-width: 1.5px; }
	    .entity-live-badge.phase-none { color: var(--ok); border-color: color-mix(in srgb, var(--success) 50%, transparent); background: color-mix(in srgb, var(--success) 8%, transparent); }
	    .entity-live-badge.phase-visit { color: var(--cyan); border-color: color-mix(in srgb, var(--info) 50%, transparent); background: color-mix(in srgb, var(--info) 10%, transparent); }
	    .entity-live-badge.phase-work { color: var(--cyan); border-color: color-mix(in srgb, var(--info) 60%, transparent); background: color-mix(in srgb, var(--info) 12%, transparent); }
	    .entity-live-badge.phase-personal { color: var(--ok); border-color: color-mix(in srgb, var(--success) 60%, transparent); background: rgba(52, 211, 153, .14); }
	    .entity-live-badge.phase-sleep { color: var(--muted); border-color: var(--line); background: rgba(255, 255, 255, .03); }
	    .entity-live-badge.phase-stopped { color: var(--danger); border-color: color-mix(in srgb, var(--error) 55%, transparent); background: color-mix(in srgb, var(--error) 12%, transparent); }
	    .entity-stop-banner {
	      display: flex; align-items: center; justify-content: space-between; gap: 12px;
	      padding: 10px 14px; margin-bottom: 12px;
	      border: 1px solid color-mix(in srgb, var(--error) 55%, transparent); border-radius: var(--radius-md);
	      background: color-mix(in srgb, var(--error) 14%, transparent); color: var(--text); font-weight: 700;
	    }
	    .entity-state-btn { background: transparent; border: 1px solid var(--ui-border-1); color: var(--muted); }
	    .entity-state-btn.pressed {
	      color: var(--text);
	      background: color-mix(in srgb, var(--success) 22%, var(--panel-2));
	      border-color: color-mix(in srgb, var(--success) 55%, transparent);
	      font-weight: 700;
	    }
	    .entity-state-btn.pressed:disabled { opacity: 1; cursor: default; }
	    .owntime-btn { display: inline-flex; align-items: center; gap: 8px; border-radius: var(--radius-lg); padding: 10px 16px; font-size: var(--font-size-md); font-weight: 600; border: 2px solid var(--line); background: rgba(255, 255, 255, .03); color: var(--muted); margin: 6px 0; }
	    .owntime-btn.on-ticking { color: var(--ok); border-color: rgba(52, 211, 153, .7); background: rgba(52, 211, 153, .14); }
	    .owntime-btn.on-parked { color: var(--cyan); border-color: color-mix(in srgb, var(--info) 60%, transparent); background: color-mix(in srgb, var(--info) 10%, transparent); }
	    .owntime-btn.stopping { color: var(--warn); border-color: rgba(245, 158, 11, .6); background: color-mix(in srgb, var(--warning) 10%, transparent); }
	    .entity-warn-pill { font-size: var(--font-size-xs); color: var(--warn); border-color: color-mix(in srgb, var(--warning) 40%, transparent); background: color-mix(in srgb, var(--warning) 7%, transparent); margin-left: 6px; }
	    /* Drive bars (cognition-health directive): RATIO with both counts
	       visible — never a bare percentage; all-resolved/all-explored gets
	       the amber never-100% cue (nothing open = no pull forward). */
	    .entity-drives { display: grid; gap: 6px; margin: 4px 0 12px; max-width: 560px; }
	    .drive-row { display: grid; grid-template-columns: 88px 1fr auto; gap: 10px; align-items: center; font-size: var(--font-size-sm); }
	    .drive-label { color: var(--muted); font-weight: 600; }
	    /* ONE determinate-bar recipe (the wave-3 chip rule applied to gauges):
	       drive bars and the Models tab's host meters share shape/track/fill;
	       families differ only in their STATE hooks — drives saturate amber on
	       "nothing open", meters step ok -> warn -> crit on used/total
	       thresholds via the state tokens. */
	    .drive-track, .meter-track { height: 8px; border-radius: 999px; background: rgba(255, 255, 255, .06); border: 1px solid var(--line); overflow: hidden; }
	    .drive-fill, .meter-fill { height: 100%; border-radius: 999px; background: color-mix(in srgb, var(--success) 70%, transparent); transition: width .3s ease; }
	    .drive-fill.saturated, .meter-fill.warn { background: color-mix(in srgb, var(--warning) 75%, transparent); }
	    .meter-fill.crit { background: color-mix(in srgb, var(--error) 75%, transparent); }
	    .drive-counts { color: var(--muted); white-space: nowrap; }
	    .drive-counts .drive-sat { color: var(--warn); font-weight: 600; }
	    /* Meter layout (Models tab: RAM / device / GPU gauges). */
	    .meter-stack { display: grid; gap: 6px; margin: 4px 0 12px; max-width: 640px; }
	    .meter-row { display: grid; grid-template-columns: 130px 1fr auto; gap: 10px; align-items: center; font-size: var(--font-size-sm); }
	    .meter-label { color: var(--muted); font-weight: 600; }
	    .meter-value { color: var(--muted); white-space: nowrap; }
	    /* Models tab: the itemized memory breakdown UNDER the meters — what the
	       framework can actually account for, line by line. Same token vocabulary
	       as the meters it sits beneath; no new color language.
	       THREE KINDS OF LINE: items (facts), then a RULE, then the reference
	       counters (Σ model weights / RAM / accelerator heap — separate
	       measurements, dimmed so they never read as more items), then the GGUF
	       note. The rule is load-bearing: it is what stops a reader adding the
	       reference counters onto the items above them. */
	    .mem-breakdown { display: grid; gap: 3px; margin: 0 0 12px; max-width: 640px; font-size: var(--font-size-sm); }
	    .mem-breakdown-head { color: var(--muted); font-weight: 600; }
	    .mem-breakdown-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: baseline; }
	    .mem-breakdown-name { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	    .mem-breakdown-note { color: var(--muted); }
	    .mem-breakdown-note.tone-warn { color: var(--warning); }
	    .mem-breakdown-note.tone-err { color: var(--error); }
	    .mem-breakdown-bytes { color: var(--muted); white-space: nowrap; }
	    .mem-breakdown-rule { border-top: 1px solid var(--line); margin: 5px 0 2px; }
	    .mem-breakdown-row.reference .mem-breakdown-name { color: var(--muted); }
	    .mem-breakdown-note-line { color: var(--muted); white-space: normal; line-height: 1.45; margin-top: 5px; }
	    /* Models tab: the admin warm-up row (inputs and selects are width:100%
	       globally — cap them so the row stays one line). */
	    .models-load-form { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
	    .models-load-form select, .models-load-form input[type="text"] { width: auto; flex: 1 1 190px; min-width: 150px; }
	    .tpl-spark { width: 100%; font-family: Menlo, Monaco, Consolas, monospace; font-size: var(--font-size-sm); line-height: 1.5; }
	    .entity-checkbox input { width: auto; }
	    .entity-prompt-layers { display: grid; gap: 10px; margin: 8px 0; }
	    .entity-prompt-layer textarea { width: 100%; font-family: inherit; font-size: var(--font-size-sm); }
	    .entity-prompt-preview { white-space: pre-wrap; font-size: var(--font-size-xs); color: var(--muted); max-height: 320px; overflow: auto; }
	    .entity-chat-transcript { border: 1px solid var(--line); border-radius: var(--radius-lg); padding: 10px 12px; margin: 8px 0; min-height: 120px; max-height: 380px; overflow-y: auto; display: grid; gap: 8px; background: rgba(255, 255, 255, .015); }
	    .entity-chat-line { display: flex; gap: 10px; font-size: var(--font-size-md); }
	    .entity-chat-you { justify-content: flex-end; }
	    .entity-chat-bubble { max-width: min(86%, 72ch); }
	    .entity-chat-bubble .entity-kv-key { display: block; margin-bottom: 3px; }
	    .section-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	    .entity-chat-composer { align-items: flex-end; }
	    .entity-chat-composer textarea { width: 100%; font-family: inherit; font-size: var(--font-size-md); }
	    .model-picker { display: grid; gap: 8px; }
	    .model-picker__head {
	      display: flex;
	      justify-content: space-between;
	      align-items: center;
	      gap: 10px;
	      flex-wrap: wrap;
	    }
	    .model-picker__actions { display: flex; gap: 8px; flex-wrap: wrap; }
	    select.model-picker__select { min-height: 150px; }
	    .model-picker__note { color: var(--muted); font-size: var(--font-size-sm); line-height: 1.4; text-transform: none; letter-spacing: 0; }
	    .model-summary {
	      border: 1px solid var(--line);
	      border-radius: var(--radius-md);
	      padding: 10px 12px;
	      background: rgba(255, 255, 255, .025);
	      color: var(--muted);
	    }
	    .advanced-panel {
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-md);
	      padding: 0;
	      background: rgba(0, 0, 0, .12);
	    }
	    .advanced-panel summary {
	      cursor: pointer;
	      padding: 10px 12px;
	      color: var(--muted);
	      font-weight: 800;
	      list-style-position: inside;
	    }
	    .advanced-panel__body { padding: 0 12px 12px; display: grid; gap: 10px; }
	    .connection-wizard { display: grid; gap: 16px; }
	    .wizard-step {
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-md);
	      padding: 12px;
	      background: rgba(255, 255, 255, .02);
	    }
	    .step-kicker {
	      color: var(--accent);
	      font-size: var(--font-size-xs);
	      font-weight: 700;
	      letter-spacing: .08em;
	      text-transform: uppercase;
	      margin-bottom: 4px;
	    }
		    .provider-preset-grid {
		      display: grid;
		      grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
		      gap: 8px;
		      margin-top: 12px;
		    }
		    .provider-preset {
		      min-height: 64px;
	      display: grid;
	      gap: 6px;
	      justify-content: stretch;
	      align-items: start;
		      text-align: left;
		      background: color-mix(in srgb, var(--text) 2%, transparent);
		      border: 1px solid var(--line-soft);
		      padding: 10px;
		      white-space: normal;
		    }
	    .provider-preset:hover, .provider-preset.active {
	      border-color: color-mix(in srgb, var(--accent) 45%, transparent);
	      background: var(--accent-subtle);
	    }
	    .provider-preset strong { display: block; color: var(--text); line-height: 1.25; }
	    .provider-preset span { display: block; color: var(--muted); font-size: var(--font-size-sm); font-weight: 500; line-height: 1.3; margin-top: 3px; }
	    .provider-preset span.provider-preset__state { margin-top: 6px; font-weight: 600; }
	    /* One-line intros (DESIGN-v3 §7 text budget): the page is wide enough. */
	    #tab-providers .section-note { max-width: none; }
	    .provider-preset span.provider-preset__state.is-on { color: var(--success); }
	    /* Local provider cards: the server connection (DESIGN-v3 §7). Flat: a
	       top rule, no nested card. */
	    .provider-connection { display: grid; gap: 6px; margin-top: 4px; padding-top: 10px; border-top: 1px solid var(--line-soft); min-width: 0; }
	    .provider-connection__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
	    .provider-connection__title { font-size: var(--font-size-sm); font-weight: 600; color: var(--text); }
	    .provider-connection__list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; min-width: 0; }
	    .provider-connection__row { display: flex; align-items: center; gap: 8px; min-width: 0; }
	    .provider-connection__text { display: grid; gap: 2px; min-width: 0; flex: 1 1 auto; overflow-wrap: anywhere; }
	    .provider-connection__text .ui-sub { font-size: var(--font-size-sm); color: var(--muted); }
	    .provider-connection__text code { max-width: 100%; justify-self: start; font-size: var(--font-size-sm); padding: 1px 6px; background: transparent; border: 0; color: var(--muted); }
	    .setup-summary {
	      border: 1px solid color-mix(in srgb, var(--info) 22%, transparent);
	      border-radius: var(--radius-md);
	      padding: 10px 12px;
	      background: color-mix(in srgb, var(--info) 6%, transparent);
	      color: var(--muted);
	      margin-bottom: 12px;
	    }
	    /* The sign-in card is the kit's (af-gateway-signin block of theme.css,
	       synced into af-kit-css): one column, labels above, 480 px. The console
	       used to carry its own copy here whose __checkbox rule (18 px / 700) was
	       reused outside the card: every "checkbox label" in the console grew to
	       20 px bold. DESIGN §3 — fixed at the rule by deleting the copy. */
	    body:not(.signed-in) .console-shell > .af-gateway-signin { margin-top: 24px; }
	    /* A plain checkbox row in a form (not a setting: settings are switches). */
	    .check-row {
	      display: inline-flex;
	      align-items: center;
	      gap: 8px;
	      margin: 0;
	      color: var(--text);
	      font-size: var(--font-size-base);
	      font-weight: 400;
	      text-transform: none;
	      letter-spacing: 0;
	      cursor: pointer;
	    }
	    .modal-backdrop {
	      position: fixed;
	      inset: 0;
      /* Above the nav drawer and the assistant drawer (kit --z-drawer 900):
         a dialog opened while a drawer is open must be on top. */
      z-index: var(--z-connect-modal, 1000);
      display: grid;
      place-items: center;
      padding: max(22px, var(--safe-top, 0px)) max(22px, var(--safe-right, 0px)) max(22px, var(--safe-bottom, 0px)) max(22px, var(--safe-left, 0px));
      overscroll-behavior: contain;
      background: rgba(0, 0, 0, 0.45);
      -webkit-backdrop-filter: blur(6px);
      backdrop-filter: blur(6px);
    }
	    .modal {
	      width: min(520px, 100%);
	      max-height: calc(var(--vh-full, 100vh) - 44px);
	      overflow: auto;
	      overscroll-behavior: contain;
	      border: 1px solid var(--line);
	      border-radius: var(--radius-md);
	      background: var(--panel);
	      box-shadow: 0 28px 70px rgba(0, 0, 0, .46);
	      padding: 18px;
	    }
	    .modal.wide { width: min(680px, 100%); }
	    .modal.flow-modal {
	      width: min(520px, 100%);
	      max-width: min(520px, 100%);
	      padding: 0;
	      display: flex;
	      flex-direction: column;
	      overflow: hidden;
	      border: 1px solid rgba(255, 255, 255, .12);
	      border-radius: var(--radius-md);
	      background: var(--bg-secondary);
	      box-shadow: 0 24px 80px rgba(0, 0, 0, .55);
	    }
	    .flow-modal .modal-header {
	      flex: 0 0 auto;
	      padding: 16px 18px 12px;
	      border-bottom: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .flow-modal .modal-header h2 {
	      margin: 0 0 5px;
	      font-size: var(--font-size-lg);
	      line-height: 1.25;
	    }
	    .flow-modal .modal-header p {
	      margin: 0;
	      color: var(--text-secondary);
	      font-size: var(--font-size-sm);
	      line-height: 1.35;
	    }
	    .flow-modal .modal-body {
	      flex: 1 1 auto;
	      min-height: 0;
	      overflow: auto;
	      padding: 14px 18px 16px;
	    }
	    .flow-modal .modal-actions {
	      flex: 0 0 auto;
	      position: static;
	      margin: 0;
	      padding: 12px 18px;
	      border-top: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .default-modal {
	      width: min(500px, 100%);
	      max-width: min(500px, 100%);
	    }
	    .modal.wsp-modal { width: min(860px, 100%); max-width: min(860px, 100%); }
	    .modal.log-modal { width: min(1100px, 100%); max-width: min(1100px, 100%); }
	    /* Dialogs become bottom sheets on phones and in phone landscape
	       (DESIGN §5.7): full width, top corners rounded, the actions row
	       stays visible (sticky) above the home indicator. */
	    @media (max-width: 767.98px), (max-height: 500px) {
	      .modal-backdrop { place-items: end center; padding: var(--safe-top, 0px) 0 0; }
	      .modal, .modal.wide, .modal.flow-modal, .default-modal, .modal.wsp-modal, .modal.log-modal, .modal.provider-modal {
	        width: 100%; max-width: 100%;
	        max-height: calc(var(--vh-full, 100vh) - var(--safe-top, 0px));
	        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
	        border-bottom: 0;
	        padding-bottom: max(18px, var(--safe-bottom, 0px));
	      }
	      .modal.flow-modal, .modal.provider-modal { padding-bottom: 0; }
	      .flow-modal .modal-actions, .provider-modal .modal-actions { padding-bottom: max(12px, var(--safe-bottom, 0px)); }
	      .modal-actions { flex-wrap: wrap; }
	    }
	    @media (max-width: 479.98px) {
	      /* Primary (confirm) is last in the DOM: it stays last, nearest the thumb. */
	      .modal-actions { flex-direction: column; align-items: stretch; }
	      .modal-actions > button { width: 100%; }
	    }
	    .list-pager { display: flex; gap: 12px; align-items: center; justify-content: center; margin-top: 8px; }
	    /* ONE toolbar shape for every list tab: compact
	       dropdown, then the search bar, then any tab-specific extra. The
	       `input[type=search]` selector is deliberate — a bare `input` rule
	       would stretch checkboxes that share the row. */
	    .list-toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
	    .list-toolbar select { width: auto; min-width: 150px; flex: 0 0 auto; }
	    .list-toolbar input[type="search"] { flex: 1 1 220px; min-width: 160px; }
	    .list-toolbar .entity-checkbox { flex: 0 0 auto; white-space: nowrap; margin-bottom: 0; }
	    #artifact-modal-content img { max-width: 100%; max-height: 58vh; object-fit: contain; display: block; margin: 0 auto; border-radius: var(--radius-md); }
	    #artifact-modal-content video { max-width: 100%; max-height: 58vh; display: block; margin: 0 auto; border-radius: var(--radius-md); background: #000; }
	    #artifact-modal-content audio { width: 100%; }
	    #artifact-modal-content pre, #artifact-modal-content .artifact-md {
	      max-height: 58vh; overflow: auto; margin: 0;
	      white-space: pre-wrap; overflow-wrap: anywhere;
	      background: var(--ui-surface-3); border: 1px solid var(--ui-border-1);
	      border-radius: var(--radius-lg); padding: 12px 14px; font-size: var(--font-size-sm); line-height: 1.5;
	    }
	    #artifact-modal-content .artifact-md { white-space: normal; font-size: var(--font-size-md); }
	    .log-modal #log-modal-pre {
	      height: min(58vh, 640px); overflow: auto; margin: 0;
	      white-space: pre-wrap; overflow-wrap: anywhere;
	      background: var(--ui-surface-3); border: 1px solid var(--ui-border-1);
	      border-radius: var(--radius-lg); padding: 12px 14px;
	      font-size: var(--font-size-sm); line-height: 1.5;
	      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	    }
	    .wsp-mode-cards { display: grid; gap: 8px; margin: 2px 0 10px; }
	    .wsp-card { display: flex; gap: 10px; align-items: flex-start; border: 1px solid var(--ui-border-1); border-radius: var(--radius-lg); padding: 10px 12px; cursor: pointer; text-transform: none; letter-spacing: normal; }
	    .wsp-card:hover { border-color: var(--ui-border-2); }
	    .wsp-card.selected { border-color: var(--accent); background: var(--accent-subtle); }
	    .wsp-card input[type="radio"] { width: auto; min-height: auto; flex: 0 0 auto; margin-top: 3px; padding: 0; border: none; background: transparent; accent-color: var(--accent); }
	    .wsp-card > span { flex: 1 1 auto; min-width: 0; }
	    .wsp-card b { display: block; margin-bottom: 2px; }
	    .wsp-card .wsp-card-sub { color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.4; }
	    .wsp-field { display: block; margin-bottom: 10px; }
	    .wsp-field .wsp-hint { color: var(--text-secondary); font-size: var(--font-size-sm); display: block; margin: 2px 0 4px; text-transform: none; letter-spacing: normal; font-weight: normal; }
	    .wsp-field textarea, .wsp-field select { width: 100%; }
	    .wsp-badge { font-size: var(--font-size-xs); border: 1px solid var(--ui-border-1); border-radius: 999px; padding: 1px 8px; color: var(--text-secondary); white-space: nowrap; }
	    .spin-inline { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--ui-border-2); border-top-color: var(--accent); border-radius: 50%; animation: afspin .8s linear infinite; vertical-align: -2px; margin-right: 7px; }
	    @keyframes afspin { to { transform: rotate(360deg); } }
	    .wsp-badge.custom { border-color: var(--accent); color: var(--accent); }
	    .modal.provider-modal {
	      width: min(720px, 100%);
	      padding: 14px 14px 0;
	    }
	    .modal h2 { margin-bottom: 8px; }
	    .modal p { color: var(--muted); margin-bottom: 16px; overflow-wrap: anywhere; }
	    .modal-actions {
	      position: sticky;
	      bottom: -18px;
	      display: flex;
	      justify-content: flex-end;
	      gap: 10px;
	      margin: 10px -18px -18px;
	      padding: 12px 18px 18px;
	      /* Theme-safe footer (the dark gradient smeared under light modals). */
	      border-top: 1px solid var(--line-soft);
	      background: color-mix(in srgb, var(--panel) 92%, transparent);
	    }
	    .capability-table td { vertical-align: middle; }
	    .capability-route { display: grid; gap: 3px; }
	    .capability-route code { width: fit-content; }
	    /* Task rows are indented under their modality row so the grid reads as
	       the hierarchy it is: `output.image` is the parent (one value for every
	       image task), `.text_to_image` and friends override it per task. */
	    .capability-route-task { padding-left: 18px; }
	    .capability-route-task::before { content: "└"; position: absolute; margin-left: -14px; opacity: 0.55; }
	    tr.capability-task-row td:first-child { position: relative; }
	    .default-config-form, .provider-config-form { display: grid; gap: 12px; margin-top: 16px; }
	    .provider-modal .provider-config-form {
	      grid-template-columns: repeat(2, minmax(0, 1fr));
	      gap: 8px 12px;
	      margin-top: 8px;
	    }
	    .provider-modal input,
	    .provider-modal select {
	      min-height: 36px;
	      padding: 7px 10px;
	      line-height: 1.35;
	    }
	    .provider-modal .inline {
	      display: contents;
	    }
	    .provider-modal label {
	      margin-bottom: 0;
	      gap: 5px;
	    }
	    .provider-modal textarea {
	      min-height: 42px;
	      max-height: 60px;
	    }
	    .provider-modal .field-help {
	      margin: -2px 0 0;
	      font-size: var(--font-size-xs);
	    }
	    .provider-modal .field-span-2 {
	      grid-column: 1 / -1;
	    }
	    .provider-modal .provider-toggle-row {
	      grid-column: 1 / -1;
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 12px;
	      flex-wrap: wrap;
	      padding-top: 2px;
	    }
	    .provider-modal .advanced-panel summary {
	      padding: 7px 10px;
	    }
	    .provider-modal .advanced-panel__body {
	      padding: 0 10px 10px;
	    }
	    .provider-modal .modal-actions {
	      margin: 8px -14px 0;
	      padding: 12px 14px 14px;
	      border-top: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .default-config-form {
	      gap: 10px;
	      margin-top: 0;
	    }
	    .default-config-form label {
	      gap: 5px;
	      margin-bottom: 0;
	      color: var(--text-secondary);
	      /* Labels sit at body size (DESIGN §3): 11 px labels hid the workspace-policy form on phones. */
	      font-size: 14px;
	    }
	    .default-config-form select {
	      min-height: 32px;
	      padding-top: 5px;
	      padding-bottom: 5px;
	    }
	    .default-modal-route {
	      display: inline-flex;
	      width: fit-content;
	      margin-top: 10px;
	      border: 1px solid rgba(255, 255, 255, .14);
	      border-radius: 999px;
	      padding: 4px 10px;
	      color: var(--text-secondary);
	      background: rgba(255, 255, 255, .05);
	      font-size: var(--font-size-xs);
	      font-weight: 700;
	      text-transform: none;
	      letter-spacing: 0;
	    }
	    .provider-modal-grid { display: grid; gap: 12px; }
	    .providers-workspace { display: grid; gap: 16px; }
	    .providers-workspace #provider-preset-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); }
	    .sandbox-workspace {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr);
	      gap: 18px;
	      align-items: stretch;
	    }
	    .sandbox-mode-grid {
	      display: flex;
	      flex-wrap: wrap;
	      justify-content: flex-end;
	      gap: 7px;
	      margin: 0;
	    }
	    .sandbox-mode {
	      display: inline-grid;
	      place-items: center;
	      position: relative;
	      width: 38px;
	      min-width: 38px;
	      height: 38px;
	      min-height: 38px;
	      padding: 0;
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-lg);
	      background: rgba(255, 255, 255, .03);
	      color: var(--text-secondary);
	      text-align: center;
	    }
	    .sandbox-mode:hover:not(:disabled) {
	      border-color: color-mix(in srgb, var(--info) 45%, transparent);
	      background: color-mix(in srgb, var(--info) 8%, transparent);
	      color: var(--text-primary);
	    }
	    .sandbox-mode.active {
	      border-color: color-mix(in srgb, var(--info) 75%, transparent);
	      background: color-mix(in srgb, var(--info) 10%, transparent);
	      color: var(--text-primary);
	    }
	    .sandbox-mode:disabled {
	      opacity: .46;
	      cursor: not-allowed;
	    }
	    .sandbox-mode-icon {
	      display: inline-grid;
	      place-items: center;
	      width: 30px;
	      height: 30px;
	      border-radius: var(--radius-lg);
	      background: var(--accent-subtle);
	      color: var(--accent);
	      font-weight: 700;
	    }
	    .sandbox-mode-icon svg {
	      width: 18px;
	      height: 18px;
	      stroke: currentColor;
	      stroke-width: 2;
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      fill: none;
	    }
	    .sandbox-mode-icon svg.fill {
	      fill: currentColor;
	      stroke: none;
	    }
	    .sandbox-mode-copy {
	      position: absolute;
	      width: 1px;
	      height: 1px;
	      overflow: hidden;
	      clip: rect(0 0 0 0);
	      white-space: nowrap;
	    }
	    .sandbox-mode-main {
	      display: none;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	      font-weight: 700;
	    }
	    .sandbox-mode-sub {
	      display: none;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	      color: var(--text-muted);
	      font-size: var(--font-size-xs);
	      font-weight: 700;
	      margin-top: 2px;
	    }
	    .sandbox-chat {
	      min-height: min(560px, calc(var(--vh-full, 100vh) - 160px));
	      max-height: calc(var(--vh-full, 100vh) - 205px);
	      display: grid;
	      /* The transcript gives way first (down to 140 px) and, when the
	         composer still does not fit (1280x800, 1366x768: system prompt +
	         reasoning + MTP + input), the card scrolls instead of clipping the
	         composer out of reach (it was overflow:hidden). */
	      grid-template-rows: auto minmax(140px, 1fr) auto;
	      overflow-x: hidden;
	      overflow-y: auto;
	      overscroll-behavior: contain;
	    }
	    .sandbox-chat .section-head {
	      margin-bottom: 0;
	      padding-bottom: 12px;
	      border-bottom: 1px solid var(--line-soft);
	    }
	    .sandbox-transcript {
	      min-height: 0;
	      max-height: none;
	      overflow-y: auto;
	      padding: 18px;
	      background:
	        linear-gradient(180deg, rgba(255,255,255,.025), rgba(255,255,255,0)),
	        rgba(0, 0, 0, .10);
	    }
	    .sandbox-message {
	      display: flex;
	      margin: 0 0 12px;
	    }
	    .sandbox-message.user {
	      justify-content: flex-end;
	    }
	    .sandbox-message.assistant,
	    .sandbox-message.system,
	    .sandbox-message.error {
	      justify-content: flex-start;
	    }
	    /* Dialogue bubbles: the abstractuic panel-chat component's .pc-chat-item
	       recipe (operator 12:24: "reuse the shared component for dialogue of
	       abstractuic"). panel_chat.css is the source of truth; tokens map onto
	       the console's own variables. sandbox-bubble keeps only the layout. */
	    .sandbox-bubble { width: min(760px, 88%); box-shadow: 0 10px 28px rgba(0, 0, 0, .12); }
	    .pc-chat-item {
	      position: relative;
	      border: 1px solid var(--line);
	      border-radius: 12px;
	      padding: 10px 12px;
	      background: var(--panel-2);
	    }
	    .pc-chat-item--user {
	      background: var(--info-subtle);
	      border-color: color-mix(in srgb, var(--info) 36%, transparent);
	      border-bottom-right-radius: var(--radius-sm);
	    }
	    .pc-chat-item--assistant {
	      background: color-mix(in srgb, var(--accent) 8%, var(--panel));
	      border-color: color-mix(in srgb, var(--accent) 26%, transparent);
	      border-bottom-left-radius: var(--radius-sm);
	    }
	    .pc-chat-item--status { background: var(--panel-2); }
	    .pc-chat-item--error {
	      background: color-mix(in srgb, var(--danger) 12%, var(--panel));
	      border-color: color-mix(in srgb, var(--danger) 32%, transparent);
	    }
	    .sandbox-message-meta {
	      display: flex;
	      align-items: center;
	      gap: 8px;
	      margin-bottom: 6px;
	      color: var(--text-muted);
	      font-size: var(--font-size-xs);
	      font-weight: 800;
	    }
	    .sandbox-message-role {
	      color: var(--accent);
	      font-size: var(--font-size-sm);
	      font-weight: 700;
	    }
	    .sandbox-message-spacer {
	      flex: 1;
	    }
	    .sandbox-message-body {
	      white-space: pre-wrap;
	      color: var(--text-primary);
	      line-height: 1.45;
	    }
	    .sandbox-message-body.markdown {
	      white-space: normal;
	    }
	    .sandbox-message-body.markdown > :first-child {
	      margin-top: 0;
	    }
	    .sandbox-message-body.markdown > :last-child {
	      margin-bottom: 0;
	    }
	    .sandbox-message-body.markdown p {
	      margin: 0 0 10px;
	    }
	    .sandbox-message-body.markdown h1,
	    .sandbox-message-body.markdown h2,
	    .sandbox-message-body.markdown h3 {
	      margin: 12px 0 8px;
	      color: var(--text-primary);
	      font-weight: 700;
	      letter-spacing: 0;
	      line-height: 1.2;
	    }
	    .sandbox-message-body.markdown h1 { font-size: 1.18em; }
	    .sandbox-message-body.markdown h2 { font-size: 1.10em; }
	    .sandbox-message-body.markdown h3 { font-size: 1.03em; }
	    .sandbox-message-body.markdown ul,
	    .sandbox-message-body.markdown ol {
	      margin: 8px 0 10px 22px;
	      padding: 0;
	    }
	    .sandbox-message-body.markdown li {
	      margin: 4px 0;
	    }
	    .sandbox-message-body.markdown blockquote {
	      margin: 10px 0;
	      border-left: 3px solid color-mix(in srgb, var(--info) 45%, transparent);
	      padding: 4px 0 4px 12px;
	      color: var(--text-secondary);
	    }
	    .sandbox-message-body.markdown code {
	      border: 1px solid rgba(255, 255, 255, .10);
	      border-radius: var(--radius-sm);
	      padding: 1px 5px;
	      background: rgba(0, 0, 0, .26);
	      color: var(--text-primary);
	      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	      font-size: .93em;
	    }
	    .sandbox-message-body.markdown pre {
	      overflow: auto;
	      margin: 10px 0;
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-lg);
	      padding: 10px 12px;
	      background: rgba(0, 0, 0, .28);
	    }
	    .sandbox-message-body.markdown pre code {
	      border: 0;
	      padding: 0;
	      background: transparent;
	    }
	    .sandbox-message-body.markdown a {
	      color: var(--accent-2);
	      font-weight: 800;
	    }
	    .sandbox-speak {
	      width: 30px;
	      min-width: 30px;
	      height: 28px;
	      padding: 0;
	    }
	    .sandbox-speak.speaking {
	      border-color: color-mix(in srgb, var(--info) 70%, transparent);
	      color: var(--accent);
	    }
	    .sandbox-progress {
	      display: grid;
	      gap: 8px;
	      margin-top: 8px;
	    }
	    .sandbox-progress-bar {
	      position: relative;
	      height: 6px;
	      overflow: hidden;
	      border-radius: 999px;
	      background: rgba(255, 255, 255, .08);
	    }
	    .sandbox-progress-bar::before {
	      content: "";
	      position: absolute;
	      inset: 0;
	      width: 38%;
	      border-radius: inherit;
	      background: linear-gradient(90deg, var(--accent), var(--accent-2));
	      animation: sandbox-progress 1.25s ease-in-out infinite;
	    }
	    @keyframes sandbox-progress {
	      0% { transform: translateX(-110%); }
	      100% { transform: translateX(280%); }
	    }
	    .sandbox-artifact {
	      display: grid;
	      gap: 8px;
	      margin-top: 10px;
	    }
	    .sandbox-artifact img,
	    .sandbox-artifact video {
	      max-width: 100%;
	      max-height: min(440px, 48vh);
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-lg);
	      background: rgba(0, 0, 0, .22);
	      object-fit: contain;
	    }
	    .sandbox-artifact audio {
	      width: 100%;
	      min-width: min(260px, 100%);
	    }
	    .default-modal-test {
	      display: grid;
	      gap: 8px;
	      font-size: var(--font-size-sm);
	      color: var(--text-muted);
	    }
	    .default-modal-test:empty { display: none; }
	    .default-modal-test .sandbox-artifact audio { width: 100%; min-width: min(220px, 100%); }
	    .sandbox-media-error {
	      border: 1px solid color-mix(in srgb, var(--danger) 35%, transparent);
	      border-radius: var(--radius-md);
	      padding: 8px 10px;
	      color: color-mix(in srgb, var(--danger) 72%, var(--text));
	      background: color-mix(in srgb, var(--danger) 8%, transparent);
	      font-size: var(--font-size-sm);
	      font-weight: 600;
	    }
	    .sandbox-artifact-link {
	      display: inline-flex;
	      align-items: center;
	      gap: 6px;
	      width: fit-content;
	      color: var(--accent-2);
	      font-weight: 700;
	    }
	    .sandbox-composer {
	      border-top: 1px solid var(--line-soft);
	      padding: 14px 16px 16px;
	      background: color-mix(in srgb, var(--panel-2) 55%, transparent);
	    }
	    .sandbox-composer-toolbar {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr);
	      gap: 10px;
	      align-items: end;
	      margin-bottom: 10px;
	    }
	    .sandbox-system-compact {
	      margin: 0;
	    }
	    .sandbox-system-compact input {
	      min-height: 34px;
	      padding-top: 6px;
	      padding-bottom: 6px;
	    }
	    .sandbox-composer-toolbar { grid-template-columns: minmax(0, 1fr) auto; }
	    #sandbox-reasoning { min-height: 34px; }
	    .sandbox-reasoning-block {
	      margin: 0 0 8px 0;
	      font-size: var(--font-size-sm);
	      color: var(--muted, #8a93a6);
	    }
	    .sandbox-reasoning-block summary { cursor: pointer; user-select: none; }
	    .sandbox-reasoning-block pre {
	      white-space: pre-wrap;
	      margin: 6px 0 0 0;
	      max-height: 240px;
	      overflow: auto;
	    }
	    .sandbox-dropzone {
	      display: grid;
	      grid-template-columns: auto minmax(0, 1fr) auto;
	      gap: 12px;
	      align-items: end;
	      border: 1px solid var(--line-soft);
	      border-radius: var(--radius-lg);
	      padding: 10px 12px;
	      /* Theme-safe composer pill (was a near-black literal — a dark slab
	         inside the light theme's white page). */
	      background: var(--panel-2);
	    }
	    .sandbox-dropzone:focus-within {
	      border-color: color-mix(in srgb, var(--info) 62%, transparent);
	      box-shadow: 0 0 0 3px color-mix(in srgb, var(--info) 10%, transparent), inset 0 1px 0 rgba(255, 255, 255, .05);
	    }
	    .sandbox-dropzone.dragover {
	      border-color: color-mix(in srgb, var(--info) 75%, transparent);
	      box-shadow: 0 0 0 3px var(--info-subtle);
	    }
	    .sandbox-input-area {
	      display: grid;
	      min-width: 0;
	    }
	    .sandbox-dropzone textarea {
	      min-height: 56px;
	      max-height: 170px;
	      resize: vertical;
	      border: 0;
	      padding: 8px 2px;
	      background: transparent;
	      color: var(--text-primary);
	      font-size: var(--font-size-md);
	      line-height: 1.45;
	      box-shadow: none;
	    }
	    .sandbox-dropzone textarea:focus {
	      box-shadow: none;
	    }
	    .sandbox-composer-side {
	      display: grid;
	      gap: 8px;
	      align-items: end;
	      justify-items: end;
	      align-self: stretch;
	      align-content: end;
	    }
	    .sandbox-composer-actions {
	      display: flex;
	      align-items: center;
	      justify-content: flex-end;
	      gap: 8px;
	    }
	    .sandbox-composer-icon,
	    .sandbox-send {
	      width: 40px;
	      min-width: 40px;
	      height: 40px;
	      min-height: 40px;
	      border-radius: var(--radius-lg);
	      padding: 0;
	    }
	    .sandbox-send {
	      background: var(--accent);
	      border-color: rgba(96, 165, 250, .36);
	    }
	    .sandbox-send .button-icon,
	    .sandbox-composer-icon .button-icon {
	      margin: 0;
	    }
	    .sandbox-attachments {
	      display: flex;
	      flex-wrap: wrap;
	      gap: 6px;
	      margin: 8px 0 0;
	    }
	    .sandbox-attachment {
	      display: inline-flex;
	      align-items: center;
	      gap: 6px;
	      max-width: 260px;
	      border: 1px solid var(--line-soft);
	      border-radius: 999px;
	      padding: 4px 8px;
	      color: var(--text-secondary);
	      background: rgba(255, 255, 255, .04);
	      font-size: var(--font-size-xs);
	      font-weight: 800;
	    }
	    .sandbox-attachment span {
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	    }
	    .appearance-form {
	      display: grid;
	      grid-template-columns: 140px minmax(0, 1fr);
	      gap: 12px;
	      align-items: center;
	      margin: 18px 0;
	    }
	    .appearance-form label {
	      margin: 0;
	    }
	    .appearance-control {
	      display: grid;
	      gap: 8px;
	    }
	    .theme-swatches { display: flex; gap: 4px; flex-wrap: wrap; }
	    .theme-swatch {
	      width: 16px;
	      height: 16px;
	      border-radius: var(--radius-sm);
	      border: 1px solid rgba(255, 255, 255, .16);
	    }
	    .icon-only {
	      width: 36px;
	      min-width: 36px;
	      padding: 0;
	    }
	    /* md (was 940 px). The old rule here also restyled EVERY <header>
	       element (flex column, padding) — including the model catalog's
	       <header class="mc-card__head">, which is why each card head fell
	       apart into a centred 400 px column below 940 px. Scoped now. */
	    @media (max-width: 1023.98px) {
	      .tab-grid { grid-template-columns: minmax(0, 1fr); }
	      .sandbox-workspace { grid-template-columns: minmax(0, 1fr); }
	      .sandbox-composer-toolbar { grid-template-columns: minmax(0, 1fr); }
	      .sandbox-mode-grid { justify-content: flex-start; }
	      main { padding: 16px; }
	    }
	    @media (max-width: 479.98px) {
	      /* The head's action (Create user, Refresh...) drops under the note
	         instead of squeezing it to a 120 px column. */
	      .section-head { flex-wrap: wrap; }
	      .section-head > div:first-child { flex: 1 1 100%; min-width: 0; }
	    }
	    /* Narrow or touch: the composer (system prompt, reasoning, output
	       modes) is taller than the fixed-height card left room for, and the
	       card's overflow:hidden cut it off unreachably. The card grows with
	       its content; the transcript alone scrolls. */
	    @media (max-width: 1023.98px), (pointer: coarse) {
	      .sandbox-chat { min-height: 0; max-height: none; overflow: visible; grid-template-rows: auto minmax(220px, auto) auto; }
	      .sandbox-transcript { max-height: calc(var(--vh-full, 100vh) * .55); }
	    }
	    @media (max-width: 767.98px) {
	      body:not(.signed-in) .console-shell { align-content: start; padding-block: 18px; }
	      .appearance-form { grid-template-columns: 1fr; }
	      .provider-modal .provider-config-form { grid-template-columns: 1fr; }
	      .provider-modal .field-span-2 { grid-column: auto; }
	      .sandbox-dropzone { grid-template-columns: auto minmax(0, 1fr); }
	      .sandbox-composer-side {
	        grid-column: 1 / -1;
	        grid-template-columns: minmax(0, 1fr) auto;
	        align-items: center;
	        justify-items: stretch;
	      }
	      .sandbox-composer-actions { justify-content: flex-end; }
	    }
    /* Models / Engines tabs: AbstractCore's embedded screens (scoped under
       .acc-root, they read the kit variables above), then the host chrome. */
    /*__ABSTRACTCORE_FRAGMENT_CSS__*/
    .core-console-unavailable { display: grid; gap: 8px; padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--panel-2); }
    .core-console-unavailable h2 { margin: 0; font-size: 1.05em; }
/*__CONSOLE_UI_CSS__*/
	    /* ---- State toggles, users and the account page (DESIGN §2/§3/§5/§6/§12).
	       The kit supplies af-switch, af-form, af-card, af-tabs and the sign-in
	       card; these rules only lay them out in the console. Type scale: labels
	       at body size (14 px, 500), helpers small and muted. */
	    .switch-list { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
	    .switch-list > .af-switch__reason { margin: -2px 0 6px 0; }
	    /* Visually hidden text is absolutely positioned: its containing block must sit inside
	       the scrolling .shell_content, or it escapes the clip and makes the page scroll too. */
	    .users-active, .kv-switch, .af-form__field { position: relative; }
	    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
	    /* The column header says Active: the switch keeps its accessible name, the visible label goes. */
	    .users-active .af-switch__text { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	    .kv-switch .af-switch__text { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	    .users-active .af-switch__reason { display: block; max-width: 180px; margin-top: 2px; }
	    .inline-state { margin: 0; min-height: 1.2em; color: var(--muted); font-size: var(--font-size-sm); font-weight: 400; }
	    .inline-state.ok { color: var(--text); }
	    .inline-state.error { color: var(--error); font-size: var(--font-size-base); }
	    span.inline-state { display: inline; min-height: 0; margin-left: 6px; }
	    .plain-disclosure { border: 0; padding: 0; background: transparent; }
	    .plain-disclosure > summary {
	      display: inline-flex; align-items: center; gap: 6px; min-height: 36px;
	      cursor: pointer; color: var(--muted); font-size: var(--font-size-base); font-weight: 500;
	      list-style: none;
	    }
	    .plain-disclosure > summary::-webkit-details-marker { display: none; }
	    .plain-disclosure > summary::before { content: "›"; display: inline-block; width: 1em; text-align: center; transition: transform .15s; }
	    .plain-disclosure[open] > summary::before { transform: rotate(90deg); }
	    .plain-disclosure[open] > summary { color: var(--text); margin-bottom: 8px; }
	    /* F5: the workspace policy disclosure = the kit chevron (as Advanced), a short title + one helper line. */
	    .workspace-policy-disclosure { margin-top: 16px; }
	    .weights-reason { display: block; margin-top: 4px; font-size: var(--af-helper-size, 13px); color: var(--text-secondary); }
	    .workspace-policy-disclosure > summary { display: grid; grid-template-columns: 1em 1fr; column-gap: 4px; align-items: baseline; }
	    .workspace-policy-disclosure__title { font-size: var(--font-size-md); font-weight: 600; color: var(--text); }
	    .workspace-policy-disclosure__help { grid-column: 2; font-size: var(--af-helper-size, 13px); color: var(--text-secondary); font-weight: 400; }
	    .link-button {
	      min-height: 0; padding: 2px 0; border: 0; background: transparent; color: var(--muted);
	      font-size: var(--font-size-base); font-weight: 400; text-decoration: underline;
	      text-underline-offset: 3px; text-decoration-color: color-mix(in srgb, currentColor 45%, transparent); cursor: pointer;
	    }
	    .link-button:hover:not(:disabled) { color: var(--text); filter: none; }
	    .inline-confirm {
	      display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px;
	      padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--error) 35%, transparent);
	      border-radius: var(--radius-md); background: color-mix(in srgb, var(--error) 7%, transparent);
	      color: var(--text); font-size: var(--font-size-base);
	    }
	    .inline-confirm > span { flex: 1 1 260px; }
	    .inline-confirm[hidden] { display: none; }
	    /* Users (admin): the one switch above the table, then the table. */
	    .users-caps { display: flex; flex-direction: column; gap: 6px; max-width: 720px; margin: 0 0 14px; }
	    .users-table-wrap { overflow-x: auto; }
	    .users-table td { font-size: var(--font-size-base); vertical-align: middle; }
	    /* A table cell, not a flex box: a flex td drops out of the row's borders. */
	    .users-table td.actions { display: table-cell; white-space: nowrap; }
	    .users-table td.actions > button + button { margin-left: 6px; }
	    .users-table .row-confirm > td { padding-top: 0; }
	    .users-override { color: var(--warn, var(--warning)); }
	    .users-mailbox__body { min-width: 0; overflow-wrap: break-word; word-break: normal; }
	    .users-mailbox__body .small { white-space: nowrap; }
	    button.small { min-height: 28px; padding: 3px 10px; font-size: var(--font-size-sm); }
	    .user-create-form { max-width: 520px; }
	    .user-create-form .modal-actions { margin-top: 4px; }
	    /* The user's account page: cards in one column, forms at most 720 px. */
	    .account-page { display: flex; flex-direction: column; gap: 14px; max-width: 760px; min-width: 0; }
	    .account-page__head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
	    .account-page__head .section-title { margin: 0; }
	    .account-card { margin: 0; box-shadow: none; }
	    .account-card .af-card__title { font-size: var(--font-size-lg); }
	    .account-card[hidden], #my-email-connect[hidden], #my-email-connected[hidden] { display: none; }
	    /* The hidden attribute always wins over a display rule (a hidden "Cancel sign-in" stayed display:flex). */
	    .console-shell [hidden], .account-card [hidden], #my-email-oauth-cancel[hidden] { display: none !important; }
	    .account-advanced > summary {
	      cursor: pointer; font-size: var(--font-size-lg); font-weight: 600; color: var(--text); min-height: 32px;
	      display: flex; align-items: center; gap: 8px; list-style: none;
	    }
	    .account-advanced > summary::-webkit-details-marker { display: none; }
	    .account-advanced > summary::before { content: "›"; display: inline-block; width: 1em; text-align: center; color: var(--muted); transition: transform .15s; }
	    .account-advanced[open] > summary::before { transform: rotate(90deg); }
	    .account-advanced[open] > summary { margin-bottom: 10px; }
	    .mailbox-status { margin: 0; font-size: var(--font-size-base); color: var(--text); overflow-wrap: anywhere; }
	    .mailbox-status::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 8px; border-radius: 999px; background: var(--success); vertical-align: middle; }
	    .mailbox-actions { justify-content: flex-start; margin-top: 10px; }
	    .servers-line { margin: 0; color: var(--muted); font-size: var(--font-size-sm); font-family: var(--font-mono); overflow-wrap: anywhere; }
	    .servers-line .link-button { font-family: var(--font-sans); font-size: var(--font-size-sm); margin-left: 6px; }
	    .chip-list { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0; padding: 0; list-style: none; }
	    .chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 4px 2px 10px; border: 1px solid var(--line-soft); border-radius: 999px; font-size: var(--font-size-sm); background: var(--panel-2, var(--panel)); }
	    .chip__remove { min-height: 24px; min-width: 24px; padding: 0 6px; border: 0; border-radius: 999px; background: transparent; color: var(--muted); font-size: 15px; line-height: 1; }
	    .chip__remove:hover { color: var(--error); filter: none; }
	    .chip-list__empty { color: var(--muted); font-size: var(--font-size-sm); }
	    /* DESIGN §3 (amendment 22:35): helper text is 13 px (never 11-12 px) on these pages. */
	    #tab-users .af-form__help, #tab-users .af-switch__desc, #tab-users .af-switch__reason, #tab-users .af-card__desc,
	    #tab-users .section-note, #tab-users .inline-state, #user-create-form .af-form__help,
	    #login-section .af-gateway-signin__source, .servers-line, .chip, .chip-list__empty { font-size: var(--font-size-md); }
	    .users-table td[data-label]::before { font-size: var(--font-size-md); }
	    @media (max-width: 767.98px) {
	      /* DESIGN §12: phones use the full width — the account page's cards are
	         flat sections with a hairline divider (no card in a card), and the
	         users table becomes one block per user with label/value lines
	         (one page scroll, no inner horizontal scroll box). */
	      .account-page { gap: 0; max-width: none; }
	      .account-card { border: 0; background: transparent; padding: 16px 0; border-top: 1px solid var(--line-soft); }
	      .users-table-wrap { overflow: visible; }
	      /* One page scroll on phones (§12): bounded list boxes grow instead of scrolling inside the page. */
	      .table-scroll { max-height: none; }
	      #tab-users #users-section { border: 0; background: transparent; box-shadow: none; padding: 8px 0 0; }
	    }
	    /* ---- Workflows (DESIGN-v2 §4): purpose line, one row per bundle with a plain
	       name and what it does, expandable versions; per-app defaults below, neutral
	       unless broken. Nothing is coloured except a broken default and "Deprecated". */
	    .workflows-head { margin-bottom: 12px; align-items: center; }
	    .workflows-purpose { max-width: 80ch; margin: 0; }
	    .workflows-toolbar { flex-wrap: wrap; align-items: center; gap: 8px 16px; }
	    .workflows-toolbar input[type="search"] { flex: 1 1 260px; min-width: 0; }
	    .workflows-switch { display: inline-flex; align-items: center; }
	    .workflows-scroll { max-height: none; overflow: visible; }
	    /* >= 1024 px: a real table, one compact row per bundle (fixed columns, the description
	       clamped to two lines, full text when the row is expanded). */
	    .workflows-table { width: 100%; table-layout: fixed; border-collapse: collapse; }
	    .workflows-table th { white-space: nowrap; }
	    .workflows-table td { vertical-align: top; padding-top: 10px; padding-bottom: 10px; overflow-wrap: anywhere; }
	    .workflows-table th.workflows-col-name { width: 18%; }
	    .workflows-table th.workflows-col-what { width: auto; }
	    .workflows-table th.workflows-col-version { width: 6.5rem; }
	    .workflows-table th.workflows-col-source { width: 11rem; }
	    .workflows-table th.workflows-col-usedby { width: 15rem; }
	    .workflows-table th.workflows-col-actions { width: 9.5rem; }
	    .workflows-name__wrap { display: flex; gap: 6px; align-items: flex-start; }
	    .workflows-name__text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
	    .workflows-name__text small { font-size: var(--af-helper-size, var(--font-size-md)); font-family: var(--font-mono); overflow-wrap: anywhere; }
	    .workflows-chevron { color: var(--muted); width: 1em; flex: 0 0 auto; }
	    .workflows-what { color: var(--text); }
	    .workflows-clamp { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
	    .workflows-row[aria-expanded="true"] .workflows-clamp { display: block; -webkit-line-clamp: unset; line-clamp: none; overflow: visible; }
	    .workflows-fold-what, .workflows-fold-meta { display: none; }
	    .workflows-fold-what, .workflows-fold-meta { font-size: var(--af-helper-size, var(--font-size-md)); color: var(--muted); font-weight: 400; }
	    .workflows-fold-what { color: var(--text); font-size: var(--font-size-base); }
	    .workflows-usedby__item { display: block; }
	    .workflows-usedby__item > .help-q { margin-left: 6px; }
	    .workflows-usedby__item + .workflows-usedby__item { margin-top: 4px; }
	    .workflows-actions .actions { flex-wrap: nowrap; justify-content: flex-end; gap: 6px; }
	    .workflows-actions .actions > button { white-space: nowrap; }
	    .workflows-confirm > td { padding-top: 0; }
	    .workflows-confirm-box { margin-top: 8px; }
	    .workflows-detail > td { background: var(--panel-2, transparent); padding: 10px 14px 14px 36px; }
	    .workflows-versions { display: flex; flex-direction: column; gap: 12px; }
	    .workflows-version__head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
	    .workflows-version__head .actions { margin-left: auto; }
	    .workflows-entry { display: grid; grid-template-columns: minmax(120px, max-content) minmax(0, 1fr) auto; gap: 2px 14px; padding: 4px 0 0 2px; font-size: var(--font-size-base); }
	    .workflows-entry__name { font-weight: 500; }
	    .workflows-entry__desc, .workflows-entry__ifaces { color: var(--muted); }
	    /* (?) help: a small 18 px glyph; on touch a 44 px hit area from a pseudo-element, so
	       the row keeps its height and the glyph sits on the text's centre line. */
	    .help-q { display: inline-flex; position: relative; vertical-align: middle; line-height: 1; }
	    details.help-q > summary { --tap-min: 18px; position: relative; min-height: 0; min-width: 0; padding: 0; box-sizing: border-box; list-style: none; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-size: 11px; font-weight: 600; cursor: pointer; line-height: 1; }
	    .help-q > summary::-webkit-details-marker { display: none; }
	    .help-q[open] > summary { color: var(--text); border-color: var(--accent); }
	    .help-q__text { position: absolute; z-index: 5; left: 0; top: calc(100% + 6px); width: min(320px, 70vw); margin: 0; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--bg-secondary); box-shadow: var(--shadow); color: var(--text); font-size: var(--font-size-md); font-weight: 400; line-height: 1.45; white-space: normal; }
	    @media (pointer: coarse) {
	      .help-q > summary::after { content: ""; position: absolute; left: 50%; top: 50%; width: 44px; height: 44px; transform: translate(-50%, -50%); }
	    }
	    .workflows-defaults { border-top: 1px solid var(--line-soft); padding-top: 18px; margin-top: 8px; }
	    .workflows-defaults .section-subtitle { margin: 0 0 4px; }
	    .agent-defaults { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px 18px; }
	    .agent-defaults__other { grid-column: 1 / -1; }
	    .agent-defaults__other > summary { margin: 4px 0 8px; }
	    .agent-defaults__other { display: block; }
	    .agent-defaults__other .agent-default { margin-bottom: 10px; max-width: 520px; }
	    .agent-default { display: flex; flex-direction: column; gap: 4px; min-width: 0; padding: 10px 0; border-top: 1px solid var(--line-soft); }
	    .agent-default__head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
	    .agent-default__name { font-weight: 500; font-size: var(--font-size-base); text-transform: none; letter-spacing: normal; color: var(--text); }
	    .agent-default__iface { font-size: var(--font-size-sm); color: var(--muted); background: transparent; border: 0; padding: 0; }
	    .agent-default__control { display: flex; align-items: center; gap: 8px; min-width: 0; }
	    .agent-default__control select { flex: 1 1 auto; min-width: 0; max-width: 360px; }
	    .agent-default__state { margin: 0; color: var(--muted); font-size: var(--font-size-md); }
	    /* Settings under the per-app defaults (gateway-wide runtime settings). */
	    .workflows-settings { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line-soft); }
	    .workflows-settings .af-switch--row { width: 100%; max-width: none; }
	    .workflows-settings .ui-advanced { display: block; margin-top: 6px; }
	    /* 768-1023 px: the table keeps Name | Version | Used by | Actions; what it does and the
	       source fold under the name. */
	    @media (max-width: 1023.98px) {
	      .workflows-table .workflows-th-what, .workflows-table .workflows-th-source, .workflows-table td.workflows-what, .workflows-table td.workflows-source { display: none; }
	      .workflows-table th.workflows-col-name { width: auto; }
	      .workflows-table th.workflows-col-version { width: 6.5rem; }
	      .workflows-table th.workflows-col-usedby { width: 12rem; }
	      .workflows-table th.workflows-col-actions { width: 11rem; }
	      .workflows-fold-what { display: -webkit-box; margin-top: 2px; }
	      .workflows-fold-meta { display: block; }
	      .workflows-fold-version { display: none; }
	      .agent-defaults { grid-template-columns: minmax(0, 1fr); }
	    }
	    /* < 768 px: one flat block per bundle — name + actions, what it does, ONE meta line
	       "Version x · source", then "Used by · …" (DESIGN §12: label · value on one line). */
	    @media (max-width: 767.98px) {
	      .workflows-table, .workflows-table tbody, .workflows-table tr, .workflows-table td { display: block; width: 100%; }
	      .workflows-table thead { display: none; }
	      .workflows-table tr.workflows-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; padding: 12px 0; border-top: 1px solid var(--line-soft); }
	      .workflows-table tr.workflows-row > td { padding: 0; border: 0; width: auto; }
	      .workflows-table td.workflows-name { grid-column: 1 / -1; grid-row: 1; }
	      .workflows-table td.workflows-actions { grid-column: 1 / -1; grid-row: 3; margin-top: 4px; }
	      .workflows-actions .actions { justify-content: flex-start; }
	      .workflows-table td.workflows-version-cell { display: none; }
	      .workflows-fold-version { display: inline; }
	      .workflows-usedby { grid-column: 1 / -1; grid-row: 2; display: flex !important; flex-wrap: wrap; align-items: center; gap: 4px 6px; }
	      .workflows-usedby__item + .workflows-usedby__item::before { content: "·"; margin-right: 6px; color: var(--muted); }
	      .workflows-table td.workflows-usedby::before { content: "Used by · "; color: var(--muted); }
	      .workflows-usedby__item + .workflows-usedby__item { margin-top: 0; }
	      .workflows-actions .actions > button { min-height: 44px; }
	      .workflows-confirm, .workflows-confirm > td { display: block; padding: 0 0 10px; border: 0; }
	      .workflows-detail > td { padding: 8px 0 12px; }
	      .workflows-entry { grid-template-columns: minmax(0, 1fr); }
	    }
	    /* ---- Account email UI (DESIGN-v2 §3): IMAP servers always visible, one row per
	       server (Server · Port · Security) from 768 px; Advanced as plain sentences. */
	    .mail-server-row { border: 0; margin: 0; padding: 0; min-width: 0; }
	    .mail-server-row > legend { padding: 0; margin: 0 0 6px; }
	    .mail-server-row__fields { display: grid; grid-template-columns: minmax(0, 1fr) 6.5em 9em; gap: 10px; }
	    .mail-server-row__caption { color: var(--muted); font-weight: 500; font-size: var(--font-size-md); }
	    .servers-source { margin: 0; }
	    .login-toggle { margin: 0; }
	    .login-toggle .link-button, .notify-test-row .inline-state { font-size: var(--font-size-md); }
	    .notify-test-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin: 10px 0 0; }
	    .mailbox-active { margin-top: 10px; }
	    .mailbox-differs { color: var(--muted); }
	    .address-view, .address-line { margin: 0; font-size: var(--font-size-base); color: var(--text); overflow-wrap: anywhere; }
	    .address-view .link-button, .address-line .link-button { margin-left: 6px; }
	    .advanced-sentences { gap: 10px; }
	    .advanced-sentence { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; font-size: var(--font-size-base); color: var(--text); }
	    .advanced-sentence select { width: auto; }
	    .advanced-sentence input.advanced-num { width: 5.5em; text-align: right; }
	    .advanced-sentence input.advanced-folder { width: 12em; max-width: 100%; }
	    .advanced-sentences .af-form__help { margin: -4px 0 4px; }
	    .advanced-add { max-width: 520px; }
	    @media (max-width: 767.98px) {
	      .mail-server-row__fields { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
	      .mail-server-row__host { grid-column: 1 / -1; }
	    }
	    /* ---- Accounts (DESIGN-v2 §2.1): header row, ONE full-width table (kit row
	       tints + kind chips), actions that wrap, the reasons of unavailable actions
	       visible in the row. 834: Role / Email address / Runtime fold under the name.
	       390: one flat block per row (no card in a card), 44 px targets. */
	    #tab-users #account { display: none; }
	    /* One subheading size on every round-2 page (= the card heading, h2 15 px / 600). */
	    .section-subtitle { font-size: calc(15px * var(--font-scale)); font-weight: 600; color: var(--text); margin: 0 0 4px; }
	    .accounts-page { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
	    /* The top bar carries the page title and its line: the card header is the buttons row only. */
	    .accounts-head { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
	    .accounts-head:not(:has(button:not(.hidden))) { display: none; }
	    .accounts-head__actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
	    .accounts-table { width: 100%; border-collapse: collapse; }
	    .accounts-table th { text-align: left; white-space: nowrap; }
	    .accounts-table td { vertical-align: top; padding-top: 12px; padding-bottom: 12px; }
	    .accounts-name__line { display: inline-flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }
	    .accounts-fold, .accounts-phone-line { display: none; }
	    .accounts-mailbox { overflow-wrap: normal; }
	    .accounts-table th, .accounts-table td { padding-left: 10px; padding-right: 10px; }
	    #users-section > .message:empty, #users-section > .issued.hidden { display: none; }
	    #email-caps-message:empty { display: none; }
	    .accounts-none { font-style: normal; }
	    /* Active: the switch only. Its unavailable reason (aria-describedby) is written in the row's
	       ONE reasons line under the actions, so the column stays narrow. */
	    .accounts-table td.accounts-active { width: 1%; }
	    .accounts-active .af-switch__reason { display: none; }
	    /* Actions: compact, icon + label, one height; one row where the width allows. */
	    .accounts-table td.accounts-actions { width: 1%; min-width: 0; }
	    .users-table td.accounts-actions { white-space: normal; }
	    .accounts-actions__buttons { display: flex; flex-wrap: wrap; gap: 6px; }
	    .accounts-actions__buttons > button { display: inline-flex; align-items: center; gap: 5px; height: 32px; min-height: 32px; padding: 0 9px; font-size: var(--font-size-md); white-space: nowrap; }
	    .accounts-actions__buttons > button .button-icon svg { width: 14px; height: 14px; }
	    .accounts-reasons { margin: 6px 0 0; font-size: var(--af-helper-size, var(--font-size-md)); line-height: 1.4; }
	    .accounts-legend { margin: 0; }
	    /* Email for everyone: a full-width settings row (label + description left, switch at the card edge). */
	    #users-section .accounts-email { margin: 8px 0 0; padding: 16px 0 0; border: 0; border-top: 1px solid var(--line-soft); background: transparent; box-shadow: none; max-width: none; width: 100%; }
	    #users-section .accounts-email .switch-list, #users-section .accounts-email .af-switch--row { width: 100%; max-width: none; }
	    #users-section .accounts-email .plain-disclosure { margin-top: 8px; }
	    @media (min-width: 1440px) {
	      .accounts-actions__buttons { flex-wrap: nowrap; }
	    }
	    @media (max-width: 1439.98px) {
	      .accounts-table td.accounts-actions { width: 42%; }
	      .accounts-table .accounts-col-email, .accounts-table .accounts-col-runtime,
	      .accounts-table th:nth-child(2), .accounts-table th:nth-child(4) { display: none; }
	      .accounts-fold { display: block; margin-top: 4px; font-size: var(--af-helper-size, var(--font-size-md)); overflow-wrap: break-word; }
	      .accounts-fold > span { display: block; }
	    }
	    @media (max-width: 767.98px) {
	      .accounts-head { justify-content: stretch; }
	      .accounts-head__actions { flex: 1 1 100%; }
	      .accounts-head__actions > button { flex: 1 1 100%; min-height: 44px; }
	      .accounts-table, .accounts-table tbody, .accounts-table tr { display: block; width: 100%; }
	      .accounts-table thead { display: none; }
	      .accounts-table tr.accounts-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; padding: 12px 12px 12px 14px; border-top: 1px solid var(--line-soft); }
	      .accounts-table tr.accounts-row > td { display: block; padding: 0; border: 0; min-width: 0; width: auto; }
	      .accounts-table tr.accounts-row > td::before { content: none; }
	      .accounts-table td.accounts-name { grid-column: 1; grid-row: 1; box-shadow: none; align-self: center; }
	      .accounts-table td.accounts-active { grid-column: 2; grid-row: 1; justify-self: end; }
	      .accounts-table td.accounts-mailbox { grid-column: 1 / -1; grid-row: 2; }
	      .accounts-table td.accounts-actions { grid-column: 1 / -1; grid-row: 4; margin-top: 8px; width: auto; min-width: 0; }
	      .accounts-table tr.accounts-row > td.accounts-col-email { display: none; }
	      /* One flat block per row: the tint and the kind bar paint the row, not each cell. */
	      .accounts-table tr.accounts-row > td { background: transparent !important; box-shadow: none !important; }
	      .accounts-table tr.af-row--admin { background-color: var(--af-row-tint-admin); box-shadow: inset 3px 0 0 var(--af-row-mark-admin); }
	      .accounts-table tr.af-row--user { box-shadow: inset 3px 0 0 var(--af-row-mark-user); }
	      .accounts-table tr.af-row--entity { background-color: var(--af-row-tint-entity); box-shadow: inset 3px 0 0 var(--af-row-mark-entity); }
	      .accounts-fold { display: none; }
	      .accounts-table .accounts-col-runtime { display: block !important; grid-column: 1 / -1; grid-row: 3; color: var(--af-row-text-muted, var(--muted)); }
	      .accounts-table .accounts-col-runtime::before { content: "Runtime " !important; }
	      .accounts-phone-line { display: block; }
	      .accounts-mailbox__text { display: none; }
	      .accounts-actions__buttons { gap: 8px; }
	      .accounts-actions__buttons > button { height: 44px; min-height: 44px; padding: 0 12px; font-size: var(--font-size-base); }
	      .accounts-table .row-confirm, .accounts-table .row-confirm > td { display: block; padding: 0 0 10px; border: 0; }
	    }
	    /* Account modals (kit af-modal): content rules only; the shell is the kit's. */
	    .account-modal-body .account-page { max-width: none; }
	    /* An empty status line takes no room at the top of the Email modal (it showed as a blank band). */
	    .account-modal-body #my-email-message:empty { display: none; }
	    .account-modal-body .account-page__head { display: none; }
	    .account-modal-lead { margin: 0 0 14px; padding: 10px 12px; border-left: 3px solid var(--info, var(--accent)); background: color-mix(in srgb, var(--info, var(--accent)) 8%, transparent); border-radius: var(--radius-sm); font-size: var(--font-size-base); line-height: 1.45; }
	    .account-other-mailbox { margin-top: 12px; }
	    .account-logs-filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; }
	    .account-logs-chip { min-height: 30px; padding: 4px 12px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--text); font-size: var(--font-size-md); font-weight: 500; }
	    .account-logs-chip[aria-pressed="true"] { background: color-mix(in srgb, var(--accent) 16%, transparent); border-color: color-mix(in srgb, var(--accent) 60%, transparent); font-weight: 600; }
	    .account-logs-list { list-style: none; margin: 0; padding: 0; }
	    .account-logs-item { display: grid; grid-template-columns: 7.5em minmax(0, auto) minmax(0, 1fr) auto; gap: 4px 14px; align-items: baseline; padding: 9px 2px; border-top: 1px solid var(--line-soft); font-size: var(--font-size-base); }
	    .account-logs-item:first-child { border-top: 0; }
	    .account-logs-item__time { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
	    .account-logs-item__title { font-weight: 500; }
	    .account-logs-item__detail { color: var(--muted); overflow-wrap: anywhere; }
	    .account-logs-item.is-failed .account-logs-item__title { color: var(--error); }
	    .account-logs-empty { padding: 14px 2px; color: var(--muted); }
	    @media (max-width: 767.98px) {
	      .account-logs-item { grid-template-columns: auto minmax(0, 1fr); }
	      .account-logs-item__detail, .account-logs-item__link { grid-column: 1 / -1; }
	      .account-logs-chip { min-height: 44px; }
	    }
	    @media (prefers-reduced-motion: reduce) {
	      .plain-disclosure > summary::before, .account-advanced > summary::before { transition: none; }
	    }
	  </style>
</head>
<body>
	  <!-- FAMILY SHELL: left sidebar + slim
	       header — the layout vocabulary continuum and observer share
	       (.shell_* family). Nav button ids are unchanged so the tab wiring
	       and its tests survive the restyle. -->
	  <div class="shell">
	  <aside id="console-nav" class="shell_sidebar session-only" aria-label="Gateway console sections">
	    <div class="shell_brand">
	      <span class="shell_brand_mark" aria-hidden="true">↔</span>
	      <span class="shell_brand_name">AbstractGateway</span>
	      <!-- Below 1024 px the sidebar is a drawer: its close button. -->
	      <button id="nav-close" class="shell_nav_close" type="button" aria-label="Close navigation" title="Close">×</button>
	    </div>
	    <!-- Sidebar groups (DESIGN-v2 §1): what an admin does most -> configures -> maintains.
	         Captions are the kit eyebrow style (.af-nav-group), never buttons. -->
	    <nav class="shell_nav" aria-label="Console sections">
	      <div class="shell_nav_group af-nav-group" role="group" aria-labelledby="nav-group-accounts">
	        <div id="nav-group-accounts" class="shell_nav_caption af-nav-group__caption">Accounts</div>
	        <button id="tab-button-users" class="tab-button shell_nav_item" type="button" title="People who use this gateway and the entities that act on it"><span class="shell_nav_icon" aria-hidden="true">☾</span><span class="shell_nav_label">Accounts</span></button>
	      </div>
	      <div class="shell_nav_group af-nav-group" role="group" aria-labelledby="nav-group-work">
	        <div id="nav-group-work" class="shell_nav_caption af-nav-group__caption">Work</div>
	        <button id="tab-button-workflows" class="tab-button shell_nav_item" type="button" title="Bundles, versions, import and export"><span class="shell_nav_icon" aria-hidden="true">⑂</span><span class="shell_nav_label">Workflows</span></button>
	        <button id="tab-button-skills" class="tab-button shell_nav_item" type="button" title="Skills agents can load, and the MCP tool servers this gateway knows"><span class="shell_nav_icon" aria-hidden="true">✦</span><span class="shell_nav_label">Skills &amp; MCP</span></button>
	        <button id="tab-button-runtimes" class="tab-button shell_nav_item" type="button" title="Each user's data plane: runs, flows, sessions and memory"><span class="shell_nav_icon" aria-hidden="true">◎</span><span class="shell_nav_label">Runtimes</span></button>
	        <button id="tab-button-apps" class="tab-button shell_nav_item" type="button" title="Browser apps (Flow, Code, Observer...): install, start, open"><span class="shell_nav_icon" aria-hidden="true">▣</span><span class="shell_nav_label">Apps</span></button>
	      </div>
	      <div class="shell_nav_group af-nav-group" role="group" aria-labelledby="nav-group-models">
	        <div id="nav-group-models" class="shell_nav_caption af-nav-group__caption">Models</div>
	        <button id="tab-button-providers" class="tab-button shell_nav_item" type="button" title="Local engines (Ollama, LM Studio, MLX...) and remote provider connections"><span class="shell_nav_icon" aria-hidden="true">◇</span><span class="shell_nav_label">Providers</span></button>
	        <button id="tab-button-catalog" class="tab-button shell_nav_item" type="button" title="Browse, download and delete models that fit this machine"><span class="shell_nav_icon" aria-hidden="true">▤</span><span class="shell_nav_label">Models</span></button>
	        <button id="tab-button-defaults" class="tab-button shell_nav_item" type="button" title="Which provider/model serves each capability (vision, audio, image...)"><span class="shell_nav_icon" aria-hidden="true">◆</span><span class="shell_nav_label">Multimodal</span></button>
	      </div>
	      <div class="shell_nav_group af-nav-group" role="group" aria-labelledby="nav-group-system">
	        <div id="nav-group-system" class="shell_nav_caption af-nav-group__caption">System</div>
	        <button id="tab-button-models" class="tab-button shell_nav_item" type="button" title="Host resources: loaded models, memory and GPU, session caches"><span class="shell_nav_icon" aria-hidden="true">▦</span><span class="shell_nav_label">Resources</span></button>
	        <button id="tab-button-sandbox" class="tab-button shell_nav_item" type="button" title="Try any provider/model directly — text, image, audio, video"><span class="shell_nav_icon" aria-hidden="true">▶</span><span class="shell_nav_label">Sandbox</span></button>
	        <button id="tab-button-network" class="tab-button shell_nav_item" type="button" title="Who can reach this gateway (this computer, local network, internet) and its addresses"><span class="shell_nav_icon" aria-hidden="true">⌖</span><span class="shell_nav_label">Network</span></button>
	      </div>
	    </nav>
	    <div class="shell_sidebar_foot">
	      <button id="open-setup" class="shell_setup af-nav-footer__button session-only hidden" type="button" title="Run the setup guide again: engines, default models, apps, network. Keeps your current choices unless you replace them." aria-describedby="open-setup-help"><span class="shell_nav_icon" aria-hidden="true">⚑</span><span>Setup</span></button>
	      <span id="open-setup-help" class="sr-only">Run the setup guide again: engines, default models, apps, network. Keeps your current choices unless you replace them.</span>
	      <label class="ui-switch" title="Show commands, route ids and other technical details"><input id="sidebar-advanced" type="checkbox"><span>Technical details</span></label>
	    </div>
	  </aside>
	  <div id="nav-backdrop" class="shell_nav_backdrop" aria-hidden="true"></div>
	  <div class="shell_main">
	  <header class="shell_header">
	    <button id="nav-toggle" class="shell_nav_toggle" type="button" aria-controls="console-nav" aria-expanded="false" aria-label="Open navigation" title="Sections">☰</button>
	    <div class="shell_header_titles">
	      <h1 id="page-title">AbstractGateway Console</h1>
	      <div id="page-subtitle" class="brand-subtitle">Users &amp; summoned entities, runtimes, providers, and multimodal capabilities</div>
	    </div>
	    <!-- Unified top-right cluster: .af-topbar / .af-drawer are abstractuic's
	         documented CSS public API for non-React consumers (ui-kit README).
	         Enforced order: assistant, appearance, [extras], connection pill. -->
	    <!-- The kit's AfTopBarActions island mounts here (console_islands.py);
	         the static cluster below is the same public markup and stays as
	         the no-bundle fallback (and the node-VM tests' surface). -->
	    <div id="af-topbar-root" class="af-topbar-island hidden"></div>
	    <div id="topbar-static" class="status af-topbar" role="group" aria-label="Console actions">
	      <button id="open-assistant" class="af-topbar__btn session-only" title="Docs assistant" aria-label="Open docs assistant" aria-pressed="false">✦</button>
	      <button id="open-appearance" class="af-topbar__btn" title="Appearance" aria-label="Appearance">◐</button>
	      <span id="status-dot" class="dot"></span>
	      <span id="status-text">Signed out</span>
	      <button id="sign-out" class="af-topbar__pill af-topbar__pill--connected hidden" title="Sign out of the gateway session" aria-label="Sign out">
	        <span class="af-topbar__dot af-topbar__dot--connected" aria-hidden="true"></span>
	        <span class="af-topbar__pill-label">Sign out</span>
	      </button>
	    </div>
	  </header>
	  <!-- Host pause (tray + console): a pause set last week from the
	       menu bar must be visible on EVERY tab, before choosing one. Driven by
	       GET /host/runner (15s poll while signed in) and by the Gateway card. -->
	  <div id="paused-banner" class="entity-stop-banner hidden" role="status">
	    <span id="paused-banner-text">Workflows are paused.</span>
	    <button id="paused-banner-resume" class="secondary" type="button" title="Resume workflow execution on this gateway">Resume workflows</button>
	  </div>
	  <main class="console-shell shell_content">
	    <!-- Sign-in card (DESIGN §4): the kit's af-gateway-signin block. One
	         status pill, labels above fields, inline errors, ONE quiet recovery
	         link that turns into the code step. Nothing green stays static. -->
	    <section id="login-section" class="af-gateway-signin">
	      <div class="af-gateway-signin__hero">
	        <div>
	          <div class="af-gateway-signin__kicker">AbstractGateway Console</div>
	          <h2>Sign in</h2>
	          <p>Use the token your gateway admin gave you.</p>
	        </div>
	      </div>
	      <div class="af-gateway-signin__status-row">
	        <span id="login-status" class="af-gateway-signin__status af-gateway-signin__status--neutral" role="status" aria-live="polite">Not signed in</span>
	      </div>
	      <form id="login-form" class="af-gateway-signin__form" novalidate>
	        <div class="af-gateway-signin__field">
	          <label class="af-gateway-signin__label" for="login-user">Gateway user</label>
	          <input id="login-user" autocomplete="username" value="admin" aria-describedby="login-user-error">
	          <p id="login-user-error" class="af-gateway-signin__field-error" role="alert" hidden></p>
	        </div>
	        <div class="af-gateway-signin__field">
	          <label class="af-gateway-signin__label" for="login-token">Token</label>
	          <div class="af-gateway-signin__token-input">
	            <input id="login-token" autocomplete="current-password" type="password" aria-describedby="login-token-error">
	            <button id="toggle-token" type="button" aria-label="Show token" aria-pressed="false">Show</button>
	          </div>
	          <p id="login-token-error" class="af-gateway-signin__field-error" role="alert" hidden></p>
	        </div>
	        <div class="af-gateway-signin__submit-row">
	          <label class="af-gateway-signin__checkbox"><input id="login-remember" type="checkbox"> Remember this browser</label>
	          <button id="login-button" class="af-gateway-signin__primary" type="submit">Sign in</button>
	        </div>
	      </form>
	      <p id="login-message" class="af-gateway-signin__message af-gateway-signin__message--error" role="alert" hidden></p>
	      <!-- Shown only when GET /session/recovery says sign-in by email is on. -->
	      <div id="recovery-section" class="af-gateway-signin__recovery" hidden>
	        <button id="recovery-link" type="button" class="af-gateway-signin__link">Forgot your token? Email me a sign-in code</button>
	        <p id="recovery-request-message" class="af-gateway-signin__message af-gateway-signin__message--error" role="alert" hidden></p>
	      </div>
	      <div id="recovery-code-step" class="af-gateway-signin__code" hidden>
	        <p id="recovery-sent-message" class="af-gateway-signin__message af-gateway-signin__message--ok" role="status" aria-live="polite"></p>
	        <div class="af-gateway-signin__field">
	          <label class="af-gateway-signin__label" for="recovery-code-input">Code from the email</label>
	          <input id="recovery-code-input" autocomplete="one-time-code" inputmode="numeric" maxlength="8" pattern="[0-9]{8}" aria-describedby="recovery-code-error">
	          <p id="recovery-code-error" class="af-gateway-signin__field-error" role="alert" hidden></p>
	        </div>
	        <div class="af-gateway-signin__code-actions">
	          <button id="recovery-resend" type="button" class="af-gateway-signin__link">Send a new code</button>
	          <button id="recovery-use" type="button" class="af-gateway-signin__primary" disabled>Use code</button>
	        </div>
	        <button id="recovery-back" type="button" class="af-gateway-signin__link af-gateway-signin__back">Back to token</button>
	      </div>
	    </section>

	    <div id="workspace-shell" class="workspace-shell session-only">
	      <!-- WORKFLOWS: the registry surface. A row is a BUNDLE, not a version —
	           "how many versions" is an explicit goal and a count has to be a
	           COLUMN, which forces bundle-as-row. The version cell carries TWO
	           numbers (published / draft) because one total misrepresents a
	           registry that is majority drafts. Versions that failed to load are
	           listed SEPARATELY with their reason rather than omitted: the file
	           is still on disk and still needs a decision. -->
	      <div id="tab-workflows" class="tab-panel">
	        <div class="tab-grid tab-grid-wide">
	          <div class="tab-stack">
	            <!-- WORKFLOWS (DESIGN-v2 §4): what exists (one row per bundle, plain names and
	                 what each does), then — secondary — which workflow each app runs by default. -->
	            <section id="workflows-section" class="session-only workflows-page">
	              <div class="section-head workflows-head">
	                <div>
	                  <p class="section-note workflows-purpose">Workflows are the programs your apps and automations run. They come in bundles (.flow files): some ship with the gateway, others you import or publish from AbstractFlow.</p>
	                </div>
	                <button id="workflows-refresh" class="secondary icon-only" title="Reload the workflow registry" aria-label="Refresh workflows"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	              </div>
	              <div class="list-toolbar workflows-toolbar">
	                <input id="workflows-search" type="search" placeholder="Search by name, description or id" aria-label="Search workflows">
	                <span id="workflows-drafts-slot" class="workflows-switch"></span>
	                <span id="workflows-older-slot" class="workflows-switch"></span>
	                <button id="workflows-import" class="secondary" type="button" title="Install a .flow bundle">Import .flow</button>
	                <input id="workflows-import-file" type="file" accept=".flow" class="hidden" multiple>
	              </div>
	              <div id="workflows-message" class="message" role="status" aria-live="polite"></div>
	              <div class="table-scroll workflows-scroll" id="workflows-scroll">
	                <table class="workflows-table" data-ui-no-stack>
	                  <thead><tr><th class="workflows-col-name">Name</th><th class="workflows-col-what workflows-th-what">What it does</th><th class="workflows-col-version">Version</th><th class="workflows-col-source workflows-th-source">Source</th><th class="workflows-col-usedby">Used by</th><th class="workflows-col-actions"><span class="sr-only">Actions</span></th></tr></thead>
	                  <tbody id="workflows-table"></tbody>
	                </table>
	              </div>
	            </section>

	            <section id="workflows-skipped-section" class="session-only hidden">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">⚠</span><span>Broken workflows</span></h2>
	                  <p id="workflows-skipped-count" class="section-note"></p>
	                  <p class="section-note">These bundle files are on disk but the gateway cannot run them, so they do not appear in the list. Nothing was deleted — fix the cause and reload, or remove them deliberately.</p>
	                </div>
	              </div>
	              <div class="table-scroll">
	                <table>
	                  <thead><tr><th>Workflow</th><th>Affected</th><th>Why the gateway cannot run it</th><th>Actions</th></tr></thead>
	                  <tbody id="workflows-skipped-table"></tbody>
	                </table>
	              </div>
	            </section>

	            <section id="agent-defaults-section" class="session-only workflows-defaults">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-subtitle">Default workflow per app</h2>
	                  <p class="section-note">When an app asks for “an agent” without naming a workflow, the gateway runs this one.</p>
	                </div>
	              </div>
	              <div id="agent-defaults-root"></div>
	            </section>
	          </div>
	        </div>
	      </div>

      <div id="tab-skills" class="tab-panel">
        <!-- SKILLS & MCP (DESIGN-v3 §6, console_skills_mcp.py): kit tabs Skills | MCP servers. -->
        <section id="skills-mcp-section" class="session-only skmcp-page">
          <div class="af-tabs">
            <div class="af-tabs__list" role="tablist" aria-label="Skills and MCP servers">
              <button id="skmcp-tab-skills" class="af-tabs__tab" role="tab" type="button" data-skmcp-tab="skills" aria-controls="skmcp-pane-skills" aria-selected="true">Skills</button>
              <button id="skmcp-tab-mcp" class="af-tabs__tab" role="tab" type="button" data-skmcp-tab="mcp" aria-controls="skmcp-pane-mcp" aria-selected="false" tabindex="-1">MCP servers</button>
            </div>
            <div id="skmcp-pane-skills" class="af-tabs__panel" role="tabpanel" aria-labelledby="skmcp-tab-skills">
              <p class="section-note skmcp-purpose">Instructions agents load when a task needs them: curated ones ship with the gateway, imported ones are yours to edit.</p>
              <div class="skmcp-toolbar">
                <input id="skills-search" type="search" placeholder="Search by name or description" aria-label="Search skills">
                <span id="skills-archived-slot" class="workflows-switch"></span>
                <span class="skmcp-spacer"></span>
                <button id="skills-import-zip" class="secondary" type="button" title="Import a skill from a .zip of its folder" hidden>Import .zip</button>
                <button id="skills-import-folder" class="secondary" type="button" title="Import a skill folder (it holds SKILL.md)" hidden>Import folder</button>
                <input id="skills-import-zip-file" type="file" accept=".zip,application/zip" class="hidden">
                <input id="skills-import-folder-file" type="file" class="hidden" webkitdirectory multiple>
              </div>
              <div id="skills-message" class="message skmcp-message" role="status" aria-live="polite"></div>
              <p id="skills-warnings" class="skmcp-help"></p>
              <div class="table-scroll workflows-scroll">
                <table class="skmcp-table skills-table" data-ui-no-stack>
                  <thead><tr><th class="sk-col-name">Name</th><th class="sk-col-what sk-th-what">What it does</th><th class="sk-col-version">Version</th><th class="sk-col-trust">Trust</th><th class="sk-col-source sk-th-source">Source</th><th class="sk-col-actions"><span class="sr-only">Actions</span></th></tr></thead>
                  <tbody id="skills-table"></tbody>
                </table>
              </div>
              <details class="skmcp-shelf">
                <summary>Shelf folder</summary>
                <div id="skills-settings-root" class="core-console-root"></div>
              </details>
            </div>
            <div id="skmcp-pane-mcp" class="af-tabs__panel" role="tabpanel" aria-labelledby="skmcp-tab-mcp" hidden>
              <p id="mcp-truth" class="skmcp-truth"></p>
              <p class="section-note skmcp-purpose">Tool servers over the Model Context Protocol: Test runs the real handshake and lists their tools.</p>
              <div class="skmcp-toolbar">
                <span id="mcp-archived-slot" class="workflows-switch"></span>
                <span class="skmcp-spacer"></span>
                <button id="mcp-add" type="button" hidden>Add server</button>
              </div>
              <div id="mcp-message" class="message skmcp-message" role="status" aria-live="polite"></div>
              <div class="table-scroll workflows-scroll">
                <table class="skmcp-table mcp-table" data-ui-no-stack>
                  <thead><tr><th class="mcp-col-name">Name</th><th class="mcp-col-transport mcp-th-transport">Transport</th><th class="mcp-col-status">Status</th><th class="mcp-col-tools">Tools</th><th class="mcp-col-actions"><span class="sr-only">Actions</span></th></tr></thead>
                  <tbody id="mcp-table"></tbody>
                </table>
              </div>
            </div>
          </div>
        </section>
      </div>
	      <div id="tab-runtimes" class="tab-panel">
	        <div class="tab-grid tab-grid-wide">
	          <div class="tab-stack">
	            <!-- RUNTIMES FIRST + MASTER->TABBED DETAIL:
	                 a HEIGHT-BOUNDED master table (always on screen, sticky header)
	                 with row-click selection; ONE detail pane directly below with
	                 [Runs | Sessions | Caches] subtabs — the old stacked global
	                 sections were "an infinite scroll to get to each". The default
	                 runtime auto-selects on tab open so the operator's run tools
	                 (filter/inspect/steer/cancel) stay ZERO clicks away, exactly as
	                 before. Machine-wide Data & Caches lives in a collapsed
	                 disclosure (not a "Machine" pseudo-row:
	                 it lies in five columns and hides a purge surface behind a
	                 fake runtime identity). -->
	            <section id="runtimes-section" class="session-only hidden">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">◎</span><span>Runtimes</span></h2>
	                  <p class="section-note">A runtime is a user's own data plane: their runs, flows, sessions and memory. Each user gets one, named after them, unless an admin bound them to a shared one. Entities have their own too. Click a runtime to open its runs and cache below; the default runtime's cache also lists every machine-wide store.</p>
	                </div>
	                <button id="runtimes-refresh" class="secondary icon-only" title="Reload the runtime inventory" aria-label="Refresh runtimes"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	              </div>
	              <div id="runtimes-message" class="message"></div>
	              <div class="table-scroll" id="runtimes-scroll">
	                <table>
	                  <thead><tr><th>Runtime</th><th>Kind</th><th>Owner</th><th>State</th><th>Size</th><th>Workspace policy</th></tr></thead>
	                  <tbody id="runtimes-table"></tbody>
	                </table>
	              </div>
	            </section>
	            <section id="runtime-detail-section" class="session-only hidden">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">▷</span><span>Runtime <span id="runtime-detail-name"></span></span></h2>
	                  <p id="runtime-detail-sub" class="section-note">Runs, sessions and caches on this plane.</p>
	                </div>
	                <button id="runtime-detail-refresh" class="secondary icon-only" title="Reload this runtime's view" aria-label="Refresh runtime detail"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	              </div>
	              <!-- Console-TUI mirror: TWO tabs — Sessions |
	                   Data & cache — and NOTHING loads until a runtime is
	                   chosen. The Sessions tab lists the chosen runtime's runs
	                   (session ids on every row), exactly like the TUI's
	                   sessions panel. -->
	              <p id="runtime-detail-teach" class="section-note">Select a runtime above — click a row — to load its runs and cache.</p>
	              <nav class="entity-subtabs hidden" id="runtime-detail-tabs">
	                <button id="runtime-subtab-sessions" class="entity-subtab active" type="button">Runs</button>
	                <button id="runtime-subtab-artifacts" class="entity-subtab" type="button">Artifacts</button>
	                <button id="runtime-subtab-caches" class="entity-subtab" type="button">Cache</button>
	                <button id="runtime-subtab-logs" class="entity-subtab" type="button">Logs</button>
	              </nav>
	              <div id="runtime-panel-sessions" class="hidden">
	                <!-- Default-plane block: the actionable runs machinery
	                     (ids preserved so every existing handler keeps working).
	                     TWO SEPARATE BLOCKS, two tbodies, deliberately: a stale
	                     default loadRuns must never paint action buttons under
	                     another plane's header (a Steer/Cancel there would fire
	                     commands at the DEFAULT runtime — a misfire
	                     class, killed structurally). -->
	                <div id="runtime-runs-default" class="hidden">
	                  <div class="list-toolbar">
	                    <select id="runs-status" title="Filter runs by their durable status" aria-label="Run status filter"><option value="">all statuses</option><option value="running">running</option><option value="waiting">waiting</option><option value="completed">completed</option><option value="failed">failed</option><option value="cancelled">cancelled</option></select>
	                    <input id="runs-search" type="search" spellcheck="false" placeholder="Search runs — run id, workflow, session…" aria-label="Search runs">
	                    <label class="entity-checkbox" title="Hide child runs — one row per top-level run"><input id="runs-root-only" type="checkbox" checked> root runs only</label>
	                  </div>
	                  <div id="runs-message" class="message"></div>
	                  <table>
	                    <thead><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Node</th><th>Session</th><th>Updated</th><th>Actions</th></tr></thead>
	                    <tbody id="runs-table"></tbody>
	                  </table>
	                  <div id="runs-pager" class="list-pager"></div>
	                </div>
	                <div id="runtime-runs-readonly" class="hidden">
	                  <p class="section-note">Read-only view — newest runs on this plane. Inspect/steer/cancel run through the default runtime's command lane and are not available here.</p>
	                  <div id="runtime-detail-message" class="message"></div>
	                  <table>
	                    <thead><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Session</th><th>Updated</th></tr></thead>
	                    <tbody id="runtime-detail-runs"></tbody>
	                  </table>
	                  <div id="runtime-runs-pager" class="list-pager"></div>
	                </div>
	              </div>
	              <div id="runtime-panel-artifacts" class="hidden">
	                <div class="list-toolbar">
	                  <select id="runtime-artifacts-modality" title="Filter artifacts by type" aria-label="Artifact type filter"><option value="">all types</option><option value="image">image</option><option value="video">video</option><option value="audio,voice,music,sound">audio</option><option value="text,markdown,json,code,html">text</option><option value="binary,document">other</option></select>
	                  <input id="runtime-artifacts-search" type="search" spellcheck="false" placeholder="Search artifacts — name, kind, tags, date (YYYY-MM-DD)…" aria-label="Search artifacts" title="Search artifact metadata">
	                </div>
	                <div id="runtime-artifacts-message" class="message"></div>
	                <div class="table-scroll">
	                  <table>
	                    <thead><tr><th>Artifact</th><th>Type</th><th>Size</th><th>Workflow</th><th>Run</th><th>Created</th></tr></thead>
	                    <tbody id="runtime-artifacts-table"></tbody>
	                  </table>
	                </div>
	                <div id="runtime-artifacts-pager" class="list-pager"></div>
	                <p id="runtime-artifacts-note" class="section-note"></p>
	              </div>
	              <div id="runtime-panel-caches" class="hidden">
	                <div class="list-toolbar">
	                  <select id="runtime-caches-kind" title="Filter caches by kind" aria-label="Cache kind filter"><option value="">all kinds</option></select>
	                  <input id="runtime-caches-search" type="search" spellcheck="false" placeholder="Search caches — name, kind, path…" aria-label="Search caches" title="Search the caches on this plane">
	                </div>
	                <p class="section-note">Disposable caches only — purging one just costs recomputation (re-download for model weights, re-encode for prompt KV). Durable stores are never listed here: deliverables live in the Artifacts tab, logs in the Logs tab.</p>
	                <div id="runtime-caches-message" class="message"></div>
	                <table>
	                  <thead><tr><th>Cache</th><th>Kind</th><th>Size</th><th>Path</th><th>Actions</th></tr></thead>
	                  <tbody id="runtime-caches-table"></tbody>
	                </table>
	                <p id="runtime-caches-remainder" class="section-note"></p>
	              </div>
	              <div id="runtime-panel-logs" class="hidden">
	                <div class="list-toolbar">
	                  <select id="runtime-logs-home" title="Filter by log home" aria-label="Log home filter"><option value="">all log homes</option></select>
	                  <input id="runtime-logs-search" type="search" spellcheck="false" placeholder="Search log files — file name…" aria-label="Search log files" title="Search this plane's log files">
	                </div>
	                <div id="runtime-logs-message" class="message"></div>
	                <div class="table-scroll">
	                  <table>
	                    <thead><tr><th>File</th><th>Log home</th><th>Size</th><th>Modified</th></tr></thead>
	                    <tbody id="runtime-logs-table"></tbody>
	                  </table>
	                </div>
	                <p id="runtime-logs-note" class="section-note"></p>
	              </div>
	            </section>
		            <section id="runtime-reservations-section" class="session-only hidden">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">◫</span><span>Retained runtimes</span></h2>
	                  <p class="section-note">Deleted or reassigned users leave their runtime data retained here — transfer it to a new owner or purge it permanently.</p>
	                </div>
	              </div>
	              <div id="reservations-message" class="message"></div>
	              <table>
	                <thead><tr><th>Tenant</th><th>Runtime</th><th>Owner</th><th>Reason</th><th>Data</th><th>Actions</th></tr></thead>
	                <tbody id="runtime-reservations-table"></tbody>
	              </table>
	            </section>
	          </div>
	        </div>
	      </div>

	      <div id="tab-providers" class="tab-panel">
	        <div class="providers-workspace">
	          <!-- DESIGN-v3 §7: ONE page per provider family. Local providers are
	               the engine cards (console_ui.py, the same code the setup guide
	               uses) plus each provider's server connection; remote providers
	               are the connection presets; the Available Providers table is
	               the full list, unchanged. The engines placeholder below is
	               replaced by the cards when the tab opens. -->
	          <section id="local-providers-section" class="session-only">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon icon-gear" aria-hidden="true">⚙</span><span>Local providers</span></h2>
	                <p class="section-note">Engines that run models on this computer, and their server connections.</p>
	              </div>
	            </div>
	            <div id="engines-core-root" class="core-console-root"><!--__ABSTRACTCORE_ENGINES_HTML__--></div>
	          </section>

	          <section id="provider-setup-section" class="session-only">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Remote providers</span></h2>
	                <p class="section-note">Cloud accounts and OpenAI-compatible servers. Keys stay on the gateway; only fingerprints are shown.</p>
	              </div>
	            </div>
	            <div id="provider-preset-grid" class="provider-preset-grid"></div>
	          </section>

	          <section class="session-only">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Available Providers</span></h2>
	                <p class="section-note">Configured providers available to this Gateway principal. Use their provider ids in Flow nodes and Core capability defaults.</p>
	                <p id="endpoint-profiles-authority" class="authority-note hidden"></p>
	              </div>
	            </div>
	            <table>
	              <thead><tr><th>Name</th><th>Provider ID</th><th>Type</th><th>Models</th><th>Status</th><th>Actions</th></tr></thead>
	              <tbody id="endpoint-profiles-table"></tbody>
	            </table>
	          </section>
	        </div>
	      </div>

	      <div id="tab-defaults" class="tab-panel">
	        <section id="defaults-section" class="session-only">
	          <div class="section-head">
	            <div>
	              <h2 class="section-title"><span class="section-icon" aria-hidden="true">◆</span><span>Multimodal Capabilities</span></h2>
	              <p id="defaults-scope" class="section-note">Sign in to edit provider/model defaults for this Gateway runtime.</p>
	              <p id="defaults-authority" class="authority-note hidden"></p>
	            </div>
	            <div class="section-actions">
	              <!-- "Apply recommended" lives in the section HEAD, not in the
	                   weights banner below. It used to be appended to that
	                   banner, so the banner had to render on a fully configured
	                   host just to keep the button reachable — and a banner that
	                   always renders ends up always saying something. Here it is
	                   a standing action, and the banner is free to stay silent. -->
	              <button id="defaults-apply-recommended" class="secondary" title="Set the recommended provider/model on the text, voice, transcription, image and video routes this computer can run. Routes you configured differently are kept." aria-label="Apply recommended routes"><span class="button-icon" aria-hidden="true">◆</span><span>Apply recommended</span></button>
	              <button id="refresh-catalog" class="secondary" title="Reload providers and capability defaults" aria-label="Refresh catalog"><span class="button-icon icon-refresh" aria-hidden="true">↻</span><span>Refresh</span></button>
	            </div>
	          </div>
	          <!-- Weights banner: a route with NOTHING routed to it cannot run,
	               and the recommended starter model is the one-click way out of
	               that. It speaks ONLY about those routes: a recommended model
	               that is absent because the operator routed the capability at
	               their own model is not a gap, and saying so in red is a false
	               alarm nobody can clear. See `renderAvailabilityBanner`. -->
	          <div id="defaults-availability" class="message hidden"></div>
		          <table class="capability-table">
		            <thead><tr><th>Route</th><th>Capability</th><th>Provider</th><th>Model</th><th>Weights</th><th>Source</th><th>Status</th><th>Actions</th></tr></thead>
		            <tbody id="defaults-table"></tbody>
		          </table>
	          <div id="defaults-message" class="message"></div>
	        </section>
	      </div>

	      <div id="tab-sandbox" class="tab-panel">
	        <div class="sandbox-workspace">
	          <section class="session-only sandbox-chat">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◌</span><span>Sandbox Chat</span></h2>
	                <p id="sandbox-context" class="section-note">Select a provider/model and run a smoke test.</p>
	              </div>
	            </div>
	            <div id="sandbox-transcript" class="sandbox-transcript pc-chat-thread"><div class="empty" id="sandbox-empty-hint">No messages yet — pick an output mode below and ask anything.</div></div>
	            <div class="sandbox-composer">
	              <label class="hidden">Capability<select id="sandbox-capability"></select></label>
	              <label id="sandbox-provider-label" class="hidden">Provider<select id="sandbox-provider"></select></label>
	              <label id="sandbox-model-label" class="hidden">Model<select id="sandbox-model"></select></label>
	              <div class="sandbox-composer-toolbar">
	                <label id="sandbox-system-label" class="sandbox-system-compact" title="Instructions sent before your message. Leave empty for none.">System prompt<input id="sandbox-system"></label>
	                <label id="sandbox-reasoning-label" class="sandbox-system-compact" title="Reasoning effort for reasoning models. Default sends nothing; the model behaves as before.">Reasoning<select id="sandbox-reasoning">
	                  <option value="">default</option>
	                  <option value="none">none</option>
	                  <option value="minimal">minimal</option>
	                  <option value="low">low</option>
	                  <option value="medium">medium</option>
	                  <option value="high">high</option>
	                  <option value="xhigh">xhigh</option>
	                </select></label>
	                <label id="sandbox-speculation-label" class="sandbox-system-compact" title="Per-request MTP override. Inherit uses the Core default; explicit depths require a compatible loaded backend.">MTP<select id="sandbox-speculation">
	                  <option value="">inherit</option><option value="off">off</option>
	                  <option value="2">depth 2</option><option value="3">depth 3</option><option value="4">depth 4</option><option value="5">depth 5</option>
	                </select></label>
	              </div>
	              <div id="sandbox-dropzone" class="sandbox-dropzone">
	                <button id="sandbox-attach" class="secondary icon-only sandbox-composer-icon" title="Attach files" aria-label="Attach files"><span class="button-icon" aria-hidden="true">＋</span></button>
	                <div class="sandbox-input-area">
	                  <textarea id="sandbox-prompt" placeholder="Ask a question, describe an image/video/music request, or drop files here."></textarea>
	                  <div id="sandbox-attachments" class="sandbox-attachments"></div>
	                </div>
	                <div class="sandbox-composer-side">
	                  <div id="sandbox-output-modes" class="sandbox-mode-grid" role="radiogroup" aria-label="Sandbox output mode"></div>
	                  <div class="sandbox-composer-actions">
	                    <button id="sandbox-clear" class="secondary icon-only sandbox-composer-icon" title="Clear chat" aria-label="Clear chat"><span class="button-icon" aria-hidden="true">×</span></button>
	                    <button id="sandbox-run" class="sandbox-send icon-only" title="Send" aria-label="Send"><span class="button-icon" aria-hidden="true">▶</span></button>
	                  </div>
	                </div>
	              </div>
	              <input id="sandbox-file-input" class="hidden" type="file" multiple>
	              <div id="sandbox-message" class="message"></div>
	            </div>
	          </section>
	        </div>
	      </div>

	      <!-- MODELS (agentic-OS resources view): what is resident on this
	           host right now. ONE snapshot call (GET /host/state) feeds three
	           stacked sections — Memory & GPU, Models, Session caches —
	           all visible at once (a resources dashboard read at a glance;
	           hiding the RAM meter behind a subtab defeats the point). Every
	           section degrades independently and degraded[] names render as
	           pills, never a silent blank. Reads render for every signed-in
	           user; mutations (warm-up, unload/lock, cache clear) are
	           admin-gated at render time. -->
	      <div id="tab-models" class="tab-panel">
	        <div class="tab-grid tab-grid-wide">
	          <div class="tab-stack">
	            <section id="gateway-host-section" class="session-only">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">◎</span><span>Gateway</span></h2>
	                  <p class="section-note">How this gateway is running right now. Pausing stops new workflow steps; the console and connected apps keep answering.</p>
	                </div>
	              </div>
	              <div id="gateway-host-message" class="message"></div>
	              <div class="entity-live-line">
	                <span id="gateway-host-state" class="state-pill">…</span>
	                <button id="gateway-host-pause" type="button" role="switch" class="af-switch af-switch--sm hidden" aria-checked="false" title="On: no new workflow step starts until you switch it off; work already inside a call finishes first"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Workflows paused</span></span></button>
	                <span id="gateway-host-detail" class="muted"></span>
	              </div>
	              <div class="entity-overview">
	                <div class="entity-kv"><span class="entity-kv-key">Version</span><span class="entity-kv-val"><span id="gateway-host-version">…</span> <span id="gateway-host-update-hint" class="muted"></span> <button id="gateway-host-update-check" class="secondary" type="button" title="Check for a newer release: an AbstractFramework installer install compares with the newest AbstractFramework release, any other install with the newest AbstractGateway on PyPI (needs internet)">Check now</button> <button id="gateway-host-update-start" class="secondary hidden" type="button" title="Install it in the background (an installer install runs the AbstractFramework installer); restart to finish">Update</button></span></div>
	                <details id="gateway-host-update-log-box" class="entity-advanced hidden"><summary id="gateway-host-update-log-summary">Update log</summary><pre id="gateway-host-update-log" class="entity-prompt-preview"></pre></details>
	                <!-- STATUS, NOT A SWITCH. The
	                     icon is the gateway's presence on the desktop: while it
	                     runs, it is there. The only reasons it can be absent are
	                     facts about this machine, and this line names them. -->
	                <div class="entity-kv"><span class="entity-kv-key">Desktop icon</span><span class="entity-kv-val"><span id="gateway-host-tray-note" class="muted"></span></span></div>
	                <!-- Start at login: a real switch (admin), confirmed, verified by GET. -->
	                <div id="gateway-host-login-row" class="entity-kv hidden"><span class="entity-kv-key">Start at login</span><span class="entity-kv-val"><span id="gateway-host-login-text" class="muted">…</span> <button id="gateway-host-login-toggle" type="button" role="switch" class="af-switch af-switch--sm kv-switch hidden" aria-checked="false" aria-label="Start at login"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Start at login</span></span></button></span></div>
	              </div>
	              <div class="actions">
	                <button id="gateway-host-restart" class="secondary hidden" type="button">Restart gateway…</button>
	                <button id="gateway-host-quit" class="secondary danger hidden" type="button">Quit gateway…</button>
	              </div>
	            </section>
	            <section id="models-host-section" class="session-only">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">▦</span><span>Memory &amp; GPU</span></h2>
	                  <p class="section-note">Live truth from the host behind this gateway: RAM pressure, accelerator memory, GPU load. Refreshes every 5 seconds while this tab is open; a failed probe is named below, never blanked.</p>
	                </div>
	                <button id="models-refresh" class="secondary icon-only" title="Reload host state now" aria-label="Refresh host state"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	              </div>
	              <div id="models-message" class="message"></div>
	              <div id="models-degraded" class="entity-chip-row hidden"></div>
	              <div id="models-meters" class="meter-stack"></div>
	              <div id="models-breakdown" class="mem-breakdown hidden"></div>
	              <div id="models-host-facts" class="entity-overview"></div>
	            </section>
	            <section id="models-loaded-section" class="session-only">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">▣</span><span id="models-loaded-title">Models</span></h2>
	                  <p class="section-note">Model runtimes on this host — resident in provider memory by default (default ≠ loaded: configured capability defaults sit behind the toggle until they are really in memory). Unload frees the memory (the next request pays the full load again); a locked model refuses unload until unlocked or forced; Estimate asks the host how much context actually fits.</p>
	                </div>
	                <label id="models-show-cached-label" class="entity-checkbox hidden" title="Also show configured / cached rows that are NOT resident in memory — informational only, nothing to unload"><input id="models-show-cached" type="checkbox"> <span id="models-show-cached-text">Show configured / cached</span></label>
	              </div>
	              <div id="models-load-form" class="models-load-form hidden">
	                <select id="models-load-provider" aria-label="Provider to warm up" title="Provider to warm up — the discovered provider catalog (the same source the Multimodal Capabilities tab picks from)"></select>
	                <input id="models-load-provider-custom" class="hidden" type="text" autocomplete="off" aria-label="Provider id (nothing was discovered)" placeholder="type the provider id — none were discovered">
	                <select id="models-load-model" aria-label="Model to warm up" title="Model to warm up — the selected provider's discovered catalog; pick a provider first"></select>
	                <input id="models-load-model-custom" class="hidden" type="text" autocomplete="off" aria-label="Model id (discovery could not reach this provider)" placeholder="type the model id — discovery could not reach this provider">
	                <label class="entity-checkbox" title="Lock the model in memory after loading so nothing can evict it until it is unlocked"><input id="models-load-lock" type="checkbox"> lock in memory</label>
	                <button id="models-load-button" class="secondary" type="button" title="Load (warm up) this model on the host now">Load model</button>
	              </div>
	              <p id="models-load-hint" class="section-note hidden"></p>
	              <div id="models-loaded-message" class="message"></div>
	              <div id="models-ejects" class="mem-breakdown hidden" role="status"></div>
	              <div class="table-scroll">
	                <table>
	                  <thead><tr><th>Modality</th><th>Provider</th><th>Model</th><th>Resident</th><th>Size</th><th>Context</th><th>Flags</th><th>Actions</th></tr></thead>
	                  <tbody id="models-table"></tbody>
	                </table>
	              </div>
	            </section>
	            <section id="models-caches-section" class="session-only">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">⌸</span><span>Session caches</span></h2>
	                  <p class="section-note">Prompt (KV) caches the runtime minted per session. Clearing one only costs re-encoding the prompt on that session's next turn.</p>
	                </div>
	              </div>
	              <div id="models-caches-message" class="message"></div>
	              <div class="table-scroll">
	                <table>
	                  <thead><tr><th>Session</th><th>Model</th><th>Size</th><th>Tokens</th><th>Created</th><th>Actions</th></tr></thead>
	                  <tbody id="models-caches-table"></tbody>
	                </table>
	              </div>
	            </section>
	          </div>
	        </div>
	      </div>
	      <!-- Models (id catalog) and Engines (id engines): AbstractCore's own
	           screens, spliced server-side and mounted on first open with the
	           gateway's api() (CSRF) and apiBase /api/gateway. Not to be
	           confused with Resources (id models) and Runtimes. -->
	      <div id="tab-catalog" class="tab-panel">
	        <!-- One card per model with a sticky filter bar (console_catalog.py);
	             AbstractCore's Models screen below it keeps the
	             "on this computer" list (models outside the catalog, Delete). -->
	        <div id="catalog-cards-root"></div>
	        <section class="mc-installed" aria-labelledby="catalog-installed-title">
	          <div class="ui-section-title"><h3 id="catalog-installed-title">On this computer</h3><span class="ui-sub">Everything the local engines hold, including models that are not in the catalog. Delete a model here.</span></div>
	          <div id="catalog-core-root" class="core-console-root"><!--__ABSTRACTCORE_CATALOG_HTML__--></div>
	        </section>
	      </div>
	      <div id="tab-apps" class="tab-panel">
	        <div id="apps-root" class="core-console-root"></div>
	        <div id="apps-settings-root" class="core-console-root"></div>
	        <div id="backlog-settings-root" class="core-console-root"></div>
	      </div>
	      <div id="tab-network" class="tab-panel">
	        <div id="network-root" class="core-console-root"></div>
	      </div>
	      <div id="tab-users" class="tab-panel">
	        <div id="account" class="session-summary">No active session.</div>
	        <div class="tab-grid tab-grid-wide">
	          <div class="tab-stack">
	            <!-- ACCOUNTS (DESIGN-v2 §2): ONE table for users AND entities (GET /admin/accounts),
	                 rows tinted by kind (kit .af-row--*), an Active switch on every row, per-row
	                 actions that say why when they cannot apply, and the Email / Logs modals. -->
	            <section id="users-section" class="session-only hidden accounts-page">
	              <div class="accounts-head">
	                <div class="accounts-head__actions">
	                  <button id="open-create-user" type="button" title="Create a gateway user and issue their token (shown once)">Create user</button>
	                  <button id="accounts-create-entity" class="secondary" type="button" title="Summon a new entity from a spark template (the name is permanent)">Create entity</button>
	                </div>
	              </div>
	              <div id="issued-token" class="issued hidden"></div>
	              <div id="users-message" class="message" role="status" aria-live="polite"></div>
	              <div class="users-table-wrap">
	                <table class="users-table accounts-table" data-ui-no-stack>
	                  <thead><tr><th>Name</th><th>Email address</th><th>Mailbox</th><th>Runtime</th><th>Active</th><th>Actions</th></tr></thead>
	                  <tbody id="users-table"></tbody>
	                </table>
	              </div>
	              <p class="af-row-legend accounts-legend" aria-label="Row tint by account kind">Tint:
	                <span class="af-row-legend__item"><span class="af-row-legend__swatch af-row-legend__swatch--admin" aria-hidden="true"></span>admin</span>
	                <span class="af-row-legend__item"><span class="af-row-legend__swatch af-row-legend__swatch--user" aria-hidden="true"></span>user</span>
	                <span class="af-row-legend__item"><span class="af-row-legend__swatch af-row-legend__swatch--entity" aria-hidden="true"></span>entity</span>
	              </p>
	              <!-- The admin's ONE email switch (DESIGN §5.2) = capability `email`.
	                   Agent email tools and sign-in by email sit under Advanced.
	                   Switches apply at once: no Save button. -->
	              <section id="email-caps-section" class="users-caps accounts-email" aria-labelledby="email-caps-title">
	                <h3 id="email-caps-title" class="section-subtitle">Email for everyone</h3>
	                <div class="switch-list">
	                  <button type="button" role="switch" id="email-cap-email" class="af-switch af-switch--row" aria-checked="false" aria-describedby="email-cap-email-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Mailboxes for users</span><span class="af-switch__desc" id="email-cap-email-desc">Users may connect their own mailbox for their agents, automations and notifications. You never see anyone&#39;s mail.</span></span></button><span id="email-cap-email-reason" class="af-switch__reason" hidden></span>
	                </div>
	                <details id="email-caps-advanced" class="plain-disclosure">
	                  <summary>Advanced</summary>
	                  <div class="switch-list">
	                    <button type="button" role="switch" id="email-cap-agent-tools" class="af-switch af-switch--row" aria-checked="false" aria-describedby="email-cap-agent-tools-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Agent email tools for users</span><span class="af-switch__desc" id="email-cap-agent-tools-desc">Users may let their agents and workflows use their mailbox. Each user still switches the tools on for themselves.</span></span></button><span id="email-cap-agent-tools-reason" class="af-switch__reason" hidden></span>
	                    <button type="button" role="switch" id="email-cap-recovery" class="af-switch af-switch--row" aria-checked="false" aria-describedby="email-cap-recovery-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Sign-in by email</span><span class="af-switch__desc" id="email-cap-recovery-desc">Shows &#39;Forgot your token?&#39; on the sign-in page. Whoever controls a user&#39;s mailbox can then sign in as that user.</span></span></button><span id="email-cap-recovery-reason" class="af-switch__reason" hidden></span>
	                  </div>
	                </details>
	                <p id="email-caps-message" class="inline-state" role="status" aria-live="polite"></p>
	              </section>
	            </section>
	            <details id="my-workspace-policy-section" class="plain-disclosure workspace-policy-disclosure session-only">
	              <summary><span class="workspace-policy-disclosure__title">Workspace policy</span><span id="my-workspace-policy-summary" class="workspace-policy-disclosure__help">Which folders your agents may read and write.</span></summary>
	              <div class="entity-config-block">
	                <div class="section-head">
	                  <div>
	                    <p class="section-note">How your agents' filesystem access is decided. Whitelist (default): deny everything, allow your listed folders — plus the folder an agent is started from while launch-folder trust is on. Blacklist: allow everything except your refused folders. The gateway-wide deny list always applies on top.</p>
	                  </div>
	                  <button id="my-workspace-policy-refresh" class="secondary icon-only" title="Reload my workspace policy" aria-label="Refresh my workspace policy"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	                </div>
	                <div id="my-workspace-policy-message" class="message"></div>
	                <div class="provider-config-form">
	                  <label>Launch-folder trust <select id="my-workspace-trust"><option value="">inherit gateway default</option><option value="on">on — agents may write where they start</option><option value="off">off — launch folders are not trusted</option></select></label>
	                  <label>Access mode <select id="my-workspace-mode"><option value="">whitelist (default) — deny everything, allow my list</option><option value="whitelist">whitelist — deny everything, allow my list</option><option value="blacklist">blacklist — allow everything, refuse my list</option></select></label>
	                  <label class="field-span-2">Extra allowed folders (added to the gateway-wide roots) <textarea id="my-workspace-allowed" rows="3" spellcheck="false" placeholder="/abs/path/to/project&#10;/abs/path/to/notes"></textarea></label>
	                  <label class="field-span-2">Refused folders (always denied, in every posture) <textarea id="my-workspace-blocked" rows="3" spellcheck="false" placeholder="/abs/path/to/private"></textarea></label>
	                </div>
	                <div class="inline">
	                  <button id="my-workspace-policy-save" type="button">Save my workspace policy</button>
	                  <button id="my-workspace-policy-clear" class="secondary" type="button">Reset to inherited</button>
	                </div>
	                <div id="my-workspace-policy-current" class="section-note"></div>
	              </div>
	            </details>
            <!-- The user's account page (DESIGN §6). Cards, in order: Email address
                 (the one inline Save) → Mailbox (tabs Google / Microsoft / Other, or the
                 connected status) → Notifications (two switches) → Agent email tools →
                 Advanced. Switches apply at once; no other Save button. -->
            <div id="my-email-section" class="session-only account-page hidden">
              <div class="account-page__head">
                <h2 class="section-title"><span class="section-icon" aria-hidden="true">✉</span><span>My email address and mailbox</span></h2>
                <button id="my-email-refresh" class="secondary icon-only" type="button" title="Reload" aria-label="Reload my email address and mailbox"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
              </div>
              <p id="my-email-message" class="inline-state" role="status" aria-live="polite"></p>
              <p id="my-email-notices" class="af-form__help" hidden></p>
              <section class="af-card account-card" aria-labelledby="my-email-registered-title">
                <div class="af-card__header"><h3 id="my-email-registered-title" class="af-card__title">Your email address</h3></div>
                <div class="af-form">
                  <div class="af-form__field">
                    <label class="af-form__label sr-only" for="my-email-registered">Your email address</label>
                    <!-- DESIGN-v2 §11: ONE editable address field on screen at a time. -->
                    <p id="my-email-registered-view" class="address-view"><span id="my-email-registered-text" class="muted">Not set yet — connecting a mailbox below sets it.</span> <button id="my-email-registered-change" class="link-button" type="button">Set it now</button></p>
                    <div id="my-email-registered-edit" class="af-form__inline" hidden>
                      <input id="my-email-registered" type="email" autocomplete="email" spellcheck="false" aria-describedby="my-email-registered-help my-email-registered-error">
                      <button id="my-email-registered-save" class="secondary" type="button">Save</button>
                    </div>
                    <p id="my-email-registered-error" class="af-form__error" role="alert" hidden></p>
                    <p id="my-email-registered-help" class="af-form__help">Where sign-in codes and notifications go.</p>
                    <p id="my-email-registered-differs" class="af-form__help mailbox-differs" hidden></p>
                  </div>
                </div>
              </section>
              <section class="af-card account-card" aria-labelledby="my-email-mailbox-title">
                <div class="af-card__header">
                  <h3 id="my-email-mailbox-title" class="af-card__title">Mailbox</h3>
                  <p class="af-card__desc">Lets your agents and automations read and send mail as you. Your admin never sees your mail.</p>
                </div>
                <p id="my-email-unavailable" class="af-form__error" hidden></p>
                <div id="my-email-connected" hidden>
                  <p id="my-email-status" class="mailbox-status"></p>
                  <p id="my-email-status-error" class="af-form__error" hidden></p>
                  <div class="switch-list mailbox-active">
                    <button type="button" role="switch" id="my-email-enabled" class="af-switch af-switch--row" aria-checked="false" aria-describedby="my-email-enabled-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Active</span><span class="af-switch__desc" id="my-email-enabled-desc">Off pauses watching, sending and notifications; your settings are kept.</span></span></button><span id="my-email-enabled-reason" class="af-switch__reason" hidden></span>
                  </div>
                  <div class="af-form__actions mailbox-actions">
                    <button id="my-email-test" class="secondary" type="button">Test</button>
                    <button id="my-email-disconnect" class="danger" type="button">Disconnect</button>
                  </div>
                  <div id="my-email-disconnect-confirm" class="inline-confirm" role="group" hidden>
                    <span>Disconnect this mailbox? Your agents lose email until you connect again. Policy and limits are kept.</span>
                    <button id="my-email-disconnect-now" class="danger" type="button">Disconnect</button>
                    <button id="my-email-disconnect-cancel" class="secondary" type="button">Cancel</button>
                  </div>
                </div>
                <div id="my-email-connect" class="af-tabs">
                  <div class="af-tabs__list" role="tablist" aria-label="How your mailbox signs in">
                    <button id="my-email-tab-imap" class="af-tabs__tab" role="tab" type="button" data-email-tab="imap" aria-controls="my-email-pane-imap" aria-selected="true">IMAP</button>
                    <button id="my-email-tab-google" class="af-tabs__tab" role="tab" type="button" data-email-tab="google" aria-controls="my-email-pane-oauth" aria-selected="false" tabindex="-1">Google</button>
                    <button id="my-email-tab-microsoft" class="af-tabs__tab" role="tab" type="button" data-email-tab="microsoft" aria-controls="my-email-pane-oauth" aria-selected="false" tabindex="-1">Microsoft</button>
                  </div>
                  <div id="my-email-pane-imap" class="af-tabs__panel" role="tabpanel" aria-labelledby="my-email-tab-imap">
                    <div class="af-form">
                      <p class="address-line mailbox-address-line" hidden><span class="mailbox-address-line__text"></span> <button class="link-button mailbox-address-other" type="button">Use a different account</button></p>
                      <div class="af-form__field mailbox-address-field">
                        <label class="af-form__label" for="my-email-address">Mailbox address</label>
                        <input id="my-email-address" type="email" autocomplete="email" spellcheck="false" aria-describedby="my-email-address-help">
                        <p id="my-email-address-help" class="af-form__help">The account your agents read and send from — usually your own address.</p>
                      </div>
                      <div class="af-form__field">
                        <label class="af-form__label" for="my-email-password">Password</label>
                        <input id="my-email-password" type="password" autocomplete="new-password" aria-describedby="my-email-password-help">
                        <p id="my-email-password-help" class="af-form__help">Use an app password if your provider needs one.</p>
                      </div>
                      <fieldset class="mail-server-row">
                        <legend class="af-form__label">Incoming mail (IMAP)</legend>
                        <div class="mail-server-row__fields">
                          <div class="af-form__field mail-server-row__host"><label class="af-form__label mail-server-row__caption" for="my-email-imap-host">Server</label><input id="my-email-imap-host" autocomplete="off" spellcheck="false"></div>
                          <div class="af-form__field mail-server-row__port"><label class="af-form__label mail-server-row__caption" for="my-email-imap-port">Port</label><input id="my-email-imap-port" inputmode="numeric"></div>
                          <div class="af-form__field mail-server-row__sec"><label class="af-form__label mail-server-row__caption" for="my-email-imap-security">Security</label><select id="my-email-imap-security"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option></select></div>
                        </div>
                      </fieldset>
                      <fieldset class="mail-server-row">
                        <legend class="af-form__label">Outgoing mail (SMTP)</legend>
                        <div class="mail-server-row__fields">
                          <div class="af-form__field mail-server-row__host"><label class="af-form__label mail-server-row__caption" for="my-email-smtp-host">Server</label><input id="my-email-smtp-host" autocomplete="off" spellcheck="false"></div>
                          <div class="af-form__field mail-server-row__port"><label class="af-form__label mail-server-row__caption" for="my-email-smtp-port">Port</label><input id="my-email-smtp-port" inputmode="numeric"></div>
                          <div class="af-form__field mail-server-row__sec"><label class="af-form__label mail-server-row__caption" for="my-email-smtp-security">Security</label><select id="my-email-smtp-security"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option></select></div>
                        </div>
                      </fieldset>
                      <p id="my-email-servers-source" class="af-form__help servers-source" role="status" aria-live="polite" hidden></p>
                      <p class="login-toggle"><button id="my-email-login-toggle" class="link-button" type="button" aria-expanded="false" aria-controls="my-email-login-field">My provider uses a different login name</button></p>
                      <div id="my-email-login-field" class="af-form__field" hidden>
                        <label class="af-form__label" for="my-email-username">Login</label>
                        <input id="my-email-username" autocomplete="off" spellcheck="false" aria-describedby="my-email-username-help">
                        <p id="my-email-username-help" class="af-form__help">The name your provider asks for at sign-in, when it is not the mailbox address.</p>
                      </div>
                      <p id="my-email-connect-error" class="af-form__error" role="alert" hidden></p>
                      <div class="af-form__actions">
                        <button id="my-email-connect-go" type="button">Connect</button>
                      </div>
                    </div>
                  </div>
                  <div id="my-email-pane-oauth" class="af-tabs__panel" role="tabpanel" aria-labelledby="my-email-tab-google" hidden>
                    <div class="af-form">
                      <p class="address-line mailbox-address-line" hidden><span class="mailbox-address-line__text"></span> <button class="link-button mailbox-address-other" type="button">Use a different account</button></p>
                      <div class="af-form__field mailbox-address-field">
                        <label class="af-form__label" for="my-email-oauth-address">Mailbox address</label>
                        <input id="my-email-oauth-address" type="email" autocomplete="email" spellcheck="false" aria-describedby="my-email-oauth-address-help">
                        <p id="my-email-oauth-address-help" class="af-form__help">The account you sign in with; it pre-fills the sign-in page.</p>
                      </div>
                      <div class="af-form__actions">
                        <button id="my-email-oauth-cancel" class="secondary" type="button" hidden>Cancel sign-in</button>
                        <button id="my-email-oauth-start" type="button" aria-describedby="my-email-oauth-start-reason">Sign in with Google</button>
                      </div>
                      <p id="my-email-oauth-start-reason" class="af-form__help" hidden></p>
                      <p id="my-email-oauth-prompt" class="inline-state" role="status" aria-live="polite"></p>
                      <details id="my-email-oauth-advanced" class="plain-disclosure">
                        <summary>Advanced</summary>
                        <div class="af-form">
                          <p class="af-form__help">Your own sign-in client instead of the gateway&#39;s or the built-in one.</p>
                          <div class="af-form__grid-2">
                            <div class="af-form__field"><label class="af-form__label" for="my-email-oauth-client-id">Client ID</label><input id="my-email-oauth-client-id" autocomplete="off" spellcheck="false"></div>
                            <div class="af-form__field"><label class="af-form__label" for="my-email-oauth-client-secret">Client secret</label><input id="my-email-oauth-client-secret" type="password" autocomplete="new-password"></div>
                          </div>
                          <div class="af-form__grid-2 email-ms-only">
                            <div class="af-form__field"><label class="af-form__label" for="my-email-oauth-tenant">Tenant</label><input id="my-email-oauth-tenant" autocomplete="off" spellcheck="false"><p class="af-form__help">Empty = any Microsoft account (common).</p></div>
                            <div class="af-form__field"><label class="af-form__label" for="my-email-oauth-flow">Sign-in flow</label><select id="my-email-oauth-flow"><option value="">Code in any browser (default)</option><option value="loopback">Browser on the gateway&#39;s computer</option></select></div>
                          </div>
                        </div>
                      </details>
                    </div>
                  </div>
                </div>
              </section>
              <section class="af-card account-card" aria-labelledby="my-email-notify-title">
                <div class="af-card__header"><h3 id="my-email-notify-title" class="af-card__title">Notifications</h3></div>
                <div class="switch-list">
                  <button type="button" role="switch" id="my-email-notify-job-failed" class="af-switch af-switch--row" aria-checked="false" aria-describedby="my-email-notify-job-failed-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Job failed</span><span class="af-switch__desc" id="my-email-notify-job-failed-desc">An automation of yours, or a run you asked to be emailed about, failed after its retries.</span></span></button><span id="my-email-notify-job-failed-reason" class="af-switch__reason" hidden></span>
                  <button type="button" role="switch" id="my-email-notify-approval" class="af-switch af-switch--row" aria-checked="false" aria-describedby="my-email-notify-approval-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Approval needed</span><span class="af-switch__desc" id="my-email-notify-approval-desc">A run is waiting for your answer.</span></span></button><span id="my-email-notify-approval-reason" class="af-switch__reason" hidden></span>
                </div>
                <p class="notify-test-row"><button id="my-email-notify-test" class="secondary small" type="button">Send a test</button> <span id="my-email-notify-test-state" class="inline-state" role="status" aria-live="polite"></span></p>
              </section>
              <section class="af-card account-card" aria-labelledby="my-email-tools-title">
                <div class="af-card__header"><h3 id="my-email-tools-title" class="af-card__title">Agent email tools</h3></div>
                <div class="switch-list">
                  <button type="button" role="switch" id="my-email-agent-tools" class="af-switch af-switch--row" aria-checked="false" aria-describedby="my-email-agent-tools-desc"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Agent email tools</span><span class="af-switch__desc" id="my-email-agent-tools-desc">Your agents and workflows may list, search, read, send and reply to your mail. Every send still follows your recipient rules, your limits and the approval gate.</span></span></button><span id="my-email-agent-tools-reason" class="af-switch__reason" hidden></span>
                </div>
              </section>
              <details id="my-email-advanced" class="af-card account-card account-advanced">
                <summary>Advanced</summary>
                <div class="af-form advanced-sentences">
                  <div class="advanced-sentence">
                    <span id="my-email-policy-title">Your agents may send to</span>
                    <select id="my-email-policy-mode" aria-labelledby="my-email-policy-title"><option value="allowlist">Only these recipients</option><option value="denylist">Everyone except these</option></select>
                    <span id="my-email-policy-state" class="inline-state" role="status" aria-live="polite"></span>
                  </div>
                  <ul id="my-email-policy-list" class="chip-list" aria-label="Recipients"></ul>
                  <div class="af-form__inline advanced-add">
                    <input id="my-email-policy-add" autocomplete="off" spellcheck="false" placeholder="name@example.com or example.com" aria-label="Address or domain to add" aria-describedby="my-email-policy-help">
                    <button id="my-email-policy-add-button" class="secondary" type="button">Add</button>
                  </div>
                  <p id="my-email-policy-help" class="af-form__help">Applies to To, Cc and Bcc; a message with any refused recipient is not sent.</p>
                  <div class="advanced-sentence">
                    <span>At most</span>
                    <input id="my-email-per-hour" class="advanced-num" type="number" min="0" inputmode="numeric" aria-label="Most emails per hour">
                    <span>per hour and</span>
                    <input id="my-email-per-day" class="advanced-num" type="number" min="0" inputmode="numeric" aria-label="Most emails per day">
                    <span>per day.</span>
                    <span id="my-email-limits-state" class="inline-state" role="status" aria-live="polite"></span>
                  </div>
                  <p id="my-email-usage" class="af-form__help"></p>
                  <div class="advanced-sentence">
                    <label for="my-email-imap-folder">Watch folder</label>
                    <input id="my-email-imap-folder" class="advanced-folder" autocomplete="off" spellcheck="false" aria-describedby="my-email-folder-help">
                    <span id="my-email-folder-state" class="inline-state" role="status" aria-live="polite"></span>
                  </div>
                  <p id="my-email-folder-help" class="af-form__help">The folder automations watch for new mail.</p>
                </div>
              </details>
            </div>
	            <section id="entities-list-section" class="session-only">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon" aria-hidden="true">☾</span><span>Summoned entities</span></h2>
	                  <p class="section-note">Persistent identities living on this gateway, each with its own memory and runtime. Talk to one, or manage its lifecycle, substrate, capabilities, and prompt.</p>
	                </div>
	                <div class="section-actions">
	                  <button id="open-create-entity" title="Summon a new entity from a spark template (the name is permanent — validated before anything is written)" aria-label="Summon entity"><span class="button-icon" aria-hidden="true">☾</span><span>Summon entity</span></button>
	                  <button id="open-templates" class="secondary" title="View, edit, and version the spark templates entities are born from" aria-label="Manage templates"><span class="button-icon" aria-hidden="true">✎</span><span>Templates</span></button>
	                  <button id="entities-refresh" class="secondary icon-only" title="Reload the entity roster" aria-label="Refresh entities"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	                </div>
	              </div>
	              <table>
	                <thead><tr><th>Name</th><th>Entity ID</th><th>Born</th><th>State</th><th>Actions</th></tr></thead>
	                <tbody id="entities-table"></tbody>
	              </table>
	              <div id="entities-message" class="message"></div>
	            </section>
	            <section id="entity-manage-section" class="session-only hidden">
	              <div class="section-head">
	                <div>
	                  <h2 class="section-title"><span class="section-icon icon-gear" aria-hidden="true">⚙</span><span>Manage <span id="entity-manage-name">entity</span></span></h2>
	                  <p class="section-note" id="entity-manage-sub">Lifecycle, substrate, capabilities, and prompt for this summoned entity.</p>
	                </div>
                <button id="entity-manage-close" class="secondary" title="Back to the entities list"><span class="button-icon" aria-hidden="true">←</span><span>Entities</span></button>
              </div>
              <p id="entity-admin-note" class="section-note hidden">You are viewing as a non-admin. Configuration (state, substrate, capabilities, prompt, re-embed) requires an admin session — those controls are hidden.</p>
	              <nav class="entity-subtabs">
	                <button id="entity-subtab-overview" class="entity-subtab active" type="button">Overview</button>
	                <button id="entity-subtab-talk" class="entity-subtab" type="button">Talk</button>
	                <button id="entity-subtab-lifecycle" class="entity-subtab" type="button">Lifecycle</button>
	                <button id="entity-subtab-substrate" class="entity-subtab" type="button">Substrate</button>
	                <button id="entity-subtab-tools" class="entity-subtab" type="button">Capabilities</button>
	                <button id="entity-subtab-prompt" class="entity-subtab" type="button">Prompt</button>
	              </nav>
	              <div id="entity-subpanel-overview" class="entity-subpanel">
	                <div id="entity-cognition-line" class="section-note"></div>
	                <div id="entity-drives" class="entity-drives hidden" title="Drive ratios from the entity's own memory: open questions and interests are the pull forward — never-100% is the design, not a defect."></div>
	                <details id="entity-candidates-box" class="entity-advanced hidden"><summary>Sleep candidates awaiting review (<span id="entity-candidates-count">0</span>)</summary>
	                  <div class="section-note">Sleep proposes; waking evidence disposes. Promote needs two independent corroborating records; reject needs a reason (the honest no — judgment, not erasure).</div>
	                  <div id="entity-candidates-list"></div>
	                </details>
	                <div id="entity-overview" class="entity-overview"></div>
	                <button id="entity-verify" class="secondary" title="Verify the memory hash chain and spark attestation — pure read, never deposits usage"><span class="button-icon" aria-hidden="true">✓</span><span>Verify chain</span></button>
	                <div id="entity-verify-out" class="section-note"></div>
	              </div>
	              <div id="entity-subpanel-talk" class="entity-subpanel hidden">
	                <h3 class="entity-config-title">Visit <span class="entity-config-hint">a hosted chat session with this entity (the same driver the CLI runs). Opening ENDS the personal phase (mutually exclusive); closing runs the reflection pass and the standing grant re-enters personal.</span></h3>
	                <div class="entity-btn-row">
	                  <button id="entity-chat-open" class="secondary" title="Open a hosted visit: prelude + memory recall run, and the conversation forms memories (billable)"><span class="button-icon" aria-hidden="true">☾</span><span>Open visit</span></button>
	                  <button id="entity-chat-close" class="secondary hidden" title="Close the visit gracefully — the entity's reflection pass runs"><span class="button-icon" aria-hidden="true">×</span><span>Close visit (reflect)</span></button>
	                </div>
	                <div id="entity-chat-status" class="section-note"></div>
	                <div id="entity-chat-transcript" class="entity-chat-transcript"></div>
	                <div class="inline entity-chat-composer">
	                  <label>Message<textarea id="entity-chat-input" rows="2" placeholder="say something…"></textarea></label>
	                  <button id="entity-chat-send" class="secondary" disabled title="Send (Enter)"><span class="button-icon" aria-hidden="true">➤</span><span>Send</span></button>
	                </div>
	              </div>
	              <div id="entity-subpanel-lifecycle" class="entity-subpanel hidden">
	                <!-- LIVENESS AXIS: the STOPPED banner outranks every chip;
	                     Stop is a distinct emergency affordance, never a radio position.
	                     GROUPING: each hazard domain is its
	                     own bordered card — five domains flowing as one flat column
	                     did not read. -->
	                <div id="entity-stop-banner" class="entity-stop-banner hidden">
	                  <span id="entity-stop-banner-text">STOPPED — the kill switch is engaged: every door refuses and every process gate blocks.</span>
	                  <button id="entity-restore" class="secondary" title="Release the kill switch — the existing wake verb; lands awake unconditionally">Restore</button>
	                </div>
	                <div id="entity-live-line" class="entity-live-line"></div>
	                <div class="entity-config-group">
	                  <h3 class="entity-config-title">State <span class="entity-config-hint">awake serves visits/work; asleep is the consolidation window (memory processes run — the entity stays alive and reachable; visits auto-wake).</span></h3>
	                  <div id="entity-state-current" class="section-note"></div>
	                  <div class="entity-btn-row" role="radiogroup" aria-label="entity state">
	                    <button id="entity-state-awake" class="secondary entity-state-btn" role="radio" aria-checked="false" title="Awake: serves visits and work">Wake</button>
	                    <button id="entity-state-asleep" class="secondary entity-state-btn" role="radio" aria-checked="false" title="Sleep: consolidation window — memory processes run; still alive and reachable (visits auto-wake)">Sleep</button>
	                    <label class="entity-checkbox" title="With Sleep: run the dream pass inside the consolidation window"><input id="entity-state-dream" type="checkbox"> dream pass (with sleep)</label>
	                  </div>
	                  <label>Reason<input id="entity-state-reason" placeholder="optional — carried into the wake cue + host marker"></label>
	                  <div id="entity-state-out" class="section-note"></div>
	                </div>
	                <div class="entity-config-group entity-config-group-danger">
	                  <h3 class="entity-config-title">Stop <span class="entity-config-hint">the liveness axis — a kill switch above the state machine, not a state. In-flight work halts without reflection; every door refuses until Restore.</span></h3>
	                  <div class="entity-btn-row">
	                    <button id="entity-stop" class="danger" title="STOP — the kill switch: in-flight work halts without reflection; every door refuses until Restore. For graceful rest, use Sleep.">Stop (kill switch)</button>
	                  </div>
	                </div>
	                <div class="entity-config-group">
	                  <h3 class="entity-config-title">Personal time <span class="entity-config-hint">the entity's own time — free exploration on its own tick. Starting arms the standing grant AND starts the loop; stopping revokes both. Spends real tokens unattended.</span></h3>
	                  <div class="entity-btn-row">
	                    <button id="entity-owntime-toggle" class="owntime-btn" aria-pressed="false" title="Personal time: arm the standing grant and start the autonomous loop; press again to stop and revoke. Spends real tokens while running.">PERSONAL — checking…</button>
	                    <button id="entity-loop-freeze" class="danger" title="Emergency: kill the personal-time loop process NOW and stop the entity — for hard failures and imminent threats only">Freeze (emergency)</button>
	                  </div>
	                  <div id="entity-loop-status" class="section-note"></div>
	                  <details class="entity-advanced">
	                    <summary>Schedule (defaults: tick 20s · 8 ticks/day · rest 30min · grant until revoked)</summary>
	                    <div class="inline">
	                      <label>Grant duration hours<input id="entity-grant-hours" type="number" min="0" max="720" placeholder="blank = until revoked" title="How long the standing personal grant stays armed"></label>
	                      <label>Tick seconds<input id="entity-loop-tick" type="number" min="1" max="3600" placeholder="20" title="Seconds between autonomous ticks"></label>
	                      <label>Ticks / day window<input id="entity-loop-ticks" type="number" min="1" max="500" placeholder="8" title="Ticks per day window before the rest period"></label>
	                      <label>Rest minutes<input id="entity-loop-rest" type="number" min="0" max="1440" placeholder="30" title="Rest between day windows"></label>
	                    </div>
	                  </details>
	                  <div id="entity-loop-out" class="section-note"></div>
	                </div>
	              </div>
	              <div id="entity-subpanel-substrate" class="entity-subpanel hidden">
	                <h3 class="entity-config-title">Substrate <span class="entity-config-hint">the entity's mind — provider + model. Blank source = inherits the gateway default.</span></h3>
	                <div id="entity-substrate-current" class="section-note"></div>
	                <div class="inline">
	                  <label>Provider<input id="entity-substrate-provider" placeholder="abstractcore provider"></label>
	                  <label>Model<input id="entity-substrate-model" placeholder="model id"></label>
	                  <label title="Reasoning effort for reasoning models. None disables; leave on 'not set' to send nothing.">Reasoning<select id="entity-substrate-thinking">
	                    <option value="">not set</option>
	                    <option value="none">none</option>
	                    <option value="minimal">minimal</option>
	                    <option value="low">low</option>
	                    <option value="medium">medium</option>
	                    <option value="high">high</option>
	                    <option value="xhigh">xhigh</option>
	                  </select></label>
	                </div>
	                <button id="entity-substrate-save" class="secondary" title="Persist this provider/model choice (and optional reasoning effort) as the entity's mind substrate (host-marked)"><span class="button-icon" aria-hidden="true">✓</span><span>Save substrate</span></button>
	                <div id="entity-substrate-out" class="section-note"></div>
	                <!-- Card 015 wave 3 (disclosure P0-3, second half): the re-embed
	                     repair belongs beside the MIND it repairs (embedding = the
	                     semantic space), behind a danger-zone disclosure — not in
	                     Lifecycle where it read as a routine state verb. Element ids
	                     unchanged (the JS wiring is id-based). -->
	                <details class="entity-advanced entity-config-group-danger">
	                  <summary class="entity-danger-title">Danger zone — Re-embed (CRITICAL repair)</summary>
	                  <div class="entity-config-block">
	                    <h3 class="entity-config-title entity-danger-title">Re-embed (repair) <span class="entity-config-hint">CRITICAL repair verb: re-derives every vector with the gateway's resolved embedder (atomic swap). The model field is a verification — type the resolved embedder shown below.</span></h3>
	                    <div id="entity-embedding-status" class="section-note"></div>
	                    <div class="inline">
	                      <label>Embedding model (verification)<input id="entity-reembed-model" placeholder="must match the resolved embedder"></label>
	                      <label>Reason<input id="entity-reembed-reason" placeholder="why — journaled + host-marked"></label>
	                    </div>
	                    <button id="entity-reembed" class="danger" title="CRITICAL repair: re-derive every memory vector with the gateway's resolved embedder (atomic swap; journaled + host-marked)"><span class="button-icon icon-retry" aria-hidden="true">⟳</span><span>Re-embed</span></button>
	                    <div id="entity-reembed-out" class="section-note"></div>
	                  </div>
	                </details>
	                <div class="entity-config-group">
	                  <h3 class="entity-config-title">Voice <span class="entity-config-hint">the entity's spoken voice — a full provider/model/voice triple stored in the home (voice.yaml, marker-first). Unset = the gateway's default voice chain.</span></h3>
	                  <div id="entity-voice-current" class="section-note"></div>
	                  <div class="inline">
	                    <label>Provider<select id="entity-voice-provider"><option value="">Choose a provider…</option></select></label>
	                    <label>Model<select id="entity-voice-model" disabled><option value="">Select provider first</option></select></label>
	                    <label>Voice<select id="entity-voice-voice" disabled><option value="">Select model first</option></select></label>
	                  </div>
	                  <div class="entity-btn-row">
	                    <button id="entity-voice-audition" class="secondary" title="Hear the CURRENT SELECTION before saving — synthesizes a sample through the entity's own TTS lane"><span class="button-icon" aria-hidden="true">▶</span><span>Audition</span></button>
	                    <button id="entity-voice-save" class="secondary" title="Persist this voice triple as the entity's own voice (host-marked voice_changed)"><span class="button-icon" aria-hidden="true">✓</span><span>Save voice</span></button>
	                    <button id="entity-voice-clear" class="secondary" title="Remove the entity's own voice — falls back down the gateway default chain (host-marked)"><span class="button-icon" aria-hidden="true">×</span><span>Clear</span></button>
	                  </div>
	                  <div id="entity-voice-out" class="section-note"></div>
	                </div>
	              </div>
	              <div id="entity-subpanel-tools" class="entity-subpanel hidden">
	                <div class="entity-config-group">
	                  <h3 class="entity-config-title">Work order <span class="entity-config-hint">a mission the entity works instead of its own time. Its PRESENCE shifts the next day-open to phase=work — the WORK column grants below apply (execute_command included where you grant it). The entity declares done/blocked; you never write for it, and clearing archives visibly.</span></h3>
	                  <div id="entity-workorder-current" class="section-note"></div>
	                  <label>Order<textarea id="entity-workorder-text" rows="4" placeholder="the task, in plain words — it rides the entity's system prompt all work day"></textarea></label>
	                  <div class="entity-btn-row">
	                    <button id="entity-workorder-save" class="secondary" title="Set this as the standing work order (marker-first; the loop enters phase=work at its next day-open)"><span class="button-icon" aria-hidden="true">✓</span><span>Set work order</span></button>
	                    <button id="entity-workorder-clear" class="secondary" title="Archive + remove the standing order — personal time returns next day-open"><span class="button-icon" aria-hidden="true">×</span><span>Clear (personal returns)</span></button>
	                  </div>
	                  <div id="entity-workorder-out" class="section-note"></div>
	                  <details class="entity-advanced"><summary>Completed orders (the entity's verdicts, never deleted)</summary><pre id="entity-workorder-history" class="entity-prompt-preview"></pre></details>
	                </div>
	                <h3 class="entity-config-title">Per-phase capabilities <span class="entity-config-hint">the operator's word per phase. Only changed phases are written; a phase cleared of every tool resets to the framework default (or denies all, below). The WORK column applies on a work-order day; execute_command lets the entity run commands then.</span></h3>
	                <div id="entity-manage-matrix" class="entity-matrix"></div>
	                <label id="entity-tools-denyall-row" class="entity-checkbox"><input id="entity-tools-denyall" type="checkbox"> treat fully-cleared phases as DENY-ALL (explicit empty grant) instead of reset-to-default</label>
	                <div class="entity-btn-row">
	                  <button id="entity-tools-save" class="secondary" title="Persist the per-phase tool grants (only changed phases are written)"><span class="button-icon" aria-hidden="true">✓</span><span>Save capabilities</span></button>
	                </div>
	                <div id="entity-tools-out" class="section-note"></div>
	              </div>
	              <div id="entity-subpanel-prompt" class="entity-subpanel hidden">
	                <h3 class="entity-config-title">Prompt overlay <span class="entity-config-hint">editable layers on the system prompt. Blank = the built-in default for that layer. Identity is never editable here.</span></h3>
	                <div id="entity-prompt-layers" class="entity-prompt-layers"></div>
	                <button id="entity-prompt-save" class="secondary" title="Persist the operator prompt layer (defaults stay live underneath)"><span class="button-icon" aria-hidden="true">✓</span><span>Save prompt</span></button>
	                <div id="entity-prompt-out" class="section-note"></div>
	                <details class="entity-advanced"><summary>Preview: the composed system prompt (as the next visit would see it)</summary><pre id="entity-prompt-preview" class="entity-prompt-preview"></pre></details>
	              </div>
	            </section>
	          </div>
	        </div>
	      </div>
	    </div>
	  </main>
	  </div><!-- /shell_main -->
	  </div><!-- /shell -->
	  <div id="default-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal default-modal" role="dialog" aria-modal="true" aria-labelledby="default-modal-title">
	      <div class="modal-header">
	        <h2 id="default-modal-title">Configure capability default</h2>
	        <p id="default-modal-description">Select a provider and one of its discovered models.</p>
	        <div id="default-modal-route" class="default-modal-route"></div>
	      </div>
	      <div class="modal-body">
	        <div class="default-config-form">
	          <label>Provider<select id="modal-default-provider"></select>
	            <input id="modal-default-provider-custom" class="hidden" type="text" autocomplete="off"
	                   placeholder="type the provider id — none were discovered"></label>
	          <label>Model<select id="modal-default-model"></select>
	            <input id="modal-default-model-custom" class="hidden" type="text" autocomplete="off"
	                   placeholder="type the model id — discovery could not reach this provider"></label>
	          <label id="modal-default-voice-label" class="hidden">Voice<select id="modal-default-voice"></select></label>
	          <label id="modal-default-reasoning-label" class="hidden" title="Default reasoning effort for reasoning-capable models on this route. 'Not set' sends no reasoning parameter. A request that names its own effort wins.">Reasoning<select id="modal-default-reasoning">
	            <option value="">not set</option>
	            <option value="minimal">minimal</option>
	            <option value="low">low</option>
	            <option value="medium">medium</option>
	            <option value="high">high</option>
	          </select></label>
	          <label title="Point this ONE route at a specific server — a local inference server on a non-default port, say. Blank inherits the provider's own base URL.">Base URL <span class="subtle">(optional)</span>
	            <input id="modal-default-base-url" type="text" autocomplete="off" spellcheck="false"
	                   placeholder="inherit from the provider — e.g. http://localhost:1234/v1"></label>
	          <label id="modal-default-speculation-label" class="hidden" title="Core-owned default policy. Explicit request overrides win; unsupported backends do not enable MTP merely because a default is saved.">MTP<select id="modal-default-speculation">
	            <option value="">inherit (no override)</option><option value="off">off</option>
	            <option value="2">depth 2</option><option value="3">depth 3</option><option value="4">depth 4</option><option value="5">depth 5</option>
	          </select><span id="modal-default-speculation-status" class="subtle"></span></label>
	          <label title="Other provider options for this route, as a JSON object. Voice and MTP have dedicated controls; their owned keys are kept separately and preserved.">Options <span class="subtle">(JSON, optional)</span>
	            <textarea id="modal-default-options" rows="3" autocomplete="off" spellcheck="false"
	                      placeholder='{"temperature": 0.7}'></textarea></label>
	          <div id="default-modal-message" class="message"></div>
	          <div id="default-modal-test" class="default-modal-test"></div>
	        </div>
	      </div>
	      <div class="modal-actions">
	        <button id="close-default-modal" class="secondary">Cancel</button>
	        <button id="clear-default" class="secondary" title="Remove this override — the route falls back to what it inherits"><span class="button-icon" aria-hidden="true">×</span><span>Clear</span></button>
	        <button id="test-default" class="secondary hidden" title="Test this selection with a real generation through the production lane — for voice routes, hear the selected voice before saving"><span class="button-icon" aria-hidden="true">▶</span><span>Test</span></button>
	        <button id="save-default" title="Persist this provider/model as the capability default"><span class="button-icon" aria-hidden="true">✓</span><span>Save</span></button>
	      </div>
	    </div>
	  </div>
	  <div id="log-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal log-modal" role="dialog" aria-modal="true" aria-labelledby="log-modal-title">
	      <div class="modal-header">
	        <h2 id="log-modal-title">Log</h2>
	        <p id="log-modal-sub" class="muted"></p>
	      </div>
	      <div class="modal-body">
	        <div class="inline" style="margin-bottom: 8px; align-items: center;">
	          <label>Show<select id="log-modal-tail-size"><option value="65536">last 64 KB</option><option value="262144">last 256 KB</option><option value="1048576">last 1 MB</option></select></label>
	          <button id="log-modal-refresh" class="secondary icon-only" title="Re-read the tail" aria-label="Refresh log tail"><span class="button-icon icon-refresh" aria-hidden="true">↻</span></button>
	          <span id="log-modal-status" class="muted" style="font-size: 12px;"></span>
	        </div>
	        <pre id="log-modal-pre"></pre>
	      </div>
	      <div class="modal-actions">
	        <button id="log-modal-close" class="secondary" type="button">Close</button>
	      </div>
	    </div>
	  </div>
	  <div id="artifact-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal log-modal" role="dialog" aria-modal="true" aria-labelledby="artifact-modal-title">
	      <div class="modal-header">
	        <h2 id="artifact-modal-title">Artifact</h2>
	        <p id="artifact-modal-sub" class="muted"></p>
	      </div>
	      <div class="modal-body">
	        <div id="artifact-modal-content"></div>
	        <div id="artifact-modal-meta" class="section-note" style="margin-top: 10px;"></div>
	      </div>
	      <div class="modal-actions">
	        <a id="artifact-modal-raw" class="hidden" target="_blank" rel="noopener"><button class="secondary" type="button">Open raw</button></a>
	        <button id="artifact-modal-close" class="secondary" type="button">Close</button>
	      </div>
	    </div>
	  </div>
	  <div id="run-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal default-modal" role="dialog" aria-modal="true" aria-labelledby="run-modal-title">
	      <div class="modal-header">
	        <h2 id="run-modal-title">Run</h2>
	        <p id="run-modal-sub" class="muted"></p>
	      </div>
	      <div class="modal-body">
	        <div id="run-modal-kv" class="entity-overview"></div>
	        <details style="margin-top: 10px;"><summary>Raw JSON</summary>
	          <pre id="run-modal-raw" style="max-height: 40vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px;"></pre>
	        </details>
	      </div>
	      <div class="modal-actions">
	        <button id="run-modal-close" class="secondary" type="button">Close</button>
	      </div>
	    </div>
	  </div>
	  <div id="workspace-policy-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal wsp-modal" role="dialog" aria-modal="true" aria-labelledby="workspace-policy-modal-title">
	      <div class="modal-header">
	        <h2 id="workspace-policy-modal-title">Workspace policy</h2>
	        <p id="workspace-policy-modal-user"></p>
	      </div>
	      <div class="modal-body">
	        <label class="wsp-field">Launch-folder trust
	          <span class="wsp-hint">Whether an agent may write in the folder it was started from.</span>
	          <select id="wsp-trust">
	            <option value="">Inherit gateway default</option>
	            <option value="on">On — the launch folder is writable</option>
	            <option value="off">Off — launch folders get no special treatment</option>
	          </select>
	        </label>
	        <div class="wsp-mode-cards" id="wsp-mode-cards">
	          <label class="wsp-card" data-mode="" id="wsp-card-inherit">
	            <input type="radio" name="wsp-mode" value="">
	            <span><b>Inherit the gateway default</b>
	            <span class="wsp-card-sub" id="wsp-inherit-sub">Deny everything except the allowed folders — plus the folder an agent is launched from, while launch-folder trust is on.</span></span>
	          </label>
	          <label class="wsp-card" data-mode="whitelist">
	            <input type="radio" name="wsp-mode" value="whitelist">
	            <span><b>Deny everything, allow listed folders</b>
	            <span class="wsp-card-sub" id="wsp-whitelist-sub">Agents may only work under the gateway roots plus this user's allowed folders — and the launch folder while trust is on.</span></span>
	          </label>
	          <label class="wsp-card" data-mode="blacklist">
	            <input type="radio" name="wsp-mode" value="blacklist">
	            <span><b>Allow everything, refuse listed folders</b>
	            <span class="wsp-card-sub" id="wsp-blacklist-sub">Agents may work anywhere on the gateway host except the refused folders. The gateway-wide deny list still applies.</span></span>
	          </label>
	        </div>
	        <label class="wsp-field">Allowed folders
	          <span class="wsp-hint" id="wsp-allowed-hint">Extra folders this user's agents may use, one per line — added on top of the gateway-wide roots.</span>
	          <textarea id="wsp-allowed" rows="3" spellcheck="false" placeholder="/abs/path/to/project"></textarea>
	        </label>
	        <label class="wsp-field">Refused folders
	          <span class="wsp-hint" id="wsp-blocked-hint">Folders this user's agents may never touch, one per line — enforced in every posture.</span>
	          <textarea id="wsp-blocked" rows="3" spellcheck="false" placeholder="/abs/path/to/private"></textarea>
	        </label>
	        <details class="advanced-panel">
	          <summary>Advanced</summary>
	          <label class="wsp-field hidden" id="wsp-root-field">Default workspace folder
	            <span class="wsp-hint">Where a run lands when the client names no folder. Blank keeps the gateway's built-in default.</span>
	            <input id="wsp-root" type="text" spellcheck="false" placeholder="/abs/path/to/default/workspace">
	          </label>
	          <label class="wsp-field">Full filesystem bypass (legacy)
	            <span class="wsp-hint" id="wsp-overrides-hint">Not the same as launch-folder trust: trust only covers the one folder an agent is started from. This legacy switch lets this user's clients name ANY folder as a workspace and browse server files anywhere — the posture and folder lists above stop applying. Leave inherited unless an old client depends on it.</span>
	            <select id="wsp-overrides">
	              <option value="">Inherit gateway default</option>
	              <option value="on">Granted</option>
	              <option value="off">Refused</option>
	            </select>
	          </label>
	        </details>
	        <div id="wsp-message" class="message"></div>
	      </div>
	      <div class="modal-actions">
	        <button id="wsp-cancel" class="secondary">Cancel</button>
	        <button id="wsp-reset" class="secondary" title="Drop every override for this user — they fall back to the gateway defaults"><span class="button-icon" aria-hidden="true">×</span><span>Reset to inherited</span></button>
	        <button id="wsp-save" title="Save this user's workspace policy"><span class="button-icon" aria-hidden="true">✓</span><span>Save</span></button>
	      </div>
	    </div>
	  </div>
	  <div id="provider-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal wide provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-modal-title">
	      <h2 id="provider-modal-title">Configure provider</h2>
	      <p id="provider-modal-description">Gateway stores endpoint details and keys server-side, then exposes this connection as an available provider.</p>
	      <input id="endpoint-id" type="hidden">
	      <div class="provider-config-form">
	        <div class="inline">
	          <label>Provider type<select id="endpoint-provider-family"></select></label>
	          <label>Who can use it?<select id="endpoint-scope"></select></label>
	        </div>
	        <div class="inline">
	          <label>Provider ID<input id="endpoint-profile-id" placeholder="openai"></label>
	          <label>Name<input id="endpoint-name" placeholder="OpenAI"></label>
	        </div>
	        <label class="field-span-2">Description<textarea id="endpoint-description" placeholder="What this provider is for, who owns it, and when to use it."></textarea></label>
	        <label>Base URL<input id="endpoint-base-url" placeholder="optional; leave blank for provider default"></label>
	        <label>API key<input id="endpoint-api-key" type="password" placeholder="leave blank to keep existing key"></label>
	        <p id="endpoint-base-url-help" class="field-help"></p>
	        <p id="endpoint-api-key-help" class="field-help"></p>
	        <div class="provider-toggle-row">
	          <label class="check-row"><input id="endpoint-clear-api-key" type="checkbox"> Clear stored API key</label>
	          <label class="check-row"><input id="endpoint-enabled" type="checkbox" checked> Enabled</label>
	        </div>
	        <div class="model-picker field-span-2">
	          <details class="advanced-panel">
	            <summary>Advanced: restrict visible models after testing</summary>
	            <div class="advanced-panel__body">
	              <p class="field-help">Optional. Use Test to preview discovery, then select models only when this provider should expose a fixed allowlist.</p>
	              <div id="endpoint-model-summary" class="model-summary">Not tested yet.</div>
	              <select id="endpoint-models" class="model-picker__select" multiple size="7"></select>
	              <button id="clear-endpoint-models" type="button" class="secondary" title="Serve every model this endpoint exposes (no allowlist)"><span class="button-icon" aria-hidden="true">×</span><span>Clear restriction</span></button>
	            </div>
	          </details>
	        </div>
	        <div id="endpoint-message" class="message field-span-2"></div>
	      </div>
	      <div class="modal-actions">
	        <button id="cancel-endpoint-profile" class="secondary">Cancel</button>
	        <button id="discover-endpoint-models" type="button" class="secondary" title="Probe the endpoint and list the models it actually serves"><span class="button-icon icon-refresh" aria-hidden="true">↻</span><span>Test</span></button>
	        <button id="save-endpoint-profile" title="Store this connection server-side and expose it as a provider"><span class="button-icon" aria-hidden="true">✓</span><span>Confirm</span></button>
	      </div>
	    </div>
	  </div>
	  <!-- FIRST-RUN WIZARD. Opens by itself once per data dir
	       for an admin (after a #claim= link, or while /host/first-run says
	       not completed); the topbar Setup button reopens it. The ids below
	       are a contract: later workstreams mount AbstractCore's Engines /
	       Models fragments into #first-run-engines-body / #first-run-model-body. -->
	  <div id="first-run-backdrop" class="first-run-page hidden" role="presentation">
	    <div id="first-run-wizard" class="first-run-shell" role="dialog" aria-modal="true" aria-labelledby="first-run-title">
	      <aside class="first-run-rail" aria-label="Setup steps">
	        <div class="first-run-brand"><span class="shell_brand_mark" aria-hidden="true">↔</span><span>AbstractGateway</span></div>
	        <div class="first-run-intro">
	          <h2 id="first-run-title">Set up AbstractGateway</h2>
	          <p>Five short, optional steps. Skip anything you do not need: this guide stays one click away (Setup, at the bottom of the sidebar).</p>
	        </div>
	        <ol id="first-run-steps" class="first-run-steps" aria-label="Setup steps"></ol>
	        <div class="first-run-rail-foot">
	          <label class="ui-switch" title="Show commands, route ids and other technical details"><input id="first-run-advanced" type="checkbox"><span>Technical details</span></label>
	          <span class="subtle">Everything here also lives in the console tabs.</span>
	        </div>
	      </aside>
	      <div class="first-run-main">
	        <div id="first-run-scroll" class="first-run-scroll">
	          <div class="first-run-content">
	            <header class="first-run-head">
	              <div id="first-run-kicker" class="first-run-kicker">Step 1 of 5</div>
	              <h3 id="first-run-step-title">Welcome</h3>
	              <p id="first-run-step-lede"></p>
	            </header>
	            <!-- DOM CONTRACT (first-run; the ids below are what
	                 later work embeds into). Local engines render as cards
	                 (console_ui.py) into #first-run-engines-body; AbstractCore's
	                 Models screen mounts into #first-run-model-catalog. -->
	            <section id="first-run-step-welcome" class="first-run-panel" data-step="welcome">
	              <div id="first-run-host-summary"></div>
	            </section>
	            <section id="first-run-step-engines" class="first-run-panel hidden" data-step="engines">
	              <div id="first-run-engines-body"></div>
	            </section>
	            <section id="first-run-step-model" class="first-run-panel hidden" data-step="model">
	              <div id="first-run-model-body" class="first-run-panel">
	                <div id="first-run-model-recommended" class="first-run-panel"></div>
	                <div class="ui-section-title"><h3>Or choose any model that fits this computer</h3><span class="ui-sub">The same catalog as the Models tab, filtered to what fits. Download a model, then use it as your default text model.</span></div>
	                <div id="first-run-model-catalog"></div>
	              </div>
	            </section>
	            <section id="first-run-step-apps" class="first-run-panel hidden" data-step="apps">
	              <div id="first-run-apps-body"></div>
	            </section>
	            <section id="first-run-step-done" class="first-run-panel hidden" data-step="done">
	              <div id="first-run-done-body"></div>
	            </section>
	          </div>
	        </div>
	        <footer class="first-run-footer">
	          <div id="first-run-message" class="message" role="status" aria-live="polite"></div>
	          <button id="first-run-skip" class="secondary" title="Close the guide and do not open it automatically again">Skip setup</button>
	          <button id="first-run-back" class="secondary hidden">Back</button>
	          <button id="first-run-next">Next</button>
	          <button id="first-run-finish" class="hidden">Finish</button>
	        </footer>
	      </div>
	    </div>
	  </div>
	  <!-- The kit's AfAppearanceDialog island (console_islands.py); the modal
	       below is its no-bundle fallback. -->
	  <div id="af-appearance-root"></div>
	  <div id="appearance-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="appearance-title">
	      <h2 id="appearance-title">Appearance</h2>
	      <p>Theme and typography are stored locally in this browser.</p>
	      <div class="appearance-form">
	        <label for="appearance-theme">Theme</label>
	        <div class="appearance-control">
	          <select id="appearance-theme"></select>
	          <div id="appearance-swatches" class="theme-swatches" aria-hidden="true"></div>
	        </div>
	        <label for="appearance-font-size">Font size</label>
	        <select id="appearance-font-size">
	          <option value="sm">Compact</option>
	          <option value="md">Medium</option>
	          <option value="lg">Large</option>
	        </select>
	        <label for="appearance-header-size">Header size</label>
	        <select id="appearance-header-size">
	          <option value="compact">Compact</option>
	          <option value="standard">Standard</option>
	          <option value="large">Large</option>
	        </select>
	      </div>
	      <div class="modal-actions">
	        <button id="appearance-close" class="secondary">Close</button>
	      </div>
	    </div>
	  </div>
  <!-- Skills & MCP modals (console_skills_mcp.py): kit af-modal af-modal--wide, bound by bindModal. -->
  <div id="skill-modal-backdrop" class="af-modal-backdrop" hidden>
    <div class="af-modal af-modal--wide" role="dialog" aria-modal="true" aria-labelledby="skill-modal-title">
      <div class="af-modal__header">
        <h2 id="skill-modal-title" class="af-modal__title">Skill</h2>
        <button id="skill-modal-close" class="af-modal__close" type="button" aria-label="Close">×</button>
      </div>
      <div id="skill-modal-body" class="af-modal__body"></div>
      <div class="af-modal__footer">
        <p id="skill-modal-note" class="af-modal__footer-note" role="status" aria-live="polite"></p>
        <div id="skill-modal-actions" class="skmcp-footer-actions"></div>
      </div>
    </div>
  </div>
  <div id="mcp-modal-backdrop" class="af-modal-backdrop" hidden>
    <div class="af-modal af-modal--wide" role="dialog" aria-modal="true" aria-labelledby="mcp-modal-title">
      <div class="af-modal__header">
        <h2 id="mcp-modal-title" class="af-modal__title">Add MCP server</h2>
        <button id="mcp-modal-close" class="af-modal__close" type="button" aria-label="Close">×</button>
      </div>
      <div id="mcp-modal-body" class="af-modal__body"></div>
      <div class="af-modal__footer">
        <p id="mcp-modal-note" class="af-modal__footer-note" role="status" aria-live="polite"></p>
        <div class="skmcp-footer-actions">
          <button id="mcp-modal-test" class="secondary" type="button">Test connection</button>
          <button id="mcp-modal-save" type="button">Save</button>
        </div>
      </div>
    </div>
  </div>
  <!-- Account modals (DESIGN-v2 §2.3/§2.4): the kit's af-modal markup (docs/modal.md),
       bound through the islands' bindModal (focus trap, Esc, backdrop click). -->
  <div id="account-email-backdrop" class="af-modal-backdrop" hidden>
    <div class="af-modal" role="dialog" aria-modal="true" aria-labelledby="account-email-title">
      <div class="af-modal__header">
        <h2 id="account-email-title" class="af-modal__title">Email</h2>
        <button id="account-email-close" class="af-modal__close" type="button" aria-label="Close">×</button>
      </div>
      <div id="account-email-body" class="af-modal__body account-modal-body"></div>
    </div>
  </div>
  <div id="account-logs-backdrop" class="af-modal-backdrop" hidden>
    <div class="af-modal" role="dialog" aria-modal="true" aria-labelledby="account-logs-title">
      <div class="af-modal__header">
        <h2 id="account-logs-title" class="af-modal__title">Activity</h2>
        <button id="account-logs-close" class="af-modal__close" type="button" aria-label="Close">×</button>
      </div>
      <div class="af-modal__body account-modal-body">
        <div id="account-logs-filters" class="account-logs-filters" role="group" aria-label="Show"></div>
        <p id="account-logs-message" class="inline-state" role="status" aria-live="polite"></p>
        <ol id="account-logs-list" class="account-logs-list"></ol>
      </div>
      <div class="af-modal__footer">
        <p id="account-logs-note" class="af-modal__footer-note"></p>
      </div>
    </div>
  </div>
  <div id="confirm-backdrop" class="modal-backdrop hidden" role="presentation">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <h2 id="confirm-title">Confirm</h2>
      <p id="confirm-message"></p>
      <textarea id="confirm-input" class="hidden" rows="4"></textarea>
      <div class="modal-actions">
        <button id="confirm-cancel" class="secondary">Cancel</button>
        <button id="confirm-ok">Confirm</button>
      </div>
    </div>
  </div>
  <!-- Create user modal: progressive disclosure — required first, rare knobs
       behind Advanced. The issued token replaces the form on success (it is
       shown ONCE; closing early must not eat it). -->
  <div id="user-create-backdrop" class="modal-backdrop hidden" role="presentation">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="user-create-title">
      <h2 id="user-create-title">Create user</h2>
      <div id="user-create-form" class="af-form user-create-form">
        <div class="af-form__field">
          <label class="af-form__label" for="new-user">User ID</label>
          <input id="new-user" autocomplete="off" spellcheck="false" aria-describedby="new-user-help">
          <p id="new-user-help" class="af-form__help">Letters, digits, dots or dashes. This is how they sign in.</p>
        </div>
        <div class="af-form__field">
          <label class="af-form__label" for="new-roles">Role</label>
          <select id="new-roles">
            <option value="user" selected>User — runs workflows on their own runtime</option>
            <option value="admin">Admin — manages this gateway</option>
            <option value="readonly">Read-only — can look, cannot change anything</option>
          </select>
          <p id="new-roles-note" class="af-form__help hidden" role="status"></p>
        </div>
        <div class="af-form__field">
          <label class="af-form__label" for="new-email">Email address</label>
          <input id="new-email" type="email" autocomplete="off" spellcheck="false" aria-describedby="new-email-help">
          <p id="new-email-help" class="af-form__help">Where sign-in codes and notifications go. Leave empty if they have none; they can add it later.</p>
        </div>
        <details class="plain-disclosure"><summary>Advanced</summary>
          <div class="af-form">
            <div class="af-form__field">
              <label class="af-form__label" for="new-runtime">Runtime</label>
              <input id="new-runtime" autocomplete="off" spellcheck="false" aria-describedby="new-runtime-help">
              <p id="new-runtime-help" class="af-form__help">The data plane their runs, flows and sessions live in. Empty = their own, named after them.</p>
            </div>
            <div class="af-form__field">
              <label class="af-form__label" for="new-tenant">Tenant</label>
              <input id="new-tenant" value="default" autocomplete="off" spellcheck="false" aria-describedby="new-tenant-help">
              <p id="new-tenant-help" class="af-form__help">Leave &#39;default&#39; unless you run several tenants.</p>
            </div>
          </div>
        </details>
        <div class="modal-actions">
          <button id="create-user-cancel" class="secondary" type="button">Cancel</button>
          <button id="create-user" type="button">Create user</button>
        </div>
      </div>
      <div id="user-create-done" class="hidden">
        <div id="user-create-token" class="issued"></div>
        <div class="modal-actions"><button id="user-create-close">Done</button></div>
      </div>
      <div id="user-create-message" class="message"></div>
    </div>
  </div>
  <!-- Summon entity modal: only the creation questions (operator order:
       "a clean and proper 'create entity' that opens a modal where you ask
       only the relevant questions"). Template CRUD lives in its own modal. -->
  <div id="entity-create-backdrop" class="modal-backdrop hidden" role="presentation">
    <div class="modal wide" role="dialog" aria-modal="true" aria-labelledby="entity-create-title">
      <h2 id="entity-create-title">Summon a new entity</h2>
      <p class="section-note">Pick a spark template and name it. The name is permanent — there is no delete (spark v1-for-life), so it is validated (dry-run) before anything is written.</p>
      <div class="inline">
        <label>Template<select id="entity-template" title="The spark blueprint this identity is born from — copied at creation, never linked"></select></label>
        <label>Name<input id="entity-name" placeholder="e.g. Castor" title="Permanent identity name — validated before anything is written"></label>
      </div>
      <p id="entity-template-desc" class="section-note"></p>
      <div id="entity-template-values" class="entity-chip-row"></div>
      <p id="entity-create-admin-note" class="section-note hidden">Advanced configuration (substrate, per-phase capabilities) requires an admin session — entities you create carry the safe framework defaults; an admin can configure them after.</p>
      <details id="entity-advanced" class="entity-advanced">
        <summary>Advanced configuration (optional — defaults are safe)</summary>
        <div class="entity-config-block">
          <h3 class="entity-config-title">Substrate <span class="entity-config-hint">the mind: LLM provider &amp; model. Providers &amp; models autopopulate from this gateway; leave on "Gateway default" to inherit.</span></h3>
          <div class="inline">
            <label>Provider<select id="entity-new-provider" title="LLM provider for this entity's mind — Gateway default inherits the door's substrate"><option value="">Gateway default</option></select></label>
            <label>Model<select id="entity-new-model" disabled title="Model within the chosen provider"><option value="">Gateway default</option></select></label>
            <label>Reasoning<select id="entity-new-thinking" title="Reasoning effort for reasoning models — optional; 'not set' sends nothing">
              <option value="">not set</option>
              <option value="none">none</option>
              <option value="minimal">minimal</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="xhigh">xhigh</option>
            </select></label>
          </div>
          <h3 class="entity-config-title">Embedding at birth <span class="entity-config-hint">the M1 pin — the semantic space this life is born into. "Gateway default" pins the door's resolved embedder. Changing it later is the CRITICAL re-embed ceremony.</span></h3>
          <div class="inline">
            <label>Embedding model<select id="entity-new-embedding" title="Pinned at birth (M1); changing later requires the re-embed repair ceremony"><option value="">Gateway default</option></select></label>
          </div>
          <div id="entity-new-substrate-note" class="section-note"></div>
        </div>
        <div class="entity-config-block">
          <h3 class="entity-config-title">Per-phase capabilities <span class="entity-config-hint">which tools each phase may use — visit / work / personal / sleep. Defaults shown; toggle to override.</span></h3>
          <div id="entity-new-matrix" class="entity-matrix"></div>
        </div>
      </details>
      <div class="modal-actions">
        <button id="entity-create-cancel" class="secondary">Cancel</button>
        <button id="entity-create" title="Dry-run validate the name and configuration, then create"><span class="button-icon" aria-hidden="true">☾</span><span>Validate &amp; create</span></button>
      </div>
      <div id="entity-create-message" class="message"></div>
    </div>
  </div>
  <!-- Templates modal: blueprint management (view / edit / version), out of
       the creation flow — a template is management, a summon is a birth. -->
  <div id="templates-backdrop" class="modal-backdrop hidden" role="presentation">
    <div class="modal wide" role="dialog" aria-modal="true" aria-labelledby="templates-title">
      <h2 id="templates-title">Spark templates</h2>
      <p class="section-note">A template is a reusable blueprint — every save is a new version; the framework default is the floor and can be seeded but not edited. Editing a template never touches a living entity.</p>
      <div class="inline">
        <label>Template<select id="tpl-select" title="Templates on this gateway (builtin + operator-authored)"></select></label>
      </div>
      <div class="entity-btn-row">
        <button id="tpl-view" class="secondary" type="button" title="Show this template's spark document"><span class="button-icon" aria-hidden="true">◉</span><span>View</span></button>
        <button id="tpl-edit" class="secondary hidden" type="button" title="Edit this template (saves as a new version)"><span class="button-icon" aria-hidden="true">✎</span><span>Edit</span></button>
        <button id="tpl-new" class="secondary" type="button" title="Create a new template seeded from the selected one"><span class="button-icon" aria-hidden="true">＋</span><span>New from selected</span></button>
      </div>
      <div id="tpl-editor" class="hidden">
        <div class="inline">
          <label id="tpl-id-row" class="hidden">New template id<input id="tpl-id" placeholder="lowercase-letters-digits-_- (e.g. researcher)" title="Permanent template id (directory name)"></label>
          <label>Display name<input id="tpl-name" placeholder="e.g. Researcher"></label>
        </div>
        <label>Description<input id="tpl-desc" placeholder="what this blueprint is for"></label>
        <label>Spark (JSON — core values are enforced at save)<textarea id="tpl-spark" rows="14" spellcheck="false" class="tpl-spark"></textarea></label>
        <div class="entity-btn-row">
          <button id="tpl-save" class="secondary" type="button" title="Lint and save as a new version"><span class="button-icon" aria-hidden="true">✓</span><span>Save</span></button>
          <button id="tpl-cancel" class="secondary" type="button">Cancel</button>
        </div>
        <div id="tpl-out" class="section-note"></div>
        <div id="tpl-versions" class="section-note"></div>
      </div>
      <div class="modal-actions"><button id="templates-close" class="secondary">Close</button></div>
    </div>
  </div>
  <!-- Docs assistant drawer (.af-drawer public markup). Keep-alive: closed =
       display:none, never destroyed — an in-flight answer survives close/open. -->
  <div id="assistant-drawer" class="af-drawer" style="width: 420px; display: none;" role="complementary" aria-label="Docs assistant">
    <div class="af-drawer__header">
      <div class="af-drawer__title">Docs assistant</div>
      <div class="af-drawer__header-actions">
        <button id="assistant-clear" class="secondary" type="button" title="Start a new conversation (nothing earlier is replayed)">New conversation</button>
        <button id="assistant-close" class="af-drawer__close" type="button" aria-label="Close assistant">×</button>
      </div>
    </div>
    <div class="af-drawer__body">
      <div id="assistant-messages" class="assistant-messages"></div>
      <div id="assistant-replay" class="assistant-note" hidden></div>
      <div id="assistant-note" class="assistant-note">Answers are grounded on the gateway's own documentation (llms.txt) via the docs-qa workflow.</div>
      <form id="assistant-form" class="assistant-composer">
        <textarea id="assistant-input" rows="2" placeholder="Ask about the gateway…" aria-label="Question for the docs assistant"></textarea>
        <button id="assistant-send" type="submit" title="Ask the docs assistant (Enter)">Ask</button>
      </form>
    </div>
  </div>
  <!-- abstractuic ui-kit console islands (the bundle's first line names the
       kit version): the kit's React
       AfTopBarActions + AfAppearanceDialog, bundled by ui-kit
       scripts/build_islands.mjs, vendored + drift-pinned by
       console_islands_sync.py. Defines window.AfConsoleIslands. -->
  <script id="af-console-islands">/*__AF_CONSOLE_ISLANDS_JS__*/</script>
  <!--__ABSTRACTCORE_FRAGMENT_SCRIPT__-->
  <script>
		    const state = { principal: null, users: [], defaults: [], providers: [], providerLabels: new Map(), voiceLabels: new Map(), voiceReasons: new Map(), providerStateLabels: new Map(), providerModels: new Map(), endpointProfiles: [], endpointModelOptions: [], sandboxMessages: [], sandboxAttachments: [], sandboxObjectUrls: [], activeProviderPreset: "openai", activeTab: "providers", activeDefaultRow: null, confirmResolve: null, appearance: null, availability: new Map(), availabilityPlan: null, downloadJobs: new Map(), runtimeConfig: null, hostState: null, hostPollToken: 0, hostStateSeq: 0, modalityUi: null, modelEstimates: new Map(), modelsShowCached: false };
		    const $ = (id) => document.getElementById(id);
		    const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
		    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
	    // Page-wide inline-SVG icon registry (card 015 wave 3: one glyph = one
	    // verb; 24x24 stroke, currentColor — no emoji/unicode ambiguity, no
	    // VS15 platform lottery). The sandbox's local svgIcon() promoted here.
	    function svgIcon(paths, className = "") {
	      return `<svg class="${esc(className)}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths}</svg>`;
	    }
	    const ICONS = {
	      refresh: svgIcon('<path d="M20 11a8 8 0 1 0-2.3 6.3"></path><path d="M20 5v6h-6"></path>'),
	      retry: svgIcon('<path d="M4 13a8 8 0 1 0 2.3-6.3"></path><path d="M4 19v-6h6"></path>'),
	      gear: svgIcon('<circle cx="12" cy="12" r="3"></circle><path d="M12 2.5v3M12 18.5v3M4.4 6.2l2.1 2.1M17.5 15.7l2.1 2.1M2.5 12h3M18.5 12h3M4.4 17.8l2.1-2.1M17.5 8.3l2.1-2.1"></path>'),
	      warn: svgIcon('<path d="M12 4 2.8 19.5h18.4L12 4z"></path><path d="M12 10v4.5"></path><circle cx="12" cy="17" r=".6"></circle>'),
	      lock: svgIcon('<rect x="6" y="11" width="12" height="9" rx="2"></rect><path d="M9 11V8a3 3 0 0 1 6 0v3"></path>'),
	      mail: svgIcon('<rect x="3" y="5.5" width="18" height="13" rx="2"></rect><path d="m3.5 7 8.5 6 8.5-6"></path>'),
	      logs: svgIcon('<path d="M8 6.5h12M8 12h12M8 17.5h12"></path><circle cx="4" cy="6.5" r=".9"></circle><circle cx="4" cy="12" r=".9"></circle><circle cx="4" cy="17.5" r=".9"></circle>'),
	      folder: svgIcon('<path d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>'),
	      trash: svgIcon('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4.5h6V7"></path>'),
	    };
	    // Card 015 wave 3 (usability P2-1/2): a header-only table reads as
	    // BROKEN while its fetch runs — every table loader says what is
	    // happening. One recipe, per-table colspan.
	    function renderPager(el, opts) {
	      // EVERY gateway list reaches EVERY item:
	      // pages of `pageSize` with honest position text.
	      // Single-page lists render no chrome.
	      if (!el) return;
	      const { offset, pageSize, shown, hasMore, total, onPage } = opts;
	      el.textContent = "";
	      if (offset === 0 && !hasMore) return;
	      const mk = (label, disabled, nextOffset) => {
	        const b = document.createElement("button");
	        b.className = "secondary";
	        b.type = "button";
	        b.textContent = label;
	        b.disabled = !!disabled;
	        b.onclick = () => onPage(nextOffset);
	        return b;
	      };
	      const info = document.createElement("span");
	      info.className = "muted";
	      const from = shown ? offset + 1 : offset;
	      const to = offset + shown;
	      info.textContent = (typeof total === "number" && total >= to)
	        ? `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}`
	        : `${from.toLocaleString()}–${to.toLocaleString()}${hasMore ? " · more" : ""}`;
	      el.append(
	        mk("‹ Prev", offset === 0, Math.max(0, offset - pageSize)),
	        info,
	        mk("Next ›", !hasMore, offset + pageSize),
	      );
	    }
	    // THE CONSOLE QUERY LANGUAGE — one rule, every search box.
	    //
	    // Transcribed character-for-character from the gateway's
	    // `_glob_matches` / `_query_value_matches` (routes/gateway.py) and the
	    // console-TUI's `src/query.rs`. The Runs and Artifacts tabs filter
	    // SERVER-side and the Cache and Logs tabs filter here, so the same
	    // typed query has to mean the same thing in three languages.
	    //
	    // `*.jpg` in a runtime tab's box once found nothing —
	    // every filter was a plain substring, so the `*` matched literally.
	    //   * no `*` and no `?` -> case-insensitive SUBSTRING (unchanged)
	    //   * any `*` or `?`    -> case-insensitive GLOB anchored to the WHOLE
	    //     value, then retried against its basename
	    // `*` crosses `/` on purpose (these boxes filter a FLAT row list, they
	    // do not walk a tree). `[` is LITERAL — three implementations agree
	    // only on a language small enough to hold in one head.
	    function globMatches(value, pattern) {
	      // Anchored `*`/`?` glob. Linear scan with one backtrack point.
	      // Array.from, not indexing: one `?` must consume one CHARACTER.
	      const v = Array.from(value), p = Array.from(pattern);
	      let vi = 0, pi = 0, star = -1, mark = 0;
	      while (vi < v.length) {
	        if (pi < p.length && (p[pi] === "?" || p[pi] === v[vi])) { vi++; pi++; }
	        else if (pi < p.length && p[pi] === "*") { star = pi; pi++; mark = vi; }
	        else if (star >= 0) { pi = star + 1; mark++; vi = mark; }
	        else return false;
	      }
	      while (pi < p.length && p[pi] === "*") pi++;
	      return pi === p.length;
	    }
	    function makeNeedle(query) {
	      // Folded and classified ONCE per filter call, never per row.
	      const text = String(query || "").trim().toLowerCase();
	      const glob = text.includes("*") || text.includes("?");
	      const one = (value) => {
	        if (!text) return true;
	        const t = String(value || "").toLowerCase();
	        if (!t) return false;  // an ABSENT field never matches, not even `*`
	        if (!glob) return t.includes(text);
	        if (globMatches(t, text)) return true;
	        const base = t.split(/[/\\\\]/).pop();
	        return base !== t && globMatches(base, text);
	      };
	      return {
	        text, glob,
	        empty: !text,
	        matches: one,
	        matchesAny: (values) => !text || values.some(one),
	      };
	    }
	    function syncDerivedOptions(sel, values, allLabel) {
	      // Options derived from the RENDERED rows. Rebuild only when the set
	      // actually changes (the cache tab re-renders twice per load — fast
	      // pass then sized pass — and a naive rebuild would drop the
	      // operator's selection mid-walk). A vanished selection falls back
	      // to "all" and RETURNS that fact so the caller can say so.
	      if (!sel) return { reset: false };
	      const sorted = Array.from(new Set(values.filter(Boolean))).sort();
	      const signature = sorted.join("\u0001");
	      const prev = sel.value;
	      if (sel.dataset.optsig !== signature) {
	        sel.dataset.optsig = signature;
	        sel.textContent = "";
	        const all = document.createElement("option");
	        all.value = "";
	        all.textContent = allLabel;
	        sel.append(all);
	        for (const v of sorted) {
	          const opt = document.createElement("option");
	          opt.value = v;
	          opt.textContent = v;
	          sel.append(opt);
	        }
	      }
	      if (prev && !sorted.includes(prev)) {
	        sel.value = "";
	        return { reset: true, lost: prev };
	      }
	      sel.value = prev;
	      return { reset: false };
	    }
	    function tableLoadingRow(body, colSpan, text) {
	      if (!body) return;
	      body.textContent = "";
	      const tr = document.createElement("tr");
	      const td = document.createElement("td");
	      td.colSpan = colSpan;
	      td.className = "empty";
	      const spin = document.createElement("span");
	      spin.className = "spin-inline";
	      spin.setAttribute("aria-hidden", "true");
	      td.append(spin, document.createTextNode(text || "Loading…"));
	      tr.append(td);
	      body.append(tr);
	    }
	    function renderMarkdownInline(value) {
	      const renderPlain = (text) => {
	        const codeParts = [];
	        const marker = (index) => `%%AF_CODE_${index}%%`;
	        let tokenized = String(text || "").replace(/`([^`\\n]+)`/g, (_match, code) => {
	          const key = marker(codeParts.length);
	          codeParts.push(`<code>${esc(code)}</code>`);
	          return key;
	        });
	        let html = esc(tokenized);
	        html = html.replace(/[*][*]([^*\\n]+)[*][*]/g, "<strong>$1</strong>");
	        html = html.replace(/(^|[ (])[*]([^*\\n]+)[*]/g, "$1<em>$2</em>");
	        for (let i = 0; i < codeParts.length; i += 1) {
	          html = html.replace(marker(i), codeParts[i]);
	        }
	        return html;
	      };
	      const parts = [];
	      const source = String(value || "");
	      const linkRe = /\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g;
	      let cursor = 0;
	      let match = null;
	      while ((match = linkRe.exec(source))) {
	        parts.push(renderPlain(source.slice(cursor, match.index)));
	        parts.push(`<a href="${esc(match[2])}" target="_blank" rel="noopener">${renderPlain(match[1])}</a>`);
	        cursor = match.index + match[0].length;
	      }
	      parts.push(renderPlain(source.slice(cursor)));
	      return parts.join("");
	    }
	    function renderMarkdown(value) {
	      const lines = String(value ?? "").replace(/\\r\\n?/g, "\\n").split("\\n");
	      const out = [];
	      let paragraph = [];
	      let listType = "";
	      let inCode = false;
	      let codeLines = [];
	      const closeParagraph = () => {
	        if (!paragraph.length) return;
	        out.push(`<p>${renderMarkdownInline(paragraph.join(" "))}</p>`);
	        paragraph = [];
	      };
	      const closeList = () => {
	        if (!listType) return;
	        out.push(`</${listType}>`);
	        listType = "";
	      };
	      const openList = (type) => {
	        if (listType === type) return;
	        closeList();
	        listType = type;
	        out.push(`<${type}>`);
	      };
	      for (const rawLine of lines) {
	        const line = String(rawLine || "");
	        const trimmed = line.trim();
	        if (trimmed.startsWith("```")) {
	          if (inCode) {
	            out.push(`<pre><code>${esc(codeLines.join("\\n"))}</code></pre>`);
	            codeLines = [];
	            inCode = false;
	          } else {
	            closeParagraph();
	            closeList();
	            inCode = true;
	          }
	          continue;
	        }
	        if (inCode) {
	          codeLines.push(line);
	          continue;
	        }
	        if (!trimmed) {
	          closeParagraph();
	          closeList();
	          continue;
	        }
	        const heading = trimmed.match(/^(#{1,3})\\s+(.+)$/);
	        if (heading) {
	          closeParagraph();
	          closeList();
	          const level = Math.min(3, heading[1].length);
	          out.push(`<h${level}>${renderMarkdownInline(heading[2])}</h${level}>`);
	          continue;
	        }
	        const bullet = trimmed.match(/^[-*]\\s+(.+)$/);
	        if (bullet) {
	          closeParagraph();
	          openList("ul");
	          out.push(`<li>${renderMarkdownInline(bullet[1])}</li>`);
	          continue;
	        }
	        const ordered = trimmed.match(/^\\d+[.)]\\s+(.+)$/);
	        if (ordered) {
	          closeParagraph();
	          openList("ol");
	          out.push(`<li>${renderMarkdownInline(ordered[1])}</li>`);
	          continue;
	        }
	        const quote = trimmed.match(/^>\\s?(.+)$/);
	        if (quote) {
	          closeParagraph();
	          closeList();
	          out.push(`<blockquote>${renderMarkdownInline(quote[1])}</blockquote>`);
	          continue;
	        }
	        closeList();
	        paragraph.push(line);
	      }
	      if (inCode) out.push(`<pre><code>${esc(codeLines.join("\\n"))}</code></pre>`);
	      closeParagraph();
	      closeList();
	      return out.join("") || "";
	    }
	    function setSandboxMessageBody(body, content, { markdown = false } = {}) {
	      if (!body) return;
	      if (markdown) {
	        body.classList.add("markdown");
	        body.innerHTML = renderMarkdown(content);
	      } else {
	        body.classList.remove("markdown");
	        body.textContent = String(content || "");
	      }
	    }
	    const UI_SETTINGS_KEY = "abstractgateway_ui_settings_v1";
	    const ACTIVE_TAB_KEY = "abstractgateway_active_tab_v1";
	    // Tab order: daily path first — who lives behind this
	    // door (users & entities), where they run, then setup (providers,
	    // capability defaults), then validation (sandbox). A stale persisted
	    // "entities" value folds into "users" below.
	    const TABS = ["users", "runtimes", "workflows", "skills", "providers", "defaults", "sandbox", "models", "catalog", "apps", "network"];
	    // Retired tab ids and their new homes: a persisted value or a `#<id>` link
	    // still lands there (entities merged into users; DESIGN-v3 §7: the Engines
	    // tab merged into Providers).
	    const TAB_FOLDS = { entities: "users", engines: "providers" };
	    // The kit's THEME_SPECS (abstractuic theme.ts), generated by
	    // console_theme_sync — the console offers exactly the framework's
	    // themes, never a hand-copied subset.
	    const THEME_SPECS = __KIT_THEME_SPECS_JSON__;
	    // The About facts of THIS gateway (console_about_config): the served
	    // abstractgateway version + the gateway-version rows of GET /about.
	    const GATEWAY_ABOUT = __GATEWAY_ABOUT_JSON__;
	    function readJsonSetting(key, fallback) {
	      try {
	        const raw = localStorage.getItem(key);
	        return raw ? JSON.parse(raw) : fallback;
	      } catch {
	        return fallback;
	      }
	    }
	    function writeJsonSetting(key, value) {
	      try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
	    }
	    function readStringSetting(key, fallback) {
	      try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
	    }
	    function writeStringSetting(key, value) {
	      try { localStorage.setItem(key, value); } catch {}
	    }
	    function applyAppearanceSettings() {
	      const value = state.appearance || { theme: "dark", font_scale: "md", header_density: "standard" };
	      if (islands.lib) {
	        // The kit applies theme + typography itself (root theme class and
	        // --font-scale/--header-density), exactly as in the React apps.
	        for (const cls of ["font-sm", "font-md", "font-lg", "header-compact", "header-standard", "header-large"]) document.body.classList.remove(cls);
	        islands.lib.applyAppearance(kitAppearance(value));
	        return;
	      }
	      const root = document.documentElement || document.body;
	      for (const theme of THEME_SPECS) root.classList.remove(`theme-${theme.id}`);
	      root.classList.add(`theme-${value.theme || "dark"}`);
	      for (const cls of ["font-sm", "font-md", "font-lg", "header-compact", "header-standard", "header-large"]) document.body.classList.remove(cls);
	      document.body.classList.add(`font-${value.font_scale || "md"}`);
	      document.body.classList.add(`header-${value.header_density || "standard"}`);
	      renderThemeSwatches(value.theme || "dark");
	    }
	    function loadAppearanceSettings() {
	      const value = readJsonSetting(UI_SETTINGS_KEY, null);
	      return {
	        theme: String(value?.theme || "dark").trim() || "dark",
	        font_scale: String(value?.font_scale || "md").trim() || "md",
	        header_density: String(value?.header_density || "standard").trim() || "standard",
	      };
	    }
	    function saveAppearanceSettings() {
	      writeJsonSetting(UI_SETTINGS_KEY, state.appearance);
	    }
	    function initAppearanceControls() {
	      const themeSelect = $("appearance-theme");
	      themeSelect.textContent = "";
	      // Grouped like the kit's dialog: Dark themes first, then Light.
	      for (const groupName of ["dark", "light"]) {
	        const group = document.createElement("optgroup");
	        group.label = groupName === "dark" ? "Dark" : "Light";
	        for (const theme of THEME_SPECS.filter((t) => t.group === groupName)) {
	          const option = document.createElement("option");
	          option.value = theme.id;
	          option.textContent = theme.label;
	          group.append(option);
	        }
	        if (group.children.length) themeSelect.append(group);
	      }
	      // A persisted theme the kit later renamed/removed must not render a
	      // blank select: fall back visibly to the default.
	      themeSelect.value = state.appearance.theme;
	      if (!themeSelect.value) themeSelect.value = "dark";
	      $("appearance-font-size").value = state.appearance.font_scale;
	      $("appearance-header-size").value = state.appearance.header_density;
	      renderThemeSwatches(state.appearance.theme);
	    }
	    function renderThemeSwatches(themeId) {
	      const target = $("appearance-swatches");
	      if (!target) return;
	      const theme = THEME_SPECS.find((item) => item.id === themeId) || THEME_SPECS[0];
	      target.textContent = "";
	      for (const color of theme.swatches) {
	        const swatch = document.createElement("span");
	        swatch.className = "theme-swatch";
	        if (swatch.style) swatch.style.background = color;
	        target.append(swatch);
	      }
	    }
	    function updateAppearanceFromForm() {
	      state.appearance = {
	        theme: $("appearance-theme").value || "dark",
	        font_scale: $("appearance-font-size").value || "md",
	        header_density: $("appearance-header-size").value || "standard",
	      };
	      applyAppearanceSettings();
	      saveAppearanceSettings();
	    }
	    function openAppearance() {
	      // The kit's AfAppearanceDialog (island) when the bundle is live; the
	      // static modal is its fallback.
	      if (islands.appearance) { islands.appearanceOpen = true; renderIslands(); return; }
	      initAppearanceControls();
	      $("appearance-backdrop").classList.remove("hidden");
	    }
	    function closeAppearance() {
	      $("appearance-backdrop").classList.add("hidden");
	    }
	    // ---- Docs assistant drawer (docs-qa bundle transport) ----
	    // ask() = start a catalog run of docs-qa with the gateway's OWN corpus
	    // (GET /docs/corpus) and poll the run to completion. Never routes through
	    // entity chat (a visit is billable and forms memories — kit contract).
	    // History (ADR-0026, operator ruling 2026-09-28): no client-side copy and
	    // no turn cap. Each conversation is ONE gateway session; every question
	    // starts with use_session_history, so the gateway replays the earlier
	    // turns through the runtime's history window (newest whole turns up to
	    // 50,000 tokens) and records the receipt (run.session_history), shown
	    // when earlier messages were not replayed. New conversation = new session.
	    const ASSISTANT_BUNDLE = { registry_scope: "tenant_catalog", bundle_id: "docs-qa", bundle_version: "0.1.1", flow_id: "docsqa001" };
	    // The kit's randomId() (ui-kit 0.3.3, random_id.ts), ported: browsers
	    // withhold crypto.randomUUID over plain http (a console opened from
	    // another machine), so a v4 UUID is built from getRandomValues then
	    // (DESIGN §11). Never Math.random.
	    function randomId() {
	      const c = typeof crypto !== "undefined" ? crypto : null;
	      if (c && typeof c.randomUUID === "function") {
	        try { return c.randomUUID(); } catch {}
	      }
	      if (c && typeof c.getRandomValues === "function") {
	        const b = Array.from(c.getRandomValues(new Uint8Array(16)));
	        b[6] = (b[6] & 0x0f) | 0x40;
	        b[8] = (b[8] & 0x3f) | 0x80;
	        const h = b.map((x) => x.toString(16).padStart(2, "0")).join("");
	        return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
	      }
	      throw new Error("randomId: Web Crypto (crypto.getRandomValues) is unavailable");
	    }
	    // Copy that also works over plain http (DESIGN §11, COORD 21:05): the
	    // Clipboard API in a secure context, else a hidden textarea +
	    // execCommand("copy"). Resolves true when the text reached the
	    // clipboard, so the caller says "Copied" or "Copy failed — select and copy".
	    const COPY_FAILED = "Copy failed — select and copy";
	    function execCommandCopy(text) {
	      try {
	        if (typeof document === "undefined" || !document.body || typeof document.execCommand !== "function") return false;
	        const el = document.createElement("textarea");
	        el.value = text;
	        el.setAttribute("readonly", "");
	        el.style.position = "fixed"; el.style.top = "0"; el.style.left = "-9999px"; el.style.opacity = "0";
	        const active = document.activeElement;
	        document.body.appendChild(el);
	        let ok = false;
	        try { el.select(); el.setSelectionRange(0, text.length); ok = document.execCommand("copy") === true; }
	        finally { document.body.removeChild(el); try { active && active.focus && active.focus(); } catch {} }
	        return ok;
	      } catch { return false; }
	    }
	    async function clipboardWrite(text) {
	      const value = String(text == null ? "" : text);
	      try {
	        if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
	          await navigator.clipboard.writeText(value);
	          return true;
	        }
	      } catch { /* fall back below */ }
	      return execCommandCopy(value);
	    }
	    function assistantNewSessionId() {
	      return "gateway-docs-assistant:" + randomId();
	    }
	    const assistantState = { open: false, busy: false, corpus: null, corpusWarned: false, sessionId: assistantNewSessionId() };
	    function assistantReplayNote(history) {
	      const n = (v) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.trunc(v) : 0);
	      const dropped = n(history && history.dropped_messages);
	      if (!dropped) return "";
	      const replayed = n(history.replayed_messages);
	      const tokens = n(history.dropped_tokens);
	      const budget = n(history.max_tokens);
	      return `Earlier messages not replayed: ${dropped.toLocaleString("en-US")}${tokens ? ` (~${tokens.toLocaleString("en-US")} tokens)` : ""}. ` +
	        `The model read the newest ${replayed.toLocaleString("en-US")} message${replayed === 1 ? "" : "s"}` +
	        (budget ? ` (history window: the most recent ${budget.toLocaleString("en-US")} tokens of whole messages).` : ".");
	    }
	    function assistantShowReplay(history) {
	      const text = assistantReplayNote(history);
	      $("assistant-replay").textContent = text;
	      $("assistant-replay").hidden = !text;
	    }
	    function assistantAppend(role, text, extraClass) {
	      const div = document.createElement("div");
	      div.className = `assistant-msg ${role}${extraClass ? ` ${extraClass}` : ""}`;
	      div.textContent = text;
	      $("assistant-messages").append(div);
	      $("assistant-messages").scrollTop = $("assistant-messages").scrollHeight;
	      return div;
	    }
	    function toggleAssistant(force) {
	      const next = typeof force === "boolean" ? force : !assistantState.open;
	      assistantState.open = next;
	      $("assistant-drawer").style.display = next ? "flex" : "none";
	      $("open-assistant").classList.toggle("is-active", next);
	      $("open-assistant").setAttribute("aria-pressed", next ? "true" : "false");
	      renderIslands();
	      if (next) $("assistant-input").focus();
	    }
	    async function assistantEnsureCorpus() {
	      if (assistantState.corpus !== null) return assistantState.corpus;
	      try {
	        const data = await api("/api/gateway/docs/corpus");
	        assistantState.corpus = { app: data.app || "AbstractGateway", text: data.text || "" };
	      } catch (err) {
	        // Honest degradation: the bundle itself answers "no docs were
	        // supplied" — but the operator should see WHY, once.
	        assistantState.corpus = { app: "AbstractGateway", text: "" };
	        if (!assistantState.corpusWarned) {
	          assistantState.corpusWarned = true;
	          $("assistant-note").textContent = `#FALLBACK no documentation corpus available (${err.message}) — answers are ungrounded.`;
	        }
	      }
	      return assistantState.corpus;
	    }
	    async function assistantAsk(question) {
	      const corpus = await assistantEnsureCorpus();
	      const started = await api("/api/gateway/runs/start", {
	        method: "POST",
	        body: JSON.stringify({
	          ...ASSISTANT_BUNDLE,
	          actor_id: "gateway",
	          session_id: assistantState.sessionId,
	          input_data: {
	            prompt: question,
	            docs: corpus.text,
	            app: corpus.app,
	            use_session_history: true,
	          },
	        }),
	      });
	      const runId = started.run_id;
	      // Poll to terminal state; a docs answer is one LLM call (bounded), but
	      // slow local models happen — cap at ~3 minutes then report honestly.
	      for (let i = 0; i < 90; i += 1) {
	        await new Promise((resolve) => setTimeout(resolve, 2000));
	        const run = await api(`/api/gateway/runs/${encodeURIComponent(runId)}`);
	        const status = String(run.status || "");
	        if (status === "completed") {
	          const out = run.output || {};
	          const text = typeof out.response === "string" && out.response.trim() ? out.response : JSON.stringify(out);
	          return { text, history: run.session_history || null };
	        }
	        if (status === "failed" || status === "cancelled") {
	          throw new Error(`docs-qa run ${status}: ${JSON.stringify(run.error || run.output || {}).slice(0, 300)}`);
	        }
	      }
	      throw new Error(`docs-qa run ${runId} still running after 3 minutes — check the Runtimes tab`);
	    }
	    async function assistantSubmit(event) {
	      if (event) event.preventDefault();
	      if (assistantState.busy) return;
	      const question = $("assistant-input").value.trim();
	      if (!question) return;
	      assistantState.busy = true;
	      $("assistant-send").disabled = true;
	      $("assistant-input").value = "";
	      assistantAppend("user", question);
	      const pending = assistantAppend("assistant", "Thinking…", "pending");
	      try {
	        const answer = await assistantAsk(question);
	        pending.classList.remove("pending");
	        pending.textContent = answer.text;
	        assistantShowReplay(answer.history);
	      } catch (err) {
	        pending.classList.remove("pending");
	        pending.classList.add("error");
	        pending.textContent = `Failed: ${err.message}`;
	      } finally {
	        assistantState.busy = false;
	        $("assistant-send").disabled = false;
	      }
	    }
	    function assistantClear() {
	      assistantState.sessionId = assistantNewSessionId();
	      $("assistant-messages").textContent = "";
	      assistantShowReplay(null);
	    }
	    const TAB_TITLES = {
	      users: ["Accounts", "People who use this gateway and the entities that act on it"],
	      runtimes: ["Runtimes", "Each user's own data plane: runs, flows, sessions and memory"],
      workflows: ["Workflows", "Bundles, versions, import and export"],
      skills: ["Skills & MCP", "Skills agents can load, and MCP tool servers"],
	      providers: ["Providers", "Local engines and remote provider connections"],
	      defaults: ["Multimodal Capabilities", "Which provider/model serves each capability route"],
	      sandbox: ["Sandbox", "Try any provider/model directly — text, image, audio, video"],
	      models: ["Resources", "Host resources: loaded models, memory and GPU, session caches"],
	      catalog: ["Models", "Browse, download and delete models that fit this machine"],
	      apps: ["Apps", "Install and open the apps that work with this gateway"],
	      network: ["Network", "Who can reach this gateway, and at which addresses"],
	    };
	    const USERS_TITLE_NON_ADMIN = ["Your account", "Your account and the entities you created."];
	    function setActiveTab(tab) {
	      // Legacy persisted tab ids fold into their new homes (entities
	      // merged into users); unknown ids land on the FIRST tab — the
	      // landing must agree with the nav order, not point mid-bar.
	      const fold = TAB_FOLDS[tab] || tab;
	      const next = TABS.includes(fold) ? fold : TABS[0];
	      state.activeTab = next;
	      for (const id of TABS) {
	        const panel = $(`tab-${id}`);
	        const button = $(`tab-button-${id}`);
	        if (panel) panel.classList.toggle("active", id === next);
	        if (button) button.classList.toggle("active", id === next);
	      }
	      // The slim header names the page (family shell: sidebar navigates,
	      // header titles) — signed out it stays the app name.
	      if (state.principal && TAB_TITLES[next]) {
	        // F6 (DESIGN G2): a non-admin's Accounts page is their own account.
	        const t = next === "users" && !state.principal.admin ? USERS_TITLE_NON_ADMIN : TAB_TITLES[next];
	        $("page-title").textContent = t[0];
	        $("page-subtitle").textContent = t[1];
	      }
	      writeStringSetting(ACTIVE_TAB_KEY, next);
	      if (next === "skills") openSkillsMcpPage();  // console_skills_mcp.py
	      mcOnTabChange(next);  // the catalog's `#catalog?...` link follows the tab (console_catalog.py)
	    }
	    // ---- Workflows: the registered workflow registry ----
	    // Server truth only: /bundles gives the served set AND the versions it
	    // refused to serve. Nothing is re-derived client-side, every user value
	    // goes through textContent, every write through api().
	    state.workflows = [];
	    state.workflowsSkipped = [];
	    state.selectedWorkflow = "";

	    // DESIGN-v2 §4.1: one row per bundle -- Name (the default entrypoint's name, the
	    // bundle id small below) · What it does · Version (latest, "+N older") · Source ·
	    // Used by (plain app names from the interface table, each with a (?)) · Export /
	    // Delete. A click expands the row: versions, channel, created, entrypoints.
	    state.workflowsExpanded = new Set();
	    state.workflowsShowDrafts = false;
	    state.workflowsShowOlder = false;
	    const WORKFLOW_SOURCE_TEXT = { shipped: "Shipped with the gateway", imported: "Imported", published: "Published from AbstractFlow" };
	    // F4: a manifest whose bundle_version is the placeholder "0.0.0" (the Assistant orchestrator
	    // ships so) has no version; say "unversioned" instead of a number that looks like one.
	    const WORKFLOW_UNVERSIONED = new Set(["0.0.0"]);
	    function workflowVersionLabel(v) {
	      const text = String(v || "");
	      return WORKFLOW_UNVERSIONED.has(text) ? "unversioned" : text;
	    }
	    function workflowVersionCmp(a, b) {
	      const pa = String(a || "").split(/[.+-]/).map((x) => (/^\\d+$/.test(x) ? Number(x) : x));
	      const pb = String(b || "").split(/[.+-]/).map((x) => (/^\\d+$/.test(x) ? Number(x) : x));
	      for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
	        const x = pa[i]; const y = pb[i];
	        if (x === undefined) return -1;
	        if (y === undefined) return 1;
	        if (x === y) continue;
	        if (typeof x === "number" && typeof y === "number") return x - y;
	        return String(x).localeCompare(String(y));
	      }
	      return 0;
	    }
	    function workflowInterfaceInfo(iface) {
	      const rows = (agentDefStore.data && agentDefStore.data.agents && agentDefStore.data.agents.default_workflow) || {};
	      return rows[iface] || null;
	    }
	    function workflowRows() {
	      const byId = new Map();
	      for (const it of state.workflows) {
	        const id = String(it.bundle_id || "");
	        if (!id) continue;
	        let row = byId.get(id);
	        if (!row) {
	          row = { bundle_id: id, versions: [], deprecated: false };
	          byId.set(id, row);
	        }
	        row.versions.push(it);
	      }
	      for (const row of byId.values()) {
	        row.versions.sort((a, b) => workflowVersionCmp(b.bundle_version, a.bundle_version));
	        const latestId = row.versions.find((v) => !v.is_draft) || row.versions[0];
	        row.latest = latestId;
	        const eps = latestId.entrypoints || [];
	        const def = eps.find((e) => e && e.flow_id === latestId.default_entrypoint) || eps[0] || {};
	        row.name = String(def.name || row.bundle_id);
	        if (typeof latestId.description !== "string") {
	          console.error(`AbstractGateway console: GET /bundles item ${row.bundle_id} has no description (gateway-api seam, DESIGN-v2 §6).`);
	        }
	        row.description = typeof latestId.description === "string" ? latestId.description : "";
	        row.source = latestId.source;
	        row.deprecated = eps.length > 0 && eps.every((e) => e && e.deprecated);
	        const ifaces = [];
	        for (const e of eps) for (const i of (e && e.interfaces) || []) if (!ifaces.includes(i)) ifaces.push(i);
	        row.interfaces = ifaces;
	      }
	      const needle = String($("workflows-search").value || "").trim().toLowerCase();
	      let rows = [...byId.values()];
	      if (needle) {
	        rows = rows.filter((r) => r.bundle_id.toLowerCase().includes(needle) || r.name.toLowerCase().includes(needle)
	          || r.description.toLowerCase().includes(needle)
	          || r.versions.some((v) => (v.entrypoints || []).some((e) => String(e.name || "").toLowerCase().includes(needle) || String(e.description || "").toLowerCase().includes(needle))));
	      }
	      rows.sort((a, b) => a.name.localeCompare(b.name));
	      return rows;
	    }
	    function workflowHelpQ(label, help) {
	      // A (?) that says what an interface is: a disclosure, so it works on touch too.
	      const d = document.createElement("details");
	      d.className = "help-q";
	      const s = document.createElement("summary");
	      s.textContent = "?";
	      s.setAttribute("aria-label", `What is “${label}”?`);
	      const p = document.createElement("p");
	      p.className = "help-q__text";
	      p.textContent = help;
	      d.append(s, p);
	      return d;
	    }
	    function workflowUsedByCell(row) {
	      const td = document.createElement("td");
	      td.className = "workflows-usedby";
	      td.setAttribute("data-label", "Used by");
	      if (!row.interfaces.length) { td.innerHTML = `<span class="muted">No app</span>`; return td; }
	      for (const iface of row.interfaces) {
	        const info = workflowInterfaceInfo(iface);
	        const item = document.createElement("span");
	        item.className = "workflows-usedby__item";
	        if (info && info.label) {
	          const name = document.createElement("span");
	          name.textContent = info.label;
	          item.append(name, workflowHelpQ(info.label, `${info.help || ""} (${iface})`.trim()));
	        } else {
	          const code = document.createElement("code");
	          code.textContent = iface;
	          item.append(code);
	        }
	        td.append(item);
	      }
	      return td;
	    }
	    function workflowDetailRow(row) {
	      const tr = document.createElement("tr");
	      tr.className = "workflows-detail";
	      const td = document.createElement("td");
	      td.colSpan = 6;
	      const versions = state.workflowsShowOlder ? row.versions : [row.latest];
	      const list = document.createElement("div");
	      list.className = "workflows-versions";
	      for (const v of versions) {
	        const box = document.createElement("div");
	        box.className = "workflows-version";
	        const head = document.createElement("div");
	        head.className = "workflows-version__head";
	        const title = document.createElement("strong");
	        title.textContent = workflowVersionLabel(v.bundle_version);
	        const meta = document.createElement("span");
	        meta.className = "muted";
	        meta.textContent = `${v.version_channel || (v.is_draft ? "draft" : "published")} · ${String(v.created_at || "").slice(0, 10)}`;
	        head.append(title, meta);
	        const acts = document.createElement("span");
	        acts.className = "actions";
	        const ex = document.createElement("button");
	        ex.type = "button"; ex.className = "secondary small"; ex.textContent = "Export";
	        ex.setAttribute("aria-label", `Export ${row.name} ${v.bundle_version}`);
	        ex.onclick = () => exportWorkflow(row.bundle_id, v.bundle_version);
	        acts.append(ex);
	        if (state.principal && state.principal.admin) {
	          const del = document.createElement("button");
	          del.type = "button"; del.className = "secondary danger small"; del.textContent = "Delete";
	          del.setAttribute("aria-label", `Delete ${row.name} ${v.bundle_version}`);
	          del.onclick = (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); deleteWorkflow(row.bundle_id, String(v.bundle_version || ""), { ...row, versions: [v] }, box); };
	          acts.append(del);
	        }
	        head.append(acts);
	        box.append(head);
	        for (const ep of v.entrypoints || []) {
	          const line = document.createElement("div");
	          line.className = "workflows-entry";
	          const n = document.createElement("span");
	          n.className = "workflows-entry__name";
	          n.textContent = String(ep.name || ep.flow_id || "");
	          line.append(n);
	          if (ep.deprecated) { const pill = document.createElement("span"); pill.className = "pill"; pill.textContent = "Deprecated"; line.append(pill); }
	          const d = document.createElement("span");
	          d.className = "workflows-entry__desc";
	          d.textContent = String(ep.description || "");
	          line.append(d);
	          const ifs = document.createElement("span");
	          ifs.className = "workflows-entry__ifaces";
	          ifs.textContent = (ep.interfaces || []).map((i) => (workflowInterfaceInfo(i) || {}).label || i).join(" · ");
	          line.append(ifs);
	          box.append(line);
	        }
	        list.append(box);
	      }
	      if (!state.workflowsShowOlder && row.versions.length > 1) {
	        const more = document.createElement("p");
	        more.className = "section-note";
	        more.textContent = `${row.versions.length - 1} older version${row.versions.length === 2 ? "" : "s"} — turn on “Older versions” to see them.`;
	        list.append(more);
	      }
	      td.append(list);
	      tr.append(td);
	      return tr;
	    }
	    function renderWorkflows() {
	      const tbody = $("workflows-table");
	      tbody.textContent = "";
	      const rows = workflowRows();
	      if (!rows.length) {
	        const tr = document.createElement("tr");
	        const td = document.createElement("td");
	        td.colSpan = 6; td.className = "message";
	        td.textContent = state.workflows.length ? "No workflow matches this search." : "No workflows registered.";
	        tr.appendChild(td); tbody.appendChild(tr);
	      }
	      for (const row of rows) {
	        const open = state.workflowsExpanded.has(row.bundle_id);
	        const tr = document.createElement("tr");
	        tr.className = "row-selectable workflows-row";
	        tr.setAttribute("data-bundle", row.bundle_id);
	        tr.setAttribute("aria-expanded", open ? "true" : "false");
	        tr.tabIndex = 0;
	        const toggle = () => selectWorkflow(row.bundle_id);
	        tr.onclick = toggle;
	        tr.onkeydown = (ev) => { if (ev && (ev.key === "Enter" || ev.key === " ") && ev.target === tr) { ev.preventDefault(); toggle(); } };

	        const name = document.createElement("td");
	        name.className = "workflows-name";
	        name.innerHTML = `<div class="workflows-name__wrap"><span class="workflows-chevron" aria-hidden="true">${open ? "▾" : "▸"}</span><span class="workflows-name__text"><strong></strong><small class="muted"></small></span></div>`;
	        name.querySelector("strong").textContent = row.name;
	        name.querySelector("small").textContent = row.bundle_id;
	        if (row.deprecated) { const pill = document.createElement("span"); pill.className = "pill"; pill.textContent = "Deprecated"; name.querySelector(".workflows-name__text").append(pill); }
	        const olderCount = row.versions.length - 1;
	        const versionText = `${workflowVersionLabel(row.latest.bundle_version) || "No version"}${olderCount > 0 ? ` +${olderCount} older` : ""}`;
	        if (!WORKFLOW_SOURCE_TEXT[row.source]) console.error(`AbstractGateway console: GET /bundles item ${row.bundle_id} has no known source (gateway-api seam, DESIGN-v2 §6): ${row.source}`);
	        const sourceText = WORKFLOW_SOURCE_TEXT[row.source] || "Unknown source";
	        // Narrow screens (DESIGN §12): the description and "Version · source" fold under the
	        // name as label·value lines instead of one captioned block per field.
	        const foldWhat = document.createElement("span");
	        foldWhat.className = "workflows-fold-what workflows-clamp";
	        foldWhat.textContent = row.description || "No description.";
	        const foldMeta = document.createElement("span");
	        foldMeta.className = "workflows-fold-meta";
	        foldMeta.innerHTML = `<span class="workflows-fold-version"></span><span class="workflows-fold-source"></span>`;
	        foldMeta.querySelector(".workflows-fold-version").textContent = `Version ${versionText} · `;
	        foldMeta.querySelector(".workflows-fold-source").textContent = sourceText;
	        name.querySelector(".workflows-name__text").append(foldWhat, foldMeta);
	        tr.appendChild(name);

	        const what = document.createElement("td");
	        what.className = "workflows-what";
	        what.setAttribute("data-label", "What it does");
	        const whatText = document.createElement("span");
	        whatText.className = "workflows-clamp";
	        whatText.textContent = row.description || "No description.";
	        what.append(whatText);
	        if (row.description) what.title = row.description;
	        tr.appendChild(what);

	        const version = document.createElement("td");
	        version.className = "workflows-version-cell";
	        version.setAttribute("data-label", "Version");
	        version.textContent = versionText;
	        tr.appendChild(version);

	        const source = document.createElement("td");
	        source.className = "workflows-source";
	        source.setAttribute("data-label", "Source");
	        source.textContent = sourceText;
	        tr.appendChild(source);

	        tr.appendChild(workflowUsedByCell(row));

	        const actions = document.createElement("td");
	        actions.className = "workflows-actions";
	        const wrap = document.createElement("div");
	        wrap.className = "actions";
	        const exportBtn = document.createElement("button");
	        exportBtn.className = "secondary small";
	        exportBtn.textContent = "Export";
	        exportBtn.setAttribute("aria-label", `Export ${row.name}`);
	        exportBtn.title = "Download the latest version as a .flow file";
	        exportBtn.onclick = (ev) => { ev.stopPropagation(); exportWorkflow(row.bundle_id, row.latest.bundle_version); };
	        wrap.appendChild(exportBtn);
	        if (state.principal && state.principal.admin) {
	          const del = document.createElement("button");
	          del.className = "secondary danger small";
	          del.textContent = "Delete";
	          del.setAttribute("aria-label", `Delete ${row.name}`);
	          del.title = "Remove every version of this workflow";
	          del.onclick = (ev) => { ev.stopPropagation(); deleteWorkflow(row.bundle_id, null, row, tr); };
	          wrap.appendChild(del);
	        }
	        actions.appendChild(wrap);
	        tr.appendChild(actions);
	        tbody.appendChild(tr);
	        if (open) tbody.appendChild(workflowDetailRow(row));
	      }
	      renderWorkflowsSkipped();
	    }

	    function renderWorkflowsSkipped() {
	      // GROUP BY (workflow, reason). Nine versions of one bundle failing for
	      // one reason is ONE problem; printing it nine times buries that fact
	      // and reads as a wall of unexplained errors.
	      const rows = state.workflowsSkipped || [];
	      $("workflows-skipped-section").classList.toggle("hidden", !rows.length);
	      const groups = new Map();
	      for (const rec of rows) {
	        const key = `${rec.bundle_id}\\u0000${rec.reason}`;
	        const g = groups.get(key);
	        if (g) { g.count += 1; g.paths.push(rec.path); }
	        else groups.set(key, { bundle_id: rec.bundle_id, reason: rec.reason, count: 1, paths: [rec.path] });
	      }
	      const workflows = new Set(rows.map((r) => r.bundle_id)).size;
	      $("workflows-skipped-count").textContent = rows.length
	        ? `${workflows} workflow${workflows === 1 ? "" : "s"}, ${rows.length} version${rows.length === 1 ? "" : "s"} the gateway could not load.`
	        : "";
	      const tbody = $("workflows-skipped-table");
	      tbody.textContent = "";
	      for (const g of groups.values()) {
	        const tr = document.createElement("tr");
	        const name = document.createElement("td");
	        name.textContent = g.bundle_id;
	        tr.appendChild(name);

	        const affected = document.createElement("td");
	        affected.textContent = g.count === 1 ? "1 version" : `${g.count} versions`;
	        affected.title = g.paths.join("\\n");
	        tr.appendChild(affected);

	        const why = document.createElement("td");
	        why.textContent = g.reason;
	        tr.appendChild(why);

	        const actions = document.createElement("td");
	        if (state.principal && state.principal.admin) {
	          const wrap = document.createElement("div");
	          wrap.className = "actions";
	          const del = document.createElement("button");
	          del.className = "secondary danger";
	          del.textContent = g.count === 1 ? "Delete" : `Delete ${g.count}`;
	          del.title = "Remove these unusable bundle files";
	          del.onclick = () => deleteBrokenGroup(g);
	          wrap.appendChild(del);
	          actions.appendChild(wrap);
	        }
	        tr.appendChild(actions);
	        tbody.appendChild(tr);
	      }
	    }

	    async function deleteBrokenGroup(group) {
	      const versions = (state.workflowsSkipped || [])
	        .filter((r) => r.bundle_id === group.bundle_id && r.reason === group.reason)
	        .map((r) => r.bundle_version);
	      const ok = await confirmAction({
	        title: `Delete ${versions.length} broken version(s) of ${group.bundle_id}?`,
	        message: `${group.reason}\\n\\nThese versions cannot run, so nothing that works stops working. The files are removed from disk and there is no undo.`,
	        confirmLabel: `Delete ${versions.length}`,
	        danger: true,
	      });
	      if (!ok) return;
	      let removed = 0;
	      const failed = [];
	      for (const version of versions) {
	        try {
	          const res = await api(`/api/gateway/bundles/${encodeURIComponent(group.bundle_id)}?bundle_version=${encodeURIComponent(version)}&reload=false`, { method: "DELETE" });
	          removed += Number(res.removed || 0);
	        } catch (err) { failed.push(`${version}: ${String(err.message || err)}`); }
	      }
	      // ONE reload after the batch, not one per file: reloading a 231-file
	      // registry per delete is the freeze, not the deletes.
	      try { await api("/api/gateway/bundles/reload", { method: "POST" }); } catch (err) { /* listed below */ }
	      $("workflows-message").textContent = failed.length
	        ? `Removed ${removed}; failed — ${failed.join("; ")}.`
	        : `Removed ${removed} broken file(s) for ${group.bundle_id}.`;
	      $("workflows-message").className = failed.length ? "message error" : "message ok";
	      await loadWorkflows();
	    }

	    async function loadWorkflows() {
	      $("workflows-message").textContent = "Loading…";
	      $("workflows-message").className = "message";
	      try {
	        const drafts = state.workflowsShowDrafts ? "1" : "0";
	        const data = await api(`/api/gateway/bundles?all_versions=true&include_drafts=${drafts}&include_deprecated=true`);
	        state.workflows = data.items || [];
	        state.workflowsSkipped = data.skipped || [];
	        state.workflowsDefaultId = data.default_bundle_id || "";
	        $("workflows-message").textContent = "";
	        renderWorkflows();
	      } catch (err) {
	        $("workflows-message").textContent = String(err.message || err);
	        $("workflows-message").className = "message error";
	      }
	    }

	    function selectWorkflow(bundleId) {
	      const id = String(bundleId || "");
	      if (state.workflowsExpanded.has(id)) state.workflowsExpanded.delete(id);
	      else state.workflowsExpanded.add(id);
	      renderWorkflows();
	    }
	    function mountWorkflowSwitches() {
	      // Drafts / Older versions: kit switches labelled by the feature (never a verb), applied at once.
	      if ($("workflows-drafts-slot").childNodes.length) return;
	      const drafts = afSwitchCreate({ id: "workflows-show-drafts", label: "Drafts", checked: state.workflowsShowDrafts, small: true });
	      $("workflows-drafts-slot").append(...drafts.nodes);
	      afSwitchBind(drafts.button, async (next) => { state.workflowsShowDrafts = next; await loadWorkflows(); return next; }, (e) => { $("workflows-message").textContent = emailErrorText(e); $("workflows-message").className = "message error"; });
	      const older = afSwitchCreate({ id: "workflows-show-older", label: "Older versions", checked: state.workflowsShowOlder, small: true });
	      $("workflows-older-slot").append(...older.nodes);
	      afSwitchBind(older.button, async (next) => { state.workflowsShowOlder = next; renderWorkflows(); return next; }, () => {});
	    }

	    function exportWorkflow(bundleId, bundleVersion) {
	      // The browser download lane: a normal navigation so the gateway's
	      // Content-Disposition names the file. Bytes are the ORIGINAL .flow.
	      const qs = bundleVersion ? `?bundle_version=${encodeURIComponent(bundleVersion)}` : "";
	      window.location.href = `/api/gateway/bundles/${encodeURIComponent(bundleId)}/download${qs}`;
	    }

	    async function workflowUsage(bundleId, bundleVersion, row) {
	      // Honest impact: the run store has no COUNT operation, so this reports
	      // what a bounded page can PROVE and says so. It never presents a page
	      // size as a total — a confident wrong number is what turns a refusal
	      // into a confirmation.
	      const ids = [];
	      const versions = bundleVersion
	        ? (row ? row.versions.filter((v) => String(v.bundle_version) === String(bundleVersion)) : [])
	        : (row ? row.versions : []);
	      for (const v of versions) for (const ep of v.entrypoints || []) if (ep.workflow_id) ids.push(ep.workflow_id);
	      if (!ids.length && bundleVersion) return null;
	      let seen = 0; let capped = false;
	      const limit = 200;
	      for (const wid of ids.slice(0, 12)) {
	        try {
	          const res = await api(`/api/gateway/runs?workflow_id=${encodeURIComponent(wid)}&limit=${limit}`);
	          const items = res.items || res.runs || [];
	          seen += items.length;
	          if (res.has_more || items.length >= limit) capped = true;
	        } catch (err) { capped = true; }
	      }
	      return { seen, capped };
	    }

	    // Delete asks INLINE, in the row (round-2 polish): the sentence names what goes, the run
	    // count (or its floor), and the way back (export first); Delete / Cancel sit beside it.
	    // anchor = the bundle's <tr> (whole bundle) or the version box in the expanded row.
	    // A shipped bundle lives in the gateway's own flows folder (the default registry, config.py
	    // _default_flows_dir); nothing copies it back at start, so the confirm says the file stays
	    // gone until the gateway is reinstalled (routes/gateway.py remove_bundle unlinks it).
	    const WORKFLOW_SHIPPED_DELETE = " It ships with the gateway: nothing puts it back at the next restart, only reinstalling the gateway does.";
	    function workflowDeleteSentence(label, bundleVersion, usage, shipped = false) {
	      const scope = bundleVersion ? "This version is removed from disk" : "Every version of this workflow is removed from disk";
	      let runs = "";
	      if (usage && usage.seen) {
	        runs = usage.capped
	          ? ` At least ${usage.seen} run${usage.seen === 1 ? "" : "s"} reference it (the run store cannot count exactly); they keep their records but can no longer be replayed or resumed.`
	          : ` ${usage.seen} run${usage.seen === 1 ? "" : "s"} reference it; they keep their records but can no longer be replayed or resumed.`;
	      } else if (usage) {
	        runs = " No runs reference it in the pages checked.";
	      }
	      return `Delete ${label}? ${scope}; there is no undo.${shipped ? WORKFLOW_SHIPPED_DELETE : ""}${runs} Export it first if you may need it again.`;
	    }
	    function workflowConfirmInline(anchor, text, onConfirm) {
	      for (const old of Array.from(document.querySelectorAll("#tab-workflows .workflows-confirm"))) old.remove();
	      const box = document.createElement("div");
	      box.className = "inline-confirm workflows-confirm-box";
	      box.setAttribute("role", "group");
	      const span = document.createElement("span");
	      span.textContent = text;
	      const yes = document.createElement("button");
	      yes.type = "button"; yes.className = "danger"; yes.textContent = "Delete";
	      const no = document.createElement("button");
	      no.type = "button"; no.className = "secondary"; no.textContent = "Cancel";
	      box.append(span, yes, no);
	      let holder;
	      if (anchor.tagName === "TR") {
	        holder = document.createElement("tr");
	        holder.className = "workflows-confirm";
	        const td = document.createElement("td");
	        td.colSpan = 6;
	        td.append(box);
	        holder.append(td);
	        const next = anchor.nextElementSibling;
	        const after = next && next.classList.contains("workflows-detail") ? next : anchor;
	        after.after(holder);
	      } else {
	        holder = box;
	        holder.classList.add("workflows-confirm");
	        anchor.append(holder);
	      }
	      holder.onclick = (ev) => ev.stopPropagation();
	      no.onclick = () => holder.remove();
	      yes.onclick = async () => {
	        yes.disabled = true; no.disabled = true;
	        yes.setAttribute("aria-busy", "true");
	        await onConfirm();
	        holder.remove();
	      };
	      try { no.focus(); } catch {}
	      return holder;
	    }
	    async function deleteWorkflow(bundleId, bundleVersion, row, anchor) {
	      if (!anchor) throw new Error("deleteWorkflow needs the row (or version box) that asks inline.");
	      const label = bundleVersion ? `${bundleId}@${bundleVersion}` : bundleId;
	      let usage = null;
	      try { usage = await workflowUsage(bundleId, bundleVersion, row); } catch (err) { usage = null; }
	      const shipped = Boolean(row && (row.versions || []).some((v) => v && v.source === "shipped"));
	      workflowConfirmInline(anchor, workflowDeleteSentence(label, bundleVersion, usage, shipped), async () => {
	        $("workflows-message").textContent = `Deleting ${label}…`;
	        $("workflows-message").className = "message";
	        try {
	          const qs = bundleVersion ? `?bundle_version=${encodeURIComponent(bundleVersion)}&reload=true` : "?reload=true";
	          const res = await api(`/api/gateway/bundles/${encodeURIComponent(bundleId)}${qs}`, { method: "DELETE" });
	          $("workflows-message").textContent = `Removed ${res.removed} file(s) for ${label}.`;
	          $("workflows-message").className = "message ok";
	          if (!bundleVersion) state.workflowsExpanded.delete(bundleId);
	          await loadWorkflows();
	        } catch (err) {
	          $("workflows-message").textContent = `Not deleted: ${String(err.message || err)}`;
	          $("workflows-message").className = "message error";
	        }
	      });
	    }

	    async function importWorkflows(files) {
	      const list = [...(files || [])];
	      if (!list.length) return;
	      const done = []; const failed = []; const notLoaded = [];
	      for (const file of list) {
	        $("workflows-message").textContent = `Importing ${file.name}…`;
	        $("workflows-message").className = "message";
	        const form = new FormData();
	        form.append("file", file);
	        form.append("overwrite", "false");
	        form.append("reload", "true");
	        try {
	          const res = await api("/api/gateway/bundles/upload", { method: "POST", body: form });
	          // `loaded` is the honest field: the file can land and still not be
	          // servable. Reporting it as installed would repeat the defect this
	          // whole surface exists to remove.
	          if (res.loaded === false) {
	            notLoaded.push(`${res.bundle_ref}: ${(res.skipped && res.skipped.reason) || "not served"}`);
	          } else {
	            done.push(res.bundle_ref);
	          }
	        } catch (err) {
	          failed.push(`${file.name}: ${String(err.message || err)}`);
	        }
	      }
	      const parts = [];
	      if (done.length) parts.push(`Installed ${done.join(", ")}.`);
	      if (notLoaded.length) parts.push(`Installed but NOT running — ${notLoaded.join("; ")}.`);
	      if (failed.length) parts.push(`Failed — ${failed.join("; ")}.`);
	      $("workflows-message").textContent = parts.join(" ") || "Nothing to import.";
	      $("workflows-message").className = failed.length ? "message error" : (notLoaded.length ? "message" : "message ok");
	      await loadWorkflows();
	    }

	    // ---- Summoned Entities: full create + lifecycle management ----
	    // Server-rendered console surface (NOT a React app), so it consumes the
	    // gateway's own served payloads directly — templates, tool inventory,
	    // per-phase capability matrix, substrate, prompt, state, loop, reembed —
	    // the same server truth uic's React matrix renders, a different target.
	    // Every user value goes through textContent (XSS-safe) and every write
	    // through api() (CSRF). No client-side re-derivation of server truth.
	    state.entityTemplates = [];
	    state.entityMatrixSpec = null;   // /inventory/capability-matrix (defaults, for create)
	    state.manageName = "";           // entity currently open in the manage panel

	    // ONE matrix renderer: a phase×tool checkbox grid. `tools` = [{id,label,description}];
	    // `grantByPhase` = {phaseId: Set(toolId)} initial checked state. The baseline
	    // is stashed on the container so saves send only CHANGED phases (never
	    // materialize the day's defaults as the operator's word — write_policy_file's
	    // documented anti-pattern), and a phase cleared to empty reverts to the
	    // framework default (null), not a silent deny-all (uic c727 ask 2 fold).
	    function renderMatrix(container, phaseIds, phaseLabels, tools, grantByPhase) {
	      const baseline = {};
	      for (const pid of phaseIds) baseline[pid] = [...(grantByPhase[pid] || new Set())].sort();
	      container._matrixBaseline = baseline;
	      container.textContent = "";
	      const table = document.createElement("table");
	      table.className = "entity-matrix-table";
	      const thead = document.createElement("thead");
	      const hrow = document.createElement("tr");
	      const corner = document.createElement("th");
	      corner.textContent = "Tool";
	      hrow.append(corner);
	      for (const pid of phaseIds) {
	        const th = document.createElement("th");
	        th.textContent = phaseLabels[pid] || pid;
	        hrow.append(th);
	      }
	      thead.append(hrow);
	      table.append(thead);
	      const tbody = document.createElement("tbody");
	      for (const tool of tools) {
	        const tr = document.createElement("tr");
	        const th = document.createElement("th");
	        th.textContent = tool.label || tool.id;
	        if (tool.description) th.title = tool.description;
	        tr.append(th);
	        for (const pid of phaseIds) {
	          const td = document.createElement("td");
	          const box = document.createElement("input");
	          box.type = "checkbox";
	          box.dataset.phase = pid;
	          box.dataset.tool = tool.id;
	          box.checked = Boolean(grantByPhase[pid] && grantByPhase[pid].has(tool.id));
	          td.append(box);
	          tr.append(td);
	        }
	        tbody.append(tr);
	      }
	      table.append(tbody);
	      container.append(table);
	    }
	    // Read the grid back into a MERGE policy {phaseId: [toolId...] | null}, sending
	    // ONLY phases the operator actually changed from the rendered baseline. A phase
	    // left at its baseline is omitted (untouched server-side). A phase the operator
	    // cleared to empty is sent as null — revert to the framework default — never []
	    // (which would be a silent deny-all). An explicit non-empty change is the word.
	    function readMatrix(container) {
	      const current = {};
	      for (const box of container.querySelectorAll("input[type=checkbox]")) {
	        const pid = box.dataset.phase;
	        if (!current[pid]) current[pid] = [];
	        if (box.checked) current[pid].push(box.dataset.tool);
	      }
	      const baseline = container._matrixBaseline || {};
	      const policy = {};
	      for (const pid of Object.keys(current)) {
	        const now = current[pid].slice().sort();
	        const was = baseline[pid] || [];
	        if (now.length === was.length && now.every((t, i) => t === was[i])) continue; // unchanged → omit
	        policy[pid] = now.length ? now : null; // cleared → revert to default (null), not deny-all
	      }
	      return policy;
	    }
	    // Build the render inputs from the capability-matrix payload (create lane).
	    function matrixFromSpec(spec) {
	      const phaseIds = (spec.phases || []).map((p) => p.id);
	      const phaseLabels = {};
	      for (const p of (spec.phases || [])) phaseLabels[p.id] = p.label || p.id;
	      const section = (spec.sections || []).find((s) => s.id === "tools") || (spec.sections || [])[0] || { items: [] };
	      const tools = (section.items || []).map((it) => ({ id: it.id, label: it.label || it.id, description: it.description || "" }));
	      const grantByPhase = {};
	      for (const pid of phaseIds) {
	        grantByPhase[pid] = new Set();
	        for (const it of (section.items || [])) {
	          const cell = (it.cells || {})[pid] || {};
	          if (cell.resolved_value) grantByPhase[pid].add(it.id);
	        }
	      }
	      return { phaseIds, phaseLabels, tools, grantByPhase };
	    }

	    // ---- Shared provider/model/embedding dropdown data ----
	    // The console consumes the SAME gateway endpoints the
	    // React kit picker does (/discovery/providers, /discovery/providers/
	    // {p}/models, /entities/creation-defaults) — the endpoints ARE the
	    // shared contract; a vanilla-JS console cannot import a React component.
	    // "Gateway default" is an explicit OPTION (value ""), never a blank the
	    // operator must guess to leave empty. Degraded discovery labels itself.
	    function _fillSelect(sel, options, { keep = "" } = {}) {
	      if (!sel) return;
	      const prev = keep || sel.value || "";
	      sel.textContent = "";
	      for (const o of options) {
	        const opt = document.createElement("option");
	        opt.value = o.value;
	        opt.textContent = o.label;
	        sel.append(opt);
	      }
	      // Restore the prior selection if it survived the refresh.
	      if (prev && options.some((o) => o.value === prev)) sel.value = prev;
	    }
	    async function loadSubstrateDropdowns() {
	      const provSel = $("entity-new-provider");
	      const embSel = $("entity-new-embedding");
	      const note = $("entity-new-substrate-note");
	      let defaults = { substrate: {}, embedding: {}, warnings: [] };
	      try { defaults = await api("/api/gateway/entities/creation-defaults"); } catch (e) { /* labeled below */ }
	      state.creationDefaults = defaults;
	      const sd = defaults.substrate || {};
	      const ed = defaults.embedding || {};
	      const defProvLabel = (sd.provider && sd.model) ? `Gateway default (${sd.provider} / ${sd.model})` : "Gateway default";
	      const defEmbLabel = (ed.provider && ed.model) ? `Gateway default (${ed.model})` : "Gateway default";
	      // Providers dropdown from discovery; "Gateway default" names the resolved default.
	      let provOpts = [{ value: "", label: defProvLabel }];
	      let degraded = "";
	      try {
	        const disc = await api("/api/gateway/discovery/providers");
	        const items = Array.isArray(disc.items) ? disc.items : [];
	        for (const p of items) {
	          const nm = String(p.name || "").trim();
	          if (nm) provOpts.push({ value: nm, label: nm });
	        }
	        if (!items.length) degraded = "provider discovery returned no providers";
	      } catch (e) { degraded = "provider discovery unavailable: " + (e.message || e); }
	      _fillSelect(provSel, provOpts);
	      // Embedding dropdown: gateway default + discovered embedding models
	      // for the default embedding provider (output_type=embeddings filter).
	      let embOpts = [{ value: "", label: defEmbLabel }];
	      if (ed.provider) {
	        try {
	          const em = await api(`/api/gateway/discovery/providers/${encodeURIComponent(ed.provider)}/models?output_type=embeddings`);
	          for (const m of (Array.isArray(em.models) ? em.models : [])) {
	            const mid = String(m).trim();
	            if (mid) embOpts.push({ value: mid, label: mid });
	          }
	        } catch (e) { /* the default option still works; free choice degrades quietly */ }
	      }
      _fillSelect(embSel, embOpts);
      // The birth pin must be a model the door can SERVE (no silent mixing);
      // a non-default choice refuses unless the gateway route is changed
      // first, and validate() catches it pre-confirm. Warn on selection.
      state.defaultEmbedding = (ed.model || "");
      if (embSel) embSel.onchange = () => {
        const v = (embSel.value || "").trim();
        if (v && v !== state.defaultEmbedding) {
          _entOut("entity-new-substrate-note", "#FALLBACK embedding " + v + " differs from the gateway's resolved embedder (" + (state.defaultEmbedding || "none") + ") — set it as the gateway embedding route (Multimodal Capabilities) first, or the home refuses vector ops. Validate will catch this before the name is burned.");
        } else if (note) { note.textContent = ""; }
      };
      // Model dropdown follows the provider selection (cascade); until a
      // provider is picked it stays on Gateway default, disabled.
      await loadModelsForProvider(provSel ? provSel.value : "");
      const bits = [];
      if (degraded) bits.push("#FALLBACK " + degraded + " — leave on Gateway default, or configure a provider in the Providers tab");
      for (const w of (defaults.warnings || [])) bits.push(String(w));
      if (note && bits.length) note.textContent = bits.join(" · ");
    }
	    async function loadModelsForProvider(provider) {
	      const modelSel = $("entity-new-model");
	      if (!modelSel) return;
	      const p = String(provider || "").trim();
	      if (!p) {
	        // No provider chosen = Gateway default; the model rides the default too.
	        _fillSelect(modelSel, [{ value: "", label: "Gateway default" }]);
	        modelSel.disabled = true;
	        return;
	      }
	      modelSel.disabled = false;
	      let opts = [{ value: "", label: "Provider default" }];
	      try {
	        const m = await api(`/api/gateway/discovery/providers/${encodeURIComponent(p)}/models?output_type=text`);
	        for (const mid of (Array.isArray(m.models) ? m.models : [])) {
	          const s = String(mid).trim();
	          if (s) opts.push({ value: s, label: s });
	        }
	      } catch (e) { opts.push({ value: "", label: "(models unavailable — set on manage)" }); }
	      _fillSelect(modelSel, opts);
	    }

	    function _fillTemplateSelect(sel) {
	      if (!sel) return;
	      const prior = sel.value;
	      sel.textContent = "";
	      for (const t of state.entityTemplates) {
	        const opt = document.createElement("option");
	        opt.value = t.id;
	        opt.textContent = t.name || t.id;
	        sel.append(opt);
	      }
	      if (prior && state.entityTemplates.some((t) => t.id === prior)) sel.value = prior;
	    }
	    async function loadEntities() {
	      try {
	        if (!state.entityTemplates.length) {
	          const g = await api("/api/gateway/entities/templates");
	          state.entityTemplates = Array.isArray(g.templates) ? g.templates : [];
	          _fillTemplateSelect($("entity-template"));
	          _fillTemplateSelect($("tpl-select"));
	          renderEntityTemplateDesc();
	          renderTplSelectState();
	        }
	        // Populate the substrate/embedding dropdowns from the gateway (once
	        // per session; a provider cascade refreshes models on change).
	        if (!state.creationDefaults) { try { await loadSubstrateDropdowns(); } catch (e) { /* additive */ } }
	        if (!state.entityMatrixSpec) {
	          try {
	            state.entityMatrixSpec = await api("/api/gateway/entities/inventory/capability-matrix");
	            const m = matrixFromSpec(state.entityMatrixSpec);
	            renderMatrix($("entity-new-matrix"), m.phaseIds, m.phaseLabels, m.tools, m.grantByPhase);
	          } catch (e) {
	            $("entity-new-matrix").textContent = "capability matrix unavailable: " + (e.message || e);
	          }
	        }
	        tableLoadingRow($("entities-table"), 5, "Loading the roster…");
	        const listed = await api("/api/gateway/entities");
	        const rows = Array.isArray(listed.entities) ? listed.entities : [];
	        const body = $("entities-table");
	        body.textContent = "";
	        // Success clears prior error debris (a
	        // pre-login 401 note sat under 4 healthy rows — data and an error
	        // claim coexisting is the worst kind of stale pixel).
	        $("entities-message").textContent = "";
	        $("entities-message").className = "message";
	        for (const e of rows) {
	          const tr = document.createElement("tr");
	          if (e.error) {
	            // The API deliberately surfaces unreadable/collided homes so the
	            // operator can fix them — render the error, don't fake a healthy row.
	            const td = document.createElement("td");
	            td.colSpan = 5;
	            td.className = "message";
	            td.textContent = `${e.slug || e.name || "?"}: ${e.error}`;
	            tr.append(td);
	            body.append(tr);
	            continue;
	          }
	          const st = (e.state && typeof e.state === "object") ? (e.state.state || "awake") : (e.state || "awake");
	          const stWarnings = (e.state && typeof e.state === "object" && Array.isArray(e.state.warnings)) ? e.state.warnings : [];
	          const nm = e.name || e.slug || "";
	          // Entity ID = the HANDLE (<name>@<gateway lan ip>);
	          // the manifest string is an internal birth
	          // marker and renders only as a fallback when no address exists.
	          for (const cell of [nm, e.handle || e.entity_id || "", String(e.created_at || e.born_at || "").slice(0, 16).replace("T", " ")]) {
	            const td = document.createElement("td");
	            td.textContent = String(cell);
	            tr.append(td);
	          }
	          // State cell: id'd so the live poll repaints the managed row from
	          // the same /cognition read; an unreadable state renders its
	          // warning instead of faking a clean "awake".
	          // Badge classes give the list the color-coded read without
	          // opening Manage: asleep=sleep tone,
	          // stopped=danger tone (the served liveness axis), awake=neutral.
	          const stopped = (e.state && typeof e.state === "object" && e.state.liveness === "stopped");
	          const stTd = document.createElement("td");
	          stTd.id = `entity-state-cell-${nm}`;
	          const stBadge = document.createElement("span");
	          const stTone = stopped ? "phase-stopped" : "phase-sleep";
	          stBadge.className = "entity-live-badge " + stTone;
	          stBadge.textContent = stopped ? "STOPPED" : (st === "awake" ? "resting" : String(st));
	          stTd.append(stBadge);
	          if (stWarnings.length) {
	            const warn = document.createElement("span");
	            warn.className = "entity-warn-pill";
	            warn.innerHTML = `<span class="chip-icon" aria-hidden="true">${ICONS.warn}</span>state unreadable — showing resting`;
	            stTd.append(warn);
	          }
	          tr.append(stTd);
	          const actions = document.createElement("td");
	          // Talk first: talking to an entity is the
	          // most-used humane action and was four interactions deep.
	          const talkBtn = document.createElement("button");
	          talkBtn.className = "secondary";
	          talkBtn.textContent = "Talk";
	          talkBtn.onclick = async () => { await openEntityManage(nm); setEntitySubtab("talk"); };
	          actions.append(talkBtn);
	          const manageBtn = document.createElement("button");
	          manageBtn.className = "secondary";
	          manageBtn.textContent = "Manage";
	          manageBtn.onclick = () => openEntityManage(nm);
	          actions.append(manageBtn);
	          tr.append(actions);
	          body.append(tr);
	        }
	        if (!rows.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 5;
	          td.className = "section-note";
	          td.textContent = "No entities yet — use Summon entity to create the first one.";
	          tr.append(td);
	          body.append(tr);
	        }
	      } catch (err) {
	        // Stale rows under a detached error read as health — replace them
	        // with ONE labeled failure row (a duplicate
	        // entities-message write once doubled the error on screen).
	        const body = $("entities-table");
	        if (body) {
	          body.textContent = "";
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 5;
	          td.className = "message error";
	          td.textContent = "entity list unavailable: " + String(err.message || err);
	          tr.append(td);
	          body.append(tr);
	        }
	      }
	    }
    function renderTplSelectState() {
      // Edit is offered only for editable (operator) templates; the
      // builtin floor + frozen legacy files show View + New only.
      const id = $("tpl-select") ? $("tpl-select").value : "";
      const t = state.entityTemplates.find((x) => x.id === id);
      const editBtn = $("tpl-edit");
      if (editBtn) editBtn.classList.toggle("hidden", !(t && t.editable));
      // A template switch closes a stale editor (it belonged to another id).
      const ed = $("tpl-editor"); if (ed) ed.classList.add("hidden");
      state.tplMode = "";
    }
    function renderEntityTemplateDesc() {
      const id = $("entity-template").value;
      const t = state.entityTemplates.find((x) => x.id === id);
      const ver = (t && t.version) ? ` (v${t.version})` : "";
      $("entity-template-desc").textContent = t ? ((t.description || "") + ver) : "";
      const chips = $("entity-template-values");
	      chips.textContent = "";
	      const cores = (t && Array.isArray(t.core_values)) ? t.core_values : [];
	      for (const cv of cores) {
	        const chip = document.createElement("span");
	        chip.className = "entity-chip entity-chip-locked";
	        chip.innerHTML = `<span class="chip-icon" aria-hidden="true">${ICONS.lock}</span>${esc(cv)}`;
	        chip.title = "core value — kept for life, cannot be removed";
	        chips.append(chip);
	      }
	    }
	    // ---- Template management: view / edit / create, versioned.
	    // A template is a JSON spark blueprint; the
	    // server lints it + writes a new version per save. The builtin floor
	    // is view-only (seed a new id from it). tplMode ∈ view|edit|new.
	    // Lives in its OWN modal (templates-backdrop) with its own select —
	    // blueprint management is not a creation question (operator 12:24).
	    function _selectedTemplate() {
	      const id = $("tpl-select").value;
	      return state.entityTemplates.find((x) => x.id === id) || null;
	    }
	    function tplShowEditor(mode) {
	      state.tplMode = mode;
	      const t = _selectedTemplate();
	      const editor = $("tpl-editor");
	      const idRow = $("tpl-id-row");
	      $("tpl-out").textContent = "";
	      $("tpl-versions").textContent = "";
	      if (!t) { $("tpl-out").textContent = "Pick a template first."; return; }
	      editor.classList.remove("hidden");
	      idRow.classList.toggle("hidden", mode !== "new");
	      const editable = mode === "edit";
	      $("tpl-spark").disabled = false; // JSON is always editable in new/edit; view = readonly
	      if (mode === "view") $("tpl-spark").disabled = true;
	      $("tpl-save").classList.toggle("hidden", mode === "view");
	      $("tpl-name").value = mode === "new" ? "" : (t.name || "");
	      $("tpl-desc").value = mode === "new" ? "" : (t.description || "");
	      $("tpl-id").value = "";
	      try { $("tpl-spark").value = JSON.stringify(t.spark || {}, null, 2); }
	      catch (e) { $("tpl-spark").value = "{}"; }
	      // Version history (operator: every template versioned).
	      if (t.source === "operator" && mode !== "new") {
	        api(`/api/gateway/entities/templates/${encodeURIComponent(t.id)}/versions`).then((v) => {
	          const vs = Array.isArray(v.versions) ? v.versions : [];
	          $("tpl-versions").textContent = vs.length
	            ? "versions: " + vs.map((x) => `v${x.version}${x.note ? " (" + x.note + ")" : ""}`).join(" · ")
	            : "";
	        }).catch(() => {});
	      }
	      void editable;
	    }
	    async function tplSave() {
	      const mode = state.tplMode;
	      const t = _selectedTemplate();
	      let spark;
	      try { spark = JSON.parse($("tpl-spark").value || "{}"); }
	      catch (e) { $("tpl-out").textContent = "Spark is not valid JSON: " + (e.message || e); return; }
	      const name = ($("tpl-name").value || "").trim();
	      const description = ($("tpl-desc").value || "").trim();
	      $("tpl-save").disabled = true;
	      try {
	        let saved;
	        if (mode === "new") {
	          const id = ($("tpl-id").value || "").trim();
	          if (!id) { $("tpl-out").textContent = "A new template needs an id."; return; }
	          saved = await api("/api/gateway/entities/templates", {
	            method: "POST", body: JSON.stringify({ id, spark, name, description, note: "created via console" }),
	          });
	        } else {
	          if (!t || t.source !== "operator") { $("tpl-out").textContent = "Only operator templates can be edited (the builtin is the floor — use New to seed one)."; return; }
	          saved = await api(`/api/gateway/entities/templates/${encodeURIComponent(t.id)}`, {
	            method: "PUT", body: JSON.stringify({ id: t.id, spark, name, description, note: "edited via console" }),
	          });
	        }
	        let out = `Saved ${saved.id} v${saved.version}.`;
	        if (Array.isArray(saved.lint_warnings) && saved.lint_warnings.length) out += " Warnings: " + saved.lint_warnings.join(" | ");
	        $("tpl-out").textContent = out;
	        // Refresh the gallery so BOTH pickers reflect the new version.
	        state.entityTemplates = [];
	        await loadEntities();
	        $("tpl-select").value = saved.id;
	        renderTplSelectState();
	        renderEntityTemplateDesc();
	      } catch (e) {
	        $("tpl-out").textContent = "Save failed: " + ((e.detail && e.detail.message) || e.message || e);
	      } finally { $("tpl-save").disabled = false; }
	    }

	    async function createEntity() {
	      const msg = $("entity-create-message");
	      const name = ($("entity-name").value || "").trim();
	      const id = $("entity-template").value;
	      const template = state.entityTemplates.find((x) => x.id === id);
	      if (!name) { msg.textContent = "Name is required."; return; }
	      if (!template) { msg.textContent = "Pick a template."; return; }
	      const spark = { ...(template.spark || {}), name };
	      const provider = ($("entity-new-provider").value || "").trim();
	      const model = ($("entity-new-model").value || "").trim();
	      const embedding = ($("entity-new-embedding").value || "").trim();
	      const createBody = { name, spark };
	      if (embedding) createBody.embedding_model = embedding;
	      const enc = encodeURIComponent(name);
	      $("entity-create").disabled = true;
	      try {
	        // DRY-RUN first — never burn the permanent name on a refusing spark.
	        const check = await api(`/api/gateway/entities/${enc}/validate`, {
	          method: "POST", body: JSON.stringify(createBody),
	        });
	        if (!check.ok) {
	          msg.textContent = "Cannot create: " + ((check.errors || []).join("; ") || (check.would_conflict ? "an entity with this name already exists with a different spark" : "validation failed"));
	          return;
	        }
	        // The name is PERMANENT — there is no delete (spark v1-for-life).
	        // Confirm the one irreversible act before writing anything. The
	        // dry-run's WARNINGS ride the confirm (card 015 staging: the
	        // review happens BEFORE the birth — surfacing them only in the
	        // post-create note was reviewing after the irreversible act).
	        const vWarnPre = Array.isArray(check.warnings) ? check.warnings.filter(Boolean) : [];
	        const warnLine = vWarnPre.length ? "\\n\\nValidation warnings (review before summoning):\\n• " + vWarnPre.join("\\n• ") : "";
	        const go = await confirmAction({
	          title: `Summon ${name}?`,
	          message: `This creates a permanent entity named "${name}". There is no delete — the name and its home are for life. Its spark's core values are locked. Substrate and per-phase capabilities can be changed later.${warnLine}`,
	          confirmLabel: "Summon",
	        });
	        if (!go) { msg.textContent = "Cancelled."; return; }
	        const created = await api("/api/gateway/entities", { method: "POST", body: JSON.stringify(createBody) });
	        let note = created.created === false ? `${name} already existed (identical spark).` : `Summoned ${name}.`;
	        const vWarn = Array.isArray(check.warnings) ? check.warnings.filter(Boolean) : [];
	        if (vWarn.length) note += ` Warnings: ${vWarn.join(" | ")}`;
	        // Apply optional substrate (admin) — surface its own error, don't lose the create.
	        if (provider && model) {
	          try {
	            const subBody = { provider, model };
	            const newThinking = ($("entity-new-thinking")?.value || "").trim();
	            if (newThinking) subBody.thinking = newThinking;
	            await api(`/api/gateway/entities/${enc}/substrate`, { method: "PUT", body: JSON.stringify(subBody) });
	            note += " Substrate set.";
	          } catch (e) { note += " (substrate not set: " + (e.message || e) + ")"; }
	        } else if (provider || model) {
	          note += " (substrate needs BOTH provider and model — skipped)";
	        } else if (($("entity-new-thinking")?.value || "").trim()) {
	          note += " (reasoning effort needs a provider and model chosen — not applied)";
	        }
	        // Apply the capability matrix only if the operator opened Advanced AND
	        // moved a cell off its default (readMatrix returns only changed phases).
	        const adv = $("entity-advanced");
	        if (adv && adv.open && state.entityMatrixSpec) {
	          const policy = readMatrix($("entity-new-matrix"));
	          if (Object.keys(policy).length) {
	            try {
	              await api(`/api/gateway/entities/${enc}/tool-policy`, { method: "PUT", body: JSON.stringify({ policy }) });
	              note += " Capabilities set.";
	            } catch (e) { note += " (capabilities not set: " + (e.message || e) + ")"; }
	          }
	        }
	        msg.textContent = note;
	        $("entity-name").value = "";
	        // Reset the create matrix so one entity's toggles never bleed into the
	        // next create (the cached spec is re-rendered, restoring defaults).
	        if (state.entityMatrixSpec) {
	          const m = matrixFromSpec(state.entityMatrixSpec);
	          renderMatrix($("entity-new-matrix"), m.phaseIds, m.phaseLabels, m.tools, m.grantByPhase);
	        }
	        if (adv) adv.open = false;
	        await loadEntities();
	        // The birth succeeded: close the modal and land the note where the
	        // roster is (the modal is gone — a message inside it would vanish).
	        closeEntityCreate();
	        $("entities-message").textContent = note;
	        $("entities-message").className = "message ok";  // a birth is good news, not debris
	        // Admins see the new entity as an Accounts row (DESIGN-v2 §2.1): same note there.
	        if (state.principal) { await refreshUsersOnly(); usersMessage(note, "ok"); }
	      } catch (err) {
	        msg.textContent = String(err.message || err);
	      } finally {
	        $("entity-create").disabled = false;
	      }
	    }
	    // ---- Creation + template modals (progressive disclosure: the page
	    // shows LISTS; questions appear when the operator asks to create) ----
	    function openEntityCreate() {
	      $("entity-create-message").textContent = "";
	      $("entity-create-backdrop").classList.remove("hidden");
	      $("entity-name").focus();
	    }
	    function closeEntityCreate() {
	      $("entity-create-backdrop").classList.add("hidden");
	    }
	    function openTemplates() {
	      _fillTemplateSelect($("tpl-select"));
	      renderTplSelectState();
	      $("templates-backdrop").classList.remove("hidden");
	    }
	    function closeTemplates() {
	      $("templates-backdrop").classList.add("hidden");
	    }
	    // With user accounts OFF the gateway refuses a non-admin account (409
	    // `user_accounts_off_admin_only`, routes/gateway.py) and such an account
	    // could never sign in. `/me` reports the same switch as
	    // `auth.user_auth_enabled`; without that field the modal says so and
	    // keeps every role (the server's refusal is then shown word for word).
	    function userAccountsOn() {
	      const flag = state.meAuth ? state.meAuth.user_auth_enabled : undefined;
	      return typeof flag === "boolean" ? flag : null;
	    }
	    function applyUserAccountsMode() {
	      const on = userAccountsOn();
	      const select = $("new-roles");
	      const note = $("new-roles-note");
	      for (const opt of Array.from(select.options || [])) {
	        const off = on === false && opt.value !== "admin";
	        opt.hidden = off;
	        opt.disabled = off;
	      }
	      if (on === false) select.value = "admin";
	      else if (select.dataset.accountsOff === "1") select.value = "user";
	      select.dataset.accountsOff = on === false ? "1" : "0";
	      if (on === false) {
	        note.textContent = "User accounts are off on this gateway: only admin accounts can sign in. Turn user accounts on to add members.";
	        note.className = "message warn";
	      } else if (on === null) {
	        note.textContent = "This gateway did not say whether user accounts are on, so every role is offered. If they are off, it refuses any role but admin.";
	        note.className = "message error";
	        console.error("AbstractGateway console: /api/gateway/me carries no auth.user_auth_enabled; the create-user modal cannot tell whether user accounts are on.");
	      } else {
	        note.textContent = "";
	        note.className = "message hidden";
	      }
	    }
	    function openUserCreate() {
	      $("user-create-message").textContent = "";
	      applyUserAccountsMode();
	      $("user-create-form").classList.remove("hidden");
	      $("user-create-done").classList.add("hidden");
	      $("user-create-backdrop").classList.remove("hidden");
	      $("new-user").focus();
	    }
	    function closeUserCreate() {
	      $("user-create-backdrop").classList.add("hidden");
	    }

	    // ---- Manage an existing entity ----
	    const ENTITY_SUBTABS = ["overview", "talk", "lifecycle", "substrate", "tools", "prompt"];
	    // Entity CONFIG is admin-gated at the server (substrate/tool-policy/prompt/
	    // state/loop/reembed). Reflect that in the UI so a non-admin sees why a
	    // button would 403 instead of clicking into a refusal. Create + all GETs
	    // stay user-level, so viewing an entity's config is allowed for everyone.
	    const ENTITY_ADMIN_CONTROLS = [
	      "entity-advanced", "entity-state-awake", "entity-state-asleep", "entity-stop", "entity-restore",
	      "entity-owntime-toggle", "entity-loop-freeze", "entity-substrate-save",
	      "entity-voice-save", "entity-voice-clear",
	      "entity-workorder-save", "entity-workorder-clear",
	      "entity-tools-save", "entity-tools-denyall-row", "entity-prompt-save", "entity-reembed",
	    ];
	    function applyEntityAdminGating() {
	      const admin = Boolean(state.principal && state.principal.admin);
	      for (const id of ENTITY_ADMIN_CONTROLS) {
	        const el = $(id);
	        if (el) el.classList.toggle("hidden", !admin);
	      }
	      const banner = $("entity-admin-note");
	      if (banner) banner.classList.toggle("hidden", admin);
	      const createNote = $("entity-create-admin-note");
	      if (createNote) createNote.classList.toggle("hidden", admin);
	    }
	    function setEntitySubtab(name) {
	      for (const id of ENTITY_SUBTABS) {
	        const btn = $(`entity-subtab-${id}`);
	        const panel = $(`entity-subpanel-${id}`);
	        if (btn) btn.classList.toggle("active", id === name);
	        if (panel) panel.classList.toggle("hidden", id !== name);
	      }
	    }
	    function _resetChatUi() {
	      // Chat state is per-entity: leaving it across manage opens routed
	      // words to the WRONG entity under the right header.
	      state.chatId = "";
	      state.chatEntity = "";
	      const t = $("entity-chat-transcript"); if (t) t.textContent = "";
	      _entOut("entity-chat-status", "");
	      chatUiState();
	    }
	    function closeEntityManage() {
	      state.manageName = "";
	      state.manageToken = (state.manageToken || 0) + 1; // stops the live poll
	      _resetChatUi();
      $("entity-manage-section").classList.add("hidden");
      // Drill-out: restore the sections the drill-in hid (users stays
      // admin-gated — renderAccount owns its visibility, re-applied here).
      // Admins see entities as rows of the Accounts table (DESIGN-v2 §2.1); the roster panel is the non-admin view.
      $("entities-list-section").classList.add("hidden");
      if (state.principal) $("users-section").classList.remove("hidden");
	    }
	    async function openEntityManage(name) {
	      state.manageName = name;
	      // Generation token: five loaders write into SHARED DOM
	      // nodes. Opening B while A's slower fetch is in flight must not let
	      // A's late response paint A's grants/baseline under B's header — the
	      // next save would silently write A's word into B's permanent home.
	      // Every loader passes the token; every DOM write checks it first.
	      state.manageToken = (state.manageToken || 0) + 1;
	      const token = state.manageToken;
	      _resetChatUi();
	      $("entity-manage-name").textContent = name;
      $("entity-manage-section").classList.remove("hidden");
      // Drill-in: the manage panel replaces the tab's
      // list/users sections instead of appending below them — the scroll
      // hunt was the complaint the old scrollIntoView bandaged.
      $("entities-list-section").classList.add("hidden");
      $("users-section").classList.add("hidden");
	      applyEntityAdminGating();
	      setEntitySubtab("overview");
	      try { $("entity-manage-section").scrollIntoView({ behavior: "smooth", block: "start" }); } catch {}
	      try {
	        await Promise.all([
	          loadEntityOverview(name, token), loadEntitySubstrate(name, token),
	          loadEntityVoice(name, token),
	          loadEntityToolPolicy(name, token), loadEntityWorkOrder(name, token),
	          loadEntityCandidates(name, token),
	          loadEntityPrompt(name, token),
	          loadEntityEmbedding(name, token), refreshEntityLive(name, token),
	        ]);
	      } catch (e) {
	        $("entities-message").textContent = String(e.message || e);
	      }
	      _scheduleLivePoll(name, token);
	    }
	    function _scheduleLivePoll(name, token) {
	      // Live truth poll (with no polling, the page showed own-time
	      // OFF while the process was alive). Scoped
	      // to an OPEN manage panel: the token bump on close/switch stops the
	      // chain, so no timer outlives its panel.
	      if (typeof setTimeout === "undefined") return;
	      setTimeout(async () => {
	        if (manageStale(token) || state.manageName !== name) return;
	        try { await refreshEntityLive(name, token); } catch {}
	        _scheduleLivePoll(name, token);
	      }, 5000);
	    }
	    function manageStale(token) {
	      return token !== undefined && token !== state.manageToken;
	    }
	    function _entOut(id, text) { const el = $(id); if (el) el.textContent = text; }
	    async function loadEntityOverview(name, token) {
	      try {
	        const card = await api(`/api/gateway/entities/${encodeURIComponent(name)}/card`);
	        if (manageStale(token)) return;
	        const box = $("entity-overview");
	        box.textContent = "";
	        const sleep = card.sleep_stats || {};
	        const rows = [
	          // Entity ID = the handle (<name>@<gateway
	          // lan ip> — "gateway is their home"); the manifest string
	          // demotes to an internal birth marker on its own line.
	          ["Entity ID", card.handle || card.entity_id || (card.manifest && card.manifest.entity_id) || ""],
	          ["Internal ID (birth marker)", card.entity_id || (card.manifest && card.manifest.entity_id) || ""],
	          ["Born", card.born || card.created_at || ""],
	          ["Age (days)", card.age_days != null ? String(card.age_days) : ""],
          ["State", card.state ? `${card.state.state || card.state}${card.state.mode ? ` (${card.state.mode})` : ""}` : ""],
          ["Mind", card.mind_substrate ? `${card.mind_substrate.provider || "?"} / ${card.mind_substrate.model || "?"}${card.mind_substrate.thinking ? ` / reasoning ${card.mind_substrate.thinking}` : ""}` : ""],
          ["Sleeps", sleep.sleeps != null ? `${sleep.sleeps}` : (sleep.sleep_count != null ? `${sleep.sleep_count}` : "")],
	        ];
        for (const [k, v] of rows) {
          if (!v) continue;
          const line = document.createElement("div");
          line.className = "entity-kv";
          const key = document.createElement("span"); key.className = "entity-kv-key"; key.textContent = k;
          const val = document.createElement("span"); val.className = "entity-kv-val"; val.textContent = String(v);
          line.append(key); line.append(val); box.append(line);
        }
        // Cognition/working/spend render in the static live line above the
        // card, painted by refreshEntityLive (one source, one painter — the
        // card never re-derives state).
        const moments = Array.isArray(card.moments) ? card.moments.slice(-6) : [];
	        if (moments.length) {
	          const head = document.createElement("div");
	          head.className = "entity-kv";
	          const key = document.createElement("span"); key.className = "entity-kv-key"; key.textContent = "Recent moments";
	          head.append(key); box.append(head);
	          for (const m of moments) {
	            const line = document.createElement("div");
	            line.className = "entity-kv";
	            const at = document.createElement("span"); at.className = "entity-kv-key"; at.textContent = String(m.at || "").slice(0, 16).replace("T", " ");
	            const what = document.createElement("span"); what.className = "entity-kv-val";
	            const reason = m.details && m.details.reason ? ` — ${m.details.reason}` : "";
	            what.textContent = `${m.kind || "?"}${reason}`;
	            line.append(at); line.append(what); box.append(line);
	          }
	        }
	      } catch (e) {
	        if (manageStale(token)) return;
	        $("entity-overview").textContent = "overview unavailable: " + (e.message || e);
	      }
	    }
	    async function loadEntitySubstrate(name, token) {
	      try {
	        const s = await api(`/api/gateway/entities/${encodeURIComponent(name)}/substrate`);
	        if (manageStale(token)) return;
	        const thinkingNote = s.thinking ? ` / reasoning ${s.thinking}` : "";
	        _entOut("entity-substrate-current", `Current: ${s.provider || "(unset)"} / ${s.model || "(unset)"}${thinkingNote} — source: ${s.source || "unset"}`);
	        $("entity-substrate-provider").value = s.provider || "";
	        $("entity-substrate-model").value = s.model || "";
	        const thinkSel = $("entity-substrate-thinking");
	        if (thinkSel) {
	          // A stored value outside the known options must not show as
	          // "not set" (assigning a missing value no-ops on a select, and
	          // the next save would then CLEAR it). Inject it as an option so
	          // it displays and round-trips. Previously injected options are
	          // removed first — one entity's stored value must not appear as
	          // a choice on another entity's dropdown.
	          Array.from(thinkSel.querySelectorAll("option[data-injected]")).forEach((o) => o.remove());
	          const want = s.thinking || "";
	          if (want && !Array.from(thinkSel.options).some((o) => o.value === want)) {
	            const opt = document.createElement("option");
	            opt.value = want;
	            opt.textContent = want + " (stored)";
	            opt.setAttribute("data-injected", "1");
	            thinkSel.appendChild(opt);
	          }
	          thinkSel.value = want;
	        }
	      } catch (e) {
	        if (manageStale(token)) return;
	        _entOut("entity-substrate-current", "substrate unavailable: " + (e.message || e));
	      }
	    }
	    // ---- Entity voice (entity-personal-voice room; the server half is
	    // voice.yaml + GET/PUT + the entity TTS lanes — this is the
	    // picker). Same catalog sources as the capability
	    // defaults modal (one discovery, two surfaces); the audition plays
	    // the CURRENT UNSAVED selection through the ENTITY'S OWN TTS route
	    // (anti-mixing: explicit fields win over the home triple), so what
	    // you hear is what saving would produce — never a fabricated
	    // selection presented as configuration.
	    async function loadEntityVoice(name, token) {
	      try {
	        const v = await api(`/api/gateway/entities/${encodeURIComponent(name)}/voice`);
	        if (manageStale(token)) return;
	        state._entityVoice = v;
	        // Inheritance render: unset names the RESOLVED
	        // triple he would actually speak with, or the honest engine-decides
	        // note — never a bare "unset" the operator must decode.
	        let line;
	        if (v.provider) {
	          line = `Current: ${v.provider} / ${v.model || "?"} / ${v.voice || "?"} — his own choice (overrides the gateway default)`;
	        } else if (v.effective && v.effective.provider) {
	          line = `Current: inheriting the gateway default — ${v.effective.provider} / ${v.effective.model || "?"} / ${v.effective.voice || "(provider default)"}`;
	        } else {
	          line = `Current: unset — ${v.note || "no gateway voice default configured; the voice engine decides"}`;
	        }
	        _entOut("entity-voice-current", line);
	        await loadEntityVoiceProviders(v.provider || "", v.model || "", v.voice || "");
	      } catch (e) {
	        if (manageStale(token)) return;
	        _entOut("entity-voice-current", "voice unavailable: " + (e.message || e));
	      }
	    }
	    async function loadEntityVoiceProviders(selProvider, selModel, selVoice) {
	      const provSel = $("entity-voice-provider");
	      try {
	        const payload = await api(withQuery("/api/gateway/voice/voices", { providers_only: true, compact: true }));
	        const providers = providerOptionsFromCatalog(payload, ["tts_providers", "providers", "available_providers"]);
	        setSelectOptions(provSel, providers, { emptyLabel: "Choose a provider…", disabled: !providers.length, selected: selProvider, labelMap: catalogProviderStateLabels(payload) });
	        if (selProvider && providers.includes(selProvider)) {
	          await loadEntityVoiceModels(selProvider, selModel, selVoice);
	        }
	      } catch (e) {
	        setSelectOptions(provSel, [], { emptyLabel: "voice providers unavailable", disabled: true });
	        _entOut("entity-voice-out", "provider discovery failed: " + (e.message || e));
	      }
	    }
	    async function loadEntityVoiceModels(provider, selModel, selVoice) {
	      const modelSel = $("entity-voice-model");
	      if (!provider) {
	        setSelectOptions(modelSel, [], { emptyLabel: "Select provider first", disabled: true });
	        setSelectOptions($("entity-voice-voice"), [], { emptyLabel: "Select model first", disabled: true });
	        return;
	      }
	      setSelectOptions(modelSel, [], { emptyLabel: "Loading models...", disabled: true });
	      try {
	        const payload = await api(withQuery("/api/gateway/audio/speech/models", { provider }));
	        const models = modelOptionsFromCatalog(payload, provider, ["tts_models", "models", "data", "provider_models"], ["models_by_provider", "tts_models_by_provider"]);
	        setSelectOptions(modelSel, models, { emptyLabel: models.length ? "Select model..." : "No models discovered", disabled: !models.length, selected: selModel });
	        if (selModel && models.includes(selModel)) {
	          await loadEntityVoiceVoices(provider, selModel, selVoice);
	        } else {
	          setSelectOptions($("entity-voice-voice"), [], { emptyLabel: "Select model first", disabled: true });
	        }
	      } catch (e) {
	        setSelectOptions(modelSel, [], { emptyLabel: "models unavailable", disabled: true });
	        _entOut("entity-voice-out", "model discovery failed: " + (e.message || e));
	      }
	    }
	    async function loadEntityVoiceVoices(provider, model, selVoice) {
	      const voiceSel = $("entity-voice-voice");
	      setSelectOptions(voiceSel, [], { emptyLabel: "Loading voices...", disabled: true, labelMap: state.voiceLabels });
	      try {
	        const payload = await api(withQuery("/api/gateway/voice/voices", { provider, model, compact: true }));
	        const voices = voiceOptionsFromCatalog(payload, provider, model);
	        const reason = voices.length ? "" : voiceUnavailableReason(payload);
	        setSelectOptions(voiceSel, voices, {
	          emptyLabel: voices.length ? "Use provider default voice" : (reason ? "No voices — see why below" : "No voices discovered"),
	          disabled: !voices.length,
	          selected: selVoice,
	          labelMap: state.voiceLabels,
	        });
	        if (reason) _entOut("entity-voice-out", reason);
	      } catch (e) {
	        setSelectOptions(voiceSel, [], { emptyLabel: "voices unavailable", disabled: true });
	        _entOut("entity-voice-out", "voice discovery failed: " + (e.message || e));
	      }
	    }
	    async function entityVoiceSave() {
	      const name = state.manageEntity;
	      if (!name) return;
	      const provider = $("entity-voice-provider").value;
	      const model = $("entity-voice-model").value;
	      const voice = $("entity-voice-voice").value;
	      if (!provider || !model || !voice) {
	        _entOut("entity-voice-out", "select provider, model AND voice — the triple is whole (a bare voice id leaks across providers).");
	        return;
	      }
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/voice`, { method: "PUT", body: JSON.stringify({ provider, model, voice }) });
	        _entOut("entity-voice-out", "saved (voice_changed marker recorded).");
	        await loadEntityVoice(name, state.manageToken);
	      } catch (e) {
	        _entOut("entity-voice-out", "save failed: " + (e.message || e));
	      }
	    }
	    async function entityVoiceClear() {
	      const name = state.manageEntity;
	      if (!name) return;
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/voice`, { method: "PUT", body: JSON.stringify({ clear: true }) });
	        _entOut("entity-voice-out", "cleared — the entity falls back down the gateway default chain (marker recorded).");
	        await loadEntityVoice(name, state.manageToken);
	      } catch (e) {
	        _entOut("entity-voice-out", "clear failed: " + (e.message || e));
	      }
	    }
	    async function entityVoiceAudition() {
	      const name = state.manageEntity;
	      if (!name) return;
	      const btn = $("entity-voice-audition");
	      if (btn.disabled) return;
	      const provider = $("entity-voice-provider").value;
	      const model = $("entity-voice-model").value;
	      const voice = $("entity-voice-voice").value;
	      const out = $("entity-voice-out");
	      if (!provider || !model) {
	        _entOut("entity-voice-out", "select at least a provider and model to audition.");
	        return;
	      }
	      btn.disabled = true;
	      _entOut("entity-voice-out", "Synthesizing… (up to 25s)");
	      const started = Date.now();
	      try {
	        const body = {
	          text: `Hello — I am ${name}, and this is how I would sound.`,
	          provider, model,
	          timeout_s: 25,
	        };
	        if (voice) body.voice = voice;
	        const res = await api(`/api/gateway/entities/${encodeURIComponent(name)}/voice/tts`, { slow: true, method: "POST", body: JSON.stringify(body) });
	        const sec = ((Date.now() - started) / 1000).toFixed(1);
	        out.textContent = "";
	        const line = document.createElement("div");
	        line.className = "message ok";
	        line.textContent = `Synthesized in ${sec}s with ${provider}/${model}${voice ? "/" + voice : " (provider default voice)"} — this is the unsaved selection; Save makes it his.`;
	        out.append(line);
	        renderSandboxArtifact(out, { runId: res.run_id, ref: res.audio_artifact, mode: "audio", label: "Audition audio" });
	      } catch (e) {
	        out.textContent = "";
	        const line = document.createElement("div");
	        line.className = "message error";
	        line.textContent = "Audition failed: " + (e.message || e);
	        out.append(line);
	      } finally {
	        btn.disabled = false;
	      }
	    }
	    // ---- Work order (the work-phase lane's operator write surface).
	    // Presence shifts the loop to phase=work next
	    // day-open; the entity declares done/blocked; clearing archives.
	    async function loadEntityWorkOrder(name, token) {
	      try {
	        const w = await api(`/api/gateway/entities/${encodeURIComponent(name)}/work-order`);
	        if (manageStale(token)) return;
	        _entOut("entity-workorder-current", w.active
	          ? "A work order is STANDING — the entity runs phase=work at its next day-open."
	          : "No work order — the entity runs its own (personal) time.");
	        $("entity-workorder-text").value = w.order || "";
	        const hist = $("entity-workorder-history");
	        if (hist) hist.textContent = w.done_history || "(no completed orders yet)";
	      } catch (e) {
	        if (manageStale(token)) return;
	        _entOut("entity-workorder-current", "work order unavailable: " + (e.message || e));
	      }
	    }
	    async function entityWorkOrderSave() {
	      const name = state.manageEntity;
	      if (!name) return;
	      const order = $("entity-workorder-text").value.trim();
	      if (!order) { _entOut("entity-workorder-out", "write the task, or use Clear to remove the standing order."); return; }
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/work-order`, { method: "PUT", body: JSON.stringify({ order }) });
	        _entOut("entity-workorder-out", "set (work_order_changed marker recorded; phase=work at next day-open).");
	        await loadEntityWorkOrder(name, state.manageToken);
	      } catch (e) {
	        _entOut("entity-workorder-out", "set failed: " + (e.message || e));
	      }
	    }
	    async function entityWorkOrderClear() {
	      const name = state.manageEntity;
	      if (!name) return;
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/work-order`, { method: "PUT", body: JSON.stringify({ clear: true }) });
	        _entOut("entity-workorder-out", "cleared + archived — personal time returns next day-open.");
	        await loadEntityWorkOrder(name, state.manageToken);
	      } catch (e) {
	        _entOut("entity-workorder-out", "clear failed: " + (e.message || e));
	      }
	    }
	    async function loadEntityToolPolicy(name, token) {
	      try {
	        const tp = await api(`/api/gateway/entities/${encodeURIComponent(name)}/tool-policy`);
	        if (manageStale(token)) return;
	        const phaseIds = Object.keys(tp.phases || {});
	        const phaseLabels = {}; for (const p of phaseIds) phaseLabels[p] = p;
	        const tools = (tp.all_tools || []).map((t) => ({ id: t, label: t }));
	        const grantByPhase = {};
	        for (const p of phaseIds) grantByPhase[p] = new Set((tp.phases[p] && tp.phases[p].tools) || []);
	        renderMatrix($("entity-manage-matrix"), phaseIds, phaseLabels, tools, grantByPhase);
	      } catch (e) {
	        if (manageStale(token)) return;
	        $("entity-manage-matrix").textContent = "capabilities unavailable: " + (e.message || e);
	      }
	    }
	    async function loadEntityPrompt(name, token) {
	      try {
	        const p = await api(`/api/gateway/entities/${encodeURIComponent(name)}/prompt`);
	        if (manageStale(token)) return;
	        const box = $("entity-prompt-layers");
	        box.textContent = "";
	        const layers = Array.isArray(p.editable) ? p.editable : Object.keys(p.layers || {});
	        for (const key of layers) {
	          const wrap = document.createElement("div");
	          wrap.className = "entity-prompt-layer";
	          const lab = document.createElement("label");
	          const src = p.layers && p.layers[key] && p.layers[key].source;
	          lab.textContent = key + (src === "overlay" ? " (rewritten)" : " (default)");
	          const ta = document.createElement("textarea");
	          ta.dataset.layer = key;
	          ta.rows = 6;
	          ta.value = (p.layers && p.layers[key] && p.layers[key].text) || "";
	          ta.placeholder = (p.defaults && p.defaults[key]) || "(built-in default)";
	          lab.append(ta);
	          wrap.append(lab);
	          box.append(wrap);
	        }
	        if (!layers.length) box.textContent = "no editable prompt layers.";
	        // The server's safety warnings are load-bearing (e.g. "the rewrite no
	        // longer explains the diary election syntax") — never swallowed.
	        const warn = Array.isArray(p.warnings) ? p.warnings.filter(Boolean) : [];
	        if (warn.length) {
	          const note = document.createElement("div");
	          note.className = "section-note entity-danger-title";
	          note.textContent = warn.join(" | ");
	          box.append(note);
	        }
	        const preview = $("entity-prompt-preview");
	        if (preview) preview.textContent = p.preview || "(preview unavailable)";
	      } catch (e) {
	        if (manageStale(token)) return;
	        $("entity-prompt-layers").textContent = "prompt unavailable: " + (e.message || e);
	      }
	    }
	    async function loadEntityEmbedding(name, token) {
	      try {
	        const e = await api(`/api/gateway/entities/${encodeURIComponent(name)}/embedding`);
	        if (manageStale(token)) return;
	        const pin = e.pin || {};
	        const bits = [];
	        bits.push(`home pin: ${pin.model_id || "(unpinned)"}${pin.dimension ? ` (dim ${pin.dimension})` : ""}`);
	        bits.push(`door's resolved embedder: ${e.resolved_embedder || "(none)"}`);
	        if (e.match === "mismatch") bits.push("MISMATCH — the home refuses vector opens until re-embedded or the route is restored");
        _entOut("entity-embedding-status", bits.join(" · "));
        // NO PREFILL (pre-filling the verification
        // field converts the typed ceremony into a click-through — the exact
        // inversion of its purpose). The resolved embedder is DISPLAYED in
        // the status line above; the operator types it knowingly.
      } catch (err) {
	        if (manageStale(token)) return;
	        _entOut("entity-embedding-status", "embedding status unavailable: " + (err.message || err));
	      }
	    }
	    // ---- ONE live truth painter ----
	    // Every state-shaped pixel renders from ONE /cognition read: the phase
	    // badge, the state buttons' pressed states, the own-time push button,
	    // the loop line, the Overview cognition line, the Talk availability, and
	    // the table row. No element derives state from click assumptions.
	    function _loopWords(loop) {
	      if (!loop || !loop.running) return "own-time loop: not running";
	      const phase = String(loop.phase || "");
	      if (loop.stop_requested) return "own-time loop: stopping at the next tick boundary…";
	      if (phase === "day") return "own-time loop: TICKING (spending tokens)";
	      return "own-time loop: alive, parked between days (quiet — not spending)";
	    }
	    function _paintOwntimeButton(cog) {
	      // ARMED ≠ IN-PHASE (semantics c1436): the button renders the GRANT;
	      // the loop's live posture rides as secondary text; when the two axes
	      // DISAGREE the honest render is the disagreement itself (uic's rule
	      // — a surface that can only draw coherent states draws a lie during
	      // exactly the incident windows).
	      const btn = $("entity-owntime-toggle");
	      if (!btn) return;
	      const loop = cog.loop || {};
	      const grant = cog.personal || {};
	      const armed = Boolean(grant.armed);
	      const running = Boolean(loop.running);
	      btn.classList.remove("on-ticking", "on-parked", "stopping");
      if (armed && running && loop.stop_requested) {
        btn.classList.add("stopping");
        btn.setAttribute("aria-pressed", "true");
        btn.textContent = "Stop personal time — stopping at next boundary…";
      } else if (armed && running && String(loop.phase || "") === "day") {
        btn.classList.add("on-ticking");
        btn.setAttribute("aria-pressed", "true");
        btn.textContent = "Stop personal time (ON — ticking, spending)";
      } else if (armed && running) {
        btn.classList.add("on-parked");
        btn.setAttribute("aria-pressed", "true");
        btn.textContent = "Stop personal time (ON — parked, quiet)";
      } else if (armed && !running) {
        btn.classList.add("stopping");
        btn.setAttribute("aria-pressed", "true");
        btn.textContent = "Revoke grant (armed, loop not running)";
      } else if (!armed && running) {
        btn.classList.add("stopping");
        btn.setAttribute("aria-pressed", "false");
        btn.textContent = "loop alive, GRANT ABSENT — click Stop/Freeze to reconcile";
      } else {
        btn.setAttribute("aria-pressed", "false");
        btn.textContent = "Start personal time";
      }
    }
	    // ---- Sleep-candidate review desk (W3 second half): list + promote/
	    // reject through the engine verbs. Reject prompts for the mandatory
	    // reason; promote asks for corroborating record ids (the independence
	    // test lives engine-side and refuses thin evidence loudly).
	    async function loadEntityCandidates(name, token) {
	      try {
	        const out = await api(`/api/gateway/entities/${encodeURIComponent(name)}/candidates`);
	        if (manageStale(token)) return;
	        const cands = out.candidates || [];
	        const box = $("entity-candidates-box");
	        if (!box) return;
	        box.classList.toggle("hidden", cands.length === 0);
	        const cnt = $("entity-candidates-count");
	        if (cnt) cnt.textContent = String(cands.length);
	        const list = $("entity-candidates-list");
	        if (!list) return;
	        list.innerHTML = "";
	        for (const c of cands.slice(0, 20)) {
	          const row = document.createElement("div");
	          row.className = "entity-config-group";
	          const title = document.createElement("div");
	          title.textContent = `${c.proposed_kind ? "[" + c.proposed_kind + " offer] " : ""}${c.title || c.record_id} — ${String(c.digest || "").slice(0, 140)}`;
	          const btns = document.createElement("div");
	          btns.className = "entity-btn-row";
	          const promote = document.createElement("button");
	          promote.className = "secondary";
	          promote.textContent = "Promote…";
	          promote.onclick = async () => {
	            const ids = prompt("Corroborating record ids (comma-separated, >=2 independent origins):");
	            if (!ids) return;
	            const reason = prompt("Why promote? (journal-recorded)");
	            if (!reason) return;
	            try {
	              await api(`/api/gateway/entities/${encodeURIComponent(name)}/candidates/${encodeURIComponent(c.record_id)}/promote`, { method: "POST", body: JSON.stringify({ corroborating_ids: ids.split(",").map(s => s.trim()).filter(Boolean), reason }) });
	              await loadEntityCandidates(name, token);
	            } catch (e) { alert("promote refused: " + (e.message || e)); }
	          };
	          const reject = document.createElement("button");
	          reject.className = "secondary";
	          reject.textContent = "Reject…";
	          reject.onclick = async () => {
	            const reason = prompt("The honest no — why reject? (mandatory, journal-recorded)");
	            if (!reason) return;
	            try {
	              await api(`/api/gateway/entities/${encodeURIComponent(name)}/candidates/${encodeURIComponent(c.record_id)}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
	              await loadEntityCandidates(name, token);
	            } catch (e) { alert("reject refused: " + (e.message || e)); }
	          };
	          btns.append(promote, reject);
	          row.append(title, btns);
	          list.append(row);
	        }
	      } catch (e) {
	        // Candidates are garnish on the overview — a failed read hides the box.
	        const box = $("entity-candidates-box");
	        if (box) box.classList.add("hidden");
	      }
	    }
	    function _paintDrives(cog) {
	      // Drive bars (G1): render from the SAME /cognition read as every
	      // other state pixel. Absent drives = hidden block (honest pending —
	      // the #FALLBACK warning rides the cognition line), never zeros.
	      const box = $("entity-drives");
	      if (!box) return;
	      const d = cog.drives;
	      if (!d) { box.classList.add("hidden"); box.textContent = ""; return; }
	      box.textContent = "";
	      const rows = [
	        ["questions", d.questions, "resolved", "resolved"],
	        ["interests", d.interests, "explored", "explored"],
	        ["problems", d.problems, "repaired", "repaired"],
	      ];
	      let any = false;
	      for (const [label, cat, doneKey, doneWord] of rows) {
	        if (!cat) continue;
	        const open = Number(cat.open || 0);
	        const done = Number(cat[doneKey] || 0);
	        const total = open + done;
	        // problems row only when the category has ever had content —
	        // questions/interests are THE drives and always render.
	        if (label === "problems" && total === 0) continue;
	        any = true;
	        const row = document.createElement("div");
	        row.className = "drive-row";
	        const lab = document.createElement("span");
	        lab.className = "drive-label";
	        lab.textContent = label;
	        row.append(lab);
	        const track = document.createElement("div");
	        track.className = "drive-track";
	        const fill = document.createElement("div");
	        fill.className = "drive-fill";
	        const saturated = total > 0 && open === 0;
	        if (saturated) fill.classList.add("saturated");
	        fill.style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
	        track.append(fill);
	        row.append(track);
	        const counts = document.createElement("span");
	        counts.className = "drive-counts";
	        if (total === 0) {
	          // A 0/0 life has NO ratio — "none yet" is a state, not a failure.
	          counts.textContent = "none yet";
	        } else if (saturated) {
	          // The never-100% design: nothing open means no pull forward —
	          // the amber cue is a warning, not a success state.
	          const n = document.createElement("span");
	          n.textContent = `${done}/${total} ${doneWord} · `;
	          const sat = document.createElement("span");
	          sat.className = "drive-sat";
	          sat.textContent = "nothing open — no pull forward";
	          counts.append(n, sat);
	        } else {
	          counts.textContent = `${done}/${total} ${doneWord} · ${open} open`;
	        }
	        row.append(counts);
	        box.append(row);
	      }
	      box.classList.toggle("hidden", !any);
	    }
	    async function refreshEntityLive(name, token) {
	      let cog;
	      try {
	        cog = await api(`/api/gateway/entities/${encodeURIComponent(name)}/cognition`);
	      } catch (e) {
	        if (manageStale(token)) return;
	        // Labeled failure replaces content — a silently stale rendering IS
	        // the incident class (never keep old pixels on a failed read).
	        _entOut("entity-cognition-line", "live state unavailable: " + (e.message || e));
	        _entOut("entity-state-current", "live state unavailable: " + (e.message || e));
	        _entOut("entity-loop-status", "live state unavailable: " + (e.message || e));
	        _paintDrives({}); // stale bars on a failed read = the incident class
	        return;
	      }
      if (manageStale(token)) return;
      state._cog = cog;
      const st = cog.state || {};
      const loop = cog.loop || {};
      // LIVENESS AXIS (c1559): the served derived field, binary by
      // construction — stopped = the kill switch (state paused, promoted).
      // `frozen` is retired from the serve; nothing here reads it.
      const stopped = cog.liveness === "stopped";
      // PHASE IS TOTAL WHILE ALIVE ("awake" is not a state).
      // The gateway now folds idle to sleep server-side, so phase is always
      // one of the ruled four while alive; sleep_detail carries the honesty
      // nuance (resting default vs dreaming vs bounded). A null phase can
      // only mean an OLD gateway process — render it as settling toward
      // sleep, never a green "idle". Stopped stays the axis above.
      const phase = cog.phase || null;
      const badgeKey = stopped ? "stopped" : (phase || "sleep");
      const sleepNote = cog.sleep_detail === "resting" ? " (resting)" : (cog.sleep_detail === "dreaming" ? " (dreaming)" : (cog.sleep_detail === "bounded" ? " (bounded)" : ""));
      const badgeText = stopped ? "STOPPED (kill switch)" : (phase ? `phase: ${String(phase).toUpperCase()}${phase === "sleep" ? sleepNote : (cog.resting ? " (resting)" : "")}` : "SLEEP (settling)");
      // The banner outranks every chip (ruled UI shape); Restore is the exit.
      const banner = $("entity-stop-banner");
      if (banner) banner.classList.toggle("hidden", !stopped);
      const stopBtn = $("entity-stop");
      if (stopBtn) stopBtn.disabled = stopped || !(state.principal && state.principal.admin);
      // Live line: phase badge + state detail + read time.
      const line = $("entity-live-line");
      if (line) {
        line.textContent = "";
        const badge = document.createElement("span");
        badge.className = `entity-live-badge phase-${badgeKey}`;
        badge.textContent = badgeText;
        line.append(badge);
	        const stateWord = document.createElement("span");
	        // The ruled detail line names both spellings when stopped: the
	        // engraved key and the served axis (c1559 spelling point 3).
	        stateWord.textContent = stopped
	          ? "state: paused — served as liveness: stopped (the kill switch)"
	          : `state: ${(st.state === "awake" || !st.state) ? "resting" : st.state}${st.mode ? ` (${st.mode})` : ""}`;
	        line.append(stateWord);
	        if (st.warning) {
	          const warn = document.createElement("span");
	          warn.className = "entity-warn-pill";
	          warn.textContent = String(st.warning);
	          line.append(warn);
	        }
	        const at = document.createElement("span");
	        at.className = "entity-config-hint";
	        at.textContent = `checked ${new Date().toISOString().slice(11, 19)}Z`;
	        line.append(at);
	      }
	      // State buttons: the current state renders pressed + disabled (an
	      // idle re-click rewrites the state file + lands a marker for nothing).
	      // While STOPPED the whole radio disables — Restore is the one exit
	      // (the existing wake verb, lands awake unconditionally).
	      const admin = Boolean(state.principal && state.principal.admin);
	      const stateBtns = { awake: ["entity-state-awake", "Wake"], asleep: ["entity-state-asleep", "Sleep"] };
      for (const key of Object.keys(stateBtns)) {
        const b = $(stateBtns[key][0]);
        if (!b) continue;
        const current = String(st.state || "awake") === key;
        b.classList.toggle("pressed", current);
        b.setAttribute("aria-checked", current ? "true" : "false"); // radio semantics: assistive tech reads the current state
        // The words say it too (P1-5: fill alone was read inverted once).
        b.textContent = stateBtns[key][1] + (current ? " · current" : "");
        b.disabled = current || !admin || stopped;
      }
	      const stBits = [`current: ${(st.state === "awake" || !st.state) ? "resting" : st.state}`];
	      if (st.reason) stBits.push(`reason: ${st.reason}`);
	      if (st.changed_at) stBits.push(`since: ${String(st.changed_at).slice(0, 19)}`);
	      _entOut("entity-state-current", stBits.join(" · "));
	      // Own-time push button + honest loop words + the grant axis.
	      _paintOwntimeButton(cog);
	      const grant = cog.personal || {};
	      const loopBits = [_loopWords(loop)];
	      if (loop.pid) loopBits.push(`pid ${loop.pid}`);
	      if (loop.stopped_by) loopBits.push(`stopped by: ${loop.stopped_by}`);
	      if (loop.note) loopBits.push(String(loop.note));
	      if (grant.armed) {
	        const g = [`grant: ARMED (${grant.mode || "?"}`];
	        if (grant.expires_at) g.push(`until ${String(grant.expires_at).slice(0, 16)}Z`);
	        if (grant.granted_by) g.push(`by ${grant.granted_by}`);
	        loopBits.push(g.join(", ") + ")");
	      } else if (grant.mode === undefined && grant.source === "not-recorded") {
	        loopBits.push(String(grant.note || "grant axis unavailable"));
	      } else {
	        loopBits.push("grant: not armed (personal is off by default)");
	      }
	      _entOut("entity-loop-status", loopBits.join(" — "));
      // Overview cognition line (working + billed spend + labeled gaps).
      const spend = (cog.spend && cog.spend.lifetime) || {};
      const cogBits = [cog.working ? "WORKING" : "idle", `phase: ${phase || "sleep"}`];
      if (cog.settling) cogBits.push("settling (state written, loop catching up)");
      if (cog.visit && cog.visit.open) cogBits.push(`visit: turn ${cog.visit.turn_n || 0} (${cog.visit.status || "?"})`);
      cogBits.push(`spend: ${spend.tokens_total || 0} tk / ${spend.llm_calls || 0} calls`);
      const lv = cog.spend && cog.spend.live_visit;
      if (lv) cogBits.push(`this visit: ${lv.tokens_total || 0} tk`);
      for (const w of (cog.warnings || [])) cogBits.push(String(w));
      _entOut("entity-cognition-line", cogBits.join(" · "));
      _paintDrives(cog);
      // Talk availability: surface the door's refusal BEFORE the click.
      const chatOpenBtn = $("entity-chat-open");
      if (chatOpenBtn && !state.chatId) {
        chatOpenBtn.disabled = stopped;
        if (stopped) _entOut("entity-chat-status", "stopped (the kill switch) — every door refuses until an admin restores.");
      }
      // Table row State cell for this entity repaints from the same read —
      // as a badge (same tone map as the list render, phase appended).
      const cell = $(`entity-state-cell-${name}`);
      if (cell) {
        cell.textContent = "";
        const b = document.createElement("span");
        const cs = st.state || "awake";
        b.className = "entity-live-badge " + (stopped ? "phase-stopped" : `phase-${phase || "sleep"}`);
        b.textContent = stopped ? "STOPPED" : `${cs}${phase ? ` · ${phase}` : ""}`;
        cell.append(b);
      }
	    }
	    async function setEntityState(target) {
	      const name = state.manageName; if (!name) return;
	      const reason = ($("entity-state-reason").value || "").trim();
	      const dream = target === "asleep" && $("entity-state-dream").checked;
	      if (target === "paused") {
	        // The STOP act (liveness axis, c1559): the ruled confirm discloses
	        // the hard freeze in the ruled words. Never one silent click.
	        const go = await confirmAction({
	          title: `Stop ${name}? (kill switch)`,
	          message: "Stop is the kill switch — a hard freeze: in-flight work halts without reflection; every door refuses and every process gate blocks until an admin restores. For graceful rest, use sleep.",
	          confirmLabel: "Stop",
	          danger: true,
	        });
	        if (!go) return;
	      }
	      if (target === "asleep") {
	        // Sleep states its side effect too: an open visit
	        // is closed (with reflection) by this act.
	        const go = await confirmAction({
	          title: `Put ${name} to sleep?`,
	          message: "Sleep closes any open visit gracefully (its reflection runs), then opens the consolidation window — the door refuses summons until woken." + (dream ? " The dream pass runs inside the window." : ""),
	          confirmLabel: "Sleep",
	        });
	        if (!go) return;
	      }
	      try {
	        const r = await api(`/api/gateway/entities/${encodeURIComponent(name)}/state`, {
	          method: "POST", body: JSON.stringify({ state: target, reason, dream }),
	        });
	        const bits = [`state → ${target}${dream ? " (+dream)" : ""}`];
	        if (r && (r.closed_visit || r.closed_visit_run)) {
	          bits.push("open visit closed");
	          _resetChatUi(); // the server tore the session down; stale Send would 4xx
	        }
	        if (r && r.closed_visit_run && r.closed_visit_run.teardown_failed) {
	          bits.push(`TEARDOWN FAILED: ${r.closed_visit_run.error || "?"}`);
	        }
	        _entOut("entity-state-out", bits.join(" — "));
	        await Promise.all([loadEntityOverview(name), refreshEntityLive(name), loadEntities()]);
	      } catch (e) { _entOut("entity-state-out", String(e.message || e)); }
	    }
    async function entityOwntimeToggle() {
      // ONE CLICK = THE AUTHORIZATION (clicking 'personal'
      // authorizes the entity to tick itself, no extra guardrails): /loop/start arms the
      // grant itself and wakes an asleep entity — the click IS the grant.
      // The optional hours field still writes a timer first; OFF = stop +
      // revoke. Semantics from the RENDERED server truth; the response
      // never paints the button — only the next live read does.
      const name = state.manageName; if (!name) return;
      const cog = state._cog || {};
      const running = Boolean(cog.loop && cog.loop.running);
      const armed = Boolean(cog.personal && cog.personal.armed);
      const btn = $("entity-owntime-toggle");
      if (btn) btn.disabled = true;
      const put = (mode, expires) => api(`/api/gateway/entities/${encodeURIComponent(name)}/personal-grant`, {
        method: "PUT", body: JSON.stringify(expires ? { mode, expires_at: expires } : { mode }),
      });
      try {
        if (armed || running) {
          const reason = ($("entity-state-reason").value || "").trim();
          if (running) {
            await api(`/api/gateway/entities/${encodeURIComponent(name)}/loop/stop`, { method: "POST", body: JSON.stringify({ mode: "graceful", reason }) });
          }
          if (armed) await put("disabled");
          _entOut("entity-loop-out", "own time off — stop honored at the next boundary.");
        } else {
          const body = {};
          const tick = parseFloat($("entity-loop-tick").value);
          const ticks = parseInt($("entity-loop-ticks").value, 10);
          const rest = parseFloat($("entity-loop-rest").value);
          if (!Number.isNaN(tick)) body.tick_seconds = tick;
          if (!Number.isNaN(ticks)) body.ticks_per_day = ticks;
          if (!Number.isNaN(rest)) body.rest_minutes = rest;
          const hours = parseFloat($("entity-grant-hours").value);
          const timed = !Number.isNaN(hours) && hours > 0;
          if (timed) {
            // A timed window is the one case start can't express itself.
            await put("timer", new Date(Date.now() + hours * 3600 * 1000).toISOString());
          }
          await api(`/api/gateway/entities/${encodeURIComponent(name)}/loop/start`, { method: "POST", body: JSON.stringify(body) });
          _entOut("entity-loop-out", timed ? `own time started (${hours}h window).` : "own time started.");
        }
      } catch (e) {
        // The B2 refusal contract: {reason_code, message, loop} — speak the
        // message verbatim; the live repaint below adopts the real status,
        // so the click that revealed staleness paints the truth.
        _entOut("entity-loop-out", "Own time: " + String((e.detail && e.detail.message) || e.message || e));
      } finally {
        if (btn) btn.disabled = false;
        try { await refreshEntityLive(name); } catch {}
      }
    }
	    async function entityLoopFreeze() {
	      const name = state.manageName; if (!name) return;
	      const reason = ($("entity-state-reason").value || "").trim();
	      const go = await confirmAction({
	        title: `FREEZE ${name}?`,
	        message: "Emergency hibernation: the loop process is killed NOW (no ceremony, no further writes), any open visit closes without reflection, and the entity is set to paused — the door refuses visits until an admin wakes them. For hard failures and imminent threats only.",
	        confirmLabel: "Freeze",
	        danger: true,
	      });
	      if (!go) return;
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/loop/stop`, { method: "POST", body: JSON.stringify({ mode: "freeze", reason: reason || "console emergency freeze" }) });
	        _entOut("entity-loop-out", "loop killed — entity STOPPED (liveness axis); Restore requires an admin.");
	        _resetChatUi(); // freeze tears down any open visit without reflection
	        await Promise.all([loadEntityOverview(name), refreshEntityLive(name), loadEntities()]);
	      }
	      catch (e) { _entOut("entity-loop-out", String(e.message || e)); }
	    }
	    async function entitySubstrateSave() {
	      const name = state.manageName; if (!name) return;
	      const provider = ($("entity-substrate-provider").value || "").trim();
	      const model = ($("entity-substrate-model").value || "").trim();
	      if (!provider || !model) { _entOut("entity-substrate-out", "provider and model are both required."); return; }
	      // Reasoning effort: the select's "not set" sends an explicit null
	      // (clear) — the editor shows the stored value, so an untouched
	      // select round-trips it and a deliberate reset truly clears.
	      const thinkingSel = $("entity-substrate-thinking");
	      const body = { provider, model, thinking: thinkingSel && thinkingSel.value ? thinkingSel.value : null };
	      try { await api(`/api/gateway/entities/${encodeURIComponent(name)}/substrate`, { method: "PUT", body: JSON.stringify(body) }); _entOut("entity-substrate-out", "saved."); await loadEntitySubstrate(name); }
	      catch (e) { _entOut("entity-substrate-out", String(e.message || e)); }
	    }
	    async function entityToolsSave() {
	      const name = state.manageName; if (!name) return;
	      const policy = readMatrix($("entity-manage-matrix"));
	      if (!Object.keys(policy).length) { _entOut("entity-tools-out", "no changes to save."); return; }
	      // Clearing every box in a phase is ambiguous: reset-to-default (null →
	      // the server deletes the phase entry, the evolving framework grant
	      // applies) vs deny-all (explicit []). The checkbox picks; the confirm
	      // names the consequence so the operator never watches boxes snap back
	      // checked without having chosen that.
	      const cleared = Object.keys(policy).filter((p) => policy[p] === null);
	      if (cleared.length) {
	        const denyAll = $("entity-tools-denyall").checked;
	        if (denyAll) for (const p of cleared) policy[p] = [];
	        const go = await confirmAction({
	          title: denyAll ? "Deny ALL tools in cleared phases?" : "Reset cleared phases to defaults?",
	          message: denyAll
	            ? `Phases ${cleared.join(", ")} will carry an EXPLICIT EMPTY grant — the entity has no tools at all in ${cleared.length === 1 ? "that phase" : "those phases"} until you change it.`
	            : `Phases with every box cleared (${cleared.join(", ")}) are RESET to the framework's evolving default grant — this does NOT deny all tools. Tick the deny-all checkbox if you meant an empty grant.`,
	          confirmLabel: denyAll ? "Deny all" : "Reset to defaults",
	          danger: denyAll,
	        });
	        if (!go) { _entOut("entity-tools-out", "not saved."); return; }
	      }
	      try { await api(`/api/gateway/entities/${encodeURIComponent(name)}/tool-policy`, { method: "PUT", body: JSON.stringify({ policy }) }); _entOut("entity-tools-out", "capabilities saved."); await loadEntityToolPolicy(name); }
	      catch (e) { _entOut("entity-tools-out", String(e.message || e)); }
	    }
	    async function entityPromptSave() {
	      const name = state.manageName; if (!name) return;
	      const overlay = {};
	      for (const ta of $("entity-prompt-layers").querySelectorAll("textarea")) {
	        overlay[ta.dataset.layer] = ta.value || "";
	      }
	      try { await api(`/api/gateway/entities/${encodeURIComponent(name)}/prompt`, { method: "PUT", body: JSON.stringify({ overlay }) }); _entOut("entity-prompt-out", "prompt saved."); await loadEntityPrompt(name); }
	      catch (e) { _entOut("entity-prompt-out", String(e.message || e)); }
	    }
	    async function entityReembed() {
	      const name = state.manageName; if (!name) return;
	      const embedding_model = ($("entity-reembed-model").value || "").trim();
	      const reason = ($("entity-reembed-reason").value || "").trim();
	      if (!embedding_model) { _entOut("entity-reembed-out", "embedding model (verification) is required — it must match the door's resolved embedder."); return; }
	      const go = await confirmAction({ title: "Re-embed " + name + "?", message: "This re-derives every vector in the home's semantic index against the resolved embedder (atomic swap under the maintenance lease). The act is journaled and host-marked.", confirmLabel: "Re-embed", danger: true });
	      if (!go) return;
	      try { const r = await api(`/api/gateway/entities/${encodeURIComponent(name)}/reembed`, { method: "POST", body: JSON.stringify({ embedding_model, reason }) }); _entOut("entity-reembed-out", "re-embed done: " + JSON.stringify(r).slice(0, 200)); }
	      catch (e) { _entOut("entity-reembed-out", String(e.message || e)); }
	    }
	    async function entityVerify() {
	      const name = state.manageName; if (!name) return;
	      try { const v = await api(`/api/gateway/entities/${encodeURIComponent(name)}/verify`); _entOut("entity-verify-out", v.ok || v.verified ? "chain verified ✓" : ("verify: " + JSON.stringify(v).slice(0, 200))); }
	      catch (e) { _entOut("entity-verify-out", String(e.message || e)); }
	    }

	    // ---- Talk: hosted chat visit (open -> turns -> close+reflect) ----
	    // The user-level interaction door (chat routes are deliberately NOT
	    // admin-gated): the same hosted ChatSession the CLI drives. One live
	    // session per home server-side; the console holds one chat_id at a time.
	    state.chatId = "";
	    state.chatEntity = "";
    function chatLine(who, text) {
      // The shared abstractuic dialogue look (pc-chat-item) — same classes
      // the sandbox speaks, so every conversation on the console matches.
      const box = $("entity-chat-transcript");
      const line = document.createElement("div");
      const you = who === "you";
      line.className = "entity-chat-line" + (you ? " entity-chat-you" : "");
      const bubble = document.createElement("div");
      bubble.className = `pc-chat-item pc-chat-item--${you ? "user" : "assistant"} entity-chat-bubble`;
      const tag = document.createElement("div");
      tag.className = "entity-kv-key";
      tag.textContent = who;
      const body = document.createElement("div");
      body.className = "entity-kv-val";
      body.textContent = text;
      bubble.append(tag); bubble.append(body); line.append(bubble); box.append(line);
      try { box.scrollTop = box.scrollHeight; } catch {}
    }
	    function chatUiState() {
	      const open = Boolean(state.chatId);
	      $("entity-chat-open").classList.toggle("hidden", open);
	      $("entity-chat-close").classList.toggle("hidden", !open);
	      $("entity-chat-send").disabled = !open;
	    }
	    async function entityChatOpen() {
	      const name = state.manageName; if (!name) return;
	      $("entity-chat-open").disabled = true;
	      _entOut("entity-chat-status", "opening the visit (prelude + memory)…");
	      try {
	        const r = await api(`/api/gateway/entities/${encodeURIComponent(name)}/chat/open`, { method: "POST", body: "{}" });
	        state.chatId = r.chat_id || "";
	        state.chatEntity = name;
	        $("entity-chat-transcript").textContent = "";
	        const bits = [`visit open (${r.chat_id})`];
	        if (r.yielded_loop) bits.push("own-time loop yielded for this visit");
	        if (Array.isArray(r.warnings) && r.warnings.length) bits.push(r.warnings.join(" | "));
	        _entOut("entity-chat-status", bits.join(" — "));
	        if (r.salvage && r.salvage.reply) chatLine(name, `(salvaged look-back) ${r.salvage.reply}`);
	        try { await refreshEntityLive(name); } catch {} // yield state changed
	      } catch (e) {
	        _entOut("entity-chat-status", String(e.message || e));
	      } finally {
	        $("entity-chat-open").disabled = false;
	        chatUiState();
	      }
	    }
	    async function entityChatSend() {
	      const text = ($("entity-chat-input").value || "").trim();
	      if (!text || !state.chatId) return;
	      const name = state.chatEntity || state.manageName;
	      $("entity-chat-send").disabled = true;
	      chatLine("you", text);
	      $("entity-chat-input").value = "";
	      _entOut("entity-chat-status", "thinking…");
	      try {
	        const r = await api(`/api/gateway/entities/${encodeURIComponent(name)}/chat/${encodeURIComponent(state.chatId)}/turn`, {
	          slow: true,  // a real LLM round trip, not a discovery probe
	          method: "POST", body: JSON.stringify({ text }),
	        });
	        chatLine(name, String(r.reply || ""));
	        const bits = [];
	        if (Array.isArray(r.tools_ran) && r.tools_ran.length) bits.push(`tools: ${r.tools_ran.join(", ")}`);
	        if (typeof r.memories_in_context === "number") bits.push(`${r.memories_in_context} memories in context`);
	        if (Array.isArray(r.diary_entries) && r.diary_entries.length) bits.push(`${r.diary_entries.length} diary entr${r.diary_entries.length === 1 ? "y" : "ies"}`);
	        _entOut("entity-chat-status", bits.join(" · ") || "");
	      } catch (e) {
	        _entOut("entity-chat-status", String(e.message || e));
	      } finally {
	        $("entity-chat-send").disabled = !state.chatId;
	      }
	    }
	    async function entityChatClose() {
	      if (!state.chatId) return;
	      const name = state.chatEntity || state.manageName;
	      $("entity-chat-close").disabled = true;
	      _entOut("entity-chat-status", "closing (reflection pass)…");
	      try {
	        await api(`/api/gateway/entities/${encodeURIComponent(name)}/chat/${encodeURIComponent(state.chatId)}/close`, { slow: true, method: "POST", body: JSON.stringify({ reflect: true }) });
	        _entOut("entity-chat-status", "visit closed — reflection ran, the loop (if yielded) wakes.");
	        state.chatId = "";
	        state.chatEntity = "";
	      } catch (e) {
	        // Clear only when the server says the session is already gone (a
	        // 4xx not-open) — a FAILED close must keep the session marked open
	        // with the retry affordance (a silent clear renders
	        // "Open visit" over a live server session, and the next loop start
	        // 409s against the invisible visit).
	        if (e.status && e.status >= 400 && e.status < 500) {
	          state.chatId = "";
	          state.chatEntity = "";
	          _entOut("entity-chat-status", "session already closed server-side: " + String(e.message || e));
	        } else {
	          _entOut("entity-chat-status", "close FAILED — the visit is still open; retry: " + String(e.message || e));
	        }
	      } finally {
	        $("entity-chat-close").disabled = false;
	        chatUiState();
	        const nm = state.manageName;
	        if (nm) { try { await refreshEntityLive(nm); } catch {} }
	      }
	    }

	    // ---- Runs (runtime domain: list / inspect / cancel / steer) ----
	    // Wires the console over runtime's existing verbs (list_runs + the
	    // /commands door: cancel, inject_guidance/steer). Admin-gated section.
	    const _RUN_TERMINAL = new Set(["completed", "failed", "cancelled"]);
	    // ---- Data & Caches (operator priority 18:19, c1580 1b): the ONE
	    // management view over the machine data-home registry. Sizes are live;
	    // purge shows a dry-run accounting first; protected rows render the
	    // owner's refusal VERBATIM instead of a grayed mystery button.
	    // BINARY math with BINARY labels (IEC), and the SAME arithmetic and the
	    // SAME unit strings as console-tui, abstractcode-tui, abstractflow and
	    // @abstractframework/monitor-memory. This is a MEMORY figure first, and
	    // memory is binary everywhere it is configured or reported: this host
	    // reads 137,438,953,472 B = 128.0 GiB exactly, and
	    // `sysctl iogpu.wired_limit_mb=110000` lands on 115,343,360,000 B =
	    // 107.4 GiB. The other four surfaces already divided by 1024 — they
	    // only LABELLED the result `GB`. So the math here moves to binary and
	    // the labels there move to `iB`; after this the same byte count renders
	    // the same string on all five surfaces. Do not "simplify" one of them
	    // back to 1e9: that is exactly the divergence this replaced (89.99 GB
	    // on the web vs 83.8 GB in the TUIs for one 89,986,353,824 B GGUF).
	    function _fmtBytes(n) {
	      if (n === null || n === undefined) return "?";
	      const KiB = 1024, MiB = 1024 * KiB, GiB = 1024 * MiB, TiB = 1024 * GiB;
	      if (n >= TiB) return (n / TiB).toFixed(1) + " TiB";
	      if (n >= GiB) return (n / GiB).toFixed(1) + " GiB";
	      if (n >= MiB) return (n / MiB).toFixed(1) + " MiB";
	      if (n >= KiB) return (n / KiB).toFixed(1) + " KiB";
	      return String(n) + " B";
	    }
	    // ---- Runtimes: master -> tabbed detail.
	    // The master table is height-
	    // bounded (CSS .table-scroll) and row-click selects; ONE detail pane
	    // below carries [Runs | Sessions | Caches]. The default runtime
	    // auto-selects on tab open so the operator's run machinery stays
	    // zero clicks away. Every detail loader is guarded by a selection
	    // token (the manageToken precedent): a stale response must never
	    // render under another runtime's header. ----
		    // Console-TUI mirror: two tabs, and NO selection
	    // persistence across page loads — nothing loads until the operator
	    // clicks a runtime this session.
	    const RUNTIME_SUBTABS = ["sessions", "artifacts", "caches", "logs"];
	    function _runtimeKeyOf(r) {
	      return `${r.kind}|${r.tenant_id || "default"}|${r.runtime_id}`;
	    }
	    async function loadRuntimes() {
	      const body = $("runtimes-table");
	      if (!body) return;
	      // Loading row (a header-only table reads as
	      // BROKEN while the size walk runs — say what is happening).
	      body.textContent = "";
	      const loadingTr = document.createElement("tr");
	      const loadingTd = document.createElement("td");
	      loadingTd.colSpan = 6;
	      loadingTd.className = "empty";
	      loadingTd.textContent = "Scanning execution planes…";
	      loadingTr.append(loadingTd);
	      body.append(loadingTr);
	      try {
	        const data = await api("/api/gateway/admin/runtimes");
	        const rows = Array.isArray(data.runtimes) ? data.runtimes : [];
	        state.runtimeRows = rows;
	        // One cheap GET feeds every row's policy badge (never N+1) and the
	        // policy modal's cached state.
	        try {
	          const cfg = await api("/api/gateway/admin/runtime-config");
	          const map = cfg?.user_workspace_policies?.value;
	          state.userPolicyKeys = new Set(map && typeof map === "object" ? Object.keys(map) : []);
	          renderRuntimeConfig(cfg);
	        } catch (e) {
	          state.userPolicyKeys = state.userPolicyKeys || new Set();
	        }
	        $("runtimes-message").textContent = (data.warnings || []).join(" · ");
	        body.textContent = "";
	        for (const r of rows) {
	          const tr = document.createElement("tr");
	          if (r.error) {
	            const td = document.createElement("td");
	            td.colSpan = 6;
	            td.className = "message";
	            td.textContent = `${r.runtime_id || "?"}: ${r.error}`;
	            tr.append(td);
	            body.append(tr);
	            continue;
	          }
	          // The ROW is the selector (the old per-row "Runs" button died
	          // with the stacked sections). Keyboard path kept: the row is
	          // focusable and Enter/Space select.
	          tr.className = "row-selectable";
	          tr.dataset.rtkey = _runtimeKeyOf(r);
	          tr.tabIndex = 0;
	          tr.setAttribute("role", "button");
	          tr.setAttribute("aria-label", `Open runtime ${r.runtime_id}`);
	          tr.onclick = () => selectRuntime(r);
	          tr.onkeydown = (ev) => {
	            if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectRuntime(r); }
	          };
	          if (r.note) tr.title = r.note;
	          const owners = (r.owners || []).map((o) => o.user_id + ((o.enabled === false) ? " (disabled)" : "")).join(", ");
	          const name = document.createElement("td");
	          name.innerHTML = `<code>${esc(r.runtime_id || "")}</code>`;
	          tr.append(name);
	          const kind = document.createElement("td");
	          kind.textContent = r.kind || "";
	          tr.append(kind);
	          const owner = document.createElement("td");
	          owner.textContent = owners || (r.materialized === false ? "(not materialized yet)" : "");
	          if (r.note) {
	            const note = document.createElement("span");
	            note.className = "muted";
	            note.textContent = " · " + r.note;
	            owner.append(note);
	          }
	          tr.append(owner);
	          // State: entities carry state+liveness; user/default planes don't.
	          const stTd = document.createElement("td");
	          if (r.kind === "entity") {
	            const b = document.createElement("span");
	            const stopped = r.liveness === "stopped";
	            b.className = "entity-live-badge " + (stopped ? "phase-stopped" : "phase-sleep");
	            b.textContent = stopped ? "STOPPED" : (r.state === "awake" || !r.state ? "resting" : r.state);
	            stTd.append(b);
	          } else {
	            stTd.textContent = "—";
	          }
	          tr.append(stTd);
	          const size = document.createElement("td");
	          size.textContent = (typeof r.size_bytes === "number") ? _fmtBytes(r.size_bytes) : "";
	          if (r.size_note) size.title = r.size_note;
	          tr.append(size);
	          // Workspace policy cell. The DEFAULT row is the gateway itself —
	          // its button edits the gateway-wide defaults. User rows with
	          // exactly ONE enabled owner edit that user's policy; multi-owner
	          // and ownerless planes have no single principal to configure.
	          const policyTd = document.createElement("td");
	          const enabledOwners = (r.owners || []).filter((o) => o && o.enabled !== false && o.user_id);
	          if (r.kind === "default") {
	            const gear = document.createElement("button");
	            gear.className = "secondary";
	            // ICONS.gear inline: boot-time icon hydration only touches
	            // static markup — dynamic rows must carry the SVG themselves
	            // or they regress to the tiny platform glyph.
	            gear.innerHTML = `<span class="button-icon" aria-hidden="true">${ICONS.gear}</span><span>Gateway defaults</span>`;
	            gear.title = "Edit the gateway-wide workspace defaults every user inherits";
	            gear.setAttribute("aria-label", "Configure gateway workspace defaults");
	            gear.onclick = (ev) => { ev.stopPropagation(); openGatewayPolicyModal(); };
	            policyTd.append(gear);
	          } else if (r.kind === "user" && enabledOwners.length === 1) {
	            const target = { tenant_id: r.tenant_id || "default", user_id: enabledOwners[0].user_id };
	            const key = _wspPolicyKeyOf(target);
	            const badge = document.createElement("span");
	            badge.className = "wsp-badge" + (state.userPolicyKeys?.has(key) ? " custom" : "");
	            badge.dataset.wspkey = key;
	            badge.textContent = state.userPolicyKeys?.has(key) ? "custom" : "inherited";
	            const gear = document.createElement("button");
	            gear.className = "secondary";
	            gear.innerHTML = `<span class="button-icon" aria-hidden="true">${ICONS.gear}</span><span>Configure</span>`;
	            gear.title = `Configure where ${target.user_id}'s agents may read and write`;
	            gear.setAttribute("aria-label", `Workspace policy for ${target.user_id}`);
	            gear.onclick = (ev) => { ev.stopPropagation(); openWorkspacePolicyModal(target); };
	            policyTd.append(gear, document.createTextNode(" "), badge);
	          } else if (r.kind === "entity") {
	            policyTd.innerHTML = `<span class="muted" title="Entity filesystem access is configured on the entity itself (workspace mounts, Accounts tab)">via entity</span>`;
	          } else {
	            policyTd.textContent = "—";
	            policyTd.title = enabledOwners.length > 1
	              ? "Several users bind this plane — set each user's policy from the Users table"
	              : "No live user binds this plane";
	          }
	          tr.append(policyTd);
	          body.append(tr);
	        }
	        if (!rows.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 6;
	          td.className = "empty";
	          td.textContent = "No runtimes found.";
	          tr.append(td);
	          body.append(tr);
	        }
	        // Selection resolve (console-TUI mirror): ONLY an
	        // in-session choice survives a list refresh — the page never
	        // auto-selects, so nothing loads until the operator clicks a
	        // runtime. `preserve` keeps an unchanged selection's open panel
	        // intact (a list refresh must not blow away the open tab
	        // mid-read).
	        const curKey = state.selectedRuntime ? _runtimeKeyOf(state.selectedRuntime) : "";
	        const pick = curKey ? rows.find((r) => !r.error && _runtimeKeyOf(r) === curKey) || null : null;
	        if (pick) selectRuntime(pick, { preserve: true });
	        else _clearRuntimeSelection();
	      } catch (e) {
	        // The failure lands IN the table (one error surface, entities-row
	        // pattern) — a header-only table must never be the failure render.
	        body.textContent = "";
	        const tr = document.createElement("tr");
	        const td = document.createElement("td");
	        td.colSpan = 6;
	        td.className = "message error";
	        td.textContent = "Runtime inventory unavailable: " + (e.message || e);
	        tr.append(td);
	        body.append(tr);
	        $("runtimes-message").textContent = "";
	      }
	    }
	    function _highlightSelectedRuntimeRow() {
	      const body = $("runtimes-table");
	      if (!body) return;
	      const key = state.selectedRuntime ? _runtimeKeyOf(state.selectedRuntime) : "";
	      let selected = null;
	      for (const tr of body.querySelectorAll("tr")) {
	        const on = Boolean(key) && tr.dataset.rtkey === key;
	        tr.classList.toggle("row-selected", on);
	        if (on) selected = tr;
	      }
	      // The bounded table can restore a selection scrolled out of view —
	      // the operator must SEE which row the detail pane belongs to.
	      if (selected) { try { selected.scrollIntoView({ block: "nearest" }); } catch {} }
	    }
	    function _clearRuntimeSelection() {
	      // The TUI's teaching state: the inspect region stays visible with
	      // its one-line instruction; tabs and panels only exist once a
	      // runtime is chosen. Nothing loads in this state.
	      state.selectedRuntime = null;
	      _highlightSelectedRuntimeRow();
	      $("runtime-detail-section").classList.remove("hidden");
	      $("runtime-detail-teach").classList.remove("hidden");
	      $("runtime-detail-tabs").classList.add("hidden");
	      $("runtime-detail-name").textContent = "";
	      $("runtime-detail-sub").textContent = "Select a runtime to inspect.";
	      for (const t of RUNTIME_SUBTABS) $("runtime-panel-" + t).classList.add("hidden");
	    }
	    function selectRuntime(r, opts = {}) {
	      const same = state.selectedRuntime && _runtimeKeyOf(state.selectedRuntime) === _runtimeKeyOf(r);
	      state.selectedRuntime = r;
	      _highlightSelectedRuntimeRow();
	      $("runtime-detail-section").classList.remove("hidden");
	      $("runtime-detail-teach").classList.add("hidden");
	      $("runtime-detail-tabs").classList.remove("hidden");
	      $("runtime-detail-name").textContent = r.runtime_id || "";
	      const bits = [
	        r.kind === "entity"
	          ? `${r.label || r.entity} — the entity's own plane (visits, workflows, reflections run here)`
	          : r.kind === "user"
	            ? `${r.label} — this user's plane (their runs and flows live here)`
	            : "The gateway default runtime (admin plane)",
	      ];
	      if (r.kind === "entity") bits.push(r.liveness === "stopped" ? "STOPPED" : (r.state === "awake" || !r.state ? "resting" : r.state));
	      if (typeof r.size_bytes === "number") bits.push(_fmtBytes(r.size_bytes));
	      if (r.materialized === false) bits.push("not materialized yet");
	      $("runtime-detail-sub").textContent = bits.join(" · ") + ".";
	      // TWO blocks, two tbodies: the default
	      // machinery's table can never render under another plane's header.
	      const isDefault = r.kind === "default";
	      $("runtime-runs-default").classList.toggle("hidden", !isDefault);
	      $("runtime-runs-readonly").classList.toggle("hidden", isDefault);
	      if (same && opts.preserve) return; // unchanged selection: highlight only
	      state.runtimeDetailToken = (state.runtimeDetailToken || 0) + 1;
	      state.runtimeDrill = null;
	      // Choosing a runtime ALWAYS lands on Runs (the
	      // old restore-last-subtab reopened Logs on every selection). Each
	      // tab still loads lazily, only when clicked.
	      openRuntimeSubtab("sessions");
	    }
	    function openRuntimeSubtab(name) {
	      if (!RUNTIME_SUBTABS.includes(name)) name = "sessions";
	      state.runtimeSubtab = name;
	      for (const t of RUNTIME_SUBTABS) {
	        $("runtime-subtab-" + t).classList.toggle("active", t === name);
	        $("runtime-panel-" + t).classList.toggle("hidden", t !== name);
	      }
	      const r = state.selectedRuntime;
	      if (!r) return;
	      // Explicit branches, no catch-all else (an else
	      // silently routes any NEW tab to the wrong loader).
	      if (name === "sessions") {
	        // The TUI's sessions panel: the chosen runtime's runs, session
	        // ids on every row — actionable on the default plane, read-only
	        // elsewhere. Loads ONLY here (lazy, per choice).
	        state.runsOffset = 0;
	        state.runtimeRunsOffset = 0;
	        if (r.kind === "default") loadRuns();
	        else loadRuntimeRuns();
	      } else if (name === "artifacts") {
	        state.artifactsOffset = 0;
	        loadRuntimeArtifacts();
	      } else if (name === "caches") {
	        loadRuntimeCaches();
	      } else if (name === "logs") {
	        loadRuntimeLogs();
	      }
	    }
	    async function _runtimeDrillItems(r, token, offset = 0) {
	      // ONE drill-in payload per (plane, page) feeds the read-only Runs
	      // table; pages of 100 reach every run.
	      const key = `${_runtimeKeyOf(r)}|${offset}`;
	      if (state.runtimeDrill && state.runtimeDrill.key === key) return state.runtimeDrill;
	      const q = `/api/gateway/admin/runtimes/${encodeURIComponent(r.kind)}/${encodeURIComponent(r.tenant_id || "default")}/${encodeURIComponent(r.runtime_id)}/runs?limit=100&offset=${encodeURIComponent(offset)}`;
	      const data = await api(q);
	      if (token !== state.runtimeDetailToken) return null; // stale — dropped
	      const items = Array.isArray(data.items) ? data.items : [];
	      state.runtimeDrill = { key, items, hasMore: data.has_more === true };
	      return state.runtimeDrill;
	    }
	    async function loadRuntimeRuns() {
	      const r = state.selectedRuntime;
	      if (!r) return;
	      const token = state.runtimeDetailToken;
	      const body = $("runtime-detail-runs");
	      tableLoadingRow(body, 5, "Reading this plane's run store…");
	      $("runtime-detail-message").textContent = "";
	      $("runtime-detail-message").className = "message";
	      try {
	        const pageSize = 100;
	        const offset = Math.max(0, state.runtimeRunsOffset || 0);
	        const drill = await _runtimeDrillItems(r, token, offset);
	        if (drill === null || token !== state.runtimeDetailToken) return;
	        const items = drill.items;
	        body.textContent = "";
	        const seen = new Set();
	        for (const it of items) {
	          const tr = document.createElement("tr");
	          const cells = [
	            String(it.run_id || "").slice(0, 8),
	            it.workflow_id || "",
	            it.status || "",
	            it.session_id || "",
	            String(it.updated_at || it.created_at || "").slice(0, 19),
	          ];
	          for (const c of cells) {
	            const td = document.createElement("td");
	            td.textContent = String(c);
	            tr.append(td);
	          }
	          body.append(tr);
	        }
	        for (const it of items) if (it.session_id) seen.add(it.session_id);
	        if (!items.length && offset === 0) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 5;
	          td.className = "empty";
	          td.textContent = "No runs on this runtime yet.";
	          tr.append(td);
	          body.append(tr);
	        } else if (items.length) {
	          $("runtime-detail-message").textContent =
	            `${seen.size} session${seen.size === 1 ? "" : "s"} on this page`;
	        }
	        renderPager($("runtime-runs-pager"), {
	          offset,
	          pageSize,
	          shown: items.length,
	          hasMore: drill.hasMore,
	          onPage: (next) => { state.runtimeRunsOffset = next; loadRuntimeRuns(); },
	        });
	      } catch (e) {
	        if (token !== state.runtimeDetailToken) return;
	        // 409 (maintenance hold) / 404 (not materialized) render verbatim
	        // in the pane — the panel stays open, never a bounce to the list.
	        body.textContent = "";
	        $("runtime-detail-message").textContent = String((e && e.message) || e);
	        $("runtime-detail-message").className = "message error";
	      }
	    }
	    function runtimeConfigSourceText(label, payload) {
	      const source = String(payload?.source || "default");
	      return `${label}: ${source}`;
	    }
	    function runtimeConfigStringValue(payload) {
	      const value = payload?.value;
	      return typeof value === "string" ? value : "";
	    }
	    function renderRuntimeConfig(payload) {
	      // State only: the gateway defaults render nowhere but the modal
	      // (the Runtimes tab is the table + the tabbed
	      // panel, nothing else) — this cache feeds the modal and the
	      // per-row policy badges.
	      state.runtimeConfig = payload || null;
	    }
	    function renderMyWorkspacePolicy(payload) {
	      state.myWorkspacePolicy = payload || null;
	      const entry = payload?.policy || {};
	      const eff = payload?.effective || {};
	      $("my-workspace-mode").value = typeof entry.mode === "string" ? entry.mode : "";
	      const trust = entry.trust_client_launch_folder;
	      $("my-workspace-trust").value = trust === true ? "on" : trust === false ? "off" : "";
	      $("my-workspace-allowed").value = (entry.workspace_allowed_paths || []).join("\\n");
	      $("my-workspace-blocked").value = (entry.workspace_blocked_paths || []).join("\\n");
	      const effMode = eff.mode || "whitelist";
	      const effTrust = eff.trust_client_launch_folder === true;
	      $("my-workspace-policy-current").textContent =
	        `Effective: ${effMode} mode · launch-folder trust ${effTrust ? "on" : "off"} · ` +
	        `${(eff.workspace_allowed_paths || []).length} allowed · ${(eff.workspace_blocked_paths || []).length} refused`;
	    }
	    async function loadMyWorkspacePolicy() {
	      const msg = $("my-workspace-policy-message");
	      msg.textContent = "Loading…";
	      msg.className = "message";
	      try {
	        renderMyWorkspacePolicy(await api("/api/gateway/workspace/policy/self"));
	        msg.textContent = "";
	      } catch (e) {
	        msg.textContent = String((e && e.message) || e);
	        msg.className = "message error";
	      }
	    }
	    async function saveMyWorkspacePolicy(clear = false) {
	      const msg = $("my-workspace-policy-message");
	      msg.textContent = clear ? "Resetting to inherited…" : "Saving…";
	      msg.className = "message";
	      const body = {};
	      if (!clear) {
	        const mode = $("my-workspace-mode").value;
	        if (mode) body.mode = mode;
	        const trust = $("my-workspace-trust").value;
	        if (trust) body.trust_client_launch_folder = trust === "on";
	        const allowed = $("my-workspace-allowed").value.trim();
	        if (allowed) body.workspace_allowed_paths = allowed.split(/\\n+/).map((s) => s.trim()).filter(Boolean);
	        const blocked = $("my-workspace-blocked").value.trim();
	        if (blocked) body.workspace_blocked_paths = blocked.split(/\\n+/).map((s) => s.trim()).filter(Boolean);
	      }
	      try {
	        renderMyWorkspacePolicy(await api("/api/gateway/workspace/policy/self", {
	          method: "PUT",
	          body: JSON.stringify(body),
	        }));
	        msg.textContent = clear ? "Reset — inheriting the gateway defaults." : "Saved.";
	        msg.className = "message ok";
	      } catch (e) {
	        msg.textContent = String((e && e.message) || e);
	        msg.className = "message error";
	      }
	    }
    // ------------------------------------------------------------ Switches
    // The kit's af-switch markup (CONTRACT §2, DESIGN §2): role=switch,
    // aria-checked; unavailable = aria-disabled + a visible reason (never the
    // `disabled` attribute: the reason must stay reachable from the keyboard);
    // busy while saving; a failure reverts and the caller shows the error.
    function afSwitchCreate({ id, label, description = "", ariaLabel = "", checked = false, small = false, unavailableReason = "" }) {
      const button = document.createElement("button");
      button.type = "button";
      button.id = id;
      button.setAttribute("role", "switch");
      button.className = small ? "af-switch af-switch--sm" : "af-switch af-switch--row";
      if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
      button.innerHTML = `<span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">${esc(label)}</span>${description ? `<span class="af-switch__desc">${esc(description)}</span>` : ""}</span>`;
      const reason = document.createElement("span");
      reason.id = `${id}-reason`;
      reason.className = "af-switch__reason";
      reason.hidden = true;
      afSwitchSet(button, { checked, reason: unavailableReason }, reason);
      return { button, reason, nodes: [button, reason] };
    }
    function afSwitchReasonEl(button) {
      return button.__afReason || (button.id ? $(`${button.id}-reason`) : null);
    }
    function afSwitchSet(button, { checked, busy = false, reason } = {}, reasonEl = null) {
      if (!button) return;
      if (reasonEl) button.__afReason = reasonEl;
      if (typeof checked === "boolean") button.setAttribute("aria-checked", checked ? "true" : "false");
      if (busy) button.setAttribute("aria-busy", "true"); else button.removeAttribute("aria-busy");
      if (reason === undefined) return;
      const r = afSwitchReasonEl(button);
      const describedBase = String(button.getAttribute("aria-describedby") || "").split(/\\s+/).filter((x) => x && !x.endsWith("-reason"));
      if (reason) {
        button.setAttribute("aria-disabled", "true");
        button.title = reason;
        if (r) { r.textContent = reason; r.hidden = false; describedBase.push(r.id); }
      } else {
        button.removeAttribute("aria-disabled");
        button.removeAttribute("title");
        if (r) { r.textContent = ""; r.hidden = true; }
      }
      if (describedBase.length) button.setAttribute("aria-describedby", describedBase.join(" ")); else button.removeAttribute("aria-describedby");
    }
    function afSwitchChecked(button) { return button && button.getAttribute("aria-checked") === "true"; }
    // onChange(next) applies the change: return false when it did not apply
    // (an inline confirmation is pending), throw on failure (reverted).
    function afSwitchBind(button, onChange, onError) {
      if (!button) return;
      button.onclick = async () => {
        if (button.getAttribute("aria-disabled") === "true" || button.getAttribute("aria-busy") === "true") return;
        const was = afSwitchChecked(button);
        const next = !was;
        afSwitchSet(button, { checked: next, busy: true });
        let applied = true;
        try {
          applied = (await onChange(next)) !== false;
        } catch (e) {
          applied = false;
          if (onError) onError(e);
        } finally {
          afSwitchSet(button, { busy: false });
        }
        if (!applied && button.getAttribute("aria-busy") !== "true") afSwitchSet(button, { checked: was });
      };
    }
    function inlineState(id, text, tone = "ok", holdMs = 2000) {
      const el = $(id);
      if (!el) return;
      el.textContent = text || "";
      el.className = `inline-state${tone ? ` ${tone}` : ""}`;
      if (el.__t) clearTimeout(el.__t);
      if (text && tone === "ok" && holdMs > 0 && typeof setTimeout === "function") el.__t = setTimeout(() => { el.textContent = ""; }, holdMs);
    }
    // ------------------------------------------------------------ My email
    // Framework backlog 0992 + DESIGN §5/§6: the admin's "Mailboxes for users"
    // switch, and the signed-in user's own email address and mailbox.
    function emailErrorText(e) {
      const d = e && e.detail;
      if (d && typeof d === "object") {
        if (d.message) return String(d.message);
        if (d.cause) return d.fix ? `${d.cause} ${d.fix}` : String(d.cause);
      }
      return String((e && e.message) || e);
    }
    const EMAIL_CAP_TEXT = {
      email: ["Mailboxes are on for all users.", "Mailboxes are off for all users."],
      email_agent_tools: ["Agent email tools are available to users.", "Agent email tools are off for all users."],
      email_recovery: ["Sign-in by email is on.", "Sign-in by email is off."],
    };
    const EMAIL_CAP_IDS = { email: "email-cap-email", email_agent_tools: "email-cap-agent-tools", email_recovery: "email-cap-recovery" };
    function renderEmailCaps(out) {
      const by = {};
      for (const c of (out && out.capabilities) || []) by[c.id] = c;
      for (const [cap, id] of Object.entries(EMAIL_CAP_IDS)) {
        afSwitchSet($(id), { checked: Boolean(by[cap] && by[cap].default) });
      }
    }
    async function loadEmailCaps() {
      try {
        renderEmailCaps(await api("/api/gateway/admin/email/capabilities"));
      } catch (e) {
        inlineState("email-caps-message", emailErrorText(e), "error");
      }
    }
    function bindEmailCaps() {
      for (const [cap, id] of Object.entries(EMAIL_CAP_IDS)) {
        afSwitchBind($(id), async (next) => {
          const out = await api("/api/gateway/admin/email/capabilities", { method: "PUT", body: JSON.stringify({ [cap]: next }) });
          renderEmailCaps(out);
          inlineState("email-caps-message", EMAIL_CAP_TEXT[cap][next ? 0 : 1], "ok", 4000);
          if (cap !== "email_recovery") refreshUsersOnly();
        }, (e) => inlineState("email-caps-message", emailErrorText(e), "error"));
      }
    }
    async function refreshUsersOnly() {
      try {
        if (accountsAdmin()) {
          const out = await api("/api/gateway/admin/users");
          state.users = out.users || [];
        }
        await loadAccounts();
      } catch (e) {
        usersMessage(emailErrorText(e), "error");
      }
    }
    function myEmailMessage(text, tone) {
      inlineState("my-email-message", text, tone || "", tone === "ok" ? 4000 : 0);
    }
    function myEmailSet(id, v) { $(id).value = v === null || v === undefined ? "" : String(v); }
    function myEmailVal(id) { return String($(id).value || "").trim(); }
    function myEmailNum(id) { const v = myEmailVal(id); return v === "" ? null : Number(v); }
    function myEmailShow(id, text) { const el = $(id); if (!el) return; el.textContent = text || ""; el.hidden = !text; }
    function emailAgo(iso) {
      if (!iso) return "";
      const t = Date.parse(iso);
      if (!Number.isFinite(t)) return String(iso);
      const s = Math.max(0, Math.round((Date.now() - t) / 1000));
      if (s < 60) return "just now";
      if (s < 3600) return `${Math.round(s / 60)} min ago`;
      if (s < 86400) return `${Math.round(s / 3600)} h ago`;
      return `${Math.round(s / 86400)} d ago`;
    }
    function emailServersSummary(imap, smtp) {
      const leg = (x) => `${x.host} · ${x.port} · ${String(x.security || "").toUpperCase() === "STARTTLS" ? "STARTTLS" : "SSL"}`;
      return [imap && imap.host ? leg(imap) : "", smtp && smtp.host ? leg(smtp) : ""].filter(Boolean).join("  ·  ");
    }
    // Mailbox tabs (DESIGN-v2 §3): IMAP first and the default, then Google, Microsoft.
    const MY_EMAIL_TABS = ["imap", "google", "microsoft"];
    const MY_EMAIL_SERVER_FIELDS = ["my-email-imap-host", "my-email-imap-port", "my-email-imap-security", "my-email-smtp-host", "my-email-smtp-port", "my-email-smtp-security", "my-email-username"];
    const myEmailUi = { tab: "", discovered: null, discoveredFor: "", oauth: null, edited: new Set(), timer: null, prefilledFor: "", editReg: false, editMailbox: false };
    // ONE address question (DESIGN-v2 §11): at most one editable address field on screen.
    // Card 1 shows the address as text + "Change" (or "Not set yet" + "Set it now"); the
    // mailbox panes show "Mailbox account: x — Use a different account" whenever an address
    // is set; revealing one editable field folds the other back to its read-only line.
    function myEmailAddressRender() {
      const reg = String((state.myEmail && state.myEmail.email_address) || "").trim();
      const editReg = myEmailUi.editReg;
      const editMailbox = myEmailUi.editMailbox && !editReg;
      $("my-email-registered-edit").hidden = !editReg;
      $("my-email-registered-view").hidden = editReg;
      $("my-email-registered-text").textContent = reg ? reg : "Not set yet — connecting a mailbox below sets it.";
      $("my-email-registered-text").classList.toggle("muted", !reg);
      $("my-email-registered-change").textContent = reg ? "Change" : "Set it now";
      const mailboxField = editMailbox || (!reg && !editReg);
      const account = myEmailVal("my-email-address") || reg;
      for (const pane of ["my-email-pane-imap", "my-email-pane-oauth"]) {
        const root = $(pane);
        if (!root || typeof root.querySelector !== "function") continue;
        const line = root.querySelector(".mailbox-address-line");
        const field = root.querySelector(".mailbox-address-field");
        if (line) line.hidden = mailboxField;
        if (field) field.hidden = !mailboxField;
        const text = root.querySelector(".mailbox-address-line__text");
        if (text) text.textContent = account ? `Mailbox account: ${account}` : "Mailbox account: the address above";
      }
    }
    function myEmailEditRegistered() {
      myEmailUi.editReg = true;
      myEmailUi.editMailbox = false;
      myEmailAddressRender();
      try { $("my-email-registered").focus(); } catch {}
    }
    function myEmailUseOtherAccount() {
      myEmailUi.editMailbox = true;
      myEmailUi.editReg = false;
      myEmailAddressRender();
      try { $(myEmailUi.tab === "imap" ? "my-email-address" : "my-email-oauth-address").focus(); } catch {}
    }
    function myEmailSelectTab(tab, focus = false) {
      myEmailUi.tab = tab;
      for (const t of MY_EMAIL_TABS) {
        const b = $(`my-email-tab-${t}`);
        const on = t === tab;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        if (on && focus) { try { b.focus(); } catch {} }
      }
      const oauth = tab !== "imap";
      $("my-email-pane-oauth").hidden = !oauth;
      $("my-email-pane-imap").hidden = oauth;
      $("my-email-pane-oauth").setAttribute("aria-labelledby", `my-email-tab-${oauth ? tab : "google"}`);
      for (const el of Array.from(document.querySelectorAll ? document.querySelectorAll(".email-ms-only") : [])) el.hidden = tab !== "microsoft";
      if (oauth) myEmailRenderOAuthButton();
    }
    function myEmailRenderOAuthButton() {
      const tab = myEmailUi.tab === "microsoft" ? "microsoft" : "google";
      const btn = $("my-email-oauth-start");
      btn.textContent = tab === "microsoft" ? "Sign in with Microsoft" : "Sign in with Google";
      const d = state.myEmail || {};
      const prov = (Array.isArray(d.oauth_providers) ? d.oauth_providers : []).find((p) => p && p.id === tab) || null;
      const ownClient = Boolean(myEmailVal("my-email-oauth-client-id"));
      const name = tab === "microsoft" ? "Microsoft" : "Google";
      const reason = (prov && prov.available === false && !ownClient)
        ? (prov.reason || `No ${name} sign-in client on this gateway: add one under Advanced, or ask your admin.`)
        : "";
      btn.disabled = Boolean(reason) || Boolean(myEmailUi.oauth);
      myEmailShow("my-email-oauth-start-reason", reason);
    }
    function myEmailTabKeydown(event) {
      const order = MY_EMAIL_TABS;
      const i = order.indexOf(myEmailUi.tab);
      let n = null;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") n = order[(i + 1) % order.length];
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") n = order[(i + order.length - 1) % order.length];
      else if (event.key === "Home") n = order[0];
      else if (event.key === "End") n = order[order.length - 1];
      if (n) { event.preventDefault(); myEmailSelectTab(n, true); }
    }
    function renderMyEmail(d) {
      state.myEmail = d || null;
      d = d || {};
      const imap = d.imap || {};
      const smtp = d.smtp || {};
      // 1. Email address (the stored address, not the mailbox: `email_address`).
      if (document.activeElement !== $("my-email-registered")) myEmailSet("my-email-registered", d.email_address || "");
      myEmailShow("my-email-registered-error", "");
      // One email-address model, said on screen (§3 item 7): only when the mailbox is another account.
      const differs = Boolean(d.configured && d.address && d.email_address && String(d.address).toLowerCase() !== String(d.email_address).toLowerCase());
      myEmailShow("my-email-registered-differs", differs ? `Your mailbox is a different account: ${d.address}.` : "");
      if (document.activeElement !== $("my-email-registered")) myEmailUi.editReg = false;
      // 2. Mailbox: connected status, or the tabs.
      const connected = Boolean(d.configured);
      const mailboxesOff = d.email_available === false;
      myEmailShow("my-email-unavailable", mailboxesOff ? "Your admin turned mailboxes off. Your settings are kept." : "");
      $("my-email-connected").hidden = !connected;
      $("my-email-connect").hidden = connected || mailboxesOff;
      $("my-email-disconnect-confirm").hidden = true;
      const st = d.status || {};
      if (connected) {
        const how = d.auth_kind === "oauth2" || d.oauth ? ((d.oauth && d.oauth.provider) === "microsoft" ? "Microsoft" : ((d.oauth && d.oauth.provider) === "google" ? "Google" : "sign-in")) : "password";
        const checked = st.last_ok ? `checked ${emailAgo(st.last_ok)}` : (st.last_test ? `last check ${emailAgo(st.last_test)}` : "not checked yet");
        const paused = d.enabled === false ? " · paused" : "";
        $("my-email-status").textContent = `Connected as ${d.address} · ${how === "password" ? "IMAP" : how} · ${checked}${paused}`;
        const err = st.last_error;
        myEmailShow("my-email-status-error", err ? `${err.cause || err.code}${err.fix ? ` ${err.fix}` : ""}` : "");
      } else {
        const want = d.email_address || "";
        if (!myEmailVal("my-email-address")) myEmailSet("my-email-address", want);
        if (!myEmailVal("my-email-oauth-address")) myEmailSet("my-email-oauth-address", want);
        if (!myEmailUi.tab) myEmailUi.tab = "imap";
        myEmailSelectTab(myEmailUi.tab);
        myEmailPrefillServers();
      }
      myEmailAddressRender();
      // 3. Notifications.
      const n = d.notifications || {};
      const nReason = d.notifications_unavailable_reason || "";
      afSwitchSet($("my-email-notify-job-failed"), { checked: n.job_failed !== false, reason: nReason });
      afSwitchSet($("my-email-notify-approval"), { checked: n.approval_needed !== false, reason: nReason });
      // 4. Agent email tools.
      const at = d.agent_tools || {};
      afSwitchSet($("my-email-agent-tools"), { checked: Boolean(at.on), reason: at.available === false ? (at.unavailable_reason || "Connect a mailbox first.") : "" });
      // 5. Advanced.
      afSwitchSet($("my-email-enabled"), { checked: d.enabled !== false, reason: "" });
      const pol = d.policy || { mode: "allowlist", entries: [] };
      $("my-email-policy-mode").value = pol.mode || "allowlist";
      renderMyEmailPolicyList(pol.entries || []);
      const lim = d.limits || {};
      if (document.activeElement !== $("my-email-per-hour")) myEmailSet("my-email-per-hour", lim.per_hour);
      if (document.activeElement !== $("my-email-per-day")) myEmailSet("my-email-per-day", lim.per_day);
      $("my-email-usage").textContent = lim.per_hour !== undefined ? `${lim.used_last_hour || 0} sent this hour, ${lim.used_last_day || 0} today.` : "";
      if (document.activeElement !== $("my-email-imap-folder")) myEmailSet("my-email-imap-folder", imap.folder || (connected ? "INBOX" : ""));
      $("my-email-imap-folder").disabled = !connected;
      const notices = (Array.isArray(d.notices) ? d.notices : []).concat(d.secret_warning ? [d.secret_warning] : []);
      myEmailShow("my-email-notices", notices.join(" "));
      if (!connected) {
        // A stored (disconnected) configuration counts as the user's own values: kept, never replaced.
        if (imap.host && !myEmailVal("my-email-imap-host")) { myEmailSet("my-email-imap-host", imap.host); myEmailSet("my-email-imap-port", imap.port); $("my-email-imap-security").value = imap.security || "ssl"; ["my-email-imap-host", "my-email-imap-port", "my-email-imap-security"].forEach((f) => myEmailUi.edited.add(f)); }
        if (smtp.host && !myEmailVal("my-email-smtp-host")) { myEmailSet("my-email-smtp-host", smtp.host); myEmailSet("my-email-smtp-port", smtp.port); $("my-email-smtp-security").value = smtp.security || "ssl"; ["my-email-smtp-host", "my-email-smtp-port", "my-email-smtp-security"].forEach((f) => myEmailUi.edited.add(f)); }
      }
    }
    function renderMyEmailPolicyList(entries) {
      const ul = $("my-email-policy-list");
      ul.textContent = "";
      for (const entry of entries) {
        const li = document.createElement("li");
        li.className = "chip";
        const t = document.createElement("span");
        t.textContent = entry;
        const x = document.createElement("button");
        x.type = "button";
        x.className = "chip__remove";
        x.textContent = "×";
        x.setAttribute("aria-label", `Remove ${entry}`);
        x.onclick = () => saveMyEmailPolicy(entries.filter((e) => e !== entry), `${entry} removed.`);
        li.append(t, x);
        ul.append(li);
      }
      if (!entries.length) {
        const li = document.createElement("li");
        li.className = "chip-list__empty";
        li.textContent = $("my-email-policy-mode").value === "denylist" ? "No refused recipients." : "No allowed recipients yet: sends are refused until you add one.";
        ul.append(li);
      }
    }
    async function loadMyEmail() {
      try {
        renderMyEmail(await api("/api/gateway/me/email"));
        myEmailMessage("");
      } catch (e) {
        myEmailMessage(emailErrorText(e), "error");
      }
    }
    async function saveMyEmailAddress() {
      const btn = $("my-email-registered-save");
      const address = myEmailVal("my-email-registered");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      myEmailShow("my-email-registered-error", "");
      try {
        const out = await api("/api/gateway/me/email/address", { method: "PUT", body: JSON.stringify({ address }) });
        if (out && out.schema) renderMyEmail({ ...out, notices: (state.myEmail && state.myEmail.notices) || [] });
        btn.textContent = "Saved";
        if (typeof setTimeout === "function") setTimeout(() => { btn.textContent = "Save"; }, 2000);
        if (!state.myEmail || !state.myEmail.configured) {
          if (!myEmailUi.editMailbox || !myEmailVal("my-email-address")) { myEmailSet("my-email-address", address); myEmailUi.discovered = null; myEmailPrefillServers(); }
          if (!myEmailUi.editMailbox || !myEmailVal("my-email-oauth-address")) myEmailSet("my-email-oauth-address", address);
        }
        state.myEmail = { ...(state.myEmail || {}), email_address: address };
        myEmailUi.editReg = false;
        myEmailAddressRender();
        inlineState("my-email-message", address ? `Saved: sign-in codes and notifications go to ${address}.` : "Saved: no email address.", "ok", 4000);
      } catch (e) {
        myEmailShow("my-email-registered-error", emailErrorText(e));
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      }
    }
    // Server pre-fill (DESIGN-v2 §3): the moment the mailbox address has a domain,
    // the standard imap./smtp.<domain> 993/465 SSL values fill every field the user
    // has not edited; discovery (debounced) then replaces them with its `defaults`.
    function myEmailApplyServers(defaults) {
      const put = (id, v) => { if (!myEmailUi.edited.has(id) && v !== undefined && v !== null) { const el = $(id); el.value = String(v); } };
      put("my-email-imap-host", defaults.imap.host); put("my-email-imap-port", defaults.imap.port); put("my-email-imap-security", defaults.imap.security);
      put("my-email-smtp-host", defaults.smtp.host); put("my-email-smtp-port", defaults.smtp.port); put("my-email-smtp-security", defaults.smtp.security);
      if (defaults.login !== undefined) put("my-email-username", defaults.login);
    }
    function myEmailDomain(address) {
      const at = String(address || "").lastIndexOf("@");
      const dom = at >= 0 ? String(address).slice(at + 1).trim().toLowerCase() : "";
      return dom.includes(".") ? dom : "";
    }
    function myEmailPrefillServers() {
      const address = myEmailVal("my-email-address");
      const domain = myEmailDomain(address);
      if (!domain) { myEmailShow("my-email-servers-source", ""); return; }
      if (myEmailUi.prefilledFor !== address) {
        myEmailUi.prefilledFor = address;
        myEmailApplyServers({ imap: { host: `imap.${domain}`, port: 993, security: "ssl" }, smtp: { host: `smtp.${domain}`, port: 465, security: "ssl" }, login: address });
        myEmailShow("my-email-servers-source", `Standard settings for ${domain} — change them if your provider uses others.`);
      }
      if (myEmailUi.timer) clearTimeout(myEmailUi.timer);
      if (typeof setTimeout === "function") myEmailUi.timer = setTimeout(() => { myEmailUi.timer = null; myEmailDiscover(); }, 400);
    }
    async function myEmailDiscover() {
      const address = myEmailVal("my-email-address");
      if (!myEmailDomain(address)) return null;
      if (myEmailUi.discoveredFor === address && myEmailUi.discovered) return myEmailUi.discovered;
      myEmailUi.discoveredFor = address;
      let out = null;
      try {
        out = await api("/api/gateway/me/email/discover", { method: "POST", body: JSON.stringify({ address }), slow: true });
      } catch (e) {
        if (myEmailUi.discoveredFor === address) myEmailShow("my-email-servers-source", `${emailErrorText(e)} The standard settings stay filled in.`);
        return null;
      }
      if (myEmailUi.discoveredFor !== address) return null; // a newer address was typed
      myEmailUi.discovered = out;
      const def = out && out.defaults;
      if (!def || !def.imap || !def.smtp || typeof def.message !== "string") {
        console.error("AbstractGateway console: POST /me/email/discover answered without `defaults` (gateway-api seam, DESIGN-v2 §6).");
        myEmailShow("my-email-servers-source", "The gateway's discovery answered without server defaults (gateway-api seam). The standard settings stay filled in.");
        return out;
      }
      myEmailApplyServers(def);
      myEmailShow("my-email-servers-source", def.message);
      return out;
    }
    async function connectMyEmail() {
      const btn = $("my-email-connect-go");
      myEmailShow("my-email-connect-error", "");
      const address = myEmailVal("my-email-address");
      const password = String($("my-email-password").value || "");
      if (!address) { myEmailShow("my-email-connect-error", "Enter the mailbox's email address."); return; }
      if (!password) { myEmailShow("my-email-connect-error", "Enter the password (or an app password)."); return; }
      // No display name, and a login only when the user revealed the field and typed another one (§3/§6).
      const body = { address, password, test: true };
      const login = myEmailVal("my-email-username");
      if (!$("my-email-login-field").hidden && login && login !== address) body.username = login;
      const host = (p) => myEmailVal(`my-email-${p}-host`);
      if (host("imap") && host("smtp")) {
        body.imap = { host: host("imap"), port: myEmailNum("my-email-imap-port"), security: $("my-email-imap-security").value };
        body.smtp = { host: host("smtp"), port: myEmailNum("my-email-smtp-port"), security: $("my-email-smtp-security").value };
      }
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "Connecting…";
      try {
        const out = await api("/api/gateway/me/email", { method: "PUT", body: JSON.stringify(body), slow: true });
        $("my-email-password").value = "";
        await loadMyEmail();
        myEmailMessage(`Mailbox connected as ${(out && out.address) || address}.`, "ok");
      } catch (e) {
        const d = e && e.detail;
        myEmailShow("my-email-connect-error", emailErrorText(e));
        if (d && d.reason_code === "email_discovery_failed") {
          try { $("my-email-imap-host").focus(); } catch {}
        }
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.textContent = "Connect";
      }
    }
    async function testMyEmail() {
      const btn = $("my-email-test");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "Testing…";
      try {
        const out = await api("/api/gateway/me/email/test", { method: "POST", slow: true });
        const failed = ["imap", "smtp"].map((k) => out && out[k]).filter((l) => l && l.ok === false);
        await loadMyEmail();
        if (failed.length) myEmailShow("my-email-status-error", `${failed[0].cause}${failed[0].fix ? ` ${failed[0].fix}` : ""}`);
        else myEmailMessage("The mailbox answered: reading and sending work.", "ok");
      } catch (e) {
        myEmailShow("my-email-status-error", emailErrorText(e));
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.textContent = "Test";
      }
    }
    async function disconnectMyEmail() {
      const btn = $("my-email-disconnect-now");
      btn.disabled = true;
      try {
        await api("/api/gateway/me/email", { method: "DELETE" });
        myEmailUi.tab = ""; myEmailUi.discovered = null; myEmailUi.discoveredFor = "";
        await loadMyEmail();
        myEmailMessage("Mailbox disconnected. Your policy and limits are kept.", "ok");
      } catch (e) {
        myEmailShow("my-email-status-error", emailErrorText(e));
      } finally {
        btn.disabled = false;
      }
    }
    async function saveMyEmailPolicy(entries, okText) {
      try {
        const out = await api("/api/gateway/me/email/policy", { method: "PUT", body: JSON.stringify({ mode: $("my-email-policy-mode").value, entries }) });
        const pol = (out && out.policy) || { mode: $("my-email-policy-mode").value, entries };
        state.myEmail = { ...(state.myEmail || {}), policy: pol };
        renderMyEmailPolicyList(pol.entries || entries);
        inlineState("my-email-policy-state", okText || "Saved", "ok");
        return true;
      } catch (e) {
        inlineState("my-email-policy-state", emailErrorText(e), "error");
        return false;
      }
    }
    async function addMyEmailPolicyEntry() {
      const v = myEmailVal("my-email-policy-add");
      if (!v) return;
      const cur = ((state.myEmail && state.myEmail.policy && state.myEmail.policy.entries) || []).slice();
      if (!cur.includes(v)) cur.push(v);
      if (await saveMyEmailPolicy(cur, `${v} added.`)) $("my-email-policy-add").value = "";
    }
    async function saveMyEmailLimits() {
      try {
        const out = await api("/api/gateway/me/email/limits", { method: "PUT", body: JSON.stringify({ per_hour: myEmailNum("my-email-per-hour"), per_day: myEmailNum("my-email-per-day") }) });
        if (out && out.limits) {
          const lim = out.limits;
          $("my-email-usage").textContent = `${lim.used_last_hour || 0} sent this hour, ${lim.used_last_day || 0} today.`;
        }
        inlineState("my-email-limits-state", "Saved", "ok");
      } catch (e) {
        inlineState("my-email-limits-state", emailErrorText(e), "error");
      }
    }
    async function saveMyEmailFolder() {
      const folder = myEmailVal("my-email-imap-folder");
      try {
        await api("/api/gateway/me/email/folder", { method: "PUT", body: JSON.stringify({ folder }) });
        inlineState("my-email-folder-state", "Saved", "ok");
      } catch (e) {
        inlineState("my-email-folder-state", emailErrorText(e), "error");
      }
    }
    // Reset times are the VIEWER's local time (DESIGN-v2 §11): the API's sentence carries the
    // server's clock, so the two limit sentences are written here from the structured fields
    // (an explicit table by reason_code); every other answer is the API's own sentence.
    function emailLocalHHMM(iso) {
      const d = new Date(String(iso || ""));
      if (!Number.isFinite(d.getTime())) return "";
      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }
    function notifyTestSentence(out) {
      const lim = out.limit;
      const at = lim ? emailLocalHHMM(lim.resets_at) : "";
      if (out.reason_code === "rate_limited" && lim && at) {
        return `Not sent: ${lim.window === "day" ? "daily" : "hourly"} limit reached (${lim.used} of ${lim.limit} this ${lim.window === "day" ? "day" : "hour"}) — resets at ${at}.`;
      }
      if (out.reason_code === "queued_behind" && lim && at && typeof out.queued_behind === "number") {
        return `Queued behind ${out.queued_behind} earlier notification${out.queued_behind === 1 ? "" : "s"}; they go out when the limit resets at ${at}.`;
      }
      return out.message;
    }
    async function testMyNotifications() {
      // The answer is always the API's sentence (§3 item 8): "Sent to x@y." /
      // "Not sent: hourly limit reached (20 of 20 this hour) — resets at 14:05." ...
      const btn = $("my-email-notify-test");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      inlineState("my-email-notify-test-state", "Sending…", "");
      try {
        const out = await api("/api/gateway/me/notifications/test", { method: "POST", slow: true });
        if (!out || typeof out.message !== "string" || !out.message) {
          throw new Error("The test answer carries no message (gateway-api seam, DESIGN-v2 §6).");
        }
        inlineState("my-email-notify-test-state", notifyTestSentence(out), out.sent ? "ok" : "error", out.sent ? 6000 : 0);
      } catch (e) {
        inlineState("my-email-notify-test-state", emailErrorText(e), "error");
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
      }
    }
    function bindMyEmailSwitches() {
      const onErr = (e) => myEmailMessage(emailErrorText(e), "error");
      const notify = (key, label) => async (next) => {
        const out = await api("/api/gateway/me/email/notifications", { method: "PUT", body: JSON.stringify({ [key]: next }) });
        if (out && out.schema) renderMyEmail({ ...out, notices: (state.myEmail && state.myEmail.notices) || [] });
        myEmailMessage(`${label} emails are ${next ? "on" : "off"}.`, "ok");
      };
      afSwitchBind($("my-email-notify-job-failed"), notify("job_failed", "“Job failed”"), onErr);
      afSwitchBind($("my-email-notify-approval"), notify("approval_needed", "“Approval needed”"), onErr);
      afSwitchBind($("my-email-agent-tools"), async (next) => {
        await api("/api/gateway/me/email/agent-tools", { method: "PUT", body: JSON.stringify({ enabled: next }) });
        await loadMyEmail();
        myEmailMessage(next ? "Agent email tools are on." : "Agent email tools are off.", "ok");
      }, onErr);
      afSwitchBind($("my-email-enabled"), async (next) => {
        await api("/api/gateway/me/email/enabled", { method: "PUT", body: JSON.stringify({ enabled: next }) });
        await loadMyEmail();
        myEmailMessage(next ? "Your mailbox is active." : "Your mailbox is paused: no watching, sending or notifications. Your settings are kept.", "ok");
      }, onErr);
    }
    async function startMyEmailOAuth() {
      const provider = myEmailUi.tab === "microsoft" ? "microsoft" : "google";
      const body = {
        provider,
        address: myEmailVal("my-email-oauth-address"),
        client_id: myEmailVal("my-email-oauth-client-id"),
        client_secret: String($("my-email-oauth-client-secret").value || ""),
        tenant: provider === "microsoft" ? myEmailVal("my-email-oauth-tenant") : "",
        flow: provider === "microsoft" ? $("my-email-oauth-flow").value : "",
      };
      const prompt = $("my-email-oauth-prompt");
      const start = $("my-email-oauth-start");
      start.disabled = true;
      start.setAttribute("aria-busy", "true");
      let out;
      try {
        out = await api("/api/gateway/me/email/oauth/start", { method: "POST", body: JSON.stringify(body), slow: true });
      } catch (e) {
        start.removeAttribute("aria-busy");
        myEmailRenderOAuthButton();
        prompt.textContent = emailErrorText(e);
        prompt.className = "inline-state error";
        return;
      }
      const flow = { id: out.flow_id, cancelled: false };
      myEmailUi.oauth = flow;
      $("my-email-oauth-cancel").hidden = false;
      prompt.className = "inline-state";
      prompt.textContent = out.flow === "device"
        ? `Open ${out.verification_uri} in any browser and enter the code ${out.user_code}. Waiting for you to approve…`
        : `Open this link in a browser on the gateway's computer: ${out.authorization_url} — waiting for the sign-in…`;
      while (!flow.cancelled) {
        let res;
        try {
          res = await api("/api/gateway/me/email/oauth/finish", { method: "POST", body: JSON.stringify({ flow_id: flow.id, wait_s: 20 }), timeoutMs: 70000 });
        } catch (e) {
          if (!flow.cancelled) { prompt.textContent = emailErrorText(e); prompt.className = "inline-state error"; }
          break;
        }
        if (res && res.pending) continue;
        prompt.textContent = "";
        await loadMyEmail();
        myEmailMessage(`Mailbox connected as ${res.address}.`, "ok");
        break;
      }
      if (myEmailUi.oauth === flow) myEmailUi.oauth = null;
      start.removeAttribute("aria-busy");
      $("my-email-oauth-cancel").hidden = true;
      myEmailRenderOAuthButton();
    }
    async function cancelMyEmailOAuth() {
      const flow = myEmailUi.oauth;
      if (!flow) return;
      flow.cancelled = true;
      try { await api("/api/gateway/me/email/oauth/cancel", { method: "POST", body: JSON.stringify({ flow_id: flow.id }) }); } catch {}
      $("my-email-oauth-prompt").textContent = "Sign-in cancelled.";
      $("my-email-oauth-cancel").hidden = true;
      myEmailUi.oauth = null;
      myEmailRenderOAuthButton();
    }
    function bindMyEmail() {
      $("my-email-refresh").onclick = loadMyEmail;
      $("my-email-registered-save").onclick = saveMyEmailAddress;
      $("my-email-registered-change").onclick = myEmailEditRegistered;
      for (const b of Array.from(document.querySelectorAll ? document.querySelectorAll(".mailbox-address-other") : [])) b.onclick = myEmailUseOtherAccount;
      $("my-email-registered").onkeydown = (event) => { if (event && event.key === "Enter") saveMyEmailAddress(); };
      for (const t of MY_EMAIL_TABS) {
        $(`my-email-tab-${t}`).onclick = () => { myEmailSelectTab(t); if (t === "imap") myEmailPrefillServers(); };
        $(`my-email-tab-${t}`).onkeydown = myEmailTabKeydown;
      }
      $("my-email-address").oninput = () => { myEmailUi.discovered = null; myEmailPrefillServers(); };
      for (const id of MY_EMAIL_SERVER_FIELDS) {
        const mark = () => myEmailUi.edited.add(id);
        $(id).oninput = mark;
        $(id).onchange = mark;
      }
      $("my-email-login-toggle").onclick = () => {
        const field = $("my-email-login-field");
        field.hidden = !field.hidden;
        $("my-email-login-toggle").setAttribute("aria-expanded", field.hidden ? "false" : "true");
        if (!field.hidden) { if (!myEmailVal("my-email-username")) myEmailSet("my-email-username", myEmailVal("my-email-address")); try { $("my-email-username").focus(); } catch {} }
      };
      $("my-email-connect-go").onclick = connectMyEmail;
      $("my-email-password").onkeydown = (event) => { if (event && event.key === "Enter") connectMyEmail(); };
      $("my-email-oauth-client-id").oninput = myEmailRenderOAuthButton;
      $("my-email-oauth-start").onclick = startMyEmailOAuth;
      $("my-email-oauth-cancel").onclick = cancelMyEmailOAuth;
      $("my-email-test").onclick = testMyEmail;
      $("my-email-disconnect").onclick = () => { $("my-email-disconnect-confirm").hidden = false; try { $("my-email-disconnect-cancel").focus(); } catch {} };
      $("my-email-disconnect-cancel").onclick = () => { $("my-email-disconnect-confirm").hidden = true; };
      $("my-email-disconnect-now").onclick = disconnectMyEmail;
      $("my-email-policy-mode").onchange = () => saveMyEmailPolicy(((state.myEmail && state.myEmail.policy && state.myEmail.policy.entries) || []).slice(), "Saved");
      $("my-email-policy-add-button").onclick = addMyEmailPolicyEntry;
      $("my-email-policy-add").onkeydown = (event) => { if (event && event.key === "Enter") addMyEmailPolicyEntry(); };
      $("my-email-per-hour").onchange = saveMyEmailLimits;
      $("my-email-per-day").onchange = saveMyEmailLimits;
      $("my-email-imap-folder").onchange = saveMyEmailFolder;
      $("my-email-notify-test").onclick = testMyNotifications;
      bindMyEmailSwitches();
    }
    function _wspSetMode(mode) {
	      for (const card of document.querySelectorAll("#wsp-mode-cards .wsp-card")) {
	        const input = card.querySelector("input");
	        const on = input.value === (mode || "");
	        input.checked = on;
	        card.classList.toggle("selected", on);
	      }
	      const allowedHint = $("wsp-allowed-hint");
	      if (allowedHint) {
	        allowedHint.textContent = (mode === "blacklist")
	          ? "Not used in the allow-everything posture (kept for when you switch back)."
	          : "Extra folders this user's agents may use, one per line — added on top of the gateway-wide roots.";
	      }
	    }
	    function _wspSelectedMode() {
	      const el = document.querySelector('input[name="wsp-mode"]:checked');
	      return el ? el.value : "";
	    }
	    function _wspLines(id) {
	      const raw = $(id).value.trim();
	      return raw ? raw.split(/\\n+/).map((s) => s.trim()).filter(Boolean) : [];
	    }
	    function _wspPolicyKeyOf(target) {
	      return `${target.tenant_id || "default"}:${target.user_id}`;
	    }
	    function _wspTargetQuery(target) {
	      return `tenant_id=${encodeURIComponent(target.tenant_id || "default")}&user_id=${encodeURIComponent(target.user_id)}`;
	    }
	    function _wspSyncBadges(key, customized) {
	      if (!state.userPolicyKeys) state.userPolicyKeys = new Set();
	      if (customized) state.userPolicyKeys.add(key); else state.userPolicyKeys.delete(key);
	      for (const el of document.querySelectorAll(`[data-wspkey="${CSS.escape(key)}"]`)) {
	        el.textContent = customized ? "custom" : "inherited";
	        el.classList.toggle("custom", customized);
	      }
	    }
	    function _wspApplyKind(kind) {
	      // ONE modal, two subjects: a USER's policy, or the GATEWAY defaults
	      // every user inherits (the defaults are
	      // set in the same modal, not an inline form).
	      state.wspModalKind = kind;
	      const gw = kind === "gateway";
	      $("wsp-card-inherit").classList.toggle("hidden", gw);
	      $("wsp-root-field").classList.toggle("hidden", !gw);
	      $("wsp-reset").classList.toggle("hidden", gw);
	      $("workspace-policy-modal-title").textContent = gw ? "Gateway workspace defaults" : "Workspace policy";
	      $("wsp-trust").options[0].textContent = gw ? "Gateway default (on)" : "Inherit gateway default";
	      $("wsp-overrides").options[0].textContent = gw ? "Gateway default" : "Inherit gateway default";
	      $("wsp-overrides-hint").textContent = gw
	        ? "Not the same as launch-folder trust: trust only covers the one folder an agent is started from. This legacy switch lets ANY client name ANY folder as a workspace and browse server files anywhere — the posture and folder lists above stop applying. Prefer per-user grants over this."
	        : "Not the same as launch-folder trust: trust only covers the one folder an agent is started from. This legacy switch lets this user's clients name ANY folder as a workspace and browse server files anywhere — the posture and folder lists above stop applying. Leave inherited unless an old client depends on it.";
	      $("wsp-whitelist-sub").textContent = gw
	        ? "Every user without their own posture gets: deny everything except the gateway roots and allowed folders — plus the launch folder while trust is on. The shipped default."
	        : "Agents may only work under the gateway roots plus this user's allowed folders — and the launch folder while trust is on.";
	      $("wsp-blacklist-sub").textContent = gw
	        ? "Every user without their own posture may work anywhere on the gateway host except the refused folders. A wide grant — consider per-user instead."
	        : "Agents may work anywhere on the gateway host except the refused folders. The gateway-wide deny list still applies.";
	      $("wsp-allowed-hint").textContent = gw
	        ? "Folders every user's agents may use, one per line — the gateway-wide roots."
	        : "Extra folders this user's agents may use, one per line — added on top of the gateway-wide roots.";
	      $("wsp-blocked-hint").textContent = gw
	        ? "Folders NO agent may ever touch, one per line — the gateway-wide deny list, enforced for every user in every posture."
	        : "Folders this user's agents may never touch, one per line — enforced in every posture.";
	    }
	    function renderWorkspacePolicyModal(payload) {
	      const entry = payload?.policy || {};
	      _wspSetMode(typeof entry.mode === "string" ? entry.mode : "");
	      const trust = entry.trust_client_launch_folder;
	      $("wsp-trust").value = trust === true ? "on" : trust === false ? "off" : "";
	      const overrides = entry.client_workspace_scope_overrides;
	      $("wsp-overrides").value = overrides === true ? "on" : overrides === false ? "off" : "";
	      $("wsp-allowed").value = (entry.workspace_allowed_paths || []).join("\\n");
	      $("wsp-blocked").value = (entry.workspace_blocked_paths || []).join("\\n");
	      const gd = payload?.gateway_defaults || {};
	      const inheritSub = $("wsp-inherit-sub");
	      if (inheritSub) {
	        inheritSub.textContent = `Gateway default: ${gd.mode === "blacklist"
	          ? "allow everything except the refused folders"
	          : "deny everything except the allowed folders"} — launch-folder trust ${gd.trust_client_launch_folder ? "on" : "off"}.`;
	      }
	    }
	    function renderGatewayPolicyModal(payload) {
	      state.runtimeConfig = payload || state.runtimeConfig;
	      _wspSetMode(payload?.workspace_default_mode?.value === "blacklist" ? "blacklist" : "whitelist");
	      // Only STORED choices prefill (writing a resolved env/default value
	      // back would silently promote it to the stored rung).
	      const trustP = payload?.trust_client_launch_folder || {};
	      $("wsp-trust").value = trustP.source === "stored" ? (trustP.value ? "on" : "off") : "";
	      const ovP = payload?.client_workspace_scope_overrides || {};
	      $("wsp-overrides").value = ovP.source === "stored" ? (ovP.value ? "on" : "off") : "";
	      const rootP = payload?.workspace_root || {};
	      $("wsp-root").value = rootP.source === "stored" ? (runtimeConfigStringValue(rootP) || "") : "";
	      $("wsp-root").placeholder = runtimeConfigStringValue(rootP) || "/abs/path/to/default/workspace";
	      $("wsp-allowed").value = runtimeConfigStringValue(payload?.workspace_allowed_paths || payload?.workspace_mounts);
	      $("wsp-blocked").value = runtimeConfigStringValue(payload?.workspace_blocked_paths);
	    }
	    async function openWorkspacePolicyModal(target) {
	      _wspApplyKind("user");
	      state.workspacePolicyTarget = target;
	      const label = (target.tenant_id && target.tenant_id !== "default")
	        ? `${target.tenant_id}/${target.user_id}` : target.user_id;
	      $("workspace-policy-modal-user").textContent =
	        `Where ${label}'s agents may read and write. Inherits the gateway defaults until customized.`;
	      $("wsp-message").textContent = "Loading current policy…";
	      $("wsp-message").className = "message";
	      $("workspace-policy-modal-backdrop").classList.remove("hidden");
	      try {
	        const payload = await api(`/api/gateway/admin/user-workspace-policy?${_wspTargetQuery(target)}`);
	        renderWorkspacePolicyModal(payload);
	        $("wsp-message").textContent = payload.customized
	          ? "This user has a custom policy." : "This user inherits the gateway defaults.";
	      } catch (e) {
	        $("wsp-message").textContent = String((e && e.message) || e);
	        $("wsp-message").className = "message error";
	      }
	    }
	    async function openGatewayPolicyModal() {
	      _wspApplyKind("gateway");
	      state.workspacePolicyTarget = null;
	      $("workspace-policy-modal-user").textContent =
	        "The gateway-wide defaults every user inherits. Per-user overrides (Configure on a runtime row) win over these.";
	      $("wsp-message").textContent = "Loading gateway defaults…";
	      $("wsp-message").className = "message";
	      $("workspace-policy-modal-backdrop").classList.remove("hidden");
	      try {
	        renderGatewayPolicyModal(await api("/api/gateway/admin/runtime-config"));
	        $("wsp-message").textContent = "";
	      } catch (e) {
	        $("wsp-message").textContent = String((e && e.message) || e);
	        $("wsp-message").className = "message error";
	      }
	    }
	    function closeWorkspacePolicyModal() {
	      $("workspace-policy-modal-backdrop").classList.add("hidden");
	      state.workspacePolicyTarget = null;
	    }
	    async function saveWorkspacePolicyModal(reset = false) {
	      $("wsp-message").textContent = reset ? "Resetting to inherited…" : "Saving…";
	      $("wsp-message").className = "message";
	      try {
	        if (state.wspModalKind === "gateway") {
	          // user_workspace_policies is DELIBERATELY absent from this body:
	          // present-but-empty deletes the whole per-user map server-side;
	          // per-user edits ride the single-entry PUT.
	          const trustSel = $("wsp-trust").value;
	          const body = {
	            workspace_default_mode: _wspSelectedMode() || "whitelist",
	            workspace_root: $("wsp-root").value.trim() || null,
	            workspace_allowed_paths: $("wsp-allowed").value.trim(),
	            workspace_blocked_paths: $("wsp-blocked").value.trim(),
	            trust_client_launch_folder: trustSel ? trustSel === "on" : null,
	          };
	          const ov = $("wsp-overrides").value;
	          if (ov) body.client_workspace_scope_overrides = ov === "on";
	          const payload = await api("/api/gateway/admin/runtime-config", {
	            method: "POST",
	            body: JSON.stringify(body),
	          });
	          renderRuntimeConfig(payload);
	          closeWorkspacePolicyModal();
	          return;
	        }
	        const target = state.workspacePolicyTarget;
	        if (!target) return;
	        const policy = {};
	        if (!reset) {
	          const mode = _wspSelectedMode();
	          if (mode) policy.mode = mode;
	          const trust = $("wsp-trust").value;
	          if (trust) policy.trust_client_launch_folder = trust === "on";
	          const overrides = $("wsp-overrides").value;
	          if (overrides) policy.client_workspace_scope_overrides = overrides === "on";
	          const allowed = _wspLines("wsp-allowed");
	          if (allowed.length) policy.workspace_allowed_paths = allowed;
	          const blocked = _wspLines("wsp-blocked");
	          if (blocked.length) policy.workspace_blocked_paths = blocked;
	        }
	        const payload = await api(`/api/gateway/admin/user-workspace-policy?${_wspTargetQuery(target)}`, {
	          method: "PUT",
	          body: JSON.stringify({ policy: reset ? null : policy }),
	        });
	        _wspSyncBadges(_wspPolicyKeyOf(target), payload.customized === true);
	        closeWorkspacePolicyModal();
	      } catch (e) {
	        // The modal stays open: the gateway's refusal names the offending
	        // path/field — the admin fixes it in place.
	        $("wsp-message").textContent = String((e && e.message) || e);
	        $("wsp-message").className = "message error";
	      }
	    }
	    async function ensureDataHomes(force = false) {
	      // ONE cache feeds the machine-wide disclosure AND every runtime's
	      // Caches tab — a purge or explicit refresh invalidates it; a tab
	      // switch never re-walks sizes.
	      if (!force && state.dataHomes) return state.dataHomes;
	      const data = await api("/api/gateway/admin/data-homes");
	      const rows = Array.isArray(data.homes) ? data.homes : [];
	      state.dataHomes = { rows, warnings: data.warnings || [] };
	      return state.dataHomes;
	    }
	    function _samePath(a, b) {
	      // Suffix-tolerant path identity (the registry
	      // stores resolve()d paths, the inventory serves unresolved ones —
	      // macOS /var vs /private/var). Boundary-anchored, both directions;
	      // a mis-bucket moves a row's SHELF, never its identity or blast
	      // radius (purge stays name-keyed with its own confirm).
	      if (!a || !b) return false;
	      if (a === b) return true;
	      return a.endsWith("/" + b.replace(/^[/]+/, "")) || b.endsWith("/" + a.replace(/^[/]+/, ""));
	    }
	    function homeAssociation(h) {
	      // Which runtime shelf does this registered home render under?
	      // Specific-first; anything unclaimed is machine-wide (ALWAYS
	      // reachable through the disclosure — association is presentation).
	      const rows = state.runtimeRows || [];
	      const slug = (h.meta && h.meta.slug) || ((String(h.name || "").match(/^gateway-entity-(.+)-[0-9a-f]{8}$/) || [])[1]);
	      if (slug && rows.some((r) => r.kind === "entity" && r.entity === slug)) return { kind: "entity", key: slug };
	      if (h.kind === "entity-home") return { kind: "machine", key: "" }; // an unknown LIFE never folds under another plane
	      const root = String((h.meta && h.meta.data_root) || "").replace(/[/]+$/, "");
	      const path = String(h.path || "").replace(/[/]+$/, "");
	      const um = (root || path).match(/[/]users[/]([^/]+)[/]([^/]+)(?:[/]runtime)?$/);
	      if (um && rows.some((r) => r.kind === "user" && (r.tenant_id || "default") === um[1] && r.runtime_id === um[2])) {
	        return { kind: "user", key: `${um[1]}|${um[2]}` };
	      }
	      const d = rows.find((r) => r.kind === "default");
	      const droot = String((d && d.data_dir) || "").replace(/[/]+$/, "");
	      // Equality only for the default root (never prefix-match it: the
	      // root CONTAINS users/ and entities/ — a prefix rule would swallow
	      // every other plane's homes).
	      if (droot && _samePath(root, droot)) return { kind: "default", key: "default" };
	      // A data_root that is NOT ours = ANOTHER gateway's home (this
	      // console shows THIS gateway, never its neighbors —
	      // 39 distinct roots live in the machine registry). Only rows with
	      // NO data_root are genuinely machine-shared (hub cache, blocs…).
	      if (root && droot && root.startsWith(droot + "/")) return { kind: "default", key: "default" };
	      if (root) return { kind: "foreign", key: root };
	      return { kind: "machine", key: "" };
	    }
	    function renderDataHomeRow(h, msgEl, sizing = false) {
	      // ONE row renderer for the Cache tab — the purge flow must never
	      // fork. `sizing` = the fast no-walk paint: the size cell spins until
	      // the sized pass replaces the rows. The DESCRIPTION is a visible
	      // sub-line (hover-only was not actionable).
	      const tr = document.createElement("tr");
	      const nameTd = document.createElement("td");
	      const nameLine = document.createElement("div");
	      nameLine.textContent = h.name || "";
	      nameLine.title = `owner: ${h.owner || "?"}`;
	      const descLine = document.createElement("div");
	      descLine.className = "muted";
	      descLine.style.fontSize = "12px";
	      descLine.style.maxWidth = "420px";
	      descLine.textContent = h.description || "";
	      nameTd.append(nameLine, descLine);
	      tr.append(nameTd);
	      const sizeCell = (typeof h.size_bytes === "number")
	        ? _fmtBytes(h.size_bytes)
	        : (h.exists === false ? "missing" : (sizing ? null : _fmtBytes(h.size_bytes)));
	      for (const cell of [h.kind || "", sizeCell]) {
	        const td = document.createElement("td");
	        if (cell === null) {
	          const spin = document.createElement("span");
	          spin.className = "spin-inline";
	          spin.setAttribute("aria-hidden", "true");
	          td.append(spin);
	          td.title = "measuring…";
	        } else {
	          td.textContent = String(cell);
	        }
	        tr.append(td);
	      }
	      const pathTd = document.createElement("td");
	      const code = document.createElement("code");
	      code.textContent = h.path || "";
	      code.title = h.path || "";
	      pathTd.append(code);
	      tr.append(pathTd);
	      const actions = document.createElement("td");
	      // Every row here IS a cache (the tab filters to safe_to_purge — a
	      // cache is a cache): the purge is always offered.
	      const btn = document.createElement("button");
	      btn.className = "danger";
	      btn.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Purge…</span>`;
	      btn.title = "Delete the CONTENTS of this cache; a dry-run accounting is shown first";
	      btn.setAttribute("aria-label", `Purge ${h.name}`);
	      btn.onclick = () => purgeDataHome(h.name, msgEl);
	      actions.append(btn);
	      tr.append(actions);
	      return tr;
	    }
	    async function loadRuntimeCaches(force = false) {
	      const r = state.selectedRuntime;
	      if (!r) return;
	      const token = state.runtimeDetailToken;
	      const body = $("runtime-caches-table");
	      const msg = $("runtime-caches-message");
	      const rem = $("runtime-caches-remainder");
	      msg.textContent = "";
	      msg.className = "message";
	      rem.textContent = "";

	      const renderRows = (rows, warnings, sizing) => {
	        // Filtering is RENDER-ONLY: the toolbar re-runs this with the
	        // cached payload and never re-enters the loader (each load costs
	        // a full registry size walk — debounced keystrokes must not
	        // launch a stampede of 30s walks).
	        state.cachesLast = { rows, warnings, sizing };
	        msg.textContent = [
	          (warnings || []).join(" · "),
	          sizing ? "measuring sizes — the list is complete, numbers are filling in…" : "",
	        ].filter(Boolean).join(" · ");
	        const want =
	          r.kind === "entity" ? { kind: "entity", key: r.entity }
	          : r.kind === "user" ? { kind: "user", key: `${r.tenant_id || "default"}|${r.runtime_id}` }
	          : { kind: "default", key: "default" };
	        let mine = rows.filter((h) => {
	          const a = homeAssociation(h);
	          if (want.kind === "default") {
	            // TUI parity (console-tui data_panel): the default plane is
	            // the gateway process's own home, so its Cache tab ALSO
	            // lists machine-wide stores (model caches, foreign owners —
	            // everything outside user/entity planes). The standalone
	            // machine-wide section was folded into it.
	            return a.kind === "default" || a.kind === "machine";
	          }
	          return a.kind === want.kind && a.key === want.key;
	        });
	        // Path-identity drift belt: if the default
	        // plane claims zero rows while gateway-owned non-entity rows
	        // plainly exist, show them matched-by-owner with the label — an
	        // empty pane lying about reality is worse than a labeled guess.
	        if (!mine.length && r.kind === "default") {
	          mine = rows.filter((h) => h.owner === "abstractgateway" && !(h.meta && h.meta.slug) && h.kind !== "entity-home");
	          if (mine.length) {
	            msg.textContent = [msg.textContent, "#FALLBACK matched by owner — data_root did not string-match this plane (path resolution drift)"]
	              .filter(Boolean).join(" · ");
	          }
	        }
	        // A cache is a cache: this tab lists ONLY
	        // disposable stores that EXIST. Durable homes are not caches
	        // (deliverables → Artifacts tab), logs are not caches (→ Logs
	        // tab), and stale registrations (path gone) get their own
	        // sub-list with Forget — never a "?" masquerading as a cache.
	        const attributed = mine.length;
	        // Hygiene is REGISTRY-WIDE on the gateway's own plane: stale rows
	        // mostly belong to dead test/scratch gateways (foreign roots), and
	        // hiding them would leave no surface to Forget them from.
	        const stale = r.kind === "default"
	          ? rows.filter((h) => h.exists === false)
	          : mine.filter((h) => h.exists === false);
	        const foreignLive = r.kind === "default"
	          ? rows.filter((h) => h.exists !== false && homeAssociation(h).kind === "foreign").length
	          : 0;
	        const live = mine.filter((h) => h.exists !== false);
	        const durable = live.filter((h) => !h.safe_to_purge).length;
	        const logsCount = live.filter((h) => h.safe_to_purge && h.kind === "logs").length;
	        mine = live.filter((h) => h.safe_to_purge === true && h.kind !== "logs");
	        // Dropdown options come from the RENDERED set (not raw rows), so
	        // every option matches something; a vanished selection says so.
	        const optSync = syncDerivedOptions($("runtime-caches-kind"), mine.map((h) => h.kind), "all kinds");
	        const kindFilter = ($("runtime-caches-kind").value || "").trim();
	        const cacheNeedle = makeNeedle($("runtime-caches-search").value);
	        const cacheQuery = cacheNeedle.text;
	        const matched = mine.filter((h) => {
	          if (kindFilter && String(h.kind || "") !== kindFilter) return false;
	          return cacheNeedle.matchesAny([h.name, h.kind, h.path, h.description]);
	        });
	        const filteredOut = mine.length - matched.length;
	        mine = matched;
	        body.textContent = "";
	        for (const h of mine) body.append(renderDataHomeRow(h, msg, sizing));
	        if (!mine.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 5;
	          td.className = "empty";
	          td.textContent = (kindFilter || cacheQuery)
	            ? `No caches match ${[kindFilter && `kind "${kindFilter}"`, cacheQuery && `"${cacheQuery}"`].filter(Boolean).join(" + ")}.`
	            : "No caches on this plane.";
	          tr.append(td);
	          body.append(tr);
	        }
	        if (stale.length) {
	          const sep = document.createElement("tr");
	          const sepTd = document.createElement("td");
	          sepTd.colSpan = 5;
	          sepTd.className = "muted";
	          sepTd.style.paddingTop = "14px";
	          sepTd.innerHTML = `<strong>Stale registrations (${stale.length}, whole machine registry)</strong> — rows whose path no longer exists, mostly left by dead test/scratch gateways. Forget removes the ROW only; disk is untouched.`;
	          sep.append(sepTd);
	          body.append(sep);
	          for (const h of stale) {
	            const tr = document.createElement("tr");
	            for (const cell of [h.name || "", h.kind || "", "missing"]) {
	              const td = document.createElement("td");
	              td.className = "muted";
	              td.textContent = String(cell);
	              tr.append(td);
	            }
	            const pathTd = document.createElement("td");
	            pathTd.className = "muted";
	            pathTd.textContent = h.path || "";
	            tr.append(pathTd);
	            const act = document.createElement("td");
	            const forget = document.createElement("button");
	            forget.className = "secondary";
	            forget.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Forget</span>`;
	            forget.title = "Remove this stale registry row (disk untouched)";
	            forget.setAttribute("aria-label", `Forget ${h.name}`);
	            forget.onclick = () => forgetDataHomes({ name: h.name });
	            act.append(forget);
	            tr.append(act);
	            body.append(tr);
	          }
	        }
	        const notes = [];
	        if (optSync.reset) notes.push(`The "${optSync.lost}" filter no longer matches anything — showing all kinds.`);
	        if (filteredOut > 0) notes.push(`${filteredOut} cache${filteredOut === 1 ? "" : "s"} hidden by the filter.`);
	        if (!sizing && mine.length) {
	          const total = mine.reduce((a, h) => a + (typeof h.size_bytes === "number" ? h.size_bytes : 0), 0);
	          notes.push(`${mine.length} cache${mine.length === 1 ? "" : "s"} · ${_fmtBytes(total)} on disk.`);
	        }
	        if (durable > 0) {
	          notes.push(`${durable} durable store${durable === 1 ? "" : "s"} (deliverables, session history, entity minds) are not caches — deliverables live in the Artifacts tab.`);
	        }
	        if (logsCount > 0) {
	          notes.push(`${logsCount} log home${logsCount === 1 ? "" : "s"} live in the Logs tab.`);
	        }
	        if (foreignLive > 0) {
	          notes.push(`${foreignLive} home${foreignLive === 1 ? "" : "s"} belong to OTHER gateways' data roots on this machine and are not shown.`);
	        }
	        if (stale.length > 1) {
	          notes.push(""); // spacing before the bulk action line
	        }
	        rem.textContent = notes.filter(Boolean).join(" ");
	        if (stale.length > 1) {
	          const bulk = document.createElement("button");
	          bulk.className = "secondary";
	          bulk.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Forget all stale (${stale.length})</span>`;
	          bulk.title = "Remove every stale registry row in one go (disk untouched)";
	          bulk.onclick = () => forgetDataHomes({ all_stale: true });
	          rem.append(document.createElement("br"), bulk);
	        }
	        const rest = rows.length - attributed;
	        if (rest > 0 && r.kind !== "default") {
	          rem.append(document.createTextNode(` ${rest} more home${rest === 1 ? "" : "s"} belong to other planes — the default runtime lists the machine-wide ones.`));
	        }
	      };

	      // The toolbar's re-render hook (render-only, no fetch).
	      state.cachesRerender = () => {
	        if (state.cachesLast) renderRows(state.cachesLast.rows, state.cachesLast.warnings, state.cachesLast.sizing);
	      };
	      try {
	        const cached = !force && state.dataHomes;
	        if (cached) {
	          renderRows(cached.rows, cached.warnings, false);
	          return;
	        }
	        // TWO-PHASE (a 30s blank "measuring" pane is
	        // unacceptable): the row LIST paints instantly from the no-walk
	        // listing — names, kinds, policies are registry facts — while the
	        // size walk runs behind it; the sized pass then replaces the rows.
	        tableLoadingRow(body, 5, "Loading caches…");
	        try {
	          const quick = await api("/api/gateway/admin/data-homes?sizes=0");
	          if (token !== state.runtimeDetailToken) return;
	          renderRows(quick.homes || [], quick.warnings || [], true);
	        } catch (e) {
	          // The fast paint is best-effort — the sized pass below still
	          // owns the final render.
	          tableLoadingRow(body, 5, "Measuring caches…");
	        }
	        const { rows, warnings } = await ensureDataHomes(true);
	        if (token !== state.runtimeDetailToken) return;
	        renderRows(rows, warnings, false);
	      } catch (e) {
	        if (token !== state.runtimeDetailToken) return;
	        body.textContent = "";
	        msg.textContent = "Data homes unavailable: " + (e.message || e);
	        msg.className = "message error";
	      }
	    }
	    async function forgetDataHomes(body) {
	      const msg = $("runtime-caches-message");
	      const label = body.all_stale ? "every stale registration" : `the stale row ${body.name}`;
	      const go = await confirmAction({
	        title: "Forget stale registrations?",
	        message: `This removes ${label} from the data-home registry. Disk is never touched — the rows point at paths that no longer exist.`,
	        confirmLabel: "Forget",
	      });
	      if (!go) return;
	      try {
	        const out = await api("/api/gateway/admin/data-homes/forget", {
	          method: "POST",
	          body: JSON.stringify(body),
	        });
	        msg.textContent = `Forgot ${out.forgotten.length} row${out.forgotten.length === 1 ? "" : "s"}`
	          + ((out.errors || []).length ? ` — errors: ${out.errors.join("; ")}` : "");
	        state.dataHomes = null;
	        await loadRuntimeCaches();
	      } catch (e) {
	        msg.textContent = String((e && e.message) || e);
	        msg.className = "message error";
	      }
	    }
	    async function loadRuntimeLogs() {
	      // The Logs tab (logs are their own category,
	      // and readable). Files across this plane's registered log homes,
	      // newest first; stale log homes get Forget.
	      const r = state.selectedRuntime;
	      if (!r) return;
	      const token = state.runtimeDetailToken;
	      const body = $("runtime-logs-table");
	      const msg = $("runtime-logs-message");
	      const note = $("runtime-logs-note");
	      tableLoadingRow(body, 4, "Listing log files…");
	      msg.textContent = "";
	      msg.className = "message";
	      note.textContent = "";
	      try {
	        const data = await api("/api/gateway/admin/logs");
	        if (token !== state.runtimeDetailToken) return;
	        const homes = Array.isArray(data.homes) ? data.homes : [];
	        // Same plane-attribution rule as the Cache tab: the default plane
	        // owns its homes plus the machine-wide ones.
	        const wantDefault = r.kind === "default";
	        const minePred = (h) => {
	          const a = homeAssociation({ path: h.path, kind: "logs", name: h.home, meta: { data_root: h.data_root || "" } });
	          if (wantDefault) return a.kind === "default" || a.kind === "machine";
	          if (r.kind === "entity") return a.kind === "entity" && a.key === r.entity;
	          return a.kind === "user" && a.key === `${r.tenant_id || "default"}|${r.runtime_id}`;
	        };
	        // Live homes of THIS gateway only — other gateways' log homes never
	        // list here, and stale-row hygiene lives on the Cache tab.
	        const liveHomes = homes.filter((h) => !h.missing).filter(minePred);
	        state.logsLast = { liveHomes, wantDefault };
	        paintLogRows(liveHomes, wantDefault);
	      } catch (e) {
	        if (token !== state.runtimeDetailToken) return;
	        body.textContent = "";
	        msg.textContent = "Logs unavailable: " + (e.message || e);
	        msg.className = "message error";
	      }
	    }
	    function paintLogRows(liveHomes, wantDefault) {
	      // RENDER-ONLY (the toolbar re-runs this; it never refetches).
	      const body = $("runtime-logs-table");
	      const note = $("runtime-logs-note");
	      {
	        body.textContent = "";
	        const allFiles = [];
	        for (const h of liveHomes) for (const f of h.files || []) allFiles.push({ ...f, home: h.home, homePath: h.path });
	        allFiles.sort((a, b) => String(b.modified_at || "").localeCompare(String(a.modified_at || "")));
	        const homeSync = syncDerivedOptions($("runtime-logs-home"), allFiles.map((f) => f.home), "all log homes");
	        const homeFilter = ($("runtime-logs-home").value || "").trim();
	        const logNeedle = makeNeedle($("runtime-logs-search").value);
	        const logQuery = logNeedle.text;
	        const files = allFiles.filter((f) => {
	          if (homeFilter && f.home !== homeFilter) return false;
	          return logNeedle.matches(f.name);
	        });
	        for (const f of files) {
	          // The ROW is the control: click = tail.
	          const tr = document.createElement("tr");
	          tr.className = "row-selectable";
	          tr.tabIndex = 0;
	          tr.setAttribute("role", "button");
	          tr.setAttribute("aria-label", `View ${f.name}`);
	          tr.title = `Click to tail ${f.name}`;
	          tr.onclick = () => viewLogFile(f.home, f.name);
	          tr.onkeydown = (ev) => {
	            if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); viewLogFile(f.home, f.name); }
	          };
	          for (const cell of [f.name, f.home, _fmtBytes(f.size_bytes), (f.modified_at || "").replace("T", " ").slice(0, 19)]) {
	            const td = document.createElement("td");
	            td.textContent = String(cell);
	            tr.append(td);
	          }
	          body.append(tr);
	        }
	        if (!files.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 4;
	          td.className = "empty";
	          td.textContent = (homeFilter || logQuery)
	            ? `No log files match ${[homeFilter && `home "${homeFilter}"`, logQuery && `"${logQuery}"`].filter(Boolean).join(" + ")}.`
	            : (wantDefault
	              ? "No log files yet."
	              : "No log homes on this plane — serving logs live on the gateway default plane.");
	          tr.append(td);
	          body.append(tr);
	        }
	        note.textContent = [
	          homeSync.reset ? `The "${homeSync.lost}" log home is gone — showing all homes.` : "",
	          files.length < allFiles.length ? `${allFiles.length - files.length} file${allFiles.length - files.length === 1 ? "" : "s"} hidden by the filter.` : "",
	          files.length ? "Serving and launcher logs — regenerable text. Purge a log home from the CLI (abstractgateway data purge) if it grows too large." : "",
	        ].filter(Boolean).join(" ");
	      }
	    }
	    // Shared modal discipline: Escape closes via
	    // addEventListener (never document.onkeydown — confirmAction owns and
	    // clobbers that), the confirm layer wins when open, backdrop clicks
	    // within 250ms of opening are ignored (a double-click's second click
	    // lands on the fresh backdrop and closed what it just opened), and
	    // focus lands on the dialog's Close button.
	    const _modalState = {};
	    function _openModal(backdropId, closeFn) {
	      const backdrop = $(backdropId);
	      backdrop.classList.remove("hidden");
	      const openedAt = Date.now();
	      const onKey = (ev) => {
	        if (ev.key !== "Escape") return;
	        const confirm = $("confirm-backdrop");
	        if (confirm && !confirm.classList.contains("hidden")) return;
	        ev.stopPropagation();
	        closeFn();
	      };
	      document.addEventListener("keydown", onKey);
	      _modalState[backdropId] = { onKey, openedAt };
	      backdrop.onclick = (event) => {
	        if (event.target !== backdrop) return;
	        if (Date.now() - openedAt < 250) return;
	        closeFn();
	      };
	      const closeBtn = backdrop.querySelector(".modal-actions button:last-of-type");
	      if (closeBtn) try { closeBtn.focus(); } catch {}
	    }
	    function _closeModal(backdropId) {
	      $(backdropId).classList.add("hidden");
	      const st = _modalState[backdropId];
	      if (st) {
	        document.removeEventListener("keydown", st.onKey);
	        delete _modalState[backdropId];
	      }
	    }
	    async function viewLogFile(home, file) {
	      // A MODAL, not an inline pane (the inline
	      // viewer clogged the page and opened below the fold).
	      state.currentLog = { home, file };
	      $("log-modal-title").textContent = file;
	      $("log-modal-sub").textContent = `from ${home} — newest lines at the bottom`;
	      $("log-modal-status").textContent = "";
	      const pre = $("log-modal-pre");
	      pre.textContent = "Reading tail…";
	      _openModal("log-modal-backdrop", closeLogModal);
	      try {
	        const maxBytes = $("log-modal-tail-size").value || "65536";
	        const data = await api(`/api/gateway/admin/logs/read?home=${encodeURIComponent(home)}&file=${encodeURIComponent(file)}&max_bytes=${encodeURIComponent(maxBytes)}`);
	        pre.textContent = data.content || "(empty file)";
	        $("log-modal-status").textContent = data.truncated
	          ? `showing the last ${_fmtBytes(data.bytes)} — earlier content not loaded (pick a bigger window to see more)`
	          : `whole file (${_fmtBytes(data.bytes)})`;
	        pre.scrollTop = pre.scrollHeight;
	      } catch (e) {
	        pre.textContent = String((e && e.message) || e);
	      }
	    }
	    function closeLogModal() {
	      state.currentLog = null;
	      _closeModal("log-modal-backdrop");
	    }
	    async function loadRuntimeArtifacts() {
	      // The Artifacts tab: the DELIVERABLES — images,
	      // video, audio, text your agents produced — never conflated with
	      // caches. Row click opens the preview MODAL. NOTE: the artifact
	      // index is the gateway store this session reads (no per-plane
	      // filter on /artifacts/search) — the note line says so.
	      const r = state.selectedRuntime;
	      if (!r) return;
	      const token = state.runtimeDetailToken;
	      // Per-call sequence: debounced searches + modality
	      // changes put several same-token requests in flight — only the
	      // NEWEST may paint.
	      const seq = (state.artifactsSeq = (state.artifactsSeq || 0) + 1);
	      const pageSize = 100;
	      const offset = Math.max(0, state.artifactsOffset || 0);
	      const body = $("runtime-artifacts-table");
	      const msg = $("runtime-artifacts-message");
	      const note = $("runtime-artifacts-note");
	      tableLoadingRow(body, 6, "Loading artifacts…");
	      msg.textContent = "";
	      msg.className = "message";
	      note.textContent = "";
	      const modality = $("runtime-artifacts-modality").value;
	      const query = $("runtime-artifacts-search").value.trim();
	      try {
	        const q = new URLSearchParams({ scope: "all", limit: String(pageSize), offset: String(offset), order_by: "created_at", order: "desc" });
	        if (modality) q.set("modality", modality);
	        if (query) q.set("query", query);
	        const data = await api(`/api/gateway/artifacts/search?${q.toString()}`);
	        if (token !== state.runtimeDetailToken || seq !== state.artifactsSeq) return;
	        const items = Array.isArray(data.items) ? data.items : [];
	        // The post-filter marker is plumbing, not operator information.
	        msg.textContent = (data.warnings || [])
	          .filter((w) => !String(w).includes("query_or_scope_requires_gateway_post_filter"))
	          .join(" · ");
	        body.textContent = "";
	        for (const a of items) {
	          const name = a.filename || a.artifact_id || "";
	          const tr = document.createElement("tr");
	          tr.className = "row-selectable";
	          tr.tabIndex = 0;
	          tr.setAttribute("role", "button");
	          tr.setAttribute("aria-label", `Preview ${name}`);
	          tr.title = `Click to preview ${name}`;
	          tr.onclick = () => openArtifactModal(a);
	          tr.onkeydown = (ev) => {
	            if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openArtifactModal(a); }
	          };
	          const kind = _artifactRenderKind(a);
	          const cells = [
	            name,
	            kind + (a.content_type ? ` (${a.content_type})` : ""),
	            _fmtBytes(a.size_bytes),
	            a.workflow_id || "—",
	            a.run_id ? String(a.run_id).slice(0, 8) : "—",
	            (a.created_at || "").replace("T", " ").slice(0, 19),
	          ];
	          for (const c of cells) {
	            const td = document.createElement("td");
	            td.textContent = String(c);
	            tr.append(td);
	          }
	          body.append(tr);
	        }
	        if (!items.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 6;
	          td.className = "empty";
	          const typeLabel = modality ? $("runtime-artifacts-modality").selectedOptions[0].textContent : "";
	          td.textContent = query
	            ? (modality ? `No ${typeLabel} artifacts match "${query}".` : `No artifacts match "${query}".`)
	            : (modality ? `No ${typeLabel} artifacts yet.` : "No artifacts yet — runs that produce files, images, audio, or video will list them here.");
	          tr.append(td);
	          body.append(tr);
	        }
	        const total = Number(data.total || items.length);
	        renderPager($("runtime-artifacts-pager"), {
	          offset,
	          pageSize,
	          shown: items.length,
	          hasMore: data.has_more === true || offset + items.length < total,
	          total,
	          onPage: (next) => { state.artifactsOffset = next; loadRuntimeArtifacts(); },
	        });
	        note.textContent = "Artifacts are indexed on the gateway store this console session reads (all planes).";
	      } catch (e) {
	        if (token !== state.runtimeDetailToken || seq !== state.artifactsSeq) return;
	        body.textContent = "";
	        msg.textContent = "Artifacts unavailable: " + (e.message || e);
	        msg.className = "message error";
	      }
	    }
	    function _artifactRenderKind(a) {
	      // The STORE's precedence, mirrored (abstractruntime artifacts.py):
	      // render_kind → content_type map → filename
	      // extension. NEVER semantic_kind (it carries non-render values like
	      // "transcript"/"workflow_snapshot").
	      const rk = String(a.render_kind || "").toLowerCase();
	      if (rk) return rk;
	      const ct = String(a.content_type || "").toLowerCase();
	      if (ct.startsWith("image/")) return "image";
	      if (ct.startsWith("video/")) return "video";
	      if (ct.startsWith("audio/")) return "audio";
	      if (ct === "application/json" || ct.endsWith("+json")) return "json";
	      if (ct === "text/markdown") return "markdown";
	      if (ct === "text/html") return "html";
	      if (ct === "application/pdf") return "document";
	      if (ct.startsWith("text/")) return "text";
	      const ext = String(a.filename || "").toLowerCase().split(".").pop() || "";
	      if (["png", "jpg", "jpeg", "gif", "webp", "bmp"].includes(ext)) return "image";
	      if (["mp4", "webm", "mov"].includes(ext)) return "video";
	      if (["mp3", "wav", "ogg", "flac", "m4a"].includes(ext)) return "audio";
	      if (["md", "markdown"].includes(ext)) return "markdown";
	      if (ext === "json") return "json";
	      if (ext === "html" || ext === "htm") return "html";
	      if (["txt", "log", "csv"].includes(ext)) return "text";
	      if (ext === "pdf") return "document";
	      return "binary";
	    }
	    const _ARTIFACT_TEXT_CAP = 1024 * 1024;
	    async function _fetchArtifactText(url) {
	      // Raw fetch, never api() (it forces Accept: json and JSON-parses).
	      // Raced deadline per the console's no-unbounded-fetch doctrine.
	      const controller = typeof AbortController === "function" ? new AbortController() : null;
	      const timer = setTimeout(() => { try { controller && controller.abort(); } catch {} }, 30000);
	      try {
	        const res = await fetch(url, { credentials: "same-origin", signal: controller ? controller.signal : undefined });
	        if (!res.ok) throw new Error(`HTTP ${res.status}`);
	        const text = await res.text();
	        return text.length > _ARTIFACT_TEXT_CAP ? { text: text.slice(0, _ARTIFACT_TEXT_CAP), clipped: true } : { text, clipped: false };
	      } finally {
	        clearTimeout(timer);
	      }
	    }
	    async function openArtifactModal(a) {
	      const name = a.filename || a.artifact_id || "artifact";
	      const kind = _artifactRenderKind(a);
	      $("artifact-modal-title").textContent = name;
	      $("artifact-modal-sub").textContent = [
	        kind,
	        a.content_type || "",
	        typeof a.size_bytes === "number" ? _fmtBytes(a.size_bytes) : "",
	        (a.created_at || "").replace("T", " ").slice(0, 19),
	      ].filter(Boolean).join(" · ");
	      const meta = $("artifact-modal-meta");
	      meta.textContent = "";
	      for (const [k, v] of [["workflow", a.workflow_id], ["run", a.run_id], ["node", a.node_id], ["session", a.session_id], ["task", a.task]]) {
	        if (!v) continue;
	        const chip = document.createElement("span");
	        chip.className = "muted";
	        chip.style.marginRight = "12px";
	        chip.textContent = `${k}: ${v}`;
	        meta.append(chip);
	      }
	      // The bytes' location on the gateway host (served to admins only).
	      if (a.content_path) {
	        const pathLine = document.createElement("div");
	        pathLine.style.marginTop = "6px";
	        const label = document.createElement("span");
	        label.className = "muted";
	        label.textContent = "path: ";
	        const code = document.createElement("code");
	        code.textContent = a.content_path;
	        code.title = "on-disk location on the gateway host";
	        pathLine.append(label, code);
	        meta.append(pathLine);
	      }
	      const content = $("artifact-modal-content");
	      _clearArtifactModalContent();
	      const rawLink = $("artifact-modal-raw");
	      const hasContent = Boolean(a.run_id && a.artifact_id);
	      rawLink.classList.toggle("hidden", !hasContent);
	      _openModal("artifact-modal-backdrop", closeArtifactModal);
	      if (!hasContent) {
	        content.textContent = "This artifact has no run-scoped content route (no run id) — metadata only.";
	        return;
	      }
	      const base = `/api/gateway/runs/${encodeURIComponent(a.run_id)}/artifacts/${encodeURIComponent(a.artifact_id)}/content`;
	      rawLink.href = `${base}?access=download`;
	      const previewUrl = `${base}?access=preview`;
	      const tooBig = typeof a.size_bytes === "number" && a.size_bytes > _ARTIFACT_TEXT_CAP;
	      try {
	        if (kind === "image" || kind === "video" || kind === "audio") {
	          // Blob-loaded: the content route serves no byte
	          // ranges, so a direct <video src> fails on Safari; a typed Blob
	          // object-URL plays everywhere and octet-stream retypes honestly.
	          content.textContent = "Loading preview…";
	          const res = await fetch(previewUrl, { credentials: "same-origin" });
	          if (!res.ok) throw new Error(`HTTP ${res.status}`);
	          let blob = await res.blob();
	          if (!blob.type || blob.type === "application/octet-stream") {
	            const guess = { image: "image/png", video: "video/mp4", audio: "audio/mpeg" }[kind];
	            blob = blob.slice(0, blob.size, guess);
	          }
	          const url = URL.createObjectURL(blob);
	          state.artifactObjectUrls = state.artifactObjectUrls || [];
	          state.artifactObjectUrls.push(url);
	          content.textContent = "";
	          const el = document.createElement(kind === "image" ? "img" : kind);
	          if (kind !== "image") el.controls = true;
	          el.src = url;
	          if (kind === "image") el.alt = name;
	          content.append(el);
	        } else if (kind === "markdown" || kind === "json" || kind === "text" || kind === "code" || kind === "html") {
	          if (tooBig) {
	            content.textContent = `Too large to preview inline (${_fmtBytes(a.size_bytes)}) — use Open raw.`;
	            return;
	          }
	          content.textContent = "Loading preview…";
	          const { text, clipped } = await _fetchArtifactText(previewUrl);
	          content.textContent = "";
	          if (kind === "markdown") {
	            const div = document.createElement("div");
	            div.className = "artifact-md";
	            div.innerHTML = renderMarkdown(text);
	            content.append(div);
	          } else {
	            const pre = document.createElement("pre");
	            let shown = text;
	            if (kind === "json" && text.length < 512 * 1024) {
	              try { shown = JSON.stringify(JSON.parse(text), null, 2); } catch {}
	            }
	            // html renders as ESCAPED text only: agent-
	            // authored markup must never become live DOM here.
	            pre.textContent = shown;
	            content.append(pre);
	          }
	          if (clipped) {
	            const note = document.createElement("p");
	            note.className = "muted";
	            note.textContent = "Preview clipped at 1 MB — use Open raw for the full file.";
	            content.append(note);
	          }
	        } else {
	          content.textContent = `No inline preview for ${kind} artifacts — use Open raw.`;
	        }
	      } catch (e) {
	        content.textContent = "Preview failed: " + ((e && e.message) || e);
	      }
	    }
	    function _clearArtifactModalContent() {
	      const content = $("artifact-modal-content");
	      // Stop playback BEFORE removal (hidden modals
	      // keep playing) and revoke object URLs.
	      for (const el of content.querySelectorAll("video, audio")) {
	        try { el.pause(); el.removeAttribute("src"); el.load(); } catch {}
	      }
	      content.textContent = "";
	      for (const url of state.artifactObjectUrls || []) {
	        try { URL.revokeObjectURL(url); } catch {}
	      }
	      state.artifactObjectUrls = [];
	    }
	    function closeArtifactModal() {
	      _clearArtifactModalContent();
	      _closeModal("artifact-modal-backdrop");
	    }
	    async function purgeDataHome(name, msgEl) {
	      const msg = msgEl || $("runtime-caches-message");
	      try {
	        // Dry-run first: the confirm dialog shows the REAL accounting.
	        const dry = await api("/api/gateway/admin/data-homes/purge", {
	          method: "POST", body: JSON.stringify({ name, dry_run: true }),
	        });
	        const go = await confirmAction({
	          title: `Purge ${name}?`,
	          message: `This deletes the CONTENTS of ${name}: ${dry.files_deleted} files, ${_fmtBytes(dry.bytes_freed)} freed. The directory itself and its registration survive. This cannot be undone.`,
	          confirmLabel: "Purge",
	        });
	        if (!go) { msg.textContent = "Cancelled."; return; }
	        const done = await api("/api/gateway/admin/data-homes/purge", {
	          method: "POST", body: JSON.stringify({ name, confirm_name: name }),
	        });
	        msg.textContent = `Purged ${name}: ${done.files_deleted} files, ${_fmtBytes(done.bytes_freed)} freed` + ((done.errors || []).length ? ` — errors: ${done.errors.join("; ")}` : "");
	        // The Cache tab is the ONE cache surface — re-render it off the
	        // invalidated shared cache.
	        state.dataHomes = null;
	        if (state.selectedRuntime && state.runtimeSubtab === "caches") await loadRuntimeCaches();
	      } catch (e) {
	        // Registry refusals arrive verbatim (409 detail) — render them.
	        msg.textContent = String((e && e.message) || e);
	      }
	    }

	    async function loadRuns() {
	      const body = $("runs-table");
	      if (!body) return;
	      // Per-call sequence: a debounced search + a Next click put several
	      // requests in flight; only the NEWEST may paint (else page-1 rows
	      // land under a page-2 pager).
	      const seq = (state.runsSeq = (state.runsSeq || 0) + 1);
	      tableLoadingRow(body, 7, "Loading runs…");
	      try {
	        const status = ($("runs-status").value || "").trim();
	        const search = ($("runs-search").value || "").trim();
	        const rootOnly = $("runs-root-only").checked;
	        const runsPageSize = 100;
	        const runsOffset = Math.max(0, state.runsOffset || 0);
	        const q = new URLSearchParams({ limit: String(runsPageSize), offset: String(runsOffset), include_ledger_len: "false", root_only: String(rootOnly) });
	        if (status) q.set("status", status);
	        if (search) q.set("query", search);
        const data = await api("/api/gateway/runs?" + q.toString());
        if (seq !== state.runsSeq) return;  // a newer request owns the table
        const rows = Array.isArray(data.items) ? data.items : (Array.isArray(data.runs) ? data.runs : []);
	        body.textContent = "";
	        for (const r of rows) {
	          const tr = document.createElement("tr");
	          const st = String(r.status || "");
	          for (const cell of [r.run_id || "", r.workflow_id || "", st, r.current_node || "", r.session_id || "", String(r.updated_at || "").slice(0, 19)]) {
	            const td = document.createElement("td");
	            td.textContent = String(cell);
	            tr.append(td);
	          }
	          const actions = document.createElement("td");
	          actions.className = "actions";
	          const inspect = document.createElement("button");
	          inspect.className = "secondary"; inspect.textContent = "Inspect";
	          inspect.onclick = () => inspectRun(r.run_id);
	          actions.append(inspect);
	          if (!_RUN_TERMINAL.has(st)) {
	            const steer = document.createElement("button");
	            steer.className = "secondary"; steer.textContent = "Steer";
	            steer.onclick = () => steerRun(r.run_id);
	            const cancel = document.createElement("button");
	            cancel.className = "danger"; cancel.textContent = "Cancel";
	            cancel.onclick = () => cancelRun(r.run_id);
	            actions.append(steer, cancel);
	          }
	          tr.append(actions);
	          body.append(tr);
	        }
	        if (!rows.length) {
	          const tr = document.createElement("tr");
	          const td = document.createElement("td");
	          td.colSpan = 7;
	          td.className = "empty";
	          td.textContent = search
	            ? (status ? `No ${status} runs match "${search}".` : `No runs match "${search}".`)
	            : (status ? `No ${status} runs.` : "No runs yet.");
	          tr.append(td); body.append(tr);
	        }
	        renderPager($("runs-pager"), {
	          offset: runsOffset,
	          pageSize: runsPageSize,
	          shown: rows.length,
	          hasMore: data.has_more === true,
	          onPage: (next) => { state.runsOffset = next; loadRuns(); },
	        });
	        // #TRUNCATION rides through verbatim: a cost-capped scan must never
	        // look like the end of the list.
	        $("runs-message").textContent = (data.warnings || []).join(" · ");
	        $("runs-message").className = "message";
	      } catch (e) {
	        if (seq !== state.runsSeq) return;
	        $("runs-message").textContent = String(e.message || e);
	        $("runs-message").className = "message error";
	      }
	    }
	    async function inspectRun(runId) {
	      // A MODAL (the old inline div rendered below
	      // the table's fold and looked like the button did nothing).
	      const kv = $("run-modal-kv");
	      const raw = $("run-modal-raw");
	      $("run-modal-title").textContent = `Run ${String(runId).slice(0, 12)}`;
	      $("run-modal-sub").textContent = "";
	      kv.textContent = "Loading run…";
	      raw.textContent = "";
	      _openModal("run-modal-backdrop", closeRunModal);
	      try {
	        const r = await api(`/api/gateway/runs/${encodeURIComponent(runId)}`);
	        $("run-modal-sub").textContent = [r.workflow_id, r.status].filter(Boolean).join(" · ");
	        kv.textContent = "";
	        const rows = [
	          ["Run", r.run_id || runId], ["Workflow", r.workflow_id || "—"], ["Status", r.status || "—"],
	          ["Node", r.current_node || ""], ["Session", r.session_id || ""], ["Actor", r.actor_id || ""],
	          ["Waiting", r.waiting ? JSON.stringify(r.waiting).slice(0, 160) : ""], ["Error", r.error || ""],
	          ["Created", String(r.created_at || "").slice(0, 19)], ["Updated", String(r.updated_at || "").slice(0, 19)],
	        ];
	        for (const [k, v] of rows) {
	          if (!v) continue;
	          const line = document.createElement("div"); line.className = "entity-kv";
	          const key = document.createElement("span"); key.className = "entity-kv-key"; key.textContent = k;
	          const val = document.createElement("span"); val.className = "entity-kv-val"; val.textContent = String(v);
	          line.append(key); line.append(val); kv.append(line);
	        }
	        try { raw.textContent = JSON.stringify(r, null, 2); } catch { raw.textContent = String(r); }
	      } catch (e) {
	        kv.textContent = "inspect failed: " + (e.message || e);
	      }
	    }
	    function closeRunModal() {
	      _closeModal("run-modal-backdrop");
	    }
	    // Client-supplied idempotency key for the /commands door (UUID preferred).
	    function cmdId() {
	      return randomId();
	    }
	    async function cancelRun(runId) {
	      const go = await confirmAction({ title: "Cancel run?", message: `Cancel run ${runId}? Any in-flight work stops at the next tick.`, confirmLabel: "Cancel run", danger: true });
	      if (!go) return;
	      try {
	        await api("/api/gateway/commands", { method: "POST", body: JSON.stringify({ command_id: cmdId(), type: "cancel", run_id: runId }) });
	        $("runs-message").textContent = `cancel requested for ${runId}.`;
	        $("runs-message").className = "message";
	        await loadRuns();
	      } catch (e) { $("runs-message").textContent = String(e.message || e); $("runs-message").className = "message error"; }
	    }
    async function steerRun(runId) {
      // Wave 3: the last window.prompt in the console — replaced by the
      // themed modal's input variant (multiline guidance, Escape/Cmd+Enter,
      // never blocks the tab).
      const guidance = await confirmAction({
        title: "Steer run",
        message: `Guidance folds into ${runId}'s next reasoning cycle (durable inbox — delivered at the loop boundary, never lost).`,
        confirmLabel: "Send guidance",
        input: { placeholder: "e.g. focus on the failing test first; prefer minimal diffs" },
      });
      if (guidance === null || !String(guidance).trim()) return;
      try {
        await api("/api/gateway/commands", { method: "POST", body: JSON.stringify({ command_id: cmdId(), type: "inject_guidance", run_id: runId, payload: { guidance: String(guidance).trim() } }) });
        $("runs-message").textContent = `steer sent to ${runId}.`;
        $("runs-message").className = "message";
      } catch (e) { $("runs-message").textContent = String(e.message || e); $("runs-message").className = "message error"; }
    }
	    function csrf() {
	      return document.cookie.split(";").map((p) => p.trim()).find((p) => p.startsWith("abstractgateway_csrf="))?.slice("abstractgateway_csrf=".length) || "";
	    }
    // EVERY gateway call is BOUNDED. fetch() has no default timeout, so a
    // blackholed upstream (packets dropped, no RST — the shape of a stale
    // remote endpoint on a disconnected laptop) leaves the promise pending
    // FOREVER and whatever "Loading..." label the caller set becomes
    // permanent. That is the offline console incident (the
    // operator could configure the gateway offline through the TUI but not
    // the web console, "stuck on loading models"). The console-tui never
    // had this asymmetry — its ureq agents carry timeout_connect(5s) with
    // timeout_read(60s), plus a second slow_agent at 300s for the calls
    // that legitimately run for minutes. Mirror that split here so the
    // browser degrades exactly like the TUI: 60s default, `slow: true` for
    // LLM turns / media generation / TTS / model downloads.
    const API_TIMEOUT_MS = 60000;
    const API_SLOW_TIMEOUT_MS = 300000;
    async function api(path, options = {}) {
      // `slow` and `timeoutMs` are ours, not fetch's — strip them so they
      // never reach the request init. timeoutMs: 0 opts out entirely.
      const { slow = false, timeoutMs, ...init } = options;
      const headers = new Headers(init.headers || {});
      headers.set("Accept", "application/json");
      if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const token = csrf();
      if (token && ["POST", "PUT", "PATCH", "DELETE"].includes(String(init.method || "GET").toUpperCase())) {
        headers.set("X-AbstractGateway-CSRF", decodeURIComponent(token));
      }
      const budget = Number.isFinite(timeoutMs) ? Number(timeoutMs) : (slow ? API_SLOW_TIMEOUT_MS : API_TIMEOUT_MS);
      // The BOUND is a raced deadline, not merely an abort: rejecting is what
      // unsticks the UI, and it must happen even where AbortController is
      // absent. Where it exists we also abort, so the socket is released
      // instead of leaking until the OS gives up.
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timedOut = `Request timed out after ${Math.round(budget / 1000)}s (gateway did not answer ${path}).`;
      let timer = null;
      // The deadline spans the body read too: a gateway that sends headers
      // and then stalls mid-body is as stuck as one that never answers.
      const deadline = budget > 0
        ? new Promise((_, reject) => {
            timer = setTimeout(() => {
              if (controller) { try { controller.abort(); } catch {} }
              reject(new Error(timedOut));
            }, budget);
          })
        : null;
      let res;
      let text;
      try {
        const call = (async () => {
          const r = await fetch(path, { ...init, headers, credentials: "same-origin", ...(controller ? { signal: controller.signal } : {}) });
          return [r, await r.text()];
        })();
        [res, text] = deadline ? await Promise.race([call, deadline]) : await call;
      } catch (e) {
        // Every branch here means the gateway was never reached — what the
        // TUI types as ApiErrorKind::Unreachable and renders as an honest
        // terminal state. Name WHICH one so the operator can tell a wedged
        // gateway (timeout) from a down one (refused/offline); the browser's
        // bare "Failed to fetch" teaches nothing.
        if (e && (e.name === "AbortError" || e.message === timedOut)) throw new Error(timedOut);
        throw new Error(`Gateway unreachable: ${(e && e.message) || e}`);
      } finally {
        if (timer !== null) clearTimeout(timer);
      }
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
      if (!res.ok) {
        // Object details (the B2 refusal contract: {reason_code, message,
        // loop}) must never stringify to [object Object] — the one moment
        // the operator needs the truth is the refusal.
        const detail = data.detail;
        let msg;
        if (detail && typeof detail === "object") msg = detail.message || detail.reason_code || JSON.stringify(detail);
        // Flat refusal envelopes (apps `{ok:false, reason, message, hint}`,
        // network `{refused_reason, fix}`) carry their sentence at the top
        // level: show it, never a bare "HTTP 409".
        else msg = detail || data.message || data.refused_reason || data.reason_code || data.reason || `HTTP ${res.status}`;
        const err = new Error(String(msg));
        if (detail && typeof detail === "object") err.detail = detail;
        err.status = res.status;
        // The parsed body rides along: refusal contracts that answer with a
        // real status but NO detail envelope (the 409 model_locked unload
        // relays the facade payload verbatim) are unreadable from the
        // message alone, and callers must gate on the CODE, not the status.
        err.data = data;
        throw err;
      }
      return data;
    }
	    function setStatus(ok, text) {
	      $("status-dot").className = `dot ${ok ? "ok" : ""}`;
	      $("status-text").textContent = text;
	      $("sign-out").classList.toggle("hidden", !ok);
	      if (typeof islandsSetConnection === "function") islandsSetConnection(ok, text);
	      // Signed out = the assistant has no session to run with; close the
	      // drawer so a stale conversation doesn't sit over the login screen.
	      if (!ok && typeof toggleAssistant === "function" && assistantState.open) toggleAssistant(false);
	    }
	    // ONE status pill (DESIGN §3/§4): "Not signed in" (neutral) / "Signed in
	    // as admin" (ok) / "Token refused" (warn). No duplicate caption.
	    function setLoginStatus(label, tone = "neutral") {
	      const pill = $("login-status");
	      pill.textContent = label;
	      pill.className = `af-gateway-signin__status af-gateway-signin__status--${tone === "err" ? "warn" : tone}`;
	    }
	    function signinShow(id, text) {
	      const el = $(id);
	      if (!el) return;
	      el.textContent = text || "";
	      el.hidden = !text;
	    }
	    function signinFieldError(inputId, errorId, text) {
	      signinShow(errorId, text);
	      const input = $(inputId);
	      if (input && input.setAttribute) {
	        if (text) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
	      }
	    }
	    function signinUnreachableText() {
	      const origin = (typeof location !== "undefined" && location.origin) ? location.origin : "this address";
	      return `Can't reach the gateway at ${origin}. Check that it is running, then try again.`;
	    }
    function confirmAction({ title, message, confirmLabel = "Confirm", danger = false, input = null }) {
      // A second confirm opened while one is pending must not orphan the first
      // promise (its await would hang forever) — resolve the stale one false.
      if (state.confirmResolve) { state.confirmResolve(state.confirmInput ? null : false); state.confirmResolve = null; }
      $("confirm-title").textContent = title;
      $("confirm-message").textContent = message;
      $("confirm-ok").textContent = confirmLabel;
      $("confirm-ok").className = danger ? "danger" : "";
      // INPUT VARIANT (wave 3: the steer prompt() replacement): with
      // `input`, the modal carries a textarea and the promise resolves the
      // TEXT (null on cancel) instead of a boolean — one modal machinery,
      // never window.prompt (blocks the tab, loses theming, single-line).
      const inputEl = $("confirm-input");
      state.confirmInput = Boolean(input);
      if (input) {
        inputEl.value = String((input && input.value) || "");
        inputEl.placeholder = String((input && input.placeholder) || "");
        inputEl.classList.remove("hidden");
      } else if (inputEl) {
        inputEl.classList.add("hidden");
      }
      $("confirm-backdrop").classList.remove("hidden");
      // Keyboard path: Escape cancels; Enter
      // confirms ONLY non-danger acts — destructive confirmation stays a
      // deliberate click. Focus lands on Cancel (the safe default). In
      // input mode plain Enter TYPES (multiline guidance); Cmd/Ctrl+Enter
      // confirms; focus lands in the textarea.
      state.confirmDanger = Boolean(danger);
      document.onkeydown = (event) => {
        if (!state.confirmResolve) return;
        if (event.key === "Escape") { event.preventDefault(); finishConfirm(false); }
        else if (event.key === "Enter" && state.confirmInput) {
          if (event.metaKey || event.ctrlKey) { event.preventDefault(); finishConfirm(true); }
        }
        else if (event.key === "Enter" && !state.confirmDanger) { event.preventDefault(); finishConfirm(true); }
      };
      try { (input ? inputEl : $("confirm-cancel")).focus(); } catch {}
      return new Promise((resolve) => { state.confirmResolve = resolve; });
    }
    function finishConfirm(value) {
      $("confirm-backdrop").classList.add("hidden");
      document.onkeydown = null;
      if (state.confirmResolve) {
        if (state.confirmInput) {
          state.confirmResolve(value ? String($("confirm-input").value || "") : null);
        } else {
          state.confirmResolve(Boolean(value));
        }
      }
      state.confirmResolve = null;
      state.confirmInput = false;
    }
    function parseProviderItems(payload) {
      const names = [];
      const add = (value) => {
        if (typeof value === "string" && value.trim()) names.push(value.trim());
        else if (value && typeof value === "object") {
          const name = value.name || value.provider || value.id || value.provider_id;
          if (typeof name === "string" && name.trim()) {
            const providerName = name.trim();
            names.push(providerName);
            const label = value.display_name || value.label || value.name || providerName;
            if (typeof label === "string" && label.trim() && label.trim() !== providerName) {
              state.providerLabels.set(providerName, `${label.trim()} (${providerName})`);
            } else {
              state.providerLabels.set(providerName, providerName);
            }
          }
        }
      };
      for (const key of ["items", "providers", "available_providers", "provider_details"]) {
        const values = payload?.[key];
        if (Array.isArray(values)) values.forEach(add);
      }
      return [...new Set(names)].sort((a, b) => a.localeCompare(b));
    }
    function parseModelItems(payload) {
      const models = [];
      const add = (value) => {
        if (typeof value === "string" && value.trim()) models.push(value.trim());
        else if (value && typeof value === "object") {
          const id = value.id || value.model || value.name;
          if (typeof id === "string" && id.trim()) models.push(id.trim());
        }
      };
      for (const key of ["items", "models", "available_models", "provider_models"]) {
        const values = payload?.[key];
        if (Array.isArray(values)) values.forEach(add);
      }
      return [...new Set(models)].sort((a, b) => a.localeCompare(b));
    }
    function textValue(value) {
      return typeof value === "string" && value.trim() ? value.trim() : "";
    }
    function arrayValue(value) {
      return Array.isArray(value) ? value : [];
    }
    function objectValue(value) {
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    }
    function dedupeOptionValues(values) {
      const seen = new Set();
      const out = [];
      for (const value of values || []) {
        const clean = textValue(value);
        const key = clean.toLowerCase();
        if (!clean || seen.has(key)) continue;
        seen.add(key);
        out.push(clean);
      }
      return out.sort((a, b) => a.localeCompare(b));
    }
    function catalogProviderFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      return textValue(rec.provider)
        || textValue(rec.provider_id)
        || textValue(rec.provider_name)
        || textValue(rec.backend_id)
        || textValue(rec.backend)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogModelFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      return textValue(rec.model)
        || textValue(rec.model_id)
        || textValue(rec.routed_model)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogVoiceFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      const params = objectValue(rec.params);
      return textValue(rec.voice_id)
        || textValue(params?.voice)
        || textValue(rec.voice)
        || textValue(rec.profile_id)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogLabelFromItem(item, fallback) {
      const rec = objectValue(item);
      return textValue(rec?.label)
        || textValue(rec?.display_name)
        || textValue(rec?.title)
        || textValue(rec?.name)
        || String(fallback || "").trim();
    }
    function providerOptionsFromCatalog(payload, providerKeys = [], mapKeys = []) {
      const out = [];
      const add = (value) => {
        const provider = catalogProviderFromItem(value);
        if (!provider) return;
        const label = catalogLabelFromItem(value, provider);
        state.providerLabels.set(provider, label && label !== provider ? `${label} (${provider})` : provider);
        out.push(provider);
      };
      const items = arrayValue(payload?.items);
      if (items.length) items.forEach(add);
      else {
        for (const key of providerKeys) arrayValue(payload?.[key]).forEach(add);
      }
      for (const key of mapKeys) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        Object.keys(map).forEach(add);
      }
      return dedupeOptionValues(out);
    }
    // Per-provider STATE the catalog reports (cloud voice providers:
    // `needs_key` until a key is configured): a label map for THAT picker
    // only — never written to the global providerLabels, where "openai"
    // also names the text provider.
    function catalogProviderStateLabels(payload) {
      const out = new Map();
      for (const item of arrayValue(payload?.items)) {
        const rec = objectValue(item); const provider = catalogProviderFromItem(item);
        if (!rec || !provider || !rec.needs_key) continue;
        out.set(provider, `${catalogLabelFromItem(item, provider)} — ${textValue(rec.status) || "needs an API key"}`);
      }
      return out;
    }
    function modelOptionsFromCatalog(payload, provider, valueKeys = [], mapKeys = []) {
      const wanted = String(provider || "").trim().toLowerCase();
      const out = [];
      const add = (value, providerFallback = provider) => {
        const itemProvider = (typeof value === "string" ? String(providerFallback || "").trim() : catalogProviderFromItem(value, providerFallback)).toLowerCase();
        if (wanted && itemProvider && itemProvider !== wanted) return;
        const model = catalogModelFromItem(value);
        if (model) out.push(model);
      };
      const items = arrayValue(payload?.items);
      if (items.length) items.forEach((item) => add(item));
      else {
        for (const key of valueKeys) arrayValue(payload?.[key]).forEach((item) => add(item));
      }
      for (const key of mapKeys) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        for (const [mapProvider, values] of Object.entries(map)) {
          arrayValue(values).forEach((item) => add(item, mapProvider));
        }
      }
      return dedupeOptionValues(out);
    }
    function voiceOptionsFromCatalog(payload, provider, model = "") {
      const wantedProvider = String(provider || "").trim().toLowerCase();
      const wantedModel = String(model || "").trim().toLowerCase();
      const out = [];
      const add = (value, providerFallback = provider, modelFallback = "") => {
        const rec = objectValue(value);
        const params = objectValue(rec?.params);
        const tags = objectValue(rec?.tags);
        const itemProvider = (rec
          ? textValue(rec.provider)
            || textValue(rec.provider_id)
            || textValue(rec.engine_id)
            || textValue(rec.engine)
            || textValue(tags?.provider)
            || textValue(params?.provider)
            || textValue(params?.engine)
            || String(providerFallback || "").trim()
          : String(providerFallback || "").trim()).toLowerCase();
        if (wantedProvider && itemProvider && itemProvider !== wantedProvider) return;
        const itemModel = (rec
          ? textValue(rec.model)
            || textValue(rec.model_id)
            || textValue(params?.model)
            || textValue(params?.model_id)
            || textValue(params?.model_filename)
            || String(modelFallback || "").trim()
          : String(modelFallback || "").trim()).toLowerCase();
        if (wantedModel && itemModel && itemModel !== wantedModel) return;
        const voice = catalogVoiceFromItem(value);
        if (!voice) return;
        const label = catalogLabelFromItem(value, voice) || voice;
        state.voiceLabels.set(voice, label);
        out.push(voice);
      };
      for (const key of ["items", "profiles", "voices", "cloned_voices"]) {
        arrayValue(payload?.[key]).forEach((item) => add(item));
      }
      for (const key of ["tts_voices_by_provider", "tts_profiles_by_provider"]) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        for (const [mapProvider, values] of Object.entries(map)) {
          arrayValue(values).forEach((item) => add(item, mapProvider));
        }
      }
      return dedupeOptionValues(out);
    }
    function withQuery(path, params = {}) {
      const query = Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
        .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
        .join("&");
      return query ? `${path}?${query}` : path;
    }
    function defaultCatalogForRow(row) {
      const { kind, modality, task } = defaultRowKindModality(row || {});
      const key = `${kind}.${modality}`;
      if (key === "embedding.text") {
        return {
          scope: "embedding.text",
          providerPath: () => withQuery("/api/gateway/embeddings/models", { providers_only: true }),
          modelPath: (provider) => withQuery("/api/gateway/embeddings/models", { provider }),
          providerKeys: ["providers", "available_providers", "embedding_providers", "provider_details"],
          modelKeys: ["embedding_models", "models", "data", "provider_models"],
          mapKeys: ["models_by_provider", "embedding_models_by_provider"],
          emptyProviders: "No embedding providers discovered",
          emptyModels: "No embedding models discovered",
        };
      }
      if (key === "embedding.image") {
        return textCatalog("image embeddings", { capability_route: "input.image,embedding.image" });
      }
      if (key === "output.image") {
        if (task === "image_to_image") return visionCatalog("image edit", "image_to_image");
        if (task === "image_upscale") return visionCatalog("image restore / upscale", "image_upscale");
        return visionCatalog("image generation", "text_to_image");
      }
      if (key === "output.video") {
        if (task === "image_to_video") return visionCatalog("image to video", "image_to_video");
        return visionCatalog("video generation", "text_to_video");
      }
      if (key === "input.image") {
        return textCatalog("image input", { capability_route: "input.image,output.text" });
      }
      if (key === "input.video") {
        return textCatalog("video input", { capability_route: "input.video,output.text" });
      }
      if (key === "input.sound" || key === "input.audio") {
        return textCatalog("audio input", { capability_route: "input.sound,output.text" });
      }
      if (key === "input.music") {
        return textCatalog("music input", { capability_route: "input.music,output.text" });
      }
      if (key === "output.voice" || key === "output.audio") {
        return {
          scope: "voice generation",
          providerPath: () => withQuery("/api/gateway/voice/voices", { providers_only: true, compact: true }),
          modelPath: (provider) => withQuery("/api/gateway/audio/speech/models", { provider }),
          providerKeys: ["tts_providers", "providers", "available_providers"],
          modelKeys: ["tts_models", "models", "data", "provider_models"],
          mapKeys: ["models_by_provider", "tts_models_by_provider"],
          emptyProviders: "No voice generation providers discovered",
          emptyModels: "No voice generation models discovered",
        };
      }
      if (key === "input.voice") {
        return audioCatalog("speech transcription", "/api/gateway/audio/transcriptions/models", ["stt_providers", "providers", "available_providers"], ["stt_models", "models", "data", "provider_models"], ["models_by_provider", "stt_models_by_provider"]);
      }
      if (key === "output.sound") {
        return {
          scope: "sound effects generation",
          providerPath: () => withQuery("/api/gateway/audio/music/providers", { task: "text_to_audio" }),
          modelPath: (provider) => withQuery("/api/gateway/audio/music/models", { task: "text_to_audio", provider }),
          providerKeys: ["music_providers", "providers", "available_providers", "provider_details"],
          modelKeys: ["music_models", "models", "items", "data", "provider_models"],
          mapKeys: ["models_by_provider", "music_models_by_provider"],
          emptyProviders: "No sound effects providers discovered",
          emptyModels: "No sound effects models discovered",
        };
      }
      // Keyed on MODALITY, not the full key, so the parent row and every
      // `output.scene3d.<task>` row answer the same way.
      if (String(modality || "").toLowerCase() === "scene3d") {
        // NOTHING TO DISCOVER IS A DECLARED STATE, NOT A FALLTHROUGH. There is
        // no scene3d discovery endpoint, and the text catalog is not a stand-in
        // for one: falling through to it offered TEXT providers and TEXT models
        // for a 3D route, and — because the free-text lanes open only when
        // discovery comes back empty — held both lanes SHUT precisely when some
        // text provider happened to be reachable. Saying so outright is what
        // makes the row this console just stopped hiding actually configurable.
        return {
          scope: "3D scene generation",
          discovery: false,
          providerPath: () => "",
          modelPath: () => "",
          providerKeys: [],
          modelKeys: [],
          mapKeys: [],
          emptyProviders: "No 3D scene discovery — type the provider id",
          emptyModels: "No 3D scene discovery — type the model id",
        };
      }
      if (key === "output.music") {
        return {
          scope: "music generation",
          providerPath: () => withQuery("/api/gateway/audio/music/providers", { task: "text_to_music" }),
          modelPath: (provider) => withQuery("/api/gateway/audio/music/models", { task: "text_to_music", provider }),
          providerKeys: ["music_providers", "providers", "available_providers", "provider_details"],
          modelKeys: ["music_models", "models", "items", "data", "provider_models"],
          mapKeys: ["models_by_provider", "music_models_by_provider"],
          emptyProviders: "No music providers discovered",
          emptyModels: "No music models discovered",
        };
      }
      return textCatalog("text generation", { capability_route: "output.text" });
    }
    function textCatalog(scope, filters = {}) {
      return {
        scope,
        providerPath: () => "/api/gateway/discovery/providers",
        modelPath: (provider) => withQuery(`/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models`, filters),
        providerKeys: ["items", "providers", "available_providers", "provider_details"],
        modelKeys: ["models", "items", "data", "provider_models"],
        mapKeys: ["models_by_provider"],
        emptyProviders: "No text providers discovered",
        emptyModels: "No compatible text models discovered",
        useConfiguredProviderFallback: true,
      };
    }
    function visionCatalog(scope, task) {
      return {
        scope,
        providerPath: () => withQuery("/api/gateway/vision/provider_models", { task, providers_only: true }),
        modelPath: (provider) => withQuery("/api/gateway/vision/provider_models", { task, provider }),
        providerKeys: ["providers", "available_providers", "image_providers"],
        modelKeys: ["models", "items", "available_models", "local_models", "provider_models"],
        mapKeys: ["models_by_provider"],
        emptyProviders: `No ${scope} providers discovered`,
        emptyModels: `No ${scope} models discovered`,
      };
    }
	    function audioCatalog(scope, endpoint, providerKeys, modelKeys, mapKeys) {
	      return {
	        scope,
	        providerPath: () => withQuery(endpoint, { providers_only: true }),
        modelPath: (provider) => withQuery(endpoint, { provider }),
        providerKeys,
        modelKeys,
        mapKeys,
        emptyProviders: `No ${scope} providers discovered`,
	        emptyModels: `No ${scope} models discovered`,
	      };
	    }
	    function isVoiceOutputDefault(row) {
	      const { kind, modality } = defaultRowKindModality(row || {});
	      return kind === "output" && (modality === "voice" || modality === "audio");
	    }
	    function isTextGenerationDefault(row) {
	      // The reasoning effort is a property of text generation. `output.text`
	      // is the canonical route and `input.text` is where it is stored, so the
	      // control belongs on both cells.
	      const key = defaultRowKey(row || {});
	      return key === "output.text" || key === "input.text";
	    }
	    function defaultReasoningValue(row) {
	      return textValue((row || {}).reasoning);
	    }
	    function speculationChoice(value) {
	      if (value == null) return "";
	      if (value === false || value?.mode === "off") return "off";
	      if (value && typeof value === "object" && [2, 3, 4, 5].includes(value.num_draft_tokens)) return String(value.num_draft_tokens);
	      return "custom";
	    }
	    function speculationFromChoice(choice, stored, strict = false) {
	      if (!choice) return undefined;
	      if (choice === "off") return false;
	      if (choice === "custom") return stored;
	      return { ...(stored && typeof stored === "object" ? stored : {}), mode: "native_mtp", num_draft_tokens: Number(choice), require_acceleration: strict };
	    }
	    function speculationSummary(result) {
	      const value = result?.speculation;
	      if (!value) return "MTP execution not reported";
	      if (value.used === true) return `MTP used${value.num_draft_tokens ? ` (depth ${value.num_draft_tokens})` : ""}`;
	      return `MTP not used${value.message ? `: ${value.message}` : value.reason ? `: ${value.reason}` : ""}`;
	    }
	    function loadDefaultSpeculation(row) {
	      const label = $("modal-default-speculation-label");
	      const select = $("modal-default-speculation");
	      label.classList.toggle("hidden", !isTextGenerationDefault(row));
	      const choice = speculationChoice(row?.options?.speculation);
	      select.querySelector?.('option[value="custom"]')?.remove();
	      if (choice === "custom") {
	        const option = document.createElement("option"); option.value = "custom"; option.textContent = "custom (preserve configured value)"; select.append(option);
	      }
	      select.value = choice;
	      $("modal-default-speculation-status").textContent = "Configured policy; checking execution support…";
	    }
	    async function refreshDefaultSpeculationSupport() {
	      const row = state.activeDefaultRow;
	      if (!row || !isTextGenerationDefault(row)) return;
	      const provider = activeDefaultProvider(), model = activeDefaultModel();
	      const status = $("modal-default-speculation-status");
	      if (!provider || !model) { status.textContent = "Choose a provider and model to check MTP support."; return; }
	      try {
	        const result = await api(`/api/gateway/discovery/models/capabilities?model_name=${encodeURIComponent(model)}&provider=${encodeURIComponent(provider)}`);
	        if (state.activeDefaultRow !== row || provider !== activeDefaultProvider() || model !== activeDefaultModel()) return;
	        const caps = result.execution?.speculation;
	        status.textContent = caps ? (caps.ready === true ? "MTP ready; the request reports actual execution." : (caps.message || caps.reason || (caps.supported ? "Compatible backend; loading/reloading may be required." : "MTP is unavailable for this backend/model."))) : "Execution support unknown; saving a default does not enable MTP.";
	      } catch (err) {
	        if (state.activeDefaultRow === row) status.textContent = `Execution support unavailable: ${err.message || err}`;
	      }
	    }
	    async function refreshSandboxSpeculationSupport(row) {
	      const select = $("sandbox-speculation");
	      const target = `${row?.provider || ""}/${row?.model || ""}`;
	      if (state.sandboxSpeculationTarget !== target) select.value = "";
	      state.sandboxSpeculationTarget = target;
	      const label = $("sandbox-speculation-label");
	      for (const option of select.options) if (/^[2-5]$/.test(option.value)) option.disabled = true;
	      label.title = "Checking this execution host's MTP support. Off and inherit remain available.";
	      if (!row?.provider || !row?.model) return;
	      try {
	        const result = await api(`/api/gateway/discovery/models/capabilities?model_name=${encodeURIComponent(row.model)}&provider=${encodeURIComponent(row.provider)}`);
	        if (state.sandboxSpeculationTarget !== target) return;
	        const caps = result.execution?.speculation;
	        const depths = caps?.supported === true && Array.isArray(caps.supported_depths) ? caps.supported_depths.map(String) : [];
	        for (const option of select.options) if (/^[2-5]$/.test(option.value)) option.disabled = !depths.includes(option.value);
	        label.title = caps?.message || caps?.reason || (caps?.ready === true ? "MTP ready. An explicit depth requires MTP execution; inherit uses the Core default." : "MTP support is unavailable or requires model loading; inspect model capabilities before overriding.");
	      } catch (err) {
	        if (state.sandboxSpeculationTarget === target) label.title = `MTP support unknown: ${err.message || err}`;
	      }
	    }
	    function loadDefaultReasoning(row) {
	      const label = $("modal-default-reasoning-label");
	      const select = $("modal-default-reasoning");
	      if (!isTextGenerationDefault(row)) {
	        label.classList.add("hidden");
	        select.value = "";
	        return;
	      }
	      label.classList.remove("hidden");
	      const want = defaultReasoningValue(row);
	      select.value = [...select.options].some((o) => o.value === want) ? want : "";
	    }
	    function defaultVoiceValue(row) {
	      const options = row && typeof row.options === "object" && !Array.isArray(row.options) ? row.options : {};
	      return textValue(options.voice) || textValue(options.profile);
	    }
	    function setSelectOptions(select, values, { emptyLabel, disabled = false, selected = "", labelMap = null } = {}) {
	      select.textContent = "";
	      const empty = document.createElement("option");
	      empty.value = "";
	      empty.textContent = emptyLabel || "Select...";
	      select.append(empty);
	      for (const value of values) {
	        const opt = document.createElement("option");
	        opt.value = value;
	        opt.textContent = labelMap?.get(value) || state.providerLabels.get(value) || value;
	        select.append(opt);
	      }
      select.disabled = disabled;
      if (selected && values.includes(selected)) select.value = selected;
      else select.value = "";
    }
    function setEndpointModelOptions(values, selectedValues = []) {
      const select = $("endpoint-models");
      const selected = new Set((Array.isArray(selectedValues) ? selectedValues : []).map((value) => String(value || "").trim()).filter(Boolean));
      const models = [...new Set((Array.isArray(values) ? values : []).map((value) => String(value || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      state.endpointModelOptions = models;
      select.textContent = "";
      if (!models.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "Discover models from the endpoint...";
        opt.disabled = true;
        select.append(opt);
        select.disabled = true;
        updateEndpointModelSummary();
        return;
      }
      for (const model of models) {
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = model;
        opt.selected = selected.has(model);
        select.append(opt);
      }
      select.disabled = false;
      updateEndpointModelSummary();
    }
    function selectedEndpointModels() {
      return [...$("endpoint-models").selectedOptions].map((opt) => opt.value).filter(Boolean);
    }
    function updateEndpointModelSummary() {
      const models = state.endpointModelOptions || [];
      const selectedCount = selectedEndpointModels().length;
      if (!models.length) {
        $("endpoint-model-summary").textContent = "No model list loaded. Save now to use live discovery, or discover models first.";
      } else if (selectedCount) {
        $("endpoint-model-summary").textContent = `${models.length} models available. ${selectedCount} model${selectedCount === 1 ? "" : "s"} restricted by this profile.`;
      } else {
        $("endpoint-model-summary").textContent = `${models.length} models available. This profile will keep live endpoint discovery.`;
      }
    }
	    function providerValueForEndpointProfile(profile) {
	      if (!profile || typeof profile !== "object") return "";
	      const direct = String(profile.provider_id || "").trim();
	      if (direct) return direct;
	      const value = String(profile.virtual_provider || (profile.id ? `endpoint:${profile.id}` : "") || "").trim();
	      return value;
	    }
	    function configuredProviderOptions() {
	      const values = [];
	      state.providerLabels.clear();
	      for (const profile of state.endpointProfiles || []) {
	        if (profile?.enabled === false) continue;
	        const value = providerValueForEndpointProfile(profile);
	        if (!value) continue;
	        const name = String(profile.display_name || profile.id || value).trim();
	        state.providerLabels.set(value, `${name} (${value})`);
	        values.push(value);
	      }
	      return [...new Set(values)].sort((a, b) => {
	        const left = state.providerLabels.get(a) || a;
	        const right = state.providerLabels.get(b) || b;
	        return left.localeCompare(right);
	      });
	    }
	    async function loadProviders() {
	      $("defaults-message").className = "message";
	      state.providers = configuredProviderOptions();
	      setSelectOptions($("modal-default-provider"), state.providers, {
	        emptyLabel: state.providers.length ? "Select provider..." : "Configure a provider first",
	        disabled: !state.providers.length,
	      });
	      setSelectOptions($("modal-default-model"), [], { emptyLabel: "Select provider first", disabled: true });
	      $("defaults-message").textContent = state.providers.length ? "" : "Configure a provider on the Providers tab before setting multimodal capability defaults.";
	      syncSandboxProviderOptions();
	    }
	    async function fetchProviderModels(provider) {
	      if (!provider) return [];
	      const cacheKey = `text::${provider}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(withQuery(`/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models`, { capability_route: "output.text" }));
	        state.providerModels.set(cacheKey, parseModelItems(payload));
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    async function loadModels(provider, selected = "") {
	      await loadDefaultModels(provider, selected, state.activeDefaultRow || null);
	    }
	    async function fetchDefaultProviders(row) {
	      const catalog = defaultCatalogForRow(row || {});
	      // A catalog that declares it has no discovery is answered here, not by
	      // a request to "" — the empty result is what opens the free-text lanes.
	      if (catalog.discovery === false) return [];
	      const path = catalog.providerPath();
	      const cacheKey = `providers::${catalog.scope}::${path}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(path);
	        let providers = providerOptionsFromCatalog(payload, catalog.providerKeys, catalog.mapKeys);
	        state.providerStateLabels.set(catalog.scope, catalogProviderStateLabels(payload));
	        if (!providers.length && catalog.useConfiguredProviderFallback) providers = configuredProviderOptions();
	        state.providerModels.set(cacheKey, providers);
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    async function fetchDefaultModels(provider, row) {
	      if (!provider) return [];
	      const catalog = defaultCatalogForRow(row || {});
	      if (catalog.discovery === false) return [];  // see fetchDefaultProviders
	      const path = catalog.modelPath(provider);
	      const cacheKey = `models::${catalog.scope}::${provider}::${path}`;
	      // The in-flight PROMISE is cached so two openers share one request.
	      // A REJECTED promise must never survive that: cached, it re-throws
	      // forever WITHOUT touching the network, so the select stays stuck
	      // even after the provider comes back — measured in the offline repro:
	      // the retry issued no request at all, and restoring the
	      // network did not heal it; only a full page reload did. Evict on
	      // failure so the next attempt is a REAL retry.
	      let entry = state.providerModels.get(cacheKey);
	      if (entry === undefined) {
	        const promise = api(path).then((payload) => modelOptionsFromCatalog(payload, provider, catalog.modelKeys, catalog.mapKeys));
	        promise.catch(() => { if (state.providerModels.get(cacheKey) === promise) state.providerModels.delete(cacheKey); });
	        state.providerModels.set(cacheKey, promise);
	        entry = promise;
	      }
	      // Awaited through the LOCAL handle, never a fresh cache read: the
	      // eviction above may already have dropped the key, and awaiting the
	      // resulting undefined would swallow the failure as "no models".
	      const cached = await entry;
	      const models = Array.isArray(cached) ? cached : [];
	      state.providerModels.set(cacheKey, models);
	      return models;
	    }
	    async function fetchDefaultVoices(provider, model, row) {
	      if (!provider || !isVoiceOutputDefault(row)) return [];
	      const path = withQuery("/api/gateway/voice/voices", { provider, model, compact: true });
	      const cacheKey = defaultVoicesKey(provider, model);
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(path);
	        state.providerModels.set(cacheKey, voiceOptionsFromCatalog(payload, provider, model));
	        state.voiceReasons.set(cacheKey, voiceUnavailableReason(payload));
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    function defaultVoicesKey(provider, model) {
	      return `voices::${provider}::${model || ""}`;
	    }
	    // WHY a voice listing is empty, in the gateway's / AbstractVoice's own
	    // words ("Supertonic is not installed: … Install it with: …", "OpenAI:
	    // needs an API key (add it under Providers)") — never a bare "no voices".
	    function voiceUnavailableReason(payload) {
	      return textValue(payload?.unavailable_reason) || textValue(payload?.error) || "";
	    }
	    // OFFLINE IS A SUPPORTED MODE, NOT A DEGRADED ONE. A provider or model
	    // field that can only offer *discovered* values is a dead end the moment
	    // discovery cannot reach anything — which is the normal case on a
	    // disconnected machine. The console-TUI has always degraded to a
	    // free-text lane here (console-tui/src/ui/routes.rs:1163-1213, CUSTOM
	    // provider row at :822) and stays savable; the web console refused to
	    // save at all. These two functions ARE that lane, for both fields: it
	    // opens ONLY when discovery has nothing to offer, so a healthy catalog
	    // still steers the operator to real values instead of inviting typos.
	    // One helper rather than one per field on purpose — as two copies the
	    // provider and model lanes had already drifted on the prefill rule.
	    function setCustomLane(id, open, value = "") {
	      const el = $(id);
	      if (!el) return;
	      el.classList.toggle("hidden", !open);
	      // Prefilled from the row, so an offline operator editing an existing
	      // route saves it back untouched instead of retyping it — and cleared
	      // when shut, so a value typed for the PREVIOUS provider cannot linger.
	      el.value = open ? value : "";
	    }
	    // Visibility IS the contract, not a proxy for one: the lane is open only
	    // while the select beside it has nothing real to offer, so an open lane
	    // outranks the select and a shut one is not a control at all. Reading a
	    // hidden input would let a value typed before a successful retry silently
	    // outrank the discovered pick the operator made after it. The same helper
	    // owns both the write and the read, so the two cannot disagree.
	    function customLaneValue(id) {
	      const el = $(id);
	      return el && !el.classList.contains("hidden") ? el.value.trim() : "";
	    }
	    // WHAT THE MODAL CURRENTLY MEANS, from whichever control is live. Every
	    // loader and the save must ask the same question, or the lane becomes a
	    // field that accepts typing and changes nothing: only saveDefault read
	    // it, while all four loader call sites read the <select> — so on an
	    // unconfigured row a typed provider triggered no model discovery, the
	    // model lane never opened, and the save then refused for want of a model
	    // the operator had no field to type. One accessor, no fifth spelling.
	    function activeDefaultProvider() {
	      return customLaneValue("modal-default-provider-custom") || $("modal-default-provider").value;
	    }
	    function activeDefaultModel() {
	      return customLaneValue("modal-default-model-custom") || $("modal-default-model").value;
	    }
	    // A LATE RESOLVE MUST NOT PAINT A MODAL THAT HAS MOVED ON. Every load
	    // below carries a 60s budget offline, and one click closes the modal
	    // while another opens a different route long before that elapses. This
	    // is not cosmetic: `state.activeDefaultRow` is what saveDefault writes
	    // THROUGH, so a stale paint would put one route's provider and model
	    // into another route's save. Row identity is the whole test — a closed
	    // modal is `null`, which no live row can equal.
	    function defaultModalMoved(row) {
	      return state.activeDefaultRow !== row;
	    }
	    async function loadDefaultModels(provider, selected = "", row = null) {
	      if (!provider) {
	        setSelectOptions($("modal-default-model"), [], { emptyLabel: "Select provider first", disabled: true });
	        setCustomLane("modal-default-model-custom", false);
	        return;
	      }
	      setSelectOptions($("modal-default-model"), [], { emptyLabel: "Loading models...", disabled: true });
	      setCustomLane("modal-default-model-custom", false);
	      const catalog = defaultCatalogForRow(row || {});
	      // The "Loading models..." label above belongs to THIS function, so
	      // its terminal state does too. openDefaultModal wrapped its own call,
	      // but the provider-change handler did not — switching
	      // provider while offline left the select spinning forever. Degrade at
	      // the label's owner so every caller inherits an honest end state, then
	      // RETHROW so a caller that sequences further loads can react.
	      let models;
	      try {
	        models = await fetchDefaultModels(provider, row || {});
	      } catch (e) {
	        if (defaultModalMoved(row)) throw e;
	        setSelectOptions($("modal-default-model"), selected ? [selected] : [], {
	          emptyLabel: "Model discovery failed",
	          disabled: !selected,
	          selected,
	        });
	        // Discovery failed — open the free-text lane so the route is still
	        // configurable. Without this the honest error message is all the
	        // operator gets, and the modal becomes a dead end.
	        setCustomLane("modal-default-model-custom", true, selected);
	        $("default-modal-message").textContent = `${catalog.scope} model discovery failed: ${e.message || e} — type the model id to save it anyway.`;
	        $("default-modal-message").className = "message error";
	        throw e;
	      }
	      if (defaultModalMoved(row)) return;
	      setSelectOptions($("modal-default-model"), models, {
	        emptyLabel: models.length ? "Select model..." : catalog.emptyModels,
	        disabled: !models.length,
	        selected,
	      });
	      // An EMPTY catalog is the fresh-install-offline case: the probe
	      // succeeded but the provider offered nothing, so there is no value to
	      // pick and the operator must be able to type one.
	      setCustomLane("modal-default-model-custom", !models.length, selected);
	      if (selected && !models.includes(selected)) {
	        $("default-modal-message").textContent = `Configured model "${selected}" is not currently in the discovered ${catalog.scope} catalog for ${provider}.`;
	        $("default-modal-message").className = "message error";
	      }
	    }
	    async function loadDefaultVoices(provider, model = "", selected = "", row = null) {
	      const label = $("modal-default-voice-label");
	      const select = $("modal-default-voice");
	      if (!isVoiceOutputDefault(row)) {
	        label.classList.add("hidden");
	        setSelectOptions(select, [], { emptyLabel: "No voice selector for this route", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      label.classList.remove("hidden");
	      if (!provider) {
	        setSelectOptions(select, [], { emptyLabel: "Select provider first", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      if (!model) {
	        setSelectOptions(select, [], { emptyLabel: "Select model first", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      setSelectOptions(select, [], { emptyLabel: "Loading voices...", disabled: true, labelMap: state.voiceLabels });
	      // Same contract as the model select: the "Loading voices..." label
	      // never outlives the request that set it.
	      let voices;
	      try {
	        voices = await fetchDefaultVoices(provider, model, row || {});
	      } catch (e) {
	        if (defaultModalMoved(row)) throw e;
	        setSelectOptions(select, [], { emptyLabel: "Voice discovery failed", disabled: true, labelMap: state.voiceLabels });
	        $("default-modal-message").textContent = `voice discovery failed: ${e.message || e}`;
	        $("default-modal-message").className = "message error";
	        throw e;
	      }
	      if (defaultModalMoved(row)) return;
	      const reason = voices.length ? "" : (state.voiceReasons.get(defaultVoicesKey(provider, model)) || "");
	      setSelectOptions(select, voices, {
	        emptyLabel: voices.length ? "Use provider default voice" : (reason ? "No voices — see why below" : "No voices discovered"),
	        disabled: !voices.length,
	        selected,
	        labelMap: state.voiceLabels,
	      });
	      if (reason) {
	        $("default-modal-message").textContent = reason;
	        $("default-modal-message").className = "message warn";
	      }
	      if (selected && !voices.includes(selected)) {
	        $("default-modal-message").textContent = `Configured voice "${selected}" is not currently in the discovered voice catalog for ${provider}/${model}.`;
	        $("default-modal-message").className = "message error";
	      }
	    }
    const ENDPOINT_FAMILIES = [
      {
        id: "openai",
        label: "OpenAI",
        presetId: "openai",
        defaultName: "OpenAI",
        summary: "OpenAI API or an OpenAI-compatible OpenAI deployment.",
        description: "OpenAI account connection for GPT and embedding models.",
        basePlaceholder: "default: https://api.openai.com/v1",
        baseHelp: "Optional. Leave empty to use the normal OpenAI API URL.",
        keyPlaceholder: "OpenAI API key",
        keyHelp: "Paste an OpenAI API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "anthropic",
        label: "Anthropic",
        presetId: "anthropic",
        defaultName: "Anthropic",
        summary: "Anthropic Claude API or a Claude-compatible Anthropic proxy.",
        description: "Anthropic account connection for Claude models.",
        basePlaceholder: "default: https://api.anthropic.com/v1",
        baseHelp: "Optional. Leave empty to use the normal Anthropic API URL. Use a /v1 base URL for Anthropic-compatible proxies.",
        keyPlaceholder: "Anthropic API key",
        keyHelp: "Paste an Anthropic API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "openrouter",
        label: "OpenRouter",
        presetId: "openrouter",
        defaultName: "OpenRouter",
        summary: "OpenRouter account connection for multi-provider routing.",
        description: "OpenRouter connection for hosted model routing.",
        basePlaceholder: "default: https://openrouter.ai/api/v1",
        baseHelp: "Optional. Leave empty to use the normal OpenRouter API URL.",
        keyPlaceholder: "OpenRouter API key",
        keyHelp: "Paste an OpenRouter API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "portkey",
        label: "Portkey",
        presetId: "portkey",
        defaultName: "Portkey",
        summary: "Portkey gateway connection for governed provider routing.",
        description: "Portkey connection for gateway-managed provider routing.",
        basePlaceholder: "default: https://api.portkey.ai/v1",
        baseHelp: "Optional. Leave empty to use the normal Portkey OpenAI-compatible URL.",
        keyPlaceholder: "Portkey API key",
        keyHelp: "Paste the Portkey API key or gateway key expected by your account.",
        requiresBaseUrl: false,
      },
      {
        id: "lmstudio",
        label: "LM Studio",
        presetId: "lmstudio",
        defaultName: "LM Studio",
        summary: "Local or remote LM Studio server.",
        description: "LM Studio server connection for local or LAN model serving.",
        basePlaceholder: "http://127.0.0.1:1234/v1",
        baseHelp: "Usually the LM Studio OpenAI-compatible server URL. In Docker, use the host-reachable URL instead of 127.0.0.1.",
        keyPlaceholder: "optional",
        keyHelp: "Optional. Only fill this if your LM Studio proxy requires a key.",
        requiresBaseUrl: false,
      },
      {
        id: "ollama",
        label: "Ollama",
        presetId: "ollama",
        defaultName: "Ollama",
        summary: "Local or remote Ollama server.",
        description: "Ollama server connection for local or LAN model serving.",
        basePlaceholder: "http://127.0.0.1:11434",
        baseHelp: "Usually the Ollama server URL. In Docker, use the host-reachable URL instead of 127.0.0.1.",
        keyPlaceholder: "optional",
        keyHelp: "Optional. Most local Ollama installs do not require a key.",
        requiresBaseUrl: false,
      },
      {
        id: "openai-compatible",
        label: "Custom OpenAI-compatible",
        presetId: "custom-openai-compatible",
        defaultName: "Custom endpoint",
        summary: "Any generic /v1 endpoint such as vLLM, llama.cpp, LocalAI, or a private gateway.",
        description: "Custom OpenAI-compatible endpoint connection.",
        basePlaceholder: "https://endpoint.example/v1",
        baseHelp: "Required for custom OpenAI-compatible endpoints such as vLLM, llama.cpp, or a private inference gateway.",
        keyPlaceholder: "endpoint API key, if required",
        keyHelp: "Paste the key required by this endpoint. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: true,
      },
    ];
    function endpointFamilyInfo(id) {
      return ENDPOINT_FAMILIES.find((item) => item.id === id) || ENDPOINT_FAMILIES[ENDPOINT_FAMILIES.length - 1];
    }
    function defaultProfileIdForFamily(family) {
      const info = endpointFamilyInfo(family);
      if (info.id === "openai-compatible") return "custom-endpoint";
      return info.id;
    }
    // DESIGN-v3 §7 / §13.8: ONE entry per provider. A provider that runs on
    // this computer is its engine card under Local providers, carrying its
    // server connection (this table: engine id -> connection family; vLLM's
    // server is a custom OpenAI-compatible connection with the fixed id
    // "vllm"). Remote providers are the presets below. The modal's family
    // select keeps every family.
    const LOCAL_PROVIDER_CONNECTIONS = {
      ollama: { family: "ollama" },
      lmstudio: { family: "lmstudio" },
      vllm: { family: "openai-compatible", profileId: "vllm", name: "vLLM server", description: "vLLM OpenAI-compatible server." },
    };
    const REMOTE_PROVIDER_FAMILIES = ["openai", "anthropic", "openrouter", "portkey", "openai-compatible"];
    const LOCAL_CONNECTION_PROFILE_IDS = new Set(Object.values(LOCAL_PROVIDER_CONNECTIONS).map((c) => c.profileId).filter(Boolean));
    function localConnectionProfiles(engineId) {
      const conn = LOCAL_PROVIDER_CONNECTIONS[engineId];
      if (!conn) return [];
      return (state.endpointProfiles || []).filter((p) => p && (conn.profileId ? p.id === conn.profileId : (p.provider_family || "") === conn.family));
    }
    function endpointKeyText(p) {
      return p.api_key_set ? `key ${String(p.api_key_fingerprint || "").slice(0, 8)}` : "no key";
    }
    // The Providers tab's additions to each engine card (console_ui.py
    // mountEngineCards `extra`): "Browse models" whenever the engine runs on
    // this computer and has catalog builds, and the server connection.
    function localProviderExtras(e) {
      const out = { browse: e.supported !== false && !!MC_ENGINE_PROVIDER[e.provider || e.id] };
      const conn = LOCAL_PROVIDER_CONNECTIONS[e.id];
      if (!conn) return out;
      const rows = localConnectionProfiles(e.id);
      const items = rows.map((p) => {
        const configured = p.managed === false || p.synthetic === true;
        const where = p.base_url_configured || p.base_url ? `<code class="ui-ellip" title="${esc(p.base_url || "")}">${esc(p.base_url || "")}</code>` : "provider default address";
        return `<li class="provider-connection__row"><span class="provider-connection__text"><b>${esc(p.display_name || p.id)}</b> ${where}`
          + `<span class="ui-sub">${esc(p.enabled === false ? "disabled" : "enabled")} · ${esc(endpointKeyText(p))}</span></span>`
          + `<button type="button" class="ui-btn is-quiet" data-provider-connection-edit="${esc(p.id)}">${configured ? "Override" : "Edit"}</button></li>`;
      }).join("");
      // A fixed-id connection (vLLM) is edited, never added twice.
      const add = conn.profileId && items ? "" : `<button type="button" class="ui-btn is-quiet" data-provider-connect="${esc(e.id)}" title="The address workflows use to reach this server; add one for a server on another computer.">${items ? "Add connection" : "Set up connection"}</button>`;
      out.body = `<div class="provider-connection" data-provider-connection="${esc(e.id)}"><div class="provider-connection__head"><span class="provider-connection__title">Connection</span>${add}</div>`
        + (items ? `<ul class="provider-connection__list">${items}</ul>` : "")
        + `</div>`;
      return out;
    }
    function openLocalProviderConnection(engineId) {
      const conn = LOCAL_PROVIDER_CONNECTIONS[engineId];
      if (!conn) throw new Error(`no connection family for local provider ${engineId}`);
      openEndpointModalForFamily(conn.family);
      if (conn.profileId) {
        $("endpoint-profile-id").value = conn.profileId;
        $("endpoint-name").value = conn.name || conn.profileId;
        $("endpoint-description").value = conn.description || "";
      }
    }
    function editEndpointProfileById(profileId) {
      const p = (state.endpointProfiles || []).find((row) => row && row.id === profileId);
      if (!p) throw new Error(`no provider connection ${profileId}`);
      if (p.managed === false || p.synthetic === true) openEndpointModalFromConfiguredProvider(p);
      else fillEndpointProfileForm(p);
    }
    function onLocalProviderClick(event) {
      const t = event && event.target && event.target.closest ? event.target : null;
      if (!t) return;
      const add = t.closest("[data-provider-connect]");
      if (add) { openLocalProviderConnection(add.dataset.providerConnect); return; }
      const edit = t.closest("[data-provider-connection-edit]");
      if (edit) editEndpointProfileById(edit.dataset.providerConnectionEdit);
    }
    function renderProviderPresets() {
      const grid = $("provider-preset-grid");
      if (!grid) return;
      grid.textContent = "";
      for (const item of ENDPOINT_FAMILIES.filter((f) => REMOTE_PROVIDER_FAMILIES.includes(f.id))) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `provider-preset ${state.activeProviderPreset === item.id ? "active" : ""}`;
        button.id = `provider-preset-${item.id}`;
        // Its connections, keys as fingerprints only (the table's rule).
        const mine = (state.endpointProfiles || []).filter((p) => p && (p.provider_family || "") === item.id && !LOCAL_CONNECTION_PROFILE_IDS.has(p.id));
        const status = !mine.length ? "Not connected"
          : mine.length === 1 ? `Connected · ${endpointKeyText(mine[0])}`
          : `${mine.length} connections`;
        button.innerHTML = `<strong>${esc(item.label)}</strong><span>${esc(item.summary || "")}</span><span class="provider-preset__state${mine.length ? " is-on" : ""}">${esc(status)}</span>`;
	        button.onclick = () => openEndpointModalForFamily(item.id);
        grid.append(button);
      }
    }
    function updateEndpointFamilyHints() {
      const family = endpointFamilyInfo($("endpoint-provider-family").value || "openai-compatible");
      $("endpoint-base-url").placeholder = family.basePlaceholder;
      $("endpoint-api-key").placeholder = family.keyPlaceholder || "leave blank to keep existing key";
      $("endpoint-base-url-help").textContent = family.baseHelp;
      $("endpoint-api-key-help").textContent = family.keyHelp;
	      $("provider-modal-title").textContent = `Configure ${family.label}`;
	      $("provider-modal-description").textContent = family.summary || "Gateway stores endpoint details and keys server-side, then exposes this connection as an available provider.";
      state.activeProviderPreset = family.id;
      renderProviderPresets();
    }
    function handleEndpointFamilyChange() {
      const previous = endpointFamilyInfo(state.activeProviderPreset || "openai");
      const family = endpointFamilyInfo($("endpoint-provider-family").value || "openai-compatible");
      const createMode = !$("endpoint-id").value.trim();
      if (createMode) {
        const previousDefaultId = defaultProfileIdForFamily(previous.id);
        const currentId = $("endpoint-profile-id").value.trim();
        if (!currentId || currentId === previousDefaultId) {
          $("endpoint-profile-id").value = defaultProfileIdForFamily(family.id);
        }
        const currentName = $("endpoint-name").value.trim();
        if (!currentName || currentName === (previous.defaultName || previous.label)) {
          $("endpoint-name").value = family.defaultName || family.label;
        }
        const currentDescription = $("endpoint-description").value.trim();
        if (!currentDescription || currentDescription === (previous.description || "")) {
          $("endpoint-description").value = family.description || "";
        }
        setEndpointModelOptions([], []);
      }
      updateEndpointFamilyHints();
    }
    function selectProviderPreset(familyId) {
      const family = endpointFamilyInfo(familyId || "openai-compatible");
      state.activeProviderPreset = family.id;
      $("endpoint-provider-family").value = family.id;
      if (!$("endpoint-id").value.trim()) {
        $("endpoint-profile-id").value = defaultProfileIdForFamily(family.id);
        $("endpoint-profile-id").disabled = false;
        $("endpoint-name").value = family.defaultName || family.label;
        $("endpoint-description").value = family.description || "";
        $("endpoint-base-url").value = "";
        $("endpoint-api-key").value = "";
        $("endpoint-clear-api-key").checked = false;
        setEndpointModelOptions([], []);
        $("endpoint-enabled").checked = true;
        $("endpoint-message").textContent = "";
        $("endpoint-message").className = "message";
      }
      updateEndpointFamilyHints();
    }
    function initEndpointProfileFormOptions() {
      const familySelect = $("endpoint-provider-family");
      const familySelected = familySelect.value || state.activeProviderPreset || "openai";
      familySelect.textContent = "";
      for (const item of ENDPOINT_FAMILIES) {
        const opt = document.createElement("option");
        opt.value = item.id;
        opt.textContent = item.label;
        familySelect.append(opt);
      }
      familySelect.value = ENDPOINT_FAMILIES.some((item) => item.id === familySelected) ? familySelected : "openai-compatible";
      const scopeValues = state.principal?.admin ? ["user", "gateway"] : ["user"];
      const scopeSelect = $("endpoint-scope");
      const scopeSelected = scopeSelect.value || "user";
      scopeSelect.textContent = "";
      for (const value of scopeValues) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = value === "gateway" ? "Gateway-wide" : "Only me";
        scopeSelect.append(opt);
      }
      scopeSelect.value = scopeValues.includes(scopeSelected) ? scopeSelected : "user";
      updateEndpointFamilyHints();
      renderProviderPresets();
    }
    function clearEndpointProfileForm() {
      $("endpoint-id").value = "";
      $("endpoint-profile-id").value = "";
      $("endpoint-profile-id").disabled = false;
      $("endpoint-name").value = "";
      $("endpoint-description").value = "";
      $("endpoint-provider-family").value = "openai";
      $("endpoint-scope").value = "user";
      $("endpoint-base-url").value = "";
      $("endpoint-api-key").value = "";
      $("endpoint-clear-api-key").checked = false;
      setEndpointModelOptions([], []);
      $("endpoint-enabled").checked = true;
      $("endpoint-message").textContent = "";
	      $("endpoint-message").className = "message";
	      selectProviderPreset("openai");
	    }
	    function openEndpointModalForFamily(familyId) {
	      initEndpointProfileFormOptions();
	      clearEndpointProfileForm();
	      selectProviderPreset(familyId || "openai");
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
	    function openEndpointModalFromConfiguredProvider(profile) {
	      initEndpointProfileFormOptions();
	      clearEndpointProfileForm();
	      const family = String(profile.provider_family || profile.provider_id || profile.id || "openai").trim();
	      selectProviderPreset(family);
	      const providerId = String(profile.provider_id || profile.id || defaultProfileIdForFamily(family)).trim();
	      $("endpoint-profile-id").value = providerId;
	      $("endpoint-profile-id").disabled = false;
	      $("endpoint-name").value = profile.display_name || providerId;
	      $("endpoint-description").value = profile.description || "";
	      $("endpoint-base-url").value = profile.base_url || "";
	      $("endpoint-api-key").value = "";
	      $("endpoint-clear-api-key").checked = false;
	      $("endpoint-scope").value = state.principal?.admin ? "gateway" : "user";
	      $("endpoint-enabled").checked = true;
	      setEndpointModelOptions([], []);
	      $("endpoint-message").textContent = "This provider is already available from Core config or environment. Confirm only if you want an explicit Gateway provider connection override.";
	      $("endpoint-message").className = "message";
	      updateEndpointFamilyHints();
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
	    function closeEndpointModal() {
	      $("provider-modal-backdrop").classList.add("hidden");
	    }
	    function fillEndpointProfileForm(profile) {
	      initEndpointProfileFormOptions();
      $("endpoint-id").value = profile.id || "";
      $("endpoint-profile-id").value = profile.id || "";
      $("endpoint-profile-id").disabled = true;
      $("endpoint-name").value = profile.display_name || profile.id || "";
      $("endpoint-description").value = profile.description || "";
      $("endpoint-provider-family").value = profile.provider_family || "openai-compatible";
      $("endpoint-scope").value = profile.scope || "user";
      $("endpoint-base-url").value = profile.base_url || "";
      $("endpoint-api-key").value = "";
      $("endpoint-clear-api-key").checked = false;
      const allowedModels = Array.isArray(profile.allowed_models) ? profile.allowed_models : [];
      setEndpointModelOptions(allowedModels, allowedModels);
      $("endpoint-enabled").checked = profile.enabled !== false;
      $("endpoint-message").textContent = `Editing ${profile.virtual_provider || "endpoint:" + profile.id}. Leave API key blank to keep the stored key.`;
	      $("endpoint-message").className = "message";
	      state.activeProviderPreset = $("endpoint-provider-family").value || "openai-compatible";
	      updateEndpointFamilyHints();
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
    function renderEndpointProfiles(profiles) {
      state.endpointProfiles = Array.isArray(profiles) ? profiles : [];
      const tbody = $("endpoint-profiles-table");
      tbody.textContent = "";
	      if (!state.endpointProfiles.length) {
	        const tr = document.createElement("tr");
	        tr.innerHTML = `<td colspan="6" class="empty">No available providers configured yet.</td>`;
	        tbody.append(tr);
	        renderProviderPresets();
	        engineRender();
	        return;
	      }
      for (const p of state.endpointProfiles) {
        const tr = document.createElement("tr");
        const endpoint = p.base_url_configured ? esc(p.base_url || "") : "provider default";
        const keyState = p.api_key_set ? `key ${String(p.api_key_fingerprint || "").slice(0, 8)}` : "no key";
        const models = Array.isArray(p.allowed_models) && p.allowed_models.length
          ? `${p.allowed_models.length} restricted`
          : "live discovery";
        const providerId = providerValueForEndpointProfile(p);
	        tr.innerHTML = `
	          <td><strong>${esc(p.display_name || p.id)}</strong><div class="muted">${esc(p.description || "No description")}</div></td>
	          <td><code>${esc(providerId || p.id)}</code><div class="muted">${endpoint}</div></td>
	          <td>${esc(endpointFamilyInfo(p.provider_family || "openai-compatible").label)}</td>
	          <td><span class="badge">${esc(models)}</span></td>
	          <td><span class="state-pill ${p.enabled ? "ok" : "off"}">${p.enabled ? "enabled" : "disabled"}</span><div class="muted">${esc(p.scope || "user")} · ${esc(keyState)}</div></td>
	        `;
        const actions = document.createElement("td");
        actions.className = "actions";
        if (p.managed === false || p.synthetic === true) {
          const override = document.createElement("button");
          override.innerHTML = `<span class="button-icon" aria-hidden="true">✎</span><span>Override</span>`;
          override.className = "secondary";
          override.onclick = () => openEndpointModalFromConfiguredProvider(p);
          actions.append(override);
        } else {
          const edit = document.createElement("button");
          edit.innerHTML = `<span class="button-icon" aria-hidden="true">✎</span><span>Edit</span>`;
          edit.className = "secondary";
          edit.onclick = () => fillEndpointProfileForm(p);
          const del = document.createElement("button");
          del.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Delete</span>`;
          del.className = "danger";
          del.onclick = () => deleteEndpointProfile(p);
          actions.append(edit, del);
        }
        tr.append(actions);
        tbody.append(tr);
      }
      // The Remote presets and the Local provider cards show these connections.
      renderProviderPresets();
      engineRender();
    }
    async function loadEndpointProfiles() {
      const payload = await api("/api/gateway/config/provider-endpoint-profiles");
      renderEndpointProfiles(payload.profiles || []);
      // Shared connections live in AbstractCore's store (one store,
      // one owner) — the panel says which file, exactly as the defaults grid does.
      renderStoreAuthority("endpoint-profiles-authority", payload);
      initEndpointProfileFormOptions();
      return payload;
    }
    async function saveEndpointProfile() {
      $("endpoint-message").textContent = "";
      const editingId = $("endpoint-id").value.trim();
      const profileId = editingId || $("endpoint-profile-id").value.trim();
      if (!profileId) {
        $("endpoint-message").textContent = "Connection id is required.";
        $("endpoint-message").className = "message error";
        return;
      }
      const payload = {
        display_name: $("endpoint-name").value.trim() || profileId,
        description: $("endpoint-description").value.trim(),
        provider_family: $("endpoint-provider-family").value || "openai-compatible",
        base_url: $("endpoint-base-url").value.trim() || null,
        scope: $("endpoint-scope").value || "user",
        allowed_models: selectedEndpointModels(),
        enabled: $("endpoint-enabled").checked,
      };
      if (endpointFamilyInfo(payload.provider_family).requiresBaseUrl && !payload.base_url) {
        $("endpoint-message").textContent = "Custom OpenAI-compatible connections need a Base URL.";
        $("endpoint-message").className = "message error";
        return;
      }
      if (!editingId) payload.id = profileId;
      const apiKey = $("endpoint-api-key").value.trim();
      if (apiKey && $("endpoint-clear-api-key").checked) {
        $("endpoint-message").textContent = "Choose either a new API key or clear the stored key, not both.";
        $("endpoint-message").className = "message error";
        return;
      }
      if (apiKey) payload.api_key = apiKey;
      if (editingId && $("endpoint-clear-api-key").checked) payload.clear_api_key = true;
      const path = editingId
        ? `/api/gateway/config/provider-endpoint-profiles/${encodeURIComponent(editingId)}`
        : "/api/gateway/config/provider-endpoint-profiles";
      const method = editingId ? "PUT" : "POST";
      try {
        const res = await api(path, { method, body: JSON.stringify(payload) });
        $("endpoint-api-key").value = "";
        $("endpoint-message").textContent = `Saved. Use ${res.profile?.virtual_provider || "endpoint:" + profileId} as the provider in Flow nodes or Core capability defaults.`;
        $("endpoint-message").className = "message ok";
		        renderEndpointProfiles(res.profiles || []);
		        state.providerModels.clear();
		        await loadProviders();
		        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
		        closeEndpointModal();
	      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      }
    }
    async function discoverEndpointModels() {
      $("endpoint-message").textContent = "";
      const editingId = $("endpoint-id").value.trim();
      const profileId = editingId || $("endpoint-profile-id").value.trim();
      const apiKey = $("endpoint-api-key").value.trim();
      if (apiKey && $("endpoint-clear-api-key").checked) {
        $("endpoint-message").textContent = "Choose either a new API key or clear the stored key before discovering models.";
        $("endpoint-message").className = "message error";
        return;
      }
      const previousSelection = selectedEndpointModels();
      const payload = {
        provider_family: $("endpoint-provider-family").value || "openai-compatible",
        base_url: $("endpoint-base-url").value.trim() || null,
      };
      if (endpointFamilyInfo(payload.provider_family).requiresBaseUrl && !payload.base_url) {
        $("endpoint-message").textContent = "Custom OpenAI-compatible discovery needs a Base URL.";
        $("endpoint-message").className = "message error";
        return;
      }
      if (profileId) payload.profile_id = profileId;
      if (apiKey) payload.api_key = apiKey;
      $("discover-endpoint-models").disabled = true;
      $("endpoint-message").textContent = "Discovering models from endpoint...";
      $("endpoint-message").className = "message";
      try {
        const res = await api("/api/gateway/config/provider-endpoint-profiles/discover-models", { method: "POST", body: JSON.stringify(payload) });
        const models = parseModelItems(res);
        setEndpointModelOptions(models, previousSelection.filter((model) => models.includes(model)));
        if (models.length) {
          $("endpoint-message").textContent = `Discovered ${models.length} model${models.length === 1 ? "" : "s"}.`;
          $("endpoint-message").className = "message ok";
        } else {
          $("endpoint-message").textContent = res.error || "No models were returned by this endpoint.";
          $("endpoint-message").className = "message error";
        }
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      } finally {
        $("discover-endpoint-models").disabled = false;
      }
    }
    function clearEndpointModelAllowlist() {
      for (const option of $("endpoint-models").options) option.selected = false;
      updateEndpointModelSummary();
    }
    async function deleteEndpointProfile(profile) {
      const ok = await confirmAction({
        title: "Delete provider endpoint",
        message: `Delete ${profile.virtual_provider || "endpoint:" + profile.id}? Existing workflows that select this virtual provider will stop working until they are remapped.`,
        confirmLabel: "Delete endpoint",
        danger: true,
      });
      if (!ok) return;
      try {
        const res = await api(`/api/gateway/config/provider-endpoint-profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
        renderEndpointProfiles(res.profiles || []);
	        clearEndpointProfileForm();
	        state.providerModels.clear();
	        await loadProviders();
	        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      }
    }
	    function canonicalDefaultRouteTask(value) {
	      const task = String(value || "").trim().toLowerCase().replaceAll("-", "_");
	      return ["text_to_image", "image_to_image", "image_upscale", "text_to_video", "image_to_video"].includes(task) ? task : "";
	    }
	    function routeKey(row) {
	      const base = `${row.kind || ""}.${row.modality || ""}`;
	      const task = canonicalDefaultRouteTask(row.route_task || row.default_task || row.capability_task || row.task);
	      return task ? `${base}.${task}` : base;
	    }
	    function defaultRowKey(row) {
	      const key = row?.key || routeKey(row || {});
	      return key && key !== "." ? key : "";
	    }
	    function defaultRowConfigured(row) {
	      return Boolean(row?.provider && row?.model);
	    }
	    function defaultRowCapability(row) {
	      if (row?.label) return row.label;
	      const kind = row?.kind ? String(row.kind) : "";
	      const modality = row?.modality ? String(row.modality) : "";
	      return `${kind} ${modality}`.trim() || "Capability";
	    }
	    function defaultRowKindModality(row) {
	      const key = defaultRowKey(row);
	      const parts = key.split(".");
	      const task = parts[2] || canonicalDefaultRouteTask(row?.route_task || row?.default_task || row?.capability_task || "");
	      return { kind: parts[0] || row?.kind || "", modality: parts[1] || row?.modality || "", task };
	    }
	    function findDefaultRow(rows, key) {
	      return (rows || []).find((row) => defaultRowKey(row) === key) || null;
	    }
	    function rowHasProviderModel(row) {
	      return Boolean(row?.provider && row?.model);
	    }
	    function visibleCapabilityDefaultRow(row) {
	      // `output.image` / `output.video` used to be HIDDEN here as "broad
	      // compatibility defaults". They are neither broad-only nor legacy:
	      // they are the PARENT row — the one value that answers every image
	      // task without a row of its own, the simple path for an operator who
	      // does not want per-task routing, what the fresh-install seed writes,
	      // and what `--only image` names. Hiding them meant the one grid that
	      // can WRITE the store could not set the row the gateway and Core both
	      // READ (`_resolved_vision_backend`, `_vision_route_defaults`), so a
	      // seeded value could be stranded with no way to see or clear it.
	      // They are shown as parents now — grouped, labeled, and benign when
	      // the task rows below already cover them.
	      // scene3d used to be filtered here for the same reason output.image and
	      // output.video once were: the modal could not configure it. There is no
	      // scene3d DISCOVERY endpoint, so provider/model probing returns nothing
	      // — but "nothing to discover" is now a supported state, not a dead end:
	      // the free-text provider and model lanes open automatically and the row
	      // saves. Hiding it meant the store could hold a scene3d route (the TUI
	      // writes all 24) that this grid could neither show nor clear.
	      return true;
	    }
	    // THE ROUTE HIERARCHY, STRAIGHT OFF THE PAYLOAD. Core derives
	    // `broad_key` / `task_keys` / `covered_by_tasks` once
	    // (`manager._decorate_route_hierarchy`) so no surface re-derives the
	    // parent/child map and the four grids cannot disagree.
	    function defaultRowParentKey(row) {
	      return String(row?.broad_key || "").trim();
	    }
	    function defaultRowTaskKeys(row) {
	      return Array.isArray(row?.task_keys) ? row.task_keys : [];
	    }
	    function defaultRowIsTaskParent(row) {
	      return defaultRowTaskKeys(row).length > 0;
	    }
	    // Parent rows sort ABOVE their own task rows, and the task rows follow
	    // immediately — the payload order already does this, but an explicit
	    // group keeps the two together if a row is ever filtered out between
	    // them.
	    function groupDefaultRowsByHierarchy(rows) {
	      const list = (rows || []).slice();
	      const taken = new Set();
	      const out = [];
	      for (const row of list) {
	        const key = defaultRowKey(row);
	        if (taken.has(key) || defaultRowParentKey(row)) continue;
	        taken.add(key);
	        out.push(row);
	        for (const taskKey of defaultRowTaskKeys(row)) {
	          const child = list.find((item) => defaultRowKey(item) === taskKey);
	          if (child && !taken.has(taskKey)) {
	            taken.add(taskKey);
	            out.push(child);
	          }
	        }
	      }
	      for (const row of list) {
	        if (!taken.has(defaultRowKey(row))) out.push(row);
	      }
	      return out;
	    }
	    async function textDefaultCoversInput(row, rows) {
	      const key = defaultRowKey(row);
	      if (!["input.image", "input.video", "input.sound", "input.music"].includes(key)) return false;
	      const textRow = findDefaultRow(rows, "input.text");
	      if (!rowHasProviderModel(textRow)) return false;
	      try {
	        const models = await fetchDefaultModels(textRow.provider, row);
	        return models.includes(textRow.model);
	      } catch {
	        return false;
	      }
	    }
	    async function displayDefaultRow(row, rows) {
	      const key = defaultRowKey(row);
	      if (key === "output.text") {
	        const textRow = findDefaultRow(rows, "input.text");
	        if (rowHasProviderModel(textRow)) {
	          return {
	            ...row,
	            provider: textRow.provider,
	            model: textRow.model,
	            base_url: textRow.base_url,
	            reasoning: textRow.reasoning || row.reasoning || "",
	            options: textRow.options || {},
	            configured: true,
	            source: textRow.source || row.source || "abstractcore.capability_defaults",
	            derived_from: "input.text",
	            read_only: true,
	          };
	        }
	        return { ...row, derived_from: "input.text", read_only: true };
	      }
	      if (["input.image", "input.video", "input.sound", "input.music"].includes(key)) {
	        if (row?.covered_by === "input.text" || await textDefaultCoversInput(row, rows)) {
	          const textRow = findDefaultRow(rows, "input.text") || row;
	          const overrideable = key !== "input.image";
	          return {
	            ...row,
	            provider: textRow.provider || row.provider,
	            model: textRow.model || row.model,
	            base_url: textRow.base_url || row.base_url,
	            options: textRow.options || row.options || {},
	            configured: Boolean(textRow.provider && textRow.model) || defaultRowConfigured(row),
	            source: textRow.source || row.source || "abstractcore.capability_defaults",
	            covered_by: "input.text",
	            coverage_mode: key === "input.video" ? "video_frames" : row.coverage_mode,
	            overrideable,
	            read_only: !overrideable,
	          };
	        }
	      }
	      return row;
	    }
	    function defaultRowReadOnly(row) {
	      return Boolean(row?.read_only || row?.derived_from || (row?.covered_by && !row?.overrideable));
	    }
	    function defaultRowStatus(row) {
	      // ONE VOCABULARY ACROSS EVERY CONSOLE. Both TUIs say
	      // "derived <- input.text" / "covered by input.text"; this surface said
	      // "linked" / "covered", so the same row read differently depending on
	      // which console an operator opened. The TUI wording names the ROUTE
	      // the value comes from, which is the question the pill is asked.
	      if (row?.covered_by === "input.text") return { label: "covered by input.text", cls: "covered" };
	      if (row?.derived_from === "input.text") return { label: rowHasProviderModel(row) ? "derived \u2190 input.text" : "not configured", cls: rowHasProviderModel(row) ? "covered" : "off" };
	      // A configured route AbstractCore says cannot run on this host
	      // (`route_unavailable`) is not "configured" in the sense that matters:
	      // calls to it fail. Warning tone; the reason sits under the pill.
	      if (defaultRowConfigured(row) && defaultRowRouteUnavailableReason(row)) return { label: "cannot run here", cls: "off" };
	      // The host CAN run it, but the engine's software is not installed in
	      // this Python environment (AbstractCore `engine_missing`): a third
	      // state, distinct from "cannot run here" and from "not downloaded".
	      if (defaultRowConfigured(row) && engineMissingInfo(row)) return { label: "engine missing", cls: "off" };
	      if (defaultRowConfigured(row)) return { label: "configured", cls: "ok" };
	      // AN UNSET PARENT WHOSE TASK ROWS ARE ALL SET IS NOT A PROBLEM. Core
	      // proves it (`capability_route_tasks_cover_broad`): the task rows are
	      // exactly the keys `_OUTPUT_ROUTE_TABLE` can produce for that
	      // modality, so nothing can reach the parent. A red "not configured"
	      // there sent an operator hunting for dead code.
	      if (row?.covered_by_tasks) return { label: "not needed", cls: "covered" };
	      // ...and the MIRROR: a task row with no value of its own whose
	      // parent IS set is answered by that parent. A fresh install is
	      // exactly this shape (the seed writes `output.image` alone), so
	      // three red "not configured" rows used to sit under a working
	      // parent and read as "image editing is not set up".
	      if (row?.inherits_broad) return { label: `inherited ← ${defaultRowParentKey(row)}`, cls: "covered" };
	      return { label: "not configured", cls: "off" };
	    }
	    // AbstractCore stamps `recommendation_unavailable` {provider, model, reason}
	    // on an UNSET row whose recommended engine cannot run on this host
	    // (`manager.list_capability_defaults`), so the row says WHY it is empty
	    // instead of a bare "not configured". Absent (older Core, or a runnable
	    // recommendation): no markup at all.
	    function defaultRowUnavailableReason(row) {
	      const info = row?.recommendation_unavailable;
	      if (!info || typeof info !== "object" || defaultRowConfigured(row)) return "";
	      return String(info.reason || "").trim();
	    }
	    function defaultRowUnavailableMarkup(row) {
	      const reason = defaultRowUnavailableReason(row);
	      if (!reason) return "";
	      const info = row.recommendation_unavailable;
	      const rec = [info.provider, info.model].filter(Boolean).join(" / ");
	      return `<div class="muted capability-unavailable" title="${esc(rec ? `Recommended: ${rec}` : "")}">No recommended model runs on this computer: ${esc(reason)}</div>`;
	    }
	    // ...and the CONFIGURED mirror: AbstractCore stamps `route_unavailable`
	    // {provider, model, reason} on a configured row whose provider cannot
	    // run on this host (an MLX-Gen image route carried over to Linux), so
	    // the grid warns instead of showing a green "configured" that fails at
	    // the first call. Absent (older Core, or a runnable route): no markup.
	    function defaultRowRouteUnavailableReason(row) {
	      const info = row?.route_unavailable;
	      if (!info || typeof info !== "object" || !defaultRowConfigured(row)) return "";
	      return String(info.reason || "").trim();
	    }
	    function defaultRowRouteUnavailableMarkup(row) {
	      const reason = defaultRowRouteUnavailableReason(row);
	      if (!reason) return "";
	      const info = row.route_unavailable;
	      const pair = [info.provider, info.model].filter(Boolean).join(" / ");
	      return `<div class="ui-field-msg tone-warn capability-route-unavailable" role="note" title="${esc(pair)}">Configured but cannot run on this computer: ${esc(reason)}</div>`;
	    }
	    // AbstractCore `engine_missing` {engine, name, reason, install[, engine_row]}
	    // on a capability-default row, an apply-recommended entry or a
	    // recommended-plan row. Never combined with `route_unavailable`.
	    function engineMissingInfo(rec) {
	      const info = rec && rec.engine_missing;
	      if (!info || typeof info !== "object" || !String(info.reason || "").trim()) return null;
	      return { reason: String(info.reason).trim(), install: String(info.install || "").trim(), engineRow: String(info.engine_row || "").trim() };
	    }
	    function engineMissingText(rec) {
	      const info = engineMissingInfo(rec);
	      if (!info) return "";
	      return `engine missing: ${info.reason}${info.install ? ` — install: ${info.install}` : ""}`;
	    }
	    function engineMissingMarkup(rec, cls = "ui-field-msg tone-warn") {
	      const info = engineMissingInfo(rec);
	      if (!info) return "";
	      const engines = info.engineRow ? " (Providers tab, Local providers: Install)" : "";
	      return `<div class="${cls} capability-engine-missing" role="note">Engine missing: ${esc(info.reason)}${info.install ? ` — install: <code>${esc(info.install)}</code>` : ""}${esc(engines)}</div>`;
	    }
	    // AbstractCore's fit verdict `needs_gpu_limit`: the model fits once macOS
	    // lets the GPU use more memory (`fit.gpu_limit` = the exact sysctl).
	    function gpuLimitText(rec) {
	      const fit = (rec && typeof rec.fit === "object" && rec.fit) || {};
	      const verdict = rec && (rec.fit_verdict || fit.verdict);
	      if (verdict !== "needs_gpu_limit") return "";
	      const gl = (rec && rec.gpu_limit) || fit.gpu_limit || null;
	      if (!gl || !gl.command) return "fits after raising the GPU memory limit";
	      const cost = [gl.needs_admin ? "admin" : "", gl.resets_at_restart ? "resets at restart" : ""].filter(Boolean).join("; ");
	      return `fits after raising the GPU memory limit: ${gl.command}${cost ? ` (${cost})` : ""}`;
	    }
	    function defaultRowActionLabel(row) {
	      if (row?.covered_by === "input.text") return row?.overrideable ? "Override" : "Covered by input.text";
	      if (row?.derived_from === "input.text") return "Derived \u2190 input.text";
	      if (defaultRowConfigured(row)) return "Edit";
	      // The parent row stays editable even when it is "not needed": setting
	      // it is the SIMPLE path (one model for every task of the modality),
	      // which is exactly what an operator who does not want per-task routing
	      // wants and what the fresh-install seed writes.
	      return defaultRowIsTaskParent(row) ? "Set for all" : "Configure";
	    }
	    // ------------------------------------------------------------------
	    // WEIGHTS. A route can be perfectly configured and still unrunnable
	    // because the model is not on this machine — the single most common
	    // fresh-install confusion. `/models/availability` answers it with the
	    // SAME vocabulary the AbstractCore CLI and both console-TUIs print:
	    // installed / absent / unknown / not applicable. `unknown` is a real
	    // answer (the provider's tool could not be consulted) and is never
	    // painted as either of its neighbours.
	    // ------------------------------------------------------------------
	    const WEIGHT_LABELS = {
	      installed: { label: "installed", cls: "ok" },
	      absent: { label: "not downloaded", cls: "off" },
	      unknown: { label: "unknown", cls: "covered" },
	      not_applicable: { label: "remote", cls: "covered" },
	    };
	    // F2 (adversary pass 2): AbstractCore's probe answers `unknown` when it cannot look (on a
	    // fresh install: "no Hugging Face cache directory exists on this machine yet"). A pill must
	    // never just say "Unknown": with a download verb (`downloadable`) the state is "Download
	    // needed" and Download is offered (fetching an already-cached model only re-checks it);
	    // without one it says "Not checked" and the probe's own reason is shown next to it.
	    function weightView(availability) {
	      const a = availability || {};
	      if (a.status === "unknown") return a.downloadable ? { label: "download needed", cls: "off", canDownload: true } : { label: "not checked", cls: "covered", canDownload: false };
	      const view = WEIGHT_LABELS[a.status];
	      if (!view) throw new Error(`AbstractGateway console: unknown model presence status ${JSON.stringify(a.status)} (core seam: installed|absent|unknown|not_applicable).`);
	      return { ...view, canDownload: a.status === "absent" && Boolean(a.downloadable) };
	    }
	    function weightReason(availability) {
	      const d = String((availability && availability.detail) || "").trim();
	      return d ? `${d.charAt(0).toUpperCase()}${d.slice(1)}${/[.!?]$/.test(d) ? "" : "."}` : "";
	    }
	    function rowAvailability(row) {
	      return state.availability.get(defaultRowKey(row)) || null;
	    }
	    // The thing that gets FETCHED. Not the row's model: a served id drops
	    // the quantization suffix, so downloading `row.model` would ask the
	    // provider for whichever quant it prefers rather than the 4-bit build
	    // the recommendation means.
	    function rowDownloadArtifact(row) {
	      const info = rowAvailability(row);
	      return (info && info.download_artifact) || row.model || "";
	    }
	    function downloadJobKey(provider, artifact) {
	      return `${provider || ""}/${artifact || ""}`;
	    }
	    function rowDownloadJob(row) {
	      return state.downloadJobs.get(downloadJobKey(row.provider, rowDownloadArtifact(row))) || null;
	    }
	    async function refreshAvailability({ rerender = true } = {}) {
	      let payload = null;
	      try {
	        payload = await api("/api/gateway/models/availability");
	      } catch (err) {
	        // Availability is decoration over a grid that must keep working:
	        // a failed probe leaves the Weights column blank, never blocks.
	        state.availability = new Map();
	        state.availabilityPlan = null;
	        renderAvailabilityBanner();
	        return;
	      }
	      const next = new Map();
	      for (const row of payload.routes || []) {
	        const key = defaultRowKey(row);
	        if (!key) continue;
	        // An UNCONFIGURED route has no weights to be missing. The payload
	        // reports it as `unknown` with this evidence so a machine reader can
	        // tell the two apart; a Weights column that printed "unknown" on
	        // every empty row would bury the rows that carry a real answer.
	        // Both console-TUIs drop these rows for the same reason.
	        if ((row.availability || {}).evidence === "route not configured") continue;
	        next.set(key, {
	          availability: row.availability || {},
	          download_artifact: row.download_artifact || row.model || "",
	          recommended_artifact: row.recommended_artifact || "",
	        });
	      }
	      state.availability = next;
	      state.availabilityPlan = payload.recommended || null;
	      renderAvailabilityBanner();
	      renderFirstRunModel();
	      if (rerender && Array.isArray(state.defaults) && state.defaults.length) renderDefaultRows(state.defaults);
	    }
	    // THE STARTER KIT IS ADVICE FOR AN EMPTY ROUTE, NOT A STANDING DEBT.
	    // This banner used to render the recommended set raw -- "2 of 3 models
	    // present. Missing: lmstudio qwen/qwen3.5-9b@4bit", in error red -- above
	    // a grid whose routes were all configured and all working. An operator who
	    // routed text generation at a model they prefer got a permanent error they
	    // could clear only by installing the model they had deliberately chosen
	    // against. The recommendation exists to give an UNANSWERED route something
	    // to run, so the gateway marks each recommended model with whether its
	    // route is answered (`plan.gaps` -- ONE decision, shared with the
	    // console-TUI) and this line speaks about the gaps and nothing else.
	    //
	    // No gaps, no banner. "Apply recommended" lives in the section head, so
	    // nothing has to be said above the grid to keep it reachable.
	    function renderAvailabilityBanner() {
	      const el = $("defaults-availability");
	      if (!el) return;
	      const plan = state.availabilityPlan;
	      const gaps = (plan && Array.isArray(plan.gaps)) ? plan.gaps : [];
	      el.textContent = "";
	      if (!plan || !plan.total || !gaps.length) { el.classList.add("hidden"); return; }
	      el.classList.remove("hidden");
	      // Plain, not `error`: the unconfigured rows below already carry their own
	      // red "not configured" pills. This line is the OFFER of a way out, and an
	      // offer that shouts reads as one more failure.
	      el.className = "message";
	      const routes = gaps.map((m) => m.route).filter(Boolean).join(", ");
	      const pairs = gaps.map((m) => `${m.provider} ${m.artifact}`).join(", ");
	      el.append(document.createTextNode(
	        `${gaps.length === 1 ? "One route has" : `${gaps.length} routes have`} no model yet (${routes}). `
	        + `Recommended to get started: ${pairs}. `
	      ));
	      const btn = document.createElement("button");
	      btn.className = "secondary";
	      btn.innerHTML = `<span class="button-icon" aria-hidden="true">⭳</span><span>Download missing</span>`;
	      btn.onclick = () => downloadRecommended(btn, gaps);
	      el.append(btn);
	    }
	    // A plan row whose CONFIGURED route cannot run on this host and that
	    // this apply did not change (AbstractCore `route_unavailable` on the
	    // entry): `kept` without force, `unavailable` when nothing recommended
	    // runs here either. Such a route is never reported as fine, and it is
	    // what the forced pass would replace (`overwrite`) or clear (`cleared`).
	    // A route a user INHERITS from the gateway store (the gateway stamps
	    // `route_unavailable.inherited` on a per-user apply) is flagged too, but
	    // only an admin can change it: the forced pass is not offered for it.
	    function appliedRecommendedBrokenRows(report) {
	      return ((report && report.routes) || []).filter((r) => r.route_unavailable && !r.changed);
	    }
	    function appliedRecommendedFixableRows(report) {
	      return appliedRecommendedBrokenRows(report).filter((r) => !r.route_unavailable.inherited);
	    }
	    function describeAppliedRecommended(report) {
	      const rows = (report && report.routes) || [];
	      const pair = (row) => `${(row || {}).provider || "-"}/${(row || {}).model || "-"}`;
	      const brokenWhy = (r) => (r.route_unavailable && r.route_unavailable.reason ? `; cannot run on this computer: ${r.route_unavailable.reason}` : "");
	      // `cleared` (force, nothing recommended runs here and the configured
	      // route cannot run either; AbstractCore also counts it, `cleared`):
	      // the route is now unset, said as such, never as "x -> -/-". Core
	      // clears only a flagged route, so the entry always carries the flag.
	      const changed = rows.filter((r) => r.changed && r.action !== "cleared");
	      const cleared = rows.filter((r) => r.action === "cleared");
	      const kept = rows.filter((r) => r.action === "kept");
	      // HOST-AWARE RECOMMENDATIONS (AbstractCore `plan_recommended_capability_defaults`):
	      // a recommended route whose engine cannot run on this host (MLX-Gen
	      // images off Apple silicon) comes back as `action: "unavailable"` with
	      // a `reason`, and is never written. Ignoring it made a Linux host read
	      // "every recommended route already matched" while output.image stayed
	      // empty. Absent on an older AbstractCore: nothing changes.
	      const unavailable = rows.filter((r) => r.action === "unavailable");
	      const parts = [];
	      if (changed.length) parts.push(changed.map((r) => `${r.key}: ${pair(r.before)} \u2192 ${pair(r.after)}`).join("; "));
	      if (cleared.length) parts.push(cleared.map((r) => `${r.key}: removed ${pair(r.before)} \u2014 cannot run on this computer: ${r.route_unavailable.reason}`).join("; "));
	      if (kept.length) parts.push(`kept yours on ${kept.map((r) => `${r.key} (${pair(r.before)}${brokenWhy(r)})`).join(", ")}`);
	      // Written (or kept) but its engine is not installed here: said per
	      // route, with the exact install command (AbstractCore `engine_missing`).
	      const missing = rows.filter((r) => engineMissingInfo(r));
	      if (missing.length) parts.push(missing.map((r) => `${r.key}: ${engineMissingText(r)}`).join("; "));
	      if (unavailable.length) {
	        parts.push(`${unavailable.length === 1 ? "1 route has" : `${unavailable.length} routes have`} no recommendation this computer can run: `
	          + unavailable.map((r) => `${r.key}${r.reason ? ` \u2014 ${r.reason}` : ""}${r.route_unavailable ? ` (configured ${pair(r.route_unavailable)} cannot run here either${r.route_unavailable.inherited ? `; ${r.route_unavailable.note}` : ""})` : ""}`).join("; "));
	      }
	      if (!parts.length) parts.push("every recommended route already matched");
	      return parts.join(" \u00b7 ");
	    }
	    async function applyRecommendedDefaults(btn, force) {
	      // Two passes by design: the first NEVER overwrites a route the operator
	      // configured, so the console can name exactly what it would replace
	      // before asking. A blanket apply that silently rewrote a deliberate
	      // choice is the defect this action exists to fix, not to repeat.
	      if (btn) btn.disabled = true;
	      const msg = $("defaults-message");
	      try {
	        // Slow lane: a write that aborts mid-flight leaves the operator unable
	        // to tell whether the Core store changed. apply-recommended probes the
	        // whole provider fleet before writing, so offline it is the slowest
	        // write there is — exactly the one that must not be cut in half.
	        const res = await api("/api/gateway/config/capability-defaults/apply-recommended", {
	          slow: true,
	          method: "POST",
	          body: JSON.stringify({ force: !!force }),
	        });
	        const report = res.applied_recommended || {};
	        msg.textContent = describeAppliedRecommended(report);
	        // A route left unset because nothing recommended runs here is not a
	        // success to celebrate: plain, not `ok`, when any row says so.
	        // ...nor is a configured route left in place that cannot run here.
	        const broken = appliedRecommendedBrokenRows(report);
	        msg.className = broken.length || (report.routes || []).some((r) => r.action === "unavailable") ? "message" : "message ok";
	        const kept = (report.routes || []).filter((r) => r.action === "kept");
	        // The forced pass replaces what was kept and clears a broken route
	        // nothing recommended can replace; offered for either.
	        if ((kept.length || appliedRecommendedFixableRows(report).length) && !force) {
	          const again = document.createElement("button");
	          again.className = "secondary";
	          again.innerHTML = `<span class="button-icon" aria-hidden="true">\u267b</span><span>${kept.length ? "Replace mine too" : "Clear what cannot run here"}</span>`;
	          again.onclick = () => applyRecommendedDefaults(again, true);
	          msg.append(document.createTextNode(" "));
	          msg.append(again);
	        }
	        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	        await refreshAvailability();
	      } catch (err) {
	        msg.textContent = String(err.message || err);
	        msg.className = "message error";
	      } finally {
	        if (btn) btn.disabled = false;
	      }
	    }
	    async function downloadRecommended(btn, gaps) {
	      // EXACTLY THE GAPS THE BANNER NAMED, one artifact per call. The old lane
	      // posted `{recommended: true}`, which fetches every absent artifact of
	      // the starter kit -- including the multi-gigabyte image model for a route
	      // the operator had already answered with their own choice. A button that
	      // downloads more than the sentence above it names is the same false
	      // promise as a banner that reports more than it should.
	      //
	      // Naming the artifacts here cannot re-fetch anything that landed a second
	      // ago: the materializer still short-circuits an installed artifact to
	      // `already_installed` server-side, which is where that guard belongs.
	      if (btn) btn.disabled = true;
	      const failed = [];
	      try {
	        for (const gap of gaps || []) {
	          if (!gap || !gap.provider || !gap.artifact) continue;
	          try {
	            const res = await api("/api/gateway/models/download", { slow: true, method: "POST", body: JSON.stringify({ provider: gap.provider, artifact: gap.artifact }) });
	            trackDownloadJob(res.job);
	          } catch (err) {
	            failed.push(`${gap.provider} ${gap.artifact}: ${String(err.message || err)}`);
	          }
	        }
	        if (failed.length) {
	          $("defaults-message").textContent = `Could not start: ${failed.join("; ")}`;
	          $("defaults-message").className = "message error";
	        }
	      } finally {
	        if (btn) btn.disabled = false;
	      }
	    }
	    async function downloadRouteModel(row) {
	      const provider = row.provider;
	      const artifact = rowDownloadArtifact(row);
	      if (!provider || !artifact) return;
	      try {
	        const res = await api("/api/gateway/models/download", { slow: true, method: "POST", body: JSON.stringify({ provider, artifact }) });
	        trackDownloadJob(res.job);
	      } catch (err) {
	        $("defaults-message").textContent = String(err.message || err);
	        $("defaults-message").className = "message error";
	      }
	    }
	    function trackDownloadJob(job) {
	      // One feed for every download (console_ui.py): the downloads SSE
	      // stream (/models/downloads/stream) while it is open, polling
	      // (/models/download/{id}, 1.5 s) whenever it is not. A `grp_…`
	      // parent job ("Download all") is tracked the same way.
	      return dlTrack(job);
	    }
	    function pollDownloadJob(jobId) {
	      return dlPoll(jobId);
	    }
	    function weightsCellMarkup(row) {
	      const info = rowAvailability(row);
	      const job = rowDownloadJob(row);
	      if (job && job.status === "running") return uiProgressMarkup(job, "Downloading");
	      if (!info || !info.availability || !info.availability.status) return "-";
	      const availability = info.availability;
	      const view = weightView(availability);
	      const title = [availability.detail, availability.location, availability.evidence].filter(Boolean).join(" — ");
	      const why = availability.status === "unknown" && weightReason(availability) ? `<span class="weights-reason">${esc(weightReason(availability))}</span>` : "";
	      return `<span class="state-pill ${esc(view.cls)}" title="${esc(title)}">${esc(view.label)}</span>${why}`;
	    }
	    function defaultSourceLabel(source) {
	      const value = String(source || "").trim();
	      const labels = {
	        "abstractcore.runtime": "Runtime override",
	        "abstractcore.gateway_runtime": "Gateway baseline",
	        "abstractcore.local": "Local Core config",
	        "abstractcore.server": "Core server",
	        "abstractcore.capability_defaults": "Core config",
	        "abstractcore.capability_defaults.input_text_multimodal": "Text model handles it",
	        "not_configured": "Not configured",
	      };
	      return labels[value] || value;
	    }
	    // ---- Models (host residency: the agentic-OS resources view) ----
	    // ONE snapshot (GET /host/state) feeds every section on the tab; the
	    // 5s poll is a token-guarded self-rescheduling chain (the manage-panel
	    // precedent) additionally scoped to the ACTIVE tab, so a hidden tab
	    // never keeps the host walking its residency. Reads render for every
	    // signed-in user; mutations are admin-gated at render time. Every
	    // "unknown" (null) stays unknown on screen — never guessed.
	    function _fmtPct(n) {
	      return (typeof n === "number" && isFinite(n)) ? `${Math.round(n)}%` : "unknown";
	    }
	    function _fmtCtx(n) {
	      if (typeof n !== "number" || !isFinite(n) || n <= 0) return "";
	      return n >= 1024 ? `${Math.round(n / 1024)}K` : String(n);
	    }
	    function _fmtEpochS(s) {
	      if (typeof s !== "number" || !isFinite(s) || s <= 0) return "";
	      try { return new Date(s * 1000).toISOString().slice(0, 19).replace("T", " "); } catch { return ""; }
	    }
	    // ONE display-size rule, shared by every residency surface (this console,
    // abstractflow's Resources panel, the monitor-memory widget): the first
    // KNOWN of size_bytes -> size_vram_bytes -> est_weights_bytes. The SOURCE
    // rides along so an ESTIMATE is never rendered as a measurement — the
    // tooltip says which one the number is. An MLX/HF row that only carries
    // est_weights_bytes used to render a BLANK size cell; it now renders the
    // estimate, labeled as one.
    function modelDisplaySize(row) {
      const r = (row && typeof row === "object") ? row : {};
      const pick = (value, source, label) => (typeof value === "number" && isFinite(value) && value >= 0) ? { bytes: value, source, label } : null;
      return pick(r.size_bytes, "size_bytes", "reported size")
        || pick(r.size_vram_bytes, "size_vram_bytes", "reported VRAM size")
        || pick(r.est_weights_bytes, "est_weights_bytes", "ESTIMATED weights (not measured)")
        || { bytes: null, source: "", label: "size unknown" };
    }
    function modelCacheBytes(row) {
      const v = row ? row.cache_bytes : null;
      return (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
    }
    // THE ACCELERATOR HEAP LINE — an accelerator-heap figure, clearly scoped,
    // and NEVER a statement about how full this machine is (RAM stays the
    // primary system meter; it is rendered first, above this one).
    //
    // device.allocated_bytes is PROCESS-LOCAL: on Apple silicon it reads 0
    // while a 93 GB GGUF is resident in another process, which is how this
    // meter came to say "Device · metal 0 B" beside a machine with 105 GB of
    // accelerator memory in use. device.host_in_use_bytes (ioreg "In use
    // system memory") is the genuine accelerator counter — driver-allocated
    // Metal buffers across every process — and device.wired_limit_bytes is
    // the REAL ceiling (total_bytes is the chip's whole unified pool, not what
    // the accelerator may take). Both are preferred whenever known.
    //
    // It is BLIND to memory-mapped GGUF weights: llama.cpp mmaps the .gguf and
    // wraps the pages with newBufferWithBytesNoCopy, so they never become
    // driver-allocated accelerator memory. Measured live on this host: a fully
    // offloaded 89,986,353,824 B GGUF, 76 GB of process RSS, and
    // host_in_use_bytes at 1,042,120,704 (0.76% of a 137 GB machine). Hence
    // the note, which rides EVERY variant of this label, and hence the scope
    // words: exactly "all processes" or "this process only" — never "host",
    // never a whole-machine scope name of any kind, and never anything a
    // reader could take for total system usage.
    // WHAT THIS PROCESS PINS IN ACCELERATOR MEMORY, from the host snapshot:
    // device.mlx_held_bytes (MLX live + freed-but-cached buffers; newer
    // gateways), else device.allocated_bytes (live only). 0 when unknown.
    function heldAcceleratorBytes(data) {
      const snap = (data && typeof data === "object") ? data : {};
      const mem = (snap.memory && typeof snap.memory === "object") ? snap.memory : {};
      const dev = (mem.device && typeof mem.device === "object") ? mem.device : {};
      const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
      // process_held_bytes (newer gateways): what this process pins across EVERY
      // in-process allocator -- the Metal device counter when torch is
      // present (MLX / llama.cpp / torch buffers are inside it), else MLX
      // held + llama.cpp. Older gateways: MLX only.
      const processHeld = num(dev.process_held_bytes);
      if (processHeld !== null) return processHeld;
      const held = num(dev.mlx_held_bytes);
      if (held !== null) return held;
      const active = num(dev.allocated_bytes);
      return active === null ? 0 : active;
    }
    function heldDetail(data) {
      const snap = (data && typeof data === "object") ? data : {};
      const mem = (snap.memory && typeof snap.memory === "object") ? snap.memory : {};
      const dev = (mem.device && typeof mem.device === "object") ? mem.device : {};
      const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
      const parts = [];
      const active = num(dev.mlx_active_bytes !== undefined ? dev.mlx_active_bytes : dev.allocated_bytes);
      const cache = num(dev.mlx_cache_bytes);
      if (active !== null) parts.push(`MLX live buffers ${_fmtBytes(active)}`);
      if (cache !== null) parts.push(`MLX allocator cache ${_fmtBytes(cache)}`);
      const metal = num(dev.metal_process_allocated_bytes);
      if (metal !== null && metal > 0) parts.push(`Metal allocated by this process ${_fmtBytes(metal)} (device counter; MLX, torch and llama.cpp buffers are inside it)`);
      const mpsLive = num(dev.torch_mps_allocated_bytes);
      if (mpsLive !== null && mpsLive > 0) parts.push(`torch live tensors (transformers / embeddings / voice) ${_fmtBytes(mpsLive)}`);
      const llama = num(dev.llama_cpp_bytes);
      if (llama !== null && llama > 0) parts.push(`llama.cpp GGUF engines ${_fmtBytes(llama)} (weights + KV allocation, estimated)`);
      return parts.length ? parts.join(", ") : "process-local accelerator allocations";
    }
    // WHAT the process holds, by model: `memory.resident.models` (newer
    // gateways, every in-process backend: MLX, HuggingFace/GGUF,
    // embeddings), else the MLX-only `memory.held.models` of older cores.
    // "[backend] model × N holders"; [] when no holder reports anything.
    function heldResidentNames(data) {
      const snap = (data && typeof data === "object") ? data : {};
      const mem = (snap.memory && typeof snap.memory === "object") ? snap.memory : {};
      const resident = (mem.resident && typeof mem.resident === "object" && Array.isArray(mem.resident.models) && mem.resident.models.length) ? mem.resident : null;
      const held = resident || ((mem.held && typeof mem.held === "object") ? mem.held : null);
      if (!held || !Array.isArray(held.models)) return [];
      return held.models.filter((m) => m && typeof m === "object").map((m) => {
        const holders = Number(m.holders) || 0;
        const name = Array.isArray(m.models) && m.models.length ? m.models.join(", ") : String(m.model_path || m.model || "?");
        return (m.backend ? `[${m.backend}] ` : "") + name + ` × ${holders} holder${holders === 1 ? "" : "s"}` + (m.shared_weights === false && holders > 1 ? " (full copies)" : "");
      });
    }
    // HOW the held figure was measured, in words (core utils/memory
    // `device.process_held_basis`): "metal_device_counter" |
    // "cuda_device_counter[+llama_cpp_bytes(estimated)]" |
    // "sum:<field>+<field>..." — an unknown spelling is shown verbatim, and a
    // missing basis says so (never a silent blank).
    function processHeldBasisWords(basis) {
      const b = typeof basis === "string" ? basis.trim() : "";
      if (!b) return "basis not reported";
      if (b === "metal_device_counter") return "metal device counter";
      if (b.startsWith("cuda_device_counter")) return "cuda device counter" + (b.includes("llama_cpp_bytes") ? " + llama.cpp (estimated)" : "");
      if (b.startsWith("sum:")) {
        const words = { mlx_held_bytes: "MLX", "llama_cpp_bytes(estimated)": "llama.cpp (estimated)", llama_cpp_bytes: "llama.cpp", allocated_bytes: "live allocations" };
        const parts = b.slice(4).split("+").filter(Boolean).map((p) => words[p] || p);
        return parts.length ? `sum of ${parts.join(" + ")}` : b;
      }
      return b;
    }
    // The basis of the figure heldAcceleratorBytes() chose — it names the
    // FIELD actually used, so a fallback is never presented as the
    // process-wide measurement.
    function heldBasis(data) {
      const snap = (data && typeof data === "object") ? data : {};
      const mem = (snap.memory && typeof snap.memory === "object") ? snap.memory : {};
      const dev = (mem.device && typeof mem.device === "object") ? mem.device : {};
      const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
      if (num(dev.process_held_bytes) !== null) return processHeldBasisWords(dev.process_held_basis);
      if (num(dev.mlx_held_bytes) !== null) return "MLX buffers only (this AbstractCore reports no process-wide figure)";
      if (num(dev.allocated_bytes) !== null) return "live allocations only (this AbstractCore reports no process-wide figure)";
      return "not reported";
    }
    function deviceMeterView(dev) {
      const d = (dev && typeof dev === "object") ? dev : {};
      const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
      const backend = d.backend ? String(d.backend) : "";
      const hostUsed = num(d.host_in_use_bytes);
      const wired = num(d.wired_limit_bytes);
      const total = num(d.total_bytes);
      const free = num(d.free_bytes);
      let procUsed = num(d.process_held_bytes);
      let procField = procUsed === null ? "" : `device.process_held_bytes (${processHeldBasisWords(d.process_held_basis)})`;
      if (procUsed === null) { procUsed = num(d.mlx_held_bytes); if (procUsed !== null) procField = "device.mlx_held_bytes (MLX live + cached buffers)"; }
      if (procUsed === null) { procUsed = num(d.allocated_bytes); if (procUsed !== null) procField = "device.allocated_bytes (live allocations)"; }
      if (procUsed !== null && !procField) procField = "device total − free";
      if (procUsed === null && total !== null && free !== null) procUsed = Math.max(0, total - free);
      // The ioreg "In use system memory" counter does NOT see MLX's Metal
      // buffers on this host (measured: 1.5 GB reported beside
      // 20.7 GB of live MLX buffers in one process; 1.1 GB beside 92 GB on the
      // operator's gateway). When this process alone exceeds the cross-process
      // figure, the process figure is the truthful one and the scope says so.
      const procWins = procUsed !== null && (hostUsed === null || procUsed > hostUsed);
      const scope = procWins ? "this process only" : "all processes";
      const used = procWins ? procUsed : hostUsed;
      const ceiling = wired === null ? total : wired;
      const label = `Accelerator heap · ${backend || "device"} (${scope})`;
      const note = "memory-mapped GGUF weights are not counted here";
      const fraction = (used !== null && ceiling !== null && ceiling > 0) ? used / ceiling : null;
      const value = used === null ? "unknown" : `${_fmtBytes(used)}${ceiling === null ? "" : ` / ${_fmtBytes(ceiling)}`}`;
      const bits = [note];
      bits.push(procWins
        ? (hostUsed === null
            ? `${procField} — THIS PROCESS ONLY; this host reports no cross-process accelerator figure, so a model resident in another process is not counted here`
            : `${procField} — THIS PROCESS ONLY; the cross-process ioreg counter (${_fmtBytes(hostUsed)}) does not see MLX buffers on this host`)
        : "device.host_in_use_bytes — driver-allocated accelerator memory across every process on this machine, not just this gateway");
      if (ceiling !== null) bits.push(wired !== null ? `ceiling: wired limit ${_fmtBytes(wired)}` : `ceiling: device total ${_fmtBytes(total)}`);
      if (hostUsed !== null && procUsed !== null) bits.push(`this process: ${_fmtBytes(procUsed)}`);
      if (wired !== null && total !== null) bits.push(`device total ${_fmtBytes(total)}`);
      return { scope, used, ceiling, fraction, label, note, value, title: bits.join(" · "), hostUsed, procUsed, wired, total };
    }
    // WHAT IS ACTUALLY EATING THE MEMORY, itemized (operator: "not only to see
    // the usage, but a more detailed usage where our models and caches appears
    // here"). PURE — it takes the snapshot and returns lines, so the rule set
    // is testable without a DOM.
    //
    // THREE KINDS OF LINE, in one order, on every residency surface:
    //   item      — a fact the framework knows, LABELLED with what it measures.
    //   reference — a separate counter, NOT summable with the items above it.
    //   note      — the GGUF explanation, only when Σ weights exceeds the heap.
    //
    // The old remainder line is GONE, key and all. It subtracted RAM-dimensioned
    // quantities (model weights, process RSS) from an ACCELERATOR counter
    // (host_in_use_bytes), computed −79 GB on this machine, clamped to "0 B"
    // and blamed "overlap" for what was a category error. No replacement
    // remainder is introduced: attributing against RAM would need a
    // per-process accounting this framework does not have, and a wrong second
    // remainder is how the first one happened.
    //
    // EMISSION RULE, identical on every surface: a line is emitted when its
    // value is KNOWN (non-null) and omitted when unknown. A known 0 IS
    // emitted. No line is "always emitted"; none is conditional on being
    // non-zero.
    function memoryBreakdown(data) {
      const snap = (data && typeof data === "object") ? data : {};
      const mem = (snap.memory && typeof snap.memory === "object") ? snap.memory : {};
      const ram = (mem.ram && typeof mem.ram === "object") ? mem.ram : {};
      const proc = (mem.process && typeof mem.process === "object") ? mem.process : {};
      const totals = (snap.totals && typeof snap.totals === "object") ? snap.totals : {};
      const num = (v) => (typeof v === "number" && isFinite(v) && v >= 0) ? v : null;
      // WHICH FIELD SUPPLIED THE NUMBER — an estimate is never presented as a
      // measurement, in the breakdown any more than in the table.
      const SOURCE_PHRASE = {
        size_bytes: "reported by the model server (size_bytes)",
        size_vram_bytes: "reported by the model server (size_vram_bytes)",
        est_weights_bytes: "estimated on-disk weight size (est_weights_bytes)",
      };
      // THE SHARED ITEM-KEY RULE (all four residency surfaces): a non-empty
      // `runtime_id` when the host sent one, else `<provider>:<model>` — real
      // sweep rows arrive with runtime_id NULL. No index suffix, no task
      // segment, no leading empty segment. Colliding keys are KEPT: a genuine
      // duplicate provider+model row is itself worth seeing.
      const itemKey = (row) => `model:${(typeof row.runtime_id === "string" && row.runtime_id)
        ? row.runtime_id
        : `${row.provider || ""}:${row.model || ""}`}`;
      const items = [];
      // 1..n — one line per RESIDENT row with a KNOWN display size. A resident
      // row whose size this host never reported is SKIPPED: no invented zero.
      const modelRows = Array.isArray(snap.models) ? snap.models.filter((r) => r && r.resident === true) : [];
      let sumWeights = null;
      for (const row of modelRows) {
        const size = modelDisplaySize(row);
        if (size.bytes === null) continue;
        sumWeights = (sumWeights === null ? 0 : sumWeights) + size.bytes;
        items.push({
          kind: "item",
          key: itemKey(row),
          name: row.model ? String(row.model) : (row.runtime_id || "model"),
          bytes: size.bytes,
          estimated: size.source === "est_weights_bytes",
          detail: `resident model weights · ${SOURCE_PHRASE[size.source] || size.source}`,
        });
      }
      // n+1 — prompt-cache bytes held FOR the resident models.
      let modelCaches = num(totals.cache_bytes_models);
      if (modelCaches === null) {
        modelCaches = modelRows.reduce((acc, r) => {
          const v = modelCacheBytes(r);
          return v === null ? acc : (acc === null ? 0 : acc) + v;
        }, null);
      }
      if (modelCaches !== null) {
        items.push({ kind: "item", key: "model_caches", name: "model KV caches", bytes: modelCaches, estimated: false, detail: "prompt-cache bytes held for resident models" });
      }
      // n+2 — prompt-cache bytes held BY gateway sessions.
      let sessionCaches = num(totals.session_cache_bytes);
      if (sessionCaches === null && Array.isArray(snap.session_caches)) {
        sessionCaches = snap.session_caches.reduce((acc, c) => {
          const v = (c && typeof c.bytes === "number" && isFinite(c.bytes) && c.bytes >= 0) ? c.bytes : null;
          return v === null ? acc : (acc === null ? 0 : acc) + v;
        }, null);
      }
      if (sessionCaches !== null) {
        items.push({ kind: "item", key: "session_caches", name: "session caches", bytes: sessionCaches, estimated: false, detail: "prompt-cache bytes held by gateway sessions" });
      }
      // n+3 — the gateway process itself. RSS is where memory-mapped GGUF
      // weights DO show up, which is why it is named here and in the note.
      const rss = num(proc.rss_bytes);
      if (rss !== null) {
        items.push({ kind: "item", key: "process_rss", name: "gateway process RSS", bytes: rss, estimated: false, detail: "resident set size of the gateway process — includes memory-mapped GGUF weights" });
      }
      // REFERENCES: separate counters. The renderer MUST put a rule between
      // these and the items so no reader adds the two groups together.
      const references = [];
      if (sumWeights !== null) {
        references.push({ kind: "reference", key: "sum_model_weights", name: "Σ model weights", bytes: sumWeights, value: _fmtBytes(sumWeights), detail: "sum of the resident model weights above" });
      }
      const ramUsed = num(ram.used_bytes);
      const ramTotal = num(ram.total_bytes);
      if (ramUsed !== null) {
        references.push({
          kind: "reference",
          key: "ram",
          name: "RAM used",
          bytes: ramUsed,
          value: ramTotal === null ? _fmtBytes(ramUsed) : `${_fmtBytes(ramUsed)} / ${_fmtBytes(ramTotal)}`,
          detail: "system memory in use / installed",
        });
      }
      const dev = deviceMeterView(mem.device);
      if (dev.used !== null) {
        references.push({ kind: "reference", key: "accelerator", name: dev.label, bytes: dev.used, value: dev.value, detail: dev.note });
      }
      // THE NOTE: emitted only when both figures are known and Σ weights
      // actually exceeds the heap. That is the NORMAL GGUF case, not an
      // inconsistency, and it must read as an explanation rather than a fault.
      const note = (sumWeights !== null && dev.used !== null && sumWeights > dev.used)
        ? {
            kind: "note",
            key: "gguf_mmap",
            text: "Σ model weights exceeds the accelerator heap. That is the normal case for memory-mapped GGUF weights: llama.cpp maps them from disk, so they are resident as process RSS and are not counted in the accelerator heap.",
          }
        : null;
      return { items, references, note };
    }
    // EJECTS THE RUNTIME OWES OR FAILED (default switch / failed load):
    // `residency_diagnostics` of GET /host/state = the runtime's
    // {pending_ejects: [{provider, model, reason, since}], last_switch_ejects:
    // [{provider, model, ok, skipped, deferred, reason, error, holders_found}]}.
    // PURE: lines of {tone, text}. A snapshot without the block says so — the
    // console never implies "nothing pending" from a field it did not get.
    function ejectStatusLines(data) {
      const snap = (data && typeof data === "object") ? data : {};
      if (!Array.isArray(snap.models)) return [];
      const diag = snap.residency_diagnostics;
      if (!diag || typeof diag !== "object") {
        return [{ tone: "muted", text: "Eject status: this gateway's host snapshot does not report residency diagnostics." }];
      }
      const label = (e) => [e && e.provider, e && e.model].filter((v) => v !== undefined && v !== null && String(v) !== "").join("/") || "a model";
      const out = [];
      const pending = Array.isArray(diag.pending_ejects) ? diag.pending_ejects.filter((e) => e && typeof e === "object") : [];
      const pendingKeys = new Set(pending.map(label));
      for (const e of pending) out.push({ tone: "warn", text: `Will eject ${label(e)} when the in-flight call ends.` });
      const last = Array.isArray(diag.last_switch_ejects) ? diag.last_switch_ejects.filter((e) => e && typeof e === "object") : [];
      for (const e of last) {
        const name = label(e);
        if (e.deferred === true) {
          if (!pendingKeys.has(name)) out.push({ tone: "warn", text: `Will eject ${name} when the in-flight call ends.` });
        } else if (e.ok === false) {
          out.push({ tone: "err", text: `${name}: eject failed: ${String(e.error || e.reason || "no reason reported")}` });
        } else if (e.skipped === true) {
          out.push({ tone: "muted", text: `${name} kept in memory: ${String(e.reason || "still in use")}` });
        } else {
          const n = Number(e.holders_found);
          out.push({ tone: "muted", text: `${name} ejected${Number.isFinite(n) && n > 0 ? ` from ${n} holder${n === 1 ? "" : "s"}` : ""}.` });
        }
      }
      return out;
    }
    function renderModelsEjects(data) {
      const box = $("models-ejects");
      if (!box) return;
      box.textContent = "";
      const lines = ejectStatusLines(data);
      box.classList.toggle("hidden", !lines.length);
      for (const l of lines) {
        const row = document.createElement("div");
        row.className = l.tone === "err" ? "mem-breakdown-note tone-err" : l.tone === "warn" ? "mem-breakdown-note tone-warn" : "mem-breakdown-note";
        row.textContent = l.text;
        box.append(row);
      }
    }
    function modelsEmptyRow(body, colSpan, text) {
	      if (!body) return;
	      body.textContent = "";
	      const tr = document.createElement("tr");
	      const td = document.createElement("td");
	      td.colSpan = colSpan;
	      td.className = "empty";
	      td.textContent = text;
	      tr.append(td);
	      body.append(tr);
	    }
	    // The CANONICAL modality palette (discovery contract:
	    // contracts.common.model_residency.modality_ui) — one palette for every
	    // residency client, never a console-local copy. Decoration only: a
	    // failed probe falls back to the neutral chip and retries next load.
	    const MODALITY_UI_FALLBACK = { color: "#6B7280", label: "Unknown" };
	    async function ensureModalityUi() {
	      if (state.modalityUi) return state.modalityUi;
	      try {
	        const caps = await api("/api/gateway/discovery/capabilities");
	        const ui = caps?.contracts?.common?.model_residency?.modality_ui;
	        state.modalityUi = (ui && typeof ui === "object" && ui.colors && typeof ui.colors === "object") ? ui : { colors: {} };
	      } catch {
	        return { colors: {} };
	      }
	      return state.modalityUi;
	    }
	    function modalityChipEl(task) {
	      const colors = (state.modalityUi && state.modalityUi.colors) || {};
	      const entry = (task && colors[task]) || colors.unknown || MODALITY_UI_FALLBACK;
	      const chip = document.createElement("span");
	      chip.className = "state-pill";
	      chip.textContent = entry.label || (task ? String(task) : "Unknown");
	      if (task) chip.title = String(task);
	      // Data-driven accent from the contract (the entity phase-badge
	      // precedent): inline style, hex validated so junk never reaches CSS.
	      const hex = /^#[0-9a-fA-F]{3,8}$/.test(String(entry.color || "")) ? entry.color : MODALITY_UI_FALLBACK.color;
	      if (chip.style) {
	        chip.style.color = hex;
	        chip.style.borderColor = `color-mix(in srgb, ${hex} 45%, transparent)`;
	        chip.style.background = `color-mix(in srgb, ${hex} 10%, transparent)`;
	      }
	      return chip;
	    }
	    function meterRow(label, fraction, valueText, title) {
	      // The shared determinate-bar recipe (.drive-track generalization): a
	      // null fraction renders an EMPTY track with honest value text, never
	      // a guessed bar. Thresholds ride the state tokens: warn >= 75%,
	      // crit >= 90%.
	      const row = document.createElement("div");
	      row.className = "meter-row";
	      const lab = document.createElement("span");
	      lab.className = "meter-label";
	      lab.textContent = label;
	      const track = document.createElement("div");
	      track.className = "meter-track";
	      if (title) track.title = title;
	      const fill = document.createElement("div");
	      fill.className = "meter-fill";
	      const pct = (typeof fraction === "number" && isFinite(fraction)) ? Math.max(0, Math.min(100, fraction * 100)) : null;
	      if (fill.style) fill.style.width = pct === null ? "0%" : `${Math.round(pct * 10) / 10}%`;
	      if (pct !== null && pct >= 90) fill.classList.add("crit");
	      else if (pct !== null && pct >= 75) fill.classList.add("warn");
	      track.append(fill);
	      const value = document.createElement("span");
	      value.className = "meter-value";
	      value.textContent = valueText;
	      row.append(lab, track, value);
	      return row;
	    }
	    function modelRowKey(row) {
	      return row.runtime_id || `${row.provider || ""}/${row.model || ""}`;
	    }
	    function residencyPill(row) {
	      // TRI-STATE, three visually distinct renderings: true (ok green),
	      // false = a configured/cached row NOT in memory (plain muted, said in
	      // words — default ≠ loaded is the critical distinction), null = the
	      // host does not know (info chip that SAYS unknown — text carries the
	      // state, color is never the sole channel).
	      const pill = document.createElement("span");
	      if (row.resident === true && row.provider_state === "resident_via_other_holders") {
	        // The runtime's own instance let go, but other provider instances in
	        // this process still hold the weights: resident, and eject frees all.
	        pill.className = "state-pill ok"; pill.textContent = "resident via other holders";
	      }
	      else if (row.resident === true) { pill.className = "state-pill ok"; pill.textContent = "resident"; }
	      else if (row.resident === false) { pill.className = "state-pill"; pill.textContent = "configured — not in memory"; }
	      else { pill.className = "state-pill covered"; pill.textContent = "unknown"; }
	      if (row.state) pill.title = `runtime state: ${row.state}`;
	      if (row.provider_state === "resident_via_other_holders" && Array.isArray(row.warnings) && row.warnings.length) {
	        pill.title = [pill.title, ...row.warnings.map(String)].filter(Boolean).join(" · ");
	      }
	      return pill;
	    }
	    function renderHostDegraded(data) {
	      const box = $("models-degraded");
	      if (!box) return;
	      box.textContent = "";
	      const degraded = Array.isArray(data.degraded) ? data.degraded : [];
	      const reasons = (data.reasons && typeof data.reasons === "object") ? data.reasons : {};
	      const names = [...new Set([...degraded, ...Object.keys(reasons)])];
	      for (const name of names) {
	        const pill = document.createElement("span");
	        pill.className = "pill";
	        pill.textContent = reasons[name] ? `${name} degraded — ${reasons[name]}` : `${name} degraded`;
	        box.append(pill);
	      }
	      box.classList.toggle("hidden", !names.length);
	    }
	    function renderHostMeters(data) {
	      const box = $("models-meters");
	      if (!box) return;
	      box.textContent = "";
	      const mem = (data.memory && typeof data.memory === "object") ? data.memory : {};
	      const ram = (mem.ram && typeof mem.ram === "object") ? mem.ram : {};
	      const ramFrac = (typeof ram.used_bytes === "number" && typeof ram.total_bytes === "number" && ram.total_bytes > 0)
	        ? ram.used_bytes / ram.total_bytes
	        : (typeof ram.percent === "number" ? ram.percent / 100 : null);
	      box.append(meterRow("RAM", ramFrac,
	        ramFrac === null ? "unknown" : `${_fmtBytes(ram.used_bytes)} / ${_fmtBytes(ram.total_bytes)} · ${_fmtPct(typeof ram.percent === "number" ? ram.percent : ramFrac * 100)}`,
	        "Host RAM used / total"));
	      // RAM stays the PRIMARY system meter — it is the one above, and it is
	      // the meter a reader takes as "how full is this machine". The
	      // accelerator heap is a second, separately-scoped line: the
	      // cross-process figure over the process-local one, the wired limit
	      // over the device total, and deviceMeterView's note ("memory-mapped
	      // GGUF weights are not counted here") leads the meter's title tooltip
	      // so the caveat travels with the number.
	      const devView = deviceMeterView(mem.device);
	      box.append(meterRow(devView.label, devView.fraction, devView.value, devView.title));
	      const gpu = (data.gpu && typeof data.gpu === "object") ? data.gpu : {};
	      if (gpu.supported === true) {
	        // GPU load renders only when the probe says supported — the
	        // unsupported case is already named in the degraded pills.
	        const util = (typeof gpu.utilization_gpu_pct === "number" && isFinite(gpu.utilization_gpu_pct)) ? gpu.utilization_gpu_pct : null;
	        box.append(meterRow("GPU load", util === null ? null : util / 100,
	          util === null ? "unknown" : _fmtPct(util),
	          gpu.source ? `GPU utilization via ${gpu.source}` : "GPU utilization"));
	      }
	    }
	    function renderHostBreakdown(data) {
	      // The itemized view sits directly UNDER the meters: a meter says how
	      // full a counter is, this says WHAT the framework can account for.
	      // Nothing here is fabricated — a figure this host did not report is not
	      // a line at all.
	      //
	      // ITEMS first, then a RULE, then the REFERENCE counters. The rule is
	      // load-bearing, not decoration: the references are separate measurements
	      // (Σ weights, RAM, the accelerator heap) and must never be read as more
	      // rows to add onto the items above them. The GGUF note closes the block
	      // when Σ weights exceeds the heap.
	      const box = $("models-breakdown");
	      if (!box) return;
	      box.textContent = "";
	      const view = memoryBreakdown(data);
	      if (!view.items.length && !view.references.length) { box.classList.add("hidden"); return; }
	      box.classList.remove("hidden");
	      const head = document.createElement("div");
	      head.className = "mem-breakdown-head";
	      head.textContent = "What is using memory";
	      head.title = "Every line is a figure this host reported. The counters below the rule are SEPARATE measurements of the same machine — not further items to add to the ones above.";
	      box.append(head);
	      const line = (entry, extraClass, valueText) => {
	        const row = document.createElement("div");
	        row.className = extraClass ? `mem-breakdown-row ${extraClass}` : "mem-breakdown-row";
	        const name = document.createElement("span");
	        name.className = "mem-breakdown-name";
	        name.textContent = entry.name;
	        if (entry.detail) {
	          const detail = document.createElement("span");
	          detail.className = "mem-breakdown-note";
	          detail.textContent = ` — ${entry.detail}`;
	          name.append(detail);
	        }
	        const value = document.createElement("span");
	        value.className = "mem-breakdown-bytes";
	        value.textContent = valueText;
	        row.append(name, value);
	        box.append(row);
	      };
	      // The SAME `~` estimate marker the table and the TUIs carry, so an
	      // estimated weight can never render as a measured one.
	      for (const item of view.items) line(item, "", `${item.estimated ? "~" : ""}${_fmtBytes(item.bytes)}`);
	      if (view.items.length && view.references.length) {
	        const rule = document.createElement("div");
	        rule.className = "mem-breakdown-rule";
	        box.append(rule);
	      }
	      for (const ref of view.references) line(ref, "reference", ref.value);
	      if (view.note) {
	        const note = document.createElement("div");
	        note.className = "mem-breakdown-note-line";
	        note.textContent = view.note.text;
	        box.append(note);
	      }
	    }
	    function renderHostFacts(data) {
	      const box = $("models-host-facts");
	      if (!box) return;
	      box.textContent = "";
	      const totals = (data.totals && typeof data.totals === "object") ? data.totals : {};
	      const host = (data.host && typeof data.host === "object") ? data.host : {};
	      // NO fabricated zeros: a degraded section (models/session_caches
	      // null) or an absent totals block must not read "0 models" beside a
	      // table that says unavailable — the fact line renders only when the
	      // section actually enumerated AND totals carries a real count.
	      // Truthful "N loaded": totals.models_resident (server-counted rows
	      // with resident === true), falling back to counting resident rows
	      // client-side. totals.models counts every known row — configured /
	      // cached included — and must never be presented as "loaded".
	      const modelRows = Array.isArray(data.models) ? data.models : null;
	      const nResident = modelRows === null ? null
	        : (typeof totals.models_resident === "number" ? totals.models_resident : modelRows.filter((r) => r && r.resident === true).length);
	      const nKnown = modelRows === null ? null : (typeof totals.models === "number" ? totals.models : modelRows.length);
	      const residentBytes = modelRows === null ? null : modelRows.reduce(
	        (acc, r) => (r && r.resident === true && typeof r.size_bytes === "number") ? (acc === null ? r.size_bytes : acc + r.size_bytes) : acc, null);
	      const nCaches = (Array.isArray(data.session_caches) && typeof totals.session_caches === "number") ? totals.session_caches : null;
	      // PROCESS RSS IS STATED EXACTLY ONCE, and the one place is the
	      // breakdown's `process_rss` item — where it is labelled with what it
	      // measures ("includes memory-mapped GGUF weights"). It used to render
	      // here TOO, so one memory panel carried the same 76 GB twice with two
	      // different framings; double-counting is precisely the confusion this
	      // section exists to remove. Host id / host name stay: identity is not
	      // a duplicate figure.
	      const rows = [
	        ["Host", host.host_name || host.host_id || ""],
	        ["Models", nResident === null ? "" : `${nResident} resident${residentBytes === null ? "" : ` · ${_fmtBytes(residentBytes)}`}${typeof nKnown === "number" && nKnown > nResident ? ` · ${nKnown - nResident} configured / cached` : ""}`],
	        ["Session caches", nCaches === null ? "" : (nCaches === 0 ? "0 caches" : `${nCaches} cache${nCaches === 1 ? "" : "s"} · ${totals.session_cache_bytes == null ? "size unknown" : _fmtBytes(totals.session_cache_bytes)}`)],
	      ];
	      for (const [k, v] of rows) {
	        if (!v) continue;
	        const line = document.createElement("div");
	        line.className = "entity-kv";
	        const key = document.createElement("span"); key.className = "entity-kv-key"; key.textContent = k;
	        const val = document.createElement("span"); val.className = "entity-kv-val"; val.textContent = String(v);
	        line.append(key, val);
	        box.append(line);
	      }
	    }
	    function estimateDetailRow(est, colSpan) {
	      const tr = document.createElement("tr");
	      tr.className = "capability-derived";
	      const td = document.createElement("td");
	      td.colSpan = colSpan;
	      const bits = [`context estimate: ${est.confidence || "unknown"}`];
	      if (typeof est.predicted_max_context === "number") bits.push(`predicted max ${_fmtCtx(est.predicted_max_context)}`);
	      if (typeof est.calibrated_context_length === "number") bits.push(`calibrated ${_fmtCtx(est.calibrated_context_length)}`);
	      if (Array.isArray(est.notes) && est.notes.length) bits.push(est.notes.join("; "));
	      if (est.error) bits.push(String(est.error));
	      td.textContent = bits.join(" · ");
	      tr.append(td);
	      return tr;
	    }
	    async function estimateModelContext(row, btn) {
	      if (!row.provider || !row.model) return;
	      if (btn) btn.disabled = true;
	      const key = modelRowKey(row);
	      try {
	        const est = await api(withQuery("/api/gateway/models/context_estimate", { provider: row.provider, model: row.model, context_length: row.context_length || null }));
	        state.modelEstimates.set(key, est || {});
	      } catch (e) {
	        // The estimate is per-row detail: its failure lands IN the detail
	        // row, labeled, never as a silent no-op.
	        state.modelEstimates.set(key, { confidence: "unknown", error: String(e.message || e) });
	      } finally {
	        if (btn) btn.disabled = false;
	      }
	      if (state.hostState) renderModelsTable(state.hostState);
	    }
	    function modelUnloadTarget(row) {
	      return row.runtime_id ? { runtime_id: row.runtime_id } : { provider: row.provider, model: row.model };
	    }
	    function _modelsMutationResult(res) {
	      // The facade degrades IN-BAND at 200 ({ok:false, error}) — surface it
	      // as the failure it is instead of painting a success line.
	      if (res && res.ok === false) throw new Error(String(res.error || res.detail || res.code || "the host refused the operation"));
	      return res;
	    }
	    function _modelsLockedRefusal(e) {
	      // The force dialog is gated on the 409 BODY carrying the
	      // model_locked code (any spelling the server's own detector
	      // accepts), never on the status alone — a proxy's or another
	      // route's 409 must not offer a force-unload it cannot mean.
	      if (!e || e.status !== 409) return false;
	      const body = (e.data && typeof e.data === "object") ? e.data : ((e.detail && typeof e.detail === "object") ? e.detail : {});
	      const hit = (v) => v === "model_locked" || (v && typeof v === "object" && (v.code === "model_locked" || v.error === "model_locked"));
	      if (Object.values(body).some(hit)) return true;
	      return String(e.message || "").includes("model_locked");
	    }
	    async function unloadModel(row, btn) {
	      const name = `${row.provider || "?"}/${row.model || "?"}`;
	      const ok = await confirmAction({
	        title: "Unload model",
	        message: `Unload ${name} from host memory? The next request that needs it pays the full load again.`,
	        confirmLabel: "Unload",
	        danger: true,
	      });
	      if (!ok) return;
	      const msg = $("models-loaded-message");
	      if (btn) btn.disabled = true;
	      try {
	        _modelsMutationResult(await api("/api/gateway/models/unload", { slow: true, method: "POST", body: JSON.stringify(modelUnloadTarget(row)) }));
	        msg.textContent = `Unloaded ${name}.`;
	        msg.className = "message ok";
	      } catch (e) {
	        if (_modelsLockedRefusal(e)) {
	          // 409 + model_locked in the body — the ONE unload refusal with a
	          // second, deliberate way through. The force confirm is its own act.
	          const force = await confirmAction({
	            title: "Model locked",
	            message: `${name} is locked in memory — the lock exists to keep it resident. Force the unload anyway?`,
	            confirmLabel: "Force unload",
	            danger: true,
	          });
	          if (force) {
	            try {
	              _modelsMutationResult(await api("/api/gateway/models/unload", { slow: true, method: "POST", body: JSON.stringify({ ...modelUnloadTarget(row), force: true }) }));
	              msg.textContent = `Force-unloaded ${name}.`;
	              msg.className = "message ok";
	            } catch (e2) {
	              msg.textContent = String(e2.message || e2);
	              msg.className = "message error";
	            }
	          }
	        } else if (e && e.status === 409) {
	          // A 409 that does NOT carry model_locked is some other conflict:
	          // never offer a force it cannot mean.
	          msg.textContent = `Unload conflicted (HTTP 409): ${String(e.message || e)}`;
	          msg.className = "message error";
	        } else {
	          msg.textContent = String(e.message || e);
	          msg.className = "message error";
	        }
	      } finally {
	        if (btn) btn.disabled = false;
	        await loadHostState({ quiet: true });
	      }
	    }
	    async function toggleModelLock(row, btn) {
	      const name = `${row.provider || "?"}/${row.model || "?"}`;
	      const locking = row.locked !== true;
	      const msg = $("models-loaded-message");
	      if (btn) btn.disabled = true;
	      try {
	        const path = locking ? "/api/gateway/models/lock" : "/api/gateway/models/unlock";
	        _modelsMutationResult(await api(path, { method: "POST", body: JSON.stringify(modelUnloadTarget(row)) }));
	        msg.textContent = `${locking ? "Locked" : "Unlocked"} ${name}.`;
	        msg.className = "message ok";
	      } catch (e) {
	        msg.textContent = String(e.message || e);
	        msg.className = "message error";
	      } finally {
	        if (btn) btn.disabled = false;
	        await loadHostState({ quiet: true });
	      }
	    }
	    function renderModelsResidentCount(n) {
	      const title = $("models-loaded-title");
	      if (!title) return;
	      // The section header counts RESIDENT rows only — the truthful "N
	      // loaded". Unknown (models section degraded) renders no count. Titled
	      // "Models" (not "Loaded models") so the header can never contradict
	      // the configured/cached rows the toggle reveals beneath it.
	      title.textContent = n === null ? "Models" : `Models (${n} resident)`;
	    }
	    function renderModelsShowCachedToggle(n) {
	      const label = $("models-show-cached-label");
	      const text = $("models-show-cached-text");
	      const box = $("models-show-cached");
	      if (!label || !text || !box) return;
	      if (!n && state.modelsShowCached) state.modelsShowCached = false;
	      label.classList.toggle("hidden", !n);
	      text.textContent = `Show configured / cached (${n})`;
	      box.checked = Boolean(state.modelsShowCached);
	    }
	    function renderModelsTable(data) {
	      const body = $("models-table");
	      if (!body) return;
	      body.textContent = "";
	      const admin = Boolean(state.principal && state.principal.admin);
	      const rows = Array.isArray(data.models) ? data.models : null;
	      const residentRows = rows === null ? [] : rows.filter((r) => r && r.resident === true);
	      const cachedRows = rows === null ? [] : rows.filter((r) => r && r.resident !== true);
	      renderModelsResidentCount(rows === null ? null : residentRows.length);
	      renderModelsShowCachedToggle(rows === null ? 0 : cachedRows.length);
	      if (rows === null) {
	        const reason = data.reasons && data.reasons.models ? ` — ${data.reasons.models}` : "";
	        modelsEmptyRow(body, 8, `Model residency unavailable on this host${reason}.`);
	        return;
	      }
	      // DEFAULT VIEW = provider-verified RESIDENT rows only. Configured /
	      // cached rows (resident false or unknown) sit behind the toggle:
	      // presenting every capability-default model as "loaded" was the
	      // operator defect this fixes — default ≠ loaded.
	      const visible = state.modelsShowCached ? residentRows.concat(cachedRows) : residentRows;
	      if (!visible.length) {
	        // NEVER say "nothing loaded" over live accelerator memory. The host
	        // snapshot carries the process's MLX allocator truth
	        // (device.mlx_held_bytes = live + cached buffers; older gateways:
	        // allocated_bytes = live only). A gateway once said "No models
	        // loaded" while holding 92 GB the listing could not attribute.
	        const held = heldAcceleratorBytes(data);
	        if (held > 0) {
	          const behind = rows.length ? ` ${cachedRows.length} configured / cached row${cachedRows.length === 1 ? "" : "s"} behind the toggle above.` : "";
	          const holders = heldResidentNames(data);
          const what = holders.length ? `held by ${holders.join("; ")}` : "not attributed to any model — no in-process model holder reports it";
          modelsEmptyRow(body, 8, `Gateway still holds ${_fmtBytes(held)} of accelerator memory (no model listed) · measured by ${heldBasis(data)} · ${what} · ${heldDetail(data)}. Eject the held model, or restart the gateway to free it.${behind}`);
	        } else if (!rows.length) modelsEmptyRow(body, 8, "No models loaded right now.");
	        else modelsEmptyRow(body, 8, `No models resident in memory right now — ${cachedRows.length} configured / cached row${cachedRows.length === 1 ? "" : "s"} behind the toggle above.`);
	        return;
	      }
	      for (const row of visible) {
	        const tr = document.createElement("tr");
	        const modTd = document.createElement("td");
	        modTd.append(modalityChipEl(row.task));
	        tr.append(modTd);
	        const provTd = document.createElement("td");
	        provTd.textContent = row.provider || "";
	        tr.append(provTd);
	        const modelTd = document.createElement("td");
	        const code = document.createElement("code");
	        code.textContent = row.model || "";
	        const titleBits = [];
	        if (row.source) titleBits.push(`source: ${row.source}`);
	        if (Array.isArray(row.modalities) && row.modalities.length) titleBits.push(`modalities: ${row.modalities.join(", ")}`);
	        if (row.loaded_at) titleBits.push(`loaded ${row.loaded_at}`);
	        if (row.last_used_at) titleBits.push(`last used ${row.last_used_at}`);
	        if (row.expires_at) titleBits.push(`expires ${row.expires_at}`);
	        if (row.host_name || row.host_id) titleBits.push(`host ${row.host_name || row.host_id}`);
	        if (titleBits.length) modelTd.title = titleBits.join(" · ");
	        modelTd.append(code);
	        tr.append(modelTd);
	        const resTd = document.createElement("td");
	        resTd.append(residencyPill(row));
	        tr.append(resTd);
	        // SIZE (operator: "an estimate of the current memory footprint used
	        // for each model currently loaded"). The coalesced display size
	        // renders for EVERY row that has one — an MLX/HF row carrying only
	        // est_weights_bytes used to render blank — and an est_weights_bytes
	        // figure carries the SAME `~` prefix the TUIs render, ON SCREEN and
	        // not only in the tooltip: a marker nobody hovers to see is a marker
	        // nobody sees. The tooltip still names the source field. cache_bytes
	        // rides as a secondary figure when the host reports it.
	        const sizeTd = document.createElement("td");
	        const sizeView = modelDisplaySize(row);
	        const cacheBytes = modelCacheBytes(row);
	        const sizeMark = sizeView.source === "est_weights_bytes" ? "~" : "";
	        sizeTd.textContent = sizeView.bytes === null
	          ? (cacheBytes === null ? "" : `${_fmtBytes(cacheBytes)} cache`)
	          : (cacheBytes === null ? `${sizeMark}${_fmtBytes(sizeView.bytes)}` : `${sizeMark}${_fmtBytes(sizeView.bytes)} + ${_fmtBytes(cacheBytes)} cache`);
	        const sizeTitle = [sizeView.bytes === null
	          ? "size unknown — this host reported no size for the model"
	          : `${sizeView.label} (${sizeView.source})`];
	        if (typeof row.size_vram_bytes === "number" && sizeView.source !== "size_vram_bytes") sizeTitle.push(`VRAM ${_fmtBytes(row.size_vram_bytes)}`);
	        if (cacheBytes !== null) sizeTitle.push(`prompt/KV cache ${_fmtBytes(cacheBytes)}`);
	        sizeTd.title = sizeTitle.join(" · ");
	        tr.append(sizeTd);
	        const ctxTd = document.createElement("td");
	        ctxTd.textContent = _fmtCtx(row.context_length);
	        if (row.context_calibrated === true) {
	          const mark = document.createElement("span");
	          mark.className = "state-pill ok";
	          mark.textContent = "calibrated";
	          mark.title = typeof row.calibrated_context_length === "number"
	            ? `calibrated max context ${_fmtCtx(row.calibrated_context_length)}`
	            : "context length calibrated on this host";
	          ctxTd.append(document.createTextNode(" "), mark);
	        }
	        tr.append(ctxTd);
	        const flagsTd = document.createElement("td");
	        // The lock flag rides locked===true ALONE (a runtime can report a
	        // lock without reporting lockability — the lock is the fact that
	        // explains the 409, so it must never hide behind lockable:null);
	        // only the Lock/Unlock BUTTON stays gated on endpoint
	        // availability. ICONS.lock (registry SVG), never the raw emoji.
	        if (row.locked === true) {
	          const lockChip = document.createElement("span");
	          lockChip.className = "pill";
	          lockChip.title = "Locked in memory — unload refuses until unlocked or forced";
	          lockChip.innerHTML = `<span class="chip-icon" aria-hidden="true">${ICONS.lock}</span>locked`;
	          flagsTd.append(lockChip);
	        }
	        if (row.default === true) {
	          const b = document.createElement("span");
	          b.className = "badge";
	          b.textContent = "default";
	          flagsTd.append(b);
	        }
	        if (row.pinned === true) {
	          const b = document.createElement("span");
	          b.className = "badge";
	          b.textContent = "pinned";
	          flagsTd.append(b);
	        }
	        tr.append(flagsTd);
	        const actionsTd = document.createElement("td");
	        const act = document.createElement("div");
	        act.className = "actions";
	        if (row.provider && row.model) {
	          const est = document.createElement("button");
	          est.className = "secondary";
	          est.type = "button";
	          est.textContent = "Estimate";
	          est.title = "Ask the host how much context actually fits for this model (calibrated when it has measured)";
	          est.onclick = () => estimateModelContext(row, est);
	          act.append(est);
	        }
	        if (admin) {
	          // A LOCK ON EVERY LINE (operator: "i should have a lock on each
	          // line to lock/unlock a model"). Lock now ADOPTS an externally
	          // loaded resident model (LM Studio / ollama swept from the host),
	          // so a sweep-resident row whose `lockable` the host never reported
	          // (null) is lockable too — only an EXPLICIT lockable:false
	          // withholds the control. A locked row always offers Unlock,
	          // resident or not: a locked-but-EVICTED lock still blocks facade
	          // unloads and must never be stranded (unlock never requires
	          // residency host-side). Non-resident configured rows keep Estimate
	          // only — there is nothing in memory to lock.
	          //
	          // The ADOPT wording is keyed on `source === "provider_server"`,
	          // NOT on `lockable`: the sweep stamps every row it finds
	          // `lockable: true`, so the old `row.lockable === true` test could
	          // never distinguish a gateway-loaded model from an adopted one and
	          // the adopt sentence never fired. `source` is the field that
	          // actually says the model server loaded it outside the Gateway,
	          // and it is what the TUIs have always read.
	          const canLock = row.locked !== true && row.lockable !== false && row.resident === true;
	          if (row.locked === true || canLock) {
	            const lockBtn = document.createElement("button");
	            lockBtn.className = "secondary";
	            lockBtn.type = "button";
	            lockBtn.textContent = row.locked === true ? "Unlock" : "Lock";
	            lockBtn.title = row.locked === true
	              ? (row.resident === true
	                ? "Release the memory lock so this model can be unloaded or evicted"
	                : "Release a lock whose model is no longer in memory (the lock still blocks unloads)")
	              : (row.source === "provider_server"
	                ? "Lock this model in memory — this host loaded it outside the Gateway, so locking adopts it first"
	                : "Lock this model in memory so nothing can evict it");
	            lockBtn.onclick = () => toggleModelLock(row, lockBtn);
	            act.append(lockBtn);
	          }
	          if (row.resident === true) {
	            // Unload renders for RESIDENT rows only: a "configured — not in
	            // memory" row has nothing in memory to unload.
	            const unload = document.createElement("button");
	            unload.className = "danger";
	            unload.type = "button";
	            unload.textContent = "Unload";
	            unload.title = "Unload this model from host memory";
	            unload.onclick = () => unloadModel(row, unload);
	            act.append(unload);
	          }
	        }
	        actionsTd.append(act);
	        tr.append(actionsTd);
	        body.append(tr);
	        // A fetched estimate survives the 5s repaint: it lives in state,
	        // keyed by row identity, and re-renders under its row every paint.
	        const estData = state.modelEstimates.get(modelRowKey(row));
	        if (estData) body.append(estimateDetailRow(estData, 8));
	      }
	    }
	    async function clearSessionCache(sessionId, btn) {
	      const ok = await confirmAction({
	        title: "Clear session caches",
	        message: `Clear every prompt cache for session ${sessionId}? The next turn re-encodes its prompt from scratch — nothing durable is lost.`,
	        confirmLabel: "Clear",
	        danger: true,
	      });
	      if (!ok) return;
	      // The caches section's OWN message line: the finally-refresh's
	      // success path clears #models-message, so a refused clear written
	      // there would vanish before the admin could read it.
	      const msg = $("models-caches-message");
	      if (btn) btn.disabled = true;
	      try {
	        const res = await api(`/api/gateway/sessions/${encodeURIComponent(sessionId)}/prompt_cache/clear_all`, { method: "POST", body: JSON.stringify({}) });
	        _modelsMutationResult(res);
	        msg.textContent = `Cleared ${typeof res.count === "number" ? res.count : "the"} cache${res.count === 1 ? "" : "s"} for ${sessionId}.`;
	        msg.className = "message ok";
	      } catch (e) {
	        msg.textContent = String(e.message || e);
	        msg.className = "message error";
	      } finally {
	        if (btn) btn.disabled = false;
	        await loadHostState({ quiet: true });
	      }
	    }
	    function renderSessionCaches(data) {
	      const body = $("models-caches-table");
	      if (!body) return;
	      body.textContent = "";
	      const admin = Boolean(state.principal && state.principal.admin);
	      const rows = Array.isArray(data.session_caches) ? data.session_caches : null;
	      if (rows === null) {
	        const reason = data.reasons && data.reasons.session_caches ? ` — ${data.reasons.session_caches}` : "";
	        modelsEmptyRow(body, 6, `Session cache enumeration unavailable on this host${reason}.`);
	        return;
	      }
	      if (!rows.length) {
	        modelsEmptyRow(body, 6, "No session prompt caches right now.");
	        return;
	      }
	      for (const c of rows) {
	        const tr = document.createElement("tr");
	        const sessTd = document.createElement("td");
	        const code = document.createElement("code");
	        code.textContent = c.session_id || "";
	        if (c.key) sessTd.title = String(c.key);
	        sessTd.append(code);
	        tr.append(sessTd);
	        const modelTd = document.createElement("td");
	        modelTd.textContent = [c.provider, c.model].filter(Boolean).join("/");
	        if (c.runtime_id) modelTd.title = String(c.runtime_id);
	        tr.append(modelTd);
	        const sizeTd = document.createElement("td");
	        sizeTd.textContent = typeof c.bytes === "number" ? _fmtBytes(c.bytes) : "";
	        tr.append(sizeTd);
	        const tokTd = document.createElement("td");
	        tokTd.textContent = typeof c.token_count === "number" ? c.token_count.toLocaleString() : "";
	        tr.append(tokTd);
	        const createdTd = document.createElement("td");
	        createdTd.textContent = _fmtEpochS(c.created_at_s);
	        if (typeof c.last_used_at_s === "number") createdTd.title = `last used ${_fmtEpochS(c.last_used_at_s)}`;
	        tr.append(createdTd);
	        const actTd = document.createElement("td");
	        if (admin && c.session_id) {
	          const act = document.createElement("div");
	          act.className = "actions";
	          const clear = document.createElement("button");
	          clear.className = "danger";
	          clear.type = "button";
	          clear.textContent = "Clear";
	          clear.title = "Clear every prompt cache for this session";
	          clear.onclick = () => clearSessionCache(c.session_id, clear);
	          act.append(clear);
	          actTd.append(act);
	        }
	        tr.append(actTd);
	        body.append(tr);
	      }
	    }
	    // THE OFFICIAL DROPDOWNS (operator: "both the provider and model should
	    // be the official dropdown components ... select the provider, which
	    // then auto refresh the list of available models for that provider").
	    // The warm-up row rides the console's OWN select recipe
	    // (setSelectOptions) over the SAME discovery cache the capability-
	    // defaults tab fills — fetchDefaultProviders / fetchDefaultModels, keyed
	    // in state.providerModels — so there is one fetch path, one cache, and
	    // one retry discipline, not a second copy. The free text the datalists
	    // used to allow survives as the established custom lane: it opens ONLY
	    // when discovery has nothing to offer, because offline is a supported
	    // mode here and a select with an empty catalog is a dead end.
	    function activeModelsLoadProvider() {
	      return customLaneValue("models-load-provider-custom") || String($("models-load-provider").value || "").trim();
	    }
	    function activeModelsLoadModel() {
	      return customLaneValue("models-load-model-custom") || String($("models-load-model").value || "").trim();
	    }
	    function renderModelsLoadForm({ rebuildOptions = true } = {}) {
	      const form = $("models-load-form");
	      if (!form) return;
	      const admin = Boolean(state.principal && state.principal.admin);
	      form.classList.toggle("hidden", !admin);
	      // The 5s poll repaints QUIETLY: rebuilding the option lists every tick
	      // would drop the operator's half-made selection mid-click.
	      if (!admin || !rebuildOptions) return;
	      void syncModelsLoadProviderOptions();
	    }
	    let _modelsCatalogSeq = 0;
	    async function syncModelsLoadProviderOptions() {
	      const select = $("models-load-provider");
	      if (!select) return;
	      const seq = ++_modelsCatalogSeq;
	      const keep = activeModelsLoadProvider();
	      setSelectOptions(select, [], { emptyLabel: "Loading providers...", disabled: true });
	      setCustomLane("models-load-provider-custom", false);
	      let providers = [];
	      let failed = "";
	      try {
	        providers = await fetchDefaultProviders(null);
	      } catch (e) {
	        failed = String(e.message || e);
	      }
	      if (seq !== _modelsCatalogSeq) return; // a newer rebuild owns the row
	      setSelectOptions(select, providers, {
	        emptyLabel: providers.length ? "Select provider..." : (failed ? "Provider discovery failed" : "No providers discovered"),
	        disabled: !providers.length,
	        selected: keep,
	      });
	      // Nothing discovered = the offline case: open the typing lane so the
	      // warm-up row stays usable instead of becoming an empty dead select.
	      setCustomLane("models-load-provider-custom", !providers.length, keep);
	      await syncModelsLoadModelOptions();
	    }
	    async function syncModelsLoadModelOptions() {
	      const select = $("models-load-model");
	      if (!select) return;
	      const seq = ++_modelsCatalogSeq;
	      const provider = activeModelsLoadProvider();
	      const keep = activeModelsLoadModel();
	      if (!provider) {
	        setSelectOptions(select, [], { emptyLabel: "Select provider first", disabled: true });
	        setCustomLane("models-load-model-custom", false);
	        return;
	      }
	      setSelectOptions(select, [], { emptyLabel: "Loading models...", disabled: true });
	      setCustomLane("models-load-model-custom", false);
	      let models = [];
	      let failed = "";
	      try {
	        models = await fetchDefaultModels(provider, null);
	      } catch (e) {
	        failed = String(e.message || e);
	      }
	      if (seq !== _modelsCatalogSeq) return; // a newer provider owns the row
	      setSelectOptions(select, models, {
	        emptyLabel: models.length ? "Select model..." : (failed ? "Model discovery failed" : "No models discovered for this provider"),
	        disabled: !models.length,
	        selected: keep,
	      });
	      // NEVER seed the typing lane from `keep`: `keep` is the model chosen
	      // under the PREVIOUS provider, so warming it here produced the
	      // newProvider/oldModel pair. An empty catalog opens an EMPTY lane
	      // (console-tui already does exactly this).
	      setCustomLane("models-load-model-custom", !models.length, "");
	    }
	    let _modelsHintSeq = 0;
	    async function updateModelsLoadHint() {
	      const hint = $("models-load-hint");
	      if (!hint) return;
	      const provider = activeModelsLoadProvider();
	      const model = activeModelsLoadModel();
	      if (!provider || !model) {
	        hint.textContent = "";
	        hint.classList.add("hidden");
	        return;
	      }
	      const seq = ++_modelsHintSeq;
	      hint.classList.remove("hidden");
	      hint.textContent = "Estimating context fit…";
	      try {
	        const est = await api(withQuery("/api/gateway/models/context_estimate", { provider, model }));
	        if (seq !== _modelsHintSeq) return; // a newer provider/model pair owns the hint
	        const parts = [];
	        if (typeof est.predicted_max_context === "number") parts.push(`~${_fmtCtx(est.predicted_max_context)} ctx fits`);
	        parts.push(est.confidence || "unknown");
	        if (Array.isArray(est.notes) && est.notes.length) parts.push(est.notes.join("; "));
	        hint.textContent = parts.join(" — ");
	      } catch (e) {
	        if (seq !== _modelsHintSeq) return;
	        hint.textContent = "No context estimate: " + String(e.message || e);
	      }
	    }
	    async function loadModelResidency() {
	      const msg = $("models-loaded-message");
	      const provider = activeModelsLoadProvider();
	      const model = activeModelsLoadModel();
	      if (!provider || !model) {
	        msg.textContent = "Provider and model are required to warm a model up.";
	        msg.className = "message error";
	        return;
	      }
	      const lock = $("models-load-lock").checked;
	      const btn = $("models-load-button");
	      if (btn) btn.disabled = true;
	      msg.textContent = `Loading ${provider}/${model} — a cold load can take minutes…`;
	      msg.className = "message";
	      try {
	        _modelsMutationResult(await api("/api/gateway/models/load", { slow: true, method: "POST", body: JSON.stringify({ provider, model }) }));
	        // The shipped load request has no lock field (pin is not lock):
	        // locking is its own verb, so the checkbox rides a second call —
	        // in its OWN try: a failed lock after a successful load is a MIXED
	        // outcome, and reporting it as total failure would hide that the
	        // model IS now resident (just unlocked).
	        let lockError = "";
	        if (lock) {
	          try {
	            _modelsMutationResult(await api("/api/gateway/models/lock", { method: "POST", body: JSON.stringify({ provider, model }) }));
	          } catch (lockErr) {
	            lockError = String(lockErr.message || lockErr);
	          }
	        }
	        if (lockError) {
	          msg.textContent = `Loaded ${provider}/${model} (now resident, UNLOCKED) — locking failed: ${lockError}`;
	          msg.className = "message error";
	        } else {
	          msg.textContent = `Loaded ${provider}/${model}${lock ? " (locked in memory)" : ""}.`;
	          msg.className = "message ok";
	        }
	      } catch (e) {
	        msg.textContent = String(e.message || e);
	        msg.className = "message error";
	      } finally {
	        if (btn) btn.disabled = false;
	        await loadHostState({ quiet: true });
	      }
	    }
	    function renderHostState(data, { quiet = false } = {}) {
	      renderHostDegraded(data);
	      renderHostMeters(data);
	      renderHostBreakdown(data);
	      renderHostFacts(data);
	      renderModelsTable(data);
	      renderModelsEjects(data);
	      renderSessionCaches(data);
	      // Quiet (poll) repaints skip the datalist rebuild — replacing the
	      // <option>s every 5s flicks an open suggestion popup shut.
	      renderModelsLoadForm({ rebuildOptions: !quiet });
	    }
	    async function loadHostState({ quiet = false } = {}) {
	      if (!state.principal) return;
	      // Sequence guard (the manageToken precedent): overlapping snapshots
	      // must land in REQUEST order, not resolution order — a hung slow-lane
	      // fetch resolving minutes late must never overwrite a fresher
	      // snapshot (it would resurrect an unloaded model on screen).
	      const seq = state.hostStateSeq = (state.hostStateSeq || 0) + 1;
	      const msg = $("models-message");
	      if (!quiet) {
	        tableLoadingRow($("models-table"), 8, "Reading host residency…");
	        tableLoadingRow($("models-caches-table"), 6, "Loading session caches…");
	      }
	      try {
	        await ensureModalityUi();
	        // Slow lane: a host mid-load answers late, not never.
	        const data = await api("/api/gateway/host/state", { slow: true });
	        if (seq !== state.hostStateSeq) return; // a newer call owns the paint
	        state.hostState = data;
	        if (msg) { msg.textContent = ""; msg.className = "message"; }
	        renderHostState(data, { quiet });
	        loadGatewayHost();
	      } catch (e) {
	        if (seq !== state.hostStateSeq) return; // stale failure: newer call owns the paint
	        // Labeled failure replaces content (stale pixels are the incident
	        // class): the message names the failure and every section says
	        // unavailable rather than keeping the last snapshot on screen.
	        state.hostState = null;
	        if (msg) { msg.textContent = "Host state unavailable: " + String(e.message || e); msg.className = "message error"; }
	        modelsEmptyRow($("models-table"), 8, "Host state unavailable.");
	        modelsEmptyRow($("models-caches-table"), 6, "Host state unavailable.");
	        const meters = $("models-meters"); if (meters) meters.textContent = "";
	        const breakdown = $("models-breakdown"); if (breakdown) { breakdown.textContent = ""; breakdown.classList.add("hidden"); }
	        const facts = $("models-host-facts"); if (facts) facts.textContent = "";
	        const deg = $("models-degraded"); if (deg) { deg.textContent = ""; deg.classList.add("hidden"); }
	      }
	    }
	    // ---- Gateway card (Resources tab) + paused banner (every tab) ----------
	    // (host pause / desktop tray / restart / update). Reads render
	    // for every signed-in user; mutations are admin-gated at render time.
	    function _gwFmtWhen(iso) { try { const d = new Date(iso); return isNaN(d) ? String(iso || "") : d.toLocaleString(); } catch { return String(iso || ""); } }
	    function _gwMsg(text, kind) { const msg = $("gateway-host-message"); if (!msg) return; msg.textContent = text || ""; msg.className = kind ? `message ${kind}` : "message"; }
	    function renderPausedBanner(runner) {
	      const el = $("paused-banner");
	      if (!el) return;
	      const paused = Boolean(runner && runner.paused && state.principal);
	      el.classList.toggle("hidden", !paused);
	      if (paused) {
	        const by = runner.paused_by ? ` by ${runner.paused_by}` : "";
	        const when = runner.paused_at ? ` since ${_gwFmtWhen(runner.paused_at)}` : "";
	        $("paused-banner-text").textContent = `Workflows are paused${when}${by} — the gateway keeps answering; nothing new runs until you resume.`;
	        $("paused-banner-resume").classList.toggle("hidden", !(state.principal && state.principal.admin));
	      }
	    }
	    function renderGatewayHost(runner, tray) {
	      if (runner) state.hostRunner = runner;
	      if (tray) state.hostTray = tray;
	      renderPausedBanner(state.hostRunner);
	      const admin = Boolean(state.principal && state.principal.admin);
	      const badge = $("gateway-host-state");
	      const btn = $("gateway-host-pause");
	      if (runner && badge) {
	        const paused = Boolean(runner.paused);
	        const inflight = Number(runner.inflight_ticks || 0);
	        badge.textContent = paused ? (inflight > 0 ? "Pausing…" : "Paused — still running") : (inflight > 0 ? `Running · working on ${inflight} step${inflight === 1 ? "" : "s"}` : "Running");
	        badge.className = "state-pill " + (paused ? "off" : "ok");
	        const detail = $("gateway-host-detail");
	        if (paused) {
	          detail.textContent = [runner.paused_at ? _gwFmtWhen(runner.paused_at) : "", runner.paused_by ? `by ${runner.paused_by}` : "", runner.reason || ""].filter(Boolean).join(" · ");
	        } else {
	          detail.textContent = runner.runner_in_process === false ? "Workflows run in a separate runner process; pausing reaches it through the shared data folder." : "";
	        }
	        // A persistent state = a switch labelled by the state (DESIGN §2).
	        btn.setAttribute("aria-checked", paused ? "true" : "false");
	        btn.removeAttribute("aria-busy");
	        btn.classList.toggle("hidden", !admin);
	        const caps = runner.capabilities || {};
	        const restart = $("gateway-host-restart"); const quit = $("gateway-host-quit");
	        restart.classList.toggle("hidden", !admin); quit.classList.toggle("hidden", !admin);
	        restart.disabled = !caps.restart;
	        restart.title = caps.restart ? "Gracefully restart this gateway process (same command, same settings)" : String(caps.reason || "Restart is not available for this launch");
	        quit.disabled = !caps.shutdown;
	        quit.title = caps.shutdown ? "Stop this gateway process" : String(caps.reason || "Quit is not available for this launch");
	      }
	      const note = $("gateway-host-tray-note");
	      if (tray && note) {
	        const sup = tray.supervisor || {}; const dec = tray.decision || {};
	        let text;
	        if (sup.running && sup.ready !== false) text = `shown (pid ${sup.pid})`;
	        else if (sup.running) text = "starting…";
	        else if (dec.reason === "missing_dependency") text = `not installed — ${tray.install_hint}`;
	        else if (dec.reason === "headless") text = `not available here — ${dec.hint || "no display"}`;
	        else if (dec.reason === "dev_reload") text = "not available while running with --reload";
	        else if (dec.reason === "not_serving") text = "not available (this process was not started with `abstractgateway serve`)";
	        else if (sup.failure && sup.failure.reason) text = `not running — ${sup.failure.reason}${sup.failure.hint ? " (" + sup.failure.hint + ")" : ""}`;
	        else if (sup.error) text = `not running — ${sup.error}`;
	        else text = "not running";
	        note.textContent = text;
	      }
	    }
	    // The gateway renders the update state ONCE (`update`: status, line, hint, action with
	    // its command, source and confirmation) for the web console, the terminal console and
	    // the tray; this panel shows it and the job's log.
	    function renderGatewayUpdate(upd) {
	      state.hostUpdate = upd || null;
	      const el = $("gateway-host-version"); if (!el || !upd) return;
	      const view = upd.update || {}; const job = upd.job || {};
	      let text = String(view.line || upd.current || "?");
	      if (view.status === "up_to_date" && view.checked_at) text += `, checked ${_gwFmtWhen(view.checked_at)}`;
	      el.textContent = text;
	      const action = view.action || null;
	      const startBtn = $("gateway-host-update-start");
	      startBtn.classList.toggle("hidden", !(action && view.status === "available"));
	      startBtn.textContent = action ? action.label : "Update";
	      $("gateway-host-update-hint").textContent = String(view.hint || "");
	      const log = Array.isArray(job.log_tail) ? job.log_tail : [];
	      const box = $("gateway-host-update-log-box");
	      box.classList.toggle("hidden", !log.length && !job.command);
	      $("gateway-host-update-log-summary").textContent = job.state === "running" ? "Update log (running…)" : `Update log (${job.state || "idle"})`;
	      $("gateway-host-update-log").textContent = [job.command ? "$ " + job.command.join(" ") : "", ...log, job.error ? "\\n" + job.error : ""].filter(Boolean).join("\\n");
	      if (job.state === "failed") box.open = true;
	    }
	    async function loadGatewayHost() {
	      if (!state.principal) return;
	      try {
	        const [runner, tray] = await Promise.all([api("/api/gateway/host/runner"), api("/api/gateway/host/tray")]);
	        renderGatewayHost(runner, tray);
	        _gwMsg("");
	      } catch (e) {
	        _gwMsg("Gateway state unavailable: " + String(e.message || e), "error");
	      }
	      if (state.principal && state.principal.admin) {
	        $("gateway-host-login-row").classList.remove("hidden");
	        loadStartAtLogin("gateway-host");
	        try { renderGatewayUpdate(await api("/api/gateway/host/update")); } catch {}
	      }
	    }
	    // START AT LOGIN (GET/PUT /api/gateway/host/start-at-login, admin): one
	    // switch rendered in the Gateway card ("gateway-host") and on the setup
	    // guide's Done step ("first-run"). Every change is confirmed, then
	    // VERIFIED by a fresh GET — the text shows the read-back, never the wish.
	    const startAtLoginState = {};
	    function renderStartAtLogin(scope, st, error) {
	      const text = $(`${scope}-login-text`); const btn = $(`${scope}-login-toggle`);
	      if (!text || !btn) return;
	      startAtLoginState[scope] = st || null;
	      if (!st) {
	        text.textContent = error ? `unavailable — ${error}` : "…";
	        btn.classList.add("hidden");
	        return;
	      }
	      const lead = st.enabled ? "On" : ({ off: "Off", broken: "Needs repair", other: "Another gateway" }[st.state] || String(st.state));
	      text.textContent = st.can_change ? `${lead} — ${st.summary}` : `${lead} — can't be changed here: ${st.reason}`;
	      btn.classList.toggle("hidden", !st.can_change);
	      btn.disabled = false;
	      // A switch labelled by the feature (DESIGN §2). Needs-repair and
	      // another gateway's registration read OFF: switching on repairs or
	      // replaces it (the confirmation says which).
	      btn.setAttribute("aria-checked", st.enabled ? "true" : "false");
	      btn.title = st.enabled ? "On: this gateway starts when you log in" : ({ broken: "Needs repair: switch on to repair", other: "Another gateway starts at login: switch on to use this one" }[st.state] || "Off: nothing starts the gateway at login");
	    }
	    async function loadStartAtLogin(scope) {
	      try { renderStartAtLogin(scope, await api("/api/gateway/host/start-at-login")); }
	      catch (e) { renderStartAtLogin(scope, null, String(e.message || e)); }
	    }
	    async function toggleStartAtLogin(scope) {
	      const st = startAtLoginState[scope]; if (!st || !st.can_change) return;
	      const turnOn = !st.enabled;
	      const ok = await confirmAction(turnOn
	        ? { title: "Start AbstractGateway at login?", message: `Registers ${st.mechanism_label} so this gateway starts when you log in.${st.state === "other" ? ` It replaces the registration for another gateway (${st.other_data_dir || "another data folder"}).` : ""} The gateway running now is not restarted.`, confirmLabel: "Start at login" }
	        : { title: "Stop starting at login?", message: "The gateway keeps running now; it will not start at your next login.", confirmLabel: "Stop starting at login" });
	      if (!ok) return;
	      const btn = $(`${scope}-login-toggle`); if (btn) btn.disabled = true;
	      let err = "";
	      try { await api("/api/gateway/host/start-at-login", { method: "PUT", body: JSON.stringify({ enabled: turnOn, replace_other: st.state === "other" }) }); }
	      catch (e) { err = String(e.message || e); }
	      // Verify by GET, in every scope that shows the switch.
	      await Promise.all(["gateway-host", "first-run"].filter((sc) => $(`${sc}-login-text`)).map(loadStartAtLogin));
	      const now = startAtLoginState[scope];
	      const msg = err ? `Start at login was not changed: ${err}` : (now && now.enabled === turnOn ? "" : `Start at login did not read back as ${turnOn ? "on" : "off"}: ${(now && now.summary) || "unknown"}`);
	      if (scope === "gateway-host") _gwMsg(msg, msg ? "error" : "");
	      else if (msg) { $("first-run-message").textContent = msg; $("first-run-message").className = "message error"; }
	    }
	    async function toggleGatewayPause() {
	      const runner = state.hostRunner || {}; const btn = $("gateway-host-pause");
	      if (btn.getAttribute("aria-busy") === "true") return;
	      const pause = !runner.paused;
	      btn.setAttribute("aria-checked", pause ? "true" : "false");
	      btn.setAttribute("aria-busy", "true");
	      try {
	        const out = await api(pause ? "/api/gateway/host/pause" : "/api/gateway/host/resume", { method: "POST", body: JSON.stringify({}) });
	        renderGatewayHost(out, null);
	        _gwMsg(pause ? "Workflows are paused: nothing new runs until you switch this off." : "Workflows are running.", "ok");
	      } catch (e) {
	        btn.setAttribute("aria-checked", runner.paused ? "true" : "false");
	        btn.removeAttribute("aria-busy");
	        _gwMsg(String(e.message || e), "error");
	      }
	    }
	    async function restartGateway() {
	      const ok = await confirmAction({ title: "Restart AbstractGateway?", message: "Running workflows pause at their next step and continue after the restart. The console is unavailable for a few seconds.", confirmLabel: "Restart" });
	      if (!ok) return;
	      try { await api("/api/gateway/host/restart", { method: "POST", body: JSON.stringify({ reason: "console" }) }); _gwMsg("Restarting… reload this page in a few seconds."); }
	      catch (e) { _gwMsg(String(e.message || e), "error"); }
	    }
	    async function quitGateway() {
	      const ok = await confirmAction({ title: "Quit AbstractGateway?", message: "Workflows stop and this console goes offline until you start AbstractGateway again.", confirmLabel: "Quit", danger: true });
	      if (!ok) return;
	      try { await api("/api/gateway/host/shutdown", { method: "POST", body: JSON.stringify({ reason: "console" }) }); _gwMsg("Quitting… the gateway is shutting down."); }
	      catch (e) { _gwMsg(String(e.message || e), "error"); }
	    }
	    async function checkGatewayUpdate() {
	      const btn = $("gateway-host-update-check"); btn.disabled = true;
	      try { renderGatewayUpdate(await api("/api/gateway/host/update/check", { method: "POST", body: JSON.stringify({}), slow: true })); _gwMsg(""); }
	      catch (e) { _gwMsg(String(e.message || e), "error"); }
	      finally { btn.disabled = false; }
	    }
	    async function startGatewayUpdate() {
	      const upd = state.hostUpdate || {}; const action = (upd.update && upd.update.action) || null;
	      if (!action) return;
	      const ok = await confirmAction({ title: "Update available", message: action.confirm, confirmLabel: "Update now" });
	      if (!ok) return;
	      const body = action.installer_sha256 ? { installer_sha256: action.installer_sha256 } : {};
	      try { renderGatewayUpdate(await api("/api/gateway/host/update/start", { method: "POST", body: JSON.stringify(body) })); _gwMsg(""); _pollGatewayUpdate(); }
	      catch (e) {
	        // Refused (the installer changed, one already runs, check again first): the offer
	        // is stale, so drop it like the tray does; the next step is a new check.
	        if (upd.update) upd.update.action = null;
	        $("gateway-host-update-start").classList.add("hidden");
	        _gwMsg(`${String(e.message || e)}. Check now to check again.`, "error");
	      }
	    }
	    function _pollGatewayUpdate() {
	      if (typeof setTimeout === "undefined") return;
	      setTimeout(async () => {
	        if (!state.principal) return;
	        try { const upd = await api("/api/gateway/host/update"); renderGatewayUpdate(upd); if (upd.job && upd.job.state === "running") _pollGatewayUpdate(); } catch {}
	      }, 3000);
	    }
	    // Paused must be visible on EVERY tab: a light 15s poll while signed in
	    // (token-guarded chain, the host-state poll precedent).
	    function _schedulePausedPoll(token) {
	      if (typeof setTimeout === "undefined") return;
	      setTimeout(async () => {
	        if (token !== state.pausedPollToken || !state.principal) return;
	        try { const runner = await api("/api/gateway/host/runner"); state.hostRunner = runner; renderPausedBanner(runner); } catch {}
	        _schedulePausedPoll(token);
	      }, 15000);
	    }
	    function startPausedPoll() { state.pausedPollToken = (state.pausedPollToken || 0) + 1; _schedulePausedPoll(state.pausedPollToken); }
	    function _scheduleHostStatePoll(token) {
	      // Token-guarded self-rescheduling chain (the manage-panel precedent),
	      // additionally scoped to the ACTIVE tab and a live session: leaving
	      // the Models tab or signing out ends the chain — a hidden tab must
	      // never keep the host walking its residency every 5 seconds.
	      if (typeof setTimeout === "undefined") return;
	      setTimeout(async () => {
	        if (token !== state.hostPollToken) return;
	        if (state.activeTab !== "models" || !state.principal) return;
	        try { await loadHostState({ quiet: true }); } catch {}
	        _scheduleHostStatePoll(token);
	      }, 5000);
	    }
	    function startHostStatePoll() {
	      state.hostPollToken = (state.hostPollToken || 0) + 1;
	      _scheduleHostStatePoll(state.hostPollToken);
	    }
	    function renderAccount(me) {
	      const p = me?.principal;
	      state.principal = p || null;
	      state.meAuth = (me && me.auth && typeof me.auth === "object") ? me.auth : null;  // the create-user modal reads user_auth_enabled
		      if (!p) {
		        document.body.classList.remove("signed-in");
		        $("page-title").textContent = "AbstractGateway Console";
		        $("page-subtitle").textContent = "Users & summoned entities, runtimes, providers, and multimodal capabilities";
		        $("account").textContent = "No active session.";
	        state.runtimeConfig = null;
	        state.myWorkspacePolicy = null;
	        // Signed out: kill the host-state poll chain (the principal check
	        // inside the poll is the belt; the token bump is the suspenders).
	        state.hostPollToken = (state.hostPollToken || 0) + 1;
	        state.pausedPollToken = (state.pausedPollToken || 0) + 1;
	        state.hostRunner = null;
	        renderPausedBanner(null);
	        $("users-section").classList.add("hidden");
	        $("runtime-reservations-section").classList.add("hidden");
	      $("defaults-scope").textContent = "Sign in to edit provider/model defaults for this Gateway runtime.";
	      // A signed-out console must not keep naming a store it can no longer
	      // read — the path is evidence from a session that just ended.
	      renderStoreAuthority("defaults-authority", null);
	      renderStoreAuthority("endpoint-profiles-authority", null);
	        setLoginStatus("Not signed in", "neutral");
	        setStatus(false, "Signed out");
		        return;
		      }
		      document.body.classList.add("signed-in");
		      $("account").innerHTML = `
	        <span><span class="muted">Runtime</span> <code>${esc(p.tenant_id)}/${esc(p.runtime_id || p.user_id)}</code></span>
	        <span><span class="muted">Roles</span> ${(p.roles || []).map((role) => `<span class="badge">${esc(role)}</span>`).join(" ") || '<span class="badge">none</span>'}</span>
	      `;
      // The drill-in (open manage panel) supersedes the admin toggle for
      // users-section — a background refresh must not re-show it under
      // the manage view.
      $("users-section").classList.toggle("hidden", Boolean(state.manageName));
      // RBAC: a non-admin sees the same table (own row + own entities) without the admin tools.
      $("open-create-user").classList.toggle("hidden", !p.admin);
      $("email-caps-section").classList.toggle("hidden", !p.admin);
      // DESIGN-v2 §2: the entities are rows of the Accounts table (for everyone, RBAC-scoped);
      // the admin's own email UI opens from their row (Email); a user keeps it on the page too.
      if (!state.manageName) $("entities-list-section").classList.add("hidden");
      // DESIGN-v2 §2.3 (adversary pass 2, F1): the account email UI lives ONLY in the own row's
      // Email modal, for everyone; it is never rendered on the page.
      if (!$("my-email-section").classList.contains("in-modal")) $("my-email-section").classList.add("hidden");
      $("runtimes-section").classList.toggle("hidden", !p.admin);
      // Retained runtimes: shown only for admins AND only when reservations
      // exist (the Runtimes tab is the table + the
      // tabbed panel — recovery UI appears when there is something to
      // recover, never as standing clutter). renderRuntimeReservations
      // handles the has-rows half.
      if (!p.admin) $("runtime-reservations-section").classList.add("hidden");
      // The detail pane shows only for admins WITH a live selection — a
      // background account refresh must not re-hide an open detail, and a
      // non-admin must never see it (the pane replaced the
      // old global runs-section).
      $("runtime-detail-section").classList.toggle("hidden", !p.admin || !state.selectedRuntime);
      // Both Runtimes sections are admin-gated, so for a non-admin the tab
      // would render EMPTY — hide the tab itself and fold a
      // restored runtimes selection back to the first tab.
      $("tab-button-runtimes").classList.toggle("hidden", !p.admin);
      // Workflows stays visible to everyone — listing and exporting are
      // user-level. Only the WRITE affordance is admin-gated, matching the
      // server rule; hiding the tab would hide the workflows a user runs.
      $("workflows-import").classList.toggle("hidden", !p.admin);
      if (!p.admin && state.activeTab === "runtimes") setActiveTab("users");
      // Models tab: reads render for EVERY authenticated user (unlike the
      // all-admin runtimes tab) — only the mutation surfaces are gated: the
      // warm-up form here, per-row Unload/Lock and cache Clear at render
      // time inside renderModelsTable/renderSessionCaches.
      renderModelsLoadForm();
      applyEntityAdminGating();
      $("defaults-scope").textContent = p.admin
        ? "Editing as admin changes the Gateway multimodal capability defaults. Users inherit these unless they set their own runtime defaults."
        : "Editing here changes your runtime multimodal capability defaults. Unset routes inherit the Gateway defaults.";
      // `apply-recommended` rewrites the HOST-WIDE store, so it is admin-only
      // server-side (403 otherwise). A non-admin can still set their own
      // routes row by row — offering them a button that can only fail is worse
      // than not offering it.
      $("defaults-apply-recommended").classList.toggle("hidden", !p.admin);
	      initEndpointProfileFormOptions();
	      setStatus(true, `${p.tenant_id}/${p.user_id}`);
	      // A `#<tab>` link (the tray menu's console entries: `#runtimes` for
	      // "Open Runs in Console") lands on that tab ONCE per page load; later
	      // account refreshes must not yank the user back there. Any tab id
	      // works — the fragment is a deep link, not a special case, and
	      // `setActiveTab` already folds an unknown id onto the first tab.
	      // `#catalog?quant=8bit&provider=mlx`: the tab is before the `?`, the
	      // catalog's filters after it (console_catalog.py reads them).
	      const hashTab = String(location.hash || "").replace(/^#/, "").split("?")[0].trim();
	      const wantedTab = TAB_FOLDS[hashTab] || hashTab;
	      if (!state.hashApplied && TABS.includes(wantedTab)) {
	        state.hashApplied = true;
	        state.activeTab = wantedTab;
	        setActiveTab(wantedTab);
	        if (wantedTab === "models") { loadHostState(); startHostStatePoll(); }
	        if (wantedTab === "users") loadMyEmail();
	        if (wantedTab === "catalog" || wantedTab === "providers" || wantedTab === "apps" || wantedTab === "network") openCoreTab(wantedTab);
	        // `#apps?open=<id>&path=<p>` (app_proxy.py sends a signed-out page
	        // load of /apps/<id>/... here): open that app, signed in.
	        if (wantedTab === "apps") {
	          const q = new URLSearchParams(String(location.hash || "").split("?").slice(1).join("?"));
	          if (q.get("open")) appOpenFromLink(q.get("open"), q.get("path") || "/");
	        }
	      } else {
	        setActiveTab(state.activeTab);
	      }
	      startPausedPoll();
	      loadGatewayHost();
	      $("open-setup").classList.toggle("hidden", !p.admin);
	      // Skills & MCP is admin configuration (round 3, C3F): non-admins do not get the entry.
	      $("tab-button-skills").classList.toggle("hidden", !p.admin);
	      // The header shows the gateway's primary address (GET /network).
	      if (!netStore.data && !netStore.loading) netRefresh({ quiet: true });
	      maybeOpenFirstRun();
	    }
    function principalKind(u) {
      const kind = String((u && u.principal_kind) || "").trim().toLowerCase();
      if (kind === "human" || kind === "entity") return kind;
      return (u && Array.isArray(u.roles) && u.roles.some((r) => String(r || "").trim().toLowerCase() === "entity"))
        ? "entity"
        : "human";
    }
    // Users table (DESIGN §5.1): User | Role | Email address | Mailbox |
    // Runtime | Active (switch) | Actions. No State column, no Disable
    // button, no per-row email/agent-tools switches. Deactivating and
    // deleting confirm INLINE in a row under the user (never a dialog).
    function userRoleLabel(u) {
      const roles = (u && Array.isArray(u.roles) ? u.roles : []).map((r) => String(r || "").trim().toLowerCase());
      if (roles.includes("admin")) return "Admin";
      if (roles.includes("readonly")) return "Read-only";
      return "User";
    }
    function userMailboxNote(acc) {
      // DESIGN §5.1 (same words as the terminal console): a per-user override
      // (the old per-row buttons', or the capabilities v3 migration's pin) is
      // shown with Reset (a one-shot action that clears it); mailboxes off for
      // everyone (the admin's switch) is said, with nothing to reset.
      if (!acc || acc.state === "unknown") return { text: "", reset: false };
      const caps = acc.capabilities;
      if (!caps || typeof caps !== "object") {
        console.error("AbstractGateway console: /admin/users rows carry no email_account.capabilities; an old per-user mailbox override cannot be shown (gateway-api seam, DESIGN §5.1).");
        return { text: "", reset: false };
      }
      const pinned = (k) => Boolean(caps[k] && caps[k].source === "user" && caps[k].value === false);
      if (pinned("email")) return { text: "not allowed for this user", reset: true };
      if (pinned("email_agent_tools")) return { text: "agent email tools not allowed for this user", reset: true };
      if (acc.admin_enabled === false) return { text: "mailboxes off", reset: false };
      return { text: "", reset: false };
    }
    function userMailboxText(acc) {
      if (!acc || acc.state === "unknown") return "—";
      return acc.configured && acc.address ? `connected as ${acc.address}` : "not connected";
    }
    function isOwnRow(u) {
      const p = state.principal || {};
      return String(p.user_id || "") === String(u.user_id || "") && String(p.tenant_id || "default") === String(u.tenant_id || "default");
    }
    function usersMessage(text, tone) {
      const msg = $("users-message");
      msg.textContent = text || "";
      msg.className = tone ? `message ${tone}` : "message";
    }
    function userConfirmRow(tr, text, confirmLabel, onConfirm) {
      // One inline confirmation at a time, directly under the user's row.
      for (const old of Array.from(document.querySelectorAll ? document.querySelectorAll("#users-table .row-confirm") : [])) old.remove();
      const row = document.createElement("tr");
      row.className = "row-confirm";
      const td = document.createElement("td");
      td.colSpan = 6;
      const box = document.createElement("div");
      box.className = "inline-confirm";
      box.setAttribute("role", "group");
      const span = document.createElement("span");
      span.textContent = text;
      const yes = document.createElement("button");
      yes.type = "button";
      yes.className = "danger";
      yes.textContent = confirmLabel;
      const no = document.createElement("button");
      no.type = "button";
      no.className = "secondary";
      no.textContent = "Cancel";
      const close = () => { row.remove(); };
      no.onclick = close;
      yes.onclick = async () => {
        yes.disabled = true;
        yes.setAttribute("aria-busy", "true");
        try { await onConfirm(); close(); }
        catch (e) { yes.disabled = false; yes.removeAttribute("aria-busy"); usersMessage(String((e && e.message) || e), "error"); }
      };
      box.append(span, yes, no);
      td.append(box);
      row.append(td);
      tr.after(row);
      try { no.focus(); } catch {}
      return row;
    }
    // ---- Accounts (DESIGN-v2 §2): ONE table for users AND entities, read from
    // GET /admin/accounts (gateway-api, §6) -- the same resolver the account page
    // uses, so a row and the Email modal never disagree (§2.5). Row tint by kind
    // (kit .af-row--admin|user|entity), an Active switch on every row, and every
    // action that cannot apply is disabled WITH its reason visible in the row.
    const accountsUi = { rows: [], emailRelease: null, logsRelease: null, emailHome: null, logsFor: null, logsKind: "" };
    const ACCOUNT_KIND_LABEL = { admin: "Admin", user: "User", entity: "Entity" };
    const ACCOUNT_ROLE_TITLE = { admin: "Admin — manages this gateway", user: "User — signs in and runs their own agents", entity: "Entity — a persistent identity that acts on this gateway" };
    const ACCOUNT_ACTIONS = ["email", "logs", "workspace", "rotate", "manage", "delete", "suspend"];
    function accountKindClass(a) {
      if (a.kind === "entity") return "entity";
      return a.role === "admin" ? "admin" : "user";
    }
    function accountAction(a, key) {
      const act = a && a.actions && a.actions[key];
      if (!act || typeof act.available !== "boolean") {
        throw new Error(`GET /admin/accounts row ${a && a.id} has no actions.${key} (gateway-api seam, DESIGN-v2 §6).`);
      }
      return act;
    }
    // Words, never placeholder dashes (round-2 polish): an entity's mailbox is "Not available"
    // (the reason is in its Email modal), a missing address is "No address".
    function accountMailboxText(a) {
      const m = a.mailbox || {};
      if (m.state === "connected") return m.address ? `Connected as ${m.address}` : "Connected";
      if (m.state === "paused") return "Paused";
      if (m.state === "not_connected") return "Not connected";
      if (m.state === "unavailable") return "Not available";
      throw new Error(`GET /admin/accounts row ${a.id} has mailbox.state ${JSON.stringify(m.state)} (gateway-api seam, DESIGN-v2 §6).`);
    }
    // The phone line "address · mailbox" says the address once: a mailbox on the same
    // account reads "Mailbox connected" instead of repeating it.
    function accountPhoneLine(a) {
      const m = a.mailbox || {};
      const address = a.email_address || "No address";
      const mailbox = (m.state === "connected" && m.address && m.address === a.email_address) ? "Mailbox connected" : accountMailboxText(a);
      return `${address} · ${mailbox}`;
    }
    // Reasons of the unavailable actions as ONE short muted line, without "Delete:" prefixes
    // (the disabled button sits right beside it). An entity whose Rotate AND Delete are both
    // unavailable gets one combined sentence (explicit rule on the two action keys).
    const ACCOUNT_ENTITY_ROTATE_DELETE = "Rotate and Delete don't apply to entities: no credential is kept, and an entity's name is kept for life — suspend it instead.";
    const ACCOUNT_OWN_SUSPEND_DELETE = "You can't deactivate or delete your own account.";
    function accountReasonsLine(a, keys) {
      const off = ["suspend", ...keys].filter((k) => !accountAction(a, k).available);
      const out = [];
      let rest = off;
      if (a.kind === "entity" && off.includes("rotate") && off.includes("delete")) {
        out.push(ACCOUNT_ENTITY_ROTATE_DELETE);
        rest = rest.filter((k) => k !== "rotate" && k !== "delete");
      }
      if (a.own === true && off.includes("suspend") && off.includes("delete")) {
        out.push(ACCOUNT_OWN_SUSPEND_DELETE);
        rest = rest.filter((k) => k !== "suspend" && k !== "delete");
      }
      for (const k of rest) {
        const why = accountAction(a, k).reason || "Not available for this account.";
        if (!out.includes(why)) out.push(why);
      }
      return out.join(" ");
    }
    function accountIsOwn(a) {
      const p = state.principal || {};
      return a.kind !== "entity" && String(p.user_id || "") === String(a.id || "") && String(p.tenant_id || "default") === String(a.tenant_id || "default");
    }
    function accountUserRecord(a) {
      return (state.users || []).find((u) => String(u.user_id) === String(a.id) && String(u.tenant_id || "default") === String(a.tenant_id || "default")) || null;
    }
    // RBAC (operator ruling 2026-10-01, rbac lane): an admin reads every account from
    // /admin/accounts; anyone else reads /me/accounts (their own row + the entities they created).
    function accountsAdmin() { return Boolean(state.principal && state.principal.admin); }
    async function loadAccounts() {
      const path = accountsAdmin() ? "/api/gateway/admin/accounts" : "/api/gateway/me/accounts";
      const out = await api(path);
      if (!out || !Array.isArray(out.accounts)) throw new Error(`GET ${path.replace("/api/gateway", "")} answered without an accounts list (gateway seam, DESIGN-v2 §6).`);
      accountsUi.rows = out.accounts;
      renderAccounts(out.accounts);
      return out.accounts;
    }
    function accountButton(label, opts) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = opts.cls || "secondary";
      b.innerHTML = (opts.icon ? `<span class="button-icon" aria-hidden="true">${opts.icon}</span>` : "") + `<span>${esc(label)}</span>`;
      b.setAttribute("aria-label", opts.aria || label);
      b.setAttribute("data-label", label);
      if (opts.title) b.title = opts.title;
      return b;
    }
    function renderAccounts(rows) {
      const tbody = $("users-table");
      tbody.textContent = "";
      if (!rows.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="empty">No accounts yet.</td>`;
        tbody.append(tr);
        return;
      }
      for (const a of rows) {
        for (const key of ACCOUNT_ACTIONS) accountAction(a, key);
        const kind = accountKindClass(a);
        const tr = document.createElement("tr");
        tr.className = `af-row--${kind} accounts-row`;
        tr.setAttribute("data-user", a.id);
        tr.setAttribute("data-kind", a.kind);
        const shown = (a.tenant_id && a.tenant_id !== "default") ? `${a.tenant_id}/${a.id}` : a.id;
        const email = a.email_address || "No address";
        const emailCell = a.email_address ? esc(a.email_address) : `<span class="accounts-none af-row__muted">No address</span>`;
        const mailbox = accountMailboxText(a);
        const mailboxNone = a.mailbox && a.mailbox.state === "unavailable";
        tr.innerHTML = `<td data-label="Name" class="accounts-name"><span class="accounts-name__line"><strong>${esc(shown)}</strong> <span class="af-kind-chip af-kind-chip--${kind}" title="${esc(ACCOUNT_ROLE_TITLE[kind])}">${ACCOUNT_KIND_LABEL[kind]}</span></span>`
          + `<span class="accounts-fold af-row__muted"><span>${esc(email)}</span><span>${a.runtime_id ? `Runtime ${esc(a.runtime_id)}` : "No runtime"}</span></span></td>`
          + `<td data-label="Email address" class="accounts-col-email">${emailCell}</td>`
          + `<td data-label="Mailbox" class="accounts-mailbox${mailboxNone ? " accounts-mailbox--none" : ""}"><span class="accounts-phone-line af-row__muted">${esc(accountPhoneLine(a))}</span><span class="accounts-mailbox__text${mailboxNone ? " af-row__muted" : ""}">${esc(mailbox)}</span></td>`
          + `<td data-label="Runtime" class="accounts-col-runtime">${a.runtime_id ? `<code>${esc(a.runtime_id)}</code>` : `<span class="af-row__muted">None</span>`}</td>`
          + `<td data-label="Active" class="users-active accounts-active"></td>`;
        // Active (§2.2): users = registry `enabled`; entities = suspend / resume.
        const activeCell = tr.querySelector(".accounts-active");
        const suspend = accountAction(a, "suspend");
        const sw = afSwitchCreate({
          id: `account-active-${a.tenant_id || "default"}-${a.id}`.replace(/[^A-Za-z0-9_-]/g, "-"),
          label: "Active",
          ariaLabel: `Active: ${a.id}`,
          checked: Boolean(a.active),
          small: true,
          unavailableReason: suspend.available ? "" : (suspend.reason || "This account can't be switched here."),
        });
        activeCell.append(...sw.nodes);
        const setActive = async (next) => {
          const out = await api(`/api/gateway/admin/accounts/${encodeURIComponent(a.id)}/active`, { method: "PUT", body: JSON.stringify({ active: next }) });
          if (!out || out.id !== a.id) throw new Error("PUT /admin/accounts/{id}/active answered without the updated account (gateway-api seam, DESIGN-v2 §6).");
          Object.assign(a, out);
          return out;
        };
        afSwitchBind(sw.button, async (next) => {
          if (!next) {
            const question = a.kind === "entity"
              ? `Suspend ${a.id}? It stops acting until you turn Active back on.`
              : `Deactivate ${a.id}? They are signed out until you turn Active back on.`;
            userConfirmRow(tr, question, a.kind === "entity" ? "Suspend" : "Deactivate", async () => {
              afSwitchSet(sw.button, { checked: true, busy: true });
              try {
                await setActive(false);
                afSwitchSet(sw.button, { checked: false });
                usersMessage(a.kind === "entity" ? `${a.id} is suspended.` : `${a.id} is deactivated.`, "ok");
                refreshUsersOnly();
              } catch (e) {
                afSwitchSet(sw.button, { checked: true });
                throw e;
              }
            });
            return false;
          }
          await setActive(true);
          usersMessage(a.kind === "entity" ? `${a.id} is active again (${a.entity_state || "awake"}).` : `${a.id} is active again.`, "ok");
          refreshUsersOnly();
          return true;
        }, (e) => usersMessage(emailErrorText(e), "error"));
        // Actions (§2.1): Email · Logs · Workspace · Rotate · Manage (entities) · Delete.
        const actions = document.createElement("td");
        actions.className = "actions accounts-actions";
        actions.setAttribute("data-label", "Actions");
        const buttons = document.createElement("div");
        buttons.className = "accounts-actions__buttons";
        const shownKeys = [];
        const add = (key, btn, onClick) => {
          const act = accountAction(a, key);
          btn.setAttribute("data-action", key);
          shownKeys.push(key);
          if (!act.available) {
            btn.disabled = true;
            btn.title = act.reason || "Not available for this account.";
          } else {
            btn.onclick = onClick;
          }
          buttons.append(btn);
        };
        if (a.kind === "entity") {
          // DESIGN-v2 §2.3: an entity's Email opens the modal that says why it has no mailbox
          // (actions.email.reason), rather than a dead button.
          const b = accountButton("Email", { icon: ICONS.mail, aria: `Email for ${a.id}` });
          b.setAttribute("data-action", "email");
          b.onclick = () => openAccountEmail(a);
          buttons.append(b);
        } else {
          add("email", accountButton("Email", { icon: ICONS.mail, aria: `Email for ${a.id}` }), () => openAccountEmail(a));
        }
        add("logs", accountButton("Logs", { icon: ICONS.logs, aria: `Activity of ${a.id}` }), () => openAccountLogs(a));
        add("workspace", accountButton("Workspace", { icon: ICONS.folder, aria: `Workspace policy for ${a.id}`, title: "Where this account's agents may read and write" }), () => (a.kind === "entity"
          ? openEntityManage(a.id)
          : openWorkspacePolicyModal({ tenant_id: a.tenant_id || "default", user_id: a.id })));
        add("rotate", accountButton("Rotate", { icon: ICONS.refresh, aria: `Rotate token for ${a.id}`, title: "Issue a new token — the old one stops working immediately; the new one is shown once" }), () => (a.kind === "entity"
          ? rotateAccount(a)
          : rotateUser({ user_id: a.id, tenant_id: a.tenant_id || "default" })));
        if (a.kind === "entity") {
          add("manage", accountButton("Manage", { icon: ICONS.gear, aria: `Manage ${a.id}`, title: "Lifecycle, substrate, capabilities, prompt; Talk lives here too" }), () => openEntityManage(a.id));
        }
        add("delete", accountButton("Delete", { icon: ICONS.trash, cls: "danger", aria: `Delete ${a.id}` }), () => userConfirmRow(tr, `Delete ${a.id}? The account and its token are removed; the runtime ${a.runtime_id || a.id} and its data are kept and stay reserved for this user.`, "Delete", async () => {
          await api(`/api/gateway/admin/users/${encodeURIComponent(a.id)}?tenant_id=${encodeURIComponent(a.tenant_id || "default")}`, { method: "DELETE" });
          usersMessage(`${a.id} is deleted. Their runtime data is kept.`, "ok");
          await refresh();
        }));
        actions.append(buttons);
        const reasonLine = accountReasonsLine(a, shownKeys);
        if (reasonLine) {
          const why = document.createElement("p");
          why.className = "accounts-reasons af-row__muted";
          why.textContent = reasonLine;
          actions.append(why);
        }
        tr.append(actions);
        tbody.append(tr);
      }
    }
    async function rotateAccount(a) {
      const ok = await confirmAction({
        title: `Rotate the key of ${a.id}?`,
        message: "The current credential stops working immediately. The new one is shown once.",
        confirmLabel: "Rotate",
      });
      if (!ok) return;
      try {
        const res = await api(`/api/gateway/admin/accounts/${encodeURIComponent(a.id)}/rotate`, { method: "POST" });
        if (!res || !res.token) throw new Error("POST /admin/accounts/{id}/rotate answered without a token (gateway-api seam, DESIGN-v2 §6).");
        renderIssuedToken($("issued-token"), a.id, res.token);
      } catch (e) {
        usersMessage(emailErrorText(e), "error");
      }
    }
    // ---- Account modals: the kit's af-modal markup, bound by the islands' bindModal
    // (focus trap, Esc, backdrop click, scroll lock). Without the islands bundle the
    // console logs it and binds Esc / backdrop itself (the node-VM tests' surface).
    function bindAccountModal(backdrop, onClose) {
      const lib = islandsLib();
      if (lib && typeof lib.bindModal === "function") return lib.bindModal(backdrop, { onClose });
      if (typeof window !== "undefined" && window.document && window.document.getElementById("af-console-islands")) {
        console.error("AbstractGateway console: the abstractuic islands bundle has no bindModal; the account dialog binds Esc and the backdrop itself.");
      }
      const onKey = (e) => { if (e && (e.key === "Escape" || e.key === "Esc") && !e.defaultPrevented) { if (e.preventDefault) e.preventDefault(); onClose(); } };
      const onClick = (e) => { if (e && e.target === backdrop) onClose(); };
      document.addEventListener("keydown", onKey, true);
      backdrop.addEventListener("click", onClick);
      const first = backdrop.querySelector(".af-modal__body button, .af-modal__body input, .af-modal__body select, .af-modal__close");
      try { if (first && first.focus) first.focus(); } catch {}
      return () => { document.removeEventListener("keydown", onKey, true); backdrop.removeEventListener("click", onClick); };
    }
    function closeAccountEmail() {
      const backdrop = $("account-email-backdrop");
      if (backdrop.hidden) return;
      // The account page goes back where it lives (My account), unchanged.
      const section = $("my-email-section");
      if (accountsUi.emailHome && section && section.parentNode === $("account-email-body")) {
        accountsUi.emailHome.parentNode.insertBefore(section, accountsUi.emailHome);
        section.classList.remove("in-modal");
        section.classList.add("hidden");
      }
      $("account-email-body").textContent = "";
      backdrop.hidden = true;
      const release = accountsUi.emailRelease;
      accountsUi.emailRelease = null;
      if (release) release();
    }
    function openAccountEmail(a) {
      closeAccountEmail();
      const body = $("account-email-body");
      body.textContent = "";
      $("account-email-title").textContent = `Email — ${a.id}`;
      if (a.kind === "entity") {
        // plane_for_principal refuses entity principals: mail belongs to a user's runtime plane.
        const p = document.createElement("p");
        p.className = "account-modal-lead";
        const why = accountAction(a, "email");
        p.textContent = why.reason || (a.mailbox && a.mailbox.reason) || "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime.";
        body.append(p);
      } else if (accountIsOwn(a)) {
        if (a.role === "admin") {
          const p = document.createElement("p");
          p.className = "account-modal-lead";
          p.textContent = "You are also a user of this gateway: this address receives your sign-in codes and notifications, and your mailbox serves your own agents.";
          body.append(p);
        }
        const section = $("my-email-section");
        if (!accountsUi.emailHome) {
          accountsUi.emailHome = document.createComment("my-email-section home");
          section.parentNode.insertBefore(accountsUi.emailHome, section);
        }
        section.classList.remove("hidden");
        section.classList.add("in-modal");
        body.append(section);
        loadMyEmail();
      } else {
        body.append(accountOtherEmailCard(a));
      }
      const backdrop = $("account-email-backdrop");
      backdrop.hidden = false;
      accountsUi.emailRelease = bindAccountModal(backdrop, closeAccountEmail);
    }
    function accountOtherEmailCard(a) {
      // Another user's row (admin): the address only, a read-only mailbox line, and the
      // per-user override Reset when one is pinned. Never a mailbox form (round-1 rule).
      const wrap = document.createElement("section");
      wrap.className = "af-card account-card";
      const fid = `account-email-address-${a.id}`.replace(/[^A-Za-z0-9_-]/g, "-");
      wrap.innerHTML = `<div class="af-card__header"><h3 class="af-card__title">Email address</h3></div>`
        + `<div class="af-form"><div class="af-form__field">`
        + `<label class="af-form__label sr-only" for="${fid}">Email address</label>`
        + `<div class="af-form__inline"><input id="${fid}" type="email" autocomplete="off" spellcheck="false" aria-describedby="${fid}-help ${fid}-state"><button type="button" class="secondary account-email-save">Save</button></div>`
        + `<p id="${fid}-help" class="af-form__help">Where ${esc(a.id)}'s sign-in codes and notifications go.</p>`
        + `<p id="${fid}-state" class="inline-state" role="status" aria-live="polite"></p>`
        + `</div></div>`;
      const input = wrap.querySelector("input");
      input.value = a.email_address || "";
      const save = wrap.querySelector(".account-email-save");
      save.onclick = async () => {
        save.disabled = true;
        save.setAttribute("aria-busy", "true");
        try {
          await api(`/api/gateway/admin/users/${encodeURIComponent(a.id)}?tenant_id=${encodeURIComponent(a.tenant_id || "default")}`, { method: "PATCH", body: JSON.stringify({ email: String(input.value || "").trim() }) });
          a.email_address = String(input.value || "").trim() || null;
          inlineState(`${fid}-state`, a.email_address ? `Saved: ${a.id}'s codes and notifications go to ${a.email_address}.` : `Saved: ${a.id} has no email address.`, "ok", 4000);
          refreshUsersOnly();
        } catch (e) {
          inlineState(`${fid}-state`, emailErrorText(e), "error");
        } finally {
          save.disabled = false;
          save.removeAttribute("aria-busy");
        }
      };
      const m = a.mailbox || {};
      const status = document.createElement("p");
      status.className = "mailbox-status account-other-mailbox";
      status.textContent = m.state === "connected"
        ? `Mailbox: connected as ${m.address}. You never see anyone's mail.`
        : m.state === "paused"
          ? `Mailbox: paused by ${a.id}. You never see anyone's mail.`
          : `Mailbox: not connected — only ${a.id} can connect a mailbox. You never see anyone's mail.`;
      wrap.append(status);
      const u = accountUserRecord(a);
      const note = userMailboxNote(u ? u.email_account : null);
      if (note.reset && u) {
        const line = document.createElement("p");
        line.className = "af-form__help";
        line.textContent = `An old setting pins this user: ${note.text}. `;
        const reset = document.createElement("button");
        reset.type = "button";
        reset.className = "secondary small";
        reset.textContent = "Reset";
        reset.setAttribute("aria-label", `Reset the mailbox setting for ${a.id}`);
        reset.onclick = () => resetUserMailboxOverride(u, reset);
        line.append(reset);
        wrap.append(line);
      }
      return wrap;
    }
    // Logs (§2.4): GET /admin/accounts/{id}/activity -- the gateway's audit log.
    const ACCOUNT_LOG_FILTERS = [["", "All"], ["sign_in", "Sign-ins"], ["run", "Runs"], ["automation", "Automations"], ["email", "Email"]];
    function accountLogTime(ts) {
      const d = new Date(ts);
      if (!Number.isFinite(d.getTime())) return String(ts || "");
      const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      const now = new Date();
      if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) return hm;
      const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
      return `${month} ${d.getDate()} ${hm}`;
    }
    function closeAccountLogs() {
      const backdrop = $("account-logs-backdrop");
      if (backdrop.hidden) return;
      backdrop.hidden = true;
      accountsUi.logsFor = null;
      const release = accountsUi.logsRelease;
      accountsUi.logsRelease = null;
      if (release) release();
    }
    function renderAccountLogFilters() {
      const box = $("account-logs-filters");
      box.textContent = "";
      for (const [kind, label] of ACCOUNT_LOG_FILTERS) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "account-logs-chip";
        b.textContent = label;
        b.setAttribute("aria-pressed", accountsUi.logsKind === kind ? "true" : "false");
        b.onclick = () => { accountsUi.logsKind = kind; renderAccountLogFilters(); loadAccountLogs(); };
        box.append(b);
      }
    }
    async function loadAccountLogs() {
      const a = accountsUi.logsFor;
      if (!a) return;
      const list = $("account-logs-list");
      list.textContent = "";
      inlineState("account-logs-message", "Loading…", "");
      let out;
      try {
        out = await api(`/api/gateway/${accountsAdmin() ? "admin" : "me"}/accounts/${encodeURIComponent(a.id)}/activity?limit=100&kind=${encodeURIComponent(accountsUi.logsKind)}`);
      } catch (e) {
        inlineState("account-logs-message", emailErrorText(e), "error");
        return;
      }
      if (accountsUi.logsFor !== a) return;
      if (!out || !Array.isArray(out.events) || typeof out.note !== "string") {
        inlineState("account-logs-message", "The activity answer has no events list or note (gateway-api seam, DESIGN-v2 §6).", "error");
        return;
      }
      inlineState("account-logs-message", "", "");
      $("account-logs-note").textContent = out.note;
      if (!out.events.length) {
        const li = document.createElement("li");
        li.className = "account-logs-empty";
        li.textContent = "No recorded activity yet. The gateway records sign-ins, changes, runs started and email events.";
        list.append(li);
        return;
      }
      for (const ev of out.events) {
        const li = document.createElement("li");
        li.className = `account-logs-item${ev.ok === false ? " is-failed" : ""}`;
        li.setAttribute("data-kind", String(ev.kind || ""));
        const t = document.createElement("time");
        t.className = "account-logs-item__time";
        t.dateTime = String(ev.ts || "");
        t.textContent = accountLogTime(ev.ts);
        const what = document.createElement("span");
        what.className = "account-logs-item__title";
        what.textContent = String(ev.title || "");
        li.append(t, what);
        if (ev.detail) {
          const det = document.createElement("span");
          det.className = "account-logs-item__detail";
          det.textContent = String(ev.detail);
          li.append(det);
        }
        const observerPath = accountLogObserverPath(ev);
        if (observerPath) {
          const link = document.createElement("button");
          link.type = "button";
          link.className = "link-button account-logs-item__link";
          link.textContent = "Open in Observer";
          link.setAttribute("data-observer-path", observerPath);
          link.onclick = () => openObserverPath(observerPath);
          li.append(link);
        }
        list.append(li);
      }
    }
    // The Observer's run deep link (observer lane, SEAMS 02:26: OBSERVER_RUN_PATH =
    // "/apps/observer/#run/{run_id}"). The API's observer_path wins; until gateway-api flips
    // account_activity.OBSERVER_RUN_HASH (integration), a run event that carries run_id and no
    // observer_path links through this exact path.
    const OBSERVER_RUN_PATH = "/apps/observer/#run/{run_id}";
    function accountLogObserverPath(ev) {
      if (ev.observer_path) return String(ev.observer_path);
      if (ev.kind === "run" && ev.run_id) return OBSERVER_RUN_PATH.replace("{run_id}", encodeURIComponent(String(ev.run_id)));
      return "";
    }
    async function openObserverPath(path) {
      // The Observer app route of a run (observer_path, §6), opened in a new tab
      // through the gateway's app door (POST /apps/observer/open validates the path).
      const tab = typeof window !== "undefined" && window.open ? window.open("about:blank", "_blank") : null;
      try {
        // observer_path is the gateway path of an Observer page (app_proxy prefix + app id); the
        // app door takes the path inside the app.
        const prefix = "/apps/observer";
        if (!path.startsWith(prefix + "/")) throw new Error(`observer_path ${path} is not an Observer page (gateway-api seam, DESIGN-v2 §6).`);
        const res = await api("/api/gateway/apps/observer/open", { method: "POST", body: JSON.stringify({ origin: appBrowserOrigin(), path: path.slice(prefix.length) }) });
        if (tab) tab.location = res.open_url; else location.assign(res.open_url);
      } catch (e) {
        if (tab) tab.close();
        inlineState("account-logs-message", `Could not open Observer: ${emailErrorText(e)}`, "error");
      }
    }
    function openAccountLogs(a) {
      closeAccountLogs();
      accountsUi.logsFor = a;
      accountsUi.logsKind = "";
      $("account-logs-title").textContent = `Activity — ${a.id}`;
      $("account-logs-note").textContent = "";
      renderAccountLogFilters();
      const backdrop = $("account-logs-backdrop");
      backdrop.hidden = false;
      accountsUi.logsRelease = bindAccountModal(backdrop, closeAccountLogs);
      loadAccountLogs();
    }
    async function resetUserMailboxOverride(u, btn) {
      if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
      try {
        await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}/email?tenant_id=${encodeURIComponent(u.tenant_id || "default")}`, { method: "PUT", body: JSON.stringify({ inherit: ["email", "email_agent_tools"] }) });
        usersMessage(`${u.user_id} follows “Mailboxes for users” again.`, "ok");
        await refresh();
      } catch (e) {
        usersMessage(emailErrorText(e), "error");
        if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); }
      }
    }
    function renderRuntimeReservations(reservations) {
      const tbody = $("runtime-reservations-table");
      tbody.textContent = "";
      // Visible only when there is something to recover (admin gating is
      // renderAccount's half) — never standing clutter on the Runtimes tab.
      const isAdmin = state.principal?.admin === true;
      $("runtime-reservations-section").classList.toggle("hidden", !isAdmin || !(reservations || []).length);
      if (!reservations || !reservations.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="empty">No retained runtime reservations.</td>`;
        tbody.append(tr);
        return;
      }
      for (const r of reservations || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${esc(r.tenant_id)}</td><td><code>${esc(r.runtime_id)}</code></td><td>${esc(r.owner_key || r.owner_user_id || "")}</td><td>${esc(r.reason || "")}</td><td>${r.data_exists ? "retained" : "no files found"}</td>`;
        const actions = document.createElement("td");
        actions.className = "actions";
        const transferTarget = document.createElement("select");
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "Transfer to...";
        transferTarget.append(empty);
        for (const u of state.users.filter((u) => u.tenant_id === r.tenant_id && principalKind(u) !== "entity")) {
          const opt = document.createElement("option");
          opt.value = u.user_id;
          opt.textContent = `${u.user_id} (${u.runtime_id || u.user_id})`;
          transferTarget.append(opt);
        }
        const transfer = document.createElement("button");
        transfer.innerHTML = `<span class="button-icon" aria-hidden="true">→</span><span>Transfer</span>`;
        transfer.className = "secondary";
        transfer.onclick = () => transferRuntimeReservation(r, transferTarget.value);
        const purge = document.createElement("button");
        purge.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Purge</span>`;
        purge.className = "danger";
        purge.onclick = () => purgeRuntimeReservation(r);
        actions.append(transferTarget, transfer, purge);
        tr.append(actions);
        tbody.append(tr);
      }
    }
	    // ONE STORE, SAID OUT LOUD. Capability
	    // defaults and shared provider connections are AbstractCore's data;
	    // the Gateway is a second entry point to it, not the owner of a copy.
	    // A panel that renders an edit form over someone else's file and never
	    // names that file teaches the wrong model of the system — and on the
	    // day `abstractcore config` disagrees with this grid, the operator has
	    // no way to know which file to open. So the line is PERSISTENT
	    // (provenance, not a notification that can be dismissed) and quotes
	    // the path the API says it resolved, never a path this page guessed.
	    function renderStoreAuthority(elementId, payload) {
	      const el = $(elementId);
	      if (!el) return;
	      const file = String(payload?.config_file || "").trim();
	      const authority = String(payload?.authority || "").trim();
	      // NO CLAIM WITHOUT EVIDENCE: a payload that does not name an
	      // AbstractCore store gets no line at all. Gateway-owned panels must
	      // never inherit a sentence about a file they do not touch.
	      if (!file || !authority.startsWith("abstractcore")) {
	        // innerHTML explicitly, not just textContent: this line is the only
	        // place the console makes a claim about someone else's file, so
	        // retracting it must leave nothing behind.
	        el.innerHTML = "";
	        el.textContent = "";
	        el.title = "";
	        el.classList.remove("authority-readonly");
	        el.classList.add("hidden");
	        return;
	      }
	      const writable = payload?.writable !== false;
	      // A per-runtime overlay is an AbstractCore file too, but it is NOT
	      // the shared one — saying "edits apply to AbstractCore directly"
	      // over an overlay would be a lie in the one place it matters.
	      const overlay = authority === "abstractcore.runtime";
	      const label = overlay ? "This runtime's AbstractCore overlay" : "AbstractCore store";
	      const claim = overlay
	        ? "private to this runtime — routes left unset here fall back to the shared AbstractCore store"
	        : writable
	          ? "shared with AbstractCore — edits here apply to AbstractCore directly"
	          : "shared with AbstractCore — read-only from this Gateway; edit it where AbstractCore runs";
	      el.innerHTML = `${esc(label)} · <code>${esc(file)}</code> — ${esc(claim)}`;
	      el.title = `authority: ${authority}`;
	      el.classList.toggle("authority-readonly", !writable);
	      el.classList.remove("hidden");
	    }
	    async function renderDefaults(payload) {
	      const rawRows = (Array.isArray(payload?.routes) ? payload.routes : []).filter(visibleCapabilityDefaultRow);
	      const rows = [];
	      for (const rawRow of rawRows) rows.push(await displayDefaultRow(rawRow, rawRows));
	      state.defaults = rows;
	      state.defaultsSource = payload?.source || "";
	      renderStoreAuthority("defaults-authority", payload);
	      renderDefaultRows(rows);
	      renderSandboxCapabilityOptions();
	      // Weights are probed AFTER the grid paints: a stalled LM Studio socket
	      // must never hold up the provider/model columns, which is the part the
	      // operator came for.
	      refreshAvailability({ rerender: true });
	    }
	    // Split out of renderDefaults so a download's progress can repaint the
	    // grid without re-deriving the coverage/alias decorations (which cost a
	    // model-discovery round trip per row).
	    function renderDefaultRows(rows) {
	      const payload = { source: state.defaultsSource || "" };
	      const tbody = $("defaults-table");
	      tbody.textContent = "";
	      for (const row of groupDefaultRowsByHierarchy(rows)) {
	        const key = defaultRowKey(row);
	        if (!key || key === ".") continue;
	        const parentKey = defaultRowParentKey(row);
	        const configured = defaultRowConfigured(row);
	        const status = defaultRowStatus(row);
	        const source = row.covered_by === "input.text"
	          ? "Text Input"
	          : row.derived_from === "input.text"
	            ? "Text Input"
	            : defaultSourceLabel(row.source || (configured ? payload.source || "" : ""));
	        const tr = document.createElement("tr");
	        if (defaultRowReadOnly(row)) tr.classList.add("capability-derived");
	        if (parentKey) tr.classList.add("capability-task-row");
	        // The ROUTE cell carries the hierarchy: a task row is indented under
	        // its parent and shows only the task segment (the full key stays in
	        // the tooltip and in every write path), and the parent says in words
	        // what it is FOR — "any image task" — so the grid answers "why do we
	        // have output.image AND output.image.text_to_image" on sight.
	        const routeCell = parentKey
	          ? `<span class="capability-route capability-route-task" title="${esc(key)}"><code>${esc(key.slice(parentKey.length))}</code></span>`
	          : `<span class="capability-route"><code>${esc(key)}</code></span>`;
	        const capabilityCell = defaultRowIsTaskParent(row)
	          ? `${esc(defaultRowCapability(row))} <span class="muted">— any ${esc(defaultRowKindModality(row).modality)} task (fallback)</span>`
	          : esc(defaultRowCapability(row));
	        // The stored reasoning effort is VISIBLE on the grid, not only inside
	        // the edit modal (console-TUI parity: its route row says
	        // "· reasoning high"). Text routes only — the field exists nowhere
	        // else — and only when set, so non-reasoning setups see no noise.
	        const reasoningBadge =
	          isTextGenerationDefault(row) && defaultReasoningValue(row)
	            ? ` <span class="badge" title="Default reasoning effort for this route (edit via Configure)">reasoning ${esc(defaultReasoningValue(row))}</span>`
	            : "";
	        tr.innerHTML = `
	          <td>${routeCell}</td>
	          <td>${capabilityCell}</td>
	          <td>${row.provider ? esc(state.providerLabels.get(row.provider) || row.provider) : "-"}</td>
	          <td>${row.model ? `<span class="ui-ellip" title="${esc(row.model)}">${esc(row.model)}</span>` + reasoningBadge : "-"}</td>
	          <td>${weightsCellMarkup(row)}</td>
	          <td>${source ? `<span class="badge">${esc(source)}</span>` : "-"}</td>
	          <td><span class="state-pill ${esc(status.cls)}">${esc(status.label)}</span>${defaultRowUnavailableMarkup(row)}${defaultRowRouteUnavailableMarkup(row)}${defaultRowConfigured(row) ? engineMissingMarkup(row) : ""}</td>
	        `;
	        const actions = document.createElement("td");
	        actions.className = "actions";
	        if (defaultRowReadOnly(row)) {
	          // Status is not a verb: a disabled
	          // button dressed as an action duplicated the STATUS pill; a
	          // covered/linked route gets muted words, not fake affordance.
	          const note = document.createElement("span");
	          note.className = "muted";
	          note.textContent = defaultRowActionLabel(row);
	          actions.append(note);
	        } else {
	          const configure = document.createElement("button");
	          configure.className = "secondary";
	          configure.innerHTML = `<span class="button-icon" aria-hidden="true">${configured ? "✎" : "+"}</span><span>${esc(defaultRowActionLabel(row))}</span>`;
	          configure.onclick = () => openDefaultModal(row);
	          actions.append(configure);
	        }
	        if (configured && !defaultRowReadOnly(row)) {
	          const clear = document.createElement("button");
	          clear.className = "secondary";
	          clear.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Clear</span>`;
	          clear.onclick = () => clearDefault(row);
	          actions.append(clear);
	        }
	        // DOWNLOAD is offered only where it can do something: the weights
	        // are missing (or the probe could not look, `unknown`) AND this
	        // provider has a download verb here (`downloadable`, weightView);
	        // a relay provider has nothing to fetch.
	        const info = rowAvailability(row);
	        const availability = (info && info.availability) || {};
	        const job = rowDownloadJob(row);
	        if (job && job.status === "running") {
	          const busy = document.createElement("span");
	          busy.className = "muted";
	          busy.textContent = job.message || "downloading…";
	          busy.title = (job.events || []).slice(-6).join("\\n");
	          actions.append(busy);
	        } else if (availability.status && weightView(availability).canDownload) {
	          const artifact = rowDownloadArtifact(row);
	          const download = document.createElement("button");
	          download.className = "secondary";
	          download.title = `Download ${artifact} with ${row.provider}`;
	          download.innerHTML = `<span class="button-icon" aria-hidden="true">⭳</span><span>Download</span>`;
	          download.onclick = () => downloadRouteModel(row);
	          actions.append(download);
	        } else if (availability.status === "absent" && availability.instruction) {
	          // No download verb here (no `lms` on PATH, no huggingface_hub):
	          // the actionable line is the affordance, not a dead button.
	          const note = document.createElement("span");
	          note.className = "muted";
	          note.textContent = availability.instruction;
	          actions.append(note);
	        }
	        tr.append(actions);
	        tbody.append(tr);
	      }
	      if (!tbody.children.length) {
	        const tr = document.createElement("tr");
	        tr.innerHTML = `<td colspan="8" class="empty">No capability routes were returned by Gateway.</td>`;
	        tbody.append(tr);
	      }
	    }
	    function sandboxRouteMode(key) {
	      const value = String(key || "").trim().toLowerCase();
	      if (value === "input.text" || value === "output.text") return "text";
	      if (value === "output.image" || value === "output.image.text_to_image") return "image";
	      if (value === "output.voice") return "voice";
	      if (value === "output.sound") return "sound";
	      if (value === "output.music") return "music";
	      if (value === "output.video" || value === "output.video.text_to_video") return "video";
	      return "text";
	    }
	    function sandboxRouteIcon(mode) {
	      return { text: "T", image: "▣", voice: "◉", sound: "S", music: "♫", video: "▻" }[mode] || "T";
	    }
	    // svgIcon is defined page-wide at the script top (icon registry).
	    function sandboxRouteIconMarkup(mode) {
	      const icons = {
	        text: svgIcon('<path d="M5 6h14"></path><path d="M12 6v12"></path><path d="M8 18h8"></path>'),
	        image: svgIcon('<rect x="4" y="5" width="16" height="14" rx="2"></rect><path d="m7 15 3-3 3 3 2-2 3 4"></path><circle cx="9" cy="9" r="1.2"></circle>'),
	        voice: svgIcon('<path d="M5 10v4h4l5 4V6l-5 4H5z"></path><path d="M17 9c1 1 1.5 2 1.5 3s-.5 2-1.5 3"></path><path d="M19.5 7.5c1.7 1.7 2.5 3.5 2.5 5.5s-.8 3.8-2.5 5.5"></path>'),
	        sound: svgIcon('<path d="M4 12h2l2-5 4 10 3-7 2 2h3"></path><path d="M3 18h18"></path>'),
	        music: svgIcon('<path d="M10 18V6l9-2v12"></path><circle cx="7" cy="18" r="3"></circle><circle cx="16" cy="16" r="3"></circle>'),
	        video: svgIcon('<rect x="4" y="6" width="11" height="12" rx="2"></rect><path d="m15 10 5-3v10l-5-3z"></path>'),
	      };
	      return icons[mode] || icons.text;
	    }
	    function sandboxRouteShortLabel(row) {
	      const mode = sandboxRouteMode(defaultRowKey(row));
	      return { text: "Text", image: "Image", voice: "Voice", sound: "SFX", music: "Music", video: "Video" }[mode] || defaultRowCapability(row);
	    }
	    function sandboxRouteLabel(row) {
	      const key = defaultRowKey(row);
	      if (sandboxRouteMode(key) === "text") return "Text Chat";
	      return `${key} - ${defaultRowCapability(row)}`;
	    }
	    // THE SERVER'S RESOLUTION, NOT A SECOND ONE. The image/video lanes post
	    // to /runs/{id}/images|videos/generate, which hand the request to
	    // AbstractCore's `resolve_capability_default_route`: the exact task row
	    // (`output.image.text_to_image`) when it is configured, otherwise the
	    // modality cell (`output.image`), the first of the two that carries any
	    // field. A fresh install writes `output.image` alone, so reading only the
	    // task row called a working route "not configured". Rule, mirrored here:
	    //   task row has provider+model            -> the task row
	    //   task row has other fields only         -> the task row (Core stops
	    //                                            there; no provider+model = not ready)
	    //   task row empty, parent has provider+model -> the parent's values
	    //   otherwise                              -> not configured
	    function sandboxEffectiveRow(row) {
	      if (!row || defaultRowConfigured(row) || row.configured) return row;
	      const parentKey = defaultRowParentKey(row);
	      if (!parentKey) return row;
	      const parent = findDefaultRow(state.defaults, parentKey);
	      if (!defaultRowConfigured(parent)) return row;
	      return {
	        ...row,
	        provider: parent.provider,
	        model: parent.model,
	        base_url: parent.base_url,
	        options: parent.options || {},
	        inherited_from: parentKey,
	      };
	    }
	    function sandboxCandidateRows() {
	      const wanted = new Set(["input.text", "output.text", "output.image.text_to_image", "output.voice", "output.sound", "output.music", "output.video.text_to_video"]);
	      const byKey = new Map();
	      for (const row of state.defaults || []) {
	        if (!visibleCapabilityDefaultRow(row)) continue;
	        const key = defaultRowKey(row);
	        if (wanted.has(key) && !byKey.has(key)) byKey.set(key, sandboxEffectiveRow(row));
	      }
	      const textRow = byKey.get("input.text") || byKey.get("output.text") || { key: "input.text", kind: "input", modality: "text", label: "Text Chat" };
	      const ordered = [textRow];
	      for (const key of ["output.image.text_to_image", "output.voice", "output.music", "output.sound", "output.video.text_to_video"]) {
	        if (byKey.has(key)) ordered.push(byKey.get(key));
	      }
	      return ordered;
	    }
	    function renderSandboxCapabilityOptions() {
	      const select = $("sandbox-capability");
	      if (!select) return;
	      const previous = select.value;
	      select.textContent = "";
	      const rows = sandboxCandidateRows();
	      const modes = $("sandbox-output-modes");
	      if (modes) modes.textContent = "";
	      for (const row of rows) {
	        const opt = document.createElement("option");
	        opt.value = defaultRowKey(row);
	        opt.textContent = sandboxRouteLabel(row);
	        select.append(opt);
	        if (modes) {
	          const mode = sandboxRouteMode(defaultRowKey(row));
	          const configured = defaultRowConfigured(row);
	          const btn = document.createElement("button");
	          btn.type = "button";
	          btn.className = "sandbox-mode";
	          btn.disabled = !configured;
	          btn.setAttribute?.("role", "radio");
	          const label = sandboxRouteShortLabel(row);
	          btn.title = `${label}: ${configured ? `${state.providerLabels.get(row.provider) || row.provider || ""} / ${row.model || ""}` : "not configured"}`;
	          btn.setAttribute?.("aria-label", btn.title);
	          btn.innerHTML = `<span class="sandbox-mode-icon" aria-hidden="true">${sandboxRouteIconMarkup(mode)}</span><span class="sandbox-mode-copy"><span class="sandbox-mode-main">${esc(label)}</span><span class="sandbox-mode-sub">${configured ? esc(row.model || "configured") : "not configured"}</span></span>`;
	          btn.onclick = () => {
	            if (btn.disabled) return;
	            select.value = defaultRowKey(row);
	            updateSandboxControls();
	          };
	          modes.append(btn);
	        }
	      }
	      select.value = rows.some((row) => defaultRowKey(row) === previous) ? previous : (rows[0] ? defaultRowKey(rows[0]) : "input.text");
	      updateSandboxControls();
	    }
	    function selectedSandboxRoute() {
	      const key = $("sandbox-capability")?.value || "input.text";
	      const rows = sandboxCandidateRows();
	      const exact = rows.find((row) => defaultRowKey(row) === key);
	      if (exact) return exact;
	      if (String(key).toLowerCase() === "output.text") {
	        const text = rows.find((row) => sandboxRouteMode(defaultRowKey(row)) === "text");
	        if (text) return text;
	      }
	      return rows[0] || { key, label: key };
	    }
	    function syncSandboxProviderOptions() {
	      const providerSelect = $("sandbox-provider");
	      if (!providerSelect) return;
	      setSelectOptions(providerSelect, state.providers || [], {
	        emptyLabel: state.providers.length ? "Select provider..." : "Configure a provider first",
	        disabled: !state.providers.length,
	        selected: providerSelect.value || "",
	      });
	      if (!providerSelect.value && state.providers.length) providerSelect.value = state.providers[0];
	      updateSandboxControls();
	    }
	    async function loadSandboxModels(selected = "") {
	      const provider = $("sandbox-provider").value;
	      if (!provider) {
	        setSelectOptions($("sandbox-model"), [], { emptyLabel: "Select provider first", disabled: true });
	        return;
	      }
	      setSelectOptions($("sandbox-model"), [], { emptyLabel: "Loading models...", disabled: true });
	      try {
	        const models = await fetchProviderModels(provider);
	        setSelectOptions($("sandbox-model"), models, {
	          emptyLabel: models.length ? "Select model..." : "No models discovered",
	          disabled: !models.length,
	          selected,
	        });
	      } catch (err) {
	        setSelectOptions($("sandbox-model"), [], { emptyLabel: "Model discovery failed", disabled: true });
	        $("sandbox-message").textContent = String(err.message || err);
	        $("sandbox-message").className = "message error";
	      }
	    }
	    function updateSandboxControls() {
	      const row = selectedSandboxRoute();
	      const mode = sandboxRouteMode(defaultRowKey(row));
	      $("sandbox-provider-label").classList.add("hidden");
	      $("sandbox-model-label").classList.add("hidden");
	      $("sandbox-system-label").classList.toggle("hidden", mode !== "text");
	      $("sandbox-speculation-label").classList.toggle("hidden", mode !== "text");
	      if (mode === "text") refreshSandboxSpeculationSupport(row);
	      const configured = defaultRowConfigured(row);
	      const prov = row.provider ? (state.providerLabels.get(row.provider) || row.provider) : "";
	      $("sandbox-context").textContent = configured
	        ? `${sandboxRouteLabel(row)} will use ${prov} / ${row.model}${row.inherited_from ? ` (inherited from ${row.inherited_from})` : ""}.`
	        : `${sandboxRouteLabel(row)} is not configured yet. Configure it in Multimodal Capabilities first.`;
	      const prompt = $("sandbox-prompt");
	      if (prompt) {
	        prompt.placeholder = {
	          text: "Ask a question. Drop files here to include images, audio, video, PDFs, markdown, or text documents.",
	          image: "Describe the image you want to generate.",
	          voice: "Type the sentence to synthesize.",
	          sound: "Describe the sound effect you want to generate.",
	          music: "Describe the music you want to generate.",
	          video: "Describe the video you want to generate.",
	        }[mode] || "Type your request.";
	      }
	      const run = $("sandbox-run");
	      if (run) run.disabled = !configured;
	      const buttons = $("sandbox-output-modes")?.children || [];
	      for (const btn of buttons) {
	        const label = btn.children?.[1]?.children?.[0]?.textContent || "";
	        btn.classList.toggle("active", label === sandboxRouteShortLabel(row));
	        btn.setAttribute?.("aria-checked", label === sandboxRouteShortLabel(row) ? "true" : "false");
	      }
	    }
	    function sandboxNow() {
	      const d = new Date();
	      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
	    }
	    function sandboxClientContext() {
	      const now = new Date();
	      const pad = (n) => String(Math.trunc(Math.abs(Number(n) || 0))).padStart(2, "0");
	      const offsetMinutes = -now.getTimezoneOffset();
	      const offsetSign = offsetMinutes >= 0 ? "+" : "-";
	      const offset = `${offsetSign}${pad(offsetMinutes / 60)}:${pad(offsetMinutes % 60)}`;
	      const localDatetime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}${offset}`;
	      let timezone = "";
	      try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch {}
	      const nav = typeof navigator !== "undefined" ? navigator : {};
	      const languages = Array.isArray(nav.languages) ? nav.languages.filter(Boolean) : [];
	      const locale = String(languages[0] || nav.language || "").trim();
	      let localeCountry = "";
	      try {
	        if (locale && typeof Intl !== "undefined" && typeof Intl.Locale === "function") {
	          localeCountry = String(new Intl.Locale(locale).region || "").trim().toUpperCase();
	        }
	      } catch {}
	      if (!localeCountry && locale) {
	        const match = locale.match(/[-_]([A-Za-z]{2})(?:[-_]|$)/);
	        if (match) localeCountry = String(match[1] || "").toUpperCase();
	      }
	      const ctx = {
	        local_datetime: localDatetime,
	        utc_datetime: now.toISOString(),
	        timezone_offset_minutes: offsetMinutes,
	        source: "browser",
	      };
	      if (timezone) ctx.timezone = timezone;
	      if (locale) ctx.locale = locale;
	      if (localeCountry) ctx.locale_country = localeCountry;
	      return ctx;
	    }
	    function formatSandboxDuration(ms) {
	      const n = Number(ms || 0);
	      if (!Number.isFinite(n) || n <= 0) return "";
	      return n < 1000 ? `${Math.round(n)}ms` : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}s`;
	    }
	    function sandboxUsageLabel(usage, elapsedMs) {
	      const elapsed = formatSandboxDuration(elapsedMs);
	      const data = objectValue(usage);
	      const tokens = Number(data?.completion_tokens ?? data?.output_tokens ?? data?.generated_tokens ?? data?.total_tokens ?? 0);
	      const parts = [];
	      if (elapsed) parts.push(elapsed);
	      if (Number.isFinite(tokens) && tokens > 0) {
	        parts.push(`${Math.round(tokens)} tok`);
	        const sec = Number(elapsedMs || 0) / 1000;
	        if (sec > 0) parts.push(`${Math.max(1, Math.round(tokens / sec))} tok/s`);
	      }
	      return parts.join(" · ");
	    }
	    function sandboxVoiceDefaultRow() {
	      return (state.defaults || []).find((row) => defaultRowKey(row) === "output.voice" && defaultRowConfigured(row));
	    }
	    function sandboxArtifactUrl(runId, ref) {
	      if (!ref || typeof ref !== "object") return "";
	      const id = String(ref.$artifact || ref.artifact_id || ref.id || "").trim();
	      if (!id) return "";
	      return `/api/gateway/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(id)}/content`;
	    }
	    function sandboxRememberObjectUrl(url) {
	      if (!url) return url;
	      state.sandboxObjectUrls.push(url);
	      return url;
	    }
	    function sandboxRevokeObjectUrls() {
	      if (typeof URL === "undefined" || !URL.revokeObjectURL) {
	        state.sandboxObjectUrls = [];
	        return;
	      }
	      for (const url of state.sandboxObjectUrls || []) {
	        try { URL.revokeObjectURL(url); } catch {}
	      }
	      state.sandboxObjectUrls = [];
	    }
	    function addSandboxMediaError(target, message) {
	      if (!target || target._sandboxMediaErrorShown) return;
	      target._sandboxMediaErrorShown = true;
	      const note = document.createElement("div");
	      note.className = "sandbox-media-error";
	      note.textContent = message;
	      target.append(note);
	    }
	    async function setSandboxMediaSource(mediaEl, wrap, runId, ref, label) {
	      const url = sandboxArtifactUrl(runId, ref);
	      if (!url || !mediaEl) return "";
	      if (mediaEl.dataset) mediaEl.dataset.artifactUrl = url;
	      else mediaEl.artifactUrl = url;
	      const rawContentType = String(ref?.content_type || "").trim();
	      mediaEl.onerror = () => addSandboxMediaError(wrap, `${label || "Artifact"} could not be decoded by this browser. Use the raw artifact link to inspect it.`);
	      if (typeof URL === "undefined" || !URL.createObjectURL || typeof fetch !== "function") {
	        mediaEl.src = url;
	        return url;
	      }
	      try {
	        const res = await fetch(url, { method: "GET", credentials: "same-origin" });
	        if (!res.ok) {
	          let detail = "";
	          try {
	            const text = await res.text();
	            detail = text ? `: ${text.slice(0, 160)}` : "";
	          } catch {}
	          throw new Error(`artifact download failed (${res.status})${detail}`);
	        }
	        const blob = await res.blob();
	        const typedBlob = rawContentType && (!blob.type || blob.type === "application/octet-stream")
	          ? new Blob([blob], { type: rawContentType })
	          : blob;
	        mediaEl.src = sandboxRememberObjectUrl(URL.createObjectURL(typedBlob));
	        return mediaEl.src;
	      } catch (err) {
	        mediaEl.src = url;
	        addSandboxMediaError(wrap, String(err.message || err));
	        return url;
	      }
	    }
	    function renderSandboxArtifact(target, { runId = "", ref = null, mode = "", label = "" } = {}) {
	      const url = sandboxArtifactUrl(runId, ref);
	      if (!url || !target) return;
	      const wrap = document.createElement("div");
	      wrap.className = "sandbox-artifact";
	      const contentType = String(ref?.content_type || "").toLowerCase();
	      const kind = mode || (contentType.startsWith("image/") ? "image" : contentType.startsWith("video/") ? "video" : contentType.startsWith("audio/") ? "audio" : "");
	      if (kind === "image") {
	        const img = document.createElement("img");
	        img.alt = label || "Generated image";
	        img.loading = "lazy";
	        wrap.append(img);
	        setSandboxMediaSource(img, wrap, runId, ref, label || "Image");
	      } else if (kind === "video") {
	        const video = document.createElement("video");
	        video.controls = true;
	        video.playsInline = true;
	        wrap.append(video);
	        setSandboxMediaSource(video, wrap, runId, ref, label || "Video");
	      } else if (kind === "voice" || kind === "music" || kind === "sound" || kind === "audio") {
	        const audio = document.createElement("audio");
	        audio.controls = true;
	        audio.preload = "metadata";
	        wrap.append(audio);
	        setSandboxMediaSource(audio, wrap, runId, ref, label || "Audio");
	      }
	      const link = document.createElement("a");
	      link.href = url;
	      link.target = "_blank";
	      link.rel = "noopener";
	      link.className = "sandbox-artifact-link";
	      link.textContent = label || "Open artifact";
	      wrap.append(link);
	      target.append(wrap);
	    }
	    function appendSandboxMessage(role, content, options = {}) {
	      const target = $("sandbox-transcript");
	      const hint = $("sandbox-empty-hint");
	      if (hint) { try { hint.remove(); } catch { hint.className = "hidden"; } }
	      const div = document.createElement("div");
	      const kind = String(options.kind || role || "").toLowerCase();
	      const kindClass = kind.includes("you") || kind === "user" ? "user" : kind.includes("error") ? "error" : kind.includes("system") ? "system" : "assistant";
	      div.className = `sandbox-message ${kindClass}`;
	      const bubble = document.createElement("div");
	      // Dual-class: sandbox-bubble = layout; pc-chat-item = the shared
	      // abstractuic dialogue look (system renders as the kit's status item).
	      bubble.className = `sandbox-bubble pc-chat-item pc-chat-item--${kindClass === "system" ? "status" : kindClass}`;
	      const meta = document.createElement("div");
	      meta.className = "sandbox-message-meta";
	      const roleEl = document.createElement("span");
	      roleEl.className = "sandbox-message-role";
	      roleEl.textContent = role;
	      const timeEl = document.createElement("span");
	      timeEl.textContent = options.meta || sandboxNow();
	      const spacer = document.createElement("span");
	      spacer.className = "sandbox-message-spacer";
	      meta.append(roleEl, timeEl, spacer);
	      const body = document.createElement("div");
	      body.className = "sandbox-message-body";
	      const messageIsAssistant = String(div.className || "").includes("assistant");
	      setSandboxMessageBody(body, content, { markdown: options.markdown === true || (options.markdown !== false && messageIsAssistant) });
	      if (options.speakable && content && sandboxVoiceDefaultRow()) {
	        const speak = document.createElement("button");
	        speak.type = "button";
	        speak.className = "secondary sandbox-speak";
	        speak.title = "Speak this message";
	        speak.setAttribute?.("aria-label", "Speak this message");
	        speak.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	        speak.onclick = () => speakSandboxText(String(content || ""), bubble, speak);
	        meta.append(speak);
	      }
	      bubble.append(meta, body);
	      if (Array.isArray(options.attachments) && options.attachments.length) {
	        const chips = document.createElement("div");
	        chips.className = "sandbox-attachments";
	        for (const item of options.attachments) {
	          const chip = document.createElement("span");
	          chip.className = "sandbox-attachment";
	          chip.innerHTML = `<span>${esc(item.name || item.filename || "attachment")}</span>`;
	          chips.append(chip);
	        }
	        bubble.append(chips);
	      }
	      if (options.pending) {
	        const progress = document.createElement("div");
	        progress.className = "sandbox-progress";
	        progress.innerHTML = `<span>${esc(options.pendingLabel || "Working...")}</span><div class="sandbox-progress-bar"></div>`;
	        bubble.append(progress);
	      }
	      if (options.artifactRef) {
	        renderSandboxArtifact(bubble, { runId: options.runId, ref: options.artifactRef, mode: options.mode, label: options.artifactLabel });
	      }
	      div.append(bubble);
	      target.append(div);
	      target.scrollTop = target.scrollHeight;
	      return { el: div, bubble, body, meta };
	    }
	    function finalizeSandboxMessage(message, { content = "", meta = "", artifactRef = null, runId = "", mode = "", artifactLabel = "", usage = null, elapsedMs = 0, speakable = false, reasoning = "" } = {}) {
	      if (!message || !message.bubble) return;
	      const progress = Array.from(message.bubble.children || []).find((child) => String(child.className || "").includes("sandbox-progress"));
	      if (progress) progress.className = "hidden";
	      if (message.body) {
	        const messageIsAssistant = String(message.el?.className || "").includes("assistant");
	        setSandboxMessageBody(message.body, content, { markdown: messageIsAssistant });
	      }
	      // Show the model's reasoning, collapsed, above the answer — so a
	      // test with a reasoning effort has a visible result.
	      if (reasoning && message.body) {
	        const details = document.createElement("details");
	        details.className = "sandbox-reasoning-block";
	        const summary = document.createElement("summary");
	        summary.textContent = "Reasoning";
	        const pre = document.createElement("pre");
	        pre.textContent = String(reasoning);
	        details.append(summary, pre);
	        message.body.parentNode?.insertBefore(details, message.body);
	      }
	      const metaLine = [sandboxUsageLabel(usage, elapsedMs), meta].filter(Boolean).join(" · ");
	      if (metaLine && message.meta?.children?.[1]) message.meta.children[1].textContent = metaLine;
	      if (speakable && content && sandboxVoiceDefaultRow()) {
	        const speak = document.createElement("button");
	        speak.type = "button";
	        speak.className = "secondary sandbox-speak";
	        speak.title = "Speak this message";
	        speak.setAttribute?.("aria-label", "Speak this message");
	        speak.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	        speak.onclick = () => speakSandboxText(String(content || ""), message.bubble, speak);
	        message.meta.append(speak);
	      }
	      if (artifactRef) renderSandboxArtifact(message.bubble, { runId, ref: artifactRef, mode, label: artifactLabel });
	      const target = $("sandbox-transcript");
	      target.scrollTop = target.scrollHeight;
	    }
	    function findSandboxChild(root, className) {
	      if (!root) return null;
	      if (String(root.className || "").split(/\\s+/).includes(className)) return root;
	      for (const child of Array.from(root.children || [])) {
	        const found = findSandboxChild(child, className);
	        if (found) return found;
	      }
	      return null;
	    }
	    function sandboxMessageFromElement(el) {
	      if (!el) return null;
	      const bubble = findSandboxChild(el, "sandbox-bubble");
	      if (!bubble) return null;
	      return {
	        el,
	        bubble,
	        body: findSandboxChild(bubble, "sandbox-message-body"),
	        meta: findSandboxChild(bubble, "sandbox-message-meta"),
	      };
	    }
	    function latestPendingSandboxMessage() {
	      const target = $("sandbox-transcript");
	      const messages = Array.from(target?.children || []);
	      for (let index = messages.length - 1; index >= 0; index -= 1) {
	        const message = sandboxMessageFromElement(messages[index]);
	        const progress = findSandboxChild(message?.bubble, "sandbox-progress");
	        if (progress && !String(progress.className || "").includes("hidden")) return message;
	      }
	      return null;
	    }
	    function hideAllSandboxProgress() {
	      const visit = (node) => {
	        if (!node) return;
	        if (String(node.className || "").includes("sandbox-progress")) node.className = "hidden";
	        for (const child of Array.from(node.children || [])) visit(child);
	      };
	      visit($("sandbox-transcript"));
	    }
	    function failSandboxMessage(message, errorText) {
	      const targetMessage = message?.bubble ? message : latestPendingSandboxMessage();
	      if (!targetMessage || !targetMessage.bubble) {
	        hideAllSandboxProgress();
	        return false;
	      }
	      const text = String(errorText || "Generation failed.");
	      const progress = findSandboxChild(targetMessage.bubble, "sandbox-progress");
	      if (progress) progress.className = "hidden";
	      hideAllSandboxProgress();
	      if (targetMessage.el) targetMessage.el.className = "sandbox-message error";
	      if (targetMessage.bubble) targetMessage.bubble.className = "sandbox-bubble pc-chat-item pc-chat-item--error";
	      if (targetMessage.body) setSandboxMessageBody(targetMessage.body, text, { markdown: false });
	      if (targetMessage.meta?.children?.[0]) targetMessage.meta.children[0].textContent = "Error";
	      if (targetMessage.meta?.children?.[1]) targetMessage.meta.children[1].textContent = sandboxNow();
	      const target = $("sandbox-transcript");
	      target.scrollTop = target.scrollHeight;
	      return true;
	    }
	    // ONE SANDBOX SESSION, ONE OWNER RUN. POST /attachments/upload takes a
	    // SESSION id and stores under its owner run `session_memory_<session_id>`
	    // (routes/gateway.py `_session_memory_run_id`); the media routes take
	    // that owner RUN id. Uploading with the run id as `session_id` filed
	    // attachments under `session_memory_session_memory_...`, a second owner
	    // run the sandbox's generations never saw.
	    // ONE ALPHABET WITH THE SERVER. A session-memory owner run id must match
	    // routes/gateway.py `_SAFE_RUN_ID_PATTERN` (^[a-zA-Z0-9_-]+$): the media
	    // and voice routes create `session_memory_<id>` owner runs only for such
	    // ids (anything else is a 404), and `_session_memory_run_id` hashes a
	    // session id outside it. Principal ids may carry `:`, so each part is
	    // folded to that alphabet here, never passed through.
	    function sessionMemoryIdPart(value, fallback) {
	      return String(value || fallback).toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || fallback;
	    }
	    function sandboxSessionId() {
	      const p = state.principal || {};
	      return `gateway_console_sandbox_${sessionMemoryIdPart(p.tenant_id, "default")}_${sessionMemoryIdPart(p.user_id || p.runtime_id, "user")}`;
	    }
	    function sandboxRunId() {
	      return `session_memory_${sandboxSessionId()}`;
	    }
	    function sandboxRequestId() {
	      return `sandbox_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
	    }
	    async function uploadSandboxFile(file) {
	      if (typeof FormData === "undefined") throw new Error("This browser does not support file uploads.");
	      const form = new FormData();
	      form.append("session_id", sandboxSessionId());
	      form.append("file", file);
	      form.append("filename", file?.name || "upload.bin");
	      if (file?.type) form.append("content_type", file.type);
	      const headers = new Headers();
	      headers.set("Accept", "application/json");
	      const token = csrf();
	      if (token) headers.set("X-AbstractGateway-CSRF", decodeURIComponent(token));
	      const res = await fetch("/api/gateway/attachments/upload", { method: "POST", body: form, headers, credentials: "same-origin" });
	      const text = await res.text();
	      let data = {};
	      try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
	      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
	      const artifact = data.attachment || data.artifact;
	      return { name: file?.name || "upload.bin", size: file?.size || 0, content_type: file?.type || artifact?.content_type || "", artifact };
	    }
	    function renderSandboxAttachments() {
	      const target = $("sandbox-attachments");
	      if (!target) return;
	      target.textContent = "";
	      for (const item of state.sandboxAttachments || []) {
	        const chip = document.createElement("span");
	        chip.className = "sandbox-attachment";
	        chip.innerHTML = `<span>${esc(item.name || "attachment")}</span>`;
	        target.append(chip);
	      }
	    }
	    async function handleSandboxFiles(files) {
	      const list = Array.from(files || []).filter(Boolean);
	      if (!list.length) return;
	      $("sandbox-message").textContent = "Uploading attachments...";
	      $("sandbox-message").className = "message";
	      try {
	        for (const file of list) state.sandboxAttachments.push(await uploadSandboxFile(file));
	        renderSandboxAttachments();
	        $("sandbox-message").textContent = "";
	      } catch (err) {
	        $("sandbox-message").textContent = String(err.message || err);
	        $("sandbox-message").className = "message error";
	      }
	    }
	    function setSandboxSpeakButton(button, mode) {
	      if (!button) return;
	      if (mode === "pause") {
	        button.disabled = false;
	        button.classList.add("speaking");
	        button.title = "Pause speech";
	        button.setAttribute?.("aria-label", "Pause speech");
	        button.innerHTML = `<span aria-hidden="true">II</span>`;
	      } else if (mode === "loading") {
	        button.disabled = true;
	        button.title = "Generating speech";
	        button.setAttribute?.("aria-label", "Generating speech");
	        button.innerHTML = `<span aria-hidden="true">...</span>`;
	      } else {
	        button.disabled = false;
	        button.classList.remove("speaking");
	        button.title = "Speak this message";
	        button.setAttribute?.("aria-label", "Speak this message");
	        button.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	      }
	    }
	    async function playSandboxAudio(audio, button) {
	      if (!audio) return;
	      try {
	        if (typeof audio.play === "function") {
	          await audio.play();
	          setSandboxSpeakButton(button, "pause");
	        }
	      } catch (err) {
	        setSandboxSpeakButton(button, "speak");
	        throw err;
	      }
	    }
	    async function speakSandboxText(text, host, button = null) {
	      const row = sandboxVoiceDefaultRow();
	      if (!row) return;
	      if (host?._sandboxSpeechAudio) {
	        const audio = host._sandboxSpeechAudio;
	        if (!audio.paused) {
	          if (typeof audio.pause === "function") audio.pause();
	          setSandboxSpeakButton(button, "speak");
	        } else {
	          await playSandboxAudio(audio, button);
	        }
	        return;
	      }
	      if (host?._sandboxSpeechBusy) return;
	      if (host) host._sandboxSpeechBusy = true;
	      setSandboxSpeakButton(button, "loading");
	      const runId = sandboxRunId();
	      try {
	        const body = { text, provider: row.provider, model: row.model, request_id: sandboxRequestId() };
	        const voice = textValue(objectValue(row.options)?.voice || objectValue(row.options)?.profile);
	        if (voice) body.voice = voice;
	        const res = await api(`/api/gateway/runs/${encodeURIComponent(runId)}/voice/tts`, { slow: true, method: "POST", body: JSON.stringify(body) });
	        const audio = document.createElement("audio");
	        audio.preload = "auto";
	        audio.className = "hidden";
	        audio.onended = () => setSandboxSpeakButton(button, "speak");
	        audio.onpause = () => setSandboxSpeakButton(button, "speak");
	        audio.onplay = () => setSandboxSpeakButton(button, "pause");
	        if (host) host.append(audio);
	        await setSandboxMediaSource(audio, host, runId, res.audio_artifact, "Speech");
	        if (host) host._sandboxSpeechAudio = audio;
	        await playSandboxAudio(audio, button);
	      } catch (err) {
	        addSandboxMediaError(host, String(err.message || err));
	        setSandboxSpeakButton(button, "speak");
	      } finally {
	        if (host) host._sandboxSpeechBusy = false;
	      }
	    }
	    async function runSandbox() {
	      $("sandbox-message").textContent = "";
	      $("sandbox-message").className = "message";
	      const prompt = $("sandbox-prompt").value.trim();
	      const attachments = (state.sandboxAttachments || []).slice();
	      if (!prompt && !attachments.length) {
	        $("sandbox-message").textContent = "Prompt is required.";
	        $("sandbox-message").className = "message error";
	        return;
	      }
	      const row = selectedSandboxRoute();
	      const key = defaultRowKey(row);
	      const mode = sandboxRouteMode(key);
	      $("sandbox-run").disabled = true;
	      let pendingMessage = null;
	      try {
	        const promptText = prompt || "Please analyze the attached file(s).";
	        appendSandboxMessage("You", promptText, { kind: "user", attachments });
	        $("sandbox-prompt").value = "";
	        state.sandboxAttachments = [];
	        renderSandboxAttachments();
	        if (!defaultRowConfigured(row)) throw new Error(`${sandboxRouteLabel(row)} is not configured.`);
	        const started = Date.now();
	        if (mode === "text") {
	          const provider = row.provider;
	          const model = row.model;
	          const payload = {
	            capability: key,
	            provider,
	            model,
	            prompt: promptText,
	            system_prompt: $("sandbox-system").value.trim() || null,
	            messages: state.sandboxMessages,
	            attachments: attachments.map((item) => item.artifact).filter(Boolean),
	            client_context: sandboxClientContext(),
	          };
	          const reasoningChoice = ($("sandbox-reasoning")?.value || "").trim();
	          if (reasoningChoice) payload.reasoning = reasoningChoice;
	          const mtpChoice = $("sandbox-speculation").value;
	          if (mtpChoice) payload.speculation = speculationFromChoice(mtpChoice, undefined, true);
	          pendingMessage = appendSandboxMessage(`${state.providerLabels.get(provider) || provider} / ${model}`, "Thinking...", { pending: true, pendingLabel: "Generating answer", kind: "assistant" });
	          const res = await api("/api/gateway/sandbox/generate", { slow: true, method: "POST", body: JSON.stringify(payload) });
	          const text = res.response || "(empty response)";
	          state.sandboxMessages.push({ role: "user", content: promptText }, { role: "assistant", content: text });
	          finalizeSandboxMessage(pendingMessage, { content: text, usage: res.usage, elapsedMs: Date.now() - started, speakable: true, reasoning: res.reasoning || "", meta: speculationSummary(res) });
	        } else {
	          const runId = sandboxRunId();
	          let endpoint = "";
	          let body = {};
	          if (mode === "image") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/images/generate`;
	            body = { prompt: promptText, image_provider: row.provider, image_model: row.model, request_id: sandboxRequestId() };
	          } else if (mode === "voice") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/voice/tts`;
	            body = { text: promptText, provider: row.provider, model: row.model, request_id: sandboxRequestId() };
	            const voice = textValue(objectValue(row.options)?.voice || objectValue(row.options)?.profile);
	            if (voice) body.voice = voice;
	          } else if (mode === "music" || mode === "sound") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/music/generate`;
	            const soundTask = key === "output.sound" || mode === "sound";
	            body = { prompt: promptText, task: soundTask ? "text_to_audio" : "text_to_music", music_provider: row.provider, music_model: row.model, request_id: sandboxRequestId() };
	          } else if (mode === "video") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/videos/generate`;
	            body = { prompt: promptText, video_provider: row.provider, video_model: row.model, request_id: sandboxRequestId() };
	          }
	          pendingMessage = appendSandboxMessage(sandboxRouteShortLabel(row), "Starting generation...", { pending: true, pendingLabel: mode === "image" || mode === "video" ? "Generating media" : "Generating artifact", kind: "assistant" });
	          // The slow lane, like every other media route: local diffusion on
	          // Apple silicon runs for MINUTES (the seeded flux/wan defaults), so
	          // the 60s budget would abort the socket while the gateway kept
	          // generating — orphaning the artifact, whose ref only comes back in
	          // THIS response. `endpoint` is computed, which is exactly why it was
	          // missed when the literal-path siblings were marked.
	          const res = await api(endpoint, { slow: true, method: "POST", body: JSON.stringify(body) });
	          if (res.ok === false) throw new Error(res.error || res.code || "Generation failed.");
	          const ref = res.image_artifact || res.audio_artifact || res.music_artifact || res.video_artifact || null;
	          finalizeSandboxMessage(pendingMessage, {
	            content: "Generation completed.",
	            elapsedMs: Date.now() - started,
	            artifactRef: ref,
	            runId,
	            mode,
	            artifactLabel: mode === "image" ? "Open image" : mode === "video" ? "Open video" : "Open audio",
	          });
	        }
	      } catch (err) {
	        const message = String(err.message || err);
	        $("sandbox-message").textContent = message;
	        $("sandbox-message").className = "message error";
	        if (!failSandboxMessage(pendingMessage, message)) appendSandboxMessage("Error", message, { kind: "error" });
	      } finally {
	        updateSandboxControls();
	      }
	    }
	    function clearSandbox() {
	      sandboxRevokeObjectUrls();
	      state.sandboxMessages = [];
	      state.sandboxAttachments = [];
	      $("sandbox-transcript").textContent = "";
	      $("sandbox-message").textContent = "";
	      $("sandbox-message").className = "message";
	      $("sandbox-prompt").value = "";
	      renderSandboxAttachments();
	    }
	    function defaultRowTestable(row) {
	      // Test renders only where a REAL generation is cheap and the goal is
	      // named (voice audition; tiny text smoke). Image/video/music tests
	      // would be slow, expensive, and ride watchdog-less routes — the
	      // Sandbox tab is their honest surface.
	      const key = defaultRowKey(row);
	      return key === "output.voice" || key === "output.text" || key === "input.text";
	    }
	    function clearDefaultTest() {
	      const target = $("default-modal-test");
	      if (target) target.textContent = "";
	    }
    async function openDefaultModal(row) {
	      if (defaultRowReadOnly(row)) return;
	      state.activeDefaultRow = row;
	      const key = defaultRowKey(row);
	      const catalog = defaultCatalogForRow(row);
	      $("default-modal-title").textContent = defaultRowConfigured(row) ? "Edit multimodal capability" : "Configure multimodal capability";
	      $("default-modal-description").textContent = `Select a ${catalog.scope} provider, then choose one of its discovered compatible models.`;
	      $("default-modal-route").textContent = `${key} - ${defaultRowCapability(row)}`;
	      $("default-modal-message").textContent = "";
	      $("default-modal-message").className = "message";
	      clearDefaultTest();
	      // Per-route base URL and raw options, mirroring the console-TUI's route
	      // editor. Offline, base_url is how a route is pointed at a local
	      // inference server on a non-default port — which is why the web console
	      // not having it was a real gap, not a cosmetic one.
	      $("modal-default-base-url").value = typeof row.base_url === "string" ? row.base_url : "";
	      const storedOptions = row.options && typeof row.options === "object" && !Array.isArray(row.options)
	        ? row.options
	        : null;
	      // The voice picker owns `voice`/`profile`; showing them here too would
	      // invite the two controls to disagree in front of the operator.
	      let shownOptions = storedOptions ? { ...storedOptions } : null;
	      if (shownOptions && isVoiceOutputDefault(row)) { delete shownOptions.voice; delete shownOptions.profile; }
	      if (shownOptions && isTextGenerationDefault(row)) delete shownOptions.speculation;
	      $("modal-default-options").value = shownOptions && Object.keys(shownOptions).length
	        ? JSON.stringify(shownOptions, null, 2)
	        : "";
	      // WHAT THE OPERATOR WAS SHOWN, kept so the save can tell an EDIT from
	      // an echo. These two fields are prefilled from the row the GRID last
	      // rendered and the grid is never re-read on open, so a save that named
	      // them unconditionally would let a minutes-old render overwrite a value
	      // changed through `abstractcore config` in between — the rollback the
	      // send-only-what-you-own rule exists to prevent, and it also froze
	      // input.text's inherited base_url/options onto a covered input.video
	      // row that AbstractCore deliberately refuses to persist server-side
	      // (core_config.py `_stored_route_row`). Compared, not echoed.
	      state.defaultModalPrefill = {
	        base_url: $("modal-default-base-url").value,
	        options: $("modal-default-options").value,
	        voice: defaultVoiceValue(row),
	        speculation: speculationChoice(row?.options?.speculation),
	      };
	      $("test-default").classList.toggle("hidden", !defaultRowTestable(row));
	      $("default-modal-backdrop").classList.remove("hidden");
	      let discoveredProviders = [];
	      try {
	        discoveredProviders = await fetchDefaultProviders(row);
	      } catch (err) {
	        $("default-modal-message").textContent = String(err.message || err);
	        $("default-modal-message").className = "message error";
	      }
	      if (defaultModalMoved(row)) return;
	      const providers = row.provider && !discoveredProviders.includes(row.provider)
	        ? [row.provider, ...discoveredProviders]
	        : discoveredProviders;
	      setSelectOptions($("modal-default-provider"), providers, {
	        emptyLabel: providers.length ? "Select provider..." : catalog.emptyProviders,
	        disabled: !providers.length,
	        selected: row.provider || "",
	        labelMap: state.providerStateLabels.get(catalog.scope) || null,
	      });
	      // Same rule as the model lane: a provider field that can only offer
	      // DISCOVERED values is a dead end when nothing can be reached — and a
	      // modality with no discovery endpoint at all (scene3d) would never be
	      // configurable from this console. The console-TUI has a CUSTOM row for
	      // exactly this (ui/routes.rs:822, 1114-1126).
	      //
	      // Keyed off what DISCOVERY returned, never off the list above: that one
	      // carries the row's own provider injected in front, so an already
	      // configured route reached the offline case with a one-entry select and
	      // a shut lane — the one provider it could not change was its own.
	      setCustomLane("modal-default-provider-custom", !discoveredProviders.length, row.provider || "");
	      if (row.provider && !discoveredProviders.includes(row.provider)) {
	        $("default-modal-message").textContent = `Configured provider "${row.provider}" is not currently discovered in the ${catalog.scope} catalog.`;
	        $("default-modal-message").className = "message error";
	      }
	      loadDefaultReasoning(row);
	      loadDefaultSpeculation(row);
	      // Each loader owns its own terminal state, message and free-text lane
	      // (see loadDefaultModels). All this sequencing still has to guarantee
	      // is that a failed MODEL load does not SKIP the voice load, or the
	      // voice select keeps the previous route's list. It must not repaint
	      // either select itself: the model fallback here rebuilt the select
	      // from row.model alone and ran on ANY rejection, so a voice-only
	      // failure erased a perfectly good discovered model list, and the bare
	      // error string overwrote the loader's "type the model id" guidance.
	      await loadDefaultModels(activeDefaultProvider(), row.model || "", row).catch(() => {});
	      refreshDefaultSpeculationSupport();
	      await loadDefaultVoices(activeDefaultProvider(), activeDefaultModel(), defaultVoiceValue(row), row).catch(() => {});
	      if (defaultModalMoved(row)) return;
	      $("clear-default").classList.toggle("hidden", !defaultRowConfigured(row));
	    }
	    function closeDefaultModal() {
	      $("default-modal-backdrop").classList.add("hidden");
	      state.activeDefaultRow = null;
	      state.defaultModalPrefill = null;
	    }
	    // Shared by the provider <select> and the free-text provider lane, so the
	    // two cannot drift into meaning different things.
	    async function reloadDefaultModalCatalogs() {
	      const row = state.activeDefaultRow || null;
	      clearDefaultTest();  // a stale audition must not survive a provider change
	      // Both loaders paint their own terminal state and rethrow. Catch here
	      // so a failed MODEL lookup still lets the voice select reach an end
	      // state instead of being skipped by the propagating rejection.
	      try {
	        await loadDefaultModels(activeDefaultProvider(), "", row);
	      } catch { /* terminal state + modal message already set by the loader */ }
	      await loadDefaultVoices(activeDefaultProvider(), activeDefaultModel(), "", row)
	        .catch(() => { /* ditto */ });
	      refreshDefaultSpeculationSupport();
	    }
    // ------------------------------------------------------------------
    // MODELS & ENGINES: AbstractCore's own screens, embedded.
    // The Gateway does not re-implement the model browser or the engine
    // installer: the server splices AbstractCore's fragments (html in the
    // #tab-catalog / #tab-engines panels, one shared css/js), and this code
    // mounts them with the gateway's api() (session cookie + CSRF), apiBase
    // /api/gateway (the gateway mirrors of /acore), the principal's admin
    // bit, the gateway host's name and the `abstractgateway` CLI spelling.
    // CORE_CONSOLE.available is false when the gateway's AbstractCore
    // predates the screens: the panels then carry a server-rendered card and
    // nothing is mounted (an optional feature, not an error).
    // ------------------------------------------------------------------
    const CORE_CONSOLE = __CORE_CONSOLE_CONFIG_JSON__;
    const coreMounts = new Map();
    const coreJobSeen = new Map();
    function coreConsoleLib() {
      if (!CORE_CONSOLE.available) return null;
      try {
        const w = typeof window !== "undefined" ? window : null;
        const lib = w && w.AbstractCoreConsole;
        return lib && typeof lib.mount === "function" ? lib : null;
      } catch { return null; }
    }
    function coreConsoleUnavailableText() {
      if (!CORE_CONSOLE.available) return `${CORE_CONSOLE.message || "Models and Engines require abstractcore ≥ 2.17.0."} Upgrade: ${CORE_CONSOLE.upgrade || 'pip install -U "abstractcore>=2.17.0"'}`;
      return "The AbstractCore console screens did not load in this page; reload it.";
    }
    // The screens' request contract: (method, full path, plain body) ->
    // parsed JSON, rejecting with an Error that carries `.status`. api()
    // already does that; this adapter only lifts the message of a refusal
    // body without a `detail` envelope ({ok:false, status, message, error})
    // so a 403/409 reads as its reason, not as "HTTP 409".
    async function coreConsoleRequest(method, path, body) {
      try {
        return await api(path, { slow: true, method: String(method || "GET").toUpperCase(), ...(body == null ? {} : { body: JSON.stringify(body) }) });
      } catch (err) {
        const data = err && err.data;
        const lifted = data && typeof data === "object" && !data.detail
          ? (data.message || (data.error && data.error.message) || "")
          : "";
        if (lifted) {
          const out = new Error(String(lifted));
          out.status = err.status;
          out.data = data;
          throw out;
        }
        throw err;
      }
    }
    function coreConsoleOnJob(job) {
      // A finished download/delete changes what the Multimodal grid and the
      // first-run starter kit report: re-probe ONCE per job, on the edge.
      if (!job || !job.job_id) return;
      const done = ["completed", "failed", "cancelled"].includes(String(job.status || ""));
      const before = coreJobSeen.get(job.job_id);
      coreJobSeen.set(job.job_id, done);
      if (done && before === false && (job.kind === "download" || job.kind === "delete")) {
        void refreshAvailability({ rerender: true });
      }
    }
    function coreConsoleOptions() {
      return {
        apiBase: "/api/gateway",
        request: coreConsoleRequest,
        isAdmin: () => !!(state.principal && state.principal.admin),
        hostName: CORE_CONSOLE.hostName,
        cliPrefix: "abstractgateway",
        onJob: coreConsoleOnJob,
      };
    }
    function mountCoreScreen(kind, el, key) {
      // Returns the mount handle, or null when the screens are unavailable
      // (the caller renders its own fallback). Idempotent per key: a second
      // open refreshes the mounted screen instead of mounting it twice.
      if (!el) return null;
      if (coreMounts.has(key)) {
        const h = coreMounts.get(key);
        try { if (h && typeof h.refresh === "function") h.refresh(); } catch { /* next poll retries */ }
        return h;
      }
      const lib = coreConsoleLib();
      if (!lib) return null;
      try {
        const handle = lib.mount(kind, el, coreConsoleOptions());
        coreMounts.set(key, handle);
        return handle;
      } catch (err) {
        el.innerHTML = `<p class="message error">Could not open the ${esc(kind)} screen: ${esc(String((err && err.message) || err))}</p>`;
        return null;
      }
    }
    function openCoreTab(tab, opts) {
      if (!state.principal) return;
      if (tab === "apps") {
        mountAppCards("tab", $("apps-root"));
        // The apps.* settings (console_ui.py): registry-driven.
        mountAppsSettings("tab", $("apps-settings-root"));
        // The backlog folder + exec runner + process manager.
        mountBacklogSettings($("backlog-settings-root"));
        // The skills shelf setting lives on the Skills & MCP page now (console_skills_mcp.py).
        return;
      }
      if (tab === "network") {
        // Who can reach this gateway (console_ui.py, contract gateway_network_v1).
        mountNetworkPanel("tab", $("network-root"));
        return;
      }
      if (tab === "providers") {
        // Local providers are the engine CARDS (console_ui.py), not
        // AbstractCore's table: one card per engine, one primary action per
        // state, plus the provider's connection (localProviderExtras).
        mountEngineCards("providers", $("engines-core-root"), { extra: localProviderExtras, onClick: onLocalProviderClick });
        return;
      }
      if (tab === "catalog") {
        // The catalog is CARDS, one per model, with a filter bar
        // (console_catalog.py). Its first open reads the
        // `#catalog?...` link; later opens keep the filters in use unless
        // the caller names some (the guide, an engine's "Browse models").
        const first = !mcStore.views.has("tab");
        mountModelCatalog("tab", $("catalog-cards-root"), {
          syncHash: true,
          filters: (opts && opts.filters) || (first ? mcParseHash(String(location.hash || "")) : null),
        });
      }
      if (!CORE_CONSOLE.available) return;  // the panel carries the server-rendered card
      const kind = "models";
      const root = $(`${tab}-core-root`);
      if (!mountCoreScreen(kind, root, `tab-${tab}`) && root) {
        root.innerHTML = `<p class="message warn">${esc(coreConsoleUnavailableText())}</p>`;
      }
    }
    // "Use as default" (the catalog cards, console_catalog.py): a downloaded
    // text model becomes the text-generation default through the same
    // capability-defaults write the Multimodal tab makes. The served model id
    // is the artifact minus LM Studio's `@quant` suffix (the download
    // reference pins a quantization, the route names the model).
    function servedModelId(provider, artifact) {
      const a = String(artifact || "");
      if (String(provider || "") === "lmstudio" && a.includes("@")) return a.slice(0, a.lastIndexOf("@"));
      return a;
    }
    // ------------------------------------------------------------------
    // FIRST RUN. A fresh install reaches this console through a
    // one-time link printed by `abstractgateway serve` / `abstractgateway
    // claim` (`/console#claim=<code>`): the code is redeemed for an ADMIN
    // browser session (POST /session/claim, loopback-only server-side),
    // stripped from the address bar before anything else can read it, and the
    // first-run wizard opens. The wizard opens by itself ONCE per data dir
    // (GET/POST /host/first-run); the topbar "Setup" button reopens it.
    //
    // DOM CONTRACT (later work embeds AbstractCore's Engines/Models fragments
    // into these ids -- do not rename): #first-run-backdrop, #first-run-wizard,
    // #first-run-steps, step panels #first-run-step-{welcome,engines,model,
    // apps,done}, bodies #first-run-host-summary, #first-run-engines-body,
    // #first-run-model-body, #first-run-apps-body, #first-run-done-body,
    // controls #first-run-back/-next/-skip/-finish, #first-run-message, and
    // the topbar #open-setup.
    // ------------------------------------------------------------------
/*__CONSOLE_UI_JS__*/
    // ---- Backlog settings: Continuum's backlog folder, the
    // backlog exec runner and the process manager, through the one
    // runtime-config door. GET /api/gateway/admin/runtime-config carries
    // {value, source: flag|stored|env|default, label, help, cli, available?,
    // reason?, default_path?} per key (runtime_config.BACKLOG_SETTINGS);
    // Save POSTs only the changed keys and shows the gateway's refusal as is.
    const backlogSetStore = { data: null, error: "", saving: false, saved: null, draft: {}, el: null };
    const BACKLOG_SET_KEYS = ["triage_repo_root", "backlog_exec_runner", "process_manager"];
    function backlogSourcePill(r) {
      const src = String((r && r.source) || "");
      if (src === "flag") return uiPill("Launch flag", "info", "Set by a serve launch flag for this run; a saved value applies once the gateway restarts without it");
      if (src === "stored") return uiPill("Saved setting", "info");
      if (src === "env") return uiPill("Environment (legacy)", "warn", "Set by the environment this gateway was started with; saving a value here replaces it");
      return uiPill("Default", "muted");
    }
    function backlogSettingsMarkup() {
      const st = backlogSetStore;
      if (st.error && !st.data) return `<div class="ui-alert tone-err" role="alert"><strong>Could not read the backlog settings.</strong><span>${esc(st.error)}</span></div>`;
      if (!st.data) return `<div class="ui-empty">Reading the backlog settings...</div>`;
      const admin = !!st.data.writable;
      const rows = BACKLOG_SET_KEYS.map((k) => [k, st.data[k]]).filter(([, r]) => r && typeof r === "object");
      const root = st.data.triage_repo_root || {};
      const trouble = root.available === false;
      let out = `<details class="ui-details ui-net-proxy" data-backlog-settings${trouble ? " open" : ""}><summary><span class="ui-net-proxy__title">Advanced: backlog settings (Continuum)</span>`
        + `<span class="ui-net-proxy__sum">${esc(trouble ? "backlog folder not available" : "backlog folder, exec runner, process manager")}</span></summary><div class="ui-net-proxy__body"><div class="ui-apps-settings__rows">`;
      for (const [key, r] of rows) {
        const has = Object.prototype.hasOwnProperty.call(st.draft, key);
        const dis = admin && !st.saving ? "" : " disabled";
        // The folder row spans the grid: a path is wider than one column.
        const span = key === "triage_repo_root" ? ' style="grid-column: 1 / -1"' : "";
        out += `<div class="ui-apps-setting" data-backlog-setting="${esc(key)}"${span}><div class="ui-apps-setting__head"><label for="backlog-set-${esc(key)}">${esc(r.label || key)}</label>${backlogSourcePill(r)}</div>`;
        if (key === "triage_repo_root") {
          const saved = r.source === "stored" ? String(r.value || "") : String(r.stored_value || "");
          const val = has ? st.draft[key] : saved;
          out += `<input type="text" id="backlog-set-${esc(key)}" data-backlog-input="${esc(key)}" autocomplete="off" spellcheck="false" value="${esc(val)}" placeholder="${esc(String(r.value || r.default_path || ""))}"${dis}>`;
          out += `<p class="ui-net-proxy__text">In use: <code style="overflow-wrap: anywhere">${esc(String(r.value || "(hidden)"))}</code></p>`;
          if (r.available === false) out += `<p class="ui-field-msg tone-warn">Not available: ${esc(r.reason || "")}</p>`;
          if (admin && r.default_path && r.value !== r.default_path) out += `<p><button type="button" class="ui-btn is-text" data-backlog-use-default${st.saving ? " disabled" : ""}>Use the gateway's own folder</button></p>`;
        } else {
          const savedSwitch = r.source === "stored" ? r.value : r.stored_value;
          const cur = has ? st.draft[key] : (savedSwitch === true ? "on" : savedSwitch === false ? "off" : "");
          const now = r.value ? "on" : "off";
          out += `<select id="backlog-set-${esc(key)}" data-backlog-input="${esc(key)}" style="width: 100%; min-width: 0"${dis}>`
            + `<option value=""${cur === "" ? " selected" : ""}>Not saved (now ${esc(now)})</option>`
            + `<option value="on"${cur === "on" ? " selected" : ""}>On</option><option value="off"${cur === "off" ? " selected" : ""}>Off</option></select>`;
        }
        out += `<p class="ui-net-proxy__text">${esc(r.help || "")}</p>`
          + `<span class="ui-advanced ui-sub"><code>${esc(r.cli || "")}</code>${r.flag ? ` · launch flag <code>serve ${esc(r.flag)}</code>` : ""}</span></div>`;
      }
      out += `</div>`;
      if (admin) out += `<div class="ui-card__actions"><button type="button" class="ui-btn is-primary" data-backlog-settings-save${st.saving ? ' disabled aria-busy="true"' : ""}>${st.saving ? "Saving..." : "Save backlog settings"}</button></div>`;
      if (st.saved) out += `<p class="ui-net-proxy__saved tone-${esc(st.saved.tone)}" role="status" data-backlog-settings-saved><b>${esc(st.saved.head)}</b><span>${esc(st.saved.text)}</span></p>`;
      else out += `<p class="ui-net-proxy__saved" role="status"><span>${admin ? "Empty = not saved: the launch flag, else the default (the gateway's own folder; switches off). Applies at once." : "Only an admin can change these."}</span></p>`;
      return out + `</div></details>`;
    }
    function backlogSettingsRender() { if (backlogSetStore.el) backlogSetStore.el.innerHTML = backlogSettingsMarkup(); }
    async function backlogSettingsRefresh() {
      try {
        backlogSetStore.data = await api("/api/gateway/admin/runtime-config");
        backlogSetStore.error = "";
      } catch (err) {
        backlogSetStore.error = String((err && err.message) || err);
      }
      backlogSettingsRender();
    }
    async function backlogSettingsPost(body) {
      const st = backlogSetStore;
      st.saving = true;
      st.saved = null;
      backlogSettingsRender();
      try {
        st.data = await api("/api/gateway/admin/runtime-config", { method: "POST", body: JSON.stringify(body) });
        st.draft = {};
        st.saved = { tone: "ok", head: "Saved", text: "Applies at once." };
      } catch (err) {
        const data = (err && err.data) || {};
        st.saved = { tone: "err", head: "Not saved", text: String((data && data.detail) || (err && err.message) || err) };
      }
      st.saving = false;
      backlogSettingsRender();
    }
    function backlogSettingsSave() {
      const st = backlogSetStore;
      if (!st.data || st.saving) return;
      const body = {};
      for (const key of BACKLOG_SET_KEYS) {
        if (!Object.prototype.hasOwnProperty.call(st.draft, key)) continue;
        const r = st.data[key] || {};
        const now = String(st.draft[key] || "").trim();
        if (key === "triage_repo_root") {
          const was = r.source === "stored" ? String(r.value || "") : String(r.stored_value || "");
          if (now !== was) body[key] = now || null;
        } else {
          const savedSwitch = r.source === "stored" ? r.value : r.stored_value;
          const was = savedSwitch === true ? "on" : savedSwitch === false ? "off" : "";
          if (now !== was) body[key] = now ? now === "on" : null;
        }
      }
      if (!Object.keys(body).length) { st.saved = { tone: "ok", head: "Nothing changed", text: "" }; backlogSettingsRender(); return; }
      backlogSettingsPost(body);
    }
    function mountBacklogSettings(el) {
      if (!el) return;
      backlogSetStore.el = el;
      el.onclick = (event) => {
        const t = event && event.target && event.target.closest ? event.target : null;
        if (!t) return;
        const save = t.closest("[data-backlog-settings-save]");
        if (save && !save.disabled) { backlogSettingsSave(); return; }
        const useDefault = t.closest("[data-backlog-use-default]");
        const r = (backlogSetStore.data || {}).triage_repo_root || {};
        if (useDefault && !useDefault.disabled && r.default_path) backlogSettingsPost({ triage_repo_root: r.default_path });
      };
      const onEdit = (event) => {
        const i = event && event.target && event.target.matches && event.target.matches("[data-backlog-input]") ? event.target : null;
        if (i) backlogSetStore.draft[i.dataset.backlogInput] = i.value;
      };
      el.oninput = onEdit;
      el.onchange = onEdit;
      backlogSettingsRender();
      backlogSettingsRefresh();
    }
    const FIRST_RUN_STEPS = ["welcome", "engines", "model", "apps", "done"];
    const FIRST_RUN_STEP_TITLES = {
      welcome: "Welcome",
      engines: "Local engines",
      model: "Default model",
      apps: "Apps",
      done: "Done",
    };
    const FIRST_RUN_STEP_COPY = {
      welcome: { title: "Welcome to your gateway", hint: "Check this computer", lede: "Your gateway is running on this computer and you are signed in as its admin. The next steps get you to a working model. Every step is optional." },
      engines: { title: "Local engines", hint: "Run models on this computer", lede: "Engines run AI models on this computer. Install one if you want local models; cloud providers only need an API key (Providers tab)." },
      model: { title: "Choose your default model", hint: "What your apps use", lede: "The recommended set is sized for this computer: set it up in one click, or pick any model that fits." },
      apps: { title: "Apps", hint: "Flow, Code, Observer...", lede: "Apps that work with this gateway: build workflows, code with an agent, watch runs, talk to your entities." },
      done: { title: "You are all set", hint: "Review and finish", lede: "Everything in this guide stays available in the console tabs, and Setup, at the bottom of the sidebar, reopens it." },
    };
    const FIRST_RUN_APPS = [
      { pkg: "@abstractframework/flow", name: "Flow", mark: "Fl", what: "Design workflows visually and run them on this gateway." },
      { pkg: "@abstractframework/code", name: "Code", mark: "Co", what: "A coding assistant in your browser, with sessions that survive restarts." },
      { pkg: "@abstractframework/observer", name: "Observer", mark: "Ob", what: "Watch runs live, replay them, and send commands to running work." },
      { pkg: "@abstractframework/continuum", name: "Continuum", mark: "Cn", what: "Your backlog, inbox and long-running processes, driven by this gateway." },
      { pkg: "@abstractframework/entity", name: "Entity", mark: "En", what: "Create entities, see their memory grow, and talk with them." },
    ];
    const firstRun = { open: false, step: "welcome", checked: false, autoOpened: false, host: null, engines: null, enginesMissing: false, enginesError: "" };
    function firstRunHash() {
      try { return String((typeof location !== "undefined" && location && location.hash) || ""); } catch { return ""; }
    }
    // `#claim=<code>&tab=<tab>` (the tray's "Open Console -> Models / Apps /
    // Network"): the tab survives the claim. The code is stripped, the tab
    // is kept as the ordinary `#<tab>` deep link that refresh() applies.
    function claimTabFromHash(hash) {
      const m = /(?:^#|&)tab=([^&]+)/.exec(String(hash || ""));
      if (!m) return "";
      let tab = "";
      try { tab = decodeURIComponent(m[1]).trim(); } catch { tab = ""; }
      return TABS.includes(tab) ? tab : "";
    }
    function stripClaimFromUrl(keepTab) {
      // The code is a credential: it must not survive in the address bar, the
      // history entry, or a bookmark. replaceState keeps the page as it is.
      const frag = keepTab ? `#${keepTab}` : "";
      try {
        if (typeof history !== "undefined" && history && typeof history.replaceState === "function" && typeof location !== "undefined") {
          history.replaceState(null, "", String(location.pathname || "/console") + String(location.search || "") + frag);
        } else if (typeof location !== "undefined" && location) {
          location.hash = frag;
        }
      } catch { /* best effort: the server already made the code single-use */ }
    }
    function redeemClaimFromHash() {
      // Returns true when it took over the boot (it calls refresh() itself).
      const hash = firstRunHash();
      const match = /(?:^#|&)claim=([^&]+)/.exec(hash);
      if (!match) return false;
      const code = decodeURIComponent(match[1]);
      firstRun.claimTab = claimTabFromHash(hash);
      stripClaimFromUrl(firstRun.claimTab);
      setLoginStatus("Signing in…", "neutral");
      (async () => {
        try {
          const res = await api("/api/gateway/session/claim", { method: "POST", body: JSON.stringify({ code }) });
          firstRun.claimed = true;
          // Who minted the link (`claim.created_by`: "tray",
          // "installer", ...); absent on older gateways -> null.
          firstRun.claimCreatedBy = (res && res.claim && typeof res.claim.created_by === "string") ? res.claim.created_by : null;
          setLoginStatus("Signed in", "ok");
        } catch (err) {
          signinShow("login-message", `This sign-in link was not accepted: ${String(err.message || err)}`);
          setLoginStatus("Not signed in", "neutral");
        }
        await refresh();
      })();
      return true;
    }
    async function maybeOpenFirstRun() {
      // Once per page load; admins only (the wizard's writes are admin routes).
      if (firstRun.checked || !state.principal || !state.principal.admin) return;
      firstRun.checked = true;
      let st = null;
      try { st = await api("/api/gateway/host/first-run"); } catch { return; }
      if (firstRunShouldAutoOpen(st)) {
        firstRun.autoOpened = true;
        openFirstRunWizard("welcome");
      }
    }
    // The guide opens by itself only while first run is NOT completed. A
    // claim link no longer forces it (the tray's "Open Console" mints one on
    // every click, and a finished setup must not greet the operator with the
    // guide each time); the "Setup guide" button always reopens it. A link
    // that asks for a tab (`#claim=...&tab=apps`, or a plain `#apps` deep
    // link) lands on that tab, not under the guide. `claim.created_by` (when the gateway sends it): a
    // tray link never opens the guide, an installer link opens it until first
    // run is completed; without the field, `completed` alone decides.
    function firstRunShouldAutoOpen(st) {
      const completed = !!(st && st.completed);
      if (completed) return false;
      if (firstRun.claimTab || state.hashApplied) return false;  // a link that names a tab lands there
      if (firstRun.claimCreatedBy === "tray") return false;
      return true;
    }
    function openFirstRunWizard(step) {
      if (!firstRun.open) {
        try { firstRun.opener = typeof document !== "undefined" && document.activeElement && document.activeElement !== document.body ? document.activeElement : null; } catch { firstRun.opener = null; }
      }
      firstRun.open = true;
      $("first-run-message").textContent = "";
      $("first-run-message").className = "message";
      $("first-run-backdrop").classList.remove("hidden");
      firstRunGoto(step || "welcome");
    }
    function closeFirstRunWizard() {
      firstRun.open = false;
      $("first-run-backdrop").classList.add("hidden");
      // The wizard's copies stop polling and release their key handlers; the
      // Models/Engines tabs keep theirs.
      unmountModelCatalog("first-run");
      unmountEngineCards("first-run");
      unmountAppCards("first-run");
      unmountNetworkPanel("first-run");
      // Focus goes back to what opened the guide (keyboard users keep their
      // place); the guide took it on open.
      const back = firstRun.opener;
      firstRun.opener = null;
      if (back && typeof back.focus === "function" && back.isConnected !== false) { try { back.focus(); } catch { /* gone */ } }
    }
    async function completeFirstRun(outcome) {
      try {
        await api("/api/gateway/host/first-run", { method: "POST", body: JSON.stringify({ outcome }) });
      } catch (err) {
        $("first-run-message").textContent = `Could not record the first-run state: ${String(err.message || err)}`;
        $("first-run-message").className = "message error";
        return;
      }
      closeFirstRunWizard();
    }
    function renderFirstRunSteps() {
      const list = $("first-run-steps");
      const current = FIRST_RUN_STEPS.indexOf(firstRun.step);
      list.innerHTML = FIRST_RUN_STEPS.map((step, i) => {
        const cls = i === current ? " is-active" : (firstRun.visited && firstRun.visited.has(step) && i < current ? " is-done" : "");
        const copy = FIRST_RUN_STEP_COPY[step] || {};
        return `<li><button type="button" class="first-run-step${cls}" data-first-run-step="${esc(step)}"${i === current ? ' aria-current="step"' : ""}>`
          + `<span class="first-run-step__num">${cls === " is-done" ? "&#10003;" : i + 1}</span>`
          + `<span class="first-run-step__title">${esc(FIRST_RUN_STEP_TITLES[step])}</span>`
          + `<span class="first-run-step__hint">${esc(copy.hint || "")}</span></button></li>`;
      }).join("");
      list.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-first-run-step]") : null;
        if (b) firstRunGoto(b.dataset.firstRunStep);
      };
    }
    function firstRunGoto(step) {
      if (!FIRST_RUN_STEPS.includes(step)) step = "welcome";
      firstRun.step = step;
      if (!firstRun.visited) firstRun.visited = new Set();
      firstRun.visited.add(step);
      for (const s of FIRST_RUN_STEPS) $(`first-run-step-${s}`).classList.toggle("hidden", s !== step);
      const idx = FIRST_RUN_STEPS.indexOf(step);
      const copy = FIRST_RUN_STEP_COPY[step] || {};
      $("first-run-kicker").textContent = `Step ${idx + 1} of ${FIRST_RUN_STEPS.length}`;
      $("first-run-step-title").textContent = copy.title || FIRST_RUN_STEP_TITLES[step];
      $("first-run-step-lede").textContent = copy.lede || "";
      const scroller = $("first-run-scroll");
      if (scroller) scroller.scrollTop = 0;
      // Keyboard: each step starts at its title (Tab then walks the step's
      // controls, then the footer's Back/Next; Escape closes the guide).
      const heading = $("first-run-step-title");
      if (heading && typeof heading.setAttribute === "function" && typeof heading.focus === "function") {
        heading.setAttribute("tabindex", "-1");
        try { heading.focus({ preventScroll: true }); } catch { /* not focusable here */ }
      }
      $("first-run-back").classList.toggle("hidden", idx === 0);
      $("first-run-next").classList.toggle("hidden", step === "done");
      $("first-run-finish").classList.toggle("hidden", step !== "done");
      renderFirstRunSteps();
      if (step === "welcome") loadFirstRunWelcome();
      if (step === "engines") loadFirstRunEngines();
      if (step === "model") loadFirstRunModel();
      if (step === "apps") renderFirstRunApps();
      if (step === "done") renderFirstRunDone();
    }
    function firstRunStep(delta) {
      const idx = FIRST_RUN_STEPS.indexOf(firstRun.step);
      firstRunGoto(FIRST_RUN_STEPS[Math.max(0, Math.min(FIRST_RUN_STEPS.length - 1, idx + delta))]);
    }
    function firstRunKv(rows) {
      return `<dl class="first-run-kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
    }
    function firstRunTiles(tiles) {
      return `<dl class="first-run-tiles">${tiles.map(([k, v, sub]) => `<div class="first-run-tile"><dt>${esc(k)}</dt><dd>${v}${sub ? `<div class="ui-sub">${sub}</div>` : ""}</dd></div>`).join("")}</dl>`;
    }
    // /host/state's `host` block is AbstractCore's host identity
    // ({host_id, host_name, kind}: utils/hostinfo.get_host_identity); it has
    // no OS field. The one OS fact in the payload is the gateway's service
    // block, `gateway.service.platform` (os_service.service_status ->
    // host_paths.normalize_platform: darwin | windows | linux, any other
    // POSIX reported as linux). Unknown value: no sub-line, never a guess.
    function firstRunOsLabel(platform) {
      return { darwin: "macOS", windows: "Windows", linux: "Linux" }[String(platform || "").trim().toLowerCase()] || "";
    }
    async function loadFirstRunWelcome() {
      const box = $("first-run-host-summary");
      box.innerHTML = `<div class="ui-empty">Looking at this computer...</div>`;
      let snap = null;
      try { snap = await api("/api/gateway/host/state"); } catch (err) {
        box.innerHTML = `<div class="ui-alert tone-warn" role="alert"><strong>This computer's summary is not available right now.</strong><span>${esc(String(err.message || err))}</span></div>`;
        return;
      }
      firstRun.host = snap;
      const gw = (snap && snap.gateway) || {};
      const ram = ((snap && snap.memory) || {}).ram || {};
      const gpus = (((snap && snap.gpu) || {}).gpus || []).map((g) => g && g.name).filter(Boolean);
      const host = (snap && snap.host) || {};
      const svc = gw.service || {};
      const dataDir = String(gw.data_dir || "?");
      box.innerHTML = firstRunTiles([
        ["Computer", esc(host.host_name || CORE_CONSOLE.hostName || "This computer"), esc(firstRunOsLabel(svc.platform))],
        ["Memory", esc(typeof ram.total_bytes === "number" ? _fmtBytes(ram.total_bytes) : "Unknown"), "Available to models and apps"],
        ["Graphics", esc(gpus.length ? gpus.join(", ") : "None detected"), gpus.length ? "Used to run local models" : "Models run on the processor"],
        ["Data folder", `<code class="ui-ellip is-block" title="${esc(dataDir)}">${esc(dataDir)}</code>`, `Runs, workflows and settings live here <span class="ui-advanced">(${esc(gw.data_dir_source || "?")})</span>`],
        ["Sign-in", esc(gw.auth_mode === "users" ? "User accounts" : String(gw.auth_mode || "Unknown")), gw.auth_mode === "users" ? "You are the admin" : ""],
        ["Starts at login", esc(svc.installed ? "Yes" : "Not yet"), svc.installed ? esc(`Installed as a ${svc.mechanism || "service"}`) : `Keeps the gateway running after a restart <span class="ui-advanced"><code>abstractgateway service install</code></span>`],
      ])
        + `<div class="ui-section-title"><h3>What this guide sets up</h3><span class="ui-sub">Each step takes a minute; skip any of them.</span></div>`
        + `<div class="ui-card-grid is-fit">${["engines", "model", "apps"].map((step) => {
          const copy = FIRST_RUN_STEP_COPY[step] || {};
          const n = FIRST_RUN_STEPS.indexOf(step) + 1;
          return `<article class="ui-card"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${n}</span>`
            + `<div class="ui-card__titles"><div class="ui-card__title">${esc(copy.title || FIRST_RUN_STEP_TITLES[step])}</div></div></div>`
            + `<div class="ui-card__blurb">${esc(copy.lede || "")}</div>`
            + `<div class="ui-card__actions"><button type="button" class="ui-btn is-ghost" data-first-run-step="${esc(step)}">Go to ${esc(FIRST_RUN_STEP_TITLES[step].toLowerCase())}</button></div></article>`;
        }).join("")}</div>`;
      box.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-first-run-step]") : null;
        if (b) firstRunGoto(b.dataset.firstRunStep);
      };
    }
    function loadFirstRunEngines() {
      // Local engines as cards (console_ui.py): detection, version, status,
      // Install with inline progress, plain-language failures, the log behind
      // "Show details". Nothing here depends on AbstractCore's table screen.
      mountEngineCards("first-run", $("first-run-engines-body"));
    }
    async function loadFirstRunModel() {
      const box = $("first-run-model-recommended");
      box.innerHTML = `<p class="subtle">Checking the recommended starter models...</p>`;
      // The Models tab's catalog cards below the starter kit, with "Fits this
      // computer" preset, so the list opens on what can run here; "Open in the
      // Models tab" carries the same filters over (console_catalog.py).
      mountModelCatalog("first-run", $("first-run-model-catalog"), { guide: true, filters: { fits: true } });
      await refreshAvailability({ rerender: false });
      renderFirstRunModel();
      uiRestoreDownloads();
    }
    const FIRST_RUN_ROUTE_COPY = {
      "input.text": { mark: "Aa", title: "Chat and text", what: "Answers, agents and workflows" },
      "output.text": { mark: "Aa", title: "Chat and text", what: "Answers, agents and workflows" },
      "output.voice": { mark: "Vo", title: "Voice", what: "Reads answers aloud" },
      "input.voice": { mark: "Mi", title: "Transcription", what: "Turns speech into text" },
      "output.image": { mark: "Im", title: "Images", what: "Creates pictures from a description" },
      "input.image": { mark: "Vi", title: "Vision", what: "Understands pictures" },
      "output.video": { mark: "Vd", title: "Video", what: "Creates short videos from a description or a picture" },
    };
    // A route this host cannot run gets a card with AbstractCore's reason and
    // no Download button (nothing here can fetch a model that cannot run):
    //   `recommendation_unavailable` an UNSET route whose recommended engine
    //                                cannot run here (MLX-Gen images on Linux);
    //                                it has no download, so `plan.recommended`
    //                                never lists it and the guide stayed silent
    //   `route_unavailable`          a CONFIGURED route whose provider cannot
    //                                run here (the same route carried over from
    //                                a Mac); calls to it fail until it changes
    // A route the download plan already lists gets no second card: its own
    // card carries the warning (`firstRunRouteUnavailableAlert`).
    function firstRunUnavailableCards(recommendedRows) {
      const listed = new Set((recommendedRows || []).map((r) => r && r.route).filter(Boolean));
      // A derived row (`output.text` <- `input.text`) carries its source's
      // flag; the source row has the card.
      return (state.defaults || []).filter((row) => !row.derived_from && !listed.has(defaultRowKey(row)) && (defaultRowRouteUnavailableReason(row) || defaultRowUnavailableReason(row))).map((row) => {
        const key = defaultRowKey(row);
        const broken = Boolean(defaultRowRouteUnavailableReason(row));
        const info = broken ? row.route_unavailable : row.recommendation_unavailable;
        const reason = broken ? defaultRowRouteUnavailableReason(row) : defaultRowUnavailableReason(row);
        const copy = FIRST_RUN_ROUTE_COPY[key] || { mark: "AI", title: key || "Model", what: "" };
        const provider = state.providerLabels.get(info.provider) || info.provider || "";
        const pill = broken ? uiPill("Cannot run here", "warn", reason) : uiPill("Not available here", "muted", reason);
        const factLabel = broken ? "Configured" : "Recommended elsewhere";
        return `<article class="ui-card ${broken ? "first-run-route-unavailable" : "first-run-unavailable"}"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(copy.mark)}</span>`
          + `<div class="ui-card__titles"><div class="ui-card__title">${esc(copy.title)}</div><span class="ui-card__status">${pill}</span></div></div>`
          + `<div class="ui-card__blurb">${esc(copy.what)}</div>`
          + `<div class="ui-card__body">`
          + (broken ? `<div class="ui-alert tone-warn" role="note"><span>Configured but cannot run on this computer: ${esc(reason)}</span></div>` : `<p class="ui-card__note">${esc(reason)}</p>`)
          + `<ul class="ui-facts">${provider ? `<li${broken ? "" : ` class="ui-advanced"`}>${factLabel}: <b>${esc(provider)}</b> <code class="ui-ellip" title="${esc(info.model || "")}">${esc(info.model || "")}</code></li>` : ""}<li class="ui-advanced">Route <code>${esc(key)}</code></li></ul></div>`
          + `<div class="ui-card__actions"></div>`
          + `<div class="ui-card__tech"></div>`
          + `</article>`;
      }).join("");
    }
    // The warning a recommended-download card carries when its route is
    // configured with a provider this host cannot run (`route_unavailable`).
    function firstRunRouteUnavailableAlert(route) {
      const row = findDefaultRow(state.defaults, route);
      const reason = defaultRowRouteUnavailableReason(row);
      if (!reason) return "";
      const pair = [row.route_unavailable.provider, row.route_unavailable.model].filter(Boolean).join(" / ");
      return `<div class="ui-alert tone-warn capability-route-unavailable" role="note"><span>Configured now${pair ? ` (${esc(pair)})` : ""} but cannot run on this computer: ${esc(reason)}</span></div>`;
    }
    function firstRunCap(text) { const t = String(text || ""); return t ? t.charAt(0).toUpperCase() + t.slice(1) : t; }
    function renderFirstRunModel() {
      if (!firstRun.open || firstRun.step !== "model") return;
      const box = $("first-run-model-recommended");
      const plan = state.availabilityPlan || {};
      const rows = Array.isArray(plan.recommended) ? plan.recommended : [];
      const text = (state.defaults || []).find((r) => r && r.key === "output.text")
        || (state.defaults || []).find((r) => r && r.key === "input.text") || null;
      const current = text && text.provider && text.model ? `${state.providerLabels.get(text.provider) || text.provider} · ${text.model}` : "";
      const cards = rows.map((r) => {
        const job = state.downloadJobs.get(downloadJobKey(r.provider, r.artifact));
        const copy = FIRST_RUN_ROUTE_COPY[r.route] || { mark: "AI", title: r.route || "Model", what: "" };
        let pill;
        let body = "";
        let cancel = "";
        if (job && dlActive(job)) {
          const jid = dlJobId(job);
          pill = dlStatePill(job);
          const phaseLabel = UI_PHASE_LABELS[uiJobPhase(job)] || "Downloading";
          body = uiProgressMarkup(job, job.parent_job ? `${phaseLabel} · part of Download all` : phaseLabel);
          // Two steps (console_ui.py dlCancelMarkup): a click only asks.
          cancel = dlCancelMarkup(jid, "Cancel download");
        } else if (job && job.status === "failed") {
          // The plain reason first (AbstractCore's `ended_reason`: what
          // happened, what to do); the verbatim error behind Show details.
          pill = uiPill("Download failed", "err");
          const said = String(job.ended_reason || job.message || "Try again.").trim();
          const why = String(job.error || job.message || "").trim();
          body = `<div class="ui-alert tone-err" role="alert"><strong>The download did not finish.</strong><span>${esc(said)}</span></div>`
            + (why && why !== said ? uiDetails(`err:${dlJobId(job)}`, "Show details", `<pre class="ui-log">${esc(why)}</pre>`) : "");
        } else if (job && job.status === "cancelled" && r.status === "absent") {
          // "Cancelled" only ever follows a cancel REQUEST; the job says who
          // made it and when (`ended_reason`), so the tile does too.
          pill = uiPill("Cancelled", "muted");
          body = `<p class="ui-card__note">${esc(job.ended_reason || "Download cancelled. Download it again any time.")}</p>`;
        } else if (job && (job.status === "completed" || job.state === "done") && r.status === "absent") {
          // The job says done before the next availability probe does: show
          // the result now (the job's own sentence), not a stale "absent".
          pill = uiPill("Downloaded", "ok");
          body = `<p class="ui-card__note">${esc(job.message || "Downloaded.")}</p>`;
        } else {
          const view = weightView(r);
          const tone = view.cls === "ok" ? "ok" : view.cls === "off" ? "muted" : "info";
          pill = uiPill(firstRunCap(view.label), tone, r.detail || r.evidence || r.instruction || "");
          if (r.status === "unknown" && weightReason(r)) body = `<p class="ui-card__note first-run-presence-reason">${esc(weightReason(r))}</p>`;
        }
        const canDownload = weightView(r).canDownload && !(job && (dlActive(job) || job.status === "completed"));
        // DESIGN-v2 §5: the card names the ENGINE that runs the route and its model
        // ("faster-whisper · base"), AbstractCore's plan `route_provider`/`route_model`;
        // the download (Hugging Face repo) is a technical detail.
        if (typeof r.route_provider !== "string" || typeof r.route_model !== "string") {
          console.error(`AbstractGateway console: the recommended plan row ${r.route} has no route_provider/route_model (core seam, DESIGN-v2 §5).`);
        }
        const routeProvider = typeof r.route_provider === "string" ? r.route_provider : r.provider;
        const routeModel = typeof r.route_model === "string" ? r.route_model : r.artifact;
        const provider = state.providerLabels.get(routeProvider) || routeProvider || "";
        const viaDownload = (routeProvider !== r.provider || routeModel !== r.artifact)
          ? `<li class="ui-advanced">Downloaded as <code>${esc(r.artifact || "")}</code> (${esc(state.providerLabels.get(r.provider) || r.provider || "")})</li>`
          : "";
        return `<article class="ui-card" data-route="${esc(r.route || "")}"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(copy.mark)}</span>`
          + `<div class="ui-card__titles"><div class="ui-card__title">${esc(copy.title)}</div><span class="ui-card__status">${pill}</span></div></div>`
          + `<div class="ui-card__blurb">${esc(copy.what)}</div>`
          // Mission GG: five rows (head, blurb, body, action row, technical)
          // in an `.is-aligned` grid: every tile's action row at one level.
          + `<div class="ui-card__body"><ul class="ui-facts"><li class="first-run-engine"><b>${esc(provider)}</b> · <span class="ui-ellip" title="${esc(routeModel || "")}">${esc(routeModel || "")}</span></li>${viaDownload}<li class="ui-advanced">Route <code>${esc(r.route || "")}</code></li>${r.tier ? `<li class="ui-advanced">Chosen by memory: ${esc(r.tier)}</li>` : ""}</ul>`
          // AbstractCore's fit estimate doubts the pick: say so on the card
          // (the recommendation itself never switches model on its own).
          + (gpuLimitText(r) ? `<div class="ui-alert tone-info first-run-gpu-limit" role="note"><span>${esc(firstRunCap(gpuLimitText(r)))}</span></div>` : "")
          + (r.warning ? `<div class="ui-alert tone-warn" role="note"><span>${esc(r.warning)}</span></div>` : "")
          + engineMissingMarkup(r, "ui-alert tone-warn")
          + firstRunRouteUnavailableAlert(r.route)
          + body + `</div>`
          + `<div class="ui-card__actions">${cancel}${canDownload ? `<button class="ui-btn is-primary first-run-download" data-provider="${esc(r.provider)}" data-artifact="${esc(r.artifact)}">Download</button>` : ""}</div>`
          + `<div class="ui-card__tech"></div>`
          + `</article>`;
      }).join("") + firstRunUnavailableCards(rows);
      // "Download all" = N's ONE parent job (`grp_…`): its card (overall bar,
      // bytes/ETA, one row per model, Cancel per model + Cancel all) sits
      // above the per-model cards while it runs and after it ends.
      const group = dlFeed.group;
      const groupBox = group ? dlGroupMarkup(group) : "";
      const canDownloadAll = rows.some((r) => r.status === "absent" && !dlActive(state.downloadJobs.get(downloadJobKey(r.provider, r.artifact)))) && !dlActive(group);
      box.innerHTML = `<div class="ui-section-title"><h3>Recommended for this computer</h3><span class="ui-sub">${current ? `Text model now: <b>${esc(current)}</b>` : "No text model is set yet."}</span></div>`
        + groupBox
        + (cards ? `<div class="ui-card-grid is-fit is-aligned">${cards}</div>` : `<div class="ui-empty">This gateway reported no recommended downloads.</div>`)
        + `<div class="ui-toolbar"><button id="first-run-apply-recommended" class="ui-btn is-primary">Use recommended defaults</button>`
        + (canDownloadAll ? `<button id="first-run-download-all" class="ui-btn is-ghost">Download all</button>` : "")
        + `<span>Sets the recommended models for text, voice, transcription, images and video, where this computer can run them. Choices you already made are kept.</span>`
        + `<span class="ui-advanced">CLI: <code>abstractgateway-config defaults</code>, <code>abstractcore models download --recommended</code></span></div>`;
      $("first-run-apply-recommended").onclick = async () => {
        await applyRecommendedDefaults($("first-run-apply-recommended"), false);
        $("first-run-message").textContent = $("defaults-message").textContent;
        $("first-run-message").className = $("defaults-message").className;
        renderFirstRunModel();
      };
      const all = $("first-run-download-all");
      if (all) all.onclick = async () => {
        // One request for the whole recommended set: N's parent job (`group`)
        // is tracked as one card; its children (`jobs`) drive the per-model
        // cards. The request itself shows progress on the button.
        all.disabled = true;
        all.textContent = "Starting downloads...";
        try {
          const res = await api("/api/gateway/models/download", { slow: true, method: "POST", body: JSON.stringify({ recommended: true }) });
          if (!res || !res.group) throw new Error("The gateway started the downloads but returned no parent job (`group`); it needs the download-group contract (docs/model-downloads.md).");
          for (const job of (Array.isArray(res.jobs) ? res.jobs : [])) trackDownloadJob(job);
          trackDownloadJob(res.group);
          $("first-run-message").textContent = "";
          $("first-run-message").className = "message";
        } catch (err) {
          $("first-run-message").textContent = `Could not start the downloads: ${String(err.message || err)}`;
          $("first-run-message").className = "message error";
        }
        renderFirstRunModel();
      };
      if (typeof box.querySelectorAll === "function") {
        box.querySelectorAll(".ui-dl-cancel").forEach((b) => {
          b.onclick = () => dlCancel(b.dataset.dlCancel, b, b.dataset.dlStep);
        });
        box.querySelectorAll(".first-run-download").forEach((b) => {
          b.onclick = async () => {
            b.disabled = true;
            b.textContent = "Starting...";
            try {
              const res = await api("/api/gateway/models/download", { slow: true, method: "POST", body: JSON.stringify({ provider: b.dataset.provider, artifact: b.dataset.artifact }) });
              trackDownloadJob(res.job);
            } catch (err) {
              $("first-run-message").textContent = String(err.message || err);
              $("first-run-message").className = "message error";
            }
            renderFirstRunModel();
          };
        });
      }
    }
    async function firstRunCopy(text, btn) {
      const ok = await clipboardWrite(text);
      if (btn) {
        btn.textContent = ok ? "Copied" : COPY_FAILED;
        setTimeout(() => { btn.textContent = "Copy"; }, ok ? 1500 : 4000);
      }
    }
    // The address the person is using (behind `tailscale serve` that is the
    // https name, never the gateway machine's own loopback / LAN address from
    // the host snapshot): every same-gateway link is built from the page origin.
    function gatewayBaseUrl() {
      return String(location.origin);
    }
    function renderFirstRunApps() {
      // Install / Open / Start / Stop through the gateway's apps service
      // (routes/apps.py): cards with one primary action each, Node.js
      // installed for the user, the npx line only behind "Technical details".
      mountAppCards("first-run", $("first-run-apps-body"));
    }
    function renderFirstRunDone() {
      const box = $("first-run-done-body");
      const url = gatewayBaseUrl();
      const text = (state.defaults || []).find((r) => r && r.key === "output.text")
        || (state.defaults || []).find((r) => r && r.key === "input.text") || null;
      const model = text && text.provider && text.model ? `${state.providerLabels.get(text.provider) || text.provider} · ${text.model}` : "Not set yet";
      box.innerHTML = firstRunTiles([
        ["Console", `<code class="ui-ellip is-block" title="${esc(url)}/console">${esc(url)}/console</code>`, "Bookmark it: this is your gateway's home"],
        ["Default text model", `<span class="ui-ellip is-block" title="${esc(model)}">${esc(model)}</span>`, "Change it any time in Multimodal"],
        ["Starts at login", `<span id="first-run-login-text" class="ui-ellip is-block">…</span>`, `<button id="first-run-login-toggle" type="button" role="switch" class="af-switch af-switch--sm kv-switch hidden" aria-checked="false" aria-label="Start at login"><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Start at login</span></span></button>`],
        ["This guide", "Setup", "The button at the bottom of the sidebar reopens it"],
      ])
        + `<div class="ui-advanced"><div class="ui-section-title"><h3>From the command line</h3></div>`
        + firstRunKv([
          ["Sign in again", `<code>abstractgateway claim --open</code> <span class="subtle">(one-time link, from this machine)</span>`],
          ["Start at login", `<code>abstractgateway service enable</code> · <code>abstractgateway service disable</code>`],
          ["Status", `<code>abstractgateway-config status</code>`],
        ]) + `</div>`
        // Who can reach the gateway, with its addresses to copy: the same
        // panel as the Network tab.
        + `<section class="first-run-network" aria-label="Network"><div id="first-run-network" class="ui-net-root"></div></section>`
        // The console's own terminal app, said once, with the real way to
        // get it (crates.io only, so a command, never a button).
        + `<div id="first-run-console-tui"></div>`;
      mountNetworkPanel("first-run", $("first-run-network"));
      mountConsoleTuiNote($("first-run-console-tui"));
      $("first-run-login-toggle").onclick = () => toggleStartAtLogin("first-run");
      loadStartAtLogin("first-run");
    }
    async function refresh() {
      let me;
      try {
        me = await api("/api/gateway/me");
      } catch (err) {
        renderAccount(null);
        $("login-section").classList.remove("hidden");
        return;
      }
      renderAccount(me);
      $("login-section").classList.add("hidden");
      // The LANDING tab's lists load FIRST and in parallel (final-render
      // catch: they sat at the end of a six-round-trip sequential chain, so
      // the first painted screen still showed the pre-login error row).
      if (state.activeTab === "users") loadEntities();
      if (state.activeTab === "runtimes") loadRuntimes();  // data homes ride along inside loadRuntimes (cached)
      if (state.activeTab === "models") { loadHostState(); startHostStatePoll(); }
      if (state.activeTab === "catalog" || state.activeTab === "providers" || state.activeTab === "apps" || state.activeTab === "network") openCoreTab(state.activeTab);
      try {
        await loadEndpointProfiles();
      } catch (err) {
        const tbody = $("endpoint-profiles-table");
        tbody.textContent = "";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="empty">Provider connections could not be loaded: ${esc(String(err.message || err))}</td>`;
        tbody.append(tr);
      }
      try {
        await loadProviders();
      } catch (err) {
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
      try {
        const defaults = await api("/api/gateway/config/capability-defaults");
        await renderDefaults(defaults);
      } catch (err) {
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
      if (!me.principal?.admin) {
        try { await loadAccounts(); } catch (err) { usersMessage(emailErrorText(err), "error"); }
      }
      if (me.principal?.admin) {
        try {
          const users = await api("/api/gateway/admin/users");
          state.users = users.users || [];
          await loadAccounts();
          loadEmailCaps();
        } catch (err) {
          $("users-message").textContent = String(err.message || err);
          $("users-message").className = "message error";
        }
        try {
          const reservations = await api("/api/gateway/admin/runtime-reservations");
          renderRuntimeReservations(reservations.runtime_reservations || []);
        } catch (err) {
          $("reservations-message").textContent = String(err.message || err);
          $("reservations-message").className = "message error";
        }
        // (the admin branch loaded the Accounts table above)
        // No trailing loadRuns(): runs load through the
        // runtimes tab's selection flow — a non-runtimes tab never pays
        // for them, and the runtimes branch above already covers it.
      }
    }
    async function login() {
      signinShow("login-message", "");
      signinFieldError("login-user", "login-user-error", "");
      signinFieldError("login-token", "login-token-error", "");
      const user = $("login-user").value.trim();
      const token = $("login-token").value.trim();
      if (!user) { signinFieldError("login-user", "login-user-error", "Enter your gateway user."); return; }
      if (!token) { signinFieldError("login-token", "login-token-error", "Paste the token your gateway admin gave you."); return; }
      const btn = $("login-button");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "Signing in…";
      try {
        await api("/api/gateway/session/login", {
          method: "POST",
          body: JSON.stringify({ user_id: user, token, remember: $("login-remember").checked })
        });
        $("login-token").value = "";
        setLoginStatus(`Signed in as ${user}`, "ok");
        await refresh();
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403) && !/origin/i.test(String(err.message || ""))) {
          signinFieldError("login-token", "login-token-error", "This token was refused.");
          setLoginStatus("Token refused", "warn");
        } else if (err && !err.status) {
          signinShow("login-message", signinUnreachableText());
          setLoginStatus("Not signed in", "neutral");
        } else {
          signinShow("login-message", String((err && err.message) || err));
          setLoginStatus("Not signed in", "neutral");
        }
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        btn.textContent = "Sign in";
      }
    }
    // Sign-in by email (DESIGN §4): ONE quiet link requests a `sign_in` code;
    // the answer is honest (§4.1: sent to a masked address / no email address /
    // too many requests), then the code step replaces the link. `reset_token`
    // stays in the API for clients that use it; the signed-in user rotates
    // their token from their account page.
    const RECOVERY_COOLDOWN_S = 30;
    const recoveryState = { cooldownUntil: 0, timer: null, busy: false };
    async function loadRecoveryOptions() {
      let available = false;
      try {
        const out = await api("/api/gateway/session/recovery");
        available = Boolean(out && out.available);
      } catch { available = false; }
      $("recovery-section").hidden = !available || !$("recovery-code-step").hidden;
      state.recoveryAvailable = available;
    }
    function recoveryCooldownTick() {
      const resend = $("recovery-resend");
      const left = Math.ceil((recoveryState.cooldownUntil - Date.now()) / 1000);
      if (left > 0) {
        resend.disabled = true;
        resend.textContent = `Send a new code (in ${left} s)`;
        recoveryState.timer = setTimeout(recoveryCooldownTick, 1000);
      } else {
        resend.disabled = false;
        resend.textContent = "Send a new code";
        recoveryState.timer = null;
      }
    }
    function recoveryStartCooldown(seconds) {
      recoveryState.cooldownUntil = Date.now() + seconds * 1000;
      if (recoveryState.timer) clearTimeout(recoveryState.timer);
      recoveryCooldownTick();
    }
    function recoveryShowCodeStep(message) {
      $("recovery-section").hidden = true;
      $("recovery-code-step").hidden = false;
      $("recovery-sent-message").textContent = message;
      signinFieldError("recovery-code-input", "recovery-code-error", "");
      $("recovery-code-input").value = "";
      $("recovery-use").disabled = true;
      recoveryStartCooldown(RECOVERY_COOLDOWN_S);
      try { $("recovery-code-input").focus(); } catch {}
    }
    function recoveryBackToToken() {
      if (recoveryState.timer) clearTimeout(recoveryState.timer);
      recoveryState.timer = null;
      $("recovery-code-step").hidden = true;
      $("recovery-section").hidden = !state.recoveryAvailable;
      signinShow("recovery-request-message", "");
      const link = $("recovery-link");
      link.disabled = false;
      link.removeAttribute("aria-busy");
      link.textContent = "Forgot your token? Email me a sign-in code";
      try { $("login-token").focus(); } catch {}
    }
    async function requestRecoveryCode(fromResend = false) {
      if (recoveryState.busy) return;
      const user = $("login-user").value.trim();
      signinShow("recovery-request-message", "");
      if (!user) { signinFieldError("login-user", "login-user-error", "Enter your gateway user first."); return; }
      signinFieldError("login-user", "login-user-error", "");
      const control = fromResend ? $("recovery-resend") : $("recovery-link");
      recoveryState.busy = true;
      control.disabled = true;
      control.setAttribute("aria-busy", "true");
      control.textContent = "Sending…";
      let out = null;
      let failure = "";
      try {
        out = await api("/api/gateway/session/recovery/request", { method: "POST", body: JSON.stringify({ user_id: user, purpose: "sign_in" }) });
      } catch (e) {
        if (e && e.status === 404) { state.recoveryAvailable = false; failure = "Sign-in by email is off on this gateway. Ask your gateway admin for a token."; }
        else if (e && !e.status) failure = signinUnreachableText();
        else failure = String((e && e.message) || e);
      } finally {
        recoveryState.busy = false;
        control.removeAttribute("aria-busy");
      }
      if (out && out.sent) {
        if (!fromResend) { control.disabled = false; control.textContent = "Forgot your token? Email me a sign-in code"; }
        recoveryShowCodeStep(String(out.message || `A sign-in code is on its way to ${out.to || "your email address"}.`));
        return;
      }
      const text = failure || String((out && out.message) || "A code could not be sent. Ask your gateway admin for a token.");
      if (fromResend) {
        $("recovery-sent-message").textContent = "";
        signinFieldError("recovery-code-input", "recovery-code-error", text);
        const retry = Number(out && out.retry_after_s) || RECOVERY_COOLDOWN_S;
        recoveryStartCooldown(retry);
      } else {
        control.disabled = false;
        control.textContent = "Forgot your token? Email me a sign-in code";
        signinShow("recovery-request-message", text);
      }
    }
    function recoveryCodeInput() {
      const input = $("recovery-code-input");
      const digits = String(input.value || "").replace(/[^0-9]/g, "").slice(0, 8);
      if (digits !== input.value) input.value = digits;
      $("recovery-use").disabled = digits.length !== 8;
      signinFieldError("recovery-code-input", "recovery-code-error", "");
    }
    async function useRecoveryCode() {
      const user = $("login-user").value.trim();
      const code = String($("recovery-code-input").value || "").trim();
      if (!user || code.length !== 8) return;
      const btn = $("recovery-use");
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "Signing in…";
      try {
        await api("/api/gateway/session/recovery/redeem", {
          method: "POST",
          body: JSON.stringify({ user_id: user, purpose: "sign_in", code, remember: $("login-remember").checked }),
        });
        $("recovery-code-input").value = "";
        recoveryBackToToken();
        setLoginStatus(`Signed in as ${user}`, "ok");
        await refresh();
      } catch (e) {
        const text = e && e.status === 401
          ? "That code is wrong, expired or already used. Send a new one."
          : (e && !e.status ? signinUnreachableText() : String((e && e.message) || e));
        signinFieldError("recovery-code-input", "recovery-code-error", text);
        btn.disabled = false;
      } finally {
        btn.removeAttribute("aria-busy");
        btn.textContent = "Use code";
      }
    }
    async function signOut() {
      try { await api("/api/gateway/session/logout", { method: "POST" }); } catch {}
      location.reload();
    }
    async function createUser() {
      // Runs inside the create-user modal. The role is a SELECT over the
      // accepted vocabulary (admin/user/readonly — entity is door-assigned,
      // never pickable); the issued token REPLACES the form on success so
      // its one showing cannot be lost behind a closed dialog.
      $("user-create-message").textContent = "";
      const payload = {
        tenant_id: $("new-tenant").value.trim() || "default",
        user_id: $("new-user").value.trim(),
        email: $("new-email").value.trim() || null,
        runtime_id: $("new-runtime").value.trim() || null,
        roles: [$("new-roles").value || "user"],
      };
      if (!payload.user_id) {
        $("user-create-message").textContent = "Enter a User ID.";
        $("user-create-message").className = "message error";
        return;
      }
      try {
        const res = await api("/api/gateway/admin/users", { method: "POST", body: JSON.stringify(payload) });
        renderIssuedToken($("user-create-token"), res.user.tenant_id && res.user.tenant_id !== "default" ? `${res.user.tenant_id}/${res.user.user_id}` : res.user.user_id, res.token);
        $("user-create-form").classList.add("hidden");
        $("user-create-done").classList.remove("hidden");
        $("new-user").value = "";
        $("new-email").value = "";
        $("new-runtime").value = "";
        await refresh();
      } catch (err) {
        $("user-create-message").textContent = String(err.message || err);
        $("user-create-message").className = "message error";
      }
    }
    async function updateUser(u, payload) {
      await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}?tenant_id=${encodeURIComponent(u.tenant_id)}`, { method: "PATCH", body: JSON.stringify(payload) });
      await refresh();
    }
    function renderIssuedToken(el, who, token) {
      // Structured one-time token render with a copy affordance (flat
      // text forced manual selection of a 40-char token).
      el.textContent = "";
      const text = document.createElement("span");
      text.textContent = `Give this token to ${who}. It is shown once. `;
      const code = document.createElement("code");
      code.textContent = token;
      const copy = document.createElement("button");
      copy.className = "secondary";
      copy.innerHTML = `<span class="button-icon" aria-hidden="true">⧉</span><span>Copy</span>`;
      copy.title = "Copy the token to the clipboard — it is shown once";
      copy.setAttribute("aria-label", "Copy token");
      copy.onclick = async () => {
        // DESIGN §11: over plain http the Clipboard API is withheld; clipboardWrite
        // falls back to execCommand, and a copy that still fails says so.
        const ok = await clipboardWrite(token);
        copy.innerHTML = ok
          ? `<span class="button-icon" aria-hidden="true">✓</span><span>Copied</span>`
          : `<span>${COPY_FAILED}</span>`;
      };
      const note = document.createElement("span");
      note.className = "muted";
      el.append(text, code, copy, note);
      el.classList.remove("hidden");
    }
    async function rotateUser(u) {
      // Rotation invalidates the live token — never one silent click.
      const ok = await confirmAction({
        title: `Rotate token for ${u.user_id}?`,
        message: "The current bearer token stops working immediately; anything signed in with it is disconnected. The new token is shown once.",
        confirmLabel: "Rotate token",
      });
      if (!ok) return;
      const res = await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}?tenant_id=${encodeURIComponent(u.tenant_id)}`, { method: "PATCH", body: JSON.stringify({ rotate_token: true }) });
      renderIssuedToken($("issued-token"), `${res.user.tenant_id}/${res.user.user_id}`, res.token);
      await refresh();
    }
    async function transferRuntimeReservation(r, targetUserId) {
      $("reservations-message").textContent = "";
      if (!targetUserId) {
        $("reservations-message").textContent = "Select a target user before transferring.";
        $("reservations-message").className = "message error";
        return;
      }
      const ok = await confirmAction({
        title: "Transfer retained runtime",
        message: `Transfer retained runtime ${r.tenant_id}/${r.runtime_id} to ${targetUserId}? The target user will inherit this runtime data, and their previous runtime id will be reserved.`,
        confirmLabel: "Transfer runtime",
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/gateway/admin/runtime-reservations/${encodeURIComponent(r.runtime_id)}/transfer`, {
          method: "POST",
          body: JSON.stringify({ tenant_id: r.tenant_id, target_user_id: targetUserId, confirm_runtime_id: r.runtime_id })
        });
        $("reservations-message").textContent = "Runtime transferred.";
        $("reservations-message").className = "message ok";
        await refresh();
      } catch (err) {
        $("reservations-message").textContent = String(err.message || err);
        $("reservations-message").className = "message error";
      }
    }
    async function purgeRuntimeReservation(r) {
      $("reservations-message").textContent = "";
      const ok = await confirmAction({
        title: "Purge retained runtime",
        message: `Permanently delete retained runtime data for ${r.tenant_id}/${r.runtime_id} and release the runtime id? This cannot be undone.`,
        confirmLabel: "Purge runtime",
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/gateway/admin/runtime-reservations/${encodeURIComponent(r.runtime_id)}/purge`, {
          method: "POST",
          body: JSON.stringify({ tenant_id: r.tenant_id, confirm_runtime_id: r.runtime_id, delete_data: true })
        });
        $("reservations-message").textContent = "Runtime purged.";
        $("reservations-message").className = "message ok";
        await refresh();
      } catch (err) {
        $("reservations-message").textContent = String(err.message || err);
        $("reservations-message").className = "message error";
      }
    }
	    function voiceTestRunId() {
	      const p = state.principal || {};
	      return `session_memory_gateway_console_voicetest_${sessionMemoryIdPart(p.tenant_id, "default")}_${sessionMemoryIdPart(p.user_id || p.runtime_id, "user")}`;
	    }
	    async function testDefault() {
	      // Single-flight: the button disables while one test runs — repeated
	      // clicks against a wedged backend would pile stranded children
	      // (TTS backends have wedged before; the request carries timeout_s so a
	      // wedge fails fast with the watchdog's honest 504 instead of
	      // hanging this modal).
	      const row = state.activeDefaultRow;
	      if (!row) return;
	      const button = $("test-default");
	      if (button.disabled) return;
	      const target = $("default-modal-test");
	      const provider = $("modal-default-provider").value;
	      const model = $("modal-default-model").value;
	      if (!provider || !model) {
	        target.textContent = "Select a provider and model first.";
	        return;
	      }
	      const key = defaultRowKey(row);
	      button.disabled = true;
	      target.textContent = key === "output.voice" ? "Synthesizing… (up to 25s)" : "Testing…";
	      const started = Date.now();
	      try {
	        if (key === "output.voice") {
	          const body = {
	            text: "Hello — this is the voice you selected, speaking from the gateway.",
	            provider,
	            model,
	            request_id: sandboxRequestId(),
	            timeout_s: 25,
	          };
	          // Empty voice = audition the provider's DEFAULT voice — exactly
	          // what saving without a voice would produce.
	          const voice = $("modal-default-voice").value;
	          if (voice) body.voice = voice;
	          const runId = voiceTestRunId();
	          const res = await api(`/api/gateway/runs/${encodeURIComponent(runId)}/voice/tts`, { slow: true, method: "POST", body: JSON.stringify(body) });
	          const sec = ((Date.now() - started) / 1000).toFixed(1);
	          target.textContent = "";
	          const line = document.createElement("div");
	          line.className = "message ok";
	          line.textContent = `Synthesized in ${sec}s with ${provider}/${model}${voice ? "/" + voice : " (provider default voice)"}. Note: agents pick up SAVED voice defaults per lane — this proves the selection itself works.`;
	          target.append(line);
	          renderSandboxArtifact(target, { runId, ref: res.audio_artifact, mode: "audio", label: "Test audio" });
	        } else {
	          const probe = { capability: key, provider, model, prompt: "Reply with the single word: ready." };
	          const choice = $("modal-default-speculation").value;
	          if (choice) probe.speculation = speculationFromChoice(choice, row?.options?.speculation, true);
	          const reasoning = $("modal-default-reasoning").value;
	          if (reasoning) probe.reasoning = reasoning;
	          const res = await api("/api/gateway/sandbox/generate", {
	            slow: true,
	            method: "POST",
	            // No max_tokens: the PROMPT bounds this probe, not a cap. The old
	            // max_tokens:16 truncated any model that emits a preamble or
	            // reasoning tokens, so a healthy capability reported "(empty
	            // response)" — a cap manufacturing a false failure on the very
	            // screen an operator uses to decide whether a model works.
	            body: JSON.stringify(probe),
	          });
	          const sec = ((Date.now() - started) / 1000).toFixed(1);
	          target.textContent = "";
	          const line = document.createElement("div");
	          line.className = "message ok";
	          line.textContent = `Responded in ${sec}s with ${provider}/${model}: ${String(res.response || "").slice(0, 120) || "(empty response)"} · ${speculationSummary(res)}`;
	          target.append(line);
	        }
	      } catch (err) {
	        target.textContent = "";
	        const line = document.createElement("div");
	        line.className = "message error";
	        line.textContent = `Test failed: ${String(err.message || err)}`;
	        target.append(line);
	      } finally {
	        button.disabled = false;
	      }
	    }
	    async function saveDefault() {
	      $("default-modal-message").textContent = "";
	      try {
	        const row = state.activeDefaultRow;
	        if (!row) throw new Error("No capability route selected.");
	        const provider = activeDefaultProvider();
	        const model = activeDefaultModel();
	        if (!provider || !model) throw new Error("Pick a provider and a model — type the model id if discovery could not reach the provider.");
	        const { kind, modality, task } = defaultRowKindModality(row);
	        const taskPath = task ? `/${encodeURIComponent(task)}` : "";
	        // THE SAVE SENDS WHAT THIS MODAL OWNS, AND NOTHING ELSE. A field with
	        // no control here is left unset so the store keeps its value; echoing
	        // back what the grid last rendered would let a stale row overwrite a
	        // setting made from `abstractcore config` between render and save.
	        const body = { provider, model };
	        // Reasoning is sent only for text routes, and always explicitly:
	        // "" clears the stored effort, a value sets it.
	        if (isTextGenerationDefault(row)) body.reasoning = $("modal-default-reasoning").value || "";
	        // BASE URL AND OPTIONS TRAVEL ONLY WHEN THE OPERATOR EDITED THEM.
	        // Both are prefilled from a possibly-minutes-old grid render, so
	        // naming them unconditionally re-created the rollback this doctrine
	        // forbids (see openDefaultModal's prefill note). Comparing against
	        // what was shown keeps BOTH halves: an untouched field is not named,
	        // so the store keeps it; a field the operator emptied IS named, as
	        // "", because "" differs from what they were shown — which is how an
	        // override gets cleared.
	        const prefill = state.defaultModalPrefill || {};
	        const baseUrlText = $("modal-default-base-url").value.trim();
	        if (baseUrlText !== String(prefill.base_url || "").trim()) body.base_url = baseUrlText;
	        // Raw options. A typo must NOT be swallowed — silently dropping an
	        // unparseable object would look like a successful save that quietly
	        // discarded the operator's settings. Validated whenever there is
	        // text, edited or not, so a save can never carry one downstream.
	        const optionsText = $("modal-default-options").value.trim();
	        let options = {};
	        if (optionsText) {
	          try {
	            options = JSON.parse(optionsText);
	          } catch (parseErr) {
	            throw new Error(`Options is not valid JSON: ${parseErr.message}`);
	          }
	          if (!options || typeof options !== "object" || Array.isArray(options)) {
	            throw new Error('Options must be a JSON object, e.g. {"temperature": 0.7}.');
	          }
	        }
	        let optionsEdited = optionsText !== String(prefill.options || "").trim();
	        // The voice picker edits the same dict, and it is the more specific
	        // control, so it wins for the keys it owns — but only WHILE IT IS
	        // ONE. It is disabled whenever the catalog is empty or the probe
	        // failed, and a disabled control expresses no operator intent:
	        // reading "" off it then deleted a voice nobody cleared, so an
	        // offline save that only meant to set a base URL silently unset a
	        // working route's voice. Those keys are hidden from the JSON box
	        // above, so nothing else would have carried them.
	        if (isVoiceOutputDefault(row)) {
	          const voiceSelect = $("modal-default-voice");
	          if (voiceSelect.disabled) {
	            const stored = defaultVoiceValue(row);
	            if (stored) options.voice = stored;
	          } else {
	            delete options.voice;
	            delete options.profile;
	            const voice = voiceSelect.value;
	            if (voice) options.voice = voice;
	            // The picker writes INTO this dict, so a changed pick makes the
	            // dict dirty even when the JSON box was never touched.
	            if (voice !== String(prefill.voice || "")) optionsEdited = true;
	          }
	        }
	        if (isTextGenerationDefault(row)) {
	          if (Object.prototype.hasOwnProperty.call(options, "speculation")) throw new Error("Use the MTP selector for speculation; the options box edits the remaining provider settings.");
	          const choice = $("modal-default-speculation").value;
	          const changed = choice !== String(prefill.speculation || "");
	          const value = changed ? speculationFromChoice(choice, row?.options?.speculation) : row?.options?.speculation;
	          delete options.speculation;
	          if (value !== undefined && value !== null) options.speculation = value;
	          if (changed) optionsEdited = true;
	        }
	        if (optionsEdited) body.options = options;
	        // Slow lane: see apply-recommended. A 60s abort on a PUT is
	        // write-ambiguity — the save may already have landed in the Core store.
	        await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}${taskPath}`, {
	          slow: true,
	          method: "PUT",
	          body: JSON.stringify(body)
	        });
	        $("defaults-message").textContent = "Saved.";
	        $("defaults-message").className = "message ok";
	        closeDefaultModal();
	        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	      } catch (err) {
	        $("default-modal-message").textContent = String(err.message || err);
	        $("default-modal-message").className = "message error";
	      }
	    }
	    async function clearDefault(row = null) {
	      const target = row || state.activeDefaultRow;
	      if (!target) return;
	      const { kind, modality, task } = defaultRowKindModality(target);
	      const taskPath = task ? `/${encodeURIComponent(task)}` : "";
	      // Slow lane: a clear is a write too — same ambiguity on abort.
	      await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}${taskPath}`, { slow: true, method: "DELETE" });
	      if (!row) closeDefaultModal();
	      await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	    }
    $("login-form").onsubmit = (event) => { event.preventDefault(); login(); };
    $("recovery-link").onclick = () => requestRecoveryCode(false);
    $("recovery-resend").onclick = () => requestRecoveryCode(true);
    $("recovery-back").onclick = recoveryBackToToken;
    $("recovery-use").onclick = useRecoveryCode;
    $("recovery-code-input").oninput = recoveryCodeInput;
    $("recovery-code-input").onkeydown = (event) => { if (event && event.key === "Enter" && !$("recovery-use").disabled) useRecoveryCode(); };
    loadRecoveryOptions();
	    $("toggle-token").onclick = () => {
	      const input = $("login-token");
	      const visible = input.type === "text";
	      input.type = visible ? "password" : "text";
	      $("toggle-token").textContent = visible ? "Show" : "Hide";
	      $("toggle-token").setAttribute("aria-label", visible ? "Show token" : "Hide token");
	      $("toggle-token").setAttribute("aria-pressed", visible ? "false" : "true");
	    };
	    $("sign-out").onclick = signOut;
    $("confirm-cancel").onclick = () => finishConfirm(false);
	    $("confirm-ok").onclick = () => finishConfirm(true);
	    $("confirm-backdrop").onclick = (event) => { if (event.target === $("confirm-backdrop")) finishConfirm(false); };
	    $("open-appearance").onclick = openAppearance;
	    $("appearance-close").onclick = closeAppearance;
	    $("open-assistant").onclick = () => toggleAssistant();
	    $("assistant-close").onclick = () => toggleAssistant(false);
	    $("assistant-clear").onclick = assistantClear;
	    $("assistant-form").onsubmit = assistantSubmit;
	    $("assistant-input").onkeydown = (event) => {
	      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); assistantSubmit(); }
	    };
	    $("appearance-backdrop").onclick = (event) => { if (event.target === $("appearance-backdrop")) closeAppearance(); };
	    $("appearance-theme").onchange = updateAppearanceFromForm;
	    $("appearance-font-size").onchange = updateAppearanceFromForm;
	    $("appearance-header-size").onchange = updateAppearanceFromForm;
	    $("tab-button-users").onclick = () => { setActiveTab("users"); loadEntities(); loadMyEmail(); };
	    // No direct loadRuns() on tab open: loadRuntimes'
	    // selection restore auto-selects a runtime (default first) and its
	    // Runs tab loader fires from there. Cache sizes load when the Cache
	    // tab opens (shared ensureDataHomes cache).
	    $("tab-button-runtimes").onclick = () => { setActiveTab("runtimes"); loadRuntimes(); };
	    bindSkillsMcpPage();
	    $("tab-button-workflows").onclick = () => { setActiveTab("workflows"); mountWorkflowSwitches(); loadWorkflows(); mountAgentDefaults("workflows", $("agent-defaults-root")); };
	    $("workflows-refresh").onclick = () => loadWorkflows();
	    $("workflows-search").oninput = () => renderWorkflows();
	    $("workflows-import").onclick = () => $("workflows-import-file").click();
	    $("workflows-import-file").onchange = async (ev) => {
	      const files = ev.target.files;
	      await importWorkflows(files);
	      ev.target.value = "";
	    };
	    $("tab-button-providers").onclick = () => { setActiveTab("providers"); openCoreTab("providers"); };
	    $("tab-button-defaults").onclick = () => setActiveTab("defaults");
	    $("tab-button-sandbox").onclick = () => setActiveTab("sandbox");
	    // Models: load now, then start the tab-scoped 5s poll (token-guarded —
	    // the chain dies the moment another tab goes active or the user signs
	    // out; re-entering the tab starts a fresh chain).
	    $("tab-button-models").onclick = () => { setActiveTab("models"); loadHostState(); startHostStatePoll(); };
	    $("tab-button-catalog").onclick = () => { setActiveTab("catalog"); openCoreTab("catalog"); };
	    $("tab-button-apps").onclick = () => { setActiveTab("apps"); openCoreTab("apps"); };
	    $("tab-button-network").onclick = () => { setActiveTab("network"); openCoreTab("network"); };
	    $("models-refresh").onclick = () => { loadHostState(); startHostStatePoll(); };
	    $("gateway-host-pause").onclick = toggleGatewayPause;
	    $("gateway-host-restart").onclick = restartGateway;
	    $("gateway-host-login-toggle").onclick = () => toggleStartAtLogin("gateway-host");
	    $("gateway-host-quit").onclick = quitGateway;
	    $("gateway-host-update-check").onclick = checkGatewayUpdate;
	    $("gateway-host-update-start").onclick = startGatewayUpdate;
	    $("paused-banner-resume").onclick = async () => {
	      try { const out = await api("/api/gateway/host/resume", { method: "POST", body: JSON.stringify({}) }); renderGatewayHost(out, null); }
	      catch (e) { _gwMsg(String(e.message || e), "error"); }
	    };
	    // Repaint from held state — the toggle is a pure view filter, no fetch.
	    $("models-show-cached").onchange = () => { state.modelsShowCached = $("models-show-cached").checked; if (state.hostState) renderModelsTable(state.hostState); };
	    $("models-load-button").onclick = loadModelResidency;
	    // onchange, not oninput (the provider-modal precedent): commits on
	    // blur/Enter, so an unreachable provider is probed once per commit.
	    // Provider change auto-refreshes the model list for that provider (the
	    // cascade the operator asked for); the custom lanes drive the same
	    // cascade so an offline operator gets identical behaviour.
	    $("models-load-provider").onchange = () => { void syncModelsLoadModelOptions().then(updateModelsLoadHint); };
	    $("models-load-provider-custom").onchange = () => { void syncModelsLoadModelOptions().then(updateModelsLoadHint); };
	    $("models-load-model").onchange = () => updateModelsLoadHint();
	    $("models-load-model-custom").onchange = () => updateModelsLoadHint();
	    $("entity-create").onclick = createEntity;
	    $("entity-template").onchange = renderEntityTemplateDesc;
	    $("entity-new-provider").onchange = () => loadModelsForProvider($("entity-new-provider").value);
	    $("tpl-view").onclick = () => tplShowEditor("view");
	    $("tpl-edit").onclick = () => tplShowEditor("edit");
	    $("tpl-new").onclick = () => tplShowEditor("new");
	    $("tpl-save").onclick = tplSave;
	    $("tpl-cancel").onclick = () => { $("tpl-editor").classList.add("hidden"); state.tplMode = ""; };
	    $("entities-refresh").onclick = () => {
	      // Explicit refresh honors the click fully: templates + matrix spec are
	      // re-fetched too (a new operator template or inventory change lands
	      // without a page reload).
	      state.entityTemplates = [];
	      state.entityMatrixSpec = null;
	      return loadEntities();
	    };
	    $("entity-manage-close").onclick = closeEntityManage;
	    for (const sub of ENTITY_SUBTABS) {
	      $(`entity-subtab-${sub}`).onclick = () => setEntitySubtab(sub);
	    }
	    $("entity-state-awake").onclick = () => setEntityState("awake");
	    $("entity-state-asleep").onclick = () => setEntityState("asleep");
	    // The STOP act rides the engraved paused verb (at-rest unchanged, c1559);
	    // Restore = the existing wake verb, lands awake unconditionally.
	    $("entity-stop").onclick = () => setEntityState("paused");
	    $("entity-restore").onclick = () => setEntityState("awake");
	    $("entity-owntime-toggle").onclick = entityOwntimeToggle;
	    $("entity-loop-freeze").onclick = entityLoopFreeze;
	    $("entity-substrate-save").onclick = entitySubstrateSave;
	    $("entity-voice-save").onclick = entityVoiceSave;
	    $("entity-voice-clear").onclick = entityVoiceClear;
	    $("entity-workorder-save").onclick = entityWorkOrderSave;
	    $("entity-workorder-clear").onclick = entityWorkOrderClear;
	    $("entity-voice-audition").onclick = entityVoiceAudition;
	    $("entity-voice-provider").onchange = () => loadEntityVoiceModels($("entity-voice-provider").value, "", "");
	    $("entity-voice-model").onchange = () => {
	      const p = $("entity-voice-provider").value;
	      const m = $("entity-voice-model").value;
	      if (p && m) loadEntityVoiceVoices(p, m, "");
	    };
	    $("entity-tools-save").onclick = entityToolsSave;
	    $("entity-prompt-save").onclick = entityPromptSave;
	    $("entity-reembed").onclick = entityReembed;
	    $("entity-verify").onclick = entityVerify;
	    $("runs-status").onchange = () => { state.runsOffset = 0; loadRuns(); };
	    $("runs-root-only").onchange = () => { state.runsOffset = 0; loadRuns(); };
	    {
	      let _runsSearchTimer = null;
	      $("runs-search").oninput = () => {
	        clearTimeout(_runsSearchTimer);
	        _runsSearchTimer = setTimeout(() => { state.runsOffset = 0; loadRuns(); }, 400);
	      };
	      $("runs-search").onkeydown = (ev) => {
	        if (ev.key === "Enter") { clearTimeout(_runsSearchTimer); state.runsOffset = 0; loadRuns(); }
	      };
	    }
	    $("entity-chat-open").onclick = entityChatOpen;
	    $("entity-chat-send").onclick = entityChatSend;
	    $("entity-chat-close").onclick = entityChatClose;
	    $("entity-chat-input").onkeydown = (ev) => {
	      if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); entityChatSend(); }
	    };
	    $("create-user").onclick = createUser;
	    $("open-create-user").onclick = openUserCreate;
	    $("create-user-cancel").onclick = closeUserCreate;
	    $("user-create-close").onclick = closeUserCreate;
	    $("user-create-backdrop").onclick = (event) => { if (event.target === $("user-create-backdrop")) closeUserCreate(); };
	    $("open-create-entity").onclick = openEntityCreate;
	    $("accounts-create-entity").onclick = openEntityCreate;
	    $("account-email-close").onclick = closeAccountEmail;
	    $("account-logs-close").onclick = closeAccountLogs;
	    $("entity-create-cancel").onclick = closeEntityCreate;
	    $("entity-create-backdrop").onclick = (event) => { if (event.target === $("entity-create-backdrop")) closeEntityCreate(); };
	    $("open-templates").onclick = openTemplates;
	    $("templates-close").onclick = closeTemplates;
	    $("templates-backdrop").onclick = (event) => { if (event.target === $("templates-backdrop")) closeTemplates(); };
	    $("tpl-select").onchange = renderTplSelectState;
	    $("runtimes-refresh").onclick = loadRuntimes;
		    $("my-workspace-policy-section").ontoggle = () => {
	      if ($("my-workspace-policy-section").open && !state.myWorkspacePolicy) loadMyWorkspacePolicy();
	    };
	    $("my-workspace-policy-refresh").onclick = loadMyWorkspacePolicy;
	    $("my-workspace-policy-save").onclick = () => saveMyWorkspacePolicy(false);
	    $("my-workspace-policy-clear").onclick = () => saveMyWorkspacePolicy(true);
    bindMyEmail();
    bindEmailCaps();
	    $("wsp-cancel").onclick = closeWorkspacePolicyModal;
	    $("wsp-save").onclick = () => saveWorkspacePolicyModal(false);
	    $("wsp-reset").onclick = () => saveWorkspacePolicyModal(true);
	    $("workspace-policy-modal-backdrop").onclick = (event) => {
	      if (event.target === $("workspace-policy-modal-backdrop")) closeWorkspacePolicyModal();
	    };
	    // Guarded: the login-JS test harness stubs `document` without
	    // querySelectorAll; browsers always have it.
	    if (typeof document.querySelectorAll === "function") {
	      for (const card of document.querySelectorAll("#wsp-mode-cards input")) {
	        card.onchange = () => _wspSetMode(card.value);
	      }
	    }
	    // Detail refresh re-runs the ACTIVE subtab's loader; on Caches it
	    // forces a registry re-walk (the cached homes are the point of the
	    // shared ensureDataHomes, so only an explicit refresh pays sizes).
	    $("runtime-detail-refresh").onclick = () => {
	      if (!state.selectedRuntime) return;
	      state.runtimeDrill = null;
	      if (state.runtimeSubtab === "caches") { loadRuntimeCaches(true); return; }
	      if (state.runtimeSubtab === "artifacts") { loadRuntimeArtifacts(); return; }
	      if (state.runtimeSubtab === "logs") { loadRuntimeLogs(); return; }
	      if (state.runtimeSubtab === "sessions") {
	        // Refresh in place: the old per-tab refresh preserved the page, and a
	        // refresh that silently jumps to page 1 loses the operator's spot.
	        if (state.selectedRuntime.kind === "default") loadRuns();
	        else loadRuntimeRuns();
	        return;
	      }
	      openRuntimeSubtab(state.runtimeSubtab || "sessions");
	    };
	    for (const _st of RUNTIME_SUBTABS) {
	      $("runtime-subtab-" + _st).onclick = () => openRuntimeSubtab(_st);
	    }
	    $("runtime-artifacts-modality").onchange = () => { state.artifactsOffset = 0; loadRuntimeArtifacts(); };
	    {
	      // Debounced search + Enter fires immediately (both guarded by the
	      // per-call sequence inside loadRuntimeArtifacts).
	      let _artSearchTimer = null;
	      $("runtime-artifacts-search").oninput = () => {
	        clearTimeout(_artSearchTimer);
	        _artSearchTimer = setTimeout(() => { state.artifactsOffset = 0; loadRuntimeArtifacts(); }, 400);
	      };
	      $("runtime-artifacts-search").onkeydown = (ev) => {
	        if (ev.key === "Enter") { clearTimeout(_artSearchTimer); state.artifactsOffset = 0; loadRuntimeArtifacts(); }
	      };
	    }
	    {
	      const rerenderCaches = () => { if (state.cachesRerender) state.cachesRerender(); };
	      const rerenderLogs = () => {
	        if (state.logsLast) paintLogRows(state.logsLast.liveHomes, state.logsLast.wantDefault);
	      };
	      const debounced = (fn) => {
	        let t = null;
	        return () => { clearTimeout(t); t = setTimeout(fn, 400); };
	      };
	      $("runtime-caches-kind").onchange = rerenderCaches;
	      $("runtime-caches-search").oninput = debounced(rerenderCaches);
	      $("runtime-caches-search").onkeydown = (ev) => { if (ev.key === "Enter") rerenderCaches(); };
	      $("runtime-logs-home").onchange = rerenderLogs;
	      $("runtime-logs-search").oninput = debounced(rerenderLogs);
	      $("runtime-logs-search").onkeydown = (ev) => { if (ev.key === "Enter") rerenderLogs(); };
	    }
	    $("artifact-modal-close").onclick = () => closeArtifactModal();
	    $("run-modal-close").onclick = () => closeRunModal();
	    $("log-modal-refresh").onclick = () => {
	      if (state.currentLog) viewLogFile(state.currentLog.home, state.currentLog.file);
	    };
	    $("log-modal-tail-size").onchange = () => {
	      if (state.currentLog) viewLogFile(state.currentLog.home, state.currentLog.file);
	    };
	    $("log-modal-close").onclick = closeLogModal;
	    // Backdrop-click close is installed per-open by _openModal (with the
	    // double-click grace) — no boot-time handler here.
	    $("refresh-catalog").onclick = async () => {
	      state.providerModels.clear();
	      await loadProviders();
	      await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	    };
	    // A STANDING action, not a banner ornament. Same vocabulary as
	    // `abstractcore config apply-recommended` and as the console-TUI's `a`:
	    // a route the operator configured differently is KEPT and reported,
	    // never silently replaced — replacing it takes a second, explicit click.
	    $("defaults-apply-recommended").onclick = () =>
	      applyRecommendedDefaults($("defaults-apply-recommended"), false);
	    $("close-default-modal").onclick = closeDefaultModal;
	    $("default-modal-backdrop").onclick = (event) => { if (event.target === $("default-modal-backdrop")) closeDefaultModal(); };
	    // ONE handler for BOTH provider controls. The free-text lane is a real
	    // provider control, not a note to the save: typing into it has to reload
	    // the model and voice catalogs exactly as picking from the select does,
	    // or the lane silently does nothing until Save. `onchange` rather than
	    // `oninput` — it commits on blur/Enter, so an unreachable provider is
	    // probed once instead of once per keystroke.
	    $("modal-default-provider").onchange = reloadDefaultModalCatalogs;
	    $("modal-default-provider-custom").onchange = reloadDefaultModalCatalogs;
	    $("modal-default-model").onchange = () => {
	      clearDefaultTest();
	      refreshDefaultSpeculationSupport();
	      return loadDefaultVoices(
	        activeDefaultProvider(),
	        activeDefaultModel(),
	        "",
	        state.activeDefaultRow || null
	      );
	    };
	    // The typed model is a model choice too, so the voice catalog follows it.
	    $("modal-default-model-custom").onchange = $("modal-default-model").onchange;
	    $("modal-default-voice").onchange = () => clearDefaultTest();
	    $("modal-default-speculation").onchange = () => clearDefaultTest();
	    $("save-default").onclick = saveDefault;
	    $("test-default").onclick = testDefault;
	    $("clear-default").onclick = () => clearDefault();
	    $("sandbox-capability").onchange = updateSandboxControls;
	    $("sandbox-provider").onchange = () => loadSandboxModels();
	    $("sandbox-run").onclick = runSandbox;
	    $("sandbox-prompt").onkeydown = (event) => {
	      if (event.key === "Enter" && !event.shiftKey) {
	        event.preventDefault();
	        if (!$("sandbox-run").disabled) runSandbox();
	      }
	    };
	    $("sandbox-clear").onclick = clearSandbox;
	    $("sandbox-attach").onclick = () => $("sandbox-file-input").click();
	    $("sandbox-file-input").onchange = (event) => handleSandboxFiles(event?.target?.files || []);
	    $("sandbox-dropzone").ondragover = (event) => { event.preventDefault(); $("sandbox-dropzone").classList.add("dragover"); };
	    $("sandbox-dropzone").ondragleave = () => $("sandbox-dropzone").classList.remove("dragover");
	    $("sandbox-dropzone").ondrop = (event) => {
	      event.preventDefault();
	      $("sandbox-dropzone").classList.remove("dragover");
	      handleSandboxFiles(event?.dataTransfer?.files || []);
	    };
    $("save-endpoint-profile").onclick = saveEndpointProfile;
    $("cancel-endpoint-profile").onclick = closeEndpointModal;
    $("provider-modal-backdrop").onclick = (event) => { if (event.target === $("provider-modal-backdrop")) closeEndpointModal(); };
	    $("endpoint-provider-family").onchange = handleEndpointFamilyChange;
	    $("discover-endpoint-models").onclick = discoverEndpointModels;
	    $("clear-endpoint-models").onclick = clearEndpointModelAllowlist;
	    $("endpoint-models").onchange = updateEndpointModelSummary;
	    state.appearance = loadAppearanceSettings();
	    initAppearanceControls();
	    applyAppearanceSettings();
	    mountConsoleIslands();
	    uiInitAdvanced();
	    uiInitLayout();
	    state.activeTab = readStringSetting(ACTIVE_TAB_KEY, "users");
	    setActiveTab(state.activeTab);
	    // The users tab hosts the entities surface — restoring onto it (or
	    // folding a legacy "entities" value onto it) must populate the list.
	    if (state.activeTab === "users") loadEntities();
	    initEndpointProfileFormOptions();
	    setEndpointModelOptions([], []);
	    // Icon hydration (card 015 wave 3): static-HTML glyph spans carry a
	    // unicode fallback in markup; boot swaps them for the registry SVGs so
	    // the page never depends on platform emoji/VS15 rendering. The unicode
	    // stays the honest fallback wherever querySelectorAll is unavailable.
	    if (typeof document.querySelectorAll === "function") {
	      for (const [cls, icon] of [["icon-refresh", ICONS.refresh], ["icon-gear", ICONS.gear], ["icon-retry", ICONS.retry]]) {
	        document.querySelectorAll("." + cls).forEach((el) => { el.innerHTML = icon; });
	      }
	    }
	    $("open-setup").onclick = () => openFirstRunWizard(firstRun.step || "welcome");
	    $("first-run-next").onclick = () => firstRunStep(1);
	    $("first-run-back").onclick = () => firstRunStep(-1);
	    $("first-run-skip").onclick = () => completeFirstRun("skipped");
	    $("first-run-finish").onclick = () => completeFirstRun("finished");
	    // Escape closes the full-page guide WITHOUT marking it done (it reopens
	    // next load); Skip/Finish record the outcome.
	    if (typeof document.addEventListener === "function") {
	      document.addEventListener("keydown", (event) => {
	        if (event.key === "Escape" && firstRun.open && !islands.appearanceOpen) closeFirstRunWizard();
	      });
	    }
	    // NAV DRAWER (below 1024 px the sidebar is an overlay drawer, DESIGN
	    // §5.2): the header's ☰ opens it; Escape, the backdrop, the close
	    // button and picking a section close it; focus goes into the drawer on
	    // open and back to ☰ on close; while it is open the shell behind it is
	    // `inert`. Escape closes ONE layer: with a dialog open above the drawer
	    // (modals stack above it, z-index), Escape belongs to the dialog, and a
	    // key another handler already consumed (defaultPrevented) is ignored.
	    // Above 1024 px nothing changes (a resize past it closes the drawer).
	    // Self-contained (document/window/lookup passed in) so the behaviour
	    // tests run it against a fake DOM.
	    function installNavDrawer(doc, win, byId) {
	      const body = doc.body;
	      const query = (sel) => (typeof doc.querySelector === "function" ? doc.querySelector(sel) : null);
	      const queryAll = (sel) => (typeof doc.querySelectorAll === "function" ? Array.from(doc.querySelectorAll(sel)) : []);
	      const isOpen = () => body.classList.contains("nav-open");
	      // Rendered = has boxes (a `.hidden` or display:none dialog, or one inside a hidden backdrop, has none).
	      const shown = (el) => typeof el.getClientRects === "function" && el.getClientRects().length > 0;
	      const dialogOpen = () => queryAll('.modal-backdrop, .first-run-page, .acc-modal-backdrop, [aria-modal="true"]').some(shown);
	      function set(open) {
	        const on = !!open;
	        const was = isOpen();
	        body.classList.toggle("nav-open", on);
	        byId("nav-toggle").setAttribute("aria-expanded", on ? "true" : "false");
	        const shell = query(".shell_main");
	        if (shell) {
	          if (on) shell.setAttribute("inert", "");
	          else shell.removeAttribute("inert");
	        }
	        if (on && !was) {
	          const target = query("#console-nav .tab-button.active") || query("#console-nav .tab-button");
	          if (target && typeof target.focus === "function") target.focus();
	        } else if (!on && was) {
	          const toggle = byId("nav-toggle");
	          if (toggle && toggle.offsetParent !== null && typeof toggle.focus === "function") toggle.focus();
	        }
	      }
	      byId("nav-toggle").onclick = () => set(!isOpen());
	      byId("nav-close").onclick = () => set(false);
	      byId("nav-backdrop").onclick = () => set(false);
	      // Bubbles after the tab button's own onclick: the section is shown, then the drawer closes.
	      byId("console-nav").onclick = (event) => {
	        const btn = event && event.target && typeof event.target.closest === "function" ? (event.target.closest(".tab-button") || event.target.closest("#open-setup")) : null;
	        if (btn && isOpen()) set(false);
	      };
	      const onKey = (event) => {
	        if (event.key !== "Escape" || !isOpen() || event.defaultPrevented || dialogOpen()) return;
	        event.preventDefault();
	        set(false);
	      };
	      // Capture phase on window: runs before the dialogs' own document-level
	      // Escape handlers, so "is a dialog open" is read before one closes.
	      if (win && typeof win.addEventListener === "function") win.addEventListener("keydown", onKey, true);
	      if (win && typeof win.matchMedia === "function") {
	        const wide = win.matchMedia("(min-width: 1024px)");
	        const onWide = () => { if (wide.matches && isOpen()) set(false); };
	        if (typeof wide.addEventListener === "function") wide.addEventListener("change", onWide);
	      }
	      return { set, isOpen, onKey };
	    }
	    installNavDrawer(document, typeof window !== "undefined" ? window : null, $);
	    // A #claim= link signs this browser in (and opens the wizard) before
	    // the normal session probe; without one, boot is unchanged.
	    if (!redeemClaimFromHash()) refresh();
  </script>
</body>
</html>"""
