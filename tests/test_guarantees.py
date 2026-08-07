"""One test class per promise in ROADMAP.md.

If a promise stops holding, the class named after it is what goes red first.
Case numbers in docstrings are the labels in fixtures/deployment_events.jsonl.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import tempfile
import threading
import time
import unittest
from pathlib import Path

from relay import (
    CANCELLED,
    FAILED,
    LOCK_BACKEND,
    NEEDS_REVIEW,
    SUCCEEDED,
    IdempotencyConflict,
    InjectedCrash,
    InvalidRequest,
    Relay,
    RunCancelled,
    RunNotClaimable,
)


def load(name: str) -> dict:
    return json.loads(Path(f"fixtures/{name}").read_text())


class RelayCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db_path = self.root / "deployments.db"
        self.provider_path = self.root / "fake-hubspot.json"
        self.relay = Relay(self.db_path, self.provider_path)
        self.addCleanup(self._temporary.cleanup)

    def another_worker(self) -> Relay:
        return Relay(self.db_path, self.provider_path)

    def destination(self) -> dict:
        return json.loads(self.provider_path.read_text())

    def rewrite_destination(self, objects: dict) -> None:
        self.provider_path.write_text(json.dumps(objects))


# ---------------------------------------------------------------------------
# G1  One approved request, one deployment.
# ---------------------------------------------------------------------------
class OneRequestOneDeployment(RelayCase):
    def test_same_key_same_payload_returns_the_same_deployment(self) -> None:
        """c2: the operator submitted deploy-202 twice and got eight drafts."""
        payload = load("deployment_request.json")
        first = self.relay.submit("deploy-202", payload)
        second = self.relay.submit("deploy-202", payload)
        self.assertEqual(first, second)

        self.relay.run_once(first)
        self.assertEqual(
            len(self.relay.provider.list_objects()), len(payload["assets"])
        )

    def test_same_key_different_payload_is_refused_and_writes_nothing(self) -> None:
        """c5: deploy-303 arrived twice with payload A then payload B."""
        self.relay.submit("deploy-303", load("deployment_request.json"))
        with self.assertRaises(IdempotencyConflict):
            self.relay.submit("deploy-303", load("deployment_request_short.json"))
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_different_keys_are_different_deployments(self) -> None:
        """c3 is the control. Two intents are two deployments, not a duplicate.

        A rule that deduplicates on payload content instead of on the
        operator's key would collapse these and silently drop a campaign.
        """
        payload = load("deployment_request.json")
        first = self.relay.submit("deploy-207a", payload)
        second = self.relay.submit("deploy-207b", payload)
        self.assertNotEqual(first, second)
        self.relay.run_once(first)
        self.relay.run_once(second)
        self.assertEqual(
            len(self.relay.provider.list_objects()),
            2 * len(payload["assets"]),
        )

    def test_racing_submits_under_one_key_make_one_deployment(self) -> None:
        payload = load("deployment_request.json")
        seen: list[str] = []
        barrier = threading.Barrier(6)

        def submit() -> None:
            worker = self.another_worker()
            barrier.wait()
            try:
                seen.append(worker.submit("deploy-race", payload))
            except IdempotencyConflict:  # pragma: no cover - would be a bug
                seen.append("conflict")

        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(seen)), 1, seen)


# ---------------------------------------------------------------------------
# G2  One draft per approved asset, per deployment, across crash and retry.
# ---------------------------------------------------------------------------
class OneDraftPerApprovedAsset(RelayCase):
    def test_destination_key_does_not_depend_on_the_run_id(self) -> None:
        """The whole of c2 and c4 comes from a key derived from the run id."""
        first = Relay.external_key("deploy-404", "asset-lp-001")
        second = Relay.external_key("deploy-404", "asset-lp-001")
        self.assertEqual(first, second)
        self.assertNotEqual(
            first, Relay.external_key("deploy-405", "asset-lp-001")
        )

    def test_crash_and_recover_creates_no_duplicate_drafts(self) -> None:
        """c1: crash after a provider write, before the local receipt."""
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-101", payload)
        with self.assertRaises(InjectedCrash):
            self.relay.run_once(run_id, crash_at="after_first_provider_write")

        restarted = self.another_worker()
        restarted.recover(reclaim_after=0.0)
        self.assertEqual(
            len(restarted.provider.list_objects()), len(payload["assets"])
        )

    def test_admin_retry_reuses_the_same_deployment(self) -> None:
        """c4: a stalled run, then the admin panel's retry button."""
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-404", payload)
        self.relay.run_once(run_id)

        retried = self.relay.retry(run_id)
        self.assertEqual(retried, run_id)
        self.relay.recover(reclaim_after=0.0)
        self.assertEqual(
            len(self.relay.provider.list_objects()), len(payload["assets"])
        )

    def test_a_finished_deployment_cannot_be_run_again(self) -> None:
        """retry() on a finished deployment is a no-op, not a second run."""
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-flat", payload)
        self.relay.run_once(run_id)
        self.assertEqual(self.relay.get(run_id)["status"], SUCCEEDED)

        for _ in range(4):
            self.assertEqual(self.relay.retry(run_id), run_id)
            with self.assertRaises(RunNotClaimable):
                self.relay.run_once(run_id)
        self.assertEqual(
            len(self.relay.provider.list_objects()), len(payload["assets"])
        )

    def test_repeated_recovery_passes_are_flat(self) -> None:
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-flat-recover", payload)
        with self.assertRaises(InjectedCrash):
            self.relay.run_once(run_id, crash_at="after_first_provider_write")
        for _ in range(5):
            self.relay.recover(reclaim_after=0.0)
        self.assertEqual(
            len(self.relay.provider.list_objects()), len(payload["assets"])
        )

    def test_ambiguous_provider_error_is_resolved_by_reading_back(self) -> None:
        """c10: a write timed out and had in fact landed.

        The wrapper is failed around the destination, not inside it: the write
        lands and then the call raises, exactly as a gateway timeout looks.
        """
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-timeout", payload)

        real_create = self.relay.provider.create_draft
        state = {"first": True}

        def flaky(**kwargs: object) -> dict:
            result = real_create(**kwargs)
            if state["first"]:
                state["first"] = False
                raise TimeoutError("gateway_timeout")
            return result

        self.relay.provider.create_draft = flaky  # type: ignore[assignment]
        receipt = self.relay.run_once(run_id)
        self.assertTrue(receipt["verified"])
        self.assertEqual(
            len(self.relay.provider.list_objects()), len(payload["assets"])
        )


