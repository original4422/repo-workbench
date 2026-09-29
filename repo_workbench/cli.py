"""Command-line interface."""

import argparse
import json
import sys

from .core import discover, inspect


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check local repository delivery without changing repositories.")
    parser.add_argument("root", nargs="?", help="Scan immediate child Git repositories")
    parser.add_argument("--repo", action="append", default=[], help="Explicit repository root (repeatable)")
    parser.add_argument("--json", action="store_true", help="Stable schema version 1 JSON")
    parser.add_argument("--verify-remote", action="store_true", help="Read target branch with git ls-remote; never fetch")
    parser.add_argument("--github", action="store_true", help="Read check runs and commit statuses for HEAD with gh")
    parser.add_argument("--verify", action="store_true", help="Enable both remote and GitHub verification")
    parser.add_argument("--remote", help="Target remote name, overriding the upstream remote")
    parser.add_argument("--branch", help="Target branch name, overriding the tracked/current branch")
    parser.add_argument("--needs-attention", action="store_true", help="Show repositories not verified delivered")
    parser.add_argument("--check", action="store_true", help="Exit 1 unless every scanned repository is verified delivered")
    args = parser.parse_args(argv)
    if args.root is None and not args.repo:
        parser.error("provide a root directory or --repo")
    try:
        paths = discover(args.root, args.repo)
        if not paths:
            parser.error("no repositories found")
        repos = [inspect(path, args.verify or args.verify_remote, args.verify or args.github, args.remote, args.branch) for path in paths]
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"repo-workbench: {exc}", file=sys.stderr)
        return 2
    shown = [repo for repo in repos if not repo["delivered"]] if args.needs_attention else repos
    if args.json:
        print(json.dumps({"schema_version": 1, "repositories": shown}, indent=2, sort_keys=True))
    else:
        headers = ["REPOSITORY", "BRANCH", "WORKTREE", "CACHED A/B", "REMOTE HEAD", "CI @ HEAD", "DELIVERED"]
        rows = [headers]
        for repo in shown:
            upstream = repo["upstream"]
            rows.append([repo["name"], repo["branch"] or "(detached)", "dirty" if repo["dirty"] else "clean",
                         f"{upstream['ahead']}/{upstream['behind']}" if upstream["name"] else "—",
                         repo["publication"]["state"], repo["ci"]["state"], "yes" if repo["delivered"] else "no"])
        widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
        for row in rows:
            print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        print("\nA/B uses cached refs. Delivered = clean + exact remote HEAD + all reported CI checks successful.")
    return 1 if args.check and any(not repo["delivered"] for repo in repos) else 0
