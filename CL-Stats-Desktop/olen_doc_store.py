"""Оленімерні JSON-документи в data/cl_stats.sqlite.

Один файл = одна таблиця з одним рядком payload. Сесії, архів і ledger не чіпає.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path = ""
_noted: set[str] = set()

_DB_NAME = "cl_stats.sqlite"

# basename → (table, meta key)
_DOCS: dict[str, tuple[str, str]] = {
    "olen_history.json": ("olen_history_doc", "olen_history_imported"),
    "olen_accounts.json": ("olen_accounts_doc", "olen_accounts_imported"),
    "olen_seen_replays.json": ("olen_seen_replays_doc", "olen_seen_replays_imported"),
    "olen_pair_db.json": ("olen_pair_db_doc", "olen_pair_db_imported"),
    "olen_platoon_archive.json": ("olen_platoon_archive_doc", "olen_platoon_archive_imported"),
    "olen_advice_archive.json": ("olen_advice_archive_doc", "olen_advice_archive_imported"),
}


class CorruptJson(RuntimeError):
    pass


def _spec(path: str) -> tuple[str, str]:
    base = os.path.basename(path or "")
    spec = _DOCS.get(base)
    if not spec:
        raise KeyError(base)
    return spec


def _schema_sql() -> str:
    parts = [
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    ]
    for table, _meta in _DOCS.values():
        parts.append(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
        )
    return ";\n".join(parts)


def _open(path: str) -> sqlite3.Connection:
    global _conn, _db_path
    db = os.path.join(os.path.dirname(os.path.abspath(path)) or ".", _DB_NAME)
    if _conn is not None and _db_path == db:
        return _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in _schema_sql().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    _conn = conn
    _db_path = db
    return conn


def _write(conn: sqlite3.Connection, table: str, meta: str, data: Any) -> None:
    payload = json.dumps(data, ensure_ascii=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT INTO {table}(id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (payload,),
        )
        conn.execute(
            "INSERT INTO meta(k, v) VALUES (?, '1') "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (meta,),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _retire(path: str) -> None:
    if not path or not os.path.isfile(path):
        return
    bak = path + ".bak"
    try:
        os.replace(path, bak)
        print(f"[OlenDoc] {os.path.basename(path)} → {os.path.basename(bak)}")
    except OSError as e:
        print(f"[OlenDoc] retire {path}: {e}")


def load(path: str) -> Any | None:
    """Документ із SQLite, або одноразова міграція з JSON.

    None — джерела ще немає. Битий JSON → CorruptJson, без запису і без .bak.
    """
    table, meta = _spec(path)
    conn = _open(path)
    with _lock:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (meta,)).fetchone()
        if row and row[0] == "1":
            if path and os.path.isfile(path) and path not in _noted:
                _noted.add(path)
                print(f"[OlenDoc] sqlite є джерелом, JSON не читаю: {os.path.basename(path)}")
            got = conn.execute(f"SELECT payload FROM {table} WHERE id=1").fetchone()
            if not got:
                return None
            return json.loads(got[0])
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[OlenDoc] load {path}: {e}")
            raise CorruptJson(
                "olen JSON unreadable, sqlite migration skipped: " + path
            ) from e
        _write(conn, table, meta, data)
        _retire(path)
        return data


def save(path: str, data: Any) -> None:
    """Запис лише в SQLite. JSON не створює."""
    table, meta = _spec(path)
    conn = _open(path)
    with _lock:
        _write(conn, table, meta, data)


def close() -> None:
    global _conn, _db_path
    if _conn is not None:
        _conn.close()
        _conn = None
    _db_path = ""


def _selfcheck() -> None:
    import shutil
    import tempfile

    def wipe(path: str) -> None:
        close()
        shutil.rmtree(path, ignore_errors=True)

    def seed_sessions(path: str) -> None:
        os.makedirs(path, exist_ok=True)
        conn = sqlite3.connect(os.path.join(path, _DB_NAME))
        conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT, ord INTEGER, payload TEXT)"
        )
        conn.execute(
            "CREATE TABLE ledger_battles (ord INTEGER PRIMARY KEY, id TEXT, payload TEXT)"
        )
        conn.execute("INSERT INTO meta(k, v) VALUES ('sessions_imported', '1')")
        conn.execute("INSERT INTO meta(k, v) VALUES ('ledger_imported', '1')")
        conn.execute(
            "INSERT INTO sessions(id, source, ord, payload) VALUES (?,?,?,?)",
            ("keep", "wg", 0, '{"id":"keep"}'),
        )
        conn.execute(
            "INSERT INTO ledger_battles(ord, id, payload) VALUES (?,?,?)",
            (0, "L1", '{"id":"L1"}'),
        )
        conn.commit()
        conn.close()

    def others_ok() -> None:
        assert _conn is not None
        assert _conn.execute(
            "SELECT v FROM meta WHERE k='sessions_imported'"
        ).fetchone()[0] == "1"
        assert _conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert _conn.execute(
            "SELECT v FROM meta WHERE k='ledger_imported'"
        ).fetchone()[0] == "1"
        assert _conn.execute("SELECT COUNT(*) FROM ledger_battles").fetchone()[0] == 1

    bad = tempfile.mkdtemp(prefix="clstats-olen-bad-")
    try:
        seed_sessions(bad)
        hist = os.path.join(bad, "olen_history.json")
        with open(hist, "w", encoding="utf-8") as f:
            json.dump({"battles": [{"id": "b1"}], "sittingId": 3}, f)
        assert load(hist)["battles"][0]["id"] == "b1"
        assert not os.path.isfile(hist)
        acc = os.path.join(bad, "olen_accounts.json")
        with open(acc, "w", encoding="utf-8") as f:
            f.write("{")
        try:
            load(acc)
            raise AssertionError("corrupt accounts must not migrate")
        except CorruptJson as e:
            assert "unreadable" in str(e)
        assert os.path.isfile(acc)
        assert not os.path.isfile(acc + ".bak")
        assert _conn.execute(
            "SELECT v FROM meta WHERE k='olen_accounts_imported'"
        ).fetchone() is None
        assert _conn.execute(
            "SELECT v FROM meta WHERE k='olen_history_imported'"
        ).fetchone()[0] == "1"
        others_ok()
    finally:
        wipe(bad)

    td = tempfile.mkdtemp(prefix="clstats-olen-")
    try:
        seed_sessions(td)
        docs = {
            "olen_history.json": {"battles": [{"id": "h"}], "history": [], "sittingId": 1},
            "olen_accounts.json": {"accounts": [{"slot": 1, "nicks": ["A"]}]},
            "olen_seen_replays.json": {"names": ["r1.wotbreplay", "r2.wotbreplay"]},
            "olen_pair_db.json": {
                "version": 1, "battles_ingested": 2, "seen_ids": ["a", "b"],
                "recent": [], "by_class": {}, "by_tanks": {}, "by_mate_tanks": {},
            },
            "olen_platoon_archive.json": {"items": [{"id": "p", "battles": 4}], "open": None},
            "olen_advice_archive.json": {
                "ver": 1, "items": [{"id": "adv", "open": True}], "stats": {},
            },
        }
        for name, data in docs.items():
            with open(os.path.join(td, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        loaded = {name: load(os.path.join(td, name)) for name in docs}
        assert loaded == docs
        for name in docs:
            assert not os.path.isfile(os.path.join(td, name))
            assert os.path.isfile(os.path.join(td, name + ".bak"))
        others_ok()
        save(os.path.join(td, "olen_seen_replays.json"), {"names": ["r1.wotbreplay"]})
        again = load(os.path.join(td, "olen_seen_replays.json"))
        assert again["names"] == ["r1.wotbreplay"]
        assert load(os.path.join(td, "olen_history.json")) == docs["olen_history.json"]

        close()
        os.replace(
            os.path.join(td, "olen_history.json.bak"),
            os.path.join(td, "olen_history.json"),
        )
        conn = sqlite3.connect(os.path.join(td, _DB_NAME))
        conn.execute("DELETE FROM olen_history_doc")
        conn.execute("DELETE FROM meta WHERE k='olen_history_imported'")
        conn.commit()
        conn.close()
        assert load(os.path.join(td, "olen_history.json")) == docs["olen_history.json"]
        assert load(os.path.join(td, "olen_accounts.json")) == docs["olen_accounts.json"]
        others_ok()
    finally:
        wipe(td)

    wire = tempfile.mkdtemp(prefix="clstats-olen-wire-")
    try:
        seed_sessions(wire)
        acc_path = os.path.join(wire, "olen_accounts.json")
        with open(acc_path, "w", encoding="utf-8") as f:
            json.dump(
                {"accounts": [{"slot": 1, "role": "stats", "label": "1", "nicks": ["Probe"]}]},
                f,
            )
        from olen_random import AccountBook
        book = AccountBook(wire)
        assert any("Probe" in a["nicks"] for a in book.list())
        assert os.path.isfile(acc_path + ".bak")
        assert not os.path.isfile(acc_path)
        book.replace([
            {"slot": 1, "role": "stats", "label": "1", "nicks": ["Probe"]},
            {"slot": 2, "role": "stats", "label": "2", "nicks": ["Next"]},
        ])
        again = AccountBook(wire)
        nicks = {n for a in again.list() for n in a["nicks"]}
        assert nicks == {"Probe", "Next"}
        chk = sqlite3.connect(os.path.join(wire, _DB_NAME))
        assert chk.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert chk.execute(
            "SELECT v FROM meta WHERE k='sessions_imported'"
        ).fetchone()[0] == "1"
        assert chk.execute("SELECT COUNT(*) FROM ledger_battles").fetchone()[0] == 1
        chk.close()

        import platoon_pair_db as pdb
        orig_path = pdb._data_path
        pair_path = os.path.join(wire, "olen_pair_db.json")
        with open(pair_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "battles_ingested": 2,
                    "seen_ids": ["a", "b"],
                    "recent": [],
                    "by_class": {},
                    "by_tanks": {},
                    "by_mate_tanks": {},
                    "_tank_aliases_merged_v2": True,
                },
                f,
            )
        pdb._DB = None
        pdb._data_path = lambda: pair_path
        try:
            got = pdb.load_db(force=True)
            assert int(got.get("battles_ingested") or 0) == 2
            assert list(got.get("seen_ids") or []) == ["a", "b"]
            assert not os.path.isfile(pair_path)
            assert os.path.isfile(pair_path + ".bak")
        finally:
            pdb._DB = None
            pdb._data_path = orig_path
        chk = sqlite3.connect(os.path.join(wire, _DB_NAME))
        assert chk.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert chk.execute("SELECT COUNT(*) FROM ledger_battles").fetchone()[0] == 1
        assert chk.execute("SELECT COUNT(*) FROM olen_history_doc").fetchone()[0] == 0
        assert chk.execute(
            "SELECT COUNT(*) FROM olen_accounts_doc"
        ).fetchone()[0] == 1
        assert chk.execute(
            "SELECT COUNT(*) FROM olen_pair_db_doc"
        ).fetchone()[0] == 1
        chk.close()
    finally:
        wipe(wire)


if __name__ == "__main__":
    _selfcheck()
    print("olen_doc_store selfcheck ok")
