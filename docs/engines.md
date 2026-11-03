# Local engines: install, start, stop

The gateway can install the local inference engines (Ollama, LM Studio, MLX,
llama.cpp, vLLM, the Hugging Face stack) on the machine it runs on, from the
console or the command line, without a terminal and without a password in the
common case. This page explains what each **Install** does, when it asks for
an administrator password (almost never), and the API a UI renders. The rules every
install follows are ADR 0003 (engine installation contract); the per-engine, per-OS facts
are in [engines-install.md](./engines-install.md).

## What Install does, engine by engine

| Engine | macOS (Apple silicon) | Linux | Windows (x86_64) | Admin rights? |
|---|---|---|---|---|
| **Ollama** | Downloads the signed `Ollama-darwin.zip` from Ollama's official GitHub release, checks its published SHA-256 and the Developer ID signature (team `3MU9H2V9Y9`), places `Ollama.app` in `/Applications` when your account can write it (an admin account can, with no password) or else in `~/Applications`, then starts it and waits until `http://127.0.0.1:11434/api/version` answers | Once for this computer, under the gateway's service account (whatever account that is, root included): Ollama's official `ollama-linux-<arch>.tar.zst` from its GitHub release (SHA-256 checked), unpacked beside the installed one and swapped into `~/.local` (a failed update keeps the old one), served by a systemd user service `ollama.service` on port 11434. Needs `zstd` to unpack | `winget install Ollama.Ollama` (per-user) | No, on every system |
| **LM Studio** | Downloads the signed disk image from lmstudio.ai, checks the signature (team `D65G88RHWN`), copies `LM Studio.app` to `/Applications` or `~/Applications`, opens it once and starts its local server (`lms server start`) | LM Studio's official installer: the headless daemon and the `lms` CLI under `~/.lmstudio`. It needs the `libatomic1` system library, which minimal server images lack: the install checks it first and, when it is missing, stops in *needs tools* with the line to run (`sudo apt-get install -y libatomic1`; Fedora: `sudo dnf install -y libatomic`), then **re-check** continues | LM Studio's official PowerShell installer (the `lms` CLI under `~/.lmstudio`) | no |
| **MLX** | `abstractcore[apple]` at the installed AbstractCore version (MLX as prebuilt wheels) into the gateway's own Python | not supported | not supported | no |
| **llama.cpp** | `abstractcore[apple]`, with its llama.cpp taken from upstream's prebuilt Metal wheel (`llama-cpp-python` 0.3.28); no compiler | `abstractcore[gpu]` with the prebuilt CUDA wheel (cu125/cu130, matched to the installed CUDA) on an NVIDIA machine, else the prebuilt CPU wheel (0.3.36) | comes with the AbstractFramework installer, which adds llama.cpp's prebuilt CUDA, Vulkan or CPU build; the row's Install shows that installer's command | no |
| **vLLM** | not supported (the row says why; no Install button) | Linux with an NVIDIA GPU: `abstractcore[gpu]` into the gateway's Python | not supported natively (no Windows build): run vLLM in WSL2 or on a Linux machine and point the gateway at that server | no |
| **Hugging Face** | `abstractcore[apple]` at the installed AbstractCore version (several GB) | `abstractcore[gpu]` | `abstractcore[gpu]` at the installed AbstractCore version, with `--torch-backend=auto` so uv takes PyTorch's CUDA build | no |

Python engines only ever arrive with one of AbstractCore's install settings (`abstractcore[apple]`
on Apple silicon, `abstractcore[gpu]` on Linux and Windows x86_64), never as a bare package, so each
one installs the whole setting (several GB). An Intel Mac, a Python under Rosetta, and Windows on ARM
have no such setting:
their llama.cpp and Hugging Face rows say the engine is not available on this machine with the
install settings and offer no Install button; use Ollama, LM Studio or a remote provider there.

On Windows, Ollama and LM Studio installs use the vendor commands AbstractCore plans (winget, the
vendor PowerShell installers).

Everything the gateway's own Python receives is installed with the gateway's
`abstract*` packages pinned to the versions it runs, so an engine install can
never change the gateway itself.

### Prebuilt only, never a source build

Every engine arrives as the vendor's prebuilt binary or wheel (ADR 0003). The native packages of
AbstractCore's settings (`llama-cpp-python`, PyTorch, vLLM, MLX) are installed with
`--only-binary`, so no install ever starts a compiler. A machine with no prebuilt build of an
engine does not get that engine; its row says so in one sentence and has no Install button, for
example:

> llama.cpp has no prebuilt build for this computer (linux riscv64), and the gateway never builds
> engines from source. Ollama or LM Studio run GGUF models here when they are offered on this
> computer.

An update is atomic: when the new version fails to install or to load, the packages it changed go
back to the versions they had, and an app or archive is unpacked beside the installed one and
swapped in only once it is complete.

### When an administrator password is needed

Only when a step genuinely needs it, and never silently:

- you asked for `/Applications` (`"location": "system"`) and your account
  cannot write it (a standard, non-admin account).

A system library an engine links (LM Studio's `libatomic1` on minimal Linux images) is installed by
the machine's administrator once; the job says which line to run.

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
the gateway's data folder (`engines/jobs/<job id>.log`). A llama.cpp wheel
that does not install, for example, reads:

> The prebuilt llama.cpp metal wheel 0.3.28 did not install (the wheel file is
> corrupted). Nothing was built from source, and the llama.cpp that was installed
> before (if any) is kept. The packages it changed were put back to the versions
> they had: the engine installed before is unchanged.

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
carries `status` (`host_job_v1` words) so clients polling `GET /jobs/{id}`
read the same lifecycle as other host jobs.

Jobs live in the gateway process: a restart forgets them (the log files stay).
