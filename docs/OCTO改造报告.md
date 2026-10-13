# SafetyMind OCTO 改造报告

## 总览

**一句话结论**：OCTO 改造四个阶段（Loop 工单回路、员工/专家解耦、偏好卡片、前端可视化）均已完成施工核验与自证并全绿，统一门禁 6/6 通过，意图基准 smoke 持平基线（acc=0.9293），收尾修复为「无收尾修复」；全部改动留在工作区，未做任何 git commit（阶段一记录 HEAD 仍为 65b38ad）。

**总门禁终态（6/6 全 PASS）**

| 门禁件 | 终态 |
| --- | --- |
| test_bge_proto | exit=0 [PASS] |
| test_laya_fusion | exit=0 [PASS] |
| test_adversarial | exit=0 [PASS] |
| test_frontend_endpoints | exit=0 [PASS] |
| test_audit_tickets | exit=0 [PASS] |
| test_preference_cards | exit=0 [PASS] |

**意图基准 smoke**：test2 单模 acc=0.9293（基线 0.9293，持平无回退）。

**收尾修复记录**：无收尾修复。

---

## 各阶段

### 阶段一·Loop 工单回路

**目标**：按定稿设计完成工单五阶段回路的施工核验与自证。进入本窗口时，三个目标文件已在工作区携带完整实现（git status: M core/audit_store.py / M api/main.py / M tests/test_audit_tickets.py，HEAD 仍为 65b38ad，未做任何 commit），逐行核对实现与定稿设计一致、无需补改。实施要点：

- `core/audit_store.py`：`_migrate_locked()`（core/audit_store.py:122-152）PRAGMA table_info 探测后非破坏 ALTER ADD COLUMN 六列（stage/intent/urgency/entities/message_digest/routing_reason）并按旧两态回填留痕（open→execute / closed→accept + legacy_stage 时间线，失败仅 warning 回滚）；ticket_timeline 表 + idx_ttimeline_ticket（core/audit_store.py:67-81）；TICKET_STAGES/STAGE_LABELS 与 _STAGE_TRANSITIONS 白名单（core/audit_store.py:85-98，顺序前进 + accept/reject 可回退 feedback）；create_ticket(..., brief=None, actor='system')（core/audit_store.py:233-267）简报六字段与工单行同落库、时间线首条 create 事件 detail=完整简报 JSON；transition_ticket（core/audit_store.py:277-317）持线程锁原子校验，返回 ok/kind=not_found|bad_stage|bad_transition/error/from_stage/to_stage/ticket，进 accept/reject 置 closed+closed_ts、回中间阶段恢复 open 并清 closed_ts；get_ticket（core/audit_store.py:272）、get_ticket_timeline（core/audit_store.py:334-350）、record_ticket_event（core/audit_store.py:319-332）；close_ticket 行为不回退、stage=accept + legacy_close 留痕（core/audit_store.py:363-380）；_ticket_dict 统一 stage 归一/label/entities 解析（core/audit_store.py:158-171）。
- `api/main.py`：_chat_core 升级分支构造简报，urgency 用可读枚举名（urgency.name.lower()→critical，api/main.py:862-863），audit_log 仍存原 urgency.value（api/main.py:849）；webhook 仍在建单时发出、文本格式与 60 字摘要不变（api/main.py:872-893），成功后 record_ticket_event('notify_webhook') 补时间线，全程包在原 try/except（api/main.py:844-895）降级不阻塞；新增 require_admin 保护的 POST /tickets/{id}/transition（api/main.py:526-538，200/400/404 经 kind 区分）与 GET /tickets/{id}/timeline（api/main.py:541-550，404 不存在）；/api/python 前缀别名为全路由自动挂载（api/main.py:1251-1253），新端点自动继承。
- `tests/test_audit_tickets.py`：35 条断言覆盖设计全部要求（五阶段顺序流转、accept/reject 回退再闭环、非法跳转/未知阶段/同阶段/不存在拒绝、时间线顺序+批注+时间戳+简报详情、测试内按旧 DDL 手工建两态库验证迁移后可续转可重做、E2E 火警简报建单 + 8972 端口本地 HTTP 收端建单时 webhook 实收 + transition 端点四态 + 旧 close 兼容映射 accept）。
- 对外契约经核对未动：ChatResponse 全字段（api/main.py:897）、既有 /tickets 与 /tickets/{id}/close 响应形状（close 仍返回 {closed: bool}，api/main.py:514）、orchestrator.run() 直调（api/main.py:825）、stats() 未改（core/audit_store.py:383-406）。

