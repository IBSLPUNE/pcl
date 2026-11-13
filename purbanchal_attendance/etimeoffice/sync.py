import re
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import now_datetime

from purbanchal_attendance.etimeoffice.client import EtimeofficeClient

SETTINGS = "Etimeoffice Settings"
PUNCH = "Etimeoffice Punch"
DEVICE_ID = "E-TIMEOFFICE"
MAX_BATCHES = 50
MAX_ROW_ERRORS = 200
LOCK_TIMEOUT = timedelta(minutes=30)
RECORD_RE = re.compile(r"^\d{6}\$\d+$")


# ---------- Entry points ----------

def scheduled_sync():
    """Runs every minute from hooks. Does real work only when enabled and the interval has passed."""
    settings = frappe.get_single(SETTINGS)
    if not settings.enabled:
        return
    interval = timedelta(minutes=max(int(settings.sync_interval_minutes or 15), 1))
    last = settings.last_attempt_on
    if last and now_datetime() - last < interval:
        return
    _locked_run()


@frappe.whitelist()
def run_sync_now():
    frappe.only_for("System Manager")
    frappe.enqueue("purbanchal_attendance.etimeoffice.sync.run_manual_job", queue="long", timeout=3600)
    return {"queued": True}


def run_manual_job():
    _locked_run()


@frappe.whitelist()
def test_connection():
    frappe.only_for("System Manager")
    data = EtimeofficeClient(frappe.get_single(SETTINGS)).test_connection()
    return {"ok": True, "punches_last_24h": len(_punches_from(data))}


@frappe.whitelist()
def retry_unmapped():
    frappe.only_for("System Manager")
    frappe.enqueue("purbanchal_attendance.etimeoffice.sync.retry_unmapped_job", queue="long", timeout=3600)
    return {"queued": True}


def retry_unmapped_job():
    emp_cache = {}
    rows = frappe.get_all(PUNCH, filters={"status": "Unmapped"}, fields=["name", "empcode", "punch_time"])
    fixed = 0
    for row in rows:
        employee = _find_employee(row.empcode, emp_cache)
        if not employee:
            continue
        frappe.db.savepoint("etoff_retry")
        try:
            checkin = frappe.get_doc({
                "doctype": "Employee Checkin",
                "employee": employee,
                "time": row.punch_time,
                "device_id": DEVICE_ID,
            }).insert(ignore_permissions=True)
            frappe.db.set_value(PUNCH, row.name, {
                "status": "Imported",
                "employee": employee,
                "employee_checkin": checkin.name,
                "synced_on": now_datetime(),
            })
            frappe.db.commit()
            fixed += 1
        except Exception:
            frappe.db.rollback(save_point="etoff_retry")
            frappe.log_error(title="Etimeoffice retry failed", message=frappe.get_traceback())
    return fixed


# ---------- Lock and interval ----------

def _locked_run():
    if not _acquire_lock():
        return None
    try:
        return run_sync(frappe.get_single(SETTINGS))
    finally:
        _release_lock()


def _acquire_lock():
    now = now_datetime()
    started = frappe.db.get_single_value(SETTINGS, "sync_started_on")
    if started and now - started < LOCK_TIMEOUT:
        return False  # another run is still active
    frappe.db.set_single_value(SETTINGS, "sync_started_on", now)
    frappe.db.set_single_value(SETTINGS, "last_attempt_on", now)
    frappe.db.commit()
    return True


def _release_lock():
    frappe.db.rollback()
    frappe.db.set_single_value(SETTINGS, "sync_started_on", None)
    frappe.db.commit()


# ---------- Sync ----------

