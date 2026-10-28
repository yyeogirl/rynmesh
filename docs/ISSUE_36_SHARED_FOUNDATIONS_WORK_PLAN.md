# Issue #36 — shared atomic store, route-package pattern, record convention

Status: in progress (system track). Tracks
[#36](https://github.com/yeogirlyun/rynmesh/issues/36). Every one of the five
user-facing plans needs this in its first slice, so it lands before that work
starts.

## Problem

**The atomic-write pattern is copied nine times, and only the newest copy is
safe.** `rynmesh/mailbox_store.py` writes through a 0600 file descriptor,
fsyncs, and removes the temp file if anything fails. The eight older sites do
not:

| site | 0600 | fsync | temp cleanup | unique temp name |
|---|---|---|---|---|
| `mailbox_store._atomic_write_json` | yes | yes | yes | no |
| `mailbox_client._save_seen` | yes | yes | no | no |
| `signal50_media_ops._write_job` | no | yes | yes | yes |
| `recommendation_profile._write` | no | no | no | no |
| `settings_store` | no | no | no | no |
| `update_state.save` | no | no | no | no |
| `services/consumption._write` | no | no | no | no |
| `services/assistant_audit._write` | no | no | no | no |
| `runtime_native_install` marker | no | yes | yes | no |

The gaps are real, not stylistic. Without fsync a crash after the rename can
leave a zero-length file where a valid record used to be. Without 0600 the
node's settings, reading history, and recommendation profile are world-readable
on a shared machine. Without temp cleanup a failed write leaves `<name>.tmp`
behind forever. Without a unique temp name two writers racing on one path
corrupt each other's temp file. No site fsyncs the directory, so the rename
itself is not durable.

**There is no documented way to add endpoints.** `peer_http.py` is 2437 lines
against a 10K hard cap with mandatory restructuring at 8K, and the four
user-facing packages (first run, library, friends, assistant) each need their
own routes. Two good examples exist (`llm_package/routes.py::install_llm_routes`,
`mailbox_routes.py::install_mailbox`) but the conventions they share are
nowhere written down, so the next package will rediscover them by reading.

**Versioned records have a convention but no statement of it.**
`task_balance.py` gets it right — a `version` field, a forward-only rebuild,
and a `.migrated` copy of the old file kept before rewriting. Every user plan
adds records that need the same treatment.

## Design

### `rynmesh/atomic_io.py`

```python
class AtomicIOError(OSError): ...

def atomic_write_bytes(path, data, *, mode=0o600, dir_mode=0o700,
                       max_bytes=MAX_RECORD_BYTES, fsync_dir=True) -> None
def atomic_write_json(path, value, *, indent=None, sort_keys=True,
                      ensure_ascii=True, **write_kwargs) -> None
def read_json(path, *, default=_REQUIRED, max_bytes=MAX_RECORD_BYTES)
```

One writer, every guarantee: parent directory created at `dir_mode`, a
uniquely named temp file in the same directory (so two writers cannot collide
and the rename stays on one filesystem), the file opened at `mode`, written,
flushed, `fsync`ed, renamed, and the directory `fsync`ed so the rename itself
survives a power loss (skipped where the platform cannot, which is Windows).
Any failure removes the temp file and raises `AtomicIOError`; the destination
is either the old content or the new content, never a partial file.

`max_bytes` bounds both directions. A caller that would write more raises
before touching the filesystem; a file larger than the cap raises on read
rather than being loaded into memory.

`read_json` is strict by default and tolerant when given a `default`: missing
file or unparseable content returns the default instead of raising. That is
the shape the existing call sites already hand-roll.

**Migration keeps every site's current on-disk format.** `indent`,
`sort_keys`, and `ensure_ascii` are parameters precisely so adopting the
helper does not reformat a single existing file — the diff changes how bytes
reach the disk, not what they say.

### Route packages

`docs/ROUTE_PACKAGES.md` writes down what the two existing packages already
do, so the next four copy it rather than re-deriving it: the
`install_<name>(app, *, store, home, workers, local_control=None)` signature
and why each argument exists, where package state lives (`app.state.<name>`),
how a package registers a supervised worker (`replace=True`, so an installer
can run twice), how a local route re-checks node auth, the error-code mapping
convention, and the rule that no body, ciphertext, path, or secret reaches a
log line or a status field.

`scripts/new_route_package.py <name>` stamps out a working package and its
test from that pattern — a real generator rather than a dead example package
committed to the tree. The generated package imports, installs, registers a
worker, serves a status route, and passes its generated test on the first run;
a test in this repo runs the generator into a temporary directory and proves
that.

### Versioned records

`docs/VERSIONED_RECORDS.md` states the convention `task_balance.py` follows: a
`version` string on every record, forward-only migration at load time, a
`.migrated` copy of the previous file kept before the rewrite, unknown fields
preserved rather than dropped, and a rebuild path when the snapshot and its
source of truth disagree. `atomic_io.migration_backup(path)` performs the
one mechanical step — copy the current file aside before a forward migration
rewrites it — since every migration needs it and hand-rolling it is where the
data gets lost.

### Not in scope

Rewriting `peer_http.py` into packages (that is per-feature work as each new
package lands), a database, and any change to what the nine sites store.

## Tasks

1. `atomic_io.py` with tests, then adopt it at all nine call sites with each
   site's on-disk format preserved.
2. The route-package generator, `docs/ROUTE_PACKAGES.md`,
   `docs/VERSIONED_RECORDS.md`, and `migration_backup`, with tests.

## Acceptance

- [ ] A crash between write and rename leaves the destination readable with
      its previous content; a failed write leaves no `.tmp` file behind.
- [ ] Every adopted site writes 0600 files under 0700 directories, and the
      bytes on disk are byte-identical to what that site wrote before.
- [ ] Two concurrent writers to one path cannot corrupt each other's temp
      file; the loser's content is simply overwritten.
- [ ] A record larger than `max_bytes` raises before writing, and a file
      larger than the cap raises on read instead of being loaded.
- [ ] `read_json` returns the default for a missing or corrupt file when one
      is given, and raises when one is not.
- [ ] `scripts/new_route_package.py demo` produces a package that imports,
      installs onto a FastAPI app, registers its worker, serves its status
      route, and passes its own generated test — proven by a test here.
- [ ] Both docs describe the shipped API accurately, and no route-package
      guidance contradicts the two existing packages.
- [ ] `python -m ruff check rynmesh/ tests/` and the full pytest suite pass.

## Conflict note

[#45](https://github.com/yeogirlyun/rynmesh/pull/45) (worker supervision) is
open and touches `background_workers.py`, `peer_http.py`, and
`docs/ARCHITECTURE.md`. This branch touches none of those, and adds only new
doc files, so the two do not conflict.
