"""Extra tests written by the implementer, covering every SPEC.md section.

These are in addition to tests/test_samples.py (which is untouched).
The HTTP tests run the server in a background thread, as the spec requires.
"""
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date as D

from tracker.db import open_db
from tracker.server import make_server


def _fresh(test):
    t = open_db(":memory:")
    test.addCleanup(t.close)
    u = t.add_user("Ana")
    v = t.add_user("Budi")
    p = t.add_project("Home", u)
    q = t.add_project("Work", v)
    return t, u, v, p, q


class TestUsersProjects(unittest.TestCase):
    def test_user_strip_empty_duplicate_ci(self):
        t, *_ = _fresh(self)
        self.assertEqual(t.add_user("  Citra "), 3)
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                t.add_user(bad)
        for dup in ("ana", "ANA", " Ana "):
            with self.assertRaises(ValueError):
                t.add_user(dup)
        with self.assertRaises(ValueError):
            t.add_user(123)

    def test_project_validation(self):
        t, u, v, *_ = _fresh(self)
        with self.assertRaises(ValueError):
            t.add_project("", u)
        with self.assertRaises(ValueError):
            t.add_project("  ", u)
        with self.assertRaises(ValueError):
            t.add_project("home", u)  # duplicate, case-insensitive
        with self.assertRaises(ValueError):
            t.add_project("New", 999)  # unknown owner
        self.assertEqual(t.add_project("  Garden ", u), 3)


class TestTasks(unittest.TestCase):
    def test_add_validation(self):
        t, u, v, p, q = _fresh(self)
        with self.assertRaises(ValueError):
            t.add_task(p, "  ")
        with self.assertRaises(ValueError):
            t.add_task(999, "x")
        with self.assertRaises(ValueError):
            t.add_task(p, "x", assignee_id=999)
        with self.assertRaises(ValueError):
            t.add_task(p, "x", due="2026-12-01")
        for bad in (0, 5, -1, 2.5, "2", True):
            with self.assertRaises(ValueError, msg=str(bad)):
                t.add_task(p, "x", priority=bad)
        with self.assertRaises(ValueError):
            t.add_task(p, "x", recur="yearly")
        with self.assertRaises(ValueError):
            t.add_task(p, "x", recur="daily")  # no due date
        with self.assertRaises(ValueError):
            t.add_task(p, "x", tags="shop")  # bare string, not a list
        with self.assertRaises(ValueError):
            t.add_task(p, "x", tags=[1])

    def test_tags_normalised(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "x", tags=["Beta", "alpha", " beta ", "", "ALPHA"])
        self.assertEqual(t.get_task(i)["tags"], ["alpha", "beta"])

    def test_get_task_keys_and_unknown(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "Buy", assignee_id=u, due=D(2026, 12, 1),
                       priority=1, tags=["a"], recur=None)
        task = t.get_task(i)
        self.assertEqual(sorted(task.keys()),
                         sorted(["id", "project_id", "title", "assignee_id",
                                 "due", "priority", "tags", "status", "recur"]))
        self.assertEqual(task["status"], "open")
        with self.assertRaises(KeyError):
            t.get_task(999)

    def test_update(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "old", priority=2)
        t.update_task(i, title="  new  ", priority=1, tags=["X", "x"],
                      assignee_id=v, due=D(2026, 12, 2))
        self.assertEqual(t.get_task(i),
                         {"id": i, "project_id": p, "title": "new",
                          "assignee_id": v, "due": D(2026, 12, 2),
                          "priority": 1, "tags": ["x"], "status": "open",
                          "recur": None})
        with self.assertRaises(ValueError):
            t.update_task(i, status="done")
        with self.assertRaises(ValueError):
            t.update_task(i, recur="daily")
        with self.assertRaises(ValueError):
            t.update_task(i, priority=9)
        with self.assertRaises(ValueError):
            t.update_task(i, title=" ")
        with self.assertRaises(KeyError):
            t.update_task(999, title="x")

    def test_recur_next_dates(self):
        t, u, v, p, q = _fresh(self)
        cases = [
            (D(2026, 12, 1), "daily", D(2026, 12, 2)),
            (D(2026, 12, 1), "weekly", D(2026, 12, 8)),
            (D(2027, 1, 31), "monthly", D(2027, 2, 28)),
            (D(2026, 3, 31), "monthly", D(2026, 4, 30)),
            (D(2026, 12, 31), "monthly", D(2027, 1, 31)),
            (D(2024, 1, 31), "monthly", D(2024, 2, 29)),  # leap year
        ]
        for due, recur, expected in cases:
            i = t.add_task(p, "r", due=due, recur=recur)
            n = t.complete_task(i)
            self.assertEqual(t.get_task(n)["due"], expected)
            self.assertEqual(t.get_task(n)["status"], "open")
            self.assertEqual(t.get_task(i)["status"], "done")

    def test_complete_non_recur_and_errors(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "x")
        self.assertIsNone(t.complete_task(i))
        with self.assertRaises(ValueError):
            t.complete_task(i)  # already done
        with self.assertRaises(KeyError):
            t.complete_task(999)
        with self.assertRaises(KeyError):
            t.delete_task(999)

    def test_delete(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "x")
        t.delete_task(i)
        with self.assertRaises(KeyError):
            t.get_task(i)


