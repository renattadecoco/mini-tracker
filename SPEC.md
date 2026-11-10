# mini-tracker specification

Package `tracker`. Standard library only. All errors described as `ValueError` / `KeyError` must be exactly those types
(subclasses are fine).

## 1. Storage — `tracker.db.open_db(path) -> Tracker`

SQLite file at `path` (`":memory:"` allowed). Data persists: closing and reopening the same file returns the same
users, projects and tasks with the same ids. `Tracker.close()` closes the database.
The Tracker must also work when its methods are called from another thread (see the HTTP server below).

## 2. Users and projects

- `add_user(name) -> int`. Name is stripped; empty → ValueError. Names are unique case-insensitively → ValueError.
- `add_project(name, owner_id) -> int`. Name stripped, non-empty, unique case-insensitively → ValueError.
  Unknown owner → ValueError.

## 3. Tasks

`add_task(project_id, title, *, assignee_id=None, due=None, priority=2, tags=(), recur=None) -> int`

- `title`: stripped, non-empty. `due`: `datetime.date` or None. `priority`: int 1..4 (1 = highest).
- `tags`: iterable of strings; stored lowercased, stripped, de-duplicated, empty ones dropped, sorted.
- `recur`: None, `"daily"`, `"weekly"`, `"monthly"`. A recurring task must have a due date.
- Unknown project or assignee, or any invalid value → ValueError.

`get_task(id) -> dict` with exactly these keys:
`id, project_id, title, assignee_id, due, priority, tags, status, recur` — `due` is a `date` or None, `tags` a sorted list,
`status` is `"open"` or `"done"`. Unknown id → KeyError.

`update_task(id, **fields)` — allowed fields: `title, assignee_id, due, priority, tags` (same validation as add).
Any other field → ValueError. Unknown id → KeyError.

`complete_task(id) -> int | None` — marks the task done. If it recurs, creates the next occurrence (same project, title,
assignee, priority, tags, recur; status open) and returns its id; otherwise returns None.
Next due date: daily +1 day, weekly +7 days, monthly = same day of month in the next month, clamped to the last day of
that month (Jan 31 → Feb 28 in 2027). The next date is always computed from the completed task's due date.
Completing a task that is already done → ValueError. Unknown id → KeyError.

`delete_task(id)` — unknown id → KeyError.

## 4. Undo — `undo() -> bool`

Undoes the most recent not-yet-undone task mutation (`add_task`, `update_task`, `complete_task`, `delete_task`,
`import_csv`) and returns True; returns False when there is nothing to undo.
- undo add → the task no longer exists. undo delete → the task exists again with the same id and fields.
- undo update → previous field values. undo complete → task open again and any spawned occurrence removed.
- undo import → all tasks from that import removed.
The undo history is in memory only: after reopening the database `undo()` returns False.
Users and projects are not undoable.

## 5. Search — `search(query, today=None) -> list[int]`

Returns matching task ids sorted by due date (tasks without due last), then priority ascending, then id ascending.
`today` defaults to `date.today()`. Terms are separated by spaces and combined with AND. Empty query → all tasks
(both open and done). Double quotes group a phrase: `"buy milk"`.

| term | meaning |
|---|---|
| `word` or `"a phrase"` | title contains it, case-insensitive |
| `tag:NAME` | has tag (case-insensitive) |
| `status:open` / `status:done` | status; any other value → ValueError |
| `p:N`, `p<N`, `p<=N`, `p>N`, `p>=N` | priority comparison; N must be 1..4 else ValueError |
| `due:YYYY-MM-DD`, `due<…`, `due<=…`, `due>…`, `due>=…` | due comparison; tasks without due never match |
| `due:none` | no due date |
| `assignee:NAME` / `assignee:none` | assignee by user name (case-insensitive) / unassigned |
| `project:NAME` | project by name (case-insensitive) |
| `overdue` | status open and due < today |
| `-TERM` | negation of any term above, e.g. `-tag:home`, `-"buy milk"`, `-overdue` |

Any other `key:value` / `key<value` form, a malformed date, or an unclosed quote → ValueError.
An unknown user or project name in `assignee:` / `project:` simply matches nothing.

## 6. CSV

`export_csv(project_id=None) -> str` — header `id,project,title,assignee,due,priority,tags,status`, one row per task
ordered by id (only that project if given). `project` and `assignee` are names (assignee empty if none), `due` ISO or
empty, `tags` joined with `;`. Standard `csv` module quoting; line endings `\r\n` (csv module default).

`import_csv(text) -> int` — same columns; `id` is ignored (new ids are assigned). Project must exist by name,
assignee by name or empty, `due` ISO or empty, `priority` empty → 2, `tags` `;`-separated, `status` `open`/`done`
(empty → open). All-or-nothing: if any row is invalid, raise ValueError and import nothing. Returns rows imported.
Export followed by import into another database reproduces the same tasks (except ids).

## 7. Stats — `stats(project_id, today=None) -> dict`

`{"open": n, "done": n, "overdue": n, "by_priority": {1: n, 2: n, 3: n, 4: n}}` — `by_priority` counts open tasks only;
all four keys always present. Unknown project → KeyError.

## 8. HTTP — `tracker.server.make_server(tracker, host, port)`

Returns an `http.server` server object (supports `serve_forever()`, `shutdown()`, `server_close()`, `server_address`;
port 0 = any free port). It is run in a background thread. JSON in and out; dates are ISO strings; tags lists.

| method & path | behaviour | success |
|---|---|---|
| `GET /tasks?q=QUERY` | search (q optional) → `{"tasks": [task, ...]}` in search order | 200 |
| `POST /tasks` | body `{project_id, title, assignee_id?, due?, priority?, tags?, recur?}` → task | 201 |
| `GET /tasks/ID` | task | 200 |
| `PATCH /tasks/ID` | body = fields for update_task → updated task | 200 |
| `POST /tasks/ID/complete` | `{"next_id": int or null}` | 200 |
| `DELETE /tasks/ID` | empty body | 204 |

Errors: ValueError or invalid JSON → 400, unknown task or path → 404, both with body `{"error": "message"}`.
Responses use `Content-Type: application/json`.
