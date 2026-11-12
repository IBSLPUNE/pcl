import re

import frappe
from frappe import _
from frappe.model.document import Document

RECORD_RE = re.compile(r"^\d{6}\$\d+$")


class EtimeofficeSettings(Document):
    def validate(self):
        self.base_url = (self.base_url or "").rstrip("/")
        if self.initial_last_record and not RECORD_RE.match(self.initial_last_record):
            frappe.throw(_("Initial LastRecord must look like 042026$454 (MMyyyy$ID)"))