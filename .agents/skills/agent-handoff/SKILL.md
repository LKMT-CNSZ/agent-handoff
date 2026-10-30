---
name: agent-handoff
description: 在已安装 agent-handoff 的 Git 项目中开始、恢复、检查或交接长期任务；根据实际文件、Git 和检查点核对状态。普通的一次性修改不需要创建交接任务。
---

# agent-handoff

跨 Agent 的任务接力系统。任何兼容 Agent（DSH、Codex、Claude、未来的 Agent）都用同一套文件与命令接力。

## 0. 事实来源顺序（不可颠倒）

1. **真实代码 / 工作区文件**
2. **Git**：branch、HEAD、status、diff、log
3. **`.agent-state/` 里的 checkpoint 描述**

checkpoint 只是恢复索引，不是事实。任何"任务已完成"的声明，都要在代码里核对。
冲突时**相信代码**，并把差异报告给人类；不要按 checkpoint 描述直接覆盖代码。

## 1. 先读什么

1. 仓库根 `AGENTS.md`：项目长期规则（架构、禁止区域、测试要求）
2. 本文件
3. 需要细节时：`references/protocol.md`（协议与 Git 安全）、`references/task-schema.md`（状态字段）、`references/recovery.md`（崩溃恢复）
4. `.agent-state/active/*.yaml`：当前未完成任务

动态状态**只在** `.agent-state/`，不要写进 `AGENTS.md`。

## 2. 命令

脚本在本 skill 的 `scripts/` 下，标准库即可运行（Python 3.8+，无需 pip）。

```text
python .agents/skills/agent-handoff/scripts/checkpoint.py start  --id <area-NNNN> --title "..." --goal "..." [--check "name=command"]
python .agents/skills/agent-handoff/scripts/checkpoint.py save   [--done "..."] [--todo "..."] [--drop-todo PREFIX] [--next "..."] [--check "name=command"]
python .agents/skills/agent-handoff/scripts/resume.py            [--id ID] [--hint "words"] [--take] [--checkout]
python .agents/skills/agent-handoff/scripts/inspect.py           [--id ID] [--files] [--git]
python .agents/skills/agent-handoff/scripts/finish.py            [--id ID] [--commit "message"] [--keep-remaining]
python .agents/skills/agent-handoff/scripts/doctor.py            [--fix]
```

每个命令都有 `--json` 与 `--help`。全部命令都接受 `--agent NAME`；**必须传**，否则日志无法归因。

Windows 上如不能直接调用 Python，可使用项目内的启动器：

```text
scripts\Handoff.cmd resume --agent codex
scripts\Handoff.cmd doctor
scripts\Handoff.cmd tests
```

启动器只负责找到 Python（记录在 `.agent-state/local.json`，机器本地、不进 Git）并把参数转发给上面的 `.py`；
未显式给 `--agent` 时它会注入本机默认身份（默认为 `codex`）。`node scripts/handoff.mjs setup` 可修复安装，
`... uninstall` 移除本机配置与全局指令。

退出码：`0` 正常；`1` 有警告（doctor 专用：降级）；`2` 错误/无任务/状态损坏；`3` 被其他 Agent 的活锁阻塞；`4` 安全拒绝；`5` 验证失败。

## 3. 开始任务

```text
checkpoint.py start --id combat-0142 --title "实现轻攻击三段连击" \
  --goal "玩家三段轻攻击 Combo，含输入缓存与耐力消耗" \
  --scope Source/Combat --scope Source/Character \
  --check "rules=pwsh -NoProfile -File scripts/Test-CombatRules.ps1" \
  --remaining "第三段攻击" --next "先读 CombatRules.h 再写第三段"
```

它会：校验 id → 从当前分支创建 `feature/<id>`（无基线提交时拒绝建分支并记录）→ 采集 Git 快照 → 写 checkpoint `ck-0001` → 写锁 → 写日志。
**禁止**在 `main`/`master` 上开工；`start` 会拒绝把任务建在受保护分支上。

## 4. 阶段性 checkpoint（必须）

不要只在任务结束时写状态。长时间任务至少在这些节点写 checkpoint：

| 节点 | 建议 |
| --- | --- |
| 任务开始 | `ck-0001`（`start` 自动） |
| 架构 / 数据结构确定 | `save --done "ComboState 已建立"` |
| 核心逻辑完成 | `save --done "..." --next "..."` |
| 测试 / 验证完成 | `save --verification build=pass --verification tests=pass` |
| 交接或收尾 | `save --kind handoff --next "..."` 然后 `finish.py` |

Git 信息（branch、HEAD、status、diff、修改文件与内容哈希）由脚本自动采集，**不要手填**。
人只需要写意图：`--done`、`--todo`、`--next`、`--warning`、`--verification`、`--note`。

## 5. 恢复任务（最重要）

用户说"继续 Codex 没做完的任务"时：

1. 运行 `resume.py`（它自己会扫描 `.agent-state/active/`、按最近更新/关键词选任务、比对锁）
2. **读完 `resume.py` 输出的 MODE 与证据**，再读 `REQUIRED INVESTIGATION`
3. 按清单逐个读真实代码：`changed_files`、`new_since_checkpoint`、`hash_drift`、checkpoint 声称完成的文件
4. 确认无误后 `resume.py --take`（接管 owner + 锁），必要时 `--checkout`（要求工作区干净）
5. 继续开发；阶段完成再 `checkpoint.py save`。只有用户或项目约定授权提交时才运行 `finish.py --commit "..."`；否则保存交接 checkpoint 并释放锁。

