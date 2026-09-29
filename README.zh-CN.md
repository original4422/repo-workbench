# Repo Workbench

**一眼看清多个本地仓库是否完成交付。**

交付检查连接三个事实：工作区干净、指定远端分支指向当前 HEAD、这个 SHA 的 GitHub CI 全部成功。未提交修改、未跟踪文件、旧提交的绿灯都不会被算作已交付。

Python 3.10+、Git，Python 运行时零第三方依赖。完整参数及状态见 [English README](README.md)。

## 使用

```sh
pip install git+https://github.com/original4422/repo-workbench.git
repo-workbench ~/projects
repo-workbench ~/projects --verify
repo-workbench ~/projects --verify --needs-attention
repo-workbench --repo ~/projects/demo-agent --verify --check --json
```

默认仅扫描指定目录的直接子 Git 仓库，也支持用多个 `--repo` 指定路径。默认不联网，表中的 ahead/behind 来自本地缓存的 upstream refs。

`--verify` 通过 `git ls-remote` 查询目标分支，并使用已登录的 `gh` 查询当前提交的 check runs 和 commit statuses。也可以分别使用 `--verify-remote`、`--github`。

- `pushed`：远端目标分支的头与本地 HEAD 完全相同。
- `different-head`：两边的头不同，尚不能判断当前提交是否包含在远端历史中。
- `unavailable`：查询失败或超时；不等同于 CI 失败，也不等同于成功。
- `delivered: true`：工作区干净、远端头一致、全部已报告 CI 结果成功。没有 CI、尚未结束、跳过、中立状态都不会判为成功。

默认检查 upstream 对应的远端与分支；没有 upstream 时，仅在单 remote 情况下选择它，并使用本地分支名。可明确指定：

```sh
repo-workbench --repo ~/projects/demo-agent --verify --remote fork --branch feature
```

这个目标是 remote 的 fetch URL，不推断 push URL 或默认分支。检查依次执行，报告记录检查时观察到的状态。

## 自动化

JSON 提供稳定的 `schema_version: 1`、当前提交、工作区状态、缓存 ahead/behind、实时远端证据及 CI 明细。包含本地路径与仓库名，不输出 remote URL、凭据、文件内容、变更文件名或会话日志。

退出码：普通扫描成功为 `0`；加 `--check` 时，任一仓库未被确认交付为 `1`；输入或本地扫描错误为 `2`。`--needs-attention` 只过滤展示，不改变检查范围。

## 固定一批交付，再按原 SHA 复核

当前 HEAD 会随下一轮开发前进。交付清单把当时选定的仓库、GitHub 身份、目标分支和完整 SHA 固定下来：

```sh
repo-workbench --repo ~/projects/demo-agent --repo ~/projects/demo-evals \
  --write-manifest ~/deliveries/batch-01.json
repo-workbench --manifest ~/deliveries/batch-01.json --root ~/projects --check
```

`--write-manifest` 自动进行实时远端与 GitHub 检查。只有整批仓库都满足工作区干净、远端分支头一致、该 SHA 的 CI 成功，才创建文件；任一未通过则退出 `1`，不生成部分清单。输出目录须已存在，已有文件或位于被检查仓库内的输出路径会被拒绝。每批使用新文件名。

清单格式为 `schema_version: 1`、`kind: repo-workbench-delivery`、`verified_at` 和 `repositories` 数组，每项包含 `name`、`github`（如 `example/demo-agent`）、`branch`、`sha`。不保存绝对路径、remote URL 或凭据；完整虚构示例见英文 README。清单可编辑，`verified_at` 仅记录生成时间，复核会重新查询真实状态。

换一台机器或换一个 clone，仍可使用同一清单：

```sh
repo-workbench --manifest ~/deliveries/batch-01.json --root ~/projects \
  --map demo-agent=~/other-clones/agent --json
```

`--root` 按 `目录/name` 定位；`--map NAME=PATH` 可重复，用于单独覆盖路径。全部条目都有映射时可以省略 `--root`。本地分支名、remote 名可不同，但必须恰好有一个 fetch remote 匹配清单里的 GitHub 身份，检查的目标分支仍使用清单值。重复 name、重复或未知映射名、多个条目映射同一 clone 会报错。

复核 JSON 的 `kind` 为 `repo-workbench-delivery-recheck`，分别返回固定的 `expected_sha`、当前 `local_head`、`local_state`、`dirty`、`identity`、含远端 SHA 的 `publication`、固定 SHA 的 `expected_ci` 和本次 `verified`。**CI 始终查询清单里的 SHA**：新 HEAD 变绿不能替换旧 SHA 的证据，旧 SHA 变绿也不代表当前新 HEAD 已交付。

本次 `verified: true` 要求本地 HEAD 与清单一致、工作区干净、唯一匹配的远端身份与目标分支头一致、清单 SHA 的 CI 成功。仓库缺失、HEAD 改变、dirty、远端身份不符/歧义、远端头改变、CI 未知都单独展示。远端头不同仍只表示包含关系未知，不推断历史。

清单模式始终实时检查，不能混用扫描的 `--repo`、`--remote`、`--branch` 或 `--verify`。普通复核报告完成退出 `0`；`--check` 对整批未通过返回 `1`；无效清单、映射或参数组合返回 `2`。`--needs-attention` 仅过滤输出。原扫描的 `delivered` 语义不变。

## 开发

```sh
python3 -m unittest discover -s tests -v
python3 -m repo_workbench --help
```

测试使用真实临时 Git 仓库和本地 bare remote，覆盖缓存过期、分支分叉、多 remote、未提交变更和 SHA 精确匹配。GitHub 契约测试不需要网络与凭据。

工具仅观察，不执行 commit、push、fetch 或 clean，也不修改目标仓库。许可证：MIT。
