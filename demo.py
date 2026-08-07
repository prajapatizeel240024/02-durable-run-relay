"""What `make demo` runs.

The starter's demo crashed once, recovered, and printed a clean receipt. That
is the demo the operator did not believe. This one walks the things they
actually reported, in order, and shows what the service now says about each.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from relay import (
    IdempotencyConflict,
    InjectedCrash,
    Relay,
    RunCancelled,
)

RULE = "=" * 72


def show(label: str, value: object) -> None:
    print(f"{label}: {json.dumps(value, indent=2, default=str)}")


def heading(number: str, text: str) -> None:
    print()
    print(RULE)
    print(f"{number}  {text}")
    print(RULE)


def main() -> None:
    payload = json.loads(Path("fixtures/deployment_request.json").read_text())
    clean = json.loads(
        Path("fixtures/deployment_request_short.json").read_text()
    )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        db_path = root / "deployments.db"
        provider_path = root / "fake-hubspot.json"
        relay = Relay(db_path, provider_path)

        heading("1.", "Crash partway through, restart, recover")
        run_id = relay.submit("campaign-deploy-001", payload)
        try:
            relay.run_once(run_id, crash_at="after_first_provider_write")
        except InjectedCrash as error:
            print(f"INJECTED CRASH: {error}")

        restarted = Relay(db_path, provider_path)
        # The lease is force-expired because this demo knows the previous
        # worker is gone. A real operator either waits out the lease
        # (LEASE_SECONDS) or marks the worker dead; we do not guess.
        print("(restarted process; forcing the dead worker's lease to expire)")
        restarted.recover(reclaim_after=0.0)
        show("SUMMARY", restarted.deployment_summary(run_id))
        print(
            "drafts in the destination: "
            f"{len(restarted.provider.list_objects())}  (approved: "
            f"{len(payload['assets'])})"
        )
        print(
            "-> the crash created no duplicate drafts, because the"
            " destination key comes from the operator's idempotency key,"
            " not from the run id."
        )

        heading("2.", "Why this run is not 'verified'")
        receipt = restarted.get(run_id)["receipt"]
        for item in receipt["discrepancies"]:
            print(f"  {item['asset_id']}  field={item['field']}")
            print(f"      approved: {item['approved']!r}")
            print(f"      stored  : {item['stored']!r}")
        for clash in receipt["collisions"]:
            print(
                f"  two approved assets {clash['asset_ids']} both ended up"
                f" named {clash['display_name']!r} in the destination"
            )
        print(
            "-> the starter printed verified: true here. The destination"
            " applies its own rules to what we send, and nobody compared."
        )

        heading("3.", "The same request submitted twice (same key)")
        again = relay.submit("campaign-deploy-001", payload)
        print(f"returned the run that already exists: {again == run_id}")
        print(
            "drafts in the destination: "
            f"{len(relay.provider.list_objects())}  (still 4, not 8)"
        )

        heading("4.", "The same key with a different approved request")
        try:
            relay.submit("campaign-deploy-001", clean)
            print("accepted  <- this would be a bug")
        except IdempotencyConflict as error:
            print(f"refused: {error}")
        print(
            "drafts in the destination: "
            f"{len(relay.provider.list_objects())}  (nothing was written)"
        )

        heading("5.", "The admin panel's retry button")
        retried = relay.retry(run_id)
        print(f"retry re-armed the same deployment: {retried == run_id}")
        relay.recover(reclaim_after=0.0)
        print(
            "drafts in the destination: "
            f"{len(relay.provider.list_objects())}  (still 4, not 8)"
        )

        heading("6.", "A campaign someone cancelled")
        cancel_id = relay.submit("campaign-deploy-002", payload)
        relay.cancel(cancel_id)
        try:
            relay.run_once(cancel_id)
            print("the worker kept going  <- this would be a bug")
        except RunCancelled as error:
            print(f"the worker stopped: {error}")
        show("SUMMARY", relay.deployment_summary(cancel_id))

        heading("7.", "A request whose assets the destination does not rewrite")
        good_id = relay.submit("campaign-deploy-003", clean)
        relay.run_once(good_id)
        show("SUMMARY", relay.deployment_summary(good_id))
        print("-> 'succeeded' is reachable. It is earned by comparison.")

        heading("8.", "The destination loses two objects; operator re-checks")
        stored = json.loads(provider_path.read_text())
        prefix = Relay.external_key("campaign-deploy-003", "")
        victims = [key for key in stored if key.startswith(prefix)][:2]
        for key in victims:
            del stored[key]
        provider_path.write_text(json.dumps(stored))

        after = Relay(db_path, provider_path)
        show("CHECK AGAIN", after.audit(good_id))
        show("SUMMARY", after.deployment_summary(good_id))
        print(
            "-> the receipt still says what it said at the time. The audit"
            " re-reads the destination and disagrees with it, which is the"
            " only reason the button is worth pressing."
        )

        heading("9.", "What this still does not promise")
        for line in (
            "We cannot stop the destination changing or deleting objects"
            " after we write them. We detect it on the next audit; we do not"
            " repair it.",
            "We cannot make the destination store a value it refuses to"
            " store. We report the difference instead of accepting it"
            " silently.",
            "Serialising our writes only covers writers that go through this"
            " relay. Anything else writing to the same destination can still"
            " lose our objects.",
            "'succeeded' means the destination held what was approved at the"
            " moment we asked. It is not a promise about later.",
            "A 'failed' run is parked with its reason, not fixed. A human"
            " decides what happens next.",
        ):
            print(f"  - {line}")


if __name__ == "__main__":
    main()
