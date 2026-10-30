# agent-handoff

用于在 Codex、DSH 和其他 Agent 之间交接长期 Git 任务。以实际文件和 Git 状态为准，checkpoint 提供恢复线索。

## 安装到项目

将 `.agents/skills/agent-handoff/` 和 `scripts/` 下的两个启动文件复制到项目根目录。需要 Git、Python 3.8 或更新版本；Windows 启动器还需要 Node.js。具体用法见 [SKILL.md](.agents/skills/agent-handoff/SKILL.md)。

Windows 上运行 `node scripts/handoff.mjs setup --agent codex`，然后运行 `scripts\Handoff.cmd doctor --agent codex`。项目需要已有 Git 基线提交才能创建任务分支。按需将 `.agent-state/` 的项目状态纳入 Git，并忽略本机配置和锁文件。

工具不能替代对工作区、锁和测试结果的核对。二进制文件不可文本合并。
