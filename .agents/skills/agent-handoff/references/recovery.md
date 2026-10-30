# 崩溃恢复（RECOVERY MODE）

适用：上一个 Agent 在任意时刻消失——额度用尽、Session 断开、电脑关机、进程被杀。
典型现象：它改了 8 个文件，但没来得及写 checkpoint。

恢复的目标不是"接着往下写"，而是**先把已发生的事搞清楚**，再决定接着写什么。

## 1. 系统怎么发现

`resume.py` 重算工作区指纹，并与 checkpoint 里记录的指纹比对：

| 信号 | 含义 |
| --- | --- |
| `new_since_checkpoint` | checkpoint 之后新增的脏文件 |
| `hash_drift` | checkpoint 记录过、但内容又变了的文件 |
| `vanished_since_checkpoint` | checkpoint 里的脏文件现在不脏了（提交了 / 被还原了 / 被删了） |
| `missing_claimed_files` | `changed_files` 声称改过、现在磁盘上不存在 |
| `head_drift` | HEAD 与 checkpoint head 不同 |
| 锁 stale | 心跳超过 TTL；这只是线索，接管前仍须确认原会话状态 |

命中前四类中任意一类 → `RECOVERY MODE`。此时 `resume.py` 退出码仍是 0（证据可用），但会打印检查清单。

## 2. 强制流程

### 第一步：只读调查（禁止写代码）

```text
resume.py --id <id>            # 看 MODE、证据、REQUIRED INVESTIGATION
inspect.py --id <id> --files   # 文件表：git 状态 / 是否偏离 checkpoint / 二进制 / 修改时间
git status --short
git --no-pager diff -- <changing paths>
git --no-pager log --oneline -n 10
```

对 `new_since_checkpoint` 与 `hash_drift` 里的**每个**文件：

1. 读 diff（或整文件，如果它是新增的）
2. 用一句话说明"这段改动在做什么"
3. 判断它属于：完成度提升 / 半成品 / 实验残留 / 与任务无关 / 破坏性改动

不要 `git stash`、不要 `git checkout --`、不要格式化、不要"顺手清理"。

### 第二步：与 goal / remaining 对照

- 逐条核对 `completed` 里声称完成的项：代码里真的存在吗？是完整实现还是空壳？
- 逐条核对 `remaining`：是不是已经做了？如果做了，说明 checkpoint 落后于代码。
- 如果 `remaining` 与代码现状不符，**以代码为准重写 remaining**，并在恢复 checkpoint 里说明。

### 第三步：跑安全验证

只跑只读或幂等的检查（编译、规则测试、静态检查）。不要跑会改文件、部署、迁移数据的命令。
把命令与结果记进恢复 checkpoint。

### 第四步：写恢复 checkpoint（不要跳过）

```text
checkpoint.py save --id <id> --kind recovery \
  --note "崩溃前完成第三段起手；hitbox 判定缺末端帧；8 个文件已提交前遗留改动" \
  --done "第三段攻击主体已实现（未验证）" \
  --todo "补末端帧判定" --todo "跑 CombatComboTest" \
  --next "先补 hitbox 末端帧，再跑 rules 检查"
```

这一步把"调查结论"变成正式状态，让下一个 Agent 不必再猜。日志里会留下 `checkpoint` 事件（kind=recovery）。

### 第五步：继续开发

```text
resume.py --id <id> --take        # 接管锁与 owner（stale 锁才能直接接管）
checkpoint.py save --done "..."   # 之后正常节奏
finish.py --commit "..."          # 收尾
```

## 3. 分类决策表

| 调查结论 | 动作 |
| --- | --- |
| 改动完整、可编译/可测 | 写恢复 checkpoint，直接进入验证与收尾 |
| 改动是半成品，但方向正确 | 保留，写 checkpoint 记录缺口，继续补 |
| 改动与 goal 无关（跑题/实验） | 不要删；写 checkpoint 记录"存在无关改动，待人类决定"，并在报告里点名 |
| 改动是破坏性/危险（删接口、改 main、动未知区域） | **停止**，不要继续写；完整报告 + 一个最小问题 |
| 改动互相矛盾（两份实现） | 停止；报告冲突点，让人类选保留哪份 |
| 无法确定意图 | 完成所有安全调查后停止；报告证据与最小问题 |

## 4. 绝对不要做

- `git reset --hard` / `git checkout -- .` / `git clean -fd` 来"回到干净状态"
- 删除、覆盖、重命名未知来源的未提交文件
- 直接 `git add -A && git commit` 把别人的半成品打包提交
- 修改 `main`/`master`
- 在锁仍 live 的情况下接管（除非人类明确确认并 `--force`）
- 把 checkpoint 的文字当成事实

## 5. 什么时候必须问人类

只有这些情况值得打断：

1. 有未提交改动，且无法判断是否与任务相关（尤其涉及禁止修改的区域）
2. 两份互相冲突的实现同时存在
3. checkpoint commit 不存在 / 历史被重写（`HISTORY DIVERGED`）
4. 目录里出现来源不明的大体量二进制资产（无法 diff）
5. 需要覆盖别人可能仍在进行的工作

问题要最小化，并附证据：

```text
combat-0142 的恢复被一处改动卡住：
- Source/Combat/ComboState.cpp 里有两套第三段实现（#if 0 分支 + 现行分支）
- 两者都未提交，checkpoint 没有记录
- 已完成的只读调查：diff、编译（两套分别都能过）、最近 5 条提交
需要你确认：保留哪一套？（我不能替你选，因为会丢弃另一份未提交代码）
```

## 6. 例子：完整恢复

```text
# Codex 在 checkpoint ck-0002 之后改了 3 个文件，然后额度用尽
$ resume.py
MODE  RECOVERY MODE
  - 3 file(s) edited after the checkpoint
REQUIRED INVESTIGATION
  1. Inspect the uncheckpointed changes first: ...ComboState.cpp, ...
  2. Run `git --no-pager diff -- ...` and read the result
  3. The checkpoint claims 4 completed item(s). Confirm each one in the actual code
...
$ inspect.py --id combat-0142 --files
$ git --no-pager diff -- Source/Combat/ComboState.cpp
# 发现：第三段已经写完，但没跑测试，remaining 里还写着"第三段攻击"
$ checkpoint.py save --kind recovery --note "第三段代码已完成，未验证；checkpoint 过期" \
    --done "第三段攻击实现（未验证）" --drop-todo "第三段" --next "运行 CombatComboTest"
$ finish.py --commit "combat: 三段轻攻击 + 输入缓存"
```

## 7. 边界情况

| 情况 | 处理 |
| --- | --- |
| 仓库还没有任何提交 | 无法建分支、无 commit 可比；任务 `branch` 为空，模式降级为 `CHECKPOINT STALE`。先做基线提交（决定 LFS 策略后） |
| 换机器 / 换克隆 | checkpoint 里的 commit 可能不存在 → `HISTORY DIVERGED`。先 `git fetch`，确认提交存在再继续 |
| 分支被删 | `resume` 报 `task branch ... does not exist`；用 `git branch <name> <head>` 从历史恢复，或改任务分支并记录 |
| 锁文件存在但任务已归档 | doctor 报 orphan lock；确认无人使用后手动删除，或 `resume` 忽略 |
| 有未提交改动但在被忽略目录（`work/` 等） | 不计入指纹；恢复时忽略 |
| 二进制资产被改 | 不能合并；确认来源，必要时回退到 checkpoint 记录的版本再重做 |
| 之前用过 `git stash` | stash 不在指纹里；`git stash list` 看一眼，报告人类，不要自动 pop |
