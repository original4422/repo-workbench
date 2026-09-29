"""Local facts, remote evidence, and commit-specific CI results."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlparse


def command(args, cwd=None, timeout=30):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


def git(path, *args):
    result = command(["git", "-C", str(path), *args])
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed in {path.name}")
    return result.stdout.strip()


def config(path, key):
    result = command(["git", "-C", str(path), "config", "--get", key])
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Cannot read Git configuration in {path.name}")
    return result.stdout.strip() or None


def discover(root=None, paths=()):
    candidates = list(map(Path, paths))
    if root is not None:
        candidates.extend(p for p in Path(root).iterdir() if p.is_dir() and (p / ".git").exists())
    repos = sorted({p.resolve() for p in candidates})
    for path in repos:
        if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path:
            raise ValueError(f"Not a repository root: {path}")
    return repos


def github_slug(url):
    """Parse GitHub URLs without returning credentials or the original URL."""
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        parsed = urlparse(url)
        if parsed.hostname != "github.com" or parsed.scheme not in ("https", "ssh"):
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    return path if re.fullmatch(r"[\w.-]+/[\w.-]+", path) else None


def select_target(branch, upstream_remote, merge_ref, remotes, remote=None, target_branch=None):
    selected = remote or (upstream_remote if upstream_remote in remotes else None)
    if selected is None and len(remotes) == 1:
        selected = remotes[0]
    if selected is None:
        return {"state": "no-remote" if not remotes else "ambiguous", "remote": None, "ref": None}
    if selected not in remotes:
        return {"state": "unknown-remote", "remote": selected, "ref": None}
    ref = f"refs/heads/{target_branch}" if target_branch else (
        merge_ref if selected == upstream_remote and merge_ref else f"refs/heads/{branch}" if branch else None
    )
    return {"state": "selected" if ref else "no-branch", "remote": selected, "ref": ref}


def verify_remote(path, target, head):
    if target["state"] != "selected" or head is None:
        return {"state": target["state"] if head else "unborn", "sha": None}
    try:
        result = command(["git", "-C", str(path), "ls-remote", "--exit-code", target["remote"], target["ref"]])
    except subprocess.TimeoutExpired:
        return {"state": "unavailable", "sha": None}
    if result.returncode == 2:
        return {"state": "missing-branch", "sha": None}
    if result.returncode:
        return {"state": "unavailable", "sha": None}
    matches = [line.split()[0] for line in result.stdout.splitlines() if line.split()[1] == target["ref"]]
    sha = matches[0] if matches else None
    return {"state": "pushed" if sha == head else "different-head", "sha": sha}


def summarize_ci(head, check_pages, status):
    checks = [check for page in check_pages for check in page["check_runs"]]
    if any(check["head_sha"] != head for check in checks) or status.get("sha") != head:
        return {"state": "sha-mismatch", "checks": []}
    # Keep distinct workflow suites even when they use the same job name.
    # Reruns within one suite use the highest check-run ID.
    latest = {}
    for check in sorted(checks, key=lambda item: item["id"]):
        latest[(check["app"]["id"], check["check_suite"]["id"], check["name"])] = {
            "name": check["name"], "source": "check", "state": check["conclusion"] if check["status"] == "completed" else "pending"
        }
    evidence = list(latest.values()) + [
        {"name": item["context"], "source": "status", "state": item["state"]} for item in status["statuses"]
    ]
    evidence.sort(key=lambda item: (item["source"], item["name"]))
    states = {item["state"] for item in evidence}
    state = "none" if not evidence else "success" if states == {"success"} else (
        "failure" if states & {"failure", "error", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
        else "pending" if "pending" in states else "not-success"
    )
    return {"state": state, "checks": evidence}


def github_ci(slug, head):
    if not slug or not head:
        return {"state": "unsupported" if head else "unborn", "checks": []}
    if not shutil.which("gh"):
        return {"state": "unavailable", "checks": []}
    try:
        checks = command(["gh", "api", f"repos/{slug}/commits/{head}/check-runs?per_page=100", "--paginate", "--slurp"])
        status = command(["gh", "api", f"repos/{slug}/commits/{head}/status?per_page=100", "--paginate", "--slurp"])
    except subprocess.TimeoutExpired:
        return {"state": "unavailable", "checks": []}
    if checks.returncode or status.returncode:
        return {"state": "unavailable", "checks": []}
    pages = json.loads(status.stdout)
    combined = {"sha": pages[0]["sha"], "statuses": [item for page in pages for item in page["statuses"]]}
    if any(page["sha"] != head for page in pages):
        return {"state": "sha-mismatch", "checks": []}
    return summarize_ci(head, json.loads(checks.stdout), combined)


def inspect(path, verify=False, check_ci=False, remote=None, target_branch=None):
    path = Path(path).resolve()
    branch_result = command(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = branch_result.stdout.strip() or None
    head_result = command(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"])
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    # -z preserves spaces/newlines in filenames; no filenames enter the report.
    changes = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    remotes = git(path, "remote").splitlines()
    upstream_remote = config(path, f"branch.{branch}.remote") if branch else None
    merge_ref = config(path, f"branch.{branch}.merge") if branch else None
    upstream_result = command(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "@{upstream}"])
    upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    ahead = behind = None
    if head and upstream:
        ahead, behind = map(int, git(path, "rev-list", "--left-right", "--count", "HEAD...@{upstream}").split())
    target = select_target(branch, upstream_remote, merge_ref, remotes, remote, target_branch)
    publication = verify_remote(path, target, head) if verify else {"state": "unverified", "sha": None}
    url = config(path, f"remote.{target['remote']}.url") if target["state"] == "selected" else None
    ci = github_ci(github_slug(url) if url else None, head) if check_ci else {"state": "unchecked", "checks": []}
    delivered = bool(head and not changes and publication["state"] == "pushed" and ci["state"] == "success")
    return {
        "name": path.name, "path": str(path), "branch": branch, "head": head,
        "dirty": bool(changes), "remotes": remotes,
        "upstream": {"name": upstream, "ahead": ahead, "behind": behind, "evidence": "local-cached-refs"},
        "target": target, "publication": publication, "ci": ci, "delivered": delivered,
    }