**改动文件**：`core/audit_store.py`、`api/main.py`、`tests/test_audit_tickets.py`。

**测试**：

- `python tests/test_audit_tickets.py`（解释器 D:/claudecode/SafetyMind/.venv/Scripts/python.exe，仓库根目录，HF_HOME=D:/hf_cache）→ 首轮 35/35 PASSED；第二轮逐条核对 35 条 [PASS] 无 [FAIL]；第三轮单独取脚本退出码 PY_EXIT=0（脚本末尾 sys.exit(1 if failed else 0)）。命令实测共跑 3 次全绿。
- 未运行：门禁四件套（test_adversarial/test_frontend_endpoints/test_laya_fusion/test_bge_proto）与意图基准——按本工单指示「只跑新增/修改的测试自证，不跑全量门禁」，由脚本统一验证（终态见总览：test_audit_tickets exit=0 [PASS]）。

**遗留问题**：

1. 存量库首次打开自动执行加列+回填迁移：非破坏（只加列/回填新增列/留痕），但若 sqlite3.Error 则 rollback 并仅 warning，系统沿用两态读取（时间线可能为空），日志关键字「工单五阶段迁移失败」。
2. notify_webhook 时间线事件在后台通知成功后补写，进程在建单后即刻退出可能缺该事件（通知本身已发出，mark_notified 不受影响）。
3. accept/reject→feedback 回退会把 closed 工单重开（closed_ts 清空），安环部周报闭环时长/闭环数统计随之少算该单，需在周报口径说明中知会。
4. 旧 /tickets/{id}/close 仍可从 brief 等早期阶段直接闭环（映射 accept），宽松于新白名单——为兼容保留，业务要求严格化需后续把 close 改走 transition 通道（会破坏现网行为，未做）。
5. 回滚点：三文件改动相互独立可逆；已迁移 DB 多出的列/表对旧代码只读不冲突，但直接 SELECT * 的下游会多出列需注意。
6. 工作区另有其他工作流的未跟踪文件（mcp/resource_library.py、docs/RESOURCE_LIBRARY.md 等），本次未触碰。

### 阶段二·员工/专家解耦

**目标**：OCTO「员工编排者/无状态专家」等价重构。施工时发现上一工作流已把完整施工留在工作区（agents/agent_orchestrator.py 恰为定稿的 844 行、git diff +223/-107，tests/test_employee_agent.py 恰 384 行），逐行核对确认与定稿设计一致，未重复改写。实施要点：

- ExpertTask 任务包（agents/agent_orchestrator.py:114）+ EmployeeAgent 员工层（agents/agent_orchestrator.py:270）：_memory_block 优先结构化 MemoryContext.to_prompt_text()、渲染失败 try/except 降级回 req.context 并 warning（agents/agent_orchestrator.py:294-302）；assemble_task 一次装配 [背景信息]/[结构化实体]/用户消息（agents/agent_orchestrator.py:304-334）；CRITICAL 模板短路先于澄清→_route_decision/_domain_scores/_needs_clarification→单/并行派发→升级检查全部收入员工层；handle(req, t0) 接收门面 t0 保 latency_ms 口径（agents/agent_orchestrator.py:369-375）。
- BaseAgent 退化为无状态执行者：handle(task) 保留计时/统计/升级检查（agents/agent_orchestrator.py:177），_call_llm 直接发 task.messages 不再自拼背景（agents/agent_orchestrator.py:204-212），_build_system_prompt 仍按专家类型注入动态 Skills（agents/agent_orchestrator.py:214）；四领域专家类与 AgentStats/AgentResponse/RoutingDecision 未动。
- AgentOrchestrator 薄门面：run()=意图补全（仅 req.intent 为 None 时）+委托（agents/agent_orchestrator.py:796-812），run_parallel/recognize_intent/set_skill_manager/get_stats/update_routing_penalties 签名不变，员工层与门面共享同一专家池（agents/agent_orchestrator.py:771-777）；LLM 客户端带 core/llm_utils.llm_client_kwargs()（agents/agent_orchestrator.py:765）。
- api/main.py 接线仅一处：_chat_core 的 OrcReq 增传 memory_context=mem_ctx、knowledge=knowledge_text（api/main.py:815-817，两变量分别在 api/main.py:716、api/main.py:802-805 定义），context=full_context 保留为评测/CLI 兜底。
- 死代码 _route/_INTENT_ROUTING/_collaboration_targets 按等价重构纪律原样随迁未删（agents/agent_orchestrator.py:486-695）。
- 对外契约保持：OrchestratorResult 11 字段原样（tests/test_employee_agent.py 断言通过）、ChatResponse 未触碰、既有 API 路由不变、evaluation/evaluator.py 直调 orchestrator.run() 的字符串 context 兜底链路实测可用（T1/T5/T12 走的正是该路径）。无 git commit；测试均以 HF_HOME=D:/hf_cache 运行，未向 C 盘写文件。

