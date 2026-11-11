"""The data lake: syslog and traps from the device, and automated test results around the event."""

from __future__ import annotations

import os
import re
import time

from ts.sources import Doc, source

HOST = os.environ.get("TS_DATALAKE", "10.0.0.148")
# Everyday chatter: MAC learning, LLDP, read-only polling, config saves (counted elsewhere).
_NOISE = re.compile(r"30065\.3\.2\.0|8802\.1\.1\.2|COMMAND=.*\bshow\b|session (opened|closed) for user|30065\.3\.9\.3\.0\.1")
_TRAP_NAMES = {"1.3.6.1.6.3.1.1.5.3": "linkDown", "1.3.6.1.6.3.1.1.5.4": "linkUp", "1.3.6.1.6.3.1.1.5.1": "coldStart",
               "1.3.6.1.2.1.14.16.2.2": "ospfNbrStateChange", "1.3.6.1.2.1.68.0.1": "vrrpNewMaster", "1.3.6.1.2.1.131.0.1": "entityStateChange"}
_TRAP_OID = re.compile(r"iso\.3\.6\.1\.6\.3\.1\.1\.4\.1\.0 = OID: iso\.([\d.]+)")
LIMIT = 60


def _db():
    import pymongo
    return pymongo.MongoClient(HOST, 27017, serverSelectionTimeoutMS=4000, socketTimeoutMS=15000)["datalake"]


def _days(start: float, end: float) -> list[str]:
    days, t = [], start
    while True:
        day = time.strftime("%Y_%m_%d", time.gmtime(t))
        if day not in days:
            days.append(day)
        if t >= end:
            return days
        t = min(end, t + 86400)


def _title(doc: dict) -> str:
    raw = doc.get("raw_payload", "")
    if doc.get("protocol") == "snmp_trap":
        m = _TRAP_OID.search(raw)
        name = _TRAP_NAMES.get("1." + m.group(1).removeprefix("1."), m.group(1) if m else "trap") if m else "trap"
        return f"Trap {name} from {doc.get('source_device')}"
    return re.sub(r"^\w+\s+\d+\s+[\d:]+\s+", "", raw)[:220]


@source("datalake")
def events(start: float, end: float, device: str | None = None) -> list[Doc]:
    db = _db()
    docs = []
    for day in _days(start, end):
        query = {"timestamp": {"$gte": start, "$lte": end}, "protocol": {"$in": ["syslog", "snmp_trap"]}}
        if device:
            query["source_device"] = device
        for doc in db[day].find(query, {"_id": 0}).sort("timestamp", -1).limit(600):
            if not _NOISE.search(doc.get("raw_payload", "")):
                docs.append(Doc("datalake", float(doc["timestamp"]), _title(doc), device=doc.get("source_device", "")))
    docs = docs[:LIMIT]
    failed = db["test_results"].find({"timestamp": {"$gte": start, "$lte": end}, "status": {"$in": ["fail", "error"]}}, {"_id": 0}).limit(20)
    docs += [Doc("datalake", float(t["timestamp"]), f"Test failed: {t['suite']}.{t['name']}", f"Job {t.get('job')} #{t.get('build')}") for t in failed]
    return docs
