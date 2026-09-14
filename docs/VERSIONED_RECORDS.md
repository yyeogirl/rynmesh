# Versioned records

Local AI setup uses a private integer-v1 `llm/setup-recovery.json` journal.
It snapshots exact manifest/settings bytes and SHA-256 digests before async
configuration, bounds each source to 2 MiB and the journal to 6 MiB, and refuses
unknown versions, fields or corrupt snapshots before restoring files. A durable
`committed` receipt discards private snapshots before best-effort unlink, so a
leftover receipt cannot roll back later configuration. See
[setup rollback acceptance](acceptance/local-ai-development/setup-rollback.md).

Managed-document `library-imports/control.json` supports integer version 2 with
a validated `ryn.library-cleanup.v1` receipt. Explicit cleanup backs up original
v1 bytes before upgrade and preserves unknown control extensions. The receipt
holds store-generated names, hashes and progress, never document bodies. Its
source fence hides selected copies until file cleanup finishes; the completed
receipt drops the manifest while retaining the result and monotonic generation.
See [document cleanup](acceptance/friends-development/document-cleanup.md).

Friend-feed state supports `ryn.friend-feed.v2`, with bounded erased publication
IDs and a validated `ryn.friend-feed-cleanup.v1` receipt. Existing v1 ordinary
reads/writes retain v1; explicit reviewed cleanup saves exact encrypted
`.v1.migrated` bytes before upgrading atomically. Fresh stores use v2. Unknown
top-level extensions survive, unsupported versions fail closed, and capacity
exhaustion never drops old replay barriers. See [feed cleanup acceptance](acceptance/friend-feed-development/cleanup.md).

Reviewed reading cleanup introduces `ryn.consumption.v4` only when the owner
commits that cleanup. It retains the v3 compact projection encoding, permits
`sync: null`, and requires a validated `ryn.reading-source-erasure.v1` receipt.
Original legacy/v2/v3 bytes are backed up before the upgrade; the coordinated
cleanup review includes that impending backup. Reading records remain writable
without enabling sync. On later explicit opt-in, known causal deletion clocks
are seeded before post-cleanup local edits. Future receipt formats are refused.
The separate encrypted `ryn.reading-cleanup.v1` journal persists one current or
latest operation and a monotonic sequence. See
[`reading-cleanup.md`](acceptance/device-sync-development/reading-cleanup.md)
for bounds, recovery tests and the exact erasure scope.

Every on-disk record a route package owns follows the convention
`rynmesh/llm_package/task_balance.py` established: a version tag, a
forward-only migration at load time, a durable backup before a migration
rewrites anything, and a bias toward rebuilding a snapshot rather than
trusting one that disagrees with its source of truth. This document
describes what `task_balance.py` actually does; `rynmesh/atomic_io.py`
(added after `task_balance.py` was written) is the shared helper new
records — including the ones `scripts/new_route_package.py` generates —
should use for the write side of this convention.

## The version field

Every record carries a version string identifying its shape. `task_balance.py`
uses two, one per persistence backend:

```python
LEDGER_VERSION = "rynmesh-dev-task-balance-v1"
SNAPSHOT_VERSION = "rynmesh-dev-task-balance-view-v2"
```

`_read` refuses to load a file under the wrong version outright rather than
guessing at its shape:

```python
expected = LEDGER_VERSION if self._ledger is None else SNAPSHOT_VERSION
if value.get("version") != expected or not value.get("development_only"):
    raise TaskBalanceError("not a development Task Balance ledger")
```