**改动文件**：`agents/agent_orchestrator.py`、`api/main.py`、`tests/test_employee_agent.py`。

**测试**：

- `HF_HOME=D:/hf_cache ./.venv/Scripts/python.exe tests/test_employee_agent.py` → 14/14 PASSED（装配逐字节等价对拍 legacy_messages 黄金参照、记忆渲染失败降级、CRITICAL 先于澄清且专家零调用、澄清路径、单专家契约、升级关键词、失败降级 General、并行块格式与任务包共享、专家不注入背景（FakeClient 捕获 system/messages）、惩罚回路选优、OrchestratorResult 11 字段契约、run() 意图补全）。
- `HF_HOME=D:/hf_cache ./.venv/Scripts/python.exe tests/test_adversarial.py` → 5/5 PASSED（指令点名的最小自测门禁件；T1 死 LLM 下 CRITICAL 先于澄清在重构后代码复验，T5 死端点回退经新员工层）。
- `python -c "import evaluation.evaluator; from agents.agent_orchestrator import ..."` → import ok，Request.memory_context 默认 None、knowledge 默认 ''（向后兼容）。
- `git diff --stat -- evaluation/ core/intent_recognizer.py core/laya_recognizer.py core/intent_bge.py` → 输出为空（零改动），评测器直调契约不受影响。

**遗留问题**：

1. 本次未跑全量门禁（按指令不跑）：tests/test_frontend_endpoints.py、tests/test_audit_tickets.py、tests/test_bge_proto.py、tests/test_laya_fusion.py 及意图基准评测——后两件属门禁四件套，但意图层文件（core/intent_recognizer.py 等）经 git diff 确认零改动，建议夜窗例行评测复核意图基线。
2. 工作区另有上一工作流的资源库改动（core/audit_store.py、tests/test_audit_tickets.py、mcp/、scripts/、skills/ 等未跟踪文件），非本次施工范围，未触碰。
3. _route 与 _collaboration_targets 重构前即为无调用点死代码，本次原样随迁未删除（等价重构纪律），后续可另行清理。
4. 语义口径说明：Request 同时携带 memory_context 与不一致的 context 时结构化优先（EmployeeAgent._memory_block）；当前唯一调用方 api 层两路渲染内容一致，未来第三方直调需知晓该优先级。
5. 回滚点：git checkout agents/agent_orchestrator.py + 还原 api/main.py 中 OrcReq 增传的 hunk 即可整体回退；无 schema/数据变更。

### 阶段三·偏好卡片

**目标**：偏好卡片误抽防护定稿修订的核验+自证；本 session 对代码零改动（定稿修订已由上轮全部落地在工作区，逐行核验在位）：

- `memory/preference_cards.py`：_STANDARD_PREFIXES（memory/preference_cards.py:58-61）排除 GB/API 等 23 个标准前缀（无分隔符位号受限，R-101 式带分隔符不受限）；_GENERIC_TERMS（memory/preference_cards.py:93-100）+ 最短 2 字 _valid_term（memory/preference_cards.py:105-107）+ 称呼动词 _NAMING_VERB_RE（memory/preference_cards.py:102）三道闸置于 extract_taboo（memory/preference_cards.py:115-155），句式循环对无效术语 continue 试下一句式（memory/preference_cards.py:137-138）；extract_devices 标准号分支（memory/preference_cards.py:176-180）。
- `memory/conversation_memory.py:79`：注释与 rank_for(INJECT_LIMIT) 实现一致。
- `tests/test_skill_evolution.py:21-24`：PREFERENCE_DB_PATH 指向 tempfile.mkdtemp 并注明防污染理由。
- `tests/test_preference_cards.py:119-137`：四条误抽回归断言。
- api/main.py:368 user_id 默认 anonymous 原文核验。

