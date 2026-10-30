# 示例任务（不会被执行）

`combat-0142.example.yaml` 是一个**已完成任务**的真实状态示例，用来说明字段长什么样、
`checkpoint.py` 自动采集了哪些信息、`finish.py` 会补什么。

它放在 skill 里而不是 `.agent-state/completed/` 里，因为它是文档，不是项目历史。
想在本机跑一遍完整接力演示，用：

```text
python .agents/skills/agent-handoff/scripts/simulate_handoff.py --keep
```

真实任务由 `checkpoint.py start` 生成，不需要手抄本文件。
