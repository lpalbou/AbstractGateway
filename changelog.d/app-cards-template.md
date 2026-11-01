### Changed

- **The console's app cards are one template, declared by the app registry** (`apps_manager.AppSpec`,
  `apps_desktop.DesktopAppSpec`, docs/apps.md "Registering an app"). An entry declares its card: `description`
  (one whole sentence, no longer cut to one line), `mark`, `illustration` (a 16:9 WebP under
  `console_assets/apps/`) and an optional `family`. A card shows its illustration, mark, name, status badge,
  description and one row of launchers derived from its members' kinds: **Open in browser** with a globe badge,
  **Open on this Mac** (**Open on this computer** elsewhere) with a monitor badge, **Open in Terminal** with `>_`.
- **The Assistant is one card with two launchers**: the web Assistant (`assistant-web`) and the desktop Assistant
  (`assistant`) form the `assistant` family. The card's status badge and primary button are those of the
  Assistant installed on this computer (the web one first); the other keeps its own status badge and notes (another
  copy running, quit to run a new version) beside its own button, and each Install says what it installs.
- **The Apps tab's error state shows the reason only**: the console no longer carries its own list of apps.
- **Node.js on the Apps tab is a card only when it needs you** (missing, installing, a failed install); ready, it
  is one quiet line under **Technical details**. The Apps toolbar is one line at the top right: a **Refresh** icon
  button and the settings gear. Every app card gives its description (one richer sentence per app) exactly two
  lines, its illustration fades into the card, and the status badge is quieter.

### Added

- **`GET /api/gateway/apps` returns `cards[]`** (`id`, `family`, `name`, `description`, `mark`,
  `illustration_url`, `members`, `launchers[{app, kind}]`), and every row carries `mark`, `family` and
  `illustration_url`; a desktop row's `desktop.open_label` names its launcher.
- **`GET /console/assets/apps/<file>`** serves the card illustrations the registry declares (public like the
  console page, immutable cache with a content-hash version query in the card's URL; any other name answers 404).