class TestPersistence(unittest.TestCase):
    def test_reopen_keeps_everything(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "db.sqlite")
            t = open_db(path)
            u = t.add_user("Ana")
            p = t.add_project("Home", u)
            i = t.add_task(p, "Buy milk", due=D(2026, 12, 1), tags=["shop"])
            j = t.add_task(p, "Done one", due=D(2026, 12, 2))
            t.complete_task(j)
            t.close()
            t2 = open_db(path)
            self.addCleanup(t2.close)
            self.assertEqual(t2.get_task(i)["title"], "Buy milk")
            self.assertEqual(t2.get_task(j)["status"], "done")
            self.assertEqual(t2.search(""), [i, j])
            # ids keep increasing after reopen, never reused
            k = t2.add_task(p, "third")
            self.assertEqual(k, 3)

    def test_undo_not_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "db.sqlite")
            t = open_db(path)
            u = t.add_user("Ana")
            p = t.add_project("Home", u)
            t.add_task(p, "x")
            t.close()
            t2 = open_db(path)
            self.addCleanup(t2.close)
            self.assertFalse(t2.undo())


class TestUndo(unittest.TestCase):
    def test_undo_update_and_delete(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "old", priority=2)
        t.update_task(i, title="new", priority=1)
        self.assertTrue(t.undo())
        self.assertEqual(t.get_task(i)["title"], "old")
        self.assertEqual(t.get_task(i)["priority"], 2)
        t.delete_task(i)
        self.assertTrue(t.undo())
        restored = t.get_task(i)
        self.assertEqual((restored["title"], restored["priority"]),
                         ("old", 2))

    def test_undo_complete_removes_spawn(self):
        t, u, v, p, q = _fresh(self)
        i = t.add_task(p, "r", due=D(2026, 12, 1), recur="daily")
        n = t.complete_task(i)
        self.assertTrue(t.undo())
        self.assertEqual(t.get_task(i)["status"], "open")
        with self.assertRaises(KeyError):
            t.get_task(n)
        # undo is a stack: the add itself is still on it, so one more
        # undo removes the original task, and then there is nothing left.
        self.assertTrue(t.undo())
        with self.assertRaises(KeyError):
            t.get_task(i)
        self.assertFalse(t.undo())

    def test_undo_stack_order_and_empty(self):
        t, u, v, p, q = _fresh(self)
        a = t.add_task(p, "a")
        b = t.add_task(p, "b")
        self.assertTrue(t.undo())
        with self.assertRaises(KeyError):
            t.get_task(b)
        self.assertTrue(t.undo())
        with self.assertRaises(KeyError):
            t.get_task(a)
        self.assertFalse(t.undo())
        self.assertFalse(t.undo())


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.t, self.u, self.v, self.p, self.q = _fresh(self)
        t = self.t
        self.a = t.add_task(self.p, "Buy milk", assignee_id=self.u,
                            due=D(2026, 12, 1), priority=1, tags=["shop"])
        self.b = t.add_task(self.p, "Buy bread", due=D(2026, 12, 3),
                            priority=3, tags=["shop", "home"])
        self.c = t.add_task(self.q, "Call Ana", assignee_id=self.v,
                            due=D(2026, 12, 2), priority=2, tags=["home"])
        self.d = t.add_task(self.q, "No due task", priority=4)
        self.e = t.add_task(self.p, "Done task", due=D(2026, 11, 1),
                            priority=2)
        self.t.complete_task(self.e)

    def test_terms(self):
        t = self.t
        self.assertEqual(t.search("buy"), [self.a, self.b])
        self.assertEqual(t.search('"buy milk"'), [self.a])
        self.assertEqual(t.search("BUY MILK"), [self.a])
        self.assertEqual(t.search("tag:home"), [self.c, self.b])
        self.assertEqual(t.search("tag:SHOP"), [self.a, self.b])
        self.assertEqual(t.search("status:done"), [self.e])
        self.assertEqual(t.search("status:open"), [self.a, self.c, self.b, self.d])
        self.assertEqual(t.search("p:1"), [self.a])
        self.assertEqual(t.search("p<2"), [self.a])
        self.assertEqual(t.search("p>=3"), [self.b, self.d])
        self.assertEqual(t.search("due:2026-12-01"), [self.a])
        self.assertEqual(t.search("due<2026-12-02"), [self.e, self.a])
        self.assertEqual(t.search("due>=2026-12-03"), [self.b])
        self.assertEqual(t.search("due:none"), [self.d])
        self.assertEqual(t.search("assignee:Ana"), [self.a])
        # search order is by due date: e (Nov 1), b (Dec 3), d (no due)
        self.assertEqual(t.search("assignee:none"), [self.e, self.b, self.d])
        self.assertEqual(t.search("project:Work"), [self.c, self.d])
        # due order: e (Nov 1), a (Dec 1), b (Dec 3)
        self.assertEqual(t.search("project:home"), [self.e, self.a, self.b])

    def test_overdue_and_today(self):
        t = self.t
        today = D(2026, 12, 2)
        self.assertEqual(t.search("overdue", today=today), [self.a])
        self.assertEqual(t.search("overdue tag:shop", today=today), [self.a])
        # done task due 2026-11-01 is not overdue (it is done)
        self.assertNotIn(self.e, t.search("overdue", today=today))

    def test_negation_and_and(self):
        t = self.t
        self.assertEqual(t.search("buy -tag:home"), [self.a])
        self.assertEqual(t.search('-"buy milk" buy'), [self.b])
        self.assertEqual(t.search("tag:shop -overdue", today=D(2026, 12, 2)),
                         [self.b])
        self.assertEqual(t.search("buy tag:shop"), [self.a, self.b])
        self.assertEqual(t.search("buy tag:home"), [self.b])

    def test_unknown_names_match_nothing(self):
        t = self.t
        self.assertEqual(t.search("assignee:Nobody"), [])
        self.assertEqual(t.search("project:Nobody"), [])

    def test_invalid_queries(self):
        t = self.t
        for bad in ("status:bogus", "status:", "p:0", "p:5", "p:abc", "p:2.5",
                    "due:2026-13-01", "due:2026-02-30", "due:not-a-date",
                    "due<garbage", "foo:bar", "title:milk", "x<3",
                    '"unclosed', 'buy "unclosed', "tag:", "p:", "due:"):
            with self.assertRaises(ValueError, msg=bad):
                t.search(bad)

    def test_empty_query_returns_all_sorted(self):
        t = self.t
        # due order: e (Nov 1), a (Dec 1), c (Dec 2), b (Dec 3), d (no due)
        self.assertEqual(t.search(""), [self.e, self.a, self.c, self.b, self.d])
        self.assertEqual(t.search("   "), [self.e, self.a, self.c, self.b, self.d])


