import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from repo_workbench import core
from repo_workbench.cli import main
from repo_workbench.manifest import locate, read_manifest, recheck, validate, write_manifest
from test_workbench import run


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "sample"
        self.repo.mkdir()
        run(self.repo, "init", "-b", "main")
        run(self.repo, "config", "user.name", "Fixture")
        run(self.repo, "config", "user.email", "fixture@example.test")
        self.sha = self.commit("initial")
        self.remote = self.root / "remote.git"
        run(self.root, "init", "--bare", str(self.remote))
        run(self.repo, "remote", "add", "origin", "https://github.com/fictional/sample.git")
        # Real local bare transport; GitHub identity stays portable in config.
        run(self.repo, "config", f"url.{self.remote}.insteadOf", "https://github.com/fictional/sample.git")
        run(self.repo, "push", "-u", "origin", "main")
        self.output = self.root / "batch.json"
        self.queried = []
        self.ci_states = {}
        original = core.command

        def command(args, *positional, **kwargs):
            if args[0] != "gh":
                return original(args, *positional, **kwargs)
            endpoint = args[2]
            sha = endpoint.split("/commits/")[1].split("/")[0]
            self.queried.append(sha)
            state = self.ci_states.get(sha, "success")
            if state == "unavailable":
                return subprocess.CompletedProcess(args, 1, "", "private API error")
            payload = ([{"check_runs": [{"id": 1, "name": "test", "app": {"id": 1}, "check_suite": {"id": 1},
                                         "head_sha": sha, "status": "completed", "conclusion": state}]}]
                       if "/check-runs?" in endpoint else [{"sha": sha, "statuses": []}])
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        patcher = patch("repo_workbench.core.command", side_effect=command)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("repo_workbench.core.shutil.which", return_value="/fixture/gh")
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, content):
        (self.repo / "file").write_text(content)
        run(self.repo, "add", "file")
        run(self.repo, "commit", "-m", content)
        return run(self.repo, "rev-parse", "HEAD")

    def capture(self):
        repos = [core.inspect(self.repo, verify=True, check_ci=True)]
        self.assertTrue(write_manifest(self.output, repos))
        return read_manifest(self.output)

    def check(self, document, root=None, mappings=()):
        return recheck(document, locate(document, root or self.root, mappings))

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_capture_recheck_read_only_and_portable_schema(self):
        before = {str(p): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        document = self.capture()
        self.assertEqual(document["repositories"], [{"name": "sample", "github": "fictional/sample", "branch": "main", "sha": self.sha}])
        self.assertNotIn(str(self.root), self.output.read_text())
        self.assertTrue(self.check(document)[0]["verified"])
        after = {str(p): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_old_sha_ci_never_replaced_by_green_current_head(self):
        document = self.capture()
        new = self.commit("new")
        run(self.repo, "push")
        self.ci_states[self.sha] = "failure"
        self.queried.clear()
        row = self.check(document)[0]
        self.assertEqual(row["local_head"], new)
        self.assertEqual(row["local_state"], "different-head")
        self.assertEqual(row["publication"], {"state": "different-head", "sha": new})
        self.assertEqual(row["expected_ci"]["state"], "failure")
        self.assertEqual(set(self.queried), {self.sha})
        self.assertFalse(row["verified"])
        # The inverse also keeps old green distinct from a new red HEAD.
        self.ci_states[self.sha], self.ci_states[new] = "success", "failure"
        row = self.check(document)[0]
        self.assertEqual(row["expected_ci"]["state"], "success")
        self.assertFalse(row["verified"])
        self.assertEqual(core.inspect(self.repo, check_ci=True)["ci"]["state"], "failure")

    def test_remote_advancement_and_missing_branch(self):
        document = self.capture()
        new = self.commit("remote advancement")
        run(self.repo, "push")
        run(self.repo, "switch", "--detach", self.sha)  # Fixture setup only.
        row = self.check(document)[0]
        self.assertEqual(row["local_state"], "matches")
        self.assertEqual(row["publication"], {"state": "different-head", "sha": new})
        self.assertFalse(row["verified"])
        run(self.remote, "update-ref", "-d", "refs/heads/main")
        self.assertEqual(self.check(document)[0]["publication"]["state"], "missing-branch")

    def test_relocated_clone_remote_rename_and_fixed_branch(self):
        document = self.capture()
        clone = self.root / "renamed-clone"
        run(self.root, "clone", "--branch", "main", str(self.remote), str(clone))
        run(clone, "remote", "rename", "origin", "delivery")
        run(clone, "remote", "set-url", "delivery", "git@github.com:fictional/sample.git")
        run(clone, "config", f"url.{self.remote}.insteadOf", "git@github.com:fictional/sample.git")
        run(clone, "branch", "-m", "local-feature")
        paths = locate(document, None, [f"sample={clone}"])
        self.assertTrue(recheck(document, paths)[0]["verified"])

    def test_wrong_remote_identity_and_ambiguous_same_identity(self):
        document = self.capture()
        run(self.repo, "remote", "set-url", "origin", "https://github.com/someone/other.git")
        row = self.check(document)[0]
        self.assertEqual(row["identity"], "mismatch")
        self.assertFalse(row["verified"])
        run(self.repo, "remote", "set-url", "origin", "https://github.com/fictional/sample.git")
        run(self.repo, "remote", "add", "mirror", "git@github.com:fictional/sample.git")
        self.assertEqual(self.check(document)[0]["identity"], "ambiguous")

    def test_partial_batch_missing_dirty_and_unknown_ci(self):
        document = self.capture()
        document["repositories"].append({**document["repositories"][0], "name": "absent"})
        rows = self.check(document)
        self.assertEqual(rows[0]["local_state"], "missing")
        self.assertFalse(rows[0]["verified"])
        self.assertTrue(rows[1]["verified"])
        (self.repo / "untracked").write_text("pending")
        self.assertFalse(self.check(document)[1]["verified"])
        (self.repo / "untracked").unlink()
        self.ci_states[self.sha] = "unavailable"
        row = self.check(document)[1]
        self.assertEqual(row["expected_ci"]["state"], "unavailable")
        self.assertFalse(row["verified"])
        self.output.write_text(json.dumps(document))
        args = ["--manifest", str(self.output), "--root", str(self.root), "--json"]
        self.assertEqual(self.invoke(args)[0], 0)
        self.assertEqual(self.invoke([*args, "--check"])[0], 1)
        # Filtering affects display only; the full fixed batch remains the gate.
        self.ci_states[self.sha] = "success"
        code, output, _ = self.invoke([*args, "--check", "--needs-attention"])
        self.assertEqual(code, 1)
        self.assertEqual([row["name"] for row in json.loads(output)["repositories"]], ["absent"])

    def test_capture_all_or_nothing_and_exclusive_output(self):
        (self.repo / "pending").write_text("change")
        code, _, _ = self.invoke(["--repo", str(self.repo), "--write-manifest", str(self.output)])
        self.assertEqual(code, 1)
        self.assertFalse(self.output.exists())
        (self.repo / "pending").unlink()
        document = self.capture()
        original = self.output.read_bytes()
        with self.assertRaises(ValueError):
            write_manifest(self.output, [core.inspect(self.repo, verify=True, check_ci=True)])
        self.assertEqual(self.output.read_bytes(), original)
        with self.assertRaises(ValueError):
            write_manifest(self.repo / "batch.json", [core.inspect(self.repo, verify=True, check_ci=True)])
        self.assertFalse((self.repo / "batch.json").exists())
        failed = {**core.inspect(self.repo, verify=True, check_ci=True), "name": "another", "delivered": False}
        self.assertFalse(write_manifest(self.root / "partial.json", [core.inspect(self.repo, verify=True, check_ci=True), failed]))
        self.assertFalse((self.root / "partial.json").exists())

    def test_duplicate_names_invalid_sha_and_mapping_rejected(self):
        document = self.capture()
        duplicate = {**document, "repositories": document["repositories"] * 2}
        with self.assertRaises(ValueError):
            validate(duplicate)
        with self.assertRaises(ValueError):
            validate({**document, "repositories": [{**document["repositories"][0], "sha": "abc123"}]})
        with self.assertRaises(ValueError):
            locate(document, self.root, [f"sample={self.repo}", f"sample={self.repo}"])
        with self.assertRaises(ValueError):
            locate(document, self.root, [f"unknown={self.repo}"])
        two = {**document, "repositories": [document["repositories"][0], {**document["repositories"][0], "name": "other"}]}
        with self.assertRaises(ValueError):
            locate(two, None, [f"sample={self.repo}", f"other={self.repo}"])
        with self.assertRaises(ValueError):
            locate(document, None, [])

    def test_cli_modes_and_successful_capture(self):
        code, stdout, _ = self.invoke(["--repo", str(self.repo), "--write-manifest", str(self.output), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["repositories"][0]["delivered"])
        args = ["--manifest", str(self.output), "--root", str(self.root), "--check", "--json"]
        code, stdout, _ = self.invoke(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["repositories"][0]["expected_sha"], self.sha)
        for extra in [["--repo", str(self.repo)], ["--verify"], ["--branch", "main"], [str(self.root)], ["--write-manifest", "other.json"]]:
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as caught:
                self.invoke([*args, *extra])
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit) as caught:
            self.invoke(["--root", str(self.root)])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
