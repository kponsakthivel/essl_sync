#!/usr/bin/env python3
"""
eSSL / ZKTeco Biometric Device -> Frappe/ERPNext Employee Checkin Sync
========================================================================

Reads attendance punches off an eSSL (ZK-protocol) biometric device over
TCP/IP and pushes them into a Frappe/ERPNext site as "Employee Checkin"
records via the REST API.

Configuration is read from a `.env` file (see env.example) in the same
directory as this script, or from real environment variables if already
set (systemd EnvironmentFile= also works with this format).

Usage:
    python essl_sync.py --test-device      # verify device connectivity
    python essl_sync.py --test-frappe      # verify Frappe API connectivity
    python essl_sync.py --list-users       # list users enrolled on the device
    python essl_sync.py --sync-once        # pull + push once, then exit
    python essl_sync.py --run              # loop forever, polling every N minutes
    python essl_sync.py                    # same as --sync-once (default)

Requires:
    pip install -r requirement.txt
"""

import argparse
import configparser
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    from zk import ZK, const
except ImportError:
    print("ERROR: the 'pyzk' package is required. Run: pip install pyzk")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
STATE_FILE = SCRIPT_DIR / "sync_state.json"
LOG_FILE = SCRIPT_DIR / "essl_sync.log"


# ---------------------------------------------------------------------------
# Config loading (.env style: KEY=VALUE, # comments, blank lines ignored)
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (without
    overwriting variables that are already set in the real environment,
    so systemd EnvironmentFile= or exported shell vars still win)."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def get_config() -> dict:
    load_env_file(ENV_FILE)

    def env(name, default=None, required=False):
        val = os.environ.get(name, default)
        if required and (val is None or val == ""):
            print(f"ERROR: required config value '{name}' is missing. "
                  f"Set it in {ENV_FILE} or as an environment variable.")
            sys.exit(1)
        return val

    def env_bool(name, default=False):
        val = os.environ.get(name)
        if val is None:
            return default
        return val.strip().lower() in ("1", "true", "yes", "on")

    def env_int(name, default):
        val = os.environ.get(name)
        if val is None or val == "":
            return default
        try:
            return int(val)
        except ValueError:
            return default

    cfg = {
        "device_ip": env("DEVICE_IP", required=True),
        "device_port": env_int("DEVICE_PORT", 4370),
        "device_password": env_int("DEVICE_PASSWORD", 0),
        "device_timeout": env_int("DEVICE_TIMEOUT", 10),
        "device_force_udp": env_bool("DEVICE_FORCE_UDP", False),

        "frappe_url": env("FRAPPE_URL", required=True).rstrip("/"),
        "frappe_api_key": env("FRAPPE_API_KEY", required=True),
        "frappe_api_secret": env("FRAPPE_API_SECRET", required=True),

        "initial_sync_days": env_int("INITIAL_SYNC_DAYS", 7),
        "clear_device_logs": env_bool("CLEAR_DEVICE_LOGS", False),
        "poll_interval_minutes": env_int("POLL_INTERVAL_MINUTES", 5),
        "log_level": env("LOG_LEVEL", "INFO"),
    }
    return cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("essl_sync")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


# ---------------------------------------------------------------------------
# Sync state (tracks last-synced timestamp so repeated runs don't duplicate)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Device connection
# ---------------------------------------------------------------------------

def connect_device(cfg: dict):
    zk = ZK(
        cfg["device_ip"],
        port=cfg["device_port"],
        timeout=cfg["device_timeout"],
        password=cfg["device_password"],
        force_udp=cfg["device_force_udp"],
        ommit_ping=False,
    )
    return zk.connect()


def test_device(cfg: dict, logger: logging.Logger) -> bool:
    logger.info(f"Testing device connection to {cfg['device_ip']}:{cfg['device_port']} ...")
    try:
        conn = connect_device(cfg)
        info = {
            "firmware_version": conn.get_firmware_version(),
            "serial_number": conn.get_serialnumber(),
            "device_name": conn.get_device_name(),
            "platform": conn.get_platform(),
        }
        logger.info(f"Device OK: {info}")
        conn.disconnect()
        return True
    except Exception as e:
        logger.error(f"Device connection FAILED: {e}")
        return False