# ---------------------------------------------------------------------------
# G3  One worker at a time on a deployment.
# ---------------------------------------------------------------------------
class OneWorkerAtATime(RelayCase):
    def test_only_one_of_two_racing_workers_runs_a_deployment(self) -> None:
        """c8: run-14 was claimed by w-a and w-b."""
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-808", payload)
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(tag: str) -> None:
            worker = self.another_worker()
            barrier.wait()
            try:
                worker.run_once(run_id, worker_id=tag)
                outcomes.append("ran")
            except RunNotClaimable:
                outcomes.append("refused")

        threads = [
            threading.Thread(target=attempt, args=(tag,))
            for tag in ("w-a", "w-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ran", "refused"])

    def test_a_live_lease_is_not_stealable(self) -> None:
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-lease", payload)
        self.relay._claim(run_id, worker_id="w-a")
        self.assertIsNone(self.another_worker().claim_next())

    def test_an_expired_lease_is_stealable(self) -> None:
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-lease", payload)
        self.relay._claim(run_id, worker_id="w-a")
        self.assertEqual(
            self.another_worker().claim_next(reclaim_after=0.0), run_id
        )

    def test_a_superseded_worker_may_not_declare_success(self) -> None:
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-steal", payload)
        self.relay._claim(run_id, worker_id="w-a")
        self.another_worker()._claim(
            run_id, worker_id="w-b", reclaim_after=0.0
        )
        with self.assertRaises(RunNotClaimable):
            self.relay.run_once(run_id, worker_id="w-a", claimed=True)


