"""HTTP server for the tracker (SPEC.md section 8).

JSON in and out; dates are ISO strings; tags are lists.
Run the returned server in a background thread with serve_forever().
Standard library only.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__all__ = ["make_server"]


def _task_json(task):
    out = dict(task)
    out["tags"] = list(task["tags"])
    out["due"] = task["due"].isoformat() if task["due"] else None
    return out


def _parse_due(value):
    """Convert a JSON due value (ISO string or None) to a date or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("due must be an ISO date string or null")
    from datetime import date
    import re
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"invalid date: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid date: {value!r}") from None


def make_server(tracker, host, port):
    """Return an http.server server object bound to (host, port)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -- plumbing --------------------------------------------------
        def log_message(self, *args):  # keep test output clean
            pass

        def _send_json(self, status, obj):
            body = json.dumps(obj).encode("utf-8") if obj is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _send_error(self, status, message):
            self._send_json(status, {"error": str(message)})

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _read_json(self):
            raw = self._read_body()
            if not raw:
                raise ValueError("empty JSON body")
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid JSON") from None

        def _parse_id(self, segment):
            try:
                return int(segment)
            except (TypeError, ValueError):
                return None

        def _route(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if len(path) > 1:
                path = path.rstrip("/")
            parts = [p for p in path.split("/") if p]
            return parts, parse_qs(parsed.query)

        # -- verbs ------------------------------------------------------
        def do_GET(self):
            try:
                parts, query = self._route()
                if parts == ["tasks"]:
                    q = query.get("q", [""])[0]
                    ids = self.server.tracker.search(q)
                    tasks = [self.server.tracker.get_task(i) for i in ids]
                    self._send_json(200, {"tasks": [_task_json(t) for t in tasks]})
                    return
                if len(parts) == 2 and parts[0] == "tasks":
                    task_id = self._parse_id(parts[1])
                    if task_id is None:
                        self._send_error(404, "unknown path")
                        return
                    try:
                        task = self.server.tracker.get_task(task_id)
                    except KeyError:
                        self._send_error(404, f"unknown task: {task_id}")
                        return
                    self._send_json(200, _task_json(task))
                    return
                self._send_error(404, "unknown path")
            except ValueError as e:
                self._send_error(400, e)
            except Exception as e:  # pragma: no cover - defensive
                self._send_error(500, e)

        def do_POST(self):
            try:
                parts, _query = self._route()
                if parts == ["tasks"]:
                    body = self._read_json()
                    if not isinstance(body, dict):
                        raise ValueError("request body must be a JSON object")
                    allowed = {"project_id", "title", "assignee_id", "due",
                               "priority", "tags", "recur"}
                    for key in body:
                        if key not in allowed:
                            raise ValueError(f"unknown field: {key}")
                    if "project_id" not in body or "title" not in body:
                        raise ValueError("project_id and title are required")
                    kwargs = {}
                    if "assignee_id" in body:
                        kwargs["assignee_id"] = body["assignee_id"]
                    if "due" in body:
                        kwargs["due"] = _parse_due(body["due"])
                    if "priority" in body:
                        kwargs["priority"] = body["priority"]
                    if "tags" in body:
                        kwargs["tags"] = body["tags"]
                    if "recur" in body:
                        kwargs["recur"] = body["recur"]
                    new_id = self.server.tracker.add_task(
                        body["project_id"], body["title"], **kwargs)
                    task = self.server.tracker.get_task(new_id)
                    self._send_json(201, _task_json(task))
                    return
                if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "complete":
                    task_id = self._parse_id(parts[1])
                    if task_id is None:
                        self._send_error(404, "unknown path")
                        return
                    # Body (if any) is ignored for complete.
                    self._read_body()
                    try:
                        next_id = self.server.tracker.complete_task(task_id)
                    except KeyError:
                        self._send_error(404, f"unknown task: {task_id}")
                        return
                    self._send_json(200, {"next_id": next_id})
                    return
                self._send_error(404, "unknown path")
            except ValueError as e:
                self._send_error(400, e)
            except KeyError as e:
                self._send_error(404, f"unknown task: {e.args[0] if e.args else ''}")
            except Exception as e:  # pragma: no cover - defensive
                self._send_error(500, e)

        def do_PATCH(self):
            try:
                parts, _query = self._route()
                if len(parts) == 2 and parts[0] == "tasks":
                    task_id = self._parse_id(parts[1])
                    if task_id is None:
                        self._send_error(404, "unknown path")
                        return
                    body = self._read_json()
                    if not isinstance(body, dict):
                        raise ValueError("request body must be a JSON object")
                    fields = dict(body)
                    if "due" in fields:
                        fields["due"] = _parse_due(fields["due"])
                    try:
                        self.server.tracker.update_task(task_id, **fields)
                    except KeyError:
                        self._send_error(404, f"unknown task: {task_id}")
                        return
                    task = self.server.tracker.get_task(task_id)
                    self._send_json(200, _task_json(task))
                    return
                self._send_error(404, "unknown path")
            except ValueError as e:
                self._send_error(400, e)
            except KeyError as e:
                self._send_error(404, f"unknown task: {e.args[0] if e.args else ''}")
            except Exception as e:  # pragma: no cover - defensive
                self._send_error(500, e)

        def do_DELETE(self):
            try:
                parts, _query = self._route()
                if len(parts) == 2 and parts[0] == "tasks":
                    task_id = self._parse_id(parts[1])
                    if task_id is None:
                        self._send_error(404, "unknown path")
                        return
                    try:
                        self.server.tracker.delete_task(task_id)
                    except KeyError:
                        self._send_error(404, f"unknown task: {task_id}")
                        return
                    self.send_response(204)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send_error(404, "unknown path")
            except ValueError as e:
                self._send_error(400, e)
            except Exception as e:  # pragma: no cover - defensive
                self._send_error(500, e)

        def do_PUT(self):
            self._send_error(404, "unknown path")

        def do_OPTIONS(self):
            self._send_error(404, "unknown path")

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server((host, port), Handler)
    server.tracker = tracker
    return server
