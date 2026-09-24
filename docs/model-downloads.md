# Model downloads: progress you can see

Every model download the Gateway starts is a job that reports real progress
(bytes, total, percent, speed, time left, per file) from the first second to
the last, for every source: Hugging Face (and MLX / mlx-gen), Ollama,
LM Studio and Supertonic. A download that stops receiving bytes says it is
stalled; a cancelled download stops within about a second and never leaves
files that later read as installed.

The job machinery is AbstractCore's (`abstractcore.config.host_jobs`,
`host_job_v1`); the Gateway serves it unchanged and adds the parent job for
"Use recommended defaults", the cancel route and the event stream.

## Routes

All under `/api/gateway`.

| Method and path | Access | Returns |
|---|---|---|
| `POST /models/download` `{"provider", "artifact", "dry_run"?, "expected_bytes"?}` | admin | `{"ok": true, "job": {...}}` at once; the bytes move in the background |
| `POST /models/download` `{"recommended": true}` | admin | `{"ok": true, "recommended": true, "jobs": [one per model], "group": {parent}}` |
| `GET /models/download/{job_id}` | user | `{"ok": true, "job": {...}}`; a `grp_...` id returns the parent; 404 when unknown (jobs are per Gateway process) |
| `GET /models/downloads` | user | `{"ok": true, "jobs": [...]}`, newest first; parents are listed, and each child names its parent in `parent_job` |
| `POST /models/download/{job_id}/cancel` `{"via": "console"}`? | admin | `{"ok": true, "job": {...}}` with `cancel_requested: true` and `cancelled_by`; the job turns `cancelled` when the tool has stopped (normally < 1 s); a parent cancels every running child; 404 when unknown. A console sends `{"via": "console"}` when a person clicked Cancel; without it the cancel is recorded as `api` |
| `GET /models/downloads/stream` | user | Server-Sent Events, below |

The request bodies are unchanged from before this contract (the cancel body is optional).

### The event stream

`GET /models/downloads/stream` sends `event: downloads` with
`data: {"jobs": [...]}` (the same list as `GET /models/downloads`) each time
something changed, at most every 0.5 s, and a `: keepalive` comment every
15 s. `?job_id=<id>` streams one job as `event: job` / `data: {"job": {...}}`.
`?until_idle=1` ends the stream once nothing is running (after sending the
final state). Polling keeps working; the stream is optional.

## The job

