import hashlib
import os
import sqlite3
import time

DB_PATH = os.getenv("DB_PATH", "newsbot.db")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    return c


def init():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS seen (h TEXT PRIMARY KEY, ts INTEGER)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS subs ("
            "chat_id INTEGER, thread_id INTEGER, category TEXT, "
            "PRIMARY KEY (chat_id, thread_id))"
        )
        c.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - 3 * 86400,))


def _h(url):
    return hashlib.sha1(url.encode()).hexdigest()


def is_seen(url):
    with _conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE h=?", (_h(url),)).fetchone() is not None


def mark_seen(urls):
    now = int(time.time())
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(_h(u), now) for u in urls])


def add_sub(chat_id, thread_id, category):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO subs VALUES (?,?,?)", (chat_id, thread_id, category))


def remove_sub(chat_id, thread_id):
    with _conn() as c:
        c.execute("DELETE FROM subs WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))


def subs():
    with _conn() as c:
        return c.execute("SELECT chat_id, thread_id, category FROM subs").fetchall()


def sub_for(chat_id, thread_id):
    with _conn() as c:
        return c.execute(
            "SELECT category FROM subs WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)
        ).fetchone()
