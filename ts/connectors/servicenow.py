"""ServiceNow, through its Table API: incidents, their comments and work notes, and the people who call."""

from __future__ import annotations

import calendar
import time

import requests

from ts.connectors import ConnectorError

LOGIN_FILE = "servicenow.env"
REQUIRED = ("SN_INSTANCE", "SN_USER", "SN_PASSWORD")
CATEGORY = "network"          # the incidents that are ours: category Network
NEW, IN_PROGRESS, ON_HOLD, RESOLVED, CLOSED = "1", "2", "3", "6", "7"
AWAITING_CALLER = "1"
CLOSE_WORDS = {"solved": ("permanent fix", "solution provided", "solved (permanently)", "solved remotely"), "workaround": ("workaround", "work around"),
               "no_fault": ("fixed without action", "not reproducible", "no resolution", "not solved")}
NEVER = ("known error", "duplicate", "problem")  # these need a knowledge article or another record


def _utc(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))) if stamp else 0.0


class Connector:
    name, label = "servicenow", "ServiceNow"

    def __init__(self, settings: dict, session: requests.Session | None = None):
        instance = settings["SN_INSTANCE"].strip().rstrip("/")
        self.base = instance if instance.startswith("http") else f"https://{instance}.service-now.com"
        self.user = settings["SN_USER"]
        self.http = session or requests.Session()
        self.http.auth = (settings["SN_USER"], settings["SN_PASSWORD"])
        self.http.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self._close_codes: list[dict] | None = None

    # --- the wire ---------------------------------------------------------------
    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, f"{self.base}/api/now/{path}", timeout=30, **kw)
        except requests.RequestException as exc:
            raise ConnectorError(f"ServiceNow did not answer: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            raise ConnectorError(f"ServiceNow said {r.status_code} to {method} {path.split('?')[0]}: {r.text[:300]}")
        return r.json() if r.text else {}

    def _rows(self, table: str, query: str, fields: str, limit: int = 200) -> list[dict]:
        return self._call("GET", f"table/{table}", params={"sysparm_query": query, "sysparm_fields": fields, "sysparm_limit": limit,
                                                          "sysparm_display_value": "all", "sysparm_exclude_reference_link": "true"})["result"]

    @staticmethod
    def _v(row: dict, field: str, display: bool = False) -> str:
        cell = row.get(field)
        if isinstance(cell, dict):
            return str(cell.get("display_value" if display else "value") or "")
        return str(cell or "")

    def link(self, ext_id: str) -> str:
        return f"{self.base}/nav_to.do?uri=incident.do?sys_id={ext_id}"

    def ping(self) -> str:
        rows = self._rows("sys_user", f"user_name={self.user}", "user_name,name", 1)
        return f"signed in to {self.base} as {self._v(rows[0], 'name', True) if rows else self.user}"

    # --- their tickets, to us ----------------------------------------------------------
    def new_tickets(self, since: float) -> list[dict]:
        minutes = max(5, min(7 * 1440, int((time.time() - since) / 60) + 5))
        rows = self._rows("incident", f"category={CATEGORY}^sys_created_on>=javascript:gs.minutesAgoStart({minutes})^ORDERBYsys_created_on",
                          "sys_id,number,short_description,description,caller_id,caller_id.email,sys_created_on,sys_created_by")
        out = []
        for row in rows:
            created = _utc(self._v(row, "sys_created_on"))
            if created < since or self._v(row, "sys_created_by") == self.user and not self._v(row, "caller_id"):
                continue  # older than the connector, or one we opened ourselves
            out.append({"id": self._v(row, "sys_id"), "number": self._v(row, "number"), "created": created,
                        "caller_name": self._v(row, "caller_id", True), "caller_email": self._v(row, "caller_id.email", True),
                        "subject": self._v(row, "short_description", True), "body": self._v(row, "description", True)})
        return out

    def comments(self, ext_id: str) -> list[dict]:
        rows = self._rows("sys_journal_field", f"element_id={ext_id}^element=comments^ORDERBYsys_created_on", "sys_id,value,sys_created_by,sys_created_on", 500)
        return [{"id": self._v(r, "sys_id"), "time": _utc(self._v(r, "sys_created_on")), "body": self._v(r, "value", True),
                 "author": self._v(r, "sys_created_by")} for r in rows]

    # --- from us, to them -------------------------------------------------------------
    def add_comment(self, ext_id: str, body: str, public: bool) -> None:
        self._call("PATCH", f"table/incident/{ext_id}", json={"comments" if public else "work_notes": body[:4000]})

    def _close_code(self, outcome: str) -> str:
        if self._close_codes is None:
            self._close_codes = self._rows("sys_choice", "name=incident^element=close_code^inactive=false^language=en", "value,label", 50)
        usable = [r for r in self._close_codes if not any(w in self._v(r, "label", True).lower() for w in NEVER)]
        for word in CLOSE_WORDS.get(outcome, CLOSE_WORDS["solved"]):  # in order of preference
            for row in usable:
                if word in self._v(row, "label", True).lower():
                    return self._v(row, "value")
        resolved = [r for r in usable if "resolved" in self._v(r, "label", True).lower()]
        return self._v((resolved or usable)[0], "value") if (resolved or usable) else "Solution provided"

    def set_state(self, ext_id: str, status: str, note: str = "") -> None:
        """Our ticket status, as theirs."""
        if status in ("solved", "no_fault", "closed"):
            outcome = "no_fault" if status == "no_fault" else "solved"
            fields = {"state": RESOLVED, "close_code": self._close_code(outcome), "close_notes": (note or "Resolved by the network service desk.")[:4000]}
        elif status == "confirming":
            fields = {"state": ON_HOLD, "hold_reason": AWAITING_CALLER}
        elif status == "new":
            fields = {"state": NEW}
        else:  # working, workaround, needs_agent
            fields = {"state": IN_PROGRESS}
        self._call("PATCH", f"table/incident/{ext_id}", json=fields)

    def open_ticket(self, subject: str, body: str, fixed: bool) -> dict:
        """A ticket for something we found ourselves. Resolved at once when it is already fixed."""
        row = self._call("POST", "table/incident", json={"short_description": subject[:160], "description": body[:30000], "category": CATEGORY,
                                                          "impact": "2", "urgency": "2" if fixed else "1"})["result"]
        ext_id = row["sys_id"]
        if fixed:
            self.set_state(ext_id, "solved", "Found and fixed automatically by the network troubleshooter; the whole story is in the description.")
        else:
            self.set_state(ext_id, "working")
            self.add_comment(ext_id, "The troubleshooter could not settle this on its own. A network engineer needs to look.", public=False)
        return {"id": ext_id, "number": row.get("number", ""), "url": self.link(ext_id)}

    # --- for the demo and for testing ---------------------------------------------------
    def ensure_people(self, people: list[dict]) -> list[str]:
        made = []
        for p in people:
            first, last = p["name"].split()[0], p["name"].split()[-1]
            uid = f"{first}.{last}".lower()
            if not self._rows("sys_user", f"user_name={uid}", "sys_id", 1):
                self._call("POST", "table/sys_user", json={"user_name": uid, "first_name": first, "last_name": last, "email": f"{uid}@example.com",
                                                           "title": p["team"], "location": ""})
                made.append(p["name"])
        return made

    def file_as(self, person: dict, subject: str, body: str) -> dict:
        """File an incident the way that person would from the ServiceNow portal."""
        uid = ".".join([person["name"].split()[0], person["name"].split()[-1]]).lower()
        caller = self._rows("sys_user", f"user_name={uid}", "sys_id", 1)
        if not caller:
            raise ConnectorError(f"{person['name']} is not a ServiceNow user yet; run: python -m ts connector setup servicenow")
        row = self._call("POST", "table/incident", json={"caller_id": self._v(caller[0], "sys_id"), "short_description": subject[:160],
                                                          "description": body, "category": CATEGORY, "contact_type": "self-service"})["result"]
        return {"id": row["sys_id"], "number": row.get("number", ""), "url": self.link(row["sys_id"])}

    def reply_as(self, ext_id: str, body: str) -> None:
        """A comment as the caller would add it (for testing: the sync tells ours apart by what we sent, not by who wrote it)."""
        self._call("PATCH", f"table/incident/{ext_id}", json={"comments": body})
