; ============================================================
; eSSL Biometric Device -> SQL Server Sync Tool - Configuration
; ============================================================

[Device]
; IP address of the eSSL device (Device menu -> Comm -> Ethernet on the device itself)
ip = 192.168.1.201
; Default ZK protocol port used by eSSL / ZKTeco devices
port = 4370
; Communication password set on the device (0 if none set)
password = 0
; Timeout in seconds for device communication
timeout = 10
; Force UDP instead of TCP (only set true if the device requires it)
force_udp = false

[Database]
; SQL Server connection details
server = localhost
database = iclock
; Use "true" for Windows Authentication (Trusted_Connection), "false" to use username/password below
use_windows_auth = false
username = sa
password = YOUR_PASSWORD_HERE
; ODBC driver installed on this machine - check with odbcinst / "ODBC Data Sources" app
; Common values: "ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server", "SQL Server"
odbc_driver = ODBC Driver 17 for SQL Server

[Table]
; Name of the table to insert attendance punches into.
; If your existing iclock DB already has a CHECKINOUT table (standard ZKTeco/eSSL push-protocol
; schema), keep the defaults below - they match that schema's column names.
; If your table/columns are named differently, just change the values on the right.
table_name = CHECKINOUT
col_userid = USERID
col_checktime = CHECKTIME
col_checktype = CHECKTYPE
col_verifycode = VERIFYCODE
col_sensorid = SENSORID
col_sn = SN
; Set to true on first run if the table does not exist yet and you want the script to create it
auto_create_table = false

[Sync]
; Device serial number / identifier tag to store per punch (helps if you sync multiple devices
; into the same table). Leave blank to fetch the SN from the device automatically.
device_sn =
; If true, clears attendance logs on the device after a successful, verified sync.
; Leave FALSE until you've confirmed syncing is working reliably - device logs are your only
; backup copy of punches if something goes wrong on the DB side.
clear_device_after_sync = false
; Polling interval in minutes when running in continuous (--run) mode
poll_interval_minutes = 5

[Logging]
log_file = essl_sync.log
; DEBUG, INFO, WARNING, ERROR
log_level = INFO