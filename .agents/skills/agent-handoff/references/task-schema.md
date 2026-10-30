# 任务状态字段（task schema v1）

`.agent-state/active/<task_id>.yaml` 是任务状态的唯一正式位置。
由 `checkpoint.py` 写入；允许人类手改，但改完必须能被本 schema 解析（见第 6 节）。

## 1. 完整示例

```yaml
task_id: combat-0142
format_version: "1"
title: 实现轻攻击三段连击
goal: |-
  实现玩家三段轻攻击 Combo，含输入缓存与耐力消耗。
  第三段必须能在第二段命中后 0.25s 内接上。
owner: codex
status: in_progress
created_at: 2026-05-01T08:00:00Z
updated_at: 2026-05-01T10:20:00Z
branch: feature/combat-0142
base_branch: main
commit: a84f921
parent_task:
dependencies:
  - combat-0130
write_scope:
  - Source/WuxiaPrototype/Public/Combat
  - Source/WuxiaPrototype/Private/Combat
next_action: 实现第三段攻击并运行 CombatComboTest
completed:
  - ComboState 已建立
  - 第一段攻击完成
  - 第二段攻击完成
  - 输入缓存完成
remaining:
  - 第三段攻击
  - stamina 消耗
  - hitbox 测试
changed_files:
  - Source/WuxiaPrototype/Private/Combat/ComboState.cpp
  - Source/WuxiaPrototype/Private/Combat/WuxiaCombatComponent.cpp
file_hashes:
  - path: Source/WuxiaPrototype/Private/Combat/ComboState.cpp
    sha256: 6b1f...e2
    size: "4213"
    code: M
hashes_capped: "false"
verification:
  build: pass
  tests: partial
verification_commands:
  - name: rules
    command: pwsh -NoProfile -File scripts/Test-CombatRules.ps1
    required: "true"
  - name: workspace
    command: pwsh -NoProfile -File scripts/Validate-Workspace.ps1
    required: "false"
checkpoint:
  id: ck-0002
  at: 2026-05-01T10:20:00Z
  by: codex
  kind: progress
  count: "2"
  head: a84f9213c0ffee...
  dirty: "true"
  changed_count: "2"
  note: 第二段完成后
git_snapshot:
  branch: feature/combat-0142
  head: a84f9213c0ffee...
  head_short: a84f921
  dirty: "true"
  changed_count: "2"
  captured_at: 2026-05-01T10:20:00Z
warnings:
  - 不要修改 Source/WuxiaPrototype/Private/Character/WuxiaCharacter.cpp
notes: 动画占位，等美术
```

## 2. 字段说明

| 字段 | 谁写 | 说明 |
| --- | --- | --- |
| `task_id` | 人 | `[a-z0-9][a-z0-9._-]{1,63}`，全局唯一（active/completed/failed 三处都不允许重名） |
| `format_version` | 脚本 | schema 版本，当前 `"1"` |
| `title` | 人 | 一行标题 |
| `goal` | 人 | 目标，允许换行 |
| `owner` | 脚本 | 最近一次写入/接管者的 `--agent` |
| `status` | 脚本 | `in_progress` / `blocked` / `paused`；归档后 `completed` / `failed` |
| `created_at` / `updated_at` | 脚本 | UTC ISO8601，秒精度 |
| `branch` | 脚本 | 任务分支；无基线提交时为 null |
| `base_branch` | 脚本 | 建立分支时的起点 |
| `commit` | 脚本 | 归档时的最终提交；进行中通常为空 |
| `parent_task` | 人 | 可选，父任务 id（子方向拆分时用） |
| `dependencies` | 人 | 可选，前置任务 id 列表（仅记录，不自动阻塞） |
| `write_scope` | 人 | 建议写作用域（目录前缀），用于冲突检查 |
| `next_action` | 人或 Agent | 下一步一句话；恢复时最重要的意图字段 |
| `completed` / `remaining` | Agent | 进度清单；`--done`/`--todo`/`--drop-todo` 维护 |
| `changed_files` | 脚本 | 本次 checkpoint 时所有脏路径（相对仓库根、POSIX 分隔） |
| `file_hashes` | 脚本 | 指纹：`path` / `sha256` / `size` / `code`；用于检测 checkpoint 之后的改动 |
| `hashes_capped` | 脚本 | `"true"` 表示指纹被截断（>400 个或 >2MB 文件被跳过） |
| `verification` | 人或脚本 | 键值事实，如 `build: pass`、`tests: fail` |
| `verification_commands` | 人 | 可执行验证：`name` / `command` / `required` |
| `checkpoint` | 脚本 | 最近一次 checkpoint 的元数据 |
| `git_snapshot` | 脚本 | 写入瞬间的 Git 事实 |
| `warnings` | 人或 Agent | 禁止区域、已知坑、需要人类注意的事 |
| `notes` | 人或 Agent | 备注；`--kind recovery` 时记录恢复结论 |

