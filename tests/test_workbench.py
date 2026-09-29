import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_workbench.cli import main
from repo_workbench.core import discover, github_ci, github_slug, inspect, select_target, summarize_ci


def run(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "sample"
        self.repo.mkdir()
        run(self.repo, "init", "-b", "main")
        run(self.repo, "config", "user.name", "Fixture")
        run(self.repo, "config", "user.email", "fixture@example.test")
        self.commit("initial")

    def commit(self, text, repo=None):
        repo = repo or self.repo
        (repo / "file.txt").write_text(text)
        run(repo, "add", "file.txt")
        run(repo, "commit", "-m", text)
        return run(repo, "rev-parse", "HEAD")

    def remote(self):
        remote = self.root / "remote.git"
        remote.mkdir()
        run(remote, "init", "--bare")
        run(self.repo, "remote", "add", "origin", str(remote))
        run(self.repo, "push", "-u", "origin", "main")
        return remote

    def other(self):
        other = self.root / "other"
        run(self.root, "clone", "--branch", "main", str(self.root / "remote.git"), str(other))
        run(other, "config", "user.name", "Fixture")
        run(other, "config", "user.email", "fixture@example.test")
        return other

    def test_clean_no_remote_is_not_delivered(self):
        result = inspect(self.repo, verify=True)
        self.assertFalse(result["dirty"])
        self.assertEqual(result["publication"]["state"], "no-remote")
        self.assertFalse(result["delivered"])

    def test_dirty_and_untracked(self):
        (self.repo / "untracked.txt").write_text("new")
        self.assertTrue(inspect(self.repo)["dirty"])
        (self.repo / "untracked.txt").unlink()
        (self.repo / "file.txt").write_text("changed")
        self.assertTrue(inspect(self.repo)["dirty"])

    def test_cached_sync_is_not_remote_verification(self):
        self.remote()
        result = inspect(self.repo)
        self.assertEqual(result["upstream"]["ahead"], 0)
        self.assertEqual(result["publication"]["state"], "unverified")
        self.assertFalse(result["delivered"])

    def test_ahead_behind_diverged_and_stale_cache(self):
        self.remote()
        other = self.other()
        remote_head = self.commit("remote edit", other)
        run(other, "push")
        result = inspect(self.repo, verify=True)
        self.assertEqual(result["upstream"]["behind"], 0)  # No fetch was performed.
        self.assertEqual(result["publication"], {"state": "different-head", "sha": remote_head})
        run(self.repo, "fetch")  # Explicit test setup, never done by the product.
        self.assertEqual(inspect(self.repo)["upstream"]["behind"], 1)
        self.commit("local edit")
        result = inspect(self.repo)
        self.assertEqual((result["upstream"]["ahead"], result["upstream"]["behind"]), (1, 1))

    def test_ahead(self):
        self.remote()
        self.commit("second")
        result = inspect(self.repo, verify=True)
        self.assertEqual(result["upstream"]["ahead"], 1)
        self.assertEqual(result["publication"]["state"], "different-head")

    def test_missing_branch(self):
        self.remote()
        run(self.repo, "switch", "-c", "new")
        self.assertEqual(inspect(self.repo, verify=True)["publication"]["state"], "missing-branch")

    def test_tracking_branch_different_from_local_and_remote_default(self):
        self.remote()
        run(self.repo, "branch", "-m", "local-name")
        result = inspect(self.repo, verify=True)
        self.assertEqual(result["target"]["ref"], "refs/heads/main")
        self.assertEqual(result["publication"]["state"], "pushed")

    def test_no_upstream_single_remote_uses_current_branch(self):
        self.remote()
        run(self.repo, "branch", "--unset-upstream")
        result = inspect(self.repo, verify=True)
        self.assertEqual(result["publication"]["state"], "pushed")
        self.assertIsNone(result["upstream"]["name"])

    def test_multiple_remotes_need_target_without_upstream(self):
        remote = self.remote()
        run(self.repo, "remote", "add", "mirror", str(remote))
        run(self.repo, "branch", "--unset-upstream")
        self.assertEqual(inspect(self.repo, verify=True)["publication"]["state"], "ambiguous")
        self.assertEqual(inspect(self.repo, verify=True, remote="mirror")["publication"]["state"], "pushed")

    def test_detached_and_unborn(self):
        self.remote()
        run(self.repo, "switch", "--detach")
        self.assertEqual(inspect(self.repo, verify=True)["publication"]["state"], "no-branch")
        self.assertEqual(inspect(self.repo, verify=True, target_branch="main")["publication"]["state"], "pushed")
        empty = self.root / "empty"
        empty.mkdir()
        run(empty, "init", "-b", "main")
        self.assertIsNone(inspect(empty)["head"])

    def test_read_only_including_index_refs_config(self):
        self.remote()
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        inspect(self.repo, verify=True)
        after = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_discovery_direct_only_and_explicit_worktree(self):
        nested = self.repo / "nested"
        nested.mkdir()
        run(nested, "init")
        self.assertEqual(discover(self.root), [self.repo])
        worktree = self.root / "worktree"
        run(self.repo, "worktree", "add", "-b", "work", str(worktree))
        self.assertEqual(discover(paths=[worktree]), [worktree])
        (self.repo / "subdirectory").mkdir()
        with self.assertRaises(ValueError):
            discover(paths=[self.repo / "subdirectory"])

    def test_dirty_never_delivered_and_real_remote_ci_contract(self):
        self.remote()
        with patch("repo_workbench.core.github_ci", return_value={"state": "success", "checks": [{"name": "test"}]}):
            self.assertTrue(inspect(self.repo, verify=True, check_ci=True)["delivered"])
            (self.repo / "new").write_text("pending")
            self.assertFalse(inspect(self.repo, verify=True, check_ci=True)["delivered"])

    def test_stable_json_and_exit_codes(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(main(["--repo", str(self.repo), "--json", "--check"]), 1)
        data = json.loads(stream.getvalue())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["repositories"][0]["name"], "sample")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.root)]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--repo", str(self.root / "missing")]), 2)


