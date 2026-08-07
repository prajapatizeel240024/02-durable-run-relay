"""The failure the operator reported, and what the service now says about it.

This started as the starter's single visible test. It asserted `status ==
"done"` and a receipt existing, which the starter passed while telling the
operator a deployment was verified when it was not. The scenario is the same;
the assertions now describe the promise instead of the symptom.

The rest of the promises are checked in tests/test_guarantees.py.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from relay import NEEDS_REVIEW, InjectedCrash, Relay


def load(name: str) -> dict:
    return json.loads(Path(f"fixtures/{name}").read_text())


class ReportedDeploymentFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        self.db_path = root / "deployments.db"
        self.provider_path = root / "fake-hubspot.json"
        self.relay = Relay(self.db_path, self.provider_path)
        self.addCleanup(self._temporary.cleanup)

    def test_reported_deployment_recovers_without_duplicate_drafts(self) -> None:
        payload = load("deployment_request.json")
        run_id = self.relay.submit("reported-deployment", payload)

        with self.assertRaises(InjectedCrash):
            self.relay.run_once(run_id, crash_at="after_first_provider_write")

        restarted = Relay(self.db_path, self.provider_path)
        restarted.recover(reclaim_after=0.0)

        state = restarted.get(run_id)
        self.assertIsNotNone(state["receipt"])
        self.assertEqual(
            len(restarted.provider.list_objects()), len(payload["assets"])
        )

    def test_the_receipt_for_that_deployment_does_not_claim_verified(self) -> None:
        """The starter said verified: true here. It was not true.

        The destination applies its own rules to the display names in this
        request, so what it stored is not what was approved. The run finishes,
        the drafts exist, and the service says so -- but it does not call it
        verified, and it names the fields that differ.
        """
        payload = load("deployment_request.json")
        run_id = self.relay.submit("reported-deployment", payload)
        receipt = self.relay.run_once(run_id)

        self.assertFalse(receipt["verified"])
        self.assertEqual(self.relay.get(run_id)["status"], NEEDS_REVIEW)

        changed = {
            item["asset_id"]
            for item in receipt["discrepancies"]
            if item["field"] == "display_name"
        }
        self.assertTrue(changed, "the rewritten names should be reported")
        for item in receipt["discrepancies"]:
            approved = item["approved"]
            stored = item["stored"]
            self.assertNotEqual(approved, stored)

    def test_every_deployed_object_matches_its_source_asset(self) -> None:
        payload = load("deployment_request.json")
        receipt = self.relay.run_once(
            self.relay.submit("integrity-check", payload)
        )
        by_id = {asset["asset_id"]: asset for asset in payload["assets"]}
        for stored in receipt["objects"]:
            asset = by_id[stored["source_asset_id"]]
            self.assertEqual(stored["source_sha256"], asset["source_sha256"])
            self.assertEqual(stored["object_type"], asset["type"])


if __name__ == "__main__":
    unittest.main()
