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

## 开发

```sh
python3 -m unittest discover -s tests -v
python3 -m repo_workbench --help
```

测试使用真实临时 Git 仓库和本地 bare remote，覆盖缓存过期、分支分叉、多 remote、未提交变更和 SHA 精确匹配。GitHub 契约测试不需要网络与凭据。

工具仅观察，不执行 commit、push、fetch 或 clean，也不修改目标仓库。许可证：MIT。
