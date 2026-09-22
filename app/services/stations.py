"""Multi-station management service.

Each station has its own callsign/SSID, beacon config, position and APRS-IS
credentials. TNCs (modems) are associated with a station via modems.station_id.

When no stations exist the system falls back to the legacy station_settings
row so existing single-station installs keep working unchanged.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.db import fetch_all, fetch_one, get_connection, log_event, utc_now
from app.services.beacon_pathing import (
    BEACON_INTERVAL_MODE_FIXED,
    normalize_beacon_interval_mode,
)
from app.services.tx_scope import TX_SCOPE_ALL_ACTIVE, TX_SCOPE_SINGLE

TX_SCOPE_ALL_ACTIVE_FOR_STATION = "all_active_for_station"
_ALLOWED_TX_SCOPES = {TX_SCOPE_SINGLE, TX_SCOPE_ALL_ACTIVE_FOR_STATION}
_ALLOWED_INTERVAL_MODES = {"fixed", "proportional"}
_ALLOWED_INTERVALS = {5, 10, 15, 30, 45, 60}


# ── Queries ──────────────────────────────────────────────────────────────────

def list_stations() -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT s.*, COUNT(m.id) AS modem_count
        FROM stations s
        LEFT JOIN modems m ON m.station_id = s.id
        GROUP BY s.id
        ORDER BY s.name COLLATE NOCASE ASC, s.id ASC
        """
    )
    return [dict(r) for r in rows]


def get_station(station_id: int) -> dict[str, Any] | None:
    row = fetch_one("SELECT * FROM stations WHERE id = ?", (station_id,))
    return dict(row) if row else None


def get_primary_station() -> dict[str, Any] | None:
    """Return the first enabled station (deterministic by id). No primary concept exists."""
    row = fetch_one(
        """
        SELECT * FROM stations
        WHERE enabled = 1
        ORDER BY id ASC
        LIMIT 1
        """
    )
    return dict(row) if row else None


def get_station_for_modem(modem_id: int) -> dict[str, Any] | None:
    """Return the station that owns this modem, or None."""
    row = fetch_one(
        """
        SELECT s.*
        FROM stations s
        JOIN modems m ON m.station_id = s.id
        WHERE m.id = ?
        LIMIT 1
        """,
        (modem_id,),
    )
    return dict(row) if row else None


def get_station_modems(station_id: int) -> list[dict[str, Any]]:
    """Return all enabled TNC modems that belong to this station."""
    rows = fetch_all(
        """
        SELECT id, name, modem_type, band, device_path, enabled, station_id
        FROM modems
        WHERE station_id = ?
          AND enabled = 1
          AND modem_type IN ('TCP', 'SERIALL', 'SERIAL')
        ORDER BY name COLLATE NOCASE ASC, id ASC
        """,
        (station_id,),
    )
    return [dict(r) for r in rows]


def has_stations() -> bool:
    row = fetch_one("SELECT 1 FROM stations WHERE enabled = 1 LIMIT 1")
    return row is not None


def list_station_options() -> list[dict[str, Any]]:
    """Return id/name pairs suitable for a <select> dropdown."""
    rows = fetch_all(
        "SELECT id, name, callsign, ssid FROM stations ORDER BY name COLLATE NOCASE ASC, id ASC"
    )
    return [dict(r) for r in rows]


# ── Adapter: station row → station_settings dict ─────────────────────────────

