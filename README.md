# eSSL Biometric Device → Frappe/ERPNext Sync Tool

Connects to an eSSL/ZKTeco biometric attendance device over TCP/IP, pulls
punch logs, and creates **Employee Checkin** records in your Frappe/ERPNext
site via the REST API.

It will **not** duplicate records on repeated runs — it keeps a local
`sync_state.json` watermark of the last successfully-synced punch timestamp,
and additionally treats "duplicate entry" responses from Frappe as already-synced.

---

## 1. Requirements

- Python 3.8+
- Network access from this machine to:
  - the eSSL device (usually port `4370`)
  - your Frappe/ERPNext site (HTTPS, port 443)
- A Frappe API Key + API Secret (User → Settings → API Access → Generate Keys)
  for a user that has permission to create **Employee Checkin** records.
- Every employee you want to sync must have their device's user ID entered
  in the Employee doctype's **`attendance_device_id`** field in Frappe.

Install Python dependencies:

```bash
pip install -r requirement.txt
```

---

## 2. Configure

```bash
cp env.example .env
nano .env
```

Fill in:

- **DEVICE_IP / DEVICE_PORT** — the device's IP (Menu → Comm → Ethernet on
  the device). Port 4370 is standard for eSSL/ZK-protocol devices.
- **FRAPPE_URL / FRAPPE_API_KEY / FRAPPE_API_SECRET** — your site URL and API
  credentials.
- **INITIAL_SYNC_DAYS** — on the very first run (no sync history yet), how
  many days back to pull punches from.
- **CLEAR_DEVICE_LOGS** — leave `false` until you've confirmed syncing works
  reliably. The device's own log is your backup copy if something goes wrong.
- **POLL_INTERVAL_MINUTES** — only used in `--run` continuous mode.

---

## 3. Test before syncing anything

```bash
python essl_sync.py --test-device
python essl_sync.py --test-frappe
python essl_sync.py --list-users
```

Each should print a success message. Fix connection errors here first —
the messages will tell you whether the problem is the device, the network,
or the Frappe API key/permissions.

---

## 4. Run a sync

One-time sync (good for testing):

```bash
python essl_sync.py --sync-once
```

Continuous sync (polls every `POLL_INTERVAL_MINUTES`):

```bash
python essl_sync.py --run
```

## 5. Logs

Everything is written to `essl_sync.log` next to the script (and echoed to
the console), one line per event:

```
2026-07-09 19:20:01 [INFO] Fetching attendance records from device (since 2026-07-02 19:20:01) ...
2026-07-09 19:20:03 [INFO] 42 total record(s) on device, 3 new since last sync.
2026-07-09 19:20:04 [INFO] SYNCED user=107 time=2026-07-09 09:01:12 -> Employee Checkin HR-CHK-2026-00123
2026-07-09 19:20:04 [ERROR] FAILED user=999 time=2026-07-09 09:05:00 -> no Employee found with attendance_device_id='999'. Check Employee master data in Frappe.
2026-07-09 19:20:04 [INFO] Sync complete: 2 synced, 1 failed.
```

- Filter successes: `grep SYNCED essl_sync.log`
- Filter errors: `grep ERROR essl_sync.log`
- Failed records are **not** marked as synced, so they're retried automatically
  on the next run.

---

## 6. Running it automatically (Linux / systemd)

```bash
sudo mkdir -p /opt/essl-frappe-sync
sudo cp -r ./* /opt/essl-frappe-sync/
cd /opt/essl-frappe-sync
python3 -m venv venv
source venv/bin/activate
pip install -r requirement.txt
cp env.example .env   # then edit .env

sudo useradd -r -s /usr/sbin/nologin essl-sync
sudo chown -R essl-sync:essl-sync /opt/essl-frappe-sync

sudo cp essl_sync.service essl_sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now essl_sync.timer
```

Check it's running:

```bash
systemctl list-timers | grep essl_sync
journalctl -u essl_sync.service -f
tail -f /opt/essl-frappe-sync/essl_sync.log
```

---

## 7. Multiple devices

Run separate copies of this folder (own `.env`, own `sync_state.json`) per
device, each pointing at the same Frappe site — punches all land in the same
Employee Checkin doctype regardless of which device produced them.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `--test-device` times out | Wrong IP, device on a different subnet/VLAN, or port 4370 blocked by firewall |
| `--test-frappe` fails with 401/403 | Wrong API key/secret, or that user lacks permission to read/create Employee Checkin |
| Sync runs but no Checkins appear, log shows "no Employee found with attendance_device_id" | That device user ID isn't set in any Employee's `attendance_device_id` field in Frappe |
| Duplicate records appear | Check `sync_state.json` wasn't deleted/reset — deleting it forces a full `INITIAL_SYNC_DAYS` re-pull |
| Script works manually but not via systemd | Check `WorkingDirectory` and `ExecStart` paths in `essl_sync.service` match where you actually installed it, and that `essl-sync` user can read `.env` |