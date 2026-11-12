import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import now_datetime

from purbanchal_attendance.etimeoffice.client import EtimeofficeClient

SETTINGS = "Etimeoffice Settings"
MAX_BATCHES = 50  # safety limit per run
RECORD_RE = re.compile(r"^\d{6}\$\d+$")


def scheduled_sync():
    settings = frappe.get_single(SETTINGS)
    if settings.enabled:
        run_sync(settings)


@frappe.whitelist()
def run_sync_now():
    frappe.only_for("System Manager")
    return run_sync(frappe.get_single(SETTINGS))


@frappe.whitelist()
def test_connection():
    frappe.only_for("System Manager")
    data = EtimeofficeClient(frappe.get_single(SETTINGS)).test_connection()
    return {"ok": True, "punches_last_24h": len(_punches_from(data))}


def run_sync(settings):
    dry_run = bool(settings.dry_run)
    empcode = (settings.empcode or "ALL").strip()

    # Save the cursor only when pulling ALL employees. A single-employee pull
    # could move the cursor past other employees' punches.
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
    stats = {"fetched": 0, "created": 0, "skipped": 0}
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
    log.row_errors = "\n".join(row_errors)

    if log.status == "Success":
        if dry_run:
            log.status = "Dry Run"
        elif unmapped or row_errors:
            log.status = "Partial"

    log.finished_on = now_datetime()
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def _punches_from(data):
    if isinstance(data, list):  # DownloadPunchData returns a plain list
        return data
    return (data or {}).get("PunchData") or []


def _process_batch(punches, stats, unmapped, row_errors, emp_cache, dry_run):
    parsed = []
    for p in punches:
        stats["fetched"] += 1
        etid = str(p.get("ID") or "").strip()
        code = str(p.get("Empcode") or "").strip()
        ptime = _parse_punch_time(p.get("PunchDate"))
        if etid and code and ptime:
            parsed.append((etid, code, ptime))
        else:
            stats["skipped"] += 1
            row_errors.append(f"Unparseable record: {p}")

    ids = list({etid for etid, _code, _time in parsed})
    existing = set()
    if ids:
        existing = set(frappe.get_all(
            "Employee Checkin",
            filters={"etimeoffice_id": ["in", ids]},
            pluck="etimeoffice_id",
        ))

    for etid, code, ptime in parsed:
        if etid in existing:
            stats["skipped"] += 1
            continue

        employee = _find_employee(code, emp_cache)
        if not employee:
            unmapped.add(code)
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["created"] += 1
            continue

        # Savepoint so one bad row doesn't abort the whole batch
        frappe.db.savepoint("etimeoffice_row")
        try:
            frappe.get_doc({
                "doctype": "Employee Checkin",
                "employee": employee,
                "time": ptime,
                "device_id": "E-TIMEOFFICE",
                "etimeoffice_id": etid,
                # log_type left blank: raw punches have no IN/OUT direction.
                # HRMS Shift Type decides it.
            }).insert(ignore_permissions=True)
            existing.add(etid)
            stats["created"] += 1
        except Exception as exc:
            frappe.db.rollback(save_point="etimeoffice_row")
            stats["skipped"] += 1
            row_errors.append(f"ID {etid} | {code} | {ptime}: {exc}")


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


def _parse_punch_time(value):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(str(value), fmt)
        except (ValueError, TypeError):
            continue
    return None