def station_settings_from_station(station: dict[str, Any]) -> dict[str, Any]:
    """Convert a stations row to the station_settings dict format used by schedulers."""
    scope_raw = str(station.get("beacon_tx_scope") or TX_SCOPE_ALL_ACTIVE_FOR_STATION)
    scope = scope_raw if scope_raw in _ALLOWED_TX_SCOPES else TX_SCOPE_ALL_ACTIVE_FOR_STATION
    return {
        "station_id": station.get("id"),
        "callsign": str(station.get("callsign") or "").strip().upper(),
        "ssid": str(station.get("ssid") or "").strip(),
        "beacon_interface_id": station.get("beacon_interface_id"),
        "beacon_tx_scope": scope,
        "beacon_comment": str(station.get("beacon_comment") or "").strip(),
        "beacon_interval_mode": normalize_beacon_interval_mode(
            station.get("beacon_interval_mode"), default=BEACON_INTERVAL_MODE_FIXED
        ),
        "beacon_interval_minutes": int(station.get("beacon_interval_minutes") or 30),
        "beacon_path": str(station.get("beacon_path") or "").strip(),
        "status_enabled": int(station.get("status_enabled") or 0),
        "status_text": str(station.get("status_text") or "").strip(),
        "status_interval_minutes": int(station.get("status_interval_minutes") or 30),
        "latitude": station.get("latitude"),
        "longitude": station.get("longitude"),
        "symbol_table": station.get("symbol_table"),
        "symbol_code": station.get("symbol_code"),
        "symbol_overlay": station.get("symbol_overlay"),
        "tx_enabled": int(station.get("tx_enabled") or 0),
        "beacon_internal_tx": False,
        "default_units": "metric",
    }


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create_station(payload: dict[str, Any]) -> tuple[bool, str, int | None]:
    try:
        values = _normalize_station_payload(payload, is_create=True)
    except ValueError as exc:
        return False, str(exc), None

    now = utc_now()
    try:
        with get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO stations (
                    name, callsign, ssid,
                    beacon_comment, beacon_interval_mode, beacon_interval_minutes,
                    beacon_path, beacon_tx_scope, beacon_interface_id,
                    status_enabled, status_text, status_interval_minutes,
                    latitude, longitude, symbol_table, symbol_code, symbol_overlay,
                    tx_enabled, is_primary, enabled, notes, created_at, updated_at
                ) VALUES (
                    :name, :callsign, :ssid,
                    :beacon_comment, :beacon_interval_mode, :beacon_interval_minutes,
                    :beacon_path, :beacon_tx_scope, :beacon_interface_id,
                    :status_enabled, :status_text, :status_interval_minutes,
                    :latitude, :longitude, :symbol_table, :symbol_code, :symbol_overlay,
                    :tx_enabled, :is_primary, :enabled, :notes, :now, :now
                )
                """,
                {**values, "now": now},
            )
            new_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        return False, _humanize_integrity_error(exc, values), None
    log_event("INFO", "config", f"Created station '{values['name']}' (id={new_id})")
    return True, f"Station '{values['name']}' created.", new_id


def update_station(station_id: int, payload: dict[str, Any]) -> tuple[bool, str]:
    existing = get_station(station_id)
    if existing is None:
        return False, "Station not found."
    merged = {**existing, **payload}
    try:
        values = _normalize_station_payload(merged, is_create=False)
    except ValueError as exc:
        return False, str(exc)

    now = utc_now()
    try:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE stations SET
                    name = :name, callsign = :callsign, ssid = :ssid,
                    beacon_comment = :beacon_comment,
                    beacon_interval_mode = :beacon_interval_mode,
                    beacon_interval_minutes = :beacon_interval_minutes,
                    beacon_path = :beacon_path,
                    beacon_tx_scope = :beacon_tx_scope,
                    beacon_interface_id = :beacon_interface_id,
                    status_enabled = :status_enabled,
                    status_text = :status_text,
                    status_interval_minutes = :status_interval_minutes,
                    latitude = :latitude,
                    longitude = :longitude,
                    symbol_table = :symbol_table,
                    symbol_code = :symbol_code,
                    symbol_overlay = :symbol_overlay,
                    tx_enabled = :tx_enabled,
                    is_primary = :is_primary,
                    enabled = :enabled,
                    notes = :notes,
                    updated_at = :now
                WHERE id = :station_id
                """,
                {**values, "station_id": station_id, "now": now},
            )
    except sqlite3.IntegrityError as exc:
        return False, _humanize_integrity_error(exc, values)
    log_event("INFO", "config", f"Updated station '{values['name']}' (id={station_id})")
    return True, f"Station '{values['name']}' updated."


def duplicate_station(station_id: int) -> tuple[bool, str, int | None]:
    """Copy a station row, resolving name collisions with '(copy)', '(copy 2)', ..."""
    source = get_station(station_id)
    if source is None:
        return False, "Station not found.", None
    base_name = str(source.get("name") or "").strip() or "Station"
    new_name = _resolve_copy_name(base_name)
    payload = {k: v for k, v in source.items() if k not in {"id", "created_at", "updated_at"}}
    payload["name"] = new_name
    # Reset fields that shouldn't be silently reused across stations
    payload["enabled"] = int(source.get("enabled") or 0)
    return create_station(payload)


def _resolve_copy_name(base_name: str) -> str:
    for suffix in ("(copy)", *(f"(copy {i})" for i in range(2, 100))):
        candidate = f"{base_name} {suffix}"
        row = fetch_one("SELECT 1 FROM stations WHERE name = ?", (candidate,))
        if row is None:
            return candidate
    raise ValueError(f"Cannot find a free copy name for '{base_name}'.")


def delete_station(station_id: int) -> tuple[bool, str]:
    existing = get_station(station_id)
    if existing is None:
        return False, "Station not found."
    name = str(existing.get("name") or "")
    with get_connection() as connection:
        connection.execute(
            "UPDATE modems SET station_id = NULL, updated_at = ? WHERE station_id = ?",
            (utc_now(), station_id),
        )
        connection.execute("DELETE FROM stations WHERE id = ?", (station_id,))
    log_event("INFO", "config", f"Deleted station '{name}' (id={station_id})")
    return True, f"Station '{name}' deleted."