class GitHubTests(unittest.TestCase):
    head = "a" * 40

    def check(self, conclusion="success", sha=None, status="completed", ident=1):
        return {"id": ident, "name": "tests", "app": {"id": 1}, "check_suite": {"id": 10}, "head_sha": sha or self.head, "conclusion": conclusion, "status": status}

    def summary(self, checks=(), statuses=()):
        return summarize_ci(self.head, [{"check_runs": list(checks)}], {"sha": self.head, "statuses": list(statuses)})

    def test_no_ci_is_not_success(self):
        self.assertEqual(self.summary()["state"], "none")

    def test_old_sha_not_success(self):
        self.assertEqual(self.summary([self.check(sha="b" * 40)])["state"], "sha-mismatch")

    def test_success_pending_failure_skipped(self):
        for conclusion, status, expected in [("success", "completed", "success"), (None, "in_progress", "pending"), ("failure", "completed", "failure"), ("skipped", "completed", "not-success")]:
            with self.subTest(expected=expected):
                self.assertEqual(self.summary([self.check(conclusion, status=status)])["state"], expected)

    def test_rerun_and_commit_status(self):
        self.assertEqual(self.summary([self.check("failure"), self.check(ident=2)])["state"], "success")
        self.assertEqual(self.summary([self.check()], [{"context": "external", "state": "pending"}])["state"], "pending")
        self.assertEqual(self.summary([], [{"context": "external", "state": "success"}])["state"], "success")

    def test_same_name_in_distinct_workflows_keeps_failure(self):
        failed = self.check("failure")
        successful = self.check(ident=2)
        successful["check_suite"]["id"] = 11
        self.assertEqual(self.summary([failed, successful])["state"], "failure")

    def test_slug_does_not_leak_token(self):
        self.assertEqual(github_slug("https://token-secret@github.com/example/project.git"), "example/project")
        self.assertEqual(github_slug("git@github.com:example/project.git"), "example/project")
        self.assertEqual(github_slug("ssh://git@github.com/example/project.git"), "example/project")
        self.assertIsNone(github_slug("https://elsewhere.test/example/project"))

    def test_paginated_gh_contract_requests_exact_sha(self):
        pages = [{"check_runs": [self.check()]}]
        status = [{"sha": self.head, "statuses": [{"context": "external", "state": "success"}]}]
        responses = [subprocess.CompletedProcess([], 0, json.dumps(pages), ""), subprocess.CompletedProcess([], 0, json.dumps(status), "")]
        with patch("repo_workbench.core.shutil.which", return_value="/bin/gh"), patch("repo_workbench.core.command", side_effect=responses) as calls:
            self.assertEqual(github_ci("example/project", self.head)["state"], "success")
        for call in calls.call_args_list:
            self.assertIn(f"/commits/{self.head}/", call.args[0][2])
            self.assertIn("--paginate", call.args[0])

    def test_gh_error_is_unavailable_not_failure_or_success(self):
        response = subprocess.CompletedProcess([], 1, "", "SECRET")
        with patch("repo_workbench.core.shutil.which", return_value="/bin/gh"), patch("repo_workbench.core.command", return_value=response):
            self.assertEqual(github_ci("example/project", self.head), {"state": "unavailable", "checks": []})

    def test_explicit_target_wins(self):
        result = select_target("local", "origin", "refs/heads/main", ["origin", "fork"], "fork", "feature")
        self.assertEqual(result, {"state": "selected", "remote": "fork", "ref": "refs/heads/feature"})


if __name__ == "__main__":
    unittest.main()
