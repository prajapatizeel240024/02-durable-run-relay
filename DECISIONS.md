# Decisions

- **Time actually spent:** ~1h10m, including about 20 minutes on the Windows
  lock bug

- **What changed between your roadmap and what you shipped:**

  Three things.
  1. The roadmap treated c8 as one fault. It is two: no claim on the run, and
     concurrent writers destroying each other's objects at the destination.
     They need different fixes (a compare-and-swap claim, and a lock around the
     destination) and they fail independently, so they became G3 and G8.
  2. The roadmap did not anticipate that `recover()` needs a _skip_ list rather
     than a _stop_ condition. My first version broke out of the recovery loop
     when a run made no progress, which recreated c9 in new clothes — the runs
     behind the failing one stayed `pending`. Caught by running it, not by
     reading it. A failing run now gets one attempt per pass and is stepped
     over for the rest of that pass.
  3. `retry()` on a finished deployment ended up as a no-op returning the same
     run id, rather than an error. A finished deployment has nothing to retry,
     and raising would make the admin panel's button hostile.
  4. The first version of the destination lock used `fcntl` only, with a
     silent fall-through to a thread lock where `fcntl` was missing. On Windows
     that is no lock at all: eight worker processes corrupted the state file
     and `make stress` died reading it. A guarantee that quietly evaporates on
     a platform is worse than one that is absent, so the lock now dispatches to
     `msvcrt.locking` on Windows, and where neither exists `LOCK_BACKEND` is
     `None` and the check for G8 skips with that reason instead of passing.

- **What you had the coding agent do, and where you overrode it:**

  Agent: the mechanical rewrite of `core.py` once the design was settled, the
  first draft of the test bodies, and the docstrings.

  Overrode it in four places.
  1. It wanted to pre-validate `display_name` against
     `FakeHubSpot.DISPLAY_NAME_LIMIT` and reject long names at submit. That
     copies the destination's current rules into our validator and is wrong the
     moment HubSpot changes them. Write, read back, compare, report.
  2. It wanted to treat c3 as a duplicate and dedupe on payload hash. That
     collapses two separate operator intents and silently drops a campaign.
     c3 is the control case, not a fault.
  3. It wrote `stress.py` workers calling `claim_next(reclaim_after=0.0)`.
     That force-expires live leases, so every worker stole every run mid-flight
     and eight healthy deployments were parked as `failed`. The harness itself
     told me — that is why it exits non-zero.
  4. It was happy to describe the destination lock as fixing the lost-update
     bug. It stops us causing it. It does not make the destination
     transactional.

- **What your implementation actually promises an operator:**

  A deployment is one approved request, identified by the operator's
  idempotency key. Every approved asset becomes at most one draft in the
  destination, however many times the process crashes, restarts or is retried.
  The service calls a deployment `succeeded` only after reading every one of
  those drafts back out of the destination and finding it matching what was
  approved. Everything else gets a status that says what is wrong:
  `needs_review` (the drafts are there but not what was approved),
  `cancelled` (stopped, with the drafts already written reported), `failed`
  (given up after three attempts, with the reason).

- **For each promise, the check that fails first if it stops holding:**

  | promise                                              | check                                              |
  | ---------------------------------------------------- | -------------------------------------------------- |
  | one key, one deployment                              | `OneRequestOneDeployment`                          |
  | one draft per approved asset, across crash and retry | `OneDraftPerApprovedAsset`                         |
  | one worker at a time                                 | `OneWorkerAtATime`                                 |
  | cancellation is immediate and final                  | `CancellationSticks`                               |
  | `succeeded` is earned by comparison                  | `SuccessIsEarned`                                  |
  | "Check again" re-reads the destination               | `AuditReadsTheDestination`                         |
  | a bad run cannot block the queue                     | `OneBadRunCannotBlockTheQueue`                     |
  | our workers do not lose each other's writes          | `ConcurrentWritersDoNotLoseObjects`, `make stress` |

  These are not decorative. Breaking each guarantee in `core.py` one at a time
  turns the matching class red and nothing else of consequence:

  | mutation                                      | what went red                                                         |
  | --------------------------------------------- | --------------------------------------------------------------------- |
  | destination key derived from the run id again | 15 tests, led by `test_destination_key_does_not_depend_on_the_run_id` |
  | `verified = True` as a literal                | the four verification and audit tests                                 |
  | write loop stops checking cancellation        | `test_cancel_midflight_stops_further_writes_and_sticks`               |
  | recovery stops isolating failures             | the four `OneBadRunCannotBlockTheQueue` tests                         |
  | destination lock removed                      | `test_eight_worker_processes_lose_nothing`                            |

- **What you fixed at the cause, and what you only stopped from showing:**

  **Cause removed.**
  - Deployment identity is the operator's idempotency key, enforced by a
    `UNIQUE` index, and the destination key is derived from it. Duplicates
    cannot be created rather than being cleaned up afterwards. (c2, c4, c5)
  - Claiming is a conditional UPDATE. The database picks the winner, so the
    double claim cannot happen. (c8)
  - Completion is conditional on still holding the lease and on not being
    cancelled, so no worker can overwrite a cancellation. (c6)
  - `verified` is the output of a comparison, so the receipt cannot claim more
    than it checked. (c7)
  - Recovery isolates per run and parks after three attempts, so one run cannot
    strand the queue. (c9)

  **Symptom stopped, cause still there.**
  - The cross-process lock around the destination. The cause is a destination
    that is not safe for concurrent writers, and it is not mine to change. This
    removes _us_ as a cause of the loss. The destination can still lose objects
    and G6 is the only thing that will notice.
  - The read-back after an ambiguous write (c10). It does not stop the
    destination timing out; it stops us guessing wrong about which way the call
    went.
  - `needs_review` on the truncated names is _reporting_, not repair. The
    drafts in HubSpot are still not what the marketer approved. Someone has to
    shorten the names.

- **What is still unsafe, including anything that came up during the session
  and stayed open:**
  - The destination can change or delete objects after we write them. We detect
    it on the next audit; nothing repairs it, and nothing re-audits on a
    schedule. An operator has to press the button.
  - `list_objects()` scans the whole destination on every inspect. Fine at
    fixture scale, quadratic at real scale.
  - Lease expiry is wall-clock with no heartbeat table. Clock skew between
    worker machines could still allow a double claim; G8's lock would keep the
    destination consistent but the run's attempt accounting would be wrong.
  - On a platform with neither `flock` nor `msvcrt.locking` there is no
    cross-process lock at all and G8 does not hold. That is now detected --
    `LOCK_BACKEND` is `None` and the check for G8 skips with that reason --
    but detecting it is not the same as having the guarantee.
  - `recover()` is called by whoever calls it. There is no supervisor, so a
    `failed` run sits there until a human looks.
  - `reclaim_after=0.0` is a genuinely dangerous parameter that I have left in
    the public API because the demo needs it. It already bit me once.
  - c11 is untouched by choice.

- **The next failure I would inject:**

  Kill a worker process with `SIGKILL` partway through the write loop, while a
  second worker is live on other runs, and hold the destination lock at the
  moment of death. `flock` releases on process exit, so I expect it to survive;
  I have not proved it. That is the check I would write next.
