"""SQLite-backed task tracker.

Implements SPEC.md sections 1-7 (storage, users/projects, tasks, undo,
search, CSV, stats).  Standard library only.
"""

import calendar
import csv
import io
import json
import os
import re
import sqlite3
import threading
from datetime import date, timedelta

__all__ = ["Tracker", "open_db"]

_RECUR_VALUES = ("daily", "weekly", "monthly")
_TASK_KEYS = ("id", "project_id", "title", "assignee_id", "due",
              "priority", "tags", "status", "recur")
_CSV_HEADER = ["id", "project", "title", "assignee", "due",
               "priority", "tags", "status"]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_iso_date(value):
    """Parse a strict YYYY-MM-DD string to a date, else ValueError."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"invalid date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid date: {value!r}") from None


def _validate_title(title):
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    title = title.strip()
    if not title:
        raise ValueError("title must not be empty")
    return title


def _validate_priority(priority):
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority must be an int 1..4")
    if priority < 1 or priority > 4:
        raise ValueError("priority must be 1..4")
    return priority


def _validate_due(due):
    if due is None:
        return None
    # datetime is a subclass of date; the spec says datetime.date, so a
    # datetime is not accepted here.
    if type(due) is not date:
        raise ValueError("due must be a datetime.date or None")
    return due


def _validate_tags(tags):
    if tags is None or isinstance(tags, (str, bytes)):
        raise ValueError("tags must be an iterable of strings")
    try:
        items = list(tags)
    except TypeError:
        raise ValueError("tags must be an iterable of strings") from None
    cleaned = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError("tags must be strings")
        item = item.strip().lower()
        if item:
            cleaned.add(item)
    return sorted(cleaned)


def _validate_recur(recur):
    if recur is None:
        return None
    if recur not in _RECUR_VALUES:
        raise ValueError("recur must be None, 'daily', 'weekly' or 'monthly'")
    return recur


def _next_due(due, recur):
    if recur == "daily":
        return due + timedelta(days=1)
    if recur == "weekly":
        return due + timedelta(days=7)
    # monthly: same day of month next month, clamped to last day.
    year = due.year + (1 if due.month == 12 else 0)
    month = 1 if due.month == 12 else due.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(due.day, last_day))


class Tracker:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._undo = []  # in-memory only; never persisted
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name_lower
                    ON users (lower(name));
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    owner_id INTEGER NOT NULL REFERENCES users(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name_lower
                    ON projects (lower(name));
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL,
                    assignee_id INTEGER REFERENCES users(id),
                    due TEXT,
                    priority INTEGER NOT NULL,
                    tags TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    recur TEXT
                );
                """
            )
            self._conn.commit()

    # -- lifecycle -----------------------------------------------------
    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # -- internal helpers (lock held by callers) -----------------------
    def _row_to_task(self, row):
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "assignee_id": row["assignee_id"],
            "due": date.fromisoformat(row["due"]) if row["due"] else None,
            "priority": row["priority"],
            "tags": list(json.loads(row["tags"])),
            "status": row["status"],
            "recur": row["recur"],
        }

    def _get_row(self, task_id):
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    def _get_task(self, task_id):
        return self._row_to_task(self._get_row(task_id))

    def _user_exists(self, user_id):
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            return False
        return self._conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None

    def _project_exists(self, project_id):
        if isinstance(project_id, bool) or not isinstance(project_id, int):
            return False
        return self._conn.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is not None

    def _find_user_by_name(self, name):
        row = self._conn.execute(
            "SELECT id FROM users WHERE lower(name) = lower(?)",
            (name,)).fetchone()
        return row["id"] if row else None

    def _find_project_by_name(self, name):
        row = self._conn.execute(
            "SELECT id FROM projects WHERE lower(name) = lower(?)",
            (name,)).fetchone()
        return row["id"] if row else None

    def _insert_task_row(self, task):
        """Insert a task dict; task['id'] may be None for a new id."""
        cur = self._conn.execute(
            "INSERT INTO tasks (id, project_id, title, assignee_id, due,"
            " priority, tags, status, recur) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.get("id"),
                task["project_id"],
                task["title"],
                task["assignee_id"],
                task["due"].isoformat() if task["due"] else None,
                task["priority"],
                json.dumps(task["tags"]),
                task["status"],
                task["recur"],
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- users and projects --------------------------------------------
    def add_user(self, name):
        with self._lock:
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            name = name.strip()
            if not name:
                raise ValueError("name must not be empty")
            if self._find_user_by_name(name) is not None:
                raise ValueError(f"user already exists: {name}")
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (name) VALUES (?)", (name,))
            except sqlite3.IntegrityError:
                raise ValueError(f"user already exists: {name}") from None
            self._conn.commit()
            return cur.lastrowid

    def add_project(self, name, owner_id):
        with self._lock:
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            name = name.strip()
            if not name:
                raise ValueError("name must not be empty")
            if not self._user_exists(owner_id):
                raise ValueError(f"unknown owner: {owner_id}")
            if self._find_project_by_name(name) is not None:
                raise ValueError(f"project already exists: {name}")
            try:
                cur = self._conn.execute(
                    "INSERT INTO projects (name, owner_id) VALUES (?, ?)",
                    (name, owner_id))
            except sqlite3.IntegrityError:
                raise ValueError(f"project already exists: {name}") from None
            self._conn.commit()
            return cur.lastrowid

    # -- tasks -----------------------------------------------------------
    def add_task(self, project_id, title, *, assignee_id=None, due=None,
                 priority=2, tags=(), recur=None):
        with self._lock:
            if not self._project_exists(project_id):
                raise ValueError(f"unknown project: {project_id}")
            if assignee_id is not None and not self._user_exists(assignee_id):
                raise ValueError(f"unknown assignee: {assignee_id}")
            title = _validate_title(title)
            due = _validate_due(due)
            priority = _validate_priority(priority)
            tags = _validate_tags(tags)
            recur = _validate_recur(recur)
            if recur is not None and due is None:
                raise ValueError("a recurring task must have a due date")
            task = {
                "id": None,
                "project_id": project_id,
                "title": title,
                "assignee_id": assignee_id,
                "due": due,
                "priority": priority,
                "tags": tags,
                "status": "open",
                "recur": recur,
            }
            new_id = self._insert_task_row(task)
            self._undo.append({"kind": "add", "id": new_id})
            return new_id

    def get_task(self, task_id):
        with self._lock:
            return self._get_task(task_id)

    def update_task(self, task_id, **fields):
        with self._lock:
            old = self._get_task(task_id)  # KeyError if unknown
            allowed = {"title", "assignee_id", "due", "priority", "tags"}
            for key in fields:
                if key not in allowed:
                    raise ValueError(f"cannot update field: {key}")
            new = dict(old)
            new["tags"] = list(old["tags"])
            if "title" in fields:
                new["title"] = _validate_title(fields["title"])
            if "assignee_id" in fields:
                assignee_id = fields["assignee_id"]
                if assignee_id is not None and not self._user_exists(assignee_id):
                    raise ValueError(f"unknown assignee: {assignee_id}")
                new["assignee_id"] = assignee_id
            if "due" in fields:
                new["due"] = _validate_due(fields["due"])
            if "priority" in fields:
                new["priority"] = _validate_priority(fields["priority"])
            if "tags" in fields:
                new["tags"] = _validate_tags(fields["tags"])
            if new["recur"] is not None and new["due"] is None:
                raise ValueError("a recurring task must have a due date")
            if not fields:
                return None
            self._conn.execute(
                "UPDATE tasks SET title=?, assignee_id=?, due=?, priority=?,"
                " tags=? WHERE id=?",
                (
                    new["title"],
                    new["assignee_id"],
                    new["due"].isoformat() if new["due"] else None,
                    new["priority"],
                    json.dumps(new["tags"]),
                    task_id,
                ),
            )
            self._conn.commit()
            self._undo.append({"kind": "update", "id": task_id, "old": old})
            return None

    def complete_task(self, task_id):
        with self._lock:
            task = self._get_task(task_id)  # KeyError if unknown
            if task["status"] == "done":
                raise ValueError("task is already done")
            self._conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            self._conn.commit()
            next_id = None
            if task["recur"] is not None:
                nxt = dict(task)
                nxt["id"] = None
                nxt["due"] = _next_due(task["due"], task["recur"])
                nxt["status"] = "open"
                next_id = self._insert_task_row(nxt)
            self._undo.append(
                {"kind": "complete", "id": task_id, "next_id": next_id})
            return next_id

    def delete_task(self, task_id):
        with self._lock:
            task = self._get_task(task_id)  # KeyError if unknown
            self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._conn.commit()
            self._undo.append({"kind": "delete", "task": task})

    # -- undo ------------------------------------------------------------
    def undo(self):
        with self._lock:
            if not self._undo:
                return False
            entry = self._undo.pop()
            kind = entry["kind"]
            if kind == "add":
                self._conn.execute(
                    "DELETE FROM tasks WHERE id=?", (entry["id"],))
            elif kind == "delete":
                self._insert_task_row(entry["task"])
            elif kind == "update":
                old = entry["old"]
                self._conn.execute(
                    "UPDATE tasks SET title=?, assignee_id=?, due=?,"
                    " priority=?, tags=? WHERE id=?",
                    (
                        old["title"],
                        old["assignee_id"],
                        old["due"].isoformat() if old["due"] else None,
                        old["priority"],
                        json.dumps(old["tags"]),
                        entry["id"],
                    ),
                )
            elif kind == "complete":
                self._conn.execute(
                    "UPDATE tasks SET status='open' WHERE id=?",
                    (entry["id"],))
                if entry["next_id"] is not None:
                    self._conn.execute(
                        "DELETE FROM tasks WHERE id=?", (entry["next_id"],))
            elif kind == "import":
                for tid in entry["ids"]:
                    self._conn.execute(
                        "DELETE FROM tasks WHERE id=?", (tid,))
            else:  # pragma: no cover - internal invariant
                return False
            self._conn.commit()
            return True

    # -- search ------------------------------------------------------------
    def search(self, query, today=None):
        with self._lock:
            if not isinstance(query, str):
                raise ValueError("query must be a string")
            if today is None:
                today = date.today()
            today = _validate_due(today)
            predicates = _parse_search(query, self, today)
            rows = self._conn.execute("SELECT * FROM tasks").fetchall()
            tasks = [self._row_to_task(r) for r in rows]
            matched = [t for t in tasks if all(p(t) for p in predicates)]

            def sort_key(t):
                return (t["due"] is None, t["due"] or date.min,
                        t["priority"], t["id"])

            matched.sort(key=sort_key)
            return [t["id"] for t in matched]

    # -- CSV ---------------------------------------------------------------
    def export_csv(self, project_id=None):
        with self._lock:
            if project_id is not None:
                if not self._project_exists(project_id):
                    raise KeyError(project_id)
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE project_id=? ORDER BY id",
                    (project_id,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY id").fetchall()
            buf = io.StringIO(newline="")
            writer = csv.writer(buf, lineterminator="\r\n")
            writer.writerow(_CSV_HEADER)
            for row in rows:
                task = self._row_to_task(row)
                proj = self._conn.execute(
                    "SELECT name FROM projects WHERE id=?",
                    (task["project_id"],)).fetchone()
                project_name = proj["name"] if proj else ""
                assignee_name = ""
                if task["assignee_id"] is not None:
                    arow = self._conn.execute(
                        "SELECT name FROM users WHERE id=?",
                        (task["assignee_id"],)).fetchone()
                    assignee_name = arow["name"] if arow else ""
                writer.writerow([
                    task["id"],
                    project_name,
                    task["title"],
                    assignee_name,
                    task["due"].isoformat() if task["due"] else "",
                    task["priority"],
                    ";".join(task["tags"]),
                    task["status"],
                ])
            return buf.getvalue()

    def import_csv(self, text):
        with self._lock:
            if not isinstance(text, str):
                raise ValueError("csv text must be a string")
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames != _CSV_HEADER:
                raise ValueError("csv header does not match expected columns")
            parsed = []
            for row in reader:
                if row is None:
                    continue
                if None in row:
                    raise ValueError("csv row has extra fields")
                if any(v is None for v in row.values()):
                    raise ValueError("csv row is missing fields")
                project_name = (row["project"] or "").strip()
                if not project_name:
                    raise ValueError("csv row has no project")
                project_id = self._find_project_by_name(project_name)
                if project_id is None:
                    raise ValueError(
                        f"unknown project in csv: {project_name}")
                title = _validate_title(row["title"])
                assignee_name = (row["assignee"] or "").strip()
                assignee_id = None
                if assignee_name:
                    assignee_id = self._find_user_by_name(assignee_name)
                    if assignee_id is None:
                        raise ValueError(
                            f"unknown assignee in csv: {assignee_name}")
                due_raw = (row["due"] or "").strip()
                due = _parse_iso_date(due_raw) if due_raw else None
                prio_raw = (row["priority"] or "").strip()
                if prio_raw == "":
                    priority = 2
                else:
                    if not re.fullmatch(r"-?\d+", prio_raw):
                        raise ValueError(
                            f"invalid priority in csv: {prio_raw!r}")
                    priority = _validate_priority(int(prio_raw))
                tags_raw = row["tags"] or ""
                tags = _validate_tags(
                    tags_raw.split(";") if tags_raw else [])
                status_raw = (row["status"] or "").strip().lower()
                if status_raw == "":
                    status = "open"
                elif status_raw in ("open", "done"):
                    status = status_raw
                else:
                    raise ValueError(
                        f"invalid status in csv: {row['status']!r}")
                parsed.append({
                    "id": None,
                    "project_id": project_id,
                    "title": title,
                    "assignee_id": assignee_id,
                    "due": due,
                    "priority": priority,
                    "tags": tags,
                    "status": status,
                    "recur": None,
                })
            ids = []
            for task in parsed:
                ids.append(self._insert_task_row(task))
            if ids:
                self._undo.append({"kind": "import", "ids": ids})
            return len(ids)

    # -- stats ---------------------------------------------------------------
    def stats(self, project_id, today=None):
        with self._lock:
            if not self._project_exists(project_id):
                raise KeyError(project_id)
            if today is None:
                today = date.today()
            today = _validate_due(today)
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE project_id=?", (project_id,)).fetchall()
            tasks = [self._row_to_task(r) for r in rows]
            open_tasks = [t for t in tasks if t["status"] == "open"]
            by_priority = {1: 0, 2: 0, 3: 0, 4: 0}
            for t in open_tasks:
                by_priority[t["priority"]] += 1
            overdue = sum(
                1 for t in open_tasks
                if t["due"] is not None and t["due"] < today)
            return {
                "open": len(open_tasks),
                "done": len(tasks) - len(open_tasks),
                "overdue": overdue,
                "by_priority": by_priority,
            }


def open_db(path):
    """Open (creating if needed) the SQLite tracker database at path."""
    return Tracker(path)


# ---------------------------------------------------------------------------
# Search parsing
# ---------------------------------------------------------------------------

def _tokenize(query):
    """Split a search query into (text, is_quoted, negated) tokens."""
    tokens = []
    i, n = 0, len(query)
    while i < n:
        while i < n and query[i].isspace():
            i += 1
        if i >= n:
            break
        negated = False
        if query[i] == "-":
            negated = True
            i += 1
            if i >= n or query[i].isspace():
                raise ValueError("malformed search term: '-'")
        if i < n and query[i] == '"':
            j = query.find('"', i + 1)
            if j == -1:
                raise ValueError("unclosed quote in search query")
            tokens.append((query[i + 1:j], True, negated))
            i = j + 1
            if i < n and not query[i].isspace():
                raise ValueError("malformed search term after quote")
        else:
            j = i
            while j < n and not query[j].isspace():
                if query[j] == '"':
                    raise ValueError("unclosed quote in search query")
                j += 1
            tokens.append((query[i:j], False, negated))
            i = j
    return tokens


_TERM_RE = re.compile(r"^([A-Za-z_]+)(<=|>=|:|<|>)(.*)$", re.DOTALL)


def _parse_search(query, tracker, today):
    """Return a list of predicates, one per AND term."""
    predicates = []
    for text, is_quoted, negated in _tokenize(query):
        pred = _term_predicate(text, is_quoted, tracker, today)
        if negated:
            pred = (lambda p: (lambda t: not p(t)))(pred)
        predicates.append(pred)
    return predicates


def _term_predicate(text, is_quoted, tracker, today):
    if is_quoted:
        needle = text.lower()
        return lambda t: needle in t["title"].lower()
    low = text.lower()
    if low == "overdue":
        return lambda t: (t["status"] == "open" and t["due"] is not None
                          and t["due"] < today)
    m = _TERM_RE.match(text)
    if m:
        key, op, value = m.group(1).lower(), m.group(2), m.group(3)
        if key == "tag":
            if op != ":" or not value:
                raise ValueError(f"invalid search term: {text!r}")
            needle = value.lower()
            return lambda t: needle in t["tags"]
        if key == "status":
            if op != ":":
                raise ValueError(f"invalid search term: {text!r}")
            val = value.lower()
            if val not in ("open", "done"):
                raise ValueError(f"invalid status in search: {value!r}")
            return lambda t, v=val: t["status"] == v
        if key == "assignee":
            if op != ":" or not value:
                raise ValueError(f"invalid search term: {text!r}")
            if value.lower() == "none":
                return lambda t: t["assignee_id"] is None
            uid = tracker._find_user_by_name(value)
            if uid is None:
                return lambda t: False
            return lambda t, u=uid: t["assignee_id"] == u
        if key == "project":
            if op != ":" or not value:
                raise ValueError(f"invalid search term: {text!r}")
            pid = tracker._find_project_by_name(value)
            if pid is None:
                return lambda t: False
            return lambda t, p=pid: t["project_id"] == p
        if key == "due":
            if op == ":" and value.lower() == "none":
                return lambda t: t["due"] is None
            due_date = _parse_iso_date(value)
            if op == ":":
                return lambda t, d=due_date: t["due"] == d
            if op == "<":
                return lambda t, d=due_date: t["due"] is not None and t["due"] < d
            if op == "<=":
                return lambda t, d=due_date: t["due"] is not None and t["due"] <= d
            if op == ">":
                return lambda t, d=due_date: t["due"] is not None and t["due"] > d
            if op == ">=":
                return lambda t, d=due_date: t["due"] is not None and t["due"] >= d
        if key == "p":
            if not re.fullmatch(r"\d+", value):
                raise ValueError(f"invalid priority in search: {value!r}")
            num = int(value)
            if num < 1 or num > 4:
                raise ValueError("priority in search must be 1..4")
            if op == ":":
                return lambda t, n=num: t["priority"] == n
            if op == "<":
                return lambda t, n=num: t["priority"] < n
            if op == "<=":
                return lambda t, n=num: t["priority"] <= n
            if op == ">":
                return lambda t, n=num: t["priority"] > n
            if op == ">=":
                return lambda t, n=num: t["priority"] >= n
        raise ValueError(f"unknown search term: {text!r}")
    if ":" in text or "<" in text or ">" in text:
        raise ValueError(f"unknown search term: {text!r}")
    needle = text.lower()
    return lambda t: needle in t["title"].lower()
