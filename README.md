# SafetyMind 安全生产智能协同平台

SafetyMind 是面向**化工、石油、电力等流程工业**的安全生产智能协同平台，为一线作业人员、安全员和管理者提供设备异常处置、隐患上报、作业票证、危化品管理和应急响应等场景的智能问答与处置建议。

平台基于 [EchoMind](https://github.com/Biscuit-AI531/EchoMind) 多 Agent 架构改造：将通用客服 Agent 替换为三类安全生产专家 Agent，并内置安全生产处置规范 Skills，支持按关键词动态注入和运行时热加载。

> 说明：代码源自 EchoMind，内部服务名、容器名、网络名与部分环境变量仍沿用 `echomind`（见下文命令）。如需彻底改名，可全局替换 `docker-compose.yml`、`.env` 与代码中的 `echomind` / `ECHOMIND_` 前缀。

## 核心链路

```text
用户请求
  -> FastAPI /chat
  -> MemoryManager 读取 Redis 工作记忆 + ChromaDB 情景记忆 + 用户画像
  -> IntentRecognizer 识别意图
  -> AgentOrchestrator 路由到 安全生产协调 / 设备工艺安全 / 安全合规应急 Agent
  -> LLM 生成处置建议
  -> 写入 Redis，并异步更新 ChromaDB 用户画像
```

## 三类安全生产 Agent

| Agent | AgentType | 职责 | 典型场景 |
|-------|-----------|------|----------|
| 安全生产协调 | `general` | 接待、澄清、隐患上报分流、应急升级 | 隐患上报、事故线索、通用安全咨询 |
| 设备工艺安全 | `technical` | 设备异常、工艺参数报警、介质泄漏、联锁与检维修 | 超压超温、介质泄漏、ESD/SIS 联锁、机泵故障 |
| 安全合规应急 | `billing` | 作业票证、许可审批、危化品、事故上报与应急响应 | 动火/受限空间作业票、危化品管理、事故上报 |

## 内置安全 Skills

| Skill | 目录 | 适用 Agent | 说明 |
|-------|------|-----------|------|
| 安全生产协调接待规范 | `skills/safety_coordination/` | `general` | 接待、澄清、隐患上报分流、应急升级 |
| 设备工艺安全处置规范 | `skills/equipment_process_safety/` | `technical` | 报警处置、泄漏、联锁、检维修、风险边界 |
| 安全合规与应急处理规范 | `skills/compliance_emergency/` | `billing` | 作业票、许可审批、危化品、事故上报、应急响应 |

Skills 从 `ECHOMIND_SKILLS_DIR` 加载，命中关键词时注入对应 Agent 的 system prompt，修改后 `POST /skills/reload` 即可热加载，无需重启。

## 项目结构

```text
SafetyMind/
├── api/main.py                    # FastAPI 入口 /chat /search /knowledge /monitor /eval
├── core/intent_recognizer.py      # 三路融合意图识别
├── core/skill_loader.py           # Skills 加载与注入
├── agents/agent_orchestrator.py   # 多 Agent 路由编排
├── memory/conversation_memory.py  # Redis + ChromaDB 记忆管理
├── mcp/tool_manager.py            # MCP 工具调用、查询改写、重排、熔断、缓存、降级
├── mcp/knowledge_base.py          # ChromaDB RAG 知识库
├── monitor/performance_monitor.py # Agent/工具在线监控
├── evaluation/evaluator.py        # 端到端评测
├── skills/                        # 安全生产处置规范 Skills
│   ├── safety_coordination/SKILL.md
│   ├── equipment_process_safety/SKILL.md
│   └── compliance_emergency/SKILL.md
├── config/                        # nginx / prometheus 配置
├── docker-compose.yml             # Docker 全栈编排
├── Dockerfile
├── requirements.txt
└── .env.example
```

## 环境准备

### 必需依赖

- Docker
- Docker Compose
- Anthropic API Key，或兼容 Anthropic 协议的第三方 API Key（推荐 DeepSeek）

### 配置 `.env`

复制示例文件：

```bash
cp .env.example .env
```

最少需要配置：

```env
ANTHROPIC_API_KEY=your_api_key
```

使用 DeepSeek 这类 Anthropic 兼容接口：

```env
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_API_KEY=your_deepseek_key
```

Docker Compose 场景下，Redis 和 ChromaDB 的连接由 `docker-compose.yml` 覆盖为容器内地址，通常不需要手动改。

## Docker Compose 全栈部署

```bash
docker compose up -d --build
```

查看服务状态：

```bash
docker compose ps
```

查看应用日志：

```bash
docker compose logs -f echomind
```

启动后的端口：

| 服务 | 容器名 | 宿主机端口 | 用途 |
|------|--------|------------|------|
| SafetyMind API | `echomind-app` | `8000` | 主 API 服务 |
| Nginx | `echomind-nginx` | `80` | 反向代理 |
| ChromaDB | `echomind-chromadb` | `8001` | 向量数据库 |
| Redis | `echomind-redis` | `6379` | 工作记忆 |
| Prometheus | `echomind-prometheus` | `9090` | 监控数据 |

健康检查：

```bash
curl http://localhost:8000/health
```

Swagger 文档：

```text
http://localhost:8000/docs
```

## Docker Run 开发模式

开发时可以只用 Compose 启动依赖，然后用 `docker run` 挂载当前代码目录。

先启动 Redis 和 ChromaDB：

```bash
docker compose up -d redis chromadb
```

构建镜像：

```bash
docker compose build --no-cache echomind
```

启动 HTTP 服务：

```bash
docker run -it --rm \
  --network echomind_echomind-network \
  -p 8000:8000 \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:echomind123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  echomind
```

CLI 交互模式：

```bash
docker run -it --rm \
  --network echomind_echomind-network \
  -e ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic" \
  -e ANTHROPIC_API_KEY="your_key" \
  -e ANTHROPIC_MODEL="deepseek-v4-pro" \
  -e REDIS_URL="redis://:echomind123@redis:6379/0" \
  -e CHROMA_HOST="chromadb" \
  -e CHROMA_PORT="8000" \
  -v "$(pwd):/workspace" \
  -w /workspace \
  echomind \
  python api/main.py --cli
```

## API 接口总览

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/health` | 健康检查，返回服务状态和 Agent 统计 |
| `POST` | `/chat` | 主对话接口：记忆读取、意图识别、Agent 路由、回复生成、记忆写入 |
| `POST` | `/search` | 知识库检索优化链路：查询改写、并行召回、合并去重、LLM 重排 |
| `GET` | `/skills` | 查看当前加载的 Skills、匹配关键词和解析错误 |
| `POST` | `/skills/reload` | 运行时重新扫描 Skill 目录（热加载） |
| `POST` | `/knowledge/add` | 批量导入文档到 ChromaDB 知识库 |
| `POST` | `/knowledge/upload` | 上传 `.txt` / `.md` / `.json` 文件导入知识库 |
| `GET` | `/knowledge/stats` | 查看知识库文档片段总数 |
| `GET` | `/monitor` | 查看 Agent/工具统计、告警和优化建议 |
| `POST` | `/eval/run` | 运行内置意图识别和端到端评测 |
| `GET` | `/docs` | Swagger UI |

### Skills 动态加载

默认配置：

```env
ECHOMIND_SKILLS_DIR=./skills
ECHOMIND_SKILLS_MAX_PROMPT_CHARS=5000
```

推荐结构（每个 Skill 一个目录，主文件命名为 `SKILL.md`）：

```text
skills/equipment_process_safety/SKILL.md
skills/compliance_emergency/SKILL.md
```

`SKILL.md` 示例（设备工艺安全处置规范）：

```markdown
---
name: 设备工艺安全处置规范
description: 设备异常、工艺参数报警、介质泄漏、联锁与检维修处置规范
keywords: 报警,超压,超温,泄漏,联锁,检维修,可燃,有毒
agents: technical
enabled: true
---

# 设备工艺安全处置规范

- 涉及超压、超温、可燃/有毒介质泄漏、联锁旁路时，优先「先撤人、先隔离、先泄压、先报警」。
- 严禁建议擅自摘除联锁、旁路安全仪表、带压堵漏。
```

查看加载结果：

```bash
curl http://localhost:8000/skills
```

修改后热加载：

```bash
curl -X POST http://localhost:8000/skills/reload
```

## 使用示例

### 设备工艺报警

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "反应釜压力异常升高触发超压报警，如何处置？",
    "user_id": "user_tech",
    "conv_id": "tech_001"
  }'
```

预期路由到 `technical`（设备工艺安全）Agent。

### 隐患上报

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "现场发现有人未佩戴安全帽进入装置区",
    "user_id": "user_general",
    "conv_id": "general_001"
  }'
