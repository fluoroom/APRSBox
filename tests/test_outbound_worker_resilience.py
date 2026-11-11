import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import execute, fetch_one, init_db, utc_now
from app.services.outbound import expire_stale_queued_tx_jobs
from app.services.outbound_runtime import OutboundService


class OutboundWorkerResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._previous_db = os.environ.get("APRSBOX_DB_PATH")
        os.environ["APRSBOX_DB_PATH"] = str(Path(self._temp_dir.name) / "aprsbox-test.db")
        init_db()

    async def asyncTearDown(self) -> None:
        if self._previous_db is None:
            os.environ.pop("APRSBOX_DB_PATH", None)
        else:
            os.environ["APRSBOX_DB_PATH"] = self._previous_db
        self._temp_dir.cleanup()

    def _insert_queued_job(
        self,
        *,
        kind: str = "beacon",
        scheduled_at: str | None = None,
        interface_id: int | None = None,
    ) -> int:
        timestamp = utc_now()
        execute(
            """
            INSERT INTO outbound_jobs(
                kind, interface_id, payload_json, status, scheduled_at, created_at, updated_at
            )
            VALUES (?, ?, '{}', 'queued', ?, ?, ?)
            """,
            (kind, interface_id, scheduled_at or timestamp, timestamp, timestamp),
        )
        row = fetch_one("SELECT id FROM outbound_jobs ORDER BY id DESC LIMIT 1")
        assert row is not None
        return int(row["id"])

    async def test_worker_survives_locked_database_errors(self) -> None:
        service = OutboundService(poll_interval=0.01, worker_watchdog_interval_seconds=5.0)
        locked = sqlite3.OperationalError("database is locked")
        with (
            patch("app.services.outbound_runtime.claim_next_outbound_job", side_effect=locked),
            patch("app.services.outbound_runtime.log_event", side_effect=locked),
        ):
            await service.start()
            await asyncio.sleep(0.05)
            snapshot = service.health_snapshot()
            self.assertTrue(snapshot["worker_running"])
            self.assertGreaterEqual(snapshot["consecutive_loop_errors"], 1)
            self.assertIn("database is locked", snapshot["last_loop_error"])
        await service.stop()

    async def test_start_survives_stale_cleanup_failure(self) -> None:
        service = OutboundService(poll_interval=0.01, worker_watchdog_interval_seconds=5.0)
        with patch(
            "app.services.outbound_runtime.expire_stale_queued_tx_jobs",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            await service.start()
            await asyncio.sleep(0.05)
            self.assertTrue(service.health_snapshot()["worker_running"])
        await service.stop()

    async def test_worker_recovers_after_locked_database_errors(self) -> None:
        service = OutboundService(poll_interval=0.01, worker_watchdog_interval_seconds=5.0)
        locked = sqlite3.OperationalError("database is locked")
        with patch("app.services.outbound_runtime.claim_next_outbound_job", side_effect=locked):
            await service.start()
            await asyncio.sleep(0.05)
        try:
            # Once the lock clears the same worker keeps polling instead of
            # dying with the exception.
            await asyncio.sleep(0.08)
            snapshot = service.health_snapshot()
            self.assertTrue(snapshot["worker_running"])
            self.assertIsNotNone(snapshot["worker_last_tick_age_seconds"])
        finally:
            await service.stop()

    async def test_watchdog_restarts_a_dead_worker(self) -> None:
        service = OutboundService(poll_interval=0.01, worker_watchdog_interval_seconds=0.02)
        await service.start()
        try:
            worker = service._task
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.cancel()
            await asyncio.sleep(0.12)
            snapshot = service.health_snapshot()
            self.assertGreaterEqual(snapshot["worker_restart_count"], 1)
            self.assertTrue(snapshot["worker_running"])
            self.assertTrue(snapshot["watchdog_running"])
        finally:
            await service.stop()

    async def test_watchdog_restarts_a_stalled_worker(self) -> None:
        service = OutboundService(
            poll_interval=0.01,
            worker_watchdog_interval_seconds=0.02,
            worker_stall_restart_seconds=0.05,
        )

        async def _hang(_self: OutboundService) -> None:
            await asyncio.Event().wait()

        with patch.object(OutboundService, "_run_once", _hang):
            await service.start()
            try:
                await asyncio.sleep(0.15)
                snapshot = service.health_snapshot()
                self.assertGreaterEqual(snapshot["worker_restart_count"], 1)
                self.assertTrue(snapshot["worker_running"])
            finally:
                await service.stop()

    def test_stale_queued_tx_jobs_are_cancelled(self) -> None:
        stale_id = self._insert_queued_job(scheduled_at="2020-01-01T00:00:00+00:00")
        fresh_id = self._insert_queued_job()

        cancelled = expire_stale_queued_tx_jobs(max_age_seconds=300)

        self.assertEqual(cancelled, 1)
        stale_row = fetch_one("SELECT status, last_error FROM outbound_jobs WHERE id = ?", (stale_id,))
        assert stale_row is not None
        self.assertEqual(stale_row["status"], "cancelled")
        self.assertTrue(str(stale_row["last_error"] or "").strip())
        fresh_row = fetch_one("SELECT status FROM outbound_jobs WHERE id = ?", (fresh_id,))
        assert fresh_row is not None
        self.assertEqual(fresh_row["status"], "queued")

    def test_stale_message_jobs_keep_their_own_retry_window(self) -> None:
        message_id = self._insert_queued_job(
            kind="message",
            scheduled_at="2020-01-01T00:00:00+00:00",
        )

        cancelled = expire_stale_queued_tx_jobs(max_age_seconds=300)

        self.assertEqual(cancelled, 0)
        message_row = fetch_one("SELECT status FROM outbound_jobs WHERE id = ?", (message_id,))
        assert message_row is not None
        self.assertEqual(message_row["status"], "queued")


if __name__ == "__main__":
    unittest.main()
