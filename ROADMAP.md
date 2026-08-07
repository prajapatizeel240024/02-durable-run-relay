# Roadmap

Written before any source or test file was edited. Everything below comes from
reading `relay/core.py`, reading the fixtures, and _running_ the unmodified
starter. Running is not editing; several of the things here are invisible in
the source and only showed up when I put the starter into states `make demo`
never reaches.

---

## 1. What the evidence actually says

### 1.1 The starter's own demo hides its worst bug

`make demo` prints `verified: true` and then dumps the receipt. Reading that
dump line by line, against the fixture:

| approved                                                        | stored by the destination                  |
| --------------------------------------------------------------- | ------------------------------------------ |
| `Summer 2026 ABM campaign - primary landing page`               | `Summer 2026 ABM campaign - primary landi` |
| `Summer 2026 ABM campaign email - product deep dive`            | `Summer 2026 ABM campaign email - product` |
| `Summer 2026 ABM campaign email - product deep dive, part two ` | `Summer 2026 ABM campaign email - product` |

Three of four approved names come back different. Two come back **identical to
each other**, so the marketer sees two drafts in HubSpot they cannot tell
apart. `FakeHubSpot._store_display_name` strips and truncates to 40 characters;
the relay stores the read-back object in the receipt and never compares it to
anything. `verified` is the literal `True` on line 209.

`fixtures/deployment_request_short.json` has names of 15–18 characters and is
rewritten not at all. That fixture is there to catch a rule that fires on
everything.

### 1.2 The destination loses writes, and it is worse than `make stress` shows

`make stress` reports `objects the provider holds: 48` most of the time, and
occasionally 47, and occasionally dies with `JSONDecodeError: Extra data`. That
looked like noise until I drove the same provider from eight **processes**
instead of threads:

```
writes issued : 320
objects held  : 41
LOST          : 279
  child w7 error: JSONDecodeError   (7 of 8 workers died this way)
```

`_load()` → mutate → `_save()` is a read-modify-write on one JSON file with no
locking. Under the GIL the window is narrow and threads mostly get away with
it. Across processes it is wide open, writes vanish, and the state file ends up
unparseable. This is the operator's _"once there were fewer, and the receipt
still said verified"_, and it is the single finding I would not have got from
reading the code.

### 1.3 Reproducing the rest against the unmodified starter

| case | what I ran                                                 | what happened                                                     |
| ---- | ---------------------------------------------------------- | ----------------------------------------------------------------- |
| c2   | `submit("deploy-202", A)` twice, run both                  | 8 drafts for 4 approved assets                                    |
| c3   | `submit("deploy-207a", A)`, `submit("deploy-207b", A)`     | 8 drafts                                                          |
| c4   | run, then `retry()`                                        | 8 drafts; the stalled run reports `done`                          |
| c5   | `submit("deploy-303", A)` then `(…, B)`                    | accepted silently, two runs                                       |
| c6   | `cancel()` then `run_once()`                               | ends `done`, 4 drafts written anyway                              |
| c8   | two threads, one run                                       | both ran, nothing refused                                         |
| c9   | poison run in front of two healthy ones                    | `recover()` raised, **both queued runs stayed `pending` forever** |
| —    | empty fixture                                              | `verified: true`, `all_present: true`, 0 objects                  |
| —    | delete 2 objects from the destination, press "Check again" | `all_present: true`, `checked_objects: 4`                         |

c9 reproduced the operator's sentence word for word: two campaigns behind the
stuck one never went out. `recover()` is one loop with no `try`; the first
exception aborts the pass and everything behind it is stranded.

---

## 2. Grouping the eleven cases

Six faults, three non-faults, one out of scope. The grouping is the part I most
expect to be argued with, so the reasoning is spelled out.

