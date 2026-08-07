from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

# Which cross-process file lock we actually have. G8 -- our workers not
# destroying each other's writes at the destination -- is only enforceable if
# this is not None. It is exported so the check for G8 can skip loudly rather
# than pass quietly on a platform where the guarantee does not hold.
if fcntl is not None:
    LOCK_BACKEND: str | None = "fcntl"
elif msvcrt is not None:  # pragma: no cover - Windows
    LOCK_BACKEND = "msvcrt"
else:  # pragma: no cover - neither
    LOCK_BACKEND = None

LOCK_TIMEOUT_SECONDS = 30.0


class InjectedCrash(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class RunNotClaimable(RuntimeError):
    """Raised when a worker asks for a run somebody else is holding."""


class InvalidRequest(ValueError):
    """Raised when an approved request cannot be deployed as written."""


# ---------------------------------------------------------------------------
# Statuses. Terminal states are the ones an operator can act on.
# ---------------------------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
NEEDS_REVIEW = "needs_review"
CANCELLED = "cancelled"
FAILED = "failed"

TERMINAL = frozenset({SUCCEEDED, NEEDS_REVIEW, CANCELLED, FAILED})

LEASE_SECONDS = 30.0
MAX_ATTEMPTS = 3


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> float:
    return time.time()


class FakeHubSpot:
    """Small durable provider simulation with idempotent draft creation.

    Like the real destination, this provider owns the objects it stores. It
    accepts what you send, applies its own rules to it, and returns what it
    decided to keep.

    NOT MODIFIED. This is the destination; it is not ours to change. Everything
    we do about its behaviour is done around it, in SerializedProvider and in
    the verification step.
    """

    DISPLAY_NAME_LIMIT = 40

    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        if not self.state_path.exists():
            self.state_path.write_text("{}")

    def _load(self) -> dict[str, dict[str, str]]:
        return json.loads(self.state_path.read_text())

    def _save(self, objects: dict[str, dict[str, str]]) -> None:
        self.state_path.write_text(_canonical_json(objects))

    def _store_display_name(self, value: str) -> str:
        return str(value).strip()[: self.DISPLAY_NAME_LIMIT]

    def create_draft(
        self,
        *,
        external_key: str,
        asset: dict[str, Any],
    ) -> dict[str, str]:
        objects = self._load()
        existing = objects.get(external_key)
        candidate = {
            "object_id": f"hs-{_digest_text(external_key)[:12]}",
            "external_key": external_key,
            "source_asset_id": str(asset["asset_id"]),
            "source_sha256": str(asset["source_sha256"]),
            "object_type": str(asset["type"]),
            "display_name": self._store_display_name(asset["display_name"]),
            "status": "draft",
        }
        if existing is not None:
            if existing != candidate:
                raise IdempotencyConflict(
                    f"provider key {external_key!r} was reused"
                )
            return dict(existing)
        objects[external_key] = candidate
        self._save(objects)
        return dict(candidate)

    def read(self, external_key: str) -> dict[str, str]:
        return dict(self._load()[external_key])

    def list_objects(self) -> list[dict[str, str]]:
        return [dict(value) for value in self._load().values()]


class SerializedProvider:
    """A cross-process lock held *around* the destination client.

    The destination stores everything in one file and updates it with a
    read-modify-write. Two of our own workers calling it at the same moment
    can therefore destroy each other's writes, or catch the file half written.
    `make stress` shows this, and eight worker processes lose most of their
    writes (see ROADMAP.md).

    This wrapper does not change the destination and does not make it
    transactional. It serialises *our* callers so that we stop being the cause
    of the loss. Anything else writing to the same destination is still free to
    lose our objects, which is why audit() re-reads rather than trusting this.

    The lock is `flock` on POSIX and `msvcrt.locking` on Windows. Both are
    released by the OS when the process exits, which is what makes them safe
    against a worker being killed mid-write. If neither is available,
    LOCK_BACKEND is None and G8 does not hold on that platform -- the check
    for it skips with that reason rather than passing quietly.
    """

    def __init__(self, client: FakeHubSpot) -> None:
        self.client = client
        self._lock_path = Path(f"{client.state_path}.lock")
        self._thread_lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        # state_path, DISPLAY_NAME_LIMIT, and anything else the destination
        # exposes stay reachable.
        return getattr(self.client, name)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        # The thread lock keeps threads inside one process off each other; the
        # file lock does the same across processes. Both are needed: the file
        # lock is per-handle, so two threads sharing a handle would not see it.
        with self._thread_lock:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
            handle = os.open(self._lock_path, flags, 0o644)
            try:
                self._lock(handle)
                try:
                    yield
                finally:
                    self._unlock(handle)
            finally:
                os.close(handle)

    @staticmethod
    def _lock(handle: int) -> None:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return
        if msvcrt is not None:  # pragma: no cover - Windows
            # msvcrt has no blocking-forever mode: LK_LOCK gives up after ten
            # seconds and LK_NBLCK returns at once, so we spin ourselves.
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    os.lseek(handle, 0, os.SEEK_SET)
                    msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)
        # pragma: no cover - no OS lock available; the thread lock is all we
        # have and G8 does not hold. LOCK_BACKEND is None so the check skips.

    @staticmethod
    def _unlock(handle: int) -> None:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)

    def create_draft(self, **kwargs: Any) -> dict[str, str]:
        with self._exclusive():
            return self.client.create_draft(**kwargs)

    def read(self, external_key: str) -> dict[str, str]:
        with self._exclusive():
            return self.client.read(external_key)

    def list_objects(self) -> list[dict[str, str]]:
        with self._exclusive():
            return self.client.list_objects()


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------
REQUIRED_ASSET_FIELDS = ("asset_id", "type", "display_name", "source_sha256")


