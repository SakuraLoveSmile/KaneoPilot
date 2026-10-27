"""Balanced pytest shard selection contract checks. No network."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("pytest_shards", Path(__file__).parents[1] / "pytest_shards.py")
pytest_shards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pytest_shards)


class DiscoverTests(unittest.TestCase):
    def test_matches_pytest_default_patterns_and_skips_caches(self):
        layout = [
            "tests/test_a.py",
            "tests/b_test.py",
            "tests/helper.py",
            "tests/sub/test_c.py",
            "tests/__pycache__/test_d.py",
            "tests/.hidden/test_e.py",
            "other/test_f.py",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for rel in layout:
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_text("", encoding="utf-8")
            self.assertEqual(pytest_shards.discover(base), ["tests/b_test.py", "tests/sub/test_c.py", "tests/test_a.py"])


class PlanTests(unittest.TestCase):
    def test_every_file_lands_in_exactly_one_shard(self):
        files = [f"tests/test_{index:02d}.py" for index in range(23)]
        seconds = {name: float(index % 7) + 0.5 for index, name in enumerate(files)}
        for shards in (1, 2, 5, 23):
            buckets, _ = pytest_shards.plan(files, seconds, shards)
            self.assertEqual(sorted(name for bucket in buckets for name in bucket), files)

    def test_independent_of_input_order(self):
        files = [f"tests/test_{index:02d}.py" for index in range(9)]
        seconds = {name: float(index * 3 % 5) for index, name in enumerate(files)}
        self.assertEqual(pytest_shards.plan(files, seconds, 4), pytest_shards.plan(list(reversed(files)), seconds, 4))

    def test_longest_first_onto_lightest_shard(self):
        seconds = {
            "tests/a.py": 100.0,
            "tests/b.py": 90.0,
            "tests/c.py": 80.0,
            "tests/d.py": 10.0,
            "tests/e.py": 10.0,
            "tests/f.py": 10.0,
        }
        buckets, loads = pytest_shards.plan(sorted(seconds), seconds, 3)
        self.assertEqual(loads, [100.0, 100.0, 100.0])
        self.assertEqual(buckets, [["tests/a.py"], ["tests/b.py", "tests/e.py"], ["tests/c.py", "tests/d.py", "tests/f.py"]])

    def test_unknown_files_weigh_the_suite_mean(self):
        seconds = {"tests/a.py": 10.0, "tests/b.py": 30.0}
        _, loads = pytest_shards.plan(["tests/a.py", "tests/b.py", "tests/new.py"], seconds, 2)
        self.assertEqual(loads, [30.0, 30.0])


class RepositoryShardTests(unittest.TestCase):
    def setUp(self):
        self.table = json.loads(pytest_shards.DURATIONS.read_text(encoding="utf-8"))

    def test_committed_table_partitions_each_suite_without_empty_shards(self):
        for suite, base in pytest_shards.SUITES.items():
            files = pytest_shards.discover(pytest_shards.REPO / base)
            buckets, _ = pytest_shards.plan(files, self.table[suite], 5)
            self.assertEqual(sorted(name for bucket in buckets for name in bucket), files, suite)
            self.assertTrue(all(buckets), suite)

    def test_cli_prints_the_planned_shard(self):
        files = pytest_shards.discover(pytest_shards.REPO / "core")
        buckets, _ = pytest_shards.plan(files, self.table["core"], 5)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pytest_shards.main(["core", "--shard", "2", "--shards", "5"]), 0)
        self.assertEqual(stdout.getvalue().splitlines(), buckets[1])

    def test_cli_rejects_shard_outside_range(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pytest_shards.main(["core", "--shard", "6", "--shards", "5"])


if __name__ == "__main__":
    unittest.main()
