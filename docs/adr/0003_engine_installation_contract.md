# ADR 0003: Engine installation contract

Status: Accepted.
Date: 2026-10-29

## Context

The gateway installs local inference engines (llama.cpp, Ollama, LM Studio, vLLM, the Hugging Face
stack, MLX) from the console and the CLI (`engines_install.py`). Each engine and OS grew its own
path: on the OVH server (2026-10-29) a Hugging Face install pulled AbstractCore's setting, whose
`llama-cpp-python` was not installed yet, and started building llama.cpp from source; the llama.cpp
path itself fell back to a source build (and asked for a compiler or the Apple tools) when no wheel
fit; Ollama on Linux ran its vendor `curl | sh` as root when the gateway ran as root; the Ollama card
said "for this account" as if engines were per user; a failed update could leave a half-written
environment or app. The operator: "the setup/install is customised per OS and may hide much more
problematic things." The per-engine, per-OS audit is `docs/engines-install.md`.

## Decision

1. **Once per computer, under the gateway's service account.** An engine is installed by the
   gateway's operator (an admin of the gateway) into the gateway's own Python environment or under
   the account the gateway process runs as, and is shared by every user of the gateway. The words
   on every card say so ("for this computer", "the gateway's service account"), never "for this
   account".
2. **No root where the vendor offers a non-root path.** Ollama on Linux is its release archive
   under the service account, whatever account that is (root included), never `curl | sh` as root.
   An app goes into `/Applications` only when the account can write it, else `~/Applications`; an
   administrator password is asked only for an explicit `/Applications` that is not writable. A
   system library an engine links (LM Studio's `libatomic1`) is an administrator step the job names.
3. **Prebuilt binaries and wheels only.** Every native package of an AbstractCore setting is
   installed with `--only-binary` (`PREBUILT_ONLY`), and an install of a setting that includes
   `llama-cpp-python` names the prebuilt wheel for this host when it is not installed yet. No
   install ever starts a compiler: a host with no prebuilt build of an engine does not get it,
   with one sentence, and a failed wheel fails the install with its reason. The `needs_tools`
   state remains only for a system prerequisite (libatomic1), never for a compiler.
4. **The same states and sentences on every OS.** The job states (`queued`, `downloading`,
   `installing`, `needs_admin`, `needs_tools`, `done`, `failed`, `cancelled`) and the refusal
   sentences are shared; an OS difference is a fact in the sentence ("no prebuilt build for this
   computer (linux riscv64)"), not another flow.
5. **Atomic updates.** A Python engine install snapshots the environment's package versions and,
   when the install or the load check fails, reinstalls the changed packages at their previous
   versions (wheels only). An app or archive is copied or unpacked beside the installed one and
   swapped in with renames; a failure leaves the installed one as it was.

## Consequences

### Positive
- No compiler, no build tools, no hidden root on any operator machine; failures say what failed
  and leave the previous engine working.
- One table describes every engine on every OS, and a test checks every row.

### Negative
- A host without a prebuilt build (Intel Mac for llama.cpp, Linux on unusual architectures) gets
  no llama.cpp from the gateway; Ollama or LM Studio serve GGUF there.
- A wheels-only install can refuse a setting whose dependency publishes only a source
  distribution; the native packages are the ones listed, the rest is resolved normally.

### Neutral
- Windows Ollama and LM Studio still run AbstractCore's vendor commands (winget, the vendors'
  PowerShell installers, per-user); their sentences are the generic ones (docs/engines-install.md,
  Known gaps).

## Enforcement
- `tests/test_engine_install_contract.py`: for every engine on every simulated OS host (Linux CUDA,
  CPU, root, arm64; macOS arm64 and Intel; Windows x64 and arm64), no plan mentions a compiler or a
  source build, every pip plan is `--only-binary` for the native packages, Ollama on Linux is the
  archive under the service account (root included), a failed Ollama update keeps the installed one,
  and the swap restores the old version when the new one cannot be moved in. It runs on the Linux
  CI shards and in the Windows job.
- `tests/test_gateway_engines_install.py`: a failed llama.cpp wheel never falls back to a source
  build and restores the previous packages; a host without a prebuilt build gets no install.

## Validation
- The release report lists the engines installed on the OVH box and the Mac with this contract.

## Related
- `docs/engines-install.md` (the table), `docs/engines.md` (the user guide); ADR 0001 (Core through
  the runtime facades: detection stays AbstractCore's).