def list_users(cfg: dict, logger: logging.Logger) -> bool:
    try:
        conn = connect_device(cfg)
        conn.disable_device()
        users = conn.get_users()
        conn.enable_device()
        conn.disconnect()
        if not users:
            logger.info("No users enrolled on device.")
            return True
        logger.info(f"{len(users)} user(s) enrolled on device:")
        for u in users:
            logger.info(f"  uid={u.uid} user_id={u.user_id} name={u.name!r} privilege={u.privilege}")
        return True
    except Exception as e:
        logger.error(f"Failed to list users: {e}")
        return False


def fetch_attendance(cfg: dict, logger: logging.Logger):
    """Connect to the device and pull all attendance records currently
    stored on it. Filtering by 'since last sync' happens after this,
    in Python, since most eSSL/ZK devices don't support server-side
    filtering of the attendance log."""
    conn = connect_device(cfg)
    try:
        conn.disable_device()  # prevents new punches from being lost mid-read
        records = conn.get_attendance()
    finally:
        try:
            conn.enable_device()
        except Exception:
            pass

    if cfg["clear_device_logs"]:
        try:
            conn.clear_attendance()
            logger.info("Device attendance log cleared after read.")
        except Exception as e:
            logger.error(f"Failed to clear device log: {e}")

    conn.disconnect()
    return records


# ---------------------------------------------------------------------------
# Frappe API
# ---------------------------------------------------------------------------

def frappe_headers(cfg: dict) -> dict:
    return {
        "Authorization": f"token {cfg['frappe_api_key']}:{cfg['frappe_api_secret']}",
        "Content-Type": "application/json",
    }


def test_frappe(cfg: dict, logger: logging.Logger) -> bool:
    url = f"{cfg['frappe_url']}/api/method/frappe.auth.get_logged_user"
    logger.info(f"Testing Frappe connection to {cfg['frappe_url']} ...")
    try:
        resp = requests.get(url, headers=frappe_headers(cfg), timeout=15)
        if resp.status_code == 200:
            logger.info(f"Frappe OK: authenticated as {resp.json().get('message')}")
            return True
        logger.error(f"Frappe auth FAILED: HTTP {resp.status_code} - {resp.text[:300]}")
        return False
    except requests.RequestException as e:
        logger.error(f"Frappe connection FAILED: {e}")
        return False