# ---------------------------------------------------------------------------
# G4  Cancellation is immediate and final.
# ---------------------------------------------------------------------------
class CancellationSticks(RelayCase):
    def test_cancel_before_the_worker_starts_stops_it(self) -> None:
        """c6: a campaign someone cancelled kept going."""
        run_id = self.relay.submit("deploy-606", load("deployment_request.json"))
        self.assertTrue(self.relay.cancel(run_id))
        with self.assertRaises(RunCancelled):
            self.relay.run_once(run_id)
        self.assertEqual(self.relay.get(run_id)["status"], CANCELLED)
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_cancel_midflight_stops_further_writes_and_sticks(self) -> None:
        payload = load("deployment_request.json")
        run_id = self.relay.submit("deploy-616", payload)

        real_write = self.relay._write_one
        written = {"count": 0}

        def write_then_cancel(external: str, asset: dict) -> dict:
            result = real_write(external, asset)
            written["count"] += 1
            if written["count"] == 1:
                self.another_worker().cancel(run_id)
            return result

        self.relay._write_one = write_then_cancel  # type: ignore[assignment]
        with self.assertRaises(RunCancelled):
            self.relay.run_once(run_id)

        self.assertEqual(self.relay.get(run_id)["status"], CANCELLED)
        self.assertEqual(len(self.relay.provider.list_objects()), 1)

    def test_cancel_reports_the_drafts_it_could_not_unmake(self) -> None:
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-626", payload)
        self.relay.run_once(run_id)
        self.assertFalse(self.relay.cancel(run_id))
        self.assertEqual(self.relay.get(run_id)["status"], SUCCEEDED)

    def test_recovery_does_not_resurrect_a_cancelled_run(self) -> None:
        run_id = self.relay.submit("deploy-636", load("deployment_request.json"))
        self.relay.cancel(run_id)
        self.relay.recover(reclaim_after=0.0)
        self.assertEqual(self.relay.get(run_id)["status"], CANCELLED)


# ---------------------------------------------------------------------------
# G5  "Succeeded" means read back and compared.
# ---------------------------------------------------------------------------
class SuccessIsEarned(RelayCase):
    def test_destination_rewrites_are_reported_not_hidden(self) -> None:
        """c7: the receipt said verified; the drafts were not what we approved."""
        payload = load("deployment_request.json")
        receipt = self.relay.run_once(self.relay.submit("deploy-707", payload))
        self.assertFalse(receipt["verified"])
        self.assertEqual(self.relay.get(self.relay.submit(
            "deploy-707", payload))["status"], NEEDS_REVIEW)

        changed = {
            item["asset_id"]
            for item in receipt["discrepancies"]
            if item["field"] == "display_name"
        }
        approved_names = {
            asset["asset_id"]: asset["display_name"]
            for asset in payload["assets"]
        }
        for asset_id in changed:
            stored = next(
                obj for obj in receipt["objects"]
                if obj["source_asset_id"] == asset_id
            )
            self.assertNotEqual(stored["display_name"], approved_names[asset_id])

    def test_a_request_the_destination_does_not_rewrite_verifies(self) -> None:
        """The short fixture must not be dragged down by the rule above."""
        payload = load("deployment_request_short.json")
        receipt = self.relay.run_once(self.relay.submit("deploy-777", payload))
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["discrepancies"], [])
        self.assertEqual(receipt["collisions"], [])

    def test_two_assets_that_end_up_with_one_name_are_reported(self) -> None:
        payload = load("deployment_request.json")
        receipt = self.relay.run_once(self.relay.submit("deploy-717", payload))
        self.assertTrue(
            receipt["collisions"],
            "two approved assets share a stored name; the operator cannot"
            " tell the drafts apart and should be told",
        )

    def test_an_empty_request_is_not_vacuously_verified(self) -> None:
        """`all([])` is True. Nothing here may lean on that."""
        payload = load("deployment_request_empty.json")
        run_id = self.relay.submit("deploy-000", payload)
        receipt = self.relay.run_once(run_id)
        summary = self.relay.deployment_summary(run_id)
        self.assertEqual(receipt["assets_approved"], 0)
        self.assertEqual(receipt["objects_at_destination"], 0)
        self.assertEqual(summary["objects_at_destination"], 0)
        self.assertIn("0 approved assets", summary["summary"])

    def test_a_partial_run_is_never_reported_as_verified(self) -> None:
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-partial", payload)
        with self.assertRaises(InjectedCrash):
            self.relay.run_once(run_id, crash_at="after_first_provider_write")
        summary = self.relay.deployment_summary(run_id)
        self.assertFalse(summary["verified"])
        self.assertLess(
            summary["objects_at_destination"], summary["assets_approved"]
        )


