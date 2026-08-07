"""Drives the relay the way the production worker pool drives it.

Deployments are picked up by whichever worker is free, so several worker
*processes* run against the same database and the same destination at once.
Threads were not enough: the GIL hid the destination's lost-update window most
of the time. Processes show it.

The pool here is deliberately unpleasant. It contains:

  - twelve healthy deployments;
  - one deployment every worker tries to submit at the same instant under the
    same idempotency key;
  - one deployment whose destination key is already held by somebody else, so
    it fails the same way every time it is attempted;
  - one deployment an operator cancels before the pool starts.

Read what it reports. The numbers at the bottom are the point.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import tempfile
from pathlib import Path

from relay import CANCELLED, FAILED, NEEDS_REVIEW, SUCCEEDED, Relay

WORKERS = 8
HEALTHY = 12
DUPLICATE_KEY = "stress-duplicate-key"
POISON_KEY = "stress-poison"
CANCELLED_KEY = "stress-cancelled"


def worker(db: str, provider: str, payload: dict, barrier, results) -> None:
    relay = Relay(Path(db), Path(provider))
    tag = relay.worker_id
    claimed = 0
    failures: list[str] = []
    barrier.wait()

    # Every worker races to submit the same key with the same payload.
    try:
        submitted = relay.submit(DUPLICATE_KEY, payload)
    except Exception as error:  # pragma: no cover - reported, not raised
        submitted = f"{type(error).__name__}"

    # No reclaim_after here on purpose. A worker in the pool respects
    # another worker's lease; only a recovery sweep that knows a process died
    # is entitled to force one to expire. An earlier version of this harness
    # passed reclaim_after=0.0 here, every worker stole every run mid-flight,
    # and eight healthy deployments were parked as failed.
    while True:
        run_id = relay.claim_next()
        if run_id is None:
            break
        claimed += 1
        try:
            relay.run_once(run_id, claimed=True)
        except Exception as error:
            failures.append(run_id)
            relay.release(run_id, error=f"{type(error).__name__}: {error}")
    results.put((tag, claimed, failures, submitted))


def main() -> None:
    payload = json.loads(Path("fixtures/deployment_request.json").read_text())
    assets = len(payload["assets"])

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        db_path = root / "deployments.db"
        provider_path = root / "fake-hubspot.json"
        relay = Relay(db_path, provider_path)

        healthy = [
            relay.submit(f"stress-healthy-{i:03d}", payload)
            for i in range(HEALTHY)
        ]
        poison = relay.submit(POISON_KEY, payload)
        cancelled = relay.submit(CANCELLED_KEY, payload)
        relay.cancel(cancelled)

        # Somebody else already owns the destination key this run needs, so
        # every attempt at it fails the same way. Set up around the
        # destination, not inside it.
        squatted = Relay.external_key(POISON_KEY, payload["assets"][0]["asset_id"])
        state = json.loads(provider_path.read_text())
        state[squatted] = {
            "object_id": "hs-not-ours",
            "external_key": squatted,
            "source_asset_id": payload["assets"][0]["asset_id"],
            "source_sha256": "0" * 64,
            "object_type": "landing_page",
            "display_name": "created by something else",
            "status": "draft",
        }
        provider_path.write_text(json.dumps(state))

        barrier = mp.Barrier(WORKERS)
        results: mp.Queue = mp.Queue()
        procs = [
            mp.Process(
                target=worker,
                args=(str(db_path), str(provider_path), payload, barrier, results),
            )
            for _ in range(WORKERS)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join()

        rows = [results.get() for _ in procs]
        claimed = sum(row[1] for row in rows)
        failed_runs = {run_id for row in rows for run_id in row[2]}
        submitted = {row[3] for row in rows}

        # Anything left running is swept exactly as an operator would.
        for _ in range(3):
            Relay(db_path, provider_path).recover(reclaim_after=0.0)

        duplicate_run = relay.submit(DUPLICATE_KEY, payload)
        all_runs = healthy + [poison, cancelled, duplicate_run]
        statuses: dict[str, int] = {}
        for run_id in all_runs:
            status = relay.get(run_id)["status"]
            statuses[status] = statuses.get(status, 0) + 1

        expected_objects = (HEALTHY + 1) * assets + 1  # +1 squatted object
        held = len(relay.provider.list_objects())

        print(f"worker processes          : {WORKERS}")
        print(f"deployments submitted     : {len(all_runs)}")
        print(f"approved assets per run   : {assets}")
        print(f"runs claimed by a worker  : {claimed}")
        print(f"runs a worker gave back   : {len(failed_runs)}")
        print(f"distinct runs from {WORKERS} racing submits of one key: "
              f"{len(submitted)}")
        print(f"objects the destination holds: {held}")
        print(f"objects expected             : {expected_objects}")
        print(f"run status counts         : {statuses}")

        poison_state = relay.deployment_summary(poison)
        cancelled_state = relay.deployment_summary(cancelled)
        print()
        print(f"poison run    : {poison_state['status']} "
              f"(attempts={poison_state['attempts']})")
        print(f"              : {poison_state['last_error']}")
        print(f"cancelled run : {cancelled_state['status']} "
              f"-- {cancelled_state['summary']}")

        checks = [
            ("no duplicate drafts at the destination", held == expected_objects),
            ("racing submits produced one deployment", len(submitted) == 1),
            ("only the poison run was ever given back",
             failed_runs <= {poison}),
            (
                "the poison run did not block the pool",
                statuses.get(SUCCEEDED, 0) + statuses.get(NEEDS_REVIEW, 0)
                == HEALTHY + 1,
            ),
            ("the poison run was parked, not retried forever",
             relay.get(poison)["status"] == FAILED),
            ("the cancelled run stayed cancelled",
             relay.get(cancelled)["status"] == CANCELLED),
        ]
        print()
        for label, ok in checks:
            print(f"  [{'ok' if ok else 'FAIL'}] {label}")
        if not all(ok for _, ok in checks):
            raise SystemExit("stress found a broken guarantee")


if __name__ == "__main__":
    main()