```

预期路由到 `general`（安全生产协调）Agent。

### 作业票证

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "动火作业需要办理哪些票证，审批流程是什么？",
    "user_id": "user_bill",
    "conv_id": "bill_001"
  }'
```

预期路由到 `billing`（安全合规应急）Agent。

### 多轮对话

保持同一个 `user_id` 和 `conv_id` 即可连续对话：

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "是裂解炉区的可燃气体报警", "user_id": "user_tech", "conv_id": "tech_001"}'
```

### `/chat` 响应字段

| 字段 | 说明 |
|------|------|
| `conv_id` | 会话 ID |
| `response` | Agent 回复 |
| `intent` | 意图识别结果 |
| `agent_type` | 实际处理请求的 Agent |
| `escalated` | 是否触发升级（转应急指挥/人工） |
| `latency_ms` | 端到端耗时 |

## 知识库使用

知识库由 `mcp/knowledge_base.py` 管理，底层使用 ChromaDB collection `knowledge_base`。可导入安全操作规程、应急处置方案、作业票证规范等企业文档。

### 批量导入

```bash
curl -X POST http://localhost:8000/knowledge/add \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {
        "title": "动火作业安全规程",
        "content": "动火作业前须完成可燃气体检测合格、灭火器材到位、监护人到位，并按作业等级审批票证。"
      },
      {
        "title": "受限空间应急处置",
        "content": "受限空间作业须先气体检测、通风，配备监护和应急救援装备；出现异常立即撤离并报警。"
      }
    ]
  }'
