### Added

- **Who took his own time back is recorded** (AbstractRuntime 17be7e3 `write_personal_grant(revoked_by=)`). The
  Stop (and a Resume cancelling a grant armed meanwhile) revokes with `revoked_by: kill_switch`; the operator's own
  revoke (the personal toggle off, the personal-grant door, the loop stop's revoke) with `revoked_by: operator`;
  the next grant clears it. The life state serves `personal_revoked_by` (null while own time is given), so a
  client can say "own time revoked by the operator" or "by Stop".