class TestCsv(unittest.TestCase):
    def test_export_format(self):
        t, u, v, p, q = _fresh(self)
        t.add_task(p, "Buy, milk", assignee_id=u, due=D(2026, 12, 1),
                   priority=1, tags=["shop"])
        t.add_task(p, "Plain", priority=2)
        out = t.export_csv()
        self.assertTrue(out.startswith(
            "id,project,title,assignee,due,priority,tags,status\r\n"))
        self.assertIn('"Buy, milk"', out)  # csv quoting
        self.assertIn("\r\n", out)
        lines = out.split("\r\n")
        self.assertEqual(lines[1].split(",")[0], "1")

    def test_export_project_filter(self):
        t, u, v, p, q = _fresh(self)
        t.add_task(p, "a")
        t.add_task(q, "b")
        self.assertEqual(len(t.export_csv(p).strip().splitlines()), 2)  # header + 1
        with self.assertRaises(KeyError):
            t.export_csv(999)

    def test_round_trip(self):
        t, u, v, p, q = _fresh(self)
        t.add_task(p, "Buy milk", assignee_id=u, due=D(2026, 12, 1),
                   priority=1, tags=["Shop", "home"])
        i2 = t.add_task(p, "Done", priority=3)
        t.complete_task(i2)
        text = t.export_csv()
        t2 = open_db(":memory:")
        self.addCleanup(t2.close)
        u2 = t2.add_user("Ana")
        t2.add_project("Home", u2)
        self.assertEqual(t2.import_csv(text), 2)
        tasks = [t2.get_task(i) for i in t2.search("")]
        self.assertEqual(tasks[0]["title"], "Buy milk")
        self.assertEqual(tasks[0]["tags"], ["home", "shop"])
        self.assertEqual(tasks[0]["assignee_id"], u2)
        self.assertEqual(tasks[1]["status"], "done")

    def test_import_all_or_nothing_and_undo(self):
        t, u, v, p, q = _fresh(self)
        good = ("id,project,title,assignee,due,priority,tags,status\r\n"
                "1,Home,One,Ana,2026-12-01,1,shop,open\r\n")
        bad = ("id,project,title,assignee,due,priority,tags,status\r\n"
               "1,Home,One,Ana,2026-12-01,1,shop,open\r\n"
               "2,Nope,Two,,2026-12-01,,,\r\n")
        with self.assertRaises(ValueError):
            t.import_csv(bad)
        self.assertEqual(t.search(""), [])
        self.assertEqual(t.import_csv(good), 1)
        self.assertTrue(t.undo())
        self.assertEqual(t.search(""), [])

    def test_import_defaults_and_invalid(self):
        t, u, v, p, q = _fresh(self)
        text = ("id,project,title,assignee,due,priority,tags,status\r\n"
                "99,Home,Defaults,,,,,\r\n")
        self.assertEqual(t.import_csv(text), 1)
        task = t.get_task(t.search("")[0])
        self.assertEqual((task["priority"], task["status"], task["due"]),
                         (2, "open", None))
        for bad_text in [
            "id,project,title,assignee,due,priority,tags,status\r\n"
            "1,Home,X,,2026-13-01,,,open\r\n",
            "id,project,title,assignee,due,priority,tags,status\r\n"
            "1,Home,X,,2026-12-01,9,,open\r\n",
            "id,project,title,assignee,due,priority,tags,status\r\n"
            "1,Home,X,Ghost,,,,open\r\n",
            "id,project,title,assignee,due,priority,tags,status\r\n"
            "1,Home,X,,,,weird\r\n",
            "wrong,header\r\n1,Home,X\r\n",
        ]:
            with self.assertRaises(ValueError, msg=bad_text[:40]):
                t.import_csv(bad_text)