| Field | Meaning |
|---|---|
| `job_id` (also `job`) | `dl_...` for one download, `grp_...` for a parent |
| `kind` | `download`, or `download_group` for a parent |
| `status` | the coarse lifecycle older pollers read: `running` (queued included), `completed`, `failed`, `cancelled`; `host_status` keeps AbstractCore's own word |
| `state` | `queued`, `resolving`, `downloading`, `verifying`, `installing`, `done`, `failed`, `cancelled`, `stalled` |
| `bytes_done`, `bytes_total` | bytes so far and the total; same values as `downloaded_bytes`, `total_bytes` |
| `size_unknown`, `size_note` | `true` only when the source cannot say how big the download is; `size_note` says why |
| `percent` | `bytes_done / bytes_total × 100`; 100 when done; `null` while the size is unknown |
| `bytes_per_second` | speed over the last 5 s; falls to 0 when bytes stop, never frozen at its last value |
| `eta_s` | seconds left at that speed; `null` when unknown or stalled |
| `started_at`, `updated_at`, `finished_at` | ISO-8601 UTC; `updated_at` moves at least every 0.5 s while the job runs |
| `message` | one plain sentence to show as is |
| `detail` | the engine tool's own last line, unchanged |
| `files` | `[{name, bytes_done, bytes_total, state}]`, file state `pending`, `downloading`, `done`, `failed`, `cancelled`; Ollama layers are named `layer <digest>` |
| `current_file` | the file arriving now |
| `error` | the full reason when `failed` (the tool's own words) |
| `ended_reason` | when `failed` or `cancelled`: one plain sentence saying what happened and what a new download reuses, e.g. "The connection to Hugging Face dropped after 200 MB of 266 MB. Check the network connection, then download it again; the files that finished are kept, and the file that was in progress starts over." or "Cancelled in the console by admin at 21:15 after 105 MB of 275 MB. ..." |
| `stall_after_s`, `stalled_for_s` | the stall threshold (default 15 s) and how long the current stall has lasted |
| `transitions` | `[{at, state, why}]`, every state change |
| `cancel_requested` | `true` from the cancel request until the job ends |
| `cancelled_by`, `cancelled_by_user` | who asked for the cancel: `console` (a person clicked Cancel in a console), `api` (any other HTTP cancel), `cli` (`abstractcore models cancel`, Ctrl-C), `other_process` (a cancel marker from another program); the signed-in account when known. `null` unless a cancel was requested |
| `parent_job` | on a child of a parent job |

A parent (`download_group`) adds `children` (the full child jobs),
`child_job_ids` and `label`, and uses `files` for one row per model. Its
bytes, percent and speed add up its children; models already installed count
as done with nothing to fetch. Its `ended_reason` joins its children's. It is `stalled` only when every running child
is stalled, `failed` when every child ended and one failed (`error` names
which, with its reason), `cancelled` when one was cancelled, and `done` when
all are.

### States

- `queued`: accepted, not started (milliseconds for an in-process job).
- `resolving`: finding what to fetch and how big it is (hub file list, `lms get` search, Ollama manifest).
- `downloading`: bytes are moving.
- `stalled`: no bytes for `stall_after_s` seconds (15 by default,
  `ABSTRACTCORE_DOWNLOAD_STALL_S` on the Gateway host). The job keeps trying
  and turns back to `downloading` by itself when bytes arrive again. The
  stall and the recovery are logged (`abstractcore.host_jobs`) and listed in
  `transitions`.
- `verifying`: checking what arrived (Ollama's sha256, every Hugging Face file whole).
- `installing`: moving into the library (Ollama "writing manifest", LM Studio "Finalizing download...").
- `done`, `failed`, `cancelled`: finished. `cancelled` ONLY follows a cancel request
  (`cancelled_by` says whose); a download that stops on its own -- a dropped
  connection, a Hub error, a full disk, the Gateway restarting (the job then
  reads `failed` from its saved snapshot, and the transfer stops with it) -- is
  `failed`, with `ended_reason`.

`verifying` and `installing` never count as stalls.

### Real examples

Captured on a hermetic Gateway (port 18822, scratch caches) with the real
recommended artifact ids; the voice and image files came from a local stand-in
for huggingface.co and LM Studio from a stand-in `lms` that prints exactly what
the real one prints. Key fields only.

`queued`:

```json
{"job_id": "dl_2b0e98639928", "provider": "ollama", "artifact": "all-minilm", "status": "running", "state": "queued", "bytes_done": null, "bytes_total": null, "size_unknown": false, "percent": null, "bytes_per_second": null, "eta_s": null, "updated_at": "2026-09-24T03:28:38.435Z", "current_file": null, "message": "queued (detached)", "error": null, "stalled_for_s": null}
```

`resolving`:

```json
{"job_id": "dl_be2ff93db7d6", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit", "status": "running", "state": "resolving", "bytes_done": null, "bytes_total": null, "size_unknown": false, "percent": null, "bytes_per_second": null, "eta_s": null, "updated_at": "2026-09-24T03:27:55.745Z", "current_file": null, "message": "Preparing · reading the file list of AbstractFramework/flux.2-klein-4b-8bit", "error": null, "stalled_for_s": null}
```

`downloading`:

```json
{"job_id": "dl_f46f63c01098", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit", "status": "running", "state": "downloading", "bytes_done": 25976720, "bytes_total": 65005200, "size_unknown": false, "percent": 39.96, "bytes_per_second": 7699301.9, "eta_s": 6, "updated_at": "2026-09-24T03:25:15.363Z", "current_file": "transformer/diffusion_pytorch_model.safetensors", "message": "Downloading transformer/diffusion_pytorch_model.safetensors (2 of 5) · 26 MB of 65 MB · 7.7 MB/s · 6 s left", "error": null, "stalled_for_s": null, "files": [{"bytes_done": 1200, "bytes_total": 1200, "name": "model_index.json", "state": "done"}, {"bytes_done": 10485760, "bytes_total": 40000000, "name": "transformer/diffusion_pytorch_model.safetensors", "state": "downloading"}, {"bytes_done": 10485760, "bytes_total": 20000000, "name": "text_encoder/model.safetensors", "state": "downloading"}, {"bytes_done": 5000000, "bytes_total": 5000000, "name": "vae/diffusion_pytorch_model.safetensors", "state": "done"}, {"bytes_done": 4000, "bytes_total": 4000, "name": "README.md", "state": "done"}]}
```

`stalled`:

```json
{"job_id": "dl_f8684d929f51", "provider": "supertonic", "artifact": "supertonic-3", "status": "running", "state": "stalled", "bytes_done": 25942208, "bytes_total": 56185929, "size_unknown": false, "percent": 46.17, "bytes_per_second": 0.0, "eta_s": null, "updated_at": "2026-09-24T03:27:38.444Z", "current_file": "onnx/vector_estimator.onnx", "message": "Stalled: no data for 16 s · 26 MB of 56 MB · still trying, it resumes by itself when data flows again", "error": null, "stalled_for_s": 15.3}
```

`verifying`:

```json
{"job_id": "dl_3d015e06ebaa", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit", "status": "running", "state": "verifying", "bytes_done": 65005200, "bytes_total": 65005200, "size_unknown": false, "percent": 100.0, "bytes_per_second": 3742244.1, "eta_s": 0, "updated_at": "2026-09-24T03:28:21.002Z", "current_file": "transformer/diffusion_pytorch_model.safetensors", "message": "Verifying · checking 5 file(s) are whole · 65 MB of 65 MB", "error": null, "stalled_for_s": null}
```

`installing`:

```json
{"job_id": "dl_faa442ea7875", "provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "status": "running", "state": "installing", "bytes_done": 6000000000, "bytes_total": 6000000000, "size_unknown": false, "percent": 100.0, "bytes_per_second": 466469568.4, "eta_s": 0, "updated_at": "2026-09-24T03:26:40.607Z", "current_file": null, "message": "Installing · Finalizing download... · 6.0 GB of 6.0 GB", "error": null, "stalled_for_s": null}
```

`done`:

```json
{"job_id": "dl_e36dca05d05c", "provider": "supertonic", "artifact": "supertonic-3", "status": "completed", "state": "done", "bytes_done": 56185929, "bytes_total": 56185929, "size_unknown": false, "percent": 100.0, "bytes_per_second": null, "eta_s": 0, "updated_at": "2026-09-24T03:26:41.409Z", "current_file": null, "message": "Downloaded 56 MB in 15 s", "error": null, "stalled_for_s": null}
```

`failed`:

```json
{"job_id": "dl_842ede4aff62", "provider": "ollama", "artifact": "all-minilm", "status": "failed", "state": "failed", "bytes_done": null, "bytes_total": null, "size_unknown": false, "percent": null, "bytes_per_second": null, "eta_s": null, "updated_at": "2026-09-24T03:27:14.156Z", "current_file": null, "message": "cannot reach the Ollama server at http://127.0.0.1:11499: [Errno 61] Connection refused", "error": "cannot reach the Ollama server at http://127.0.0.1:11499: [Errno 61] Connection refused", "stalled_for_s": null}
```

`cancelled`:

```json
{"job_id": "dl_be2ff93db7d6", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit", "status": "cancelled", "state": "cancelled", "bytes_done": 25976720, "bytes_total": 65005200, "size_unknown": false, "percent": 39.96, "bytes_per_second": null, "eta_s": null, "updated_at": "2026-09-24T03:27:59.076Z", "current_file": "transformer/diffusion_pytorch_model.safetensors", "message": "cancelled", "error": null, "stalled_for_s": null}
```

`download_group (downloading)`:

```json
{"job_id": "grp_080bae3c6d3e", "status": "running", "state": "downloading", "bytes_done": 0, "bytes_total": null, "size_unknown": true, "percent": null, "bytes_per_second": null, "eta_s": null, "updated_at": "2026-09-24T03:26:26.753Z", "message": "Downloading 3 models · 0 of 3 ready · 3 of 3 sources cannot report their size yet", "error": null, "kind": "download_group", "files": [{"bytes_done": null, "bytes_total": null, "job_id": "dl_faa442ea7875", "name": "lmstudio qwen/qwen3.5-9b@4bit", "state": "downloading"}, {"bytes_done": null, "bytes_total": null, "job_id": "dl_e36dca05d05c", "name": "supertonic supertonic-3", "state": "resolving"}, {"bytes_done": null, "bytes_total": null, "job_id": "dl_8dde3c3f4834", "name": "mlx-gen AbstractFramework/flux.2-klein-4b-8bit", "state": "resolving"}], "children": "[3 child jobs]"}
```

`download_group (stalled)`:

```json
{"job_id": "grp_a1e3f58d4cd0", "status": "running", "state": "stalled", "bytes_done": 6090947408, "bytes_total": 6121191129, "size_unknown": false, "percent": 99.51, "bytes_per_second": 0.0, "eta_s": null, "updated_at": "2026-09-24T03:25:34.055Z", "message": "Stalled: no data for 16 s from supertonic supertonic-3 · 2 of 3 ready · 6.1 GB of 6.1 GB · 0 B/s", "error": null, "kind": "download_group", "files": [{"bytes_done": 6000000000, "bytes_total": 6000000000, "job_id": "dl_ec66382e80a1", "name": "lmstudio qwen/qwen3.5-9b@4bit", "state": "done"}, {"bytes_done": 25942208, "bytes_total": 56185929, "job_id": "dl_2b7360ab34fe", "name": "supertonic supertonic-3", "state": "stalled"}, {"bytes_done": 65005200, "bytes_total": 65005200, "job_id": "dl_f46f63c01098", "name": "mlx-gen AbstractFramework/flux.2-klein-4b-8bit", "state": "done"}], "children": "[3 child jobs]"}
```

## What each source reports

| Source | What the source exposes | What the job reports | Cancel |
|---|---|---|---|
| Hugging Face, MLX, mlx-gen | the hub's file list with sizes and blob names; the files being written in the cache (`blobs/<etag>…incomplete`) | the total and every file before the first byte, then per-file bytes read from disk every 0.25 s; files already complete count as done (the download resumes) | the transfer runs in a child process, stopped at once; its temporary files are removed; a marker in the repo folder keeps an unfinished download from reading as installed |
| Ollama | `/api/pull` lines with `digest`, `total`, `completed` per layer | layers added up into one total that never goes back, one `files` row per layer; "pulling manifest" is `resolving`, "verifying sha256 digest" `verifying`, "writing manifest" `installing` | the connection is closed at once; Ollama keeps the layers it has |
| LM Studio (`lms get`) | a progress bar with bytes, total, speed and time left | those numbers; "Finalizing download..." is `installing`. When no bar is printed: the bytes landing in the LM Studio models folder, "LM Studio reports no progress; N MB on disk so far", with `size_unknown: true` unless the catalog size was sent as `expected_bytes` | answers `lms get`'s "continue in the background?" with No, so LM Studio stops too, then stops the CLI |
| Supertonic (voice) | the size of each file (HEAD), then the file bodies | per-file bytes with the total known before the first byte | stops at once; the partial file is removed, finished files are kept |

Hugging Face downloads started from a job use plain HTTP rather than Xet:
Xet writes a file only once it is complete, so the bar would sit still and
then jump. `ABSTRACTCORE_HF_XET=1` turns Xet back on (progress then moves one
whole file at a time). With huggingface_hub 1.x a cancelled file restarts
from zero on the next download; files that were complete are kept.

## Limits

- Parent jobs live in the Gateway process: after a restart a `grp_...` id
  answers 404, while its children (AbstractCore jobs, persisted) can still be
  read with `GET /models/download/{dl_id}` or `GET /jobs`.
- `lms get` is LM Studio's own CLI. Its progress bar and its cancel question
  are what this relies on; if a future `lms` prints neither, the job falls
  back to bytes on disk and a plain stop of the CLI.
