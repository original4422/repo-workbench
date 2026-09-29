"""Command-line interface."""

import argparse
import json
import sys

from .core import discover, inspect
from .manifest import locate, print_report, read_manifest, recheck, write_manifest


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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write-manifest", metavar="FILE", help="Verify all selected repos and exclusively create a portable fixed-SHA manifest")
    modes.add_argument("--manifest", metavar="FILE", help="Recheck manifest SHAs against live remote and GitHub CI")
    parser.add_argument("--root", dest="manifest_root", help="Locate manifest repositories under this directory")
    parser.add_argument("--map", action="append", default=[], metavar="NAME=PATH", help="Locate one manifest repository in another clone (repeatable)")
    args = parser.parse_args(argv)
    if args.manifest:
        if args.root or args.repo or args.remote or args.branch or args.verify or args.verify_remote or args.github:
            parser.error("--manifest uses --root/--map and always verifies live; scan selection/verification flags do not apply")
        try:
            document = read_manifest(args.manifest)
            paths = locate(document, args.manifest_root, args.map)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        repos = recheck(document, paths)
        shown = [repo for repo in repos if not repo["verified"]] if args.needs_attention else repos
        print_report(shown, args.json)
        return 1 if args.check and any(not repo["verified"] for repo in repos) else 0
    if args.manifest_root or args.map:
        parser.error("--root and --map require --manifest")
    if args.root is None and not args.repo:
        parser.error("provide a root directory or --repo")
    try:
        paths = discover(args.root, args.repo)
        if not paths:
            parser.error("no repositories found")
        repos = [inspect(path, bool(args.write_manifest) or args.verify or args.verify_remote, bool(args.write_manifest) or args.verify or args.github, args.remote, args.branch) for path in paths]
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"repo-workbench: {exc}", file=sys.stderr)
        return 2
    written = True
    if args.write_manifest:
        try:
            written = write_manifest(args.write_manifest, repos)
        except (ValueError, OSError) as exc:
            print(f"repo-workbench: {exc}", file=sys.stderr)
            return 2
        if written:
            print(f"Verified delivery manifest written: {args.write_manifest}", file=sys.stderr)
        else:
            print("Manifest not written: every repository must be verified delivered.", file=sys.stderr)
    shown = [repo for repo in repos if not repo["delivered"]] if args.needs_attention else repos
    if args.json:
        print(json.dumps({"schema_version": 1, "repositories": shown}, indent=2, sort_keys=True))
    else:
        headers = ["REPOSITORY", "BRANCH", "HEAD", "WORKTREE", "CACHED A/B", "TARGET", "REMOTE HEAD", "CI @ HEAD", "DELIVERED"]
        rows = [headers]
        for repo in shown:
            upstream = repo["upstream"]
            target = repo["target"]
            target_name = f"{target['remote']}/{target['ref'].removeprefix('refs/heads/')}" if target["ref"] else target["state"]
            rows.append([repo["name"], repo["branch"] or "(detached)", repo["head"][:8] if repo["head"] else "(unborn)", "dirty" if repo["dirty"] else "clean",
                         f"{upstream['ahead']}/{upstream['behind']}" if upstream["name"] else "—",
                         target_name, repo["publication"]["state"], repo["ci"]["state"], "yes" if repo["delivered"] else "no"])
        widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
        for row in rows:
            print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        print("\nA/B uses cached refs. Delivered = clean + exact remote HEAD + all reported CI checks successful.")
    return 1 if not written or (args.check and any(not repo["delivered"] for repo in repos)) else 0