class TestStats(unittest.TestCase):
    def test_stats(self):
        t, u, v, p, q = _fresh(self)
        a = t.add_task(p, "a", due=D(2026, 12, 1), priority=1)
        t.add_task(p, "b", due=D(2026, 12, 1), priority=3)
        t.add_task(p, "c", priority=2)
        d = t.add_task(p, "d", due=D(2026, 11, 1), priority=4)
        t.complete_task(d)
        s = t.stats(p, today=D(2026, 12, 2))
        # open overdue: a and b (both due Dec 1 < Dec 2); d is done, c has no due
        self.assertEqual(s, {"open": 3, "done": 1, "overdue": 2,
                             "by_priority": {1: 1, 2: 1, 3: 1, 4: 0}})
        self.assertEqual(t.stats(q), {"open": 0, "done": 0, "overdue": 0,
                                      "by_priority": {1: 0, 2: 0, 3: 0, 4: 0}})
        with self.assertRaises(KeyError):
            t.stats(999)


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.t, self.u, self.v, self.p, self.q = _fresh(self)
        self.server = make_server(self.t, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]

    def _req(self, method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if raw is not None:
            conn.request(method, path, body=raw, headers=headers)
        elif body is not None:
            conn.request(method, path, body=json.dumps(body), headers=headers)
        else:
            conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), data

    def _json(self, method, path, body=None, raw=None):
        status, ctype, data = self._req(method, path, body=body, raw=raw)
        self.assertEqual(ctype, "application/json")
        return status, (json.loads(data) if data else None)

    def test_full_flow(self):
        status, body = self._json("POST", "/tasks", {
            "project_id": self.p, "title": "  Buy milk ",
            "assignee_id": self.u, "due": "2026-12-01",
            "priority": 1, "tags": ["Shop"]})
        self.assertEqual(status, 201)
        tid = body["id"]
        self.assertEqual(body["title"], "Buy milk")
        self.assertEqual(body["due"], "2026-12-01")
        self.assertEqual(body["tags"], ["shop"])
        self.assertEqual(body["status"], "open")

        status, body = self._json("GET", "/tasks?q=buy")
        self.assertEqual(status, 200)
        self.assertEqual([x["id"] for x in body["tasks"]], [tid])

        status, body = self._json("GET", f"/tasks/{tid}")
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], tid)

        status, body = self._json("PATCH", f"/tasks/{tid}",
                                  {"title": "Buy bread", "priority": 2})
        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "Buy bread")
        self.assertEqual(body["priority"], 2)

        status, body = self._json("POST", f"/tasks/{tid}/complete")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"next_id": None})

        status, ctype, data = self._req("DELETE", f"/tasks/{tid}")
        self.assertEqual(status, 204)
        self.assertEqual(data, b"")
        status, body = self._json("GET", f"/tasks/{tid}")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_recur_complete_via_http(self):
        tid = self.t.add_task(self.p, "Bins", due=D(2026, 12, 1),
                              recur="weekly")
        status, body = self._json("POST", f"/tasks/{tid}/complete")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["next_id"], int)
        self.assertEqual(self.t.get_task(body["next_id"])["due"], D(2026, 12, 8))

    def test_http_errors(self):
        status, body = self._json("POST", "/tasks", {
            "project_id": self.p, "title": "x", "priority": 9})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = self._json("POST", "/tasks", raw="{not json")
        self.assertEqual(status, 400)
        status, body = self._json("GET", "/tasks/999")
        self.assertEqual(status, 404)
        status, body = self._json("GET", "/nope")
        self.assertEqual(status, 404)
        status, body = self._json("GET", "/tasks?q=status:bogus")
        self.assertEqual(status, 400)
        status, body = self._json("PATCH", "/tasks/999", {"title": "x"})
        self.assertEqual(status, 404)
        # invalid field on an existing task -> 400
        tid = self.t.add_task(self.p, "x")
        status, body = self._json("PATCH", f"/tasks/{tid}", {"status": "done"})
        self.assertEqual(status, 400)
        status, body = self._json("POST", "/tasks", {"title": "no project"})
        self.assertEqual(status, 400)

    def test_get_tasks_no_query(self):
        self.t.add_task(self.p, "a")
        status, body = self._json("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["tasks"]), 1)

    def test_non_numeric_id_is_404(self):
        status, body = self._json("GET", "/tasks/abc")
        self.assertEqual(status, 404)
        status, body = self._json("POST", "/tasks/abc/complete")
        self.assertEqual(status, 404)


class TestSearchMore(unittest.TestCase):
    def test_more_comparisons(self):
        t, u, v, p, q = _fresh(self)
        a = t.add_task(p, "a", due=D(2026, 12, 1), priority=2)
        b = t.add_task(p, "b", due=D(2026, 12, 5), priority=4)
        self.assertEqual(t.search("p<=2"), [a])
        self.assertEqual(t.search("p>2"), [b])
        self.assertEqual(t.search("due<=2026-12-01"), [a])
        self.assertEqual(t.search("due>2026-12-01"), [b])
        self.assertEqual(t.search("-due:none"), [a, b])
        self.assertEqual(t.search("due:none"), [])

    def test_import_blank_row_is_invalid(self):
        t, u, v, p, q = _fresh(self)
        text = ("id,project,title,assignee,due,priority,tags,status\r\n"
                ",,,,,,,\r\n")
        with self.assertRaises(ValueError):
            t.import_csv(text)
        self.assertEqual(t.search(""), [])


if __name__ == "__main__":
    unittest.main()
