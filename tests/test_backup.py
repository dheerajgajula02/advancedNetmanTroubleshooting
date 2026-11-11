"""The parts of the nightly backup that decide whether a restore came back whole."""

import importlib.util
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "deploy" / "backup" / "boulder_backup.py"
spec = importlib.util.spec_from_file_location("boulder_backup", HERE)
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)


def test_rows_are_counted_per_table_from_a_data_only_dump():
    dump = [
        "SET statement_timeout = 0;\n",
        "COPY public.dcim_device (id, name) FROM stdin;\n",
        "1\tr1\n", "2\tr2\\nwith an escaped newline\n",
        "\\.\n",
        "COPY public.empty (id) FROM stdin;\n",
        "\\.\n",
        'COPY public."Mixed" (id) FROM stdin;\n',
        "7\n",
        "\\.\n",
    ]
    assert bb.copy_counts(dump) == {"public.dcim_device": 2, "public.empty": 0, 'public."Mixed"': 1}


def test_timescaledb_catalog_is_left_out_of_the_row_comparison():
    counts = {"public.episodes": 5, "_timescaledb_catalog.metadata": 3, "_timescaledb_config.bgw_job": 1}
    assert bb.user_tables(counts) == {"public.episodes": 5}


def test_sqlite_counts_cover_every_ordinary_table(tmp_path):
    db = sqlite3.connect(tmp_path / "x.sqlite")
    db.executescript("create table a(x); insert into a values (1),(2); create table b(y);")
    db.commit()
    db.close()
    assert bb.sqlite_counts(tmp_path / "x.sqlite") == {"a": 2, "b": 0}


def test_only_dated_folders_count_as_backups():
    assert bb.STAMP_RE.match("2026-11-11")
    assert not bb.STAMP_RE.match(".")
    assert not bb.STAMP_RE.match("2026-11-11.partial")
