import contextlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from app.db import execute, fetch_one, init_db

FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None


@contextlib.contextmanager
def temporary_database() -> Path:
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = Path(temp_dir) / "aprsbox-test.db"
        previous = os.environ.get("APRSBOX_DB_PATH")
        os.environ["APRSBOX_DB_PATH"] = str(database_path)
        try:
            init_db()
            yield database_path
        finally:
            if previous is None:
                os.environ.pop("APRSBOX_DB_PATH", None)
            else:
                os.environ["APRSBOX_DB_PATH"] = previous


def insert_station(*, name: str, callsign: str, ssid: str = "1", is_primary: bool = False) -> int:
    execute(
        """
        INSERT INTO stations(
            name, callsign, ssid, beacon_comment, beacon_interval_mode, beacon_interval_minutes,
            beacon_path, beacon_tx_scope, beacon_interface_id, status_enabled, status_text,
            status_interval_minutes, latitude, longitude, symbol_table, symbol_code, symbol_overlay,
            tx_enabled, is_primary, enabled, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, '', 'fixed', 30, '', 'single', NULL, 0, '', 30, '', '', '/', '>', '', 0, ?, 1, '',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
        """,
        (name, callsign, ssid, 1 if is_primary else 0),
    )
    row = fetch_one("SELECT id FROM stations WHERE name = ?", (name,))
    assert row is not None
    return int(row["id"])


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi is not installed in this environment")
class MessagesRouteTests(unittest.TestCase):
    def _client_as_admin(self):
        from fastapi.testclient import TestClient

        from app.dependencies import get_current_user
        from app.main import app
        from app.models import UserIdentity

        app.dependency_overrides[get_current_user] = lambda: UserIdentity(
            id=1,
            username="admin",
            role="admin",
            is_active=True,
        )
        return app, TestClient(app)

    def test_messages_redirects_to_primary_station_inbox_when_stations_exist(self) -> None:
        with temporary_database():
            # get_primary_station() resolves the first ENABLED station by id,
            # not the is_primary flag (see app/services/stations.py) — so the
            # first-created station is the redirect target here, not Station B.
            primary_id = insert_station(name="Station A", callsign="LU1AQY", ssid="1")
            insert_station(name="Station B", callsign="LU1AQY", ssid="3", is_primary=True)
            app, client = self._client_as_admin()
            try:
                response = client.get("/messages", follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["location"].endswith(f"/messages/{primary_id}"))
            finally:
                app.dependency_overrides.clear()

    def test_messages_station_page_redirects_to_messages_for_unknown_station(self) -> None:
        with temporary_database():
            insert_station(name="Station A", callsign="LU1AQY", ssid="1")
            app, client = self._client_as_admin()
            try:
                response = client.get("/messages/999999", follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["location"].endswith("/messages"))
            finally:
                app.dependency_overrides.clear()

    def test_messages_station_page_renders_with_station_scoped_active_nav(self) -> None:
        with temporary_database():
            station_id = insert_station(name="Station A", callsign="LU1AQY", ssid="1")
            app, client = self._client_as_admin()
            try:
                response = client.get(f"/messages/{station_id}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'data-station-id="{station_id}"', response.text)
                self.assertIn(f'data-nav-key="messages-{station_id}"', response.text)
            finally:
                app.dependency_overrides.clear()

    def test_messages_page_keeps_legacy_behavior_when_no_stations_configured(self) -> None:
        with temporary_database():
            app, client = self._client_as_admin()
            try:
                response = client.get("/messages", follow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertIn('data-station-id=""', response.text)
            finally:
                app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