| case       | group                                             | fault                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| c1         | **baseline, works**                               | crash-and-restart is already safe, because the run id survives the restart. It is the only path the demo exercises, which is why the starter looks fine.                                                                                                                                                                                                                                         |
| c2, c4, c5 | **F1 — the deployment has no stable identity**    | `submit()` mints a fresh UUID per call and ignores the idempotency key; `retry()` mints another. The destination key is `f"{run_id}:{asset_id}"`, so every new run id is a new name for the same approved asset and the destination stores it again. c5 is the same root cause seen from the other side: the key carries no meaning, so reusing it with a different payload is not even noticed. |
| c6         | **F2 — cancellation is advisory**                 | `cancel()` writes a status nobody reads. The write loop never checks it, and the completion step is an unconditional `SET status='done'` that overwrites it.                                                                                                                                                                                                                                     |
| c7         | **F3 — the receipt is a self-report**             | `verified: True` is a literal. The read-back is stored and never compared. `audit()` re-reads the _receipt_, not the destination, so "Check again" can only ever agree with itself.                                                                                                                                                                                                              |
| c8         | **F4 — no claim, and F5 — concurrent writers**    | two workers on one run because `run_once` sets `status='running'` with no compare-and-swap; receipt 4 vs destination 3 because of §1.2. These are two different faults that show up in one case.                                                                                                                                                                                                 |
| c9         | **F6 — recovery has no isolation and no give-up** | one `try`-less loop, no attempt counter, no terminal failure state.                                                                                                                                                                                                                                                                                                                              |
| c3         | **not a fault**                                   | two different idempotency keys are two different operator intents. Eight drafts is the correct answer. Any dedupe rule keyed on payload content would collapse these and silently drop a campaign.                                                                                                                                                                                               |
| c10        | **not a fault, corroborating**                    | a write returned `gateway_timeout` and the read-back found the object. This is evidence that destination outcomes are ambiguous, which is _why_ F1 is dangerous: retrying under an unstable key turns an ambiguous write into a duplicate.                                                                                                                                                       |
| c11        | **out of scope**                                  | `thumbnail_render` / `transient_timeout`. A different subsystem, no run in the deployment log, and nothing in the operator's report. This is the unrelated one.                                                                                                                                                                                                                                  |

---

## 3. The promise

> **A deployment is one approved request, identified by the operator's
> idempotency key. Every approved asset becomes at most one draft in the
> destination, no matter how many times the process crashes, restarts, or is
> retried. The service calls a deployment `succeeded` only after it has read
> every one of those drafts back out of the destination and found it matching
> what was approved. Anything else gets a status that says what is wrong.**

Eight guarantees, each with the check that goes red first.

| #   | guarantee                                                                                                                                                                         | first check to fail                                 |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| G1  | One idempotency key, one deployment. Same key + same payload returns the existing run; same key + different payload is refused and writes nothing; different keys stay different. | `OneRequestOneDeployment`                           |
| G2  | One draft per approved asset per deployment, stable across crash, restart, recovery and admin retry.                                                                              | `OneDraftPerApprovedAsset`                          |
| G3  | One worker at a time, decided by a conditional UPDATE in the database, with a lease that expires.                                                                                 | `OneWorkerAtATime`                                  |
| G4  | Cancellation is immediate and final, and drafts already written are reported, not hidden.                                                                                         | `CancellationSticks`                                |
| G5  | `succeeded` is the _result_ of a field-by-field comparison, never an assertion.                                                                                                   | `SuccessIsEarned`                                   |
| G6  | "Check again" re-reads the destination, not the receipt.                                                                                                                          | `AuditReadsTheDestination`                          |
| G7  | A run that keeps failing is parked with its reason and does not block the queue.                                                                                                  | `OneBadRunCannotBlockTheQueue`                      |
| G8  | Our own workers do not destroy each other's writes at the destination.                                                                                                            | `ConcurrentWritersDoNotLoseObjects` + `make stress` |

### What this will still not promise

- We cannot stop the destination changing or deleting objects after we write
  them. We detect it at the next audit. We do not repair it.
- We cannot make the destination store a value it refuses to store. We report
  the difference; we do not silently accept it and we do not quietly rename
  the operator's asset to fit.
- G8 only covers writers that go through this relay. Anything else writing to
  the same destination can still lose our objects, and only G6 will notice.
