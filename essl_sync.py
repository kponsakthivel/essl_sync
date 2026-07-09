#!/usr/bin/env python3
"""
eSSL Biometric Device -> SQL Server Sync Tool
================================================

Connects to an eSSL (ZK-protocol / TCP-IP) biometric attendance device, pulls
punch/attendance logs, and inserts new records into a SQL Server database
(by default matching the standard "iclock" CHECKINOUT table schema used by
eTimeTrackLite and similar eSSL/ZKTeco based attendance software).

USAGE
-----
    python essl_sync.py --test-device      Test connection to the biometric device only
    python essl_sync.py --test-db          Test connection to SQL Server only
    python essl_sync.py --sync-once        Pull attendance and sync to DB one time, then exit
    python essl_sync.py --run              Sync once, then keep polling every N minutes (see config.ini)
    python essl_sync.py --list-users       Print enrolled users on the device

All settings (device IP, DB connection, table/column names, poll interval, etc.)
live in config.ini next to this script - edit that file before running.

REQUIREMENTS
------------
    pip install -r requirements.txt

    You also need the SQL Server ODBC driver installed on this machine
    (e.g. "ODBC Driver 17 for SQL Server" - downloadable from Microsoft).
"""

import argparse
import configparser
import logging
import os
import sys
import time
from datetime import datetime

try:
    from zk import ZK, const
except ImportError:
    print("Missing dependency 'pyzk'. Run: pip install -r requirements.txt")
    sys.exit(1)

try:
    import pyodbc
except ImportError:
    print("Missing dependency 'pyodbc'. Run: pip install -r requirements.txt")
    sys.exit(1)


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")


# ------------------------------------------------------------------------- #
# Config / logging setup
# ------------------------------------------------------------------------- #

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def setup_logging(cfg):
    log_file = cfg.get("Logging", "log_file", fallback="essl_sync.log")
    log_level = cfg.get("Logging", "log_level", fallback="INFO").upper()

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ------------------------------------------------------------------------- #
# Device connection
# ------------------------------------------------------------------------- #

def connect_device(cfg):
    ip = cfg.get("Device", "ip")
    port = cfg.getint("Device", "port", fallback=4370)
    password = cfg.getint("Device", "password", fallback=0)
    timeout = cfg.getint("Device", "timeout", fallback=10)
    force_udp = cfg.getboolean("Device", "force_udp", fallback=False)

    logging.info(f"Connecting to eSSL device at {ip}:{port} ...")
    zk = ZK(
        ip,
        port=port,
        timeout=timeout,
        password=password,
        force_udp=force_udp,
        ommit_ping=False,
    )
    conn = zk.connect()
    logging.info("Device connection established.")
    return conn


# ------------------------------------------------------------------------- #
# Database connection
# ------------------------------------------------------------------------- #

def get_db_connection(cfg):
    server = cfg.get("Database", "server")
    database = cfg.get("Database", "database")
    driver = cfg.get("Database", "odbc_driver", fallback="ODBC Driver 17 for SQL Server")
    use_windows_auth = cfg.getboolean("Database", "use_windows_auth", fallback=False)

    if use_windows_auth:
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
            f"Trusted_Connection=yes;"
        )
    else:
        username = cfg.get("Database", "username")
        password = cfg.get("Database", "password")
        conn_str = (
            f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
            f"UID={username};PWD={password};"
        )

    logging.info(f"Connecting to SQL Server database '{database}' on '{server}' ...")
    conn = pyodbc.connect(conn_str, timeout=10)
    logging.info("Database connection established.")
    return conn


def ensure_table_exists(db_conn, cfg):
    table = cfg.get("Table", "table_name")
    auto_create = cfg.getboolean("Table", "auto_create_table", fallback=False)

    cursor = db_conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ?",
        table,
    )
    exists = cursor.fetchone()[0] > 0

    if exists:
        return

    if not auto_create:
        raise RuntimeError(
            f"Table '{table}' does not exist in the target database, and "
            f"auto_create_table is disabled in config.ini. Either create the "
            f"table yourself to match your existing schema, or set "
            f"auto_create_table = true to let this script create a basic one."
        )

    col_userid = cfg.get("Table", "col_userid")
    col_checktime = cfg.get("Table", "col_checktime")
    col_checktype = cfg.get("Table", "col_checktype")
    col_verifycode = cfg.get("Table", "col_verifycode")
    col_sensorid = cfg.get("Table", "col_sensorid")
    col_sn = cfg.get("Table", "col_sn")

    logging.warning(f"Table '{table}' not found - creating a basic version of it.")
    create_sql = f"""
        CREATE TABLE {table} (
            {col_userid} VARCHAR(50) NOT NULL,
            {col_checktime} DATETIME NOT NULL,
            {col_checktype} CHAR(1) NULL,
            {col_verifycode} INT NULL,
            {col_sensorid} VARCHAR(50) NULL,
            {col_sn} VARCHAR(50) NULL
        )
    """
    cursor.execute(create_sql)
    db_conn.commit()
    logging.info(f"Table '{table}' created.")


# ------------------------------------------------------------------------- #
# Sync logic
# ------------------------------------------------------------------------- #