# ── Validation ────────────────────────────────────────────────────────────────

def _normalize_station_payload(payload: dict[str, Any], *, is_create: bool) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Station name is required.")
    if len(name) > 64:
        raise ValueError("Station name must be 64 characters or fewer.")

    callsign = str(payload.get("callsign") or "").strip().upper()
    if callsign and not _valid_callsign_base(callsign):
        raise ValueError("Callsign must be 1–6 uppercase letters/digits.")

    ssid = str(payload.get("ssid") or "").strip()
    if ssid and (not ssid.isdigit() or not 0 <= int(ssid) <= 15):
        raise ValueError("SSID must be a number 0–15.")

    interval_minutes = _safe_int(payload.get("beacon_interval_minutes"), default=30)
    if interval_minutes not in _ALLOWED_INTERVALS:
        interval_minutes = 30

    status_interval_minutes = _safe_int(payload.get("status_interval_minutes"), default=30)
    if status_interval_minutes not in _ALLOWED_INTERVALS:
        status_interval_minutes = 30

    tx_scope = str(payload.get("beacon_tx_scope") or TX_SCOPE_ALL_ACTIVE_FOR_STATION)
    if tx_scope not in _ALLOWED_TX_SCOPES:
        tx_scope = TX_SCOPE_ALL_ACTIVE_FOR_STATION

    beacon_interface_id = payload.get("beacon_interface_id")
    if beacon_interface_id not in (None, "", "0"):
        try:
            beacon_interface_id = int(beacon_interface_id)
        except (TypeError, ValueError):
            beacon_interface_id = None
    else:
        beacon_interface_id = None

    if tx_scope == TX_SCOPE_SINGLE and beacon_interface_id is None:
        raise ValueError("TX scope 'single' requires a specific interface.")

    if beacon_interface_id is not None:
        row = fetch_one("SELECT 1 FROM modems WHERE id = ?", (beacon_interface_id,))
        if row is None:
            raise ValueError("Selected TX interface no longer exists.")

    latitude = _validate_coordinate(payload.get("latitude"), "Latitude", -90.0, 90.0)
    longitude = _validate_coordinate(payload.get("longitude"), "Longitude", -180.0, 180.0)

    symbol_table = str(payload.get("symbol_table") or "").strip() or None
    if symbol_table not in (None, "/", "\\"):
        raise ValueError("Symbol table must be '/' or '\\\\'.")
    symbol_code = str(payload.get("symbol_code") or "").strip() or None
    if symbol_code is not None and len(symbol_code) != 1:
        raise ValueError("Symbol code must be a single character.")
    symbol_overlay = str(payload.get("symbol_overlay") or "").strip() or None
    if symbol_overlay is not None and (len(symbol_overlay) != 1 or not symbol_overlay.isalnum()):
        raise ValueError("Symbol overlay must be a single alphanumeric character.")

    return {
        "name": name,
        "callsign": callsign,
        "ssid": ssid,
        "beacon_comment": str(payload.get("beacon_comment") or "").strip(),
        "beacon_interval_mode": normalize_beacon_interval_mode(
            payload.get("beacon_interval_mode"), default=BEACON_INTERVAL_MODE_FIXED
        ),
        "beacon_interval_minutes": interval_minutes,
        "beacon_path": str(payload.get("beacon_path") or "").strip().upper(),
        "beacon_tx_scope": tx_scope,
        "beacon_interface_id": beacon_interface_id,
        "status_enabled": 1 if payload.get("status_enabled") else 0,
        "status_text": str(payload.get("status_text") or "").strip(),
        "status_interval_minutes": status_interval_minutes,
        "latitude": latitude,
        "longitude": longitude,
        "symbol_table": symbol_table,
        "symbol_code": symbol_code,
        "symbol_overlay": symbol_overlay,
        "tx_enabled": 1 if payload.get("tx_enabled") else 0,
        "is_primary": 0,
        "enabled": 1 if payload.get("enabled", True) else 0,
        "notes": str(payload.get("notes") or "").strip(),
    }


def _safe_int(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _validate_coordinate(value: Any, label: str, low: float, high: float) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a decimal number.") from None
    if not low <= parsed <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return text


def _valid_callsign_base(callsign: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Z0-9]{1,6}", callsign))


def _humanize_integrity_error(exc: sqlite3.IntegrityError, values: dict[str, Any]) -> str:
    text = str(exc).lower()
    if "stations.name" in text or "unique constraint failed: stations.name" in text:
        return f"A station named '{values.get('name')}' already exists."
    if "check constraint" in text:
        return "One of the station fields has an invalid value."
    return f"Could not save station: {exc}"
