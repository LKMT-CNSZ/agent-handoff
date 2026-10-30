# 协议：状态机、锁、Git 安全、并发

本文件是 `SKILL.md` 的细节补充。冲突时以 `SKILL.md` 的硬性规则为准。

## 1. 状态机

```text
                start
                  │
                  v
            active/in_progress ──save──┐
                  │  ^                 │
                  │  └─────────────────┘
        finish.py │                   checkpoint.py save --kind handoff → 仍 active，等别人接管
                  v
        completed/<id>.yaml        failed/<id>.yaml
```

- 一个任务在任一时刻只应存在于 `active/`、`completed/`、`failed/` 之一。
- `status` 字段在 `active/` 内允许：`in_progress`、`blocked`、`paused`。
- 归档用**移动**（同一份内容写到目标目录后删除源文件），不重写历史；`history/<id>.jsonl` 只追加。

## 2. 目录职责

```text
.agent-state/
├── config.yaml          项目级配置（锁 TTL、验证超时、分支前缀、二进制扩展名）
├── active/              未完成任务，一个任务一个 YAML
├── completed/           已完成，永久保留
├── failed/              已放弃，必须带 reason
├── history/
│   ├── <task_id>.jsonl  该任务的完整事件流（created/checkpoint/taken_over/finished/failed/...）
│   └── commands.jsonl   所有写入型命令的审计记录（谁、哪台机器、什么参数、结果）
└── locks/
    └── <task_id>.lock   运行期锁，机器本地，不提交
```

`active/` `completed/` `failed/` `history/` 应提交进 Git；`locks/` 不提交（见第 5 节）。

## 3. Checkpoint 语义

checkpoint = 一次"快照声明"，包含两类信息：

1. **Git 事实（自动采集，Agent 不手填）**
   `branch`、`head`、`dirty`、`changed_count`、`changed_files`、`file_hashes`（每条含 sha256/size/status code）
2. **Agent 意图（手写）**
   `goal`、`completed`、`remaining`、`next_action`、`warnings`、`verification`、`notes`

关键机制：`file_hashes` 是**工作区脏文件的指纹**。恢复时重新计算同样指纹：

- 完全一致 → 工作区就是 checkpoint 描述的那个状态（`CLEAN`）
- 有不一致 → 上一个 Agent 在 checkpoint 之后干过活（`RECOVERY`）

指纹有上限：最多 400 个文件、单文件 2 MB（大文件跳过并置 `hashes_capped: true`）。
当 `hashes_capped` 为真时，脚本退化为"数量 + 已有路径哈希"比对，并在报告里说明。

## 4. 恢复模式判定（resume.py）

判定顺序（先命中先返回）：

1. 任务状态不合法 → `STATE CORRUPT`
2. 存在**活锁**（非本 Agent）→ `BLOCKED`
3. checkpoint commit 在当前仓库不存在 → `HISTORY DIVERGED`
4. checkpoint head 不是 HEAD 祖先 → `HISTORY DIVERGED`
5. 工作区指纹与 checkpoint 不一致 → `RECOVERY`
6. HEAD 前进了但工作区一致 → `CHECKPOINT STALE`
7. 声明的 modified 文件消失 → `CHECKPOINT STALE`
8. 仓库没有基线提交 → `CHECKPOINT STALE`（降级）
9. 其余 → `CLEAN RESUME`

`CHECKPOINT STALE` 不代表代码有问题，只代表"checkpoint 落后于 reality"，继续工作前应刷新 checkpoint。

## 5. 锁协议

`.agent-state/locks/<task_id>.lock`：

```yaml
task_id: combat-0142
agent: codex          # --agent
machine: DESK-01      # hostname
pid: "48216"          # 写入时的进程号
started_at: 2026-05-01T08:00:00Z
heartbeat: 2026-05-01T10:20:00Z
branch: feature/combat-0142
```

刷新时机：`start`、`save`、`resume --take`、`finish`。

**stale 判定**（保守）：

- 心跳早于 `lock.ttl_minutes`（默认 90 分钟）→ stale
- PID 是写锁的短命命令进程，不代表 Agent 会话；PID 消失不意味着锁过期。
- TTL 是保守的交接线索，并不能证明另一个会话已经停止；接管前仍须核对。
- 以上都不满足 → live。别人要接管必须 `--force`，并在 `history/` 留下 `lock_forced` 事件。

不要删除别人的锁文件；`release` 只释放自己的锁。

## 6. Git 安全规则

允许的写操作（全部显式、可审计）：

- `git switch -c feature/<id>`（`start`，且仅在无重名分支时）
- `git switch <task branch>`（`resume --take --checkout`，且工作区干净、分支存在）
- `git add -- <明确路径>`（`finish --commit`，只加任务范围内路径 + `.agent-state/`）
- `git commit -m <消息>`（`finish --commit`）

**永不执行**：`reset --hard`、`clean`、`push --force`、`checkout -- .`、`branch -D`、删除未跟踪文件、重写历史。
只读命令统一带 `GIT_OPTIONAL_LOCKS=0`，避免两个 Agent 同时 `git status` 抢 index 锁。

冲突处理：

| 情况 | 处理 |
| --- | --- |
| checkpoint commit ≠ 当前 HEAD | `resume.py` 判定；祖先关系正常则只是 checkpoint 落后，否则 `HISTORY DIVERGED` → 停止并报告 |
| 工作区有未知修改 | 先 `inspect.py --files` + `git diff` 调查，写恢复 checkpoint，不覆盖 |
| 两个活跃任务同一分支 | doctor 报 `Branch conflict`；停止并行，拆分分支 |
| 两个任务 `write_scope` 重叠 | doctor 报 `Write scope overlap`；协调所有者或缩小范围 |
| 需要并行开发同一任务的子方向 | 用新 id：`combat-0142-ai`、`combat-0142-anim`，各自独立分支与锁 |

## 7. 二进制资产 / 大文件

`.uasset` `.umap` `.blend` `.fbx` `.glb` `.psd` `.exr` `.wav` 等**只能整体替换，不能文本合并**。

V0.1 的做法：

- `changed_files` 里出现二进制文件时，`resume`/`inspect` 明确提示；
- `finish --commit` 把这些文件按普通二进制提交（不要试图 diff 合并）；
- 同一二进制文件同一时刻只允许一个任务修改（`write_scope` + 锁）；
- `config.yaml` 的 `assets.strategy` 只是声明（默认 `git-lfs`），doctor 会检查是否有 `filter=lfs` 与超大文件，但**不自动配置 LFS**；
- 未来接 Perforce 时，替换"资产的存放与锁"实现即可，任务/checkpoint/恢复逻辑不变。

## 8. 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功（resume：证据可用，含 RECOVERY / CHECKPOINT STALE） |
| 1 | doctor：有警告，系统降级但可用 |
| 2 | 错误：无任务、状态损坏、历史分歧、参数错误 |
| 3 | 被活锁阻塞 |
| 4 | 安全拒绝（干净工作区要求、重名任务、受保护分支等） |
| 5 | 验证命令失败，任务保持 active |

## 9. 多 Agent / 多账号并行

- 每个任务独立 `feature/` 分支；不要两个 Agent 同时改同一分支。
- 分配任务前先看 `.agent-state/active/` 与 `locks/`，避免重复劳动。
- 交接 = `save --kind handoff` + `release`，接收方 `resume --take`。
- 账号无关：状态里只记 `agent` 名字与 `machine`，不记账号 token / session id。
- 换电脑：拉取仓库 + 读 `.agent-state/` 即可恢复；锁文件不会跟随 Git（新机器上视为无锁）。
