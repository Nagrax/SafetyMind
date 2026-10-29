# 安全资源库（Resource Library）设计与集成说明

> 来源：对「安全工程师的百宝箱」小程序的逆向分析（`D:/claudecode/miniprogram-re/RE_REPORT.md`）。
> 该小程序的价值不在技术实现（微信云开发 CRUD），而在于**它把安全岗位真实工作会用到的文件整理成了体系**。
> 本文回答三个问题：集成了什么、怎么用、以及为什么这不是重复造轮子。

## 1. 集成了什么

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| 结构化资源库 | `mcp/resource_library.py` | MSDS 化学品库（GB/T 16483 的 16 段结构）+ 资料目录 + 台账支撑文件映射，JSON 持久化、线程安全、损坏自动归档重建 |
| MCP 工具 ×3 | `api/main.py` 注册 | `msds_lookup`（按名称/别名/CAS/章节查）、`resource_search`（资料目录检索）、`ledger_checklist`（台账→支撑文件清单→标缺失） |
| Skill ×2 | `skills/chemical_msds_guide/`、`skills/safety_ledger_assistant/` | 关键词触发注入，约束 Agent 先查工具再作答、保守措辞、升级条件 |
| 种子数据 | `mcp/msds_seed.json`、`mcp/resources_seed.json`、`mcp/ledger_bundles.json` | 5 种常见化学品骨架信息（公开事实，注明以厂商 SDS 为准）、13 条公开标准/模板目录元数据、9 类常见台账→支撑文件映射 |
| 批量导入 | `scripts/import_resources.py` | CSV/JSON → MSDS/资料，upsert/fill 两种模式 + CAS 去重（借鉴原小程序 saveMsdsBatch/dedupeMsds 流程，本地实现） |
| 工作流管线 | `scripts/run_resource_pipeline.py` | 一条命令：种子校验→收件箱导入→去重→台账覆盖率自检→Skill/工具接线自检→单元测试→报告落盘 |
| 测试 | `tests/test_resource_library.py` | 39 项断言：导入/去重/查询/热门/清单/降级/工具管理器全链路接线 |

与现有工作流的关系：**不新增 Agent**。资源库作为 3 个工具挂进既有 `MCPToolManager`（带熔断/缓存/降级），两个 Skill 走既有 SkillManager 关键词注入，由既有意图路由（coordination/compliance/technical）分发。

## 2. 面向两个真实痛点的工作流

### 痛点 2：台账支撑文件难找 → `ledger_checklist`

用户问"危化品台账需要哪些支撑文件？"，Agent 流程：

```
ledger_checklist(action=checklist, ledger="危化品台账")
  → 逐项支撑文件（每项带用途说明）
  → 自动在资源目录检索已有条目 → matched（告诉用户去哪拿）
  → 没有的标 missing → 明确告诉管理员"还缺这几件"
```

覆盖 9 类台账：培训教育 / 隐患排查治理 / 危险化学品 / 应急预案与演练 / 特种设备 / 作业许可 / 相关方 / 职业健康 / 安全投入。映射表在 `mcp/ledger_bundles.json`，管理员可自行补充台账类型。补齐文件走 `/knowledge/upload`（进 RAG）+ `scripts/import_resources.py resource`（进目录），清单自动转"已齐"。管线报告按台账输出覆盖率表，就是给管理员的**缺件待办清单**。

### 痛点 1：如何让领导支持安全、加大投入 → `safety_ledger_assistant` 场景二

固定汇报提纲结构，每一步都有工具供给事实：

1. **风险现状**：`knowledge_search` 取本企业隐患记录/整改单/检测报告（讲自己的问题，不是讲道理）；
2. **法规责任**：`resource_search(category="standards")` 取《安全生产法》主体责任、处罚幅度、《刑法》重大责任事故罪等（措辞保守、注明以最新文本为准）；
3. **事故教训**：`resource_search(category="videos")` 取同行业公开案例（血的教训是领导的语言）；
4. **投入方案与代价对比**：罚款幅度 vs 整改成本，以"建议"收尾，不替领导做决策。

升级条件：已发生事故/面临处罚时切换事故上报口径；法定必备项（特种设备检验、预案备案）缺失时标记为最高优先级。

## 3. 为什么这不是重复造轮子（与"普通 Agent + 本地法规文件"的差异）

普通 Agent（如 WorkBuddy）+ 一堆 PDF 的工作流是**被动问答**：文件名靠人记得、内容靠语义检索碰、答完没有留痕、没有对错度量。差异化在四层：

1. **工作流化 vs 问答化**：台账场景的输出不是"一段回答"，而是**结构化清单 + 缺件状态**——可执行、可勾销、可跟踪闭环。这是数据模型（台账→支撑文件映射）带来的，堆文件堆不出来。
2. **精确结构化检索 vs 全文语义召回**：MSDS 按 16 段结构存（section=6 只取泄漏应急段）、按 CAS/别名精确命中。语义检索对"烧碱的闪点"这类**事实型查询**恰恰是弱项（召回整篇 PDF、答非所问）。两类检索互补：结构化库答事实，RAG 答制度与边界。
3. **工作真实文件 vs 泛语料**：种子目录按台账体系组织（GB/T 33000 要素展开），每一类资源都能落到"哪个台账、哪个环节用"；导入口的 fill 模式 + CAS 去重保证增量补文件不污染已有数据。这是"把领域文件集成在一起"的工程化表达——原小程序验证过的需求，我们接住了。
4. **可评测可观测**：每次工具调用走熔断/缓存/监控（既有 Monitor 面），MSDS 查询热度自动沉淀（借鉴原小程序 msds_hot），管线自检 + 39 项测试守门。普通 Agent 接文件做不到这些度量。

一句话：**不做通用问答的第二个轮子，做通用 Agent 做不了的那一层——领域结构化数据 + 工作流化交付 + 可度量。**

## 4. 管理员操作速查

```bash
# 导入化学品（宽表 CSV：name,cas,1,2,...,16 或长表：name,cas,section,section_content）
python scripts/import_resources.py msds data/xxx.csv --mode fill   # fill=只补缺失
# 导入资料条目（CSV：title,category,description,tags,file_or_url）
python scripts/import_resources.py resource data/xxx.csv
# 完整管线（含收件箱 data/resource_library/inbox/ 自动路由导入）
python scripts/run_resource_pipeline.py
# 查看库存与台账覆盖率报告
type data\resource_library\pipeline_report.md
```

`data/resource_library/seed_fragments/` 是工作流（`resource-library-pipeline`）的种子暂存目录：每类缺件台账一个 fragment 文件，经导入脚本入库后由工作流的汇编步骤把全部 fragment 追加同步进 `mcp/resources_seed.json`（按 title 去重）。手工增补条目时直接改 seed 文件即可，不要手改 fragment。

Skill 改动后热加载：`curl -X POST http://localhost:8000/skills/reload`。

## 5. 边界与限制

- 化学品种子为**骨架信息**（公开常识性内容），正式用途必须以厂商 SDS 为准——Skill 层已强制该限定语。
- 台账清单是常见实践总结，不构成合规结论，以当地监管要求与企业制度为准。
- 资源目录种子只含**公开标准的题名/编号与模板元数据**，不含受版权保护的文件本体；文件由企业自行上传，存本地。
- 原小程序的云端数据（其数据库内容）未获取、也不应获取；本模块的初始数据全部为公开事实信息与自建映射。
