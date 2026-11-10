import unittest
from datetime import date as D

from tracker.db import open_db


class TestSamples(unittest.TestCase):
    def setUp(self):
        self.t = open_db(":memory:")
        self.addCleanup(self.t.close)
        self.u = self.t.add_user("Ana")
        self.p = self.t.add_project("Home", self.u)

    def test_add_get(self):
        i = self.t.add_task(self.p, "  Buy milk ", tags=["Shop", "shop", " "], due=D(2026, 12, 1))
        task = self.t.get_task(i)
        self.assertEqual(task["title"], "Buy milk")
        self.assertEqual(task["tags"], ["shop"])
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["due"], D(2026, 12, 1))

    def test_bad_priority(self):
        with self.assertRaises(ValueError):
            self.t.add_task(self.p, "x", priority=5)

    def test_recur_weekly(self):
        i = self.t.add_task(self.p, "Bins", due=D(2026, 12, 1), recur="weekly")
        n = self.t.complete_task(i)
        self.assertEqual(self.t.get_task(n)["due"], D(2026, 12, 8))
        self.assertEqual(self.t.get_task(i)["status"], "done")

    def test_search_order(self):
        a = self.t.add_task(self.p, "a", due=D(2026, 12, 5), priority=3)
        b = self.t.add_task(self.p, "b")
        c = self.t.add_task(self.p, "c", due=D(2026, 12, 5), priority=1)
        self.assertEqual(self.t.search(""), [c, a, b])

    def test_undo_add(self):
        i = self.t.add_task(self.p, "x")
        self.assertTrue(self.t.undo())
        with self.assertRaises(KeyError):
            self.t.get_task(i)
        self.assertFalse(self.t.undo())


if __name__ == "__main__":
    unittest.main()
