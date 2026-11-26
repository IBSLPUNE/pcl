"""IN/OUT direction for E-timeoffice check-ins, taken from the API 3 payload.

New file. Changes no existing code, fixtures, shifts, or sync cursors.

Rules:
- A vendor IN or OUT time, combined with its DateString, gives an absolute datetime.
  An OUT earlier than its IN is treated as the next day (overnight shift).
- A blank Employee Checkin gets IN or OUT only if it falls within TOLERANCE of
  exactly one vendor time for that employee.
- Punches with no match, or two matches, stay blank and are reported.
- Existing log_type values are never changed.
"""
import json
from collections import defaultdict
from datetime import datetime, time, timedelta

import frappe

DEVICE_ID = "E-TIMEOFFICE"
TOLERANCE = timedelta(minutes=10)


def _hhmm(value):
    if not value or value.strip() in ("", "--:--"):
        return None
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def load_anchors(payload):
    """Accepts the API 3 dict or a path to its saved JSON.

    Returns {Empcode: [(kind, datetime), ...]}.
    """
    if isinstance(payload, str):
        with open(payload) as f:
            payload = json.load(f)

    anchors = defaultdict(list)
    for row in payload.get("InOutPunchData", []):
        day = datetime.strptime(row["DateString"], "%d/%m/%Y").date()
        code = row["Empcode"]
        t_in = _hhmm(row.get("INTime"))
        t_out = _hhmm(row.get("OUTTime"))
        if t_in:
            anchors[code].append(("IN", datetime.combine(day, t_in)))
        if t_out:
            out_dt = datetime.combine(day, t_out)
            if t_in and t_out <= t_in:
                out_dt += timedelta(days=1)
            anchors[code].append(("OUT", out_dt))
    return anchors


def assign_from_anchors(anchors, code_to_employee, start, end=None, dry_run=True):
    """Set log_type on blank E-timeoffice check-ins that match a vendor IN/OUT time.

    anchors: output of load_anchors().
    code_to_employee: {Empcode: Employee name}, the same mapping the sync uses.
    start, end: datetime.date. end is exclusive. None means no upper bound.
    Writes only when dry_run is False. The caller commits.
    """
    employee_to_code = {v: k for k, v in code_to_employee.items()}

    filters = [
        ["device_id", "=", DEVICE_ID],
        ["time", ">=", datetime.combine(start, time.min)],
    ]
    if end:
        filters.append(["time", "<", datetime.combine(end, time.min)])

    rows = frappe.get_all(
        "Employee Checkin",
        filters=filters,
        fields=["name", "employee", "time", "log_type"],
        order_by="employee asc, time asc",
        limit_page_length=0,
    )

    by_employee = defaultdict(list)
    for r in rows:
        by_employee[r.employee].append(r)

    updates = []
    report = {
        "matched": 0,
        "already_set": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "no_code_mapping": [],
        "sequence_warnings": [],
    }

    for employee, punches in by_employee.items():
        code = employee_to_code.get(employee)
        if code is None:
            report["no_code_mapping"].append(employee)
            continue

        anchor_list = anchors.get(code, [])
        labels = []
        for p in punches:
            if p.log_type:
                report["already_set"] += 1
                labels.append(p.log_type)
                continue

            hits = {kind for kind, adt in anchor_list if abs(adt - p.time) <= TOLERANCE}
            if len(hits) == 1:
                kind = hits.pop()
                updates.append((p.name, kind))
                report["matched"] += 1
                labels.append(kind)
            elif not hits:
                report["unmatched"] += 1
                labels.append(None)
            else:
                report["ambiguous"] += 1
                labels.append(None)

        # Two IN labels in a row (or two OUTs) usually means a missing punch.
        # Reported for review, not blocked.
        known = [x for x in labels if x]
        repeats = sum(1 for a, b in zip(known, known[1:]) if a == b)
        if repeats:
            report["sequence_warnings"].append((employee, repeats))

    if not dry_run:
        for name, kind in updates:
            frappe.db.set_value("Employee Checkin", name, "log_type", kind, update_modified=False)

    return {"dry_run": dry_run, "count": len(updates), "updates": updates, "report": report}