def validate_request(payload: Any) -> None:
    """Reject an approved request we cannot deploy, before we write anything.

    A request that fails here would otherwise fail partway through the write
    loop, leaving some drafts in the destination and the run stuck.
    """
    if not isinstance(payload, dict):
        raise InvalidRequest("approved request must be an object")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise InvalidRequest("approved request must carry an 'assets' list")
    seen: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise InvalidRequest(f"asset {index} is not an object")
        for field in REQUIRED_ASSET_FIELDS:
            if field not in asset or asset[field] is None:
                raise InvalidRequest(
                    f"asset {index} is missing required field {field!r}"
                )
        asset_id = str(asset["asset_id"])
        if asset_id in seen:
            raise InvalidRequest(
                f"asset_id {asset_id!r} appears twice in one request"
            )
        seen.add(asset_id)


class Relay:
    def __init__(self, db_path: Path, provider_state_path: Path) -> None:
        self.db_path = Path(db_path)
        self.provider = SerializedProvider(FakeHubSpot(provider_state_path))
        self.worker_id = f"w-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._init_db()

    # -- storage ----------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            existing = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(deployments)"
                ).fetchall()
            }
            for column, spec in (
                ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("last_error", "TEXT"),
                ("lease_owner", "TEXT"),
                ("lease_expires_at", "REAL"),
                ("created_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE deployments ADD COLUMN {column} {spec}"
                    )

    # -- identity ---------------------------------------------------------
    @staticmethod
    def external_key(idempotency_key: str, asset_id: str) -> str:
        """The destination key for one approved asset of one deployment.

        Derived from the operator's idempotency key, never from the run id.
        The run id is minted fresh on every submit and every admin retry, so a
        run-id-derived key gives the destination a different name for the same
        approved asset each time and it stores it twice. This key is stable
        across crash, restart, recovery and retry, which is also what makes a
        retry safe after an ambiguous provider error.
        """
        return f"deploy:{_digest_text(idempotency_key)[:16]}:{asset_id}"

    # -- api --------------------------------------------------------------
    def submit(self, idempotency_key: str, payload: dict[str, Any]) -> str:
        """One idempotency key, one deployment.

        Same key + same payload returns the run that already exists.
        Same key + different payload is a conflict and writes nothing.
        Different keys are different deployments, even for the same payload.
        """
        validate_request(payload)
        payload_json = _canonical_json(payload)
        payload_hash = _digest_text(payload_json)
        run_id = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO deployments
                        (id, idempotency_key, payload_hash, payload_json,
                         status, attempts, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        run_id,
                        idempotency_key,
                        payload_hash,
                        payload_json,
                        PENDING,
                        _now(),
                    ),
                )
            return run_id
        except sqlite3.IntegrityError:
            # The UNIQUE index on idempotency_key is what makes this safe when
            # two operators press the button at the same moment.
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id, payload_hash FROM deployments"
                    " WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
            if row is None:  # pragma: no cover - defensive
                raise
            if str(row["payload_hash"]) != payload_hash:
                raise IdempotencyConflict(
                    f"idempotency key {idempotency_key!r} was already used for"
                    " a different approved request"
                ) from None
            return str(row["id"])

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        receipt_json = result.pop("receipt_json")
        result["receipt"] = (
            json.loads(receipt_json) if receipt_json is not None else None
        )
        return result

    def cancel(self, run_id: str) -> bool:
        """Cancel is immediate and final, and it cannot be undone by a worker.

        Returns True if this call cancelled the run. Returns False if the run
        had already reached a terminal state -- cancelling a finished
        deployment does not unmake the drafts it created, so we refuse rather
        than pretend.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ? AND status IN (?, ?)
                """,
                (CANCELLED, run_id, PENDING, RUNNING),
            )
            changed = cursor.rowcount == 1
        if not changed:
            self.get(run_id)  # raises KeyError for an unknown run
        return changed

    def retry(self, run_id: str) -> str:
        """The admin panel's retry button.

        Re-arms the SAME deployment. It does not mint a second one. The
        starter created a new run with the operator's key, which gave the
        destination a second set of keys for the same approved assets and so
        doubled the drafts.
        """
        run = self.get(run_id)
        status = str(run["status"])
        if status in (SUCCEEDED, NEEDS_REVIEW):
            return run_id
        if status == CANCELLED:
            raise RunCancelled(f"run {run_id} was cancelled; submit a new one")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = ?, attempts = 0, last_error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ?
                """,
                (PENDING, run_id),
            )
        return run_id

    # -- claiming ---------------------------------------------------------
    def _claim(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
        reclaim_after: float | None = None,
    ) -> str:
        """Compare-and-swap claim. The database picks the winner, not us."""
        owner = worker_id or self.worker_id
        now = _now()
        cutoff = now if reclaim_after is None else now + (
            LEASE_SECONDS - reclaim_after
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = ?, lease_owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1
                WHERE id = ?
                  AND status IN (?, ?)
                  AND attempts < ?
                  AND (lease_owner IS NULL
                       OR lease_expires_at IS NULL
                       OR lease_expires_at <= ?)
                """,
                (
                    RUNNING,
                    owner,
                    now + LEASE_SECONDS,
                    run_id,
                    PENDING,
                    RUNNING,
                    MAX_ATTEMPTS,
                    cutoff,
                ),
            )
            granted = cursor.rowcount == 1
        if not granted:
            run = self.get(run_id)
            if str(run["status"]) == CANCELLED:
                # Say the specific true thing. A caller that gets "somebody
                # else holds it" will retry; a caller that gets "cancelled"
                # will stop, which is what we want.
                raise RunCancelled(f"run {run_id} was cancelled")
            raise RunNotClaimable(
                f"run {run_id} is {run['status']} and held by"
                f" {run['lease_owner']!r} (attempts={run['attempts']})"
            )
        return owner

    def claim_next(
        self,
        *,
        worker_id: str | None = None,
        reclaim_after: float | None = None,
        exclude: set[str] | None = None,
    ) -> str | None:
        """What a worker in the pool calls: give me something to do.

        `exclude` lets a recovery pass skip a run it has already tried in this
        pass, so a run that fails every time gets one attempt per pass instead
        of monopolising the loop.
        """
        owner = worker_id or self.worker_id
        skip = exclude or set()
        now = _now()
        cutoff = now if reclaim_after is None else now + (
            LEASE_SECONDS - reclaim_after
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM deployments
                WHERE status IN (?, ?)
                  AND attempts < ?
                  AND (lease_owner IS NULL
                       OR lease_expires_at IS NULL
                       OR lease_expires_at <= ?)
                ORDER BY created_at, id
                """,
                (PENDING, RUNNING, MAX_ATTEMPTS, cutoff),
            ).fetchall()
        for row in rows:
            run_id = str(row["id"])
            if run_id in skip:
                continue
            try:
                self._claim(
                    run_id, worker_id=owner, reclaim_after=reclaim_after
                )
            except (RunNotClaimable, RunCancelled):
                continue  # somebody beat us to it; try the next one
            return run_id
        return None

    def release(self, run_id: str, *, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET status = CASE WHEN status = ? THEN ? ELSE status END,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?
                WHERE id = ?
                """,
                (RUNNING, PENDING, error, run_id),
            )

    def _renew(self, run_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments SET lease_expires_at = ?
                WHERE id = ? AND lease_owner = ?
                """,
                (_now() + LEASE_SECONDS, run_id, owner),
            )

    def _status_of(self, run_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM deployments WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return str(row["status"])

    # -- verification -----------------------------------------------------
    @staticmethod
    def _compare(asset: dict[str, Any], stored: dict[str, Any] | None) -> list[
        dict[str, Any]
    ]:
        asset_id = str(asset["asset_id"])
        if stored is None:
            return [{"asset_id": asset_id, "field": "object", "approved":
                     "present", "stored": "missing"}]
        pairs = (
            ("display_name", asset["display_name"], stored.get("display_name")),
            ("type", str(asset["type"]), stored.get("object_type")),
            ("source_sha256", str(asset["source_sha256"]),
             stored.get("source_sha256")),
            ("asset_id", asset_id, stored.get("source_asset_id")),
        )
        return [
            {
                "asset_id": asset_id,
                "field": field,
                "approved": approved,
                "stored": got,
            }
            for field, approved, got in pairs
            if approved != got
        ]

    def _inspect(self, run: dict[str, Any]) -> dict[str, Any]:
        """Read the destination now and compare it to what was approved.

        This is the only thing that ever sets `verified`. The starter wrote
        `"verified": True` as a literal, which is why an operator could get a
        verified receipt for drafts that were not what they approved.
        """
        key = str(run["idempotency_key"])
        approved = list(run["payload"].get("assets", []))
        prefix = f"deploy:{_digest_text(key)[:16]}:"

        held = {
            str(obj["external_key"]): obj
            for obj in self.provider.list_objects()
            if str(obj.get("external_key", "")).startswith(prefix)
        }

        objects: list[dict[str, Any]] = []
        missing: list[str] = []
        discrepancies: list[dict[str, Any]] = []
        expected_keys: set[str] = set()

        for asset in approved:
            external = self.external_key(key, str(asset["asset_id"]))
            expected_keys.add(external)
            stored = held.get(external)
            if stored is None:
                missing.append(str(asset["asset_id"]))
            else:
                objects.append(stored)
            discrepancies.extend(self._compare(asset, stored))

        unexpected = sorted(set(held) - expected_keys)

        # Two approved assets can land on one name once the destination
        # applies its own rules. The operator then cannot tell the drafts
        # apart, so we say so even though each object exists.
        by_name: dict[str, list[str]] = {}
        for obj in objects:
            by_name.setdefault(str(obj.get("display_name")), []).append(
                str(obj.get("source_asset_id"))
            )
        collisions = [
            {"display_name": name, "asset_ids": sorted(ids)}
            for name, ids in by_name.items()
            if len(ids) > 1
        ]

        verified = (
            not missing
            and not discrepancies
            and not unexpected
            and not collisions
            and len(objects) == len(approved)
        )
        return {
            "assets_approved": len(approved),
            "objects_at_destination": len(objects),
            "objects": objects,
            "missing": missing,
            "unexpected": unexpected,
            "discrepancies": discrepancies,
            "collisions": collisions,
            "verified": verified,
        }

    # -- the write loop ---------------------------------------------------
    def run_once(
        self,
        run_id: str,
        *,
        crash_at: str | None = None,
        worker_id: str | None = None,
        reclaim_after: float | None = None,
        claimed: bool = False,
    ) -> dict[str, Any]:
        owner = (
            (worker_id or self.worker_id)
            if claimed
            else self._claim(
                run_id, worker_id=worker_id, reclaim_after=reclaim_after
            )
        )
        run = self.get(run_id)
        key = str(run["idempotency_key"])

        try:
            for index, asset in enumerate(run["payload"]["assets"]):
                # Cancellation is checked before every write, not once at the
                # top, because the operator presses cancel mid-flight.
                if self._status_of(run_id) == CANCELLED:
                    raise RunCancelled(
                        f"run {run_id} was cancelled after {index} writes"
                    )
                external = self.external_key(key, str(asset["asset_id"]))
                self._write_one(external, asset)
                if crash_at == "after_first_provider_write" and index == 0:
                    raise InjectedCrash(
                        "crashed after provider write and before local receipt"
                    )
                self._renew(run_id, owner)
        except RunCancelled:
            self._finish_cancelled(run_id)
            raise

        if crash_at == "before_completion":
            raise InjectedCrash("crashed after all writes, before completion")

        inspection = self._inspect(run)
        receipt = {
            "run_id": run_id,
            "idempotency_key": key,
            "payload_sha256": run["payload_hash"],
            "checked_at": _now(),
            **inspection,
        }
        final = SUCCEEDED if inspection["verified"] else NEEDS_REVIEW
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployments
                SET status = ?, receipt_json = ?, last_error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (final, _canonical_json(receipt), run_id, RUNNING, owner),
            )
            settled = cursor.rowcount == 1
        if not settled:
            # Somebody cancelled us, or our lease expired and another worker
            # took over. Either way we are not entitled to declare success.
            current = self._status_of(run_id)
            if current == CANCELLED:
                raise RunCancelled(f"run {run_id} was cancelled mid-flight")
            raise RunNotClaimable(
                f"run {run_id} moved to {current} while we were working"
            )
        return receipt

    def _write_one(self, external: str, asset: dict[str, Any]) -> dict[str, str]:
        """Write one draft, resolving an ambiguous answer by reading back.

        Event c10 is a write that timed out and turned out to have landed. The
        destination key is stable, so a retry cannot duplicate; the read-back
        is here so we stop guessing which way an ambiguous call went.
        """
        try:
            return self.provider.create_draft(external_key=external, asset=asset)
        except IdempotencyConflict:
            raise
        except Exception:
            try:
                return self.provider.read(external)
            except Exception:
                raise

    def _finish_cancelled(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ?
                """,
                (run_id,),
            )

    # -- operator surfaces ------------------------------------------------
    def deployment_summary(self, run_id: str) -> dict[str, Any]:
        """What the dashboard shows. Every number here is re-read, not recalled."""
        run = self.get(run_id)
        inspection = self._inspect(run)
        status = str(run["status"])
        return {
            "run_id": run_id,
            "idempotency_key": run["idempotency_key"],
            "status": status,
            "assets_approved": inspection["assets_approved"],
            "objects_at_destination": inspection["objects_at_destination"],
            "verified": inspection["verified"] and status == SUCCEEDED,
            "attempts": run["attempts"],
            "last_error": run["last_error"],
            "summary": self._plain_language(status, inspection),
        }

    @staticmethod
    def _plain_language(status: str, inspection: dict[str, Any]) -> str:
        approved = inspection["assets_approved"]
        found = inspection["objects_at_destination"]
        if status == SUCCEEDED and inspection["verified"]:
            return (
                f"All {approved} approved assets are in the destination and"
                " match what was approved."
            )
        if status == SUCCEEDED:
            # This run really did verify when it finished. The destination has
            # changed underneath it since. Saying "succeeded" and nothing else
            # is how the starter lost the operator's trust.
            return (
                f"This deployment verified when it ran, but the destination no"
                f" longer matches it: {found} of {approved} approved assets"
                " are there now. Press Check again for the detail."
            )
        if status == NEEDS_REVIEW:
            bits = []
            if inspection["missing"]:
                bits.append(f"{len(inspection['missing'])} missing")
            if inspection["discrepancies"]:
                bits.append(
                    f"{len(inspection['discrepancies'])} field(s) changed by"
                    " the destination"
                )
            if inspection["unexpected"]:
                bits.append(f"{len(inspection['unexpected'])} unexpected draft(s)")
            if inspection["collisions"]:
                bits.append(
                    f"{len(inspection['collisions'])} pair(s) of drafts that"
                    " ended up with the same name"
                )
            return (
                f"{found} of {approved} approved assets are in the destination;"
                " needs a human: " + ", ".join(bits) + "."
            )
        if status == CANCELLED:
            return (
                f"Cancelled. {found} draft(s) had already been created before"
                " the cancel and are still in the destination."
            )
        if status == FAILED:
            return (
                f"Given up after {MAX_ATTEMPTS} attempts. {found} of {approved}"
                " approved assets were written."
            )
        return f"Not finished yet. {found} of {approved} written so far."

    def audit(self, run_id: str) -> dict[str, Any]:
        """The dashboard's "Check again" button.

        It re-reads the destination. It does not re-read the receipt. That is
        the entire point of the button, and it is what the starter did not do.
        """
        run = self.get(run_id)
        inspection = self._inspect(run)
        receipt = run.get("receipt") or {}
        return {
            "run_id": run_id,
            "status": run["status"],
            "checked_at": _now(),
            "assets_approved": inspection["assets_approved"],
            "objects_found_now": inspection["objects_at_destination"],
            "missing": inspection["missing"],
            "unexpected": inspection["unexpected"],
            "discrepancies": inspection["discrepancies"],
            "collisions": inspection["collisions"],
            "verified": inspection["verified"],
            "still_matches_receipt": bool(receipt) and (
                inspection["verified"] == bool(receipt.get("verified"))
                and inspection["objects_at_destination"]
                == receipt.get("objects_at_destination")
            ),
        }

    # -- recovery ---------------------------------------------------------
    def recover(
        self,
        *,
        reclaim_after: float | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Work through everything that is claimable, one run at a time.

        Every run is isolated. A run that keeps failing is parked in `failed`
        after MAX_ATTEMPTS with the reason recorded. Nothing queued behind it
        is affected -- that is the whole of case c9.
        """
        self._park_exhausted()
        result: dict[str, Any] = {
            "recovered": [],
            "cancelled": [],
            "failed": [],
            "needs_review": [],
        }
        # One attempt per run per pass. A run that fails goes in `tried` and is
        # stepped over for the rest of this pass, so the runs queued behind it
        # still go out. Its attempt counter persists, so repeated passes park
        # it rather than retrying it forever.
        tried: set[str] = set()
        errors: dict[str, str] = {}
        for _ in range(limit):
            run_id = self.claim_next(
                reclaim_after=reclaim_after, exclude=tried
            )
            if run_id is None:
                break
            tried.add(run_id)
            try:
                receipt = self.run_once(run_id, claimed=True)
            except RunCancelled:
                result["cancelled"].append(run_id)
            except Exception as error:  # one bad run cannot stop the rest
                message = f"{type(error).__name__}: {error}"
                errors[run_id] = message
                self.release(run_id, error=message)
            else:
                bucket = (
                    "recovered" if receipt["verified"] else "needs_review"
                )
                result[bucket].append(run_id)
        result["failed"] = self._park_exhausted()
        result["errors"] = errors
        return result

    def _park_exhausted(self) -> list[str]:
        """Give up, visibly, instead of retrying forever."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM deployments
                WHERE status IN (?, ?) AND attempts >= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (PENDING, RUNNING, MAX_ATTEMPTS, _now()),
            ).fetchall()
            parked = [str(row["id"]) for row in rows]
            for run_id in parked:
                connection.execute(
                    """
                    UPDATE deployments
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = ?
                    """,
                    (FAILED, run_id),
                )
        return parked
