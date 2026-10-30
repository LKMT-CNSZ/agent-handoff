# DSH Handoff

让 DSH、Codex 和其他 Agent 在同一个 Git 项目里接着干活。

任务进度保存在项目的 `.agent-state/` 中，包含检查点、待办、验证结果和操作记录。接手时，工具会对照 Git 和工作区检查改动、分支与锁，帮助后来者找到需要核对的地方。实际文件和 Git 状态始终比检查点描述更可靠。

## 适合什么情况

- 同一个项目需要由不同 Agent 或不同账号轮流维护。
- 任务中断后，需要找回尚未提交的改动和下一步工作。
- 希望把交接信息跟着项目走，而不是留在某次聊天里。

工具不会自动合并代码、清空工作区或替你判断冲突。二进制资产仍需要人工协调，避免多人同时修改。

## 安装

需要 Git 和 Python 3.8 或更新版本。Windows 启动器还需要 Node.js。

1. 将本仓库的 `.agents/skills/agent-handoff/` 和 `scripts/` 下的两个文件复制到目标项目的对应位置。
2. 在目标项目准备好 Git 基线提交，并在 `AGENTS.md` 中写明项目自己的协作和验证要求。
3. Windows 上在项目根目录运行：

   ```bat
   node scripts/handoff.mjs setup --agent codex
   scripts\Handoff.cmd doctor --agent codex
   ```

   `setup` 会查找 Python、记录本机配置，并配置 Codex 的全局提示。也可以用 `--no-global` 跳过全局配置。
4. 将需要共享的 `.agent-state/` 任务状态纳入 Git；不要提交 `.agent-state/local.json`、`python-path.txt` 和 `locks/` 中的锁文件。

其他环境可直接使用 skill 目录下的 Python 脚本。完整协议、命令和恢复方式见 [SKILL.md](.agents/skills/agent-handoff/SKILL.md)。

## 日常使用

在项目根目录启动或继续任务。例如，Windows 用户可以通过 `scripts\Handoff.cmd` 运行 `start`、`save`、`resume`、`finish` 和 `inspect`。开始前先检查锁和工作区；恢复时按 `resume` 输出核对实际文件，再决定是否接管。每个阶段记录检查点，收尾时运行项目要求的验证。

工具提供单元测试和交接模拟：

```bat
scripts\Handoff.cmd tests
scripts\Handoff.cmd simulate
```

模拟会在项目的 `work/` 目录创建临时仓库，不修改真实项目的任务状态。
