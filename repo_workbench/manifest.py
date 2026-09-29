"""Portable fixed-SHA delivery records and read-only live rechecks."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .core import command, config, discover, github_ci, github_slug, inspect, verify_remote


KIND = "repo-workbench-delivery"


def validate(document):
    if not isinstance(document, dict) or document.get("schema_version") != 1 or document.get("kind") != KIND:
        raise ValueError("unsupported delivery manifest")
    if not isinstance(document.get("verified_at"), str):
        raise ValueError("manifest requires verified_at")
    entries = document.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest requires repositories")
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid manifest repository")
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            raise ValueError("manifest name must be a single directory name")
        if name in names:
            raise ValueError(f"duplicate manifest name: {name}")
        names.add(name)
        slug = entry.get("github")
        if not isinstance(slug, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", slug):
            raise ValueError(f"invalid GitHub identity: {name}")
        sha = entry.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
            raise ValueError(f"full lowercase commit SHA required: {name}")
        branch = entry.get("branch")
        if not isinstance(branch, str) or not branch or command(["git", "check-ref-format", f"refs/heads/{branch}"]).returncode:
            raise ValueError(f"invalid target branch: {name}")
    return document


def write_manifest(filename, repos):
    """Return False on incomplete verification; never create partial batches."""
    output = Path(filename).resolve()
    if any(output.is_relative_to(Path(repo["path"])) for repo in repos):
        raise ValueError("manifest output must be outside inspected repositories")
    if output.exists():
        raise ValueError("manifest output already exists")
    if not all(repo["delivered"] for repo in repos):
        return False
    entries = []
    for repo in repos:
        target = repo["target"]
        url = config(Path(repo["path"]), f"remote.{target['remote']}.url")
        slug = github_slug(url) if url else None
        if not slug:
            raise ValueError(f"GitHub identity required: {repo['name']}")
        entries.append({"name": repo["name"], "github": slug, "branch": target["ref"].removeprefix("refs/heads/"), "sha": repo["head"]})
    document = validate({"schema_version": 1, "kind": KIND, "verified_at": datetime.now(timezone.utc).isoformat(),
                         "repositories": sorted(entries, key=lambda entry: entry["name"])})
    # Exclusive creation makes an existing artifact an error, including a race.
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return True


def read_manifest(filename):
    return validate(json.loads(Path(filename).read_text(encoding="utf-8")))


def locate(document, root, mappings):
    names = {entry["name"] for entry in document["repositories"]}
    paths = {}
    for mapping in mappings:
        name, separator, value = mapping.partition("=")
        if not separator or not value or name not in names or name in paths:
            raise ValueError(f"invalid or duplicate repository mapping: {name}")
        paths[name] = Path(value).expanduser().resolve()
    for name in names - paths.keys():
        if root is None:
            raise ValueError(f"--root or --map required for {name}")
        paths[name] = (Path(root).expanduser() / name).resolve()
    if len(set(paths.values())) != len(paths):
        raise ValueError("multiple manifest names map to the same repository")
    return paths


def recheck(document, paths):
    rows = []
    for entry in sorted(document["repositories"], key=lambda item: item["name"]):
        path = paths[entry["name"]]
        row = {"name": entry["name"], "github": entry["github"], "branch": entry["branch"],
               "expected_sha": entry["sha"], "local_head": None, "local_state": "missing",
               "dirty": None, "identity": "unchecked", "publication": {"state": "unavailable", "sha": None},
               "expected_ci": github_ci(entry["github"], entry["sha"]), "verified": False}
        if path.exists():
            try:
                discover(paths=[path])
                local = inspect(path)
            except (ValueError, RuntimeError, OSError):
                row["local_state"] = "unavailable"
            else:
                row["local_head"] = local["head"]
                row["dirty"] = local["dirty"]
                row["local_state"] = "matches" if local["head"] == entry["sha"] else "different-head"
                matches = [remote for remote in local["remotes"]
                           if (github_slug(config(path, f"remote.{remote}.url") or "") or "").casefold() == entry["github"].casefold()]
                row["identity"] = "matched" if len(matches) == 1 else "ambiguous" if matches else "mismatch"
                if len(matches) == 1:
                    target = {"state": "selected", "remote": matches[0], "ref": f"refs/heads/{entry['branch']}"}
                    row["publication"] = verify_remote(path, target, entry["sha"])
                else:
                    row["publication"]["state"] = f"identity-{row['identity']}"
        row["verified"] = (row["local_state"] == "matches" and row["dirty"] is False
                           and row["publication"]["state"] == "pushed" and row["expected_ci"]["state"] == "success")
        rows.append(row)
    return rows


def print_report(rows, as_json):
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "repo-workbench-delivery-recheck", "repositories": rows}, indent=2, sort_keys=True))
        return
    headers = ["REPOSITORY", "EXPECTED", "LOCAL HEAD", "LOCAL STATE", "WORKTREE", "REMOTE HEAD", "CI @ EXPECTED", "VERIFIED"]
    table = [headers]
    for row in rows:
        table.append([row["name"], row["expected_sha"][:8], (row["local_head"] or "—")[:8], row["local_state"],
                      "unknown" if row["dirty"] is None else "dirty" if row["dirty"] else "clean",
                      row["publication"]["state"], row["expected_ci"]["state"], "yes" if row["verified"] else "no"])
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for row in table:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    print("\nCI is for the manifest SHA. Different remote heads have unknown containment.")
