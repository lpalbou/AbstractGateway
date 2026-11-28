### Added

- **Failed automations have their own group, and can be edited, turned back on and archived**
  (`automation_order.py`, `routes/automations.py`). `section` is now `active`, `failed`, `paused` or
  `archived`, in that order (`failed` = the automation itself stopped; `completed` stays in `active`), and the
  list answer carries `failed_automations_count`, counted like the paused one. The list cursor encodes the
  section rank, so its prefix is now `auto3:`: an older `auto2:` cursor is a 422 instead of a wrong page.
  `capabilities` of a failed automation are `revise`, `resume`, `archive`, `discuss` (without `resume` when its
  schedule is over); of a completed one `archive`, `discuss`; the command door lets exactly those through. A
  failed automation's summary carries `failed_reason`, one plain sentence of what happened and when ("It stopped
  on 7 Nov while starting run #38."), never error text or an instruction, and `failed_detail`, the stored
  technical error for a Details view; every summary
  `flow_missing` (true when the workflow it pins is not loaded on this gateway). A failed automation whose
  stranded run never started serves `current_occurrence: null`. `paused_reason` also words the controller's own
  pause cause (a run whose workflow is not installed or not allowed fails once, then the automation pauses).
- **Release: needs the next AbstractRuntime.** `routes/automations.py` and `hosts/bundle_host.py` import names
  that AbstractRuntime 0.21.0 does not have (`resumable`, `stopped_facts`, `PAUSE_CAUSE_KEY`,
  `start_failure_details`, the `START_FAILURE_*` codes); the three AbstractRuntime pins in `pyproject.toml`
  must be raised to that release with this one.
- **The live line of a run in progress** (`automation_live.py`). `GET /automations/{id}` serves
  `summary.current_occurrence.live = {started_at, state, tool, attempt, steps_done}`, folded from the replay
  view the gateway already keeps for the run (`starting`, `thinking`, `tool` with the running call, `waiting`
  for a person; only `llm` and `tool` activities count). Tool arguments are bounded to what one line needs (8
  scalar arguments, strings cut at 200 characters). Detail read only, never the list; absent when it cannot be
  computed (it exists only while the occurrence is `running`); between two steps the state is `thinking`.

- **An occurrence row serves `task`**: the run's own task text, without the runtime's
  `[Trigger … · occurrence N · fired …]` line (`user_turn` keeps the framed text for the Context view).

- **The run a stopped automation died on is served as failed, never running**: its occurrence row has
  `status: failed`, `not_started: true`, `failure.reason_code: occurrence_not_started` with a plain
  `failure.message` and the technical text as `failure.detail`, an empty answer, no files and no `ledger_url`/`workspace_url` (that run does not exist: its run routes
  answer 404); `last_occurrence` agrees (`not_started: true`).

### Fixed

- **A run's files are the person's files only** (`automation_files.py`, `routes/automations.py`). An occurrence
  row's `artifacts` listed every artifact stored for the run, named by its id: the runtime's storage offloads
  (the agent's scratchpad, a sub-run's state, a trace copy of the context) showed as
  `62736d734fb3a58eefd061cd82b34f5d application/json`. It now lists the references in the run's input (what the
  trigger carried) and output (what the agent returned) at a display path (`attachments`, `files`, `artifacts`,
  `media`, `*_artifact`; the shared chat's `displayArtifactPath` rule), with a real file name; storage offloads,
  workflow snapshots, evidence records, internal and voice-transport artifacts are never listed (typed metadata),
  and an output the runtime offloaded whole is read so the files inside it are found. A file made in a sub-run is
  served from its own run. Same field shape (`artifact_id`, `name`, `mime_type`, `url`); none = `[]`.
- **A catalog workflow refused at dispatch fails that run, not the automation** (`hosts/bundle_host.py`): the
  refusal is typed (`start_failure: workflow_not_allowed`, not retryable). An occurrence row whose attempt never started
  shows the recorded reason in `failure.message` instead of "failed".