def record_exists(db_conn, cfg, user_id, check_time, device_sn):
    table = cfg.get("Table", "table_name")
    col_userid = cfg.get("Table", "col_userid")
    col_checktime = cfg.get("Table", "col_checktime")
    col_sn = cfg.get("Table", "col_sn")

    cursor = db_conn.cursor()
    query = (
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE {col_userid} = ? AND {col_checktime} = ? AND {col_sn} = ?"
    )
    cursor.execute(query, str(user_id), check_time, device_sn)
    return cursor.fetchone()[0] > 0


def insert_record(db_conn, cfg, user_id, check_time, check_type, verify_code, sensor_id, device_sn):
    table = cfg.get("Table", "table_name")
    col_userid = cfg.get("Table", "col_userid")
    col_checktime = cfg.get("Table", "col_checktime")
    col_checktype = cfg.get("Table", "col_checktype")
    col_verifycode = cfg.get("Table", "col_verifycode")
    col_sensorid = cfg.get("Table", "col_sensorid")
    col_sn = cfg.get("Table", "col_sn")

    cursor = db_conn.cursor()
    insert_sql = (
        f"INSERT INTO {table} "
        f"({col_userid}, {col_checktime}, {col_checktype}, {col_verifycode}, {col_sensorid}, {col_sn}) "
        f"VALUES (?, ?, ?, ?, ?, ?)"
    )
    cursor.execute(
        insert_sql,
        str(user_id),
        check_time,
        str(check_type) if check_type is not None else None,
        int(verify_code) if verify_code is not None else None,
        str(sensor_id) if sensor_id is not None else None,
        device_sn,
    )
    db_conn.commit()


def sync_once(cfg):
    device_conn = None
    db_conn = None
    inserted = 0
    skipped = 0

    try:
        device_conn = connect_device(cfg)
        db_conn = get_db_connection(cfg)
        ensure_table_exists(db_conn, cfg)

        device_sn = cfg.get("Sync", "device_sn", fallback="").strip()
        if not device_sn:
            try:
                device_sn = device_conn.get_serialnumber()
            except Exception:
                device_sn = "UNKNOWN"

        logging.info("Disabling device (prevents new punches mid-transfer) ...")
        device_conn.disable_device()

        logging.info("Fetching attendance records from device ...")
        attendances = device_conn.get_attendance()
        logging.info(f"Retrieved {len(attendances)} punch records from device.")

        for att in attendances:
            user_id = att.user_id
            check_time = att.timestamp
            # punch: 0=Fingerprint,1=Password,others depending on device; status: check-in/out code
            check_type = getattr(att, "punch", None)
            verify_code = getattr(att, "status", None)
            sensor_id = None  # not exposed per-record by pyzk; leave null unless you track per-device

            if record_exists(db_conn, cfg, user_id, check_time, device_sn):
                skipped += 1
                continue

            insert_record(
                db_conn, cfg,
                user_id, check_time, check_type, verify_code, sensor_id, device_sn,
            )
            inserted += 1

        logging.info(f"Sync complete: {inserted} new record(s) inserted, {skipped} already existed.")

        clear_after = cfg.getboolean("Sync", "clear_device_after_sync", fallback=False)
        if clear_after and inserted > 0:
            logging.info("clear_device_after_sync is enabled - clearing device attendance log ...")
            device_conn.clear_attendance()
            logging.info("Device attendance log cleared.")

    finally:
        if device_conn:
            try:
                device_conn.enable_device()
                device_conn.disconnect()
            except Exception:
                pass
        if db_conn:
            db_conn.close()

    return inserted, skipped


# ------------------------------------------------------------------------- #
# Utility commands
# ------------------------------------------------------------------------- #

def test_device(cfg):
    conn = connect_device(cfg)
    try:
        sn = conn.get_serialnumber()
        firmware = conn.get_firmware_version()
        print(f"Connected OK. Serial number: {sn} | Firmware: {firmware}")
    finally:
        conn.disconnect()


def test_db(cfg):
    conn = get_db_connection(cfg)
    cursor = conn.cursor()
    cursor.execute("SELECT @@VERSION")
    row = cursor.fetchone()
    print(f"Connected OK. SQL Server version: {row[0][:60]}...")
    conn.close()


def list_users(cfg):
    conn = connect_device(cfg)
    try:
        users = conn.get_users()
        print(f"{len(users)} enrolled user(s):")
        for u in users:
            print(f"  UID={u.uid}  User ID={u.user_id}  Name={u.name}")
    finally:
        conn.disconnect()


# ------------------------------------------------------------------------- #
# Entry point
# ------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="eSSL Biometric Device -> SQL Server Sync Tool")
    parser.add_argument("--test-device", action="store_true", help="Test connection to the biometric device")
    parser.add_argument("--test-db", action="store_true", help="Test connection to SQL Server")
    parser.add_argument("--sync-once", action="store_true", help="Sync attendance once and exit")
    parser.add_argument("--run", action="store_true", help="Sync continuously on the configured interval")
    parser.add_argument("--list-users", action="store_true", help="List users enrolled on the device")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging(cfg)

    if args.test_device:
        test_device(cfg)
    elif args.test_db:
        test_db(cfg)
    elif args.list_users:
        list_users(cfg)
    elif args.sync_once:
        sync_once(cfg)
    elif args.run:
        interval = cfg.getint("Sync", "poll_interval_minutes", fallback=5)
        logging.info(f"Starting continuous sync loop - polling every {interval} minute(s). Ctrl+C to stop.")
        while True:
            try:
                sync_once(cfg)
            except Exception as e:
                logging.error(f"Sync error: {e}")
            time.sleep(interval * 60)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()