# ---------------------------------------------------------------------------
# G6  "Check again" re-reads the destination.
# ---------------------------------------------------------------------------
class AuditReadsTheDestination(RelayCase):
    def test_audit_detects_objects_deleted_at_the_destination(self) -> None:
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-909", payload)
        self.relay.run_once(run_id)
        self.assertTrue(self.relay.audit(run_id)["verified"])

        stored = self.destination()
        for key in list(stored)[:2]:
            del stored[key]
        self.rewrite_destination(stored)

        audit = self.another_worker().audit(run_id)
        self.assertFalse(audit["verified"])
        self.assertEqual(len(audit["missing"]), 2)
        self.assertFalse(audit["still_matches_receipt"])

    def test_audit_detects_unexpected_extra_drafts(self) -> None:
        """The operator's 'sometimes there are more drafts than we approved'."""
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-919", payload)
        self.relay.run_once(run_id)

        stored = self.destination()
        stray = Relay.external_key("deploy-919", "asset-not-approved")
        stored[stray] = {
            "object_id": "hs-stray",
            "external_key": stray,
            "source_asset_id": "asset-not-approved",
            "source_sha256": "0" * 64,
            "object_type": "email",
            "display_name": "not approved by anyone",
            "status": "draft",
        }
        self.rewrite_destination(stored)

        audit = self.another_worker().audit(run_id)
        self.assertEqual(audit["unexpected"], [stray])
        self.assertFalse(audit["verified"])

    def test_audit_does_not_read_the_receipt_for_its_counts(self) -> None:
        payload = load("deployment_request_short.json")
        run_id = self.relay.submit("deploy-929", payload)
        self.relay.run_once(run_id)
        self.rewrite_destination({})
        audit = self.another_worker().audit(run_id)
        self.assertEqual(audit["objects_found_now"], 0)