**改动文件**（均为上轮修订、本 session 逐行核验在位，未再改动）：`memory/preference_cards.py`、`memory/conversation_memory.py`、`tests/test_skill_evolution.py`、`tests/test_preference_cards.py`。

**测试**：

- 自证（本 session 唯一实跑，即 ask 指定的检查）：`cd D:/claudecode/SafetyMind && .venv/Scripts/python.exe tests/test_preference_cards.py` → 32/32 PASSED，TEST_EXIT=0；四条误抽回归断言全 PASS：修辞句不成禁忌卡（安全生产不是负担，而是责任）/无术语纠正不成卡（别再叫错了）/标准号排除且真位号保留（GB50160+API2023→[]，R-101/P102/Q-1000 全保留）/真纠正句仍成卡（液碱/火碱）。
- 生产库零污染前后夹逼：测试前 `ls D:/claudecode/SafetyMind/data/ | grep -i pref` → 无匹配（GREP_EXIT=1）；测试后同命令 → 无匹配（GREP_EXIT=1）；日志确认落库路径为 %TEMP% 临时库（pref_cards_tcb56c5o）。
- 本 session 未新增/修改任何源码或测试；未跑全量门禁（按 ask 指令）；无 git 提交。

**遗留问题**：

1. 误抽防护为黑/白名单式，天然滞后于语料：新修辞句式或新标准前缀（地方标准 DB、企标 Q/ 等）仍可能漏网，词表集中在 memory/preference_cards.py:58-61/93-100 可一行扩充；MIN_HITS=2 门槛仍是最外层防线。
2. 称呼动词共现要求会漏掉「液碱不是火碱，是危化品」式无动词纠正——符合宁漏不误取舍，LLM 画像路径（absorb_profile）可补位。
3. tests/test_preference_cards.py 结尾 shutil.rmtree(TMP, ignore_errors=True) 无法删除仍被 Chroma 客户端占用的 4 个 chroma_* 子目录，本轮在 %TEMP% 留下 pref_cards_tcb56c5o 残留（连同此前多轮共 12 个同类目录），仅临时目录卫生问题不影响功能；另外测试临时库按仓库既有 tempfile.mkdtemp 模式落在 OS Temp（C 盘 Temp），铁律 7 的 D 盘约束针对下载/模型缓存（本轮 SAFETYMIND_BGE=0+ngram 嵌入，零下载零缓存写入）。
4. t6 全 lifespan 绑定真实 .env 的 Prometheus :9091，与桌面端同机同跑可能端口冲突（测试头注已声明，沿用 test_frontend_endpoints 既有模式，错峰运行即可）。
5. 全量门禁未跑（按 ask 指令只跑该文件）；test_frontend_endpoints 等全 lifespan 测试仍未设 PREFERENCE_DB_PATH，会在真实 data/ 幂等初始化零行空库（建表无数据，与「写入达注入门槛」性质不同），如需彻底归零可补一行环境变量，本次未动以控制改动面。

### 阶段四·前端可视化

**目标**：按定稿「移除」分支完成施工与自证——移除默认用户界面的 Agent 路由信息（铁律 2 合规），保留 Loop 工单进度卡与 ?dev=1 面板，并强制重建 dist。本会话核查时工作区已含定稿要求的全部源码改动（上一轮遗留），逐项核对无偏差后按工作规则强制重建 dist 并跑最小测试自证。实施要点：

- `frontend/src/App.vue`：.m-msg-meta 模板块与 AGENT_LABELS/agentLabel()/supportLabels() 三成员 grep 0 处残留；primaryAgent/supportingAgents 数据字段保留（App.vue:635-636，注释「Agent 路由字段随消息留存：供 dev 面板/排查使用，默认用户界面不渲染（铁律 2）」）；?dev=1 面板「主 Agent」（App.vue:299）未动；Loop 工单进度卡原样保留（App.vue:71-95，escalated+requestId 条件渲染，含五阶段 loop-step 步进器与最新批注）。
- `frontend/src/styles.css`：:1205 死类 `.m-msg-meta { display: none; }` 逐字在位；≥980px 媒体查询（styles.css:1275-1277）仅含 `.loop-card { max-width: 380px; }`，无 .m-msg-meta 行。
- `frontend/dist` 双式重建：npm run build 503ms + node frontend/node_modules/vite/bin/vite.js build frontend 526ms，两式产物 hash 完全一致且与上一轮相同（index-CsshdsPI.js / index-DwqvyFqw.css），内容级等价于上一轮双视口实测产物。
- 后端 core/audit_store.py（get_ticket_progress）与 api/main.py（GET /tickets/progress/{request_id}）本会话零触碰，其可用性由本轮 27/27 实测覆盖。
- 对外契约不变：ChatResponse/既有路由/OrchestratorResult/evaluator.py 直调路径均未涉及。铁律 2 合规：默认用户界面（对话/文档库/转人工）不含任何 Agent 路由信息，primaryAgent 仅剩 dev 面板渲染点（App.vue:299）与不渲染数据字段（App.vue:635-636）。

