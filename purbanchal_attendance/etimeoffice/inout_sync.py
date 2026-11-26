"""Fetch E-timeoffice API 3 and label E-TIMEOFFICE check-ins as IN or OUT.

Label-only: never creates check-ins and never changes an existing log_type.
Matching rules live in inout_from_api.py.
"""
import base64
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

import frappe
import requests
from frappe.utils import cint, getdate, now_datetime

from purbanchal_attendance.etimeoffice.inout_from_api import (
    DEVICE_ID,
    TOLERANCE,
    assign_from_anchors,
    load_anchors,
)

ENDPOINT = "/DownloadInOutPunchData"


def fetch_inout(start, end):
    """Pull API 3 for start..end (dates, end inclusive).

    The vendor rejects the HH:mm suffix on this endpoint, so dates are date-only.
    ToDate is sent one day late because the vendor's upper bound isn't documented.
    The matcher filters to the real window, so the extra day is harmless.
    """
    s = frappe.get_single("Etimeoffice Settings")
    password = s.get_password("password", raise_exception=False)
    if not (s.corporate_id and s.username and password):
        frappe.throw("Corporate ID, Username and Password are required in Etimeoffice Settings")

    raw = f"{s.corporate_id}:{s.username}:{password}:True"
    headers = {
        "Authorization": "Basic " + base64.b64encode(raw.encode()).decode(),
        "Content-Type": "application/json",
    }
    r = requests.get(
        s.base_url.rstrip("/") + ENDPOINT,
        params={
            "Empcode": "ALL",
            "FromDate": start.strftime("%d/%m/%Y"),
            "ToDate": (end + timedelta(days=1)).strftime("%d/%m/%Y"),
        },
        headers=headers,
        timeout=s.request_timeout or 60,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("Error"):
        frappe.throw(f"E-timeoffice error: {body.get('Msg')}")
    return body


def map_codes(codes):
    """Vendor Empcode -> Employee, using the same attendance_device_id lookup as the punch sync.

    Returns ({code: employee}, [codes with no unique Employee]). Never guesses.
    """
    codes = list(codes)
    if not codes:
        return {}, []

    candidates = {c: list({c, c.lstrip("0") or "0"}) for c in codes}
    all_candidates = sorted({x for v in candidates.values() for x in v})

    rows = frappe.get_all(
        "Employee",
        filters={"attendance_device_id": ["in", all_candidates]},
        fields=["name", "attendance_device_id"],
        limit_page_length=0,
    )
    by_device_id = defaultdict(list)
    for r in rows:
        by_device_id[r.attendance_device_id].append(r.name)

    mapping, unmapped = {}, []
    for c in codes:
        matches = set()
        for cand in candidates[c]:
            matches.update(by_device_id.get(cand, []))
        if len(matches) == 1:
            mapping[c] = matches.pop()
        else:
            unmapped.append(c)
    return mapping, sorted(unmapped)


def vendor_times_without_checkin(anchors, code_to_emp, start, end):
    """Report vendor IN/OUT times with no E-TIMEOFFICE check-in within TOLERANCE. Report only."""
    lo = datetime.combine(start, time.min)
    hi = datetime.combine(end + timedelta(days=1), time.min)
    rows = frappe.get_all(
        "Employee Checkin",
        filters=[
            ["device_id", "=", DEVICE_ID],
            ["time", ">=", lo],
            ["time", "<", hi],
        ],
        fields=["employee", "time"],
        limit_page_length=0,
    )
    by_emp = defaultdict(list)
    for r in rows:
        by_emp[r.employee].append(r.time)

    gaps = []
    for code, emp in code_to_emp.items():
        times = by_emp.get(emp, [])
        for kind, adt in anchors.get(code, []):
            if lo <= adt < hi and not any(abs(adt - t) <= TOLERANCE for t in times):
                gaps.append((emp, kind, str(adt)))
    return gaps


def sync_inout_log_types(start, end, dry_run=True, with_gaps=False):
    """Label blank E-TIMEOFFICE check-ins in start..end (dates, end inclusive).

    dry_run=True writes nothing. The caller commits. with_gaps=True also builds
    the missing-punch report, which is slow, so it's for manual checks only.
    """
    body = fetch_inout(start, end)
    anchors = load_anchors(body)
    code_to_emp, unmapped = map_codes(anchors.keys())
    result = assign_from_anchors(
        anchors, code_to_emp,
        start=start, end=end + timedelta(days=1), dry_run=dry_run,
    )
    result["vendor_rows"] = len(body.get("InOutPunchData", []))
    result["unmapped_codes"] = unmapped
    result["vendor_gaps"] = (
        vendor_times_without_checkin(anchors, code_to_emp, start, end) if with_gaps else []
    )
    return result


def label_recent(days=7):
    """Scheduled safety net: label blank E-TIMEOFFICE check-ins from the last N days.

    Does nothing when the integration is disabled, and writes nothing when Dry Run is on.
    """
    settings = frappe.get_single("Etimeoffice Settings")
    if not settings.enabled:
        return
    end = getdate()
    start = end - timedelta(days=days)
    sync_inout_log_types(start, end, dry_run=bool(settings.dry_run))
    frappe.db.commit()

BACKFILL_JOB_ID = "etoff_backfill_labels"


@frappe.whitelist()
def start_label_backfill(dry_run=1):
    """Button handler. Queues a background job that labels every blank E-TIMEOFFICE check-in.

    dry_run=1 only reports what would change. Requires System Manager.
    """
    frappe.only_for("System Manager")
    dry_run = bool(cint(dry_run))

    s = frappe.get_single("Etimeoffice Settings")
    if s.sync_started_on and now_datetime() - s.sync_started_on < timedelta(minutes=30):
        frappe.throw("A sync is running now. Try again when it finishes.")

    frappe.enqueue(
        "purbanchal_attendance.etimeoffice.inout_sync.run_label_backfill",
        queue="long",
        timeout=3600,
        job_id=BACKFILL_JOB_ID,
        deduplicate=True,
        dry_run=dry_run,
        user=frappe.session.user,
    )
    return {"queued": True, "dry_run": dry_run}


LABEL_RUN_TAG = "IN/OUT LABEL RUN"


def backfill_labels(start, end, chunk_days=7, dry_run=True, results=None):
    """Label blank E-TIMEOFFICE check-ins from start to end (dates, inclusive), in chunks.

    Appends one summary per chunk to `results` (if given), so a caller still has
    the completed weeks if a later week fails. Commits after each chunk when dry_run is False.
    """
    chunk_days = max(int(chunk_days), 1)
    results = [] if results is None else results
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        r = sync_inout_log_types(cur, chunk_end, dry_run=dry_run)
        if not dry_run:
            frappe.db.commit()
        results.append({
            "from": str(cur),
            "to": str(chunk_end),
            "count": r["count"],
            "vendor_rows": r["vendor_rows"],
            "unmapped_codes": r["unmapped_codes"],
            "unmapped": len(r["unmapped_codes"]),
            "report": r["report"],
        })
        cur = chunk_end + timedelta(days=1)
    return results


def _write_label_run_log(first, last, dry_run, results, started, status=None, error=None):
    """Save one label run as an Etimeoffice Sync Log row, so it can be reviewed later."""
    labeled = sum(x["count"] for x in results)
    unmatched = sum(x["report"]["unmatched"] for x in results)
    ambiguous = sum(x["report"]["ambiguous"] for x in results)
    unmapped = sorted({c for x in results for c in x["unmapped_codes"]})

    if status is None:
        if dry_run:
            status = "Dry Run"
        elif ambiguous:
            status = "Partial"
        else:
            status = "Success"

    verb = "would label" if dry_run else "labeled"
    chunk_lines = [
        f"{x['from']} to {x['to']}: {verb} {x['count']}, vendor rows {x['vendor_rows']}, "
        f"unmatched {x['report']['unmatched']}, ambiguous {x['report']['ambiguous']}, "
        f"no mapping {len(x['report']['no_code_mapping'])}"
        for x in results
    ]

    log = frappe.get_doc({
        "doctype": "Etimeoffice Sync Log",
        "status": status,
        "started_on": started,
        "finished_on": now_datetime(),
        "empcode": LABEL_RUN_TAG,
        "from_record": str(first),
        "to_record": str(last),
        "fetched": sum(x["vendor_rows"] for x in results),
        "checkins_created": labeled,
        "skipped": unmatched,
        "unmapped_codes": ", ".join(unmapped),
        "row_errors": "\n".join(chunk_lines),
        "fatal_error": error,
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def run_label_backfill(dry_run=True, user=None):
    """Background job. Labels blank E-TIMEOFFICE check-ins and saves a row to Etimeoffice Sync Log."""
    started = now_datetime()
    first, last = frappe.db.sql(
        """
        select date(min(time)), date(max(time)) from `tabEmployee Checkin`
        where device_id = %s and (log_type is null or log_type = '')
        """,
        (DEVICE_ID,),
    )[0]
    if not first:
        _notify(user, "No blank E-TIMEOFFICE check-ins found. Nothing to do.")
        return

    results = []
    try:
        backfill_labels(first, last, chunk_days=7, dry_run=dry_run, results=results)
    except Exception:
        tb = frappe.get_traceback()
        frappe.log_error(title="E-timeoffice label backfill failed", message=tb)
        frappe.db.rollback()  # drop the failed week's uncommitted writes
        log_name = _write_label_run_log(first, last, dry_run, results, started,
                                        status="Failed", error=tb)
        _notify(user, f"Backfill failed after {len(results)} weeks. See {log_name}.", indicator="red")
        return

    log_name = _write_label_run_log(first, last, dry_run, results, started)
    labeled = sum(x["count"] for x in results)
    unmatched = sum(x["report"]["unmatched"] for x in results)
    ambiguous = sum(x["report"]["ambiguous"] for x in results)
    verb = "would be labeled" if dry_run else "labeled"
    prefix = "DRY RUN. " if dry_run else ""
    summary = (
        f"{prefix}{labeled} check-ins {verb} ({first} to {last}). "
        f"Left blank: {unmatched} unmatched, {ambiguous} ambiguous. Saved as {log_name}."
    )
    frappe.logger("etimeoffice").info(summary)
    _notify(user, summary, indicator="orange" if ambiguous else "green")

def _notify(user, message, indicator="green"):
    frappe.publish_realtime(
        "etoff_label_backfill",
        {"message": message, "indicator": indicator},
        user=user,
    )


# ---------- Day fallback: two-punch days confirmed by the vendor's IN and OUT ----------

FALLBACK_TAG = "IN/OUT DAY FALLBACK"
FALLBACK_WINDOW = timedelta(hours=2)
FALLBACK_LINE_LIMIT = 300


def _parse_hhmm(value):
    """Vendor 'HH:mm' -> time. Returns None for blank, '--:--', or unreadable values."""
    text = str(value or "").strip()
    if text in ("", "--:--"):
        return None
    try:
        h, m = text.split(":")
        return time(int(h), int(m))
    except ValueError:
        return None


def plan_day_fallback(start, end):
    """Read-only. Find employee-days where the two-punch rule applies.

    Returns (proposals, skipped, vendor_row_count). Nothing is written.
    """
    body = fetch_inout(start, end)
    vendor_rows = body.get("InOutPunchData", [])

    vendor, duplicates = {}, set()
    for x in vendor_rows:
        key = (x["Empcode"], datetime.strptime(x["DateString"], "%d/%m/%Y").date())
        if key in vendor:
            duplicates.add(key)
        vendor[key] = (_parse_hhmm(x.get("INTime")), _parse_hhmm(x.get("OUTTime")))
    for key in duplicates:
        vendor[key] = (None, None)  # two vendor rows for one day: don't guess

    code_to_emp, _ = map_codes({k[0] for k in vendor})
    emp_to_code = {emp: code for code, emp in code_to_emp.items()}

    lo = datetime.combine(start, time.min)
    hi = datetime.combine(end + timedelta(days=1), time.min)
    checkins = frappe.get_all(
        "Employee Checkin",
        filters=[["device_id", "=", DEVICE_ID], ["time", ">=", lo], ["time", "<", hi]],
        fields=["name", "employee", "time", "log_type"],
        order_by="employee asc, time asc",
        limit_page_length=0,
    )
    days = defaultdict(list)
    for c in checkins:
        days[(c.employee, c.time.date())].append(c)

    proposals, skipped = [], Counter()
    for (employee, day), punches in days.items():
        if len(punches) != 2:
            skipped["not exactly two punches on the day"] += 1
            continue
        if any(p.log_type for p in punches):
            skipped["a punch already labeled"] += 1
            continue
        code = emp_to_code.get(employee)
        if not code:
            skipped["no unique vendor code"] += 1
            continue
        if (code, day) not in vendor:
            skipped["no vendor row for the day"] += 1
            continue
        v_in, v_out = vendor[(code, day)]
        if v_in is None or v_out is None:
            skipped["vendor IN or OUT is --:-- or duplicated"] += 1
            continue
        if v_out <= v_in:
            skipped["vendor OUT not after IN"] += 1
            continue
        first, second = punches
        if (abs(first.time - datetime.combine(day, v_in)) > FALLBACK_WINDOW
                or abs(second.time - datetime.combine(day, v_out)) > FALLBACK_WINDOW):
            skipped["punches more than 2h from vendor IN/OUT"] += 1
            continue
        proposals.append({
            "employee": employee,
            "code": code,
            "day": day,
            "in_name": first.name,
            "out_name": second.name,
            "in_time": first.time,
            "out_time": second.time,
            "vendor_in": v_in,
            "vendor_out": v_out,
        })
    return proposals, skipped, len(vendor_rows)


def _apply_fallback(proposals):
    """Write IN and OUT for each proposal, re-checking that both punches are still blank."""
    written = 0
    for p in proposals:
        if (frappe.db.get_value("Employee Checkin", p["in_name"], "log_type")
                or frappe.db.get_value("Employee Checkin", p["out_name"], "log_type")):
            continue
        frappe.db.set_value("Employee Checkin", p["in_name"], "log_type", "IN", update_modified=False)
        frappe.db.set_value("Employee Checkin", p["out_name"], "log_type", "OUT", update_modified=False)
        written += 2
    return written


def _save_fallback_log(first, last, dry_run, started, status, days, checkins,
                       vendor_rows, skipped, lines, error=None):
    """Save one fallback run to Etimeoffice Sync Log, so it can be reviewed later."""
    header = (
        f"Dry run: {dry_run}. Employee-days {'that would be' if dry_run else ''} labeled: "
        f"{days} ({checkins} check-ins)."
    )
    body = [header, ""]
    body += [f"skipped, {reason}: {n}" for reason, n in skipped.most_common()]
    body.append("")
    body += lines[:FALLBACK_LINE_LIMIT]
    if len(lines) > FALLBACK_LINE_LIMIT:
        body.append(f"... {len(lines) - FALLBACK_LINE_LIMIT} more not shown")

    log = frappe.get_doc({
        "doctype": "Etimeoffice Sync Log",
        "status": status,
        "started_on": started,
        "finished_on": now_datetime(),
        "empcode": FALLBACK_TAG,
        "from_record": str(first),
        "to_record": str(last),
        "fetched": vendor_rows,
        "checkins_created": checkins,
        "skipped": sum(skipped.values()),
        "row_errors": "\n".join(body),
        "fatal_error": error,
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def _notify_fallback(user, message, indicator="green"):
    frappe.publish_realtime(
        "etoff_day_fallback",
        {"message": message, "indicator": indicator},
        user=user,
    )


def run_day_fallback(dry_run=True, user=None, chunk_days=7):
    """Background job. Labels two-punch days confirmed by the vendor, in weekly chunks."""
    started = now_datetime()
    first, last = frappe.db.sql(
        """
        select date(min(time)), date(max(time)) from `tabEmployee Checkin`
        where device_id = %s and (log_type is null or log_type = '')
        """,
        (DEVICE_ID,),
    )[0]
    if not first:
        _notify_fallback(user, "No blank E-TIMEOFFICE check-ins found. Nothing to do.")
        return

    skipped, lines = Counter(), []
    days_total = checkins_total = vendor_total = 0
    cur = first
    try:
        while cur <= last:
            chunk_end = min(cur + timedelta(days=chunk_days - 1), last)
            proposals, chunk_skipped, vendor_count = plan_day_fallback(cur, chunk_end)
            vendor_total += vendor_count
            skipped.update(chunk_skipped)

            if dry_run:
                written = 2 * len(proposals)
            else:
                written = _apply_fallback(proposals)
                frappe.db.commit()  # commit each week, so finished weeks survive a later failure

            days_total += len(proposals)
            checkins_total += written
            for p in proposals:
                lines.append(
                    f"{p['code']} | {p['day']:%d/%m/%Y} | "
                    f"IN {p['in_time']:%H:%M} (vendor {p['vendor_in']:%H:%M}) | "
                    f"OUT {p['out_time']:%H:%M} (vendor {p['vendor_out']:%H:%M})"
                )
            cur = chunk_end + timedelta(days=1)
    except Exception:
        frappe.db.rollback()  # drop the failed week's uncommitted writes
        tb = frappe.get_traceback()
        frappe.log_error(title="E-timeoffice day fallback failed", message=tb)
        log_name = _save_fallback_log(first, last, dry_run, started, "Failed",
                                      days_total, checkins_total, vendor_total,
                                      skipped, lines, error=tb)
        _notify_fallback(user, f"Day fallback failed after {days_total} days. See {log_name}.", "red")
        return

    status = "Dry Run" if dry_run else "Success"
    log_name = _save_fallback_log(first, last, dry_run, started, status,
                                  days_total, checkins_total, vendor_total, skipped, lines)
    prefix = "DRY RUN. " if dry_run else ""
    verb = "would be labeled" if dry_run else "labeled"
    _notify_fallback(
        user,
        f"{prefix}{days_total} days ({checkins_total} check-ins) {verb}. "
        f"Skipped: {sum(skipped.values())} employee-days. Saved as {log_name}.",
    )


@frappe.whitelist()
def start_day_fallback(dry_run=1):
    """Button handler. Queues the day fallback job. System Manager only."""
    frappe.only_for("System Manager")
    dry_run = bool(cint(dry_run))

    s = frappe.get_single("Etimeoffice Settings")
    if s.sync_started_on and now_datetime() - s.sync_started_on < timedelta(minutes=30):
        frappe.throw("A sync is running now. Try again when it finishes.")

    frappe.enqueue(
        "purbanchal_attendance.etimeoffice.inout_sync.run_day_fallback",
        queue="long",
        timeout=3600,
        job_id="etoff_day_fallback",
        deduplicate=True,
        dry_run=dry_run,
        user=frappe.session.user,
    )
    return {"queued": True, "dry_run": dry_run}
