# Submission

- **Transcript (file or link):** `transcript/` — raw agent session, dead ends
  included

- **`make demo` output:** in full at the bottom of this file.

  Nine sections, walking the operator's report in order: crash and recover with
  no duplicates; why that run is not `verified` (the destination rewrote three
  of four approved names and collapsed two into one); the same key submitted
  twice; the same key with a different request; the admin retry button; a
  cancelled campaign; a request that does verify cleanly; the destination
  losing two objects and "Check again" catching it; and a list of what the
  service still cannot promise.

- **`make test` output:** 37 tests, one class per guarantee.

  ```
  Ran 37 tests in 8.877s
  OK
  ```

  Verbose per-test output is what `make test` prints; the class names map
  one-to-one onto the guarantee table in `DECISIONS.md`.

- **`make stress` output:** eight worker processes over a shared database and
  destination, including a duplicate-key submit race, a permanently failing run
  and a cancelled run. It prints six checks and exits non-zero if any break.

  ```
  worker processes          : 8
  deployments submitted     : 15
  approved assets per run   : 4
  runs claimed by a worker  : 16
  runs a worker gave back   : 1
  distinct runs from 8 racing submits of one key: 1
  objects the destination holds: 53
  objects expected             : 53
  run status counts         : {'needs_review': 13, 'failed': 1, 'cancelled': 1}

  poison run    : failed (attempts=3)
                : IdempotencyConflict: provider key 'deploy:7ef39fb1c4031d85:asset-lp-001' was reused
  cancelled run : cancelled -- Cancelled. 0 draft(s) had already been created before the cancel and are still in the destination.

    [ok] no duplicate drafts at the destination
    [ok] racing submits produced one deployment
    [ok] only the poison run was ever given back
    [ok] the poison run did not block the pool
    [ok] the poison run was parked, not retried forever
    [ok] the cancelled run stayed cancelled
  ```

- **The claim in this submission you are least sure of, and how you checked
  it:**

  That the destination's display-name truncation deserves `needs_review` rather
  than `succeeded`.

  It is the claim with the highest ongoing cost: every campaign with a name
  over forty characters now stops and asks for a human, and a reasonable
  reviewer could call that alert fatigue over a cosmetic difference.

  What moved me was opening the destination's state file rather than the
  receipt. Two approved assets, `asset-email-002` and `asset-email-003`, are
  both stored as `Summer 2026 ABM campaign email - product`. Not similar —
  identical. A marketer looking at HubSpot cannot tell which draft is the deep
  dive and which is part two. That is not cosmetic, and the starter reported it
  as `verified: true`.

  I checked the rule does not fire on everything: `deployment_request_short.json`
  has names of 15 to 18 characters, is rewritten not at all, and verifies
  clean. `test_a_request_the_destination_does_not_rewrite_verifies` holds that
  line.

- **Anything a reviewer should know before opening the repository:**
  1. **`make demo` no longer prints a clean run, and that is the point.** The
     main fixture ends `needs_review`. `deployment_request_short.json` is there
     to show `succeeded` is reachable.
  2. **`make stress` exits non-zero when a guarantee breaks.** It is a check,
     not a report. It caught my own bug: an early version had pool workers
     passing `reclaim_after=0.0`, which force-expires live leases, and eight
     healthy deployments were parked as `failed`.
  3. **`relay/core.py`'s `FakeHubSpot` has no code changes.** Every executable
     line is the starter's; the only edit is two sentences appended to its
     docstring saying it is not ours to change. It is the destination.
     `SerializedProvider` wraps it with a cross-process lock and the
     verification logic sits outside it, so nothing about its behaviour is
     handled by touching it.
  4. **The finding I would not have got from reading the code** is in
     `ROADMAP.md` §1.2. `make stress` under threads loses an object maybe one
     run in six. Driving the same destination from eight _processes_ loses 279
     writes of 320 and leaves the state file unparseable. The GIL was hiding it.
  5. **`tests/test_visible.py` changed its assertions.** The starter's version
     asserted `status == "done"` and a receipt existing, which passed while the
     service was telling operators a deployment was verified when it was not.
     The scenario is unchanged; the assertions now describe the promise.
  6. **G8 is the one guarantee that depends on the platform.** The lock is
     `flock` on POSIX and `msvcrt.locking` on Windows; `relay.LOCK_BACKEND`
     says which. Where neither exists the guarantee does not hold and its check
     skips with that reason rather than passing. The first version of this was
     POSIX-only and fell through to a thread lock, which is no lock at all
     across processes -- it passed on Linux and corrupted the destination on
     Windows.
  7. **c11 is deliberately untouched**, with the reasoning in `ROADMAP.md` §2.

---

## `make demo` output

