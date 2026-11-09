### Security

- **A mention can no longer join another account's visit** (2026-11-09 review K). Before this fix, a member could post
  a mention into the admin's visit session, read that visit through her own entity, and speak in it as the admin.
  Three causes:
  - The mention door reused any live visit bound to the posted session id.
  - Its 409 handed the holding session's id to the caller.
  - The visit door seated a non-participant as the visit's person.

  Now:
  - The mention door reuses only a visit it opened itself, in that session, with the caller among the stamped
    participants.
  - A busy refusal (`in_another_conversation`) names no other account's session, run or holder: only the caller's own
    holding conversation is named. Causes go to the log.
  - The visit-lane mirror turns an answer only into a visit the caller is in.
  - `POST /entities/{name}/visit/{run_id}/turn` refuses a caller who is not a stamped participant: 403
    `not_a_participant`, "This visit belongs to another conversation." The visit's speaker is always the caller.
