# 0957-gateway: [BUG] `abstractgateway service enable` on Linux does not say that the service stops at SSH logout and never starts at boot without lingering

> Package: abstractgateway (firstrun_cli, os_service, autostart, consoles' Start at login switch)
> Type: bug
> Status: implemented 2026-10-29 (autostart.linger_state / linger_note; the summary carries the sentence, so both consoles show it; the unconditional plan hint is gone)
> Created: 2026-10-28
> Priority: P1
> Labels: service, linux, headless, first-run
> Root link: AbstractFramework release-closing wave 2026-10-28, fresh-Linux test (framework root `untracked/release-closing/ovh-test.log`, step 2)

## Summary

On a headless Linux server, `abstractgateway service enable` (what the installer's summary suggests) answers
`start at login [on]` and nothing else. With `Linger=no` (the default), the systemd user unit stops when the
last SSH session ends and does not start at boot. The command must say so, with the exact fix, when lingering
is off: `sudo loginctl enable-linger <user>`.

## Evidence

Ubuntu 24.04 cloud instance, gateway 0.17.0 (`ovh-test.log` l.39-49): `service enable` wrote the unit and
printed `start at login [on]`; after a reboot the gateway started only when an SSH login started `user@1000`;
closing every SSH session for 25 s logged `Stopping abstractgateway.service`. `sudo loginctl enable-linger
ubuntu` fixed both (same pid after a 20 s no-session gap).

## Current code reality

- `os_service.build_install_plan` (`src/abstractgateway/os_service.py` l.819-828) appends the hint for every
  Linux systemd plan, without `sudo` and whatever the linger state: "To keep it running after logout / start it
  at boot, run once: loginctl enable-linger "$USER"".
- `firstrun_cli._print_autostart_result` (`src/abstractgateway/firstrun_cli.py` l.238-241) prints only the notes
  that mention `OLD registration` or `--pin-command-line`: the linger note is dropped for `service enable`.
- `autostart_status` reports `on` with no linger field; the consoles' Start at login switch shows the same.

## Scope

- In: read `loginctl show-user <user> -p Linger` (no sudo needed) on Linux systemd; when it is not `yes`,
  `service enable`, `service install` and `service status` print one line: "On a server, this service stops when
  you log out and does not start at boot until you run once: sudo loginctl enable-linger <user>".
- In: `autostart_status` carries `linger: true|false|null`; both consoles show the same sentence under the
  switch when it is `false`.
- Out: running `loginctl enable-linger` for the user (it needs admin rights; machine mutation is the operator's).

## Validation

- Test: a fake runner answering `Linger=no` makes `service enable` print the line with `sudo loginctl
  enable-linger <user>`; `Linger=yes` prints nothing; macOS and the XDG fallback never print it. Red check:
  remove the branch and the `Linger=no` test fails on the missing line.
- Real: on a Linux server with `Linger=no`, `service enable` shows the line; after `enable-linger`, `service
  status` no longer does.