- `succeeded` means the destination held what was approved _at the moment we
  asked_. It is not a claim about five minutes later.
- A `failed` run is parked, not fixed. A human decides.
- Lease expiry is wall-clock. Badly skewed clocks across worker machines could
  still allow a double claim.
- On a platform with neither `flock` nor `msvcrt`, G8 does not hold at all. The
  check for it skips with that reason rather than passing quietly.
- c11 is untouched.

---

## 4. Plan, in order

1. `ROADMAP.md`, committed on its own. _(this file)_
2. **G1.** `UNIQUE` on `idempotency_key`; `submit()` returns the existing run on
   a matching payload and raises `IdempotencyConflict` otherwise. Validate the
   request before writing anything.
3. **G2.** Derive the destination key from the idempotency key, not the run id.
   Make `retry()` re-arm the same deployment instead of creating a second one.
4. **G3.** Lease columns and a compare-and-swap claim; `claim_next()` for the
   pool; completion is conditional on still holding the lease.
5. **G4.** Check cancellation before every write; make completion refuse to
   move a cancelled run.
6. **G5/G6.** One `_inspect()` that reads the destination and compares against
   the approved payload; `run_once`, `deployment_summary` and `audit` all use
   it. Add `needs_review` as a terminal status.
7. **G7.** Per-run isolation in `recover()`, an attempt counter, and a terminal
   `failed` state.
8. **G8.** A cross-process file lock held _around_ the destination client:
   `flock` on POSIX, `msvcrt.locking` on Windows. Both are released by the OS
   on process exit, which is what makes them safe against a killed worker.
9. Rewrite `demo.py` to walk the operator's report; rewrite `stress.py` as a
   contended multi-process pool that asserts the guarantees and exits non-zero.
10. Mutation-check the tests: break each guarantee in turn, confirm the test
    named after it goes red.

### Deliberately not doing

- **Not pre-validating names against the destination's 40-character limit**,
  even though `DISPLAY_NAME_LIMIT` is right there. Copying the destination's
  current rules into our validator makes us wrong the moment they change it,
  and it is the kind of coupling that looks like a fix and rots quietly. Write,
  read back, compare, report.
- **Not auto-renaming assets to fit.** Nobody approved the shortened name.
- **Not touching `FakeHubSpot`.** It is the destination. Everything about its
  behaviour is handled around it.
- **Not touching c11.**
- **Not building a real queue.** `claim_next()` over SQLite is enough to show
  the guarantee; anything more is a different exercise.

---

## 5. Claims here another engineer could reasonably reject

1. **c3 is not a bug.** I am asserting that two different idempotency keys with
   byte-identical payloads must produce eight drafts. Someone who thinks the
   operator's real intent is "one campaign, one set of drafts" would dedupe on
   content and would call my behaviour the bug.
2. **Truncation should make the run `needs_review`, not `succeeded`.** Every
   campaign with a name over 40 characters will now stop and ask for a human.
   That is a real alert-fatigue cost, and someone could argue the drafts are
   fine and this is a paperwork failure being escalated to an incident.
3. **Serialising our own writes to the destination is a _symptom_ fix.** I
   expect to be told it is a cause fix. My reasoning: the cause is a
   destination that is not safe for concurrent writers, and I am not allowed to
   change that. I remove _us_ as a cause of the loss; the destination remains
   as capable of losing objects as it ever was, which is why G6 exists.
4. **Cancelling a finished deployment is refused rather than best-effort
   undone.** The drafts exist. Reporting them is more honest than deleting
   things nobody asked us to delete.
5. **Wall-clock leases with no heartbeat table.** Simple, and wrong under clock
   skew. I am taking the skew.

---

## 6. How I will know it worked

- `make test` — one test class per guarantee, and each one demonstrably fails
  when its guarantee is broken.
- `make stress` — a contended eight-process pool containing a duplicate-key
  race, a permanently failing run and a cancelled run; it prints its checks and
  exits non-zero if a guarantee breaks.
- `make demo` — walks the operator's report item by item and ends by printing
  what the service still cannot promise.
