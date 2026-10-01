# Repo Workbench

**Which of your local repositories are actually delivered?**

Repo Workbench scans a directory of Git repositories and connects three facts:

1. Is the working tree clean, including untracked files?
2. Does the selected remote branch currently point to this exact local commit?
3. Are the GitHub checks and commit statuses for this commit successful?

A green check from yesterday or a stale `origin/main` cannot answer all three.

Python 3.10+, Git, zero runtime Python dependencies. [中文说明](README.zh-CN.md).

## Install and scan

```sh
pip install git+https://github.com/original4422/repo-workbench.git
repo-workbench ~/projects
```

The default scan is local and read-only. It discovers immediate child repositories, including linked worktrees. Use `--repo` repeatedly to select other repository roots.

```sh
repo-workbench --repo ~/projects/demo-agent --repo ~/projects/demo-evals
repo-workbench ~/projects --json
```

Example with fictional repositories:

```text
REPOSITORY  BRANCH  HEAD      WORKTREE  CACHED A/B  TARGET       REMOTE HEAD  CI @ HEAD  DELIVERED
demo-agent  main    a1b2c3d4  clean     0/0         origin/main  unverified   unchecked  no
demo-evals  feat    e5f6a7b8  dirty     2/0         origin/feat  unverified   unchecked  no
```

`A/B` means ahead/behind the **locally cached** upstream ref. A local scan does not contact any server and cannot certify delivery.

## Verify delivery

