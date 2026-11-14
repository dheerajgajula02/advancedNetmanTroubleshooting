"""Salesforce Service Cloud, through its REST API: cases, case comments and contacts.

It signs in with an External Client App's client-credentials flow, so no person's password is kept.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from ts.connectors import ConnectorError

LOGIN_FILE = "salesforce.env"
REQUIRED = ("SF_DOMAIN", "SF_CLIENT_ID", "SF_CLIENT_SECRET")
API = "v62.0"
STATUS = {"new": "New", "working": "Working", "confirming": "Working", "workaround": "Working", "needs_agent": "Escalated",
          "solved": "Closed", "no_fault": "Closed", "closed": "Closed"}


def _when(stamp: str) -> float:
    return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() if stamp else 0.0


def _soql_time(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Connector:
    name, label = "salesforce", "Salesforce"

    def __init__(self, settings: dict, session: requests.Session | None = None):
        domain = settings["SF_DOMAIN"].strip().rstrip("/")
        self.domain = domain if domain.startswith("http") else f"https://{domain}"
        self.client = (settings["SF_CLIENT_ID"], settings["SF_CLIENT_SECRET"])
        self.http = session or requests.Session()
        self.token, self.instance, self.me = "", self.domain, ""

    # --- the wire ---------------------------------------------------------------
    def _sign_in(self) -> None:
        try:
            r = self.http.post(f"{self.domain}/services/oauth2/token", data={"grant_type": "client_credentials", "client_id": self.client[0], "client_secret": self.client[1]}, timeout=30)
        except requests.RequestException as exc:
            raise ConnectorError(f"Salesforce did not answer: {type(exc).__name__}") from exc
        if r.status_code >= 400:
            raise ConnectorError(f"Salesforce refused the sign-in ({r.status_code}): {r.text[:300]}")
        data = r.json()
        self.token, self.instance = data["access_token"], data.get("instance_url") or self.domain
        self.me = (data.get("id") or "").rstrip("/").split("/")[-1]  # the run-as user, so we know our own comments

    def _call(self, method: str, path: str, again: bool = True, **kw) -> dict:
        if not self.token:
            self._sign_in()
        try:
            r = self.http.request(method, f"{self.instance}/services/data/{API}/{path}", headers={"Authorization": f"Bearer {self.token}"}, timeout=30, **kw)
        except requests.RequestException as exc:
            raise ConnectorError(f"Salesforce did not answer: {type(exc).__name__}") from exc
        if r.status_code == 401 and again:
            self.token = ""
            return self._call(method, path, again=False, **kw)
        if r.status_code >= 400:
            raise ConnectorError(f"Salesforce said {r.status_code} to {method} {path.split('?')[0]}: {r.text[:300]}")
        return r.json() if r.text else {}

    def _query(self, soql: str) -> list[dict]:
        return self._call("GET", "query", params={"q": soql})["records"]

    def link(self, ext_id: str) -> str:
        return f"{self.instance}/lightning/r/Case/{ext_id}/view"

    def ping(self) -> str:
        self._sign_in()
        return f"signed in to {self.instance}"

    # --- their tickets, to us ----------------------------------------------------------
    def new_tickets(self, since: float) -> list[dict]:
        rows = self._query("SELECT Id, CaseNumber, Subject, Description, Contact.Name, Contact.Email, SuppliedName, SuppliedEmail, CreatedDate, CreatedById "
                           f"FROM Case WHERE CreatedDate >= {_soql_time(since)} ORDER BY CreatedDate LIMIT 200")
        out = []
        for row in rows:
            if row.get("CreatedById") == self.me and not row.get("Contact"):
                continue  # one we opened ourselves
            contact = row.get("Contact") or {}
            out.append({"id": row["Id"], "number": row.get("CaseNumber", ""), "created": _when(row.get("CreatedDate", "")),
                        "caller_name": contact.get("Name") or row.get("SuppliedName") or "", "caller_email": contact.get("Email") or row.get("SuppliedEmail") or "",
                        "subject": row.get("Subject") or "", "body": row.get("Description") or ""})
        return out

    def comments(self, ext_id: str) -> list[dict]:
        rows = self._query(f"SELECT Id, CommentBody, CreatedDate, CreatedById FROM CaseComment WHERE ParentId = {_quote(ext_id)} ORDER BY CreatedDate")
        return [{"id": r["Id"], "time": _when(r.get("CreatedDate", "")), "body": r.get("CommentBody") or "", "author": r.get("CreatedById", "")} for r in rows]

    # --- from us, to them -------------------------------------------------------------
    def add_comment(self, ext_id: str, body: str, public: bool) -> None:
        self._call("POST", "sobjects/CaseComment", json={"ParentId": ext_id, "CommentBody": body[:4000], "IsPublished": public})

    def set_state(self, ext_id: str, status: str, note: str = "") -> None:
        self._call("PATCH", f"sobjects/Case/{ext_id}", json={"Status": STATUS.get(status, "Working")})

    def open_ticket(self, subject: str, body: str, fixed: bool) -> dict:
        made = self._call("POST", "sobjects/Case", json={"Subject": subject[:255], "Description": body[:31000], "Origin": "Web", "Status": "New",
                                                          "Priority": "Medium" if fixed else "High"})
        ext_id = made["id"]
        row = self._query(f"SELECT CaseNumber FROM Case WHERE Id = {_quote(ext_id)}")
        if fixed:
            self.add_comment(ext_id, "Found and fixed automatically by the network troubleshooter; the whole story is in the description.", public=False)
            self.set_state(ext_id, "solved")
        else:
            self.set_state(ext_id, "needs_agent")
        return {"id": ext_id, "number": row[0]["CaseNumber"] if row else "", "url": self.link(ext_id)}

    # --- for the demo and for testing ---------------------------------------------------
    def _contact(self, person: dict) -> str | None:
        rows = self._query(f"SELECT Id FROM Contact WHERE Name = {_quote(person['name'])} LIMIT 1")
        return rows[0]["Id"] if rows else None

    def ensure_people(self, people: list[dict]) -> list[str]:
        made = []
        for p in people:
            if not self._contact(p):
                first, last = p["name"].split()[0], p["name"].split()[-1]
                self._call("POST", "sobjects/Contact", json={"FirstName": first, "LastName": last, "Email": f"{first}.{last}@example.com".lower(), "Department": p["team"]})
                made.append(p["name"])
        return made

    def file_as(self, person: dict, subject: str, body: str) -> dict:
        contact = self._contact(person)
        if not contact:
            raise ConnectorError(f"{person['name']} is not a Salesforce contact yet; run: python -m ts connector setup salesforce")
        made = self._call("POST", "sobjects/Case", json={"ContactId": contact, "Subject": subject[:255], "Description": body, "Origin": "Web", "Status": "New"})
        row = self._query(f"SELECT CaseNumber FROM Case WHERE Id = {_quote(made['id'])}")
        return {"id": made["id"], "number": row[0]["CaseNumber"] if row else "", "url": self.link(made["id"])}

    def reply_as(self, ext_id: str, body: str) -> None:
        self._call("POST", "sobjects/CaseComment", json={"ParentId": ext_id, "CommentBody": body, "IsPublished": True})
