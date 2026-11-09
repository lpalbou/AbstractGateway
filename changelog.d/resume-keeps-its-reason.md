### Fixed

- **Resume keeps its reason**, as Stop keeps its own. `POST /entities/{name}/lifecycle {action: "resume",
  reason}` (and the older `POST /entities/{name}/state {state: "awake", reason}` on a stopped entity)
  dropped the operator's reason: the state said only "resumed [by …]" and the `wake` marker said
  "resumed". The state now reads "resumed: <reason> [by … via …]" and the marker carries the reason;
  without a reason both say what they said before.