Install the [GitHub CLI](https://cli.github.com/) and authenticate it for GitHub checks. Git itself uses your existing remote authentication.

```sh
repo-workbench ~/projects --verify
repo-workbench ~/projects --verify --needs-attention
repo-workbench --repo ~/projects/demo-agent --verify --check --json
```

`--verify` enables both:

- `--verify-remote`: read the selected branch with `git ls-remote`, without fetching or modifying refs.
- `--github`: read check runs and commit statuses for the exact local HEAD SHA using `gh api`.

`delivered: true` means **clean tree + exact remote branch head + all reported CI results successful** at inspection time. The checks are collected sequentially; the result is a point-in-time observation, not a lock on the remote branch. Missing CI, pending checks, skipped/neutral checks, unavailable APIs, and checks for another SHA are distinct states and never count as success. The [check-runs API](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference) is queried with `filter=all`, then repeated check runs use the highest run ID per GitHub App, check suite, and check name. Separate workflow suites with the same job name remain separate results. This is a commit CI check, not an evaluation of branch-protection or review requirements.

### Target branch selection

1. `--remote NAME` overrides the target remote; otherwise use the current branch's upstream remote.
2. Without a remote upstream, select the only configured remote. Multiple remotes require `--remote`.
3. `--branch NAME` overrides the target branch; otherwise use the upstream branch when targeting its remote, or the current local branch name.
4. Detached HEAD needs `--branch`; an unborn repository has no commit to verify.

```sh
repo-workbench --repo ~/projects/demo-agent --verify --remote fork --branch feature
```

The target is the named remote's **fetch URL**, as used by `git ls-remote`. No default branch, push URL, `push.default`, or `remote.pushDefault` is inferred. A local `feature` tracking `origin/release` is checked against `release`, even if the remote default is `main`.

### Remote evidence

| State | Meaning |
|---|---|
| `unverified` | No live remote query requested |
| `pushed` | Selected remote branch points to the exact local HEAD |
| `different-head` | Remote and local heads differ; history/containment is unknown |
| `missing-branch` | Query succeeded, but that remote branch does not exist |
| `no-remote` / `ambiguous` | No remote, or multiple remotes without a selected target |
| `unknown-remote` / `no-branch` / `unborn` | Target is not checkable |
| `unavailable` | Remote query failed or timed out |

`different-head` does **not** mean the local commit was never pushed. The remote may have advanced since it was pushed. Cached ahead/behind remains a separate observation.

## JSON and exit codes

```sh
repo-workbench ~/projects --verify --json > delivery.json
```

JSON has `schema_version: 1` and a deterministically sorted `repositories` array. Each entry includes `name`, local absolute `path`, `branch`, `head`, `dirty`, remote **names**, cached `upstream` counts, selected `target`, live `publication` evidence, `ci` evidence, and `delivered`.

Remote URLs, credentials, file contents, changed filenames, and session logs are not included. JSON includes local paths and repository names for local automation.

| Exit | Meaning |
|---|---|
| `0` | Scan succeeded; with `--check`, every scanned repository is delivered |
| `1` | `--check` found at least one repository not verified delivered |
| `2` | Invalid input or local scan error, including no repositories found |

`--needs-attention` only filters output. `--check` evaluates all scanned repositories. Remote/API failures are report states, so ordinary scans still exit `0`; combine with `--check` for a gate.

## Keep a fixed delivery batch

A later commit should not replace the evidence for a batch you already accepted. Create a portable manifest for the selected repositories:

```sh
repo-workbench --repo ~/projects/demo-agent --repo ~/projects/demo-evals \
  --write-manifest ~/deliveries/batch-01.json
```

`--write-manifest` performs live remote and GitHub verification. It creates the file only when **every selected repository** is clean, has the exact remote branch head, and has successful CI. A failed batch exits `1` without creating a file. The destination directory must exist; an existing file or a destination inside an inspected repository is rejected. Give each batch a new filename.

The manifest uses repository directory names, GitHub identities, target branches, and full SHAs. It contains no absolute clone paths or remote URLs. This fictional example shows the format:

```json
{
  "schema_version": 1,
  "kind": "repo-workbench-delivery",
  "verified_at": "2026-09-30T00:00:00+00:00",
  "repositories": [
    {
      "name": "demo-agent",
      "github": "example/demo-agent",
      "branch": "main",
      "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ]
}
```

Recheck that exact batch later or from another machine:

```sh
repo-workbench --manifest ~/deliveries/batch-01.json --root ~/projects --check
repo-workbench --manifest ~/deliveries/batch-01.json --root ~/projects \
  --map demo-agent=~/other-clones/agent --json
```

`--root DIR` resolves each entry as `DIR/name`; repeat `--map NAME=PATH` to override individual locations. When every entry is mapped, `--root` is optional. Local branches and remote names may differ from the capturing clone. Exactly one fetch remote must match the recorded GitHub identity; the recorded target branch stays fixed. Duplicate names, duplicate mappings, unknown mapping names, and two entries mapped to one clone are errors.

Manifest mode always queries live evidence, so scan flags such as `--verify`, `--repo`, `--remote`, and `--branch` do not apply. The file is an editable target record; its `verified_at` records capture time and is not used to establish current success.

The recheck JSON has `kind: repo-workbench-delivery-recheck`. Each row contains `expected_sha`, `local_head`, `local_state`, `dirty`, `identity`, `publication` (including the observed remote SHA), `expected_ci`, and `verified`. **CI is queried for the recorded SHA**, even if the local HEAD has changed. A newer green commit cannot substitute for an older failing commit, and an older green commit does not certify the newer working tree.

`verified: true` requires a clean matching local HEAD, a uniquely matching GitHub remote whose target branch still points to the recorded SHA, and successful CI for that SHA. Missing clones, changed local heads, dirty trees, mismatched/ambiguous remote identities, changed remote heads, and unavailable CI remain visible independently. A different remote head still has **unknown containment**; no ancestry is inferred or fetched.

Ordinary rechecks exit `0` after reporting all entries; `--check` exits `1` if any entry is not verified. Invalid manifests, mappings, and argument combinations exit `2`. `--needs-attention` filters output only; `--check` still evaluates the complete batch. The original scan's `delivered` contract is unchanged.

## Development

```sh
git clone https://github.com/original4422/repo-workbench.git
cd repo-workbench
python3 -m repo_workbench --help
python3 -m unittest discover -s tests -v
```

Tests create real temporary Git repositories and local bare remotes. They cover dirty/untracked worktrees, stale refs, ahead/behind/divergence, branch-name differences, multiple remotes, detached and unborn states, linked worktrees, read-only file equality, GitHub SHA matching, reruns, and CLI exit codes. Manifest tests also cover old/new SHA separation, relocated clones, partial batches, exclusive output, and byte-for-byte target repository preservation. GitHub API contracts are exercised with fixtures; tests need no credentials or network.

The tool never commits, pushes, fetches, cleans, or edits scanned repositories. `GIT_OPTIONAL_LOCKS=0` avoids index refresh writes during status inspection. It is intended for non-bare local working repositories; GitHub CI checks currently support github.com remotes.

## License

MIT.
