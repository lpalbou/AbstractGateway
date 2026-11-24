### Changed

- **On Windows, commands are sandboxed under every workspace shape** (`docs/security.md`, `docs/console.md`,
  `docs/api.md`, `docs/troubleshooting.md`, `docs/configuration.md`, `serve --help`). With AbstractCore 2.39.0
  the console's state line reads `Commands sandboxed: Windows restricted token` (state `sandboxed`, no longer
  `partial`) and the commands of runs set to "Deny everything, allow listed workspaces" or to read-only by default
  run confined instead of being refused. "On Windows" in `docs/security.md` says what each setting lets a command
  read and write, its limits (drives without permissions and network shares, what stays readable under "Deny
  everything", names on the way to a workspace, a command already running when a folder first becomes read &
  write, content with permissions of its own, Git Bash, Windows' TLS, the places Windows marks Low), what the
  sandbox leaves on your folders, and each message of a command that does not run with what to do; `partial` is
  now only Linux Landlock without bubblewrap.
