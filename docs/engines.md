# Local engines: install, start, stop

The gateway can install the local inference engines (Ollama, LM Studio, MLX,
llama.cpp, vLLM, the Hugging Face stack) on the machine it runs on, from the
console or the command line, without a terminal and without a password in the
common case. This page explains what each **Install** does, when it asks for
the Apple command-line tools or an administrator password, and the API a UI
renders.

## What Install does, engine by engine

| Engine | macOS (Apple silicon) | Linux | Admin rights? |
|---|---|---|---|
| **Ollama** | Downloads the signed `Ollama-darwin.zip` from Ollama's official GitHub release, checks its published SHA-256 and the Developer ID signature (team `3MU9H2V9Y9`), places `Ollama.app` in `/Applications` when your account can write it (an admin account can, with no password) or else in `~/Applications`, then starts it and waits until `http://127.0.0.1:11434/api/version` answers | Ollama's official installer (`curl -fsSL https://ollama.com/install.sh \| sh`) | macOS: no. Linux: yes (the installer writes `/usr/local` and a systemd service) |
| **LM Studio** | Downloads the signed disk image from lmstudio.ai, checks the signature (team `D65G88RHWN`), copies `LM Studio.app` to `/Applications` or `~/Applications`, opens it once and starts its local server (`lms server start`) | LM Studio's official installer: the headless daemon and the `lms` CLI under `~/.lmstudio` | no |
| **MLX** | Prebuilt `mlx` and `mlx-lm` wheels into the gateway's own Python | not supported | no |
| **llama.cpp** | Upstream's prebuilt Metal wheel (`llama-cpp-python` 0.3.28) into the gateway's own Python; no compiler | prebuilt CPU wheel (0.3.35) | no |
| **vLLM** | not supported (the row says why; no Install button) | Linux with an NVIDIA GPU: the vLLM wheels into the gateway's Python | no |
| **Hugging Face** | `abstractcore[huggingface]` at the installed AbstractCore version (several GB) | same | no |

Windows keeps the vendor commands AbstractCore plans (winget / the vendor
PowerShell installers / pip); the flow on this page is not yet exercised there.

Everything the gateway's own Python receives is installed with the gateway's
`abstract*` packages pinned to the versions it runs, so an engine install can
never change the gateway itself.

### When the Apple command-line tools are needed

Only for llama.cpp when no prebuilt wheel fits (an Intel Mac, a Python under
Rosetta, or a broken wheel): it must then be built from source, which needs a
C compiler. The job stops **before** the build, in the `needs_tools` state,
with the reason in plain words, for example:

> The prebuilt llama.cpp wheel did not install (no matching package for this
> Python and machine); building from source needs the Apple command-line tools.

**Install tools** runs `xcode-select --install`, which opens Apple's own
installer dialog on the gateway machine's screen (no administrator password
involved). The job waits and continues by itself once the tools are there.

### When an administrator password is needed

Only when a step genuinely needs it, and never silently:

- you asked for `/Applications` (`"location": "system"`) and your account
  cannot write it (a standard, non-admin account);
- Ollama on Linux (its installer writes `/usr/local` and creates a service).

The job stops in the `needs_admin` state and shows the exact reason and the
exact command. Nothing runs with administrator rights until someone presses
**Continue with administrator password**; the gateway then asks the operating
system: on macOS the standard password dialog (`osascript … with
administrator privileges`), on a Linux desktop `pkexec`. On a machine that
cannot show a dialog (a headless server), the job shows the command to run in
a terminal and a **re-check** button. There is no hidden `sudo` anywhere.

### What you see when something fails

The job's `message` is one plain sentence (what failed and why); the whole
log (every line, never a tail) is behind it in `details` and in a log file in
the gateway's data folder (`engines/jobs/<job id>.log`). A failed llama.cpp
build, for example, reads:

> The prebuilt llama.cpp wheel did not install (the wheel file is corrupted),
> and building it from source failed: no C compiler was found. The full build
> log is in the details.

## Command line

```bash
abstractgateway engines status --probe
abstractgateway engines install ollama --yes            # [--location auto|user|system] [--force] [--dry-run]
abstractgateway engines continue <job-id>               # after needs_admin (password dialog) or needs_tools
abstractgateway engines continue <job-id> --action install_tools
abstractgateway engines cancel <job-id>
abstractgateway engines start ollama                     # stop | start: ollama, lmstudio
```

A job that stops for tools or an administrator exits with code 2 and prints
what it needs and the `continue` command.

## API (contract `gateway_engines_v2`)

All routes are under `/api/gateway`. Reads are user-level; every POST is
admin-only; `install` and `continue` also need `allow_engine_install`
([configuration.md](./configuration.md#allow_engine_install)); a dry run never does.

| Method and path | Returns |
|---|---|
| `GET /engines?probe=1` | `{schema: "gateway_engines_v2", engines: [row…], install_allowed, install_policy, host, active_job}` |
| `GET /engines/{id}?probe=1` | one row; 404 for an unknown id |
| `POST /engines/{id}/install` `{dry_run?, force?, location?: auto\|user\|system}` | a job (below); `dry_run` returns the plan; 409 `busy` while another engine installs (the same engine joins its job); 409 `unsupported_on_this_machine` |
| `GET /engines/jobs` | `{jobs: [job…]}`, newest first |
| `GET /engines/jobs/{job_id}` | one job; 404 |
| `POST /engines/jobs/{job_id}/continue` `{action?: approve_admin\|install_tools\|recheck}` | the job, resumed; 409 when it is not waiting |
| `POST /engines/jobs/{job_id}/cancel` | the job |
| `POST /engines/{id}/start`, `/stop` | `{ok, engine, action, running, base_url, …}`; 409 for an engine that is not a server |

**Row:** `id, name, description, supported, support_reason, installed,
version, install_location, running, reachable, base_url, models_count,
install {available, method: wheel|app|script|unsupported, target, needs_admin,
admin_reason, needs_tools, tools_action, notes, steps, command_preview, url,
allowed, fallback {kind: open_page, url, recheck}}, actions [{id, label,
enabled, reason?, method?, path?, url?}], active_job`. Action ids: `install`,
`open_page`, `recheck`, `start`, `stop`, `docs`. `running` is known only on a
probed read, so `start`/`stop` appear only with `probe=1`.

**Job (`engine_install_job_v1`):** `job_id, engine, state, percent,
bytes_done, bytes_total, message, details, log_path, admin_prompt {key,
reason, command, method: osascript|pkexec|manual, button, prompt_text,
where}, tools_prompt {key, reason, tools, action {kind, command, available,
button}, started}, continue_actions, can_cancel, events [{at, state, message,
percent}], result, error {code, message}, started_at, updated_at,
finished_at`. `state` is one of `queued, downloading, installing, needs_admin,
needs_tools, done, failed, cancelled`. While a step runs quietly, `message`
says so at least every 3 seconds (`… (still working, 45 s)`). The job also
carries `status` (`host_job_v1` words) so older clients polling
`GET /jobs/{id}` keep working.

Jobs live in the gateway process: a restart forgets them (the log files stay).