**改动文件**（本会话实际产物）：`frontend/dist/index.html`、`frontend/dist/assets/index-CsshdsPI.js`、`frontend/dist/assets/index-DwqvyFqw.css`。（src 与后端文件的定稿改动已在工作区，本会话未新增改动。）

**测试**：

- `npm run build`（frontend/ 目录）→ 503ms 构建成功，产物 index-CsshdsPI.js(106.31kB)/index-DwqvyFqw.css(37.25kB)。
- `node frontend/node_modules/vite/bin/vite.js build frontend` → 526ms 复验成功，产物 hash 与 npm run build 一致。
- `grep dist/assets/index-CsshdsPI.js`：「Agent 主答」=0、AGENT_LABELS=0、「处理进度」=1；`grep dist/assets/index-DwqvyFqw.css`：loop-step=1（m-msg-meta=1 为定稿要求保留的死类本体）；dist/index.html 引用 index-CsshdsPI.js 与 index-DwqvyFqw.css。
- `grep frontend/src/App.vue`：m-msg-meta/AGENT_LABELS/agentLabel/supportLabels 均 0 处；primaryAgent 仅 :299（dev 面板）与 :635-636（数据字段）。
- `.venv/Scripts/python.exe tests/test_frontend_endpoints.py` → 27/27 PASSED（含 GET /tickets/progress/{rid} 未知编号空态、/api/python/tickets/progress 别名、App.vue assistant 消息检查、dist 渲染标记 md-hr/md-inline-code），连跑两次结果一致。

**遗留问题**：

1. 本会话未修改任何 src 文件：定稿改动已在工作区（上一轮施工遗留），本会话职责为逐项核对+强制重建 dist+最小测试自证；src 与后端文件未新增改动。
2. tests/test_frontend_endpoints.py 为独立脚本（文件头注明用法 python tests/test_frontend_endpoints.py），pytest --collect-only 收集为 0 属预期，非测试失败。
3. 系统 Python（D:\Python\Python313）缺 chromadb，改用仓库 .venv/Scripts/python.exe 运行全部测试；未向 C 盘写任何文件，未执行 git commit/push。
4. 未重跑双视口浏览器实测：当前 dist 与上一轮已人工核验产物内容 hash 完全一致（字节等价），该结论沿用上一轮证据；本会话证据为构建日志+grep+27/27。
5. 门禁四件套按最小集只跑 test_frontend_endpoints.py；test_bge_proto.py / test_laya_fusion.py / test_adversarial.py 未跑（意图与评测层零改动，如实披露）。
6. 「限定升级场景展示处理者」替代方案仍未采纳（需仓库所有者确认放行），恢复点：App.vue 模板加 item.escalated && 条件 + 还原标签映射与可见样式。

---

## 验收状态与后续建议

**当前验收状态**：四个阶段施工/核验与自证完成；统一门禁 6/6 全 PASS（见总览表）；意图基准 smoke 持平基线（test2 单模 acc=0.9293 = 基线 0.9293）；收尾修复记录为「无收尾修复」。全部改动留在工作区、未做任何 git commit/push（阶段一记录 HEAD 仍为 65b38ad），终审与验收请以工作区现状为准。

**后续建议（三项）**：

1. **意图基准全量三切分**：本窗仅跑 test2 单模 smoke（acc=0.9293，与基线 0.9293 持平），全量三切分评测尚未执行；阶段二数据亦建议夜窗例行评测复核意图基线（意图层文件 core/intent_recognizer.py 等经 git diff 确认零改动）。
2. **safetymind-review 监工终审**：安排 safetymind-review 对四阶段施工与门禁结果做监工终审，确认后方可视为正式验收通过。
3. **1440/390 双视口人工截图验收**：阶段四本会话未重跑双视口浏览器实测——当前 dist 与上一轮已人工核验产物内容 hash 完全一致（字节等价），结论沿用上一轮证据；仍需完成 1440/390 双视口人工截图验收以闭环前端验收。
