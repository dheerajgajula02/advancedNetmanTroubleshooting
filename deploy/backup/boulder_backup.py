#!/usr/bin/env python3
"""Nightly backup of boulder's own state to pve2, and a check that the copy really restores.

    boulder_backup.py run     make today's backup and copy it to pve2
    boulder_backup.py check   fetch the newest copy back from pve2 and restore every piece of it

What is kept: Netman's accounts and state, the troubleshooter's data and secrets, the NetBox,
Guacamole and training databases, and Jenkins's configuration (jobs, credentials, users; no build
history). The data lake on pve is not part of this.

Runs as root from a systemd timer (see install.sh). The result of the last run and of the last
check go to /var/lib/boulder-backup/status.json, which the health check reads.
Standard library only, so it runs on the system Python.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TS = Path("/home/dheeraj/advancedNetman/advancedNetmanTroubleshooting")
LOCAL = Path("/var/backups/boulder")
STATUS = Path("/var/lib/boulder-backup/status.json")
REMOTE = "boulderbak@10.9.8.123"
KEY = "/root/.ssh/boulder-backup"
KEEP_LOCAL = 2
STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SQLITE = {
    "netman-access": Path("/var/lib/netman/access/access.sqlite"),
    "ts-history": TS / "data/history.sqlite",
    "ts-brains": TS / "data/brains.sqlite",
}
POSTGRES = ["netbox-postgres", "guacamole-postgres", "ts-training-db"]
TREES = {  # name -> (folder, patterns left out); the SQLite files are copied safely on their own
    "netman-state": (Path("/var/lib/netman"), ["./access"]),
    "ts-data": (TS / "data", ["*.sqlite", "*.sqlite-wal", "*.sqlite-shm"]),
    "ts-secrets": (TS / "secrets", []),
}
JENKINS = Path("/var/lib/jenkins")
JENKINS_TOP = ["secret.key", "secret.key.not-so-secret", "identity.key.enc"]
JENKINS_DIRS = ["secrets", "users", "nodes", "init.groovy.d"]

# Copies keep this side's owner and stay private: pve2's account numbers mean other users here.
RSYNC = ["rsync", "-rlt", "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo="]
SSH = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=20"]


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def save_status(part: str, result: dict) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        status = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        status = {}
    status[part] = result
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    tmp.chmod(0o644)
    tmp.replace(STATUS)


# ---------- pieces of a backup ----------

_SQLITE_COPY = """
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=60)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
"""


def copy_sqlite(src: Path, dst: Path) -> dict:
    """A consistent copy while the owner keeps writing. It runs as the file's owner, so SQLite
    never leaves a root-owned -wal or -shm file behind that the service could no longer write."""
    owner = pwd.getpwuid(src.stat().st_uid).pw_name
    with tempfile.TemporaryDirectory() as tmp:
        os.chown(tmp, src.stat().st_uid, -1)
        out = Path(tmp) / "copy.sqlite"
        sh(["runuser", "-u", owner, "--", sys.executable, "-c", _SQLITE_COPY, str(src), str(out)])
        counts = sqlite_counts(out)
        sh(["zstd", "-q", "-19", "-T0", str(out), "-o", str(dst)])
        os.chown(dst, 0, 0)  # zstd copies the owner of its input
    return {"tables": counts}


def sqlite_counts(path: Path) -> dict:
    import sqlite3
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in db.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
        counts = {}
        for name in names:
            try:
                counts[name] = db.execute(f'select count(*) from "{name}"').fetchone()[0]
            except Exception:  # FTS shadow tables and the like may not count on their own
                pass
        return counts
    finally:
        db.close()


def pg_env(container: str) -> dict:
    out = sh(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", container],
             capture_output=True, text=True).stdout
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    image = sh(["docker", "inspect", "-f", "{{.Config.Image}}", container], capture_output=True, text=True).stdout.strip()
    return {"user": env.get("POSTGRES_USER", "postgres"), "db": env.get("POSTGRES_DB", env.get("POSTGRES_USER", "postgres")),
            "image": image}


def copy_counts(dump_sql_lines) -> dict:
    """Rows per table in a data-only plain dump: the lines between each COPY and its end mark."""
    counts, table = {}, None
    for line in dump_sql_lines:
        if table is None:
            m = re.match(r"^COPY (\S+) ", line)
            if m:
                table = m.group(1)
                counts[table] = 0
        elif line.rstrip("\n") == "\\.":
            table = None
        else:
            counts[table] += 1
    return counts


def dump_counts(container: str, dump: Path) -> dict:
    with open(dump, "rb") as f:
        p = subprocess.Popen(["docker", "exec", "-i", container, "pg_restore", "--data-only", "-f", "-"],
                             stdin=f, stdout=subprocess.PIPE, text=True, errors="replace")
        counts = copy_counts(p.stdout)
        if p.wait() != 0:
            raise RuntimeError(f"pg_restore could not read the dump of {container}")
    return counts


def dump_postgres(container: str, dst: Path) -> dict:
    env = pg_env(container)
    with open(dst, "wb") as f:
        sh(["docker", "exec", container, "pg_dump", "-U", env["user"], "-Fc", env["db"]], stdout=f)
    return {**env, "tables": dump_counts(container, dst)}


def tar(cmd: list[str]) -> None:
    """GNU tar exits 1 when a file changed while it was read (a log being written); that copy is
    still good. 2 means it failed."""
    p = subprocess.run(["tar", "--warning=no-file-changed", "--warning=no-file-removed"] + cmd,
                       capture_output=True, text=True)
    if p.returncode > 1:
        raise RuntimeError(f"tar failed: {p.stderr.strip()[-300:]}")


def tar_tree(folder: Path, excludes: list[str], dst: Path) -> dict:
    tar(["--zstd", "-cf", str(dst), "-C", str(folder)] + [f"--exclude={x}" for x in excludes] + ["."])
    return {"files": count_tar(dst)}


def tar_jenkins(dst: Path) -> dict:
    keep = [p.name for p in JENKINS.glob("*.xml")]
    keep += [n for n in JENKINS_TOP if (JENKINS / n).exists()]
    keep += [n for n in JENKINS_DIRS if (JENKINS / n).is_dir()]
    jobs = sh(["find", "jobs", "-name", "builds", "-prune", "-o", "-name", "config.xml", "-print"],
              cwd=JENKINS, capture_output=True, text=True).stdout.split()
    plugins = sorted(p.name for p in (JENKINS / "plugins").glob("*.[jh]pi"))
    with tempfile.NamedTemporaryFile("w", delete=False) as lst:
        lst.write("\n".join(keep + jobs) + "\n")
    try:
        tar(["--zstd", "-cf", str(dst), "-C", str(JENKINS), "-T", lst.name])
    finally:
        os.unlink(lst.name)
    return {"files": count_tar(dst), "jobs": len(jobs), "plugins": plugins}


def count_tar(path: Path) -> int:
    out = sh(["tar", "--zstd", "-tf", str(path)], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if line and not line.endswith("/"))


# ---------- run ----------

def run() -> None:
    started = time.time()
    stamp = dt.date.today().isoformat()
    work = LOCAL / stamp
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, mode=0o700)
    manifest = {"stamp": stamp, "host": os.uname().nodename, "started": started, "pieces": {}}
    pieces = manifest["pieces"]
    try:
        for name, path in SQLITE.items():
            log(f"sqlite {name}")
            pieces[name] = {"kind": "sqlite", "file": f"{name}.sqlite.zst", **copy_sqlite(path, work / f"{name}.sqlite.zst")}
        for container in POSTGRES:
            log(f"postgres {container}")
            pieces[container] = {"kind": "postgres", "file": f"{container}.dump", **dump_postgres(container, work / f"{container}.dump")}
        for name, (folder, excludes) in TREES.items():
            log(f"files {name}")
            pieces[name] = {"kind": "tar", "file": f"{name}.tar.zst", **tar_tree(folder, excludes, work / f"{name}.tar.zst")}
        log("jenkins configuration")
        pieces["jenkins-config"] = {"kind": "tar", "file": "jenkins-config.tar.zst", **tar_jenkins(work / "jenkins-config.tar.zst")}
        for piece in pieces.values():
            f = work / piece["file"]
            piece["bytes"], piece["sha256"] = f.stat().st_size, sha256(f)
        manifest["finished"] = time.time()
        (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
        log(f"copying {stamp} to pve2")
        sh(RSYNC + ["--partial", "-e", " ".join(SSH), f"{work}/", f"{REMOTE}:{stamp}/"])
        for old in sorted(p for p in LOCAL.iterdir() if STAMP_RE.match(p.name))[:-KEEP_LOCAL]:
            shutil.rmtree(old)
        total = sum(p["bytes"] for p in pieces.values())
        save_status("backup", {"ok": True, "at": time.time(), "stamp": stamp, "bytes": total,
                               "seconds": round(time.time() - started), "pieces": sorted(pieces)})
        log(f"done: {len(pieces)} pieces, {total / 1e6:.1f} MB, {time.time() - started:.0f} s")
    except Exception as e:
        save_status("backup", {"ok": False, "at": time.time(), "stamp": stamp, "error": f"{type(e).__name__}: {e}"})
        raise


# ---------- check ----------

def newest_remote() -> str:
    out = sh(["rsync", "--list-only", "-e", " ".join(SSH), f"{REMOTE}:"], capture_output=True, text=True).stdout
    stamps = sorted(line.split()[-1] for line in out.splitlines() if STAMP_RE.match(line.split()[-1]))
    if not stamps:
        raise RuntimeError("no backups on pve2")
    return stamps[-1]


def user_tables(counts: dict) -> dict:
    """The tables that hold the database's own rows; TimescaleDB's catalog is its business."""
    return {t: n for t, n in counts.items() if not t.startswith("_timescaledb")}


def restore_postgres(piece: dict, dump: Path) -> None:
    """Restore into a throwaway server of the same image, with no network, then count the rows."""
    name = f"backup-check-{os.getpid()}"
    user, db = piece["user"], piece["db"]

    def psql(sql: str) -> str:
        p = subprocess.run(["docker", "exec", name, "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
                            "-AtF", "\t", "-c", sql], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"psql: {p.stderr.strip()[-300:]}")
        return p.stdout

    # In memory and without flushing to disk: it is thrown away, and boulder's disk is busy enough.
    sh(["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
        "--tmpfs", "/var/lib/postgresql/data:rw,size=2g", "-e", "POSTGRES_INITDB_ARGS=--no-sync",
        "-e", f"POSTGRES_USER={user}", "-e", f"POSTGRES_DB={db}", "-e", "POSTGRES_PASSWORD=check-only",
        piece["image"], "postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"],
       capture_output=True)
    try:
        for _ in range(180):  # the image initialises with a temporary server first, then starts for real
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            if "init process complete" in logs.stdout + logs.stderr:
                try:
                    psql("select 1")
                    break
                except RuntimeError:
                    pass
            time.sleep(1)
        else:
            raise RuntimeError("the throwaway database never came up")
        timescale = "timescale" in piece["image"]
        if timescale:
            psql("select timescaledb_pre_restore()")
        with open(dump, "rb") as f:
            p = subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", user, "-d", db, "--no-owner",
                                "--exit-on-error"], stdin=f, capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError(f"pg_restore: {p.stderr.strip()[-300:]}")
        if timescale:
            psql("select timescaledb_post_restore()")
        want = user_tables(piece["tables"])
        sql = " union all ".join(f"select '{t}', count(*) from {t}" for t in want) or "select '', 0 where false"
        got = {t: int(n) for t, n in (line.split("\t") for line in psql(sql).splitlines() if line)}
        wrong = {t: (want[t], got.get(t)) for t in want if got.get(t) != want[t]}
        if wrong:
            raise RuntimeError(f"rows differ after restore (wanted, got): {dict(list(wrong.items())[:5])}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def check() -> None:
    started = time.time()
    stamp = "?"
    try:
        stamp = newest_remote()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            here = Path(tmp)
            log(f"fetching {stamp} from pve2")
            sh(RSYNC + ["-e", " ".join(SSH), f"{REMOTE}:{stamp}/", f"{here}/"])
            manifest = json.loads((here / "manifest.json").read_text())
            for name, piece in manifest["pieces"].items():
                f = here / piece["file"]
                if sha256(f) != piece["sha256"]:
                    raise RuntimeError(f"{name}: the copy on pve2 is damaged")
                log(f"restoring {name}")
                if piece["kind"] == "sqlite":
                    raw = here / f"{name}.sqlite"
                    sh(["zstd", "-q", "-d", str(f), "-o", str(raw)])
                    import sqlite3
                    ok = sqlite3.connect(str(raw)).execute("pragma integrity_check").fetchone()[0]
                    if ok != "ok" or sqlite_counts(raw) != piece["tables"]:
                        raise RuntimeError(f"{name}: the database does not come back whole")
                elif piece["kind"] == "postgres":
                    restore_postgres(piece, f)
                else:
                    if count_tar(f) != piece["files"]:
                        raise RuntimeError(f"{name}: files are missing from the archive")
                    out = here / f"x-{name}"
                    out.mkdir()
                    sh(["tar", "--zstd", "-xf", str(f), "-C", str(out)])
        save_status("check", {"ok": True, "at": time.time(), "stamp": stamp, "seconds": round(time.time() - started),
                              "pieces": sorted(manifest["pieces"])})
        log(f"restore check passed for {stamp}")
    except Exception as e:
        save_status("check", {"ok": False, "at": time.time(), "stamp": stamp, "error": f"{type(e).__name__}: {e}"})
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["run", "check"])
    args = ap.parse_args()
    # Stopped from systemd: unwind, so the throwaway server and the fetched copy are removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    (run if args.what == "run" else check)()
    return 0


if __name__ == "__main__":
    sys.exit(main())