```

### 上传文件

```bash
curl -X POST http://localhost:8000/knowledge/upload \
  -F "file=@data/demo_docs/safety_procedures.md"
```

### 检索

```bash
curl -X POST "http://localhost:8000/search?query=动火作业需要什么票证&top_k=3"
```

`/search` 使用完整检索优化链路：查询改写 → 多子查询并行召回 → 合并去重 → LLM 重排 → 返回 Top-K。

## ChromaDB 数据查看

平台使用三个 ChromaDB collection：

| Collection | 模块 | 作用 |
|------------|------|------|
| `knowledge_base` | `mcp/knowledge_base.py` | RAG 知识库文档片段 |
| `episodic` | `memory/conversation_memory.py` | 压缩后的历史对话摘要 |
| `user_profile` | `memory/conversation_memory.py` | 用户画像，包含偏好和关键实体 |

宿主机访问 ChromaDB：

```text
http://localhost:8001
```

查看 collection 列表（在应用容器内）：

```bash
docker exec -it echomind-app bash
python - <<'PY'
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
for c in client.list_collections():
    print("-", c.name, "count=", c.count())
PY
```

## Redis 工作记忆

Redis 容器名 `echomind-redis`，默认密码 `echomind123`。

```bash
docker exec -it echomind-redis redis-cli -a echomind123
```

工作记忆 key 格式：`wm:{user_id}:{conv_id}`；会话摘要 key 格式：`summary:{user_id}:{conv_id}`。

工作记忆压缩（默认阈值 `COMPRESS_AT = 15`）：当同一会话消息达到 15 条时，旧消息压缩为摘要写入 Redis `summary` 与 ChromaDB `episodic`，最近 5 条保留在 Redis `wm`。

## Monitor 在线监控

```bash
curl http://localhost:8000/monitor
```

返回 `agent_stats`（调用次数/成功率/延迟/routing_score）、`tool_stats`（含熔断状态）、`active_alerts`、`suggestions`。

Prometheus 页面：

```text
http://localhost:9090
```

## 端到端评测

```bash
curl -X POST http://localhost:8000/eval/run
```

评测内容包括意图识别准确率、真实回复生成、LLM-as-Judge 打分（相关性/准确性/完整性/有用性）、回归检测与优化建议。

## 停止、重启和清理

```bash
docker compose stop            # 停止
docker compose restart echomind # 重启应用
docker compose down            # 停止并删除容器（保留数据卷）
docker compose down -v         # 停止并删除容器和数据卷
docker compose up -d --build   # 重新构建并启动
```

## 常见问题

### `/health` 返回 503

```bash
docker compose logs -f echomind
```

重点检查：`.env` 是否配置 `ANTHROPIC_API_KEY`、Redis/ChromaDB 是否健康、应用容器是否反复重启。

### ChromaDB 连接失败

```bash
docker compose ps chromadb
curl http://localhost:8001/api/v1/heartbeat
```

### Redis 认证失败

确认 `.env` 和 `docker-compose.yml` 中的 `REDIS_PASSWORD` 一致，默认值为 `echomind123`。

```bash
docker exec -it echomind-redis redis-cli -a echomind123 ping
```

### Skills 未生效

```bash
curl http://localhost:8000/skills
```

确认目标 Skill 的 `enabled: true`、`agents` 与当前路由到的 Agent 匹配、用户消息命中了 `keywords`。修改文件后执行 `curl -X POST http://localhost:8000/skills/reload`。

## 推荐验证流程

```bash
# 1. 启动
docker compose up -d --build

# 2. 健康检查
curl http://localhost:8000/health

# 3. 主对话（设备报警）
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "反应釜压力异常升高触发超压报警，如何处置？", "user_id": "demo_user", "conv_id": "demo_conv"}'

# 4. 查看已加载 Skills
curl http://localhost:8000/skills

# 5. 知识库统计 / 检索
curl http://localhost:8000/knowledge/stats
curl -X POST "http://localhost:8000/search?query=动火作业票证&top_k=3"

# 6. 监控与评测
curl http://localhost:8000/monitor
curl -X POST http://localhost:8000/eval/run
```