def run_sync(settings):
    dry_run = bool(settings.dry_run)
    empcode = (settings.empcode or "ALL").strip()

    # Move the cursor only when pulling ALL employees, so a single-employee test
    # cannot skip other employees' punches.
    persist_cursor = (not dry_run) and empcode.upper() == "ALL"

    cursor = settings.last_max_record or settings.initial_last_record
    if not cursor:
        frappe.throw(_("Set Initial LastRecord in Etimeoffice Settings first"))

    log = frappe.get_doc({
        "doctype": "Etimeoffice Sync Log",
        "started_on": now_datetime(),
        "empcode": empcode,
        "from_record": cursor,
        "to_record": cursor,
        "status": "Success",
    })
    stats = {"fetched": 0, "created": 0, "skipped": 0, "unmapped": 0, "blocked": False}
    unmapped, row_errors, emp_cache = set(), [], {}

    try:
        client = EtimeofficeClient(settings)
        for _batch_no in range(MAX_BATCHES):
            data = client.download_last_punches(cursor, empcode)
            punches = _punches_from(data)
            new_cursor = data.get("MaxRecord") if isinstance(data, dict) else None
            if not punches:
                break

            _process_batch(punches, stats, unmapped, row_errors, emp_cache, dry_run)
            if stats["blocked"]:
                break  # never move the cursor past a record that failed

            if persist_cursor and new_cursor and RECORD_RE.match(new_cursor):
                frappe.db.set_single_value(SETTINGS, "last_max_record", new_cursor)
                frappe.db.set_single_value(SETTINGS, "last_sync_on", now_datetime())
                frappe.db.commit()

            if not new_cursor or new_cursor == cursor or not RECORD_RE.match(new_cursor):
                break
            cursor = new_cursor
            log.to_record = cursor

    except Exception:
        frappe.db.rollback()
        log.status = "Failed"
        log.fatal_error = frappe.get_traceback()
        frappe.log_error(title="Etimeoffice sync failed", message=log.fatal_error)

    log.fetched = stats["fetched"]
    log.checkins_created = stats["created"]
    log.skipped = stats["skipped"]
    log.unmapped_codes = ", ".join(sorted(unmapped))
    log.row_errors = "\n".join(row_errors[:MAX_ROW_ERRORS])

    if log.status == "Success":
        if stats["blocked"]:
            log.status = "Partial"
        elif dry_run:
            log.status = "Dry Run"
        elif stats["unmapped"]:
            log.status = "Partial"

    log.finished_on = now_datetime()
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def _process_batch(punches, stats, unmapped, row_errors, emp_cache, dry_run):
    for p in punches:
        stats["fetched"] += 1
        table = str(p.get("Table") or "").strip()
        rec_id = str(p.get("ID") or "").strip()
        code = str(p.get("Empcode") or "").strip()
        ptime = _parse_punch_time(p.get("PunchDate"))

        if not (table and rec_id and code and ptime):
            # Stop instead of skipping, so a bad record is never lost silently.
            stats["blocked"] = True
            row_errors.append(f"Unreadable record, sync stopped before it: {p}")
            break

        key = f"{table}:{rec_id}"
        if frappe.db.exists(PUNCH, key):
            stats["skipped"] += 1
            continue

        employee = _find_employee(code, emp_cache)

        if dry_run:
            if employee:
                stats["created"] += 1
            else:
                stats["unmapped"] += 1
                unmapped.add(code)
            continue

        frappe.db.savepoint("etoff_row")
        try:
            checkin_name = None
            if employee:
                checkin = frappe.get_doc({
                    "doctype": "Employee Checkin",
                    "employee": employee,
                    "time": ptime,
                    "device_id": DEVICE_ID,
                }).insert(ignore_permissions=True)
                checkin_name = checkin.name

            frappe.get_doc({
                "doctype": PUNCH,
                "punch_key": key,
                "source_table": table,
                "source_id": rec_id,
                "empcode": code,
                "punch_time": ptime,
                "employee": employee,
                "employee_checkin": checkin_name,
                "status": "Imported" if employee else "Unmapped",
                "synced_on": now_datetime(),
            }).insert(ignore_permissions=True)
        except Exception:
            frappe.db.rollback(save_point="etoff_row")
            stats["blocked"] = True
            row_errors.append(f"Row {key} failed, sync stopped before it: {frappe.get_traceback()[-400:]}")
            break

        if employee:
            stats["created"] += 1
        else:
            stats["unmapped"] += 1
            unmapped.add(code)

    frappe.db.commit()


def _find_employee(code, cache):
    if code not in cache:
        candidates = list({code, code.lstrip("0") or "0"})
        matches = frappe.get_all(
            "Employee",
            filters={"attendance_device_id": ["in", candidates]},
            pluck="name",
            limit=2,
        )
        cache[code] = matches[0] if len(matches) == 1 else None
    return cache[code]


def _punches_from(data):
    if isinstance(data, list):
        return data
    return (data or {}).get("PunchData") or []


def _parse_punch_time(value):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(str(value), fmt)
        except (ValueError, TypeError):
            continue
    return None