The separator varies across the codebase — `rynmesh.mailbox.v1`
(`mailbox.py`) and `rynmesh.llm.task.v1` (`llm_package/task_protocol.py`)
use dots, while `task_balance.py`'s own versions, `CREDIT_LEDGER_VERSION`
("rynmesh-credits-v0.1"), and `IDENTITY_VERSION` ("rynmesh-identity-v0.1")
use hyphens. Match whichever separator the record's own module family
already uses; the requirement is the shape, not the punctuation: a string
that names *what* the record is and *which* version of it this is, checked
exactly on load, not parsed for a numeric suffix and compared loosely. A
generated package's `<Name>Store` uses a plain integer `VERSION` constant
instead (see the generator's own template) — the same exact-match
requirement, in the simplest form that a first version needs.

## Migration is forward-only, at load time

`task_balance.py` has exactly one migration today —
`_migrate_legacy`, which upgrades a standalone v1 ledger file into the
ledger-backed v2 snapshot the first time a credit ledger is attached to it.
It runs from `_initialize_ledger_backed`, at construction (i.e. at load
time), never lazily on some later write:

```python
if existing and existing.get("version") == LEDGER_VERSION and category_count == 0:
    self._migrate_legacy(existing)
    return
```

There is no code path that migrates a record *backward* to an older
version, and none should ever be added: a node that has already upgraded
its shape has no way to un-know the fields the new shape added, and a
downgrade would either drop them or invent values for them.

## Back up before you rewrite

Before `_migrate_legacy` writes the upgraded snapshot, it copies the
original file aside:

```python
shutil.copyfile(self.path, self.path.with_suffix(self.path.suffix + ".migrated"))
```

`task_balance.py` predates `rynmesh/atomic_io.py` and does this copy by
hand. The shared, durable equivalent for anything written after Task 1 is
`atomic_io.migration_backup(path)` — it reads the source first and only
overwrites an existing backup after that read succeeds, and the backup
write itself goes through the same atomic-write path as everything else in
`atomic_io`. A new migration should call it before writing the upgraded
record, exactly where `_migrate_legacy` calls `shutil.copyfile` today:

```python
def migration_backup(path: str | Path, *, suffix: str = ".migrated") -> Path | None:
    """Durably copy `path` aside to `path` + `suffix`; return the backup path."""
```

## Unknown fields are preserved on read

`_read` parses the whole JSON object and returns it as-is (beyond
`setdefault`-ing one key for older files); it never projects the record
down to a fixed set of known fields:

```python
value = json.loads(self.path.read_text(encoding="utf-8"))
...
value.setdefault("earnings", {})
return value
```

A field an older version of this code doesn't recognize survives a read
and a subsequent write unchanged. This matters across a mixed-version mesh:
an older node writing a record must not silently delete a field a newer
node put there — the general failure mode a strict, "keep only what I
recognize" reader invites.

## A snapshot that disagrees with its source of truth gets rebuilt

`task_balance.py`'s JSON file is a *materialized snapshot* over the
node's credit ledger when ledger-backed; the ledger's signed, append-only
events are the actual source of truth. `_initialize_ledger_backed` checks
the snapshot's own bookkeeping (`folded_events`) against a fresh count from
the ledger, and replays the ledger from scratch the moment they disagree,
rather than trusting the stale file:

```python
if existing and existing.get("version") == SNAPSHOT_VERSION:
    if int(existing.get("folded_events") or 0) == category_count:
        self._folded = category_count
        return
    self._write(self._replay())
    return
```

`rebuild()` exposes the same replay as an explicit operator action.
`_replay()` walks every signed event in the ledger's `dev:task_balance`
category, in order, and re-applies each transition
(`_apply_hold`/`_apply_settle`/`_apply_release`/`_apply_earning`) to a
fresh state, skipping any transition that is invalid under replay ordering
rather than aborting the whole rebuild. The pattern to copy: whenever a
record is a derived view over some other durable, authoritative source
(a ledger, another node's registry, a signed history), treat a
verification mismatch as "rebuild the view," not "trust the file on disk."
A record that is itself the only copy of its data (nothing to replay from)
has no such fallback and just has to be right.

## What belongs in a record, and what never does

A record may hold: state a package needs to survive a restart (balances,
counters, hold/settlement bookkeeping, timestamps, small bounded lists of
recent events), and identifiers that are already public by design (a peer
ID, a task ID, a service ID).

A record must never hold: a prompt or a model's output, a request or
response body, a private key or any other secret material, an invite or
pairing secret, or an absolute filesystem path. `task_balance.py`'s own
holds and settlements carry `service_id`, `provider_peer_id`, token counts,
and amounts — never the prompt or the generated text that produced them.
This is the same boundary `docs/ROUTE_PACKAGES.md`'s error-handling section
draws for exceptions and status routes; a versioned record is just another
place the same data could leak into something that outlives the request
that created it.

## Reviewing a version bump

A version bump is not just a new constant. The migration that upgrades the
old shape and the test that exercises it — feeding the migration a
real old-shape record and asserting the new shape comes out right, unknown
fields survive, and a `.migrated` backup was left behind — land in the
**same PR** as the new shape. A shape change without its migration and
test in the same PR is, by definition, untested: nothing has ever run the
migration against a record that actually predates it.
