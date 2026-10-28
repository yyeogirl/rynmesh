# Contributing to Rynmesh

Rynmesh is MIT-licensed with no proprietary dependencies. This document is the
working agreement: how code gets from an idea to something running on a user's
machine.

New contributors should begin with the
[`First Contributor Starter Guide`](docs/FIRST_CONTRIBUTOR_GUIDE.md) for the
project overview, isolated development setup, first assignments, and
pre-submission checklist.

## Canonical repository

[`yeogirlyun/rynmesh`](https://github.com/yeogirlyun/rynmesh) is the source of
truth for open-source product code, accepted issues, pull-request review, and
releases. A maintainer-only repository may hold private operational material,
credentials, or non-product notes, but it must not be required to build, test,
or release Rynmesh and it must not replace public `main` as the product history.

Changes to open-source code must be incorporated through the public repository.
An external pull request merged into public `main` must be preserved by any
internal synchronization process; a later mirror must never overwrite accepted
public work. Private infrastructure details should connect through documented
configuration seams rather than being copied into this repository.

## Two hats, two entry points

The single most common mix-up: **rynmesh.ai is for using Ryn, GitHub is for
building it.** Everyone who works on Rynmesh wears both hats.

| Hat | Entry point | What you get |
|---|---|---|
| **User** (dogfooding) | Install the DMG or `install.sh` from the latest GitHub release | The released app or package, independent of your working tree. |
| **Contributor** | `git clone` + `./scripts/dev_setup.sh` | A `.venv`, an editable install, webapp deps, and a green test suite. |

Run both. The installed app is how you notice what's actually broken for a real
user; the checkout is where you fix it. They never collide — the installed node
lives in `~/.rynmesh/app` and the dev node runs from your checkout.

## Getting set up

```bash
git clone https://github.com/yeogirlyun/rynmesh.git
cd rynmesh
./scripts/dev_setup.sh
```

That creates `.venv`, installs `rynmesh` editable with dev extras, installs
webapp dependencies, and runs the tests. If the suite is red on a fresh clone,
that's a bug — please open an issue.

## Running it while you work

**Day-to-day** — two processes, hot-reloading UI:

```bash
./.venv/bin/rynmesh-peer      # node + API on :8791
cd webapp && npm run dev      # UI on :5173, proxies /api/local to the node
```

**Before you ship** — exactly what a user gets, one process:

```bash
./scripts/build_release.zsh --skip-tests   # folds the built UI into rynmesh/webui
./.venv/bin/rynmesh-peer                   # open http://127.0.0.1:8791
```

The second form matters. In dev the UI is served by Vite; in a release the node
serves it. Anything that depends on the dev proxy will look fine in dev and
break for users — always smoke-test the packaged form before opening a PR.

## How work is tracked

Rynmesh uses GitHub's open-source workflow rather than a separate private
project-management system:

- **Discussions** are for questions, early ideas, and proposals that do not yet
  have an agreed implementation boundary.
- **Issues** are the executable backlog. An accepted issue states the user
  problem, scope, acceptance criteria, risks, and verification requirements.
- **Milestones** group issues and pull requests by product outcome or release.
- **GitHub Projects** provides the cross-milestone board for Backlog, Ready, In
  progress, In review, and Done. The issue and pull request remain the source of
  truth; the board is a view of that work.
- **Pull requests** are where implementation is reviewed and incorporated.
  Decisions that affect future contributors belong in the linked issue or PR,
  not only in private chat.
- **Releases** are immutable, tested snapshots from `main`.

The current accepted backlog is the
[P1 hardening milestone](https://github.com/yeogirlyun/rynmesh/milestone/1).
Issue [#15](https://github.com/yeogirlyun/rynmesh/issues/15) is the recommended
first contribution because it establishes the webapp safety net needed by the
user-facing work that follows.

The normal issue lifecycle is:

```text
proposal -> triage -> ready -> in progress -> in review -> done
```

Maintainers triage open issues regularly. During triage they confirm the
milestone, priority, area, size, acceptance criteria, and whether design review
is required. Large work should be split into independently testable issues
before implementation begins.

[`docs/TESTING_STRATEGY.md`](docs/TESTING_STRATEGY.md) defines the evidence a
change ships with, what a user-facing feature needs beyond working code, and
the gate each milestone must clear. Read it before opening a pull request: a
review will ask for what it lists.

### Claiming work

1. Choose an available issue labeled `good first issue` or `help wanted` from
   the [contribution center](https://www.rynmesh.ai/contribute/).
2. Comment `/claim` on the issue. The contribution workflow serializes claim
   commands per issue so only one primary contributor receives the reservation.
3. Ordinary accepted work is reserved immediately for seven days. Issues
   labeled `needs design`, `privacy`, or `size:large` are locked against a
   duplicate claim but remain approval-pending until a maintainer comments
   `/approve`.
4. Post progress or open a linked draft pull request during the reservation.
   Comment `/extend` when more time is needed or `/release` when plans change.
   Inactive reservations expire and return to Available automatically.

Other engineers may still ask questions or offer focused collaboration on a
reserved item. The reservation identifies the primary implementation owner; it
does not close public discussion.

`good first issue` means the boundaries and expected tests are already known;
it does not mean tests or review are optional. Issues involving cryptography,
node identity, authentication, credit issuance, registry trust, VPN credential
sharing, or transferable credits require an approved design issue first.

### Definition of done

An issue is done only when its acceptance criteria are met, relevant automated
tests pass, user-visible behavior and existing docs agree, CI is green, and the
pull request is reviewed and merged. A draft, local experiment, or open pull
request remains In progress or In review.

## The loop

```
   feature branch ──► PR ──► CI green + review ──► merge to main
                                                        │
                                          scripts/build_release.zsh   (maintainer)
                                          push version tag vX.Y.Z
                                                        │
                                                  GitHub Releases
                                                        │
                    everyone re-runs install.sh and uses the new build
```

1. **Branch** off `main` — `feat/digest-scheduling`, `fix/watcher-dedupe`.
2. **Write a test first** where it's practical. Every bug fix should come with a
   test that fails before it and passes after.
3. **Keep the relevant checks green** before pushing:
   ```bash
   ./.venv/bin/python -m pytest tests/ -q
   ./.venv/bin/python -m ruff check rynmesh/ tests/
   cd webapp && npm test && npm run build
   ```

   During webapp development, use `npm run test:watch` from `webapp/` to rerun
   the relevant component and interaction tests as files change.
4. **Open a PR** against `main` with what changed and why, plus how you verified
   it. Screenshots for UI work.
5. **CI runs backend, webapp, packaged-node, and desktop checks.** Green CI plus
   one review approval merges.
6. **Releases are cut from `main`** by a maintainer (see below).

Direct pushes to `main` are for maintainers doing releases and trivial fixes.
Everything else goes through a PR — including maintainers' feature work, so the
history stays reviewable.

## Cutting a release (maintainers)

```bash
# 1. bump the version in pyproject.toml, commit
# 2. build: runs tests, builds the webapp into the package, and builds the wheel
./scripts/build_release.zsh

# 3. publish: push the matching version tag; GitHub Actions verifies and releases it
git tag -s vX.Y.Z -m "Rynmesh vX.Y.Z"
git push origin vX.Y.Z
```

The release workflow builds the UI-backed wheel from a clean checkout, verifies
the installed package, generates checksums and `latest.json`, and publishes the
artifacts to GitHub Releases.

Then everyone (including you) updates the way a user does:

```bash
curl -fsSL https://github.com/yeogirlyun/rynmesh/releases/latest/download/install.sh | sh
```

## House rules

- **No module over 10,000 lines.** Approaching ~8K means it's time to split by
  responsibility, not to add "one more method."
- **Stdlib-first in the node.** New third-party runtime dependencies need a
  reason in the PR description. Optional integrations (Ollama, the Anthropic
  SDK) must degrade gracefully when absent — the node always runs without them.
- **No proprietary dependencies, ever.** Third-party services plug in through
  open seams like the manifest `attestations` field. See
  `docs/DECISION_AVARYN_SEPARATION.md`.
- **Tests are the contract.** Never make a red test pass by weakening its
  assertion; decide whether the code or the test encodes the right behavior and
  fix that one.
- **One issue, one focused pull request.** Do not mix dependency upgrades,
  refactors, and unrelated behavior changes into a feature PR.
- **Document current behavior, not aspiration.** Planned capabilities belong in
  `docs/PRODUCT_MILESTONES.md` and must be labeled as planned.
- **Match the surrounding code.** Comment density, naming, and idiom included.

## Where things live

| Path | What |
|---|---|
| `rynmesh/` | The node: daemon, store, crypto, credits, transports |
| `rynmesh/services/` | Digest, model provider, messaging, egress, updater |
| `webapp/` | React UI (built into `rynmesh/webui/` at release time) |
| `tests/` | pytest suite — the release gate |
| `scripts/` | Dev setup, release build, publish |
| `docs/` | Vision, architecture, requirements, decisions, milestones |
| `rynnet/`, `sim/` | Network testbed and scale simulator |

Start with `docs/PRODUCT_MILESTONES.md` for where the project is headed, and
`docs/ARCHITECTURE.md` for how the pieces fit.
