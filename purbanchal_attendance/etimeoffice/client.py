import base64
from datetime import timedelta

import requests
from frappe.utils import now_datetime


class EtimeofficeError(Exception):
    pass


class EtimeofficeClient:
    DATE_FMT = "%d/%m/%Y_%H:%M"

    def __init__(self, settings):
        password = settings.get_password("password", raise_exception=False)
        if not (settings.corporate_id and settings.username and password):
            raise EtimeofficeError("Corporate ID, Username and Password are required in Etimeoffice Settings")

        self.base_url = settings.base_url.rstrip("/")
        self.timeout = int(settings.request_timeout or 60)
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._basic_auth(settings.corporate_id, settings.username, password),
        })

    @staticmethod
    def _basic_auth(corporate_id, username, password):
        # Documented format: Corporateid:Username:Password:True
        raw = f"{corporate_id}:{username}:{password}:True"
        return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _get(self, endpoint, params):
        url = f"{self.base_url}/{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise EtimeofficeError(f"Network error calling {endpoint}: {exc}") from exc

        hints = {
            400: "Bad request. Check parameters and date format.",
            401: "Unauthorized. Check Corporate ID, Username and Password.",
            404: "Endpoint not found. Check Base URL.",
            500: "Server error on e-timeoffice side.",
        }
        if resp.status_code != 200:
            detail = hints.get(resp.status_code, resp.text[:300])
            raise EtimeofficeError(f"HTTP {resp.status_code} from {endpoint}: {detail}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise EtimeofficeError(f"Non-JSON response from {endpoint}") from exc

        if isinstance(data, dict) and data.get("Error"):
            raise EtimeofficeError(f"API error from {endpoint}: {data.get('Msg')}")
        return data

    def download_last_punches(self, last_record, empcode="ALL"):
        return self._get(
            "DownloadLastPunchData",
            {"Empcode": empcode, "LastRecord": last_record},
        )

    def test_connection(self):
        """Read-only check: punches from the last 24 hours."""
        now = now_datetime()
        params = {
            "Empcode": "ALL",
            "FromDate": (now - timedelta(days=1)).strftime(self.DATE_FMT),
            "ToDate": now.strftime(self.DATE_FMT),
        }
        return self._get("DownloadPunchData", params)