def push_checkin(cfg: dict, logger: logging.Logger, user_id: str, timestamp: datetime,
                  log_type: str = None) -> bool:
    """Create an Employee Checkin record in Frappe for one punch.
    Returns True on success (including 'already exists'), False on failure."""
    url = f"{cfg['frappe_url']}/api/resource/Employee Checkin"
    payload = {
        "employee_field_value": user_id,
        "employee_fieldname": "attendance_device_id",
        "time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if log_type in ("IN", "OUT"):
        payload["log_type"] = log_type

    try:
        resp = requests.post(url, headers=frappe_headers(cfg), json=payload, timeout=20)
    except requests.RequestException as e:
        logger.error(f"user={user_id} time={timestamp} -> network error pushing to Frappe: {e}")
        return False

    if resp.status_code in (200, 201):
        resp_id = resp.json().get("data", {}).get("name", "?")
        logger.info(f"SYNCED user={user_id} time={timestamp} -> Employee Checkin {resp_id}")
        return True

    body = resp.text[:500]

    # Frappe raises a DuplicateEntryError-style message when the same
    # employee+time checkin already exists; treat that as a harmless
    # "already synced" case rather than a failure.
    if resp.status_code == 409 or "duplicate" in body.lower():
        logger.info(f"SKIP (already exists) user={user_id} time={timestamp}")
        return True

    if "attendance_device_id" in body.lower() and resp.status_code == 417:
        logger.error(
            f"FAILED user={user_id} time={timestamp} -> no Employee found with "
            f"attendance_device_id='{user_id}'. Check Employee master data in Frappe."
        )
        return False

    logger.error(f"FAILED user={user_id} time={timestamp} -> HTTP {resp.status_code}: {body}")
    return False


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def run_sync_once(cfg: dict, logger: logging.Logger) -> None:
    state = load_state()
    last_synced_str = state.get("last_synced_timestamp")

    if last_synced_str:
        since = datetime.fromisoformat(last_synced_str)
    else:
        since = datetime.now() - timedelta(days=cfg["initial_sync_days"])
        logger.info(
            f"No previous sync state found. First run will look back "
            f"{cfg['initial_sync_days']} day(s), i.e. records since {since}."
        )

    logger.info(f"Fetching attendance records from device (since {since}) ...")
    try:
        records = fetch_attendance(cfg, logger)
    except Exception as e:
        logger.error(f"Failed to fetch attendance from device: {e}")
        return

    if records is None:
        records = []

    # Only process punches newer than the last sync point, and remember
    # the newest timestamp we successfully process so next run starts there.
    new_records = [r for r in records if r.timestamp > since]
    new_records.sort(key=lambda r: r.timestamp)

    logger.info(f"{len(records)} total record(s) on device, {len(new_records)} new since last sync.")

    if not new_records:
        logger.info("Nothing new to sync.")
        return

    synced = 0
    failed = 0
    newest_ok_timestamp = since

    for rec in new_records:
        # pyzk Attendance record fields: user_id, timestamp, status, punch
        # 'punch' / 'status' meaning varies by device firmware; map 0/1 -> IN/OUT
        # when possible, otherwise omit and let Frappe auto-determine log_type.
        log_type = None
        punch_value = getattr(rec, "punch", None)
        if punch_value == 0:
            log_type = "IN"
        elif punch_value == 1:
            log_type = "OUT"

        ok = push_checkin(cfg, logger, str(rec.user_id), rec.timestamp, log_type)
        if ok:
            synced += 1
            if rec.timestamp > newest_ok_timestamp:
                newest_ok_timestamp = rec.timestamp
        else:
            failed += 1
            # Stop advancing the watermark past a failed record so it's
            # retried next run, but keep trying the rest of the batch.

    state["last_synced_timestamp"] = newest_ok_timestamp.isoformat()
    state["last_run_at"] = datetime.now().isoformat()
    state["last_run_synced"] = synced
    state["last_run_failed"] = failed
    save_state(state)

    logger.info(f"Sync complete: {synced} synced, {failed} failed.")
    if failed:
        logger.warning(
            f"{failed} record(s) failed to sync and will be retried next run "
            f"(watermark held at {newest_ok_timestamp})."
        )


def run_continuous(cfg: dict, logger: logging.Logger) -> None:
    interval = cfg["poll_interval_minutes"]
    logger.info(f"Starting continuous sync loop, polling every {interval} minute(s). Ctrl+C to stop.")
    try:
        while True:
            run_sync_once(cfg, logger)
            logger.info(f"Sleeping {interval} minute(s) ...")
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl+C).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="eSSL/ZKTeco -> Frappe Employee Checkin sync")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test-device", action="store_true", help="Test connection to the biometric device")
    group.add_argument("--test-frappe", action="store_true", help="Test connection to the Frappe site")
    group.add_argument("--list-users", action="store_true", help="List users enrolled on the device")
    group.add_argument("--sync-once", action="store_true", help="Run one sync pass and exit")
    group.add_argument("--run", action="store_true", help="Run continuously, polling on an interval")
    args = parser.parse_args()

    cfg = get_config()
    logger = setup_logging(cfg["log_level"])

    if args.test_device:
        ok = test_device(cfg, logger)
        sys.exit(0 if ok else 1)

    if args.test_frappe:
        ok = test_frappe(cfg, logger)
        sys.exit(0 if ok else 1)

    if args.list_users:
        ok = list_users(cfg, logger)
        sys.exit(0 if ok else 1)

    if args.run:
        run_continuous(cfg, logger)
        return

    # default / --sync-once
    run_sync_once(cfg, logger)


if __name__ == "__main__":
    main()