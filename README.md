# eSSL Biometric Device → SQL Server Sync Tool

A standalone Python tool that connects to an eSSL biometric attendance device
over TCP/IP, pulls punch logs, and writes new records into your SQL Server
database (by default matching the standard `CHECKINOUT` table used by the
`iclock` schema that eTimeTrackLite and similar eSSL/ZKTeco-based software use).

It will **not** duplicate records on repeated runs — it checks the DB for an
existing matching row (user ID + timestamp + device serial) before inserting.

---

## 1. Requirements

- Python 3.8+
- SQL Server ODBC driver installed on this machine
  (**ODBC Driver 17 for SQL Server** — free download from Microsoft, search
  "Microsoft ODBC Driver for SQL Server download")
- Network access from this machine to:
  - the eSSL device (usually port `4370`)
  - the SQL Server instance (usually port `1433`)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## 2. Configure

Open `config.ini` and fill in:

- **[Device]** — the device's IP address (check on the device: Menu → Comm → Ethernet).
  Port 4370 is standard for eSSL/ZK-protocol devices unless changed on the device.
- **[Database]** — your SQL Server address, the `iclock` database name, and login
  credentials (or set `use_windows_auth = true` to use the account running the script).
- **[Table]** — column names for your existing attendance table. The defaults
  match the standard `CHECKINOUT` table schema (`USERID`, `CHECKTIME`, `CHECKTYPE`,
  `VERIFYCODE`, `SENSORID`, `SN`). If your table/columns are named differently,
  just change these values — the script uses them dynamically, no code edits needed.
- **[Sync]** — polling interval for continuous mode, and whether to clear the
  device's log after a successful sync (leave `false` until you trust the setup).

---

## 3. Test before syncing anything

```bash
python essl_sync.py --test-device
python essl_sync.py --test-db
python essl_sync.py --list-users
```

Each should print a success message. Fix any connection errors here before
moving on — they'll tell you immediately whether the problem is the device,
the network, or the SQL Server login (same categories of error as the
"Cannot open database" issue you were troubleshooting earlier).

---

## 4. Run a sync

One-time sync (good for testing):

```bash
python essl_sync.py --sync-once
```

Continuous sync (polls every N minutes, per `poll_interval_minutes` in config.ini):

```bash
python essl_sync.py --run
```

Check `essl_sync.log` (created next to the script) for a full run history.

---

## 5. Running it automatically (Windows)

To keep this running in the background on your server, either:

- **Task Scheduler**: create a task that runs
  `python C:\path\to\essl_sync.py --sync-once`
  on a repeating trigger (e.g. every 5 minutes) — simplest and most reliable option.
- **Long-running process**: run `python essl_sync.py --run` inside NSSM
  (Non-Sucking Service Manager) to register it as a proper Windows service.

Task Scheduler is recommended for most setups — it's simpler to monitor and
recovers cleanly if the script or device connection ever hangs.

---

## 6. Multiple devices

For more than one eSSL device, either:
- run separate copies of this folder with a different `config.ini` per device
  (each pointing at the same DB table, differentiated by `device_sn`), or
- ask me to extend the script to loop over a list of devices from one config file.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `--test-device` times out | Wrong IP, device on a different subnet/VLAN, or port 4370 blocked by firewall |
| `--test-db` fails with login error | Wrong SQL username/password, or that login isn't mapped to the `iclock` database — same category of issue as the "Cannot open database" error you saw in the eTimeTrackLite app |
| Records insert but with wrong/blank names | This tool syncs punches (user ID + timestamp) only, not names — names should already exist in your `USERINFO`/employee table from device enrollment sync, done separately |
| Duplicate records appear | Check that `col_sn` (device serial) is populated correctly — if `device_sn` is blank and the device doesn't return one, this can affect the duplicate check across syncs |