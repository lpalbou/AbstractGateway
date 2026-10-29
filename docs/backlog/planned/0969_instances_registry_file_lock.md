# 0969-gateway: [BUG] instances.json needs a file lock: two installers started in the same second can draw the same slot

> Package: abstractgateway (`instances.py`: `slot`, `forget`, `_write`)
> Type: bug
> Status: planned (not implemented)
> Created: 2026-10-29
> Priority: P3
> Labels: instances, installer, concurrency
> Root link: merge desk review of PR #37 (gateway instances), non-blocking

## Summary

`instances.slot()` assigns a named instance's slot with read, pick the smallest free one, then write
to `<config home>/abstractgateway/instances.json`. A `threading.Lock` guards that sequence, so it is
safe inside one process only. Two processes can read the same registry before either writes: two
installers started in the same second, or `abstractgateway --instance a ...` next to
`--instance b ...`. Both then draw the same slot, and two instances share one app port block
(3001+200N, 3100+200N..3199+200N). The atomic replace in `_write` keeps the file whole, but the
second write drops the first instance's row.

## Done when

- The read-pick-write in `slot()` and `forget()` runs under an inter-process lock on a lock file next to
  the registry (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows), held across the read and the write.
- A test starts two processes that register two new instances at once and finds two distinct slots and
  both rows in the registry. It is red without the lock.