# ---------------------------------------------------------------------------
# G7  One bad deployment cannot block the others.
# ---------------------------------------------------------------------------
class OneBadRunCannotBlockTheQueue(RelayCase):
    def poison(self, key: str) -> str:
        """A run whose destination key is already held by somebody else."""
        payload = load("deployment_request.json")
        run_id = self.relay.submit(key, payload)
        squatted = Relay.external_key(key, payload["assets"][0]["asset_id"])
        stored = self.destination()
        stored[squatted] = {
            "object_id": "hs-not-ours",
            "external_key": squatted,
            "source_asset_id": payload["assets"][0]["asset_id"],
            "source_sha256": "0" * 64,
            "object_type": "landing_page",
            "display_name": "created by something else",
            "status": "draft",
        }
        self.rewrite_destination(stored)
        return run_id

    def test_a_failing_run_does_not_block_the_ones_behind_it(self) -> None:
        """c9: run-16 and run-17 were submitted after run-15 and never went out."""
        bad = self.poison("deploy-505")
        behind = [
            self.relay.submit("deploy-506", load("deployment_request_short.json")),
            self.relay.submit("deploy-507", load("deployment_request_short.json")),
        ]
        self.relay.recover(reclaim_after=0.0)
        for run_id in behind:
            self.assertIn(
                self.relay.get(run_id)["status"], (SUCCEEDED, NEEDS_REVIEW)
            )
        self.assertNotEqual(self.relay.get(bad)["status"], SUCCEEDED)

    def test_a_failing_run_is_parked_with_its_reason(self) -> None:
        bad = self.poison("deploy-515")
        for _ in range(4):
            self.relay.recover(reclaim_after=0.0)
        run = self.relay.get(bad)
        self.assertEqual(run["status"], FAILED)
        self.assertIn("IdempotencyConflict", str(run["last_error"]))
        self.assertIn("Given up", self.relay.deployment_summary(bad)["summary"])

    def test_a_parked_run_stops_consuming_attempts(self) -> None:
        bad = self.poison("deploy-525")
        for _ in range(6):
            self.relay.recover(reclaim_after=0.0)
        self.assertEqual(self.relay.get(bad)["attempts"], 3)

    def test_retry_un_parks_a_failed_run_for_a_human(self) -> None:
        bad = self.poison("deploy-535")
        for _ in range(4):
            self.relay.recover(reclaim_after=0.0)
        self.assertEqual(self.relay.get(bad)["status"], FAILED)
        self.relay.retry(bad)
        self.assertEqual(self.relay.get(bad)["attempts"], 0)


# ---------------------------------------------------------------------------
# G8  Our own workers do not destroy each other's writes.
# ---------------------------------------------------------------------------
def _hammer(db: str, provider: str, barrier, tag: str, count: int) -> None:
    relay = Relay(Path(db), Path(provider))
    barrier.wait()
    for index in range(count):
        relay.provider.create_draft(
            external_key=f"{tag}:{index}",
            asset={
                "asset_id": f"asset-{index}",
                "source_sha256": "0" * 64,
                "type": "email",
                "display_name": "concurrent",
            },
        )


@unittest.skipIf(
    LOCK_BACKEND is None,
    "no cross-process file lock on this platform, so G8 does not hold here",
)
class ConcurrentWritersDoNotLoseObjects(RelayCase):
    def test_eight_worker_processes_lose_nothing(self) -> None:
        """The destination updates one file with a read-modify-write.

        Under threads the GIL hides this most of the time. Under processes the
        unguarded version loses most of the writes and leaves the state file
        unparseable.
        """
        processes, per_process = 8, 30
        barrier = mp.Barrier(processes)
        workers = [
            mp.Process(
                target=_hammer,
                args=(
                    str(self.db_path),
                    str(self.provider_path),
                    barrier,
                    f"w{index}",
                    per_process,
                ),
            )
            for index in range(processes)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        for worker in workers:
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(
            len(self.relay.provider.list_objects()), processes * per_process
        )


# ---------------------------------------------------------------------------
# Requests we refuse before writing anything.
# ---------------------------------------------------------------------------
class RequestsWeRefuse(RelayCase):
    def test_duplicate_asset_ids_are_refused(self) -> None:
        payload = load("deployment_request.json")
        payload["assets"][1]["asset_id"] = payload["assets"][0]["asset_id"]
        with self.assertRaises(InvalidRequest):
            self.relay.submit("deploy-dupe", payload)
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_a_missing_required_field_is_refused(self) -> None:
        payload = load("deployment_request.json")
        del payload["assets"][2]["source_sha256"]
        with self.assertRaises(InvalidRequest):
            self.relay.submit("deploy-missing", payload)
        self.assertEqual(self.relay.provider.list_objects(), [])

    def test_a_request_with_no_assets_list_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.relay.submit("deploy-shapeless", {"destination": "hubspot"})


if __name__ == "__main__":
    unittest.main()