```
========================================================================
1.  Crash partway through, restart, recover
========================================================================
INJECTED CRASH: crashed after provider write and before local receipt
(restarted process; forcing the dead worker's lease to expire)
SUMMARY: {
  "run_id": "8ee53aa2-ddc8-4c8b-a38f-d9a6fb69941e",
  "idempotency_key": "campaign-deploy-001",
  "status": "needs_review",
  "assets_approved": 4,
  "objects_at_destination": 4,
  "verified": false,
  "attempts": 2,
  "last_error": null,
  "summary": "4 of 4 approved assets are in the destination; needs a human: 3 field(s) changed by the destination, 1 pair(s) of drafts that ended up with the same name."
}
drafts in the destination: 4  (approved: 4)
-> the crash created no duplicate drafts, because the destination key comes from the operator's idempotency key, not from the run id.

========================================================================
2.  Why this run is not 'verified'
========================================================================
  asset-lp-001  field=display_name
      approved: 'Summer 2026 ABM campaign - primary landing page'
      stored  : 'Summer 2026 ABM campaign - primary landi'
  asset-email-002  field=display_name
      approved: 'Summer 2026 ABM campaign email - product deep dive'
      stored  : 'Summer 2026 ABM campaign email - product'
  asset-email-003  field=display_name
      approved: 'Summer 2026 ABM campaign email - product deep dive, part two '
      stored  : 'Summer 2026 ABM campaign email - product'
  two approved assets ['asset-email-002', 'asset-email-003'] both ended up named 'Summer 2026 ABM campaign email - product' in the destination
-> the starter printed verified: true here. The destination applies its own rules to what we send, and nobody compared.

========================================================================
3.  The same request submitted twice (same key)
========================================================================
returned the run that already exists: True
drafts in the destination: 4  (still 4, not 8)

========================================================================
4.  The same key with a different approved request
========================================================================
refused: idempotency key 'campaign-deploy-001' was already used for a different approved request
drafts in the destination: 4  (nothing was written)

========================================================================
5.  The admin panel's retry button
========================================================================
retry re-armed the same deployment: True
drafts in the destination: 4  (still 4, not 8)

========================================================================
6.  A campaign someone cancelled
========================================================================
the worker stopped: run 35800cc0-e248-4da6-a192-ad14eb513011 was cancelled
SUMMARY: {
  "run_id": "35800cc0-e248-4da6-a192-ad14eb513011",
  "idempotency_key": "campaign-deploy-002",
  "status": "cancelled",
  "assets_approved": 4,
  "objects_at_destination": 0,
  "verified": false,
  "attempts": 0,
  "last_error": null,
  "summary": "Cancelled. 0 draft(s) had already been created before the cancel and are still in the destination."
}

========================================================================
7.  A request whose assets the destination does not rewrite
========================================================================
SUMMARY: {
  "run_id": "478fedff-bf55-4b3b-8b89-b493b813c769",
  "idempotency_key": "campaign-deploy-003",
  "status": "succeeded",
  "assets_approved": 4,
  "objects_at_destination": 4,
  "verified": true,
  "attempts": 1,
  "last_error": null,
  "summary": "All 4 approved assets are in the destination and match what was approved."
}
-> 'succeeded' is reachable. It is earned by comparison.

========================================================================
8.  The destination loses two objects; operator re-checks
========================================================================
CHECK AGAIN: {
  "run_id": "478fedff-bf55-4b3b-8b89-b493b813c769",
  "status": "succeeded",
  "checked_at": 1785883891.1538458,
  "assets_approved": 4,
  "objects_found_now": 2,
  "missing": [
    "asset-email-001",
    "asset-email-002"
  ],
  "unexpected": [],
  "discrepancies": [
    {
      "asset_id": "asset-email-001",
      "field": "object",
      "approved": "present",
      "stored": "missing"
    },
    {
      "asset_id": "asset-email-002",
      "field": "object",
      "approved": "present",
      "stored": "missing"
    }
  ],
  "collisions": [],
  "verified": false,
  "still_matches_receipt": false
}
SUMMARY: {
  "run_id": "478fedff-bf55-4b3b-8b89-b493b813c769",
  "idempotency_key": "campaign-deploy-003",
  "status": "succeeded",
  "assets_approved": 4,
  "objects_at_destination": 2,
  "verified": false,
  "attempts": 1,
  "last_error": null,
  "summary": "This deployment verified when it ran, but the destination no longer matches it: 2 of 4 approved assets are there now. Press Check again for the detail."
}
-> the receipt still says what it said at the time. The audit re-reads the destination and disagrees with it, which is the only reason the button is worth pressing.

========================================================================
9.  What this still does not promise
========================================================================
  - We cannot stop the destination changing or deleting objects after we write them. We detect it on the next audit; we do not repair it.
  - We cannot make the destination store a value it refuses to store. We report the difference instead of accepting it silently.
  - Serialising our writes only covers writers that go through this relay. Anything else writing to the same destination can still lose our objects.
  - 'succeeded' means the destination held what was approved at the moment we asked. It is not a promise about later.
  - A 'failed' run is parked with its reason, not fixed. A human decides what happens next.
```
