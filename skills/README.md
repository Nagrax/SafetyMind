# SafetyMind Skills 文档

SafetyMind 启动时会从 `ECHOMIND_SKILLS_DIR` 读取 Skills，并在匹配用户请求时注入到对应 Agent 的 system prompt。Skills 适合维护安全生产处理规范、作业票证流程、设备工艺处置 SOP、应急响应边界、升级规则和禁止事项。

当前内置三类 Skills：

```text
skills/safety_coordination/SKILL.md      # 安全生产协调：接待、澄清、隐患上报、分流、应急升级
skills/equipment_process_safety/SKILL.md # 设备工艺安全：报警处置、泄漏、联锁、检维修和风险边界
skills/compliance_emergency/SKILL.md     # 安全合规应急：作业票、许可审批、危化品、事故上报和应急响应
```

## Skill 文件格式

推荐每个 Skill 使用独立目录，并将主文件命名为 `SKILL.md`：

```text
skills/<skill_name>/SKILL.md
```

文件顶部使用简单 front matter：

```markdown
---
name: 设备工艺安全处置规范
description: 适用于设备与工艺安全 Agent 的报警处置、泄漏和联锁处置规范
keywords: 报警,超压,泄漏,联锁,检维修,可燃,有毒
agents: technical
enabled: true
---
```

字段说明：

- `name`：Skill 展示名称，会出现在注入给模型的 prompt 中。
- `description`：简短说明，方便 `/skills` 接口排查。
- `keywords`：触发关键词，用户消息命中后才注入；多个关键词用英文逗号或中文逗号分隔均可。
- `agents`：适用 Agent，可填 `general`、`technical`、`billing`，多个值用逗号分隔。
- `enabled`：是否启用，支持 `true/false`。

## 编写要求

- 重要规则放在文档前半部分，因为过长内容会按 prompt 预算截断。
- 一类 Skill 只描述一类职责，不要把设备、合规、协调规则混在一个文件里。
- 必须包含"角色定位""处理流程""升级条件""禁止事项"等稳定章节。
- 对人员安全、介质泄漏、联锁、票证、事故上报等高风险事项必须写明禁止和升级条件。
- 对无法保证的事项使用保守措辞，例如"通常""预计""需核实后确认"。
- 对需要人工、应急指挥、上级审批处理的场景要明确写出升级条件。

## 热加载

修改 Skill 文件后，不需要重启服务，调用：

```bash
curl -X POST http://localhost:8000/skills/reload
```

查看加载结果和解析错误：

```bash
curl http://localhost:8000/skills
```