模式含义：

| MODE | 含义 | 允许的动作 |
| --- | --- | --- |
| `CLEAN RESUME` | 工作区与 checkpoint 指纹一致 | 直接继续 |
| `CHECKPOINT STALE` | HEAD 前进或声明的文件缺失，工作区本身没变 | 先刷新 checkpoint，再继续 |
| `RECOVERY MODE` | **工作区里有 checkpoint 没记录的改动**（崩溃/中断） | 只读调查 → 写恢复 checkpoint → 继续 |
| `HISTORY DIVERGED` | checkpoint commit 不是 HEAD 祖先 / 分支被重写 | 停止，报告人类 |
| `BLOCKED` | 另一个 Agent 的锁仍活着 | 不要动，等待或让人类确认后 `--force` |
| `STATE CORRUPT` | 状态文件不合法 | 停止，报告，不要猜 |

**禁止**：只因为 checkpoint 写着"第三段攻击未完成"就直接开始写。上一个 Agent 可能已经写完，只是没来得及更新 checkpoint。

## 6. 收尾 / 交接

- 交给别的 Agent：`checkpoint.py save --kind handoff --next "下一步" --note "为什么交接"`，然后释放锁 `checkpoint.py release`。对方用 `resume.py --take` 接管。
- 结束任务：`finish.py --commit "..."`。它会跑声明的验证命令、要求 `remaining` 清空、把工作与交接状态分两次提交（工作提交 + `handoff: archive` 状态提交）、把状态移到 `completed/`、写日志、删锁。**不删除历史**。
- `finish` 会创建 Git 提交，必须先确认当前任务允许提交；它会拒绝已有暂存内容，避免包含或覆盖不属于本任务的改动。
- 如果确实要带着未完成项收尾：`--keep-remaining`（会把清单原样写进归档）。没有声明验证命令时必须 `--allow-unverified`。
- 放弃任务：`finish.py --fail --reason "..."` → `failed/`。
- 不要用聊天记录交接；唯一交接物是 `.agent-state/` + Git。

## 7. 硬性安全规则

- **NEVER** 直接改 `main` / `master`；任务用 `feature/<task_id>`。
- **NEVER** `git reset --hard`、`git clean -fd`、`git push --force`、`git checkout -- .`。
- **NEVER** 删除、覆盖、格式化来源不明的未提交修改；先调查，再报告。
- **NEVER** 同一个任务同时由两个 Agent 修改；先看 `.agent-state/locks/`，只有 stale 锁或人类明确同意才能接管。
- 锁仅靠 Agent 名称和机器区分持有者，同一机器上的多个 Codex 会话可能被视作同一个持有者；不要把它当成会话级互斥。写锁命令的 PID 退出也不能证明会话结束。TTL 过期不证明对方已停止，接管前核对。
- **NEVER** 把 `.agent-state/locks/` 提交进 Git（它描述某一台机器上的进程）。
- 二进制资产（`.uasset` `.umap` `.blend` `.fbx` `.psd` `.wav` …）**不可文本合并**：谁改谁负责，不要盲改、不要重导、不要"顺手重新生成"。见 `references/protocol.md`。
- Git 与 checkpoint 冲突、或无法判断上一个 Agent 的意图时：**停止危险操作**，先完成所有安全调查，再向人类提**一个**最小必要问题。

## 8. 环境自检

先跑 `doctor.py`。它检查 Git、分支、脏工作区、孤儿任务、stale 锁、checkpoint 引用的分支/commit 是否存在、重复任务、Skill 文件完整性、状态目录可写、二进制资产策略。
`--fix` 只创建缺失目录与默认配置，不做任何 Git 操作。

修改本 Skill 自身后跑回归：

```text
python .agents/skills/agent-handoff/scripts/run_tests.py      # 单元 + 场景测试
python .agents/skills/agent-handoff/scripts/simulate_handoff.py --keep
```

`simulate_handoff.py` 在 `work/` 下建临时仓库，真实跑一遍「建任务 → checkpoint → 崩溃 → RECOVERY → 接管 → 恢复 → 收尾」，失败会保留现场。它**不会**碰真实项目状态。

本机安装 / 修复：

```text
node scripts/handoff.mjs setup --agent codex     # 找 Python、写 .agent-state/local.json、追加 Codex 全局指令、验证
node scripts/handoff.mjs setup --no-global       # 只装本机 Python 路径，不碰 ~/.codex/AGENTS.md
node scripts/handoff.mjs uninstall               # 移除全局指令块与本机配置（skill 与状态保留）
```

`setup` 末尾会用 `codex debug prompt-input` 检查 Codex 是否真的能看到本 Skill（输出 `codex skill visible`）。
它是只读的模型输入渲染，不消耗模型调用。

## 9. 关于本 Skill 的边界

- 不绑定任何 Agent 或账号：状态在文件里，证据在 Git 里，命令是普通 Python。
- 不依赖网络、NAS、数据库、Docker、消息队列。
- 不做自动合并、自动 `reset`、自动删除：所有危险动作要么拒绝，要么由人类显式确认。
- 与项目工具链无关：验证命令由任务自己声明（PowerShell、bash、编译器均可）。