未知字段会被保留（写回时排在末尾），方便未来扩展或人工附加信息。

## 3. 校验规则

`checkpoint.py` / `resume.py` / `doctor.py` 会检查：

- `task_id` 格式与唯一性
- `title`、`goal`、`owner`、`created_at`、`updated_at` 非空
- `status` 属于合法集合
- `commit` 若非空必须是 7–40 位十六进制
- `branch` 不含空格、不以 `-` 开头
- `verification_commands` 每项必须有 `name` 与 `command`
- `changed_files` 不允许绝对路径或 `..` 开头

不合法 → `resume`/`doctor` 报 `STATE CORRUPT` / `FAIL`，**不要**猜测修复；保留原文件并报告。

## 4. checkpoint 编号

`ck-0001` 起，单调递增，存于 `checkpoint.count`。不要在同一个任务里重置。
`checkpoint.kind` 取值：`baseline`（start）、`progress`、`recovery`、`handoff`。

## 5. 验证命令

`verification_commands` 由项目自己声明，脚本不假设任何构建系统：

```yaml
verification_commands:
  - name: rules
    command: pwsh -NoProfile -File scripts/Test-CombatRules.ps1
    required: "true"
```

- `required: "true"`：`finish.py` 默认执行；失败则退出码 5，任务保持 active。
- `required: "false"`：默认不执行，`--include-optional` 时才跑。
- 命令通过当前平台 shell 执行（Windows `cmd.exe`，其他 `/bin/sh`），工作目录 = 项目根。
- 没声明任何命令就 `finish` 时，必须显式 `--allow-unverified`，并在状态里记为 `unverified`。

## 6. YAML 子集限制（重要）

状态文件由内置解析器读取（不依赖 PyYAML）。它支持：

- 块映射、任意嵌套、块序列、序列中的映射项
- `key: value`、引号字符串、`|` / `|-` 多行块、`>` 折叠
- 流式列表 `[a, b]`、空 `[]` / `{}`
- `#` 注释、空行、`---` / `...`

不支持：锚点/别名/标签、多文档、制表符缩进、序列里嵌套多行标量。
加载时**所有标量都是字符串**（`true` 不会变成布尔值），因此字段使用 `"true"` 而不是 `true` 也能正常比较。

手改建议：保持两空格缩进，字符串里出现 `: `、` #` 或以 `-`/`[`/`{` 开头时加引号。

## 7. 归档后的状态

`completed/<id>.yaml` / `failed/<id>.yaml` 额外包含：

- `finished_at`、`finished_by`
- `commit`：最终提交
- `verification`：验证结果汇总（`pass` / `fail` / `timeout` / `unverified`）
- `verification_runs`：每条命令的 `name/command/status/exit_code/at/required`
- `failure_reason`（仅 failed）

归档文件不再被修改；后续同类工作请开新任务并 `--parent` 指向它。
