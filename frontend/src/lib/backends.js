const DEFAULT_BACKENDS = {
  python: {
    id: 'python',
    label: 'SafetyMind API',
    baseUrl: import.meta.env.VITE_PYTHON_API_URL || '/api/python',
    port: '8000'
  }
}

// 每浏览器唯一匿名身份：绝不共享默认值（曾因全体默认 u1001 导致内测用户互相看到彼此会话）。
// crypto.randomUUID 仅安全上下文可用（线上纯 HTTP 部署没有），必须带兜底。
function newUserId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return 'u-' + crypto.randomUUID().slice(0, 8)
  }
  return 'u-' + 'xxxxxxxx'.replace(/x/g, () => ((Math.random() * 16) | 0).toString(16))
}

export function createInitialSettings() {
  const saved = readSettings()
  return {
    backend: 'python',
    // 已保存的（含遗留的 u1001）不动——保留老浏览器的历史归属；只有"从未设置过"才生成唯一 ID
    userId: saved.userId || newUserId(),
    conversationId: saved.conversationId || '',
    endpoints: {
      python: saved.endpoints?.python || DEFAULT_BACKENDS.python.baseUrl
    }
  }
}

export function saveSettings(settings) {
  localStorage.setItem('safetymind.frontend.settings', JSON.stringify(settings))
}

export function backendMeta(type, settings) {
  const meta = DEFAULT_BACKENDS.python
  return {
    ...meta,
    baseUrl: normalizeBaseUrl(settings.endpoints?.python || meta.baseUrl)
  }
}

export async function requestHealth(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/health')
}

export async function requestConfig(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/config')
}

export async function requestConversations(type, settings, userId) {
  const params = new URLSearchParams({ user_id: userId || 'anonymous' })
  return requestJson(backendMeta(type, settings).baseUrl, `/conversations?${params}`)
}

export async function requestConversationMessages(type, settings, userId, convId) {
  const params = new URLSearchParams({ user_id: userId || 'anonymous' })
  return requestJson(backendMeta(type, settings).baseUrl, `/conversations/${encodeURIComponent(convId)}/messages?${params}`)
}

export async function deleteConversation(type, settings, userId, convId) {
  const params = new URLSearchParams({ user_id: userId || 'anonymous' })
  return requestJson(backendMeta(type, settings).baseUrl, `/conversations/${encodeURIComponent(convId)}?${params}`, { method: 'DELETE' })
}

export async function requestBootstrapAdmin(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/config/admin/bootstrap', { method: 'POST' })
}

export async function requestAdminConfig(type, settings, token) {
  return requestJson(backendMeta(type, settings).baseUrl, '/config/admin', {
    headers: { 'X-Admin-Token': token || '' }
  })
}

export async function saveAdminConfig(type, settings, token, values) {
  return requestJson(backendMeta(type, settings).baseUrl, '/config/admin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token || '' },
    body: JSON.stringify({ values })
  })
}

export async function requestMonitor(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/monitor')
}

export async function requestSkills(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/skills')
}

export async function requestFeedback(type, settings, payload) {
  return requestJson(backendMeta(type, settings).baseUrl, '/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

// 隐患确认卡：用户点击"上报隐患"后建单（镜像 /feedback：request_id 即凭据）。
// 建议后端只存 24 小时，过期回 404 由调用方提示。
export async function requestHazardConfirm(type, settings, payload) {
  return requestJson(backendMeta(type, settings).baseUrl, '/hazard/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
}

export async function reloadSkills(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/skills/reload', { method: 'POST' })
}

export async function requestKnowledgeStats(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/stats')
}

export async function runEvaluation(type, settings, body = null) {
  return requestJson(backendMeta(type, settings).baseUrl, '/eval/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  })
}

export async function requestLastEval(type, settings) {
  return requestJson(backendMeta(type, settings).baseUrl, '/eval/last')
}

export async function requestSearch(type, settings, query, topK = 5) {
  // 检索测试语义是"验证文档能否被找到"：直连向量检索（几十毫秒），
  // 不走改写+重排的完整链路（含两次 LLM，秒级），那留给 /search 演示端点。
  const params = new URLSearchParams({ query, top_k: String(topK) })
  return requestJson(backendMeta(type, settings).baseUrl, `/search/direct?${params}`, { method: 'POST' })
}

export async function requestChat(type, settings, message) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const raw = await requestJson(meta.baseUrl, '/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  })
  return normalizeChatResponse(type, raw)
}

// 流式对话（2026-10-29）：后端 /chat/stream 早就实现（事件 meta → stage* → delta → done），
// 但前端一直只调 /chat —— 于是用户实际仍是"盯着三个点等 15~50s"，上一轮所说的
// "等待期无反馈"只修了后端一半。这里补上前端接入：
//   - stage 事件驱动"正在检索资料 / 正在生成回答"的可见进度；
//   - delta 边到边追加（快路径几十毫秒即有正文）；
//   - done 带完整响应体，normalize 后与 /chat 完全同构（升级卡/依据/建单卡都不丢）。
// 任何一步不可用（旧后端、代理缓冲、网络中断）都**回退到非流式 /chat**，绝不空回。
export async function streamChat(type, settings, message, { onStage } = {}) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  const url = `${normalizeBaseUrl(meta.baseUrl)}/chat/stream`
  let finalBody = null
  let streamed = ''
  let errEvent = null
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Access-Token': localStorage.getItem('safetymind.accessToken') || ''
      },
      body: JSON.stringify(payload)
    })
    const ctype = String(response.headers.get('content-type') || '')
    if (!response.ok || !response.body || !ctype.includes('text/event-stream')) {
      throw new Error('stream-unavailable')
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // SSE 帧以空行分隔；末段可能不完整，留在 buffer 里等下一次 read
      const frames = buffer.split('\n\n')
      buffer = frames.pop() || ''
      for (const frame of frames) {
        const evt = parseSseFrame(frame)
        if (!evt) continue
        if (evt.event === 'stage') {
          if (typeof onStage === 'function') onStage(evt.data || {})
        } else if (evt.event === 'delta') {
          streamed += String((evt.data && evt.data.text) || '')
        } else if (evt.event === 'error') {
          // 排队超时/生成失败：记下来交给 UI 显示"重试"，不要静默吞掉
          errEvent = evt.data || {}
        } else if (evt.event === 'done') {
          finalBody = evt.data || {}
        }
      }
    }
    if (!finalBody) throw new Error('stream-no-done')
    // 后端 done 已带完整响应体；万一旧后端只回摘要，用累积的 delta 补齐正文
    const merged = { ...finalBody }
    if (!merged.response) merged.response = streamed
    const normalized = normalizeChatResponse(type, merged)
    // 失败信息透传给 UI（event:error 优先，其次 done 里带的 error）
    const err = errEvent || merged.error || null
    if (err) {
      normalized.error = err
      const wait = Number(err.retry_after ?? (merged.error && merged.error.retry_after) ?? 0)
      normalized.retryAfter = Number.isFinite(wait) ? wait : 0
    }
    return normalized
  } catch {
    return requestChat(type, settings, message)
  }
}

function parseSseFrame(frame) {
  const lines = String(frame || '').split('\n')
  let event = 'message'
  const dataLines = []
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return { event, data: { text: dataLines.join('\n') } }
  }
}


// 升级卡片的 Loop 工单进度（用户端克制暴露）：按上报编号查五阶段进度与最新批注。
// 未知编号回 200+found=false（"工单生成中"空态），网络/服务异常由调用方 try/catch 降级。
export async function requestTicketProgress(type, settings, requestId) {
  const meta = backendMeta(type, settings)
  const raw = await requestJson(meta.baseUrl, `/tickets/progress/${encodeURIComponent(requestId)}`)
  return normalizeTicketProgress(raw)
}

function normalizeTicketProgress(raw) {
  if (!raw || raw.found === false) return { found: false }
  return {
    found: true,
    ticketId: raw.ticket_id ?? raw.ticketId ?? 0,
    title: raw.title || '',
    status: raw.status || 'open',
    stage: raw.stage || '',
    stageLabel: raw.stage_label || raw.stageLabel || '',
    createdTs: Number(raw.created_ts ?? raw.createdTs ?? 0),
    closedTs: raw.closed_ts ?? raw.closedTs ?? null,
    steps: (raw.steps || []).map(s => ({
      stage: s.stage || '',
      label: s.label || '',
      state: s.state || 'pending',
      ts: s.ts ?? null
    })),
    verdict: {
      stage: raw.verdict?.stage || '',
      label: raw.verdict?.label || '验收',
      state: raw.verdict?.state || 'pending',
      ts: raw.verdict?.ts ?? null
    },
    latestEvent: raw.latest_event ? {
      action: raw.latest_event.action || '',
      actor: raw.latest_event.actor || '',
      note: raw.latest_event.note || '',
      toStage: raw.latest_event.to_stage || raw.latest_event.toStage || '',
      ts: Number(raw.latest_event.ts ?? 0)
    } : null
  }
}

export async function addKnowledge(type, settings, documents) {
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ documents })
  })
}

export async function uploadKnowledge(type, settings, file) {
  const form = new FormData()
  form.append('file', file)
  return requestJson(backendMeta(type, settings).baseUrl, '/knowledge/upload', {
    method: 'POST',
    body: form
  })
}

function buildChatPayload(type, settings, message) {
  return {
    message,
    user_id: settings.userId || 'anonymous',
    conv_id: settings.conversationId || undefined
  }
}

function normalizeChatResponse(type, raw) {
  return {
    backend: type,
    conversationId: raw.conversation_id || raw.conversationId || raw.conv_id || '',
    requestId: raw.request_id || raw.requestId || '',
    response: raw.response || '',
    intent: raw.intent || 'other',
    intentGroup: raw.intent_group || raw.intentGroup || 'other',
    agentType: raw.agent_type || raw.agentType || '',
    agentTypes: raw.agent_types || raw.agentTypes || [],
    primaryAgent: raw.primary_agent || raw.primaryAgent || '',
    supportingAgents: raw.supporting_agents || raw.supportingAgents || [],
    routingReason: raw.routing_reason || raw.routingReason || '',
    routingConfidence: Number(raw.routing_confidence ?? raw.routingConfidence ?? 0),
    entities: raw.entities || {},
    intentConfidence: Number(raw.intent_confidence ?? raw.intentConfidence ?? 0),
    intentSourceScores: raw.intent_source_scores || raw.intentSourceScores || {},
    escalated: Boolean(raw.escalated),
    latencyMs: Number(raw.latency_ms ?? raw.latencyMs ?? 0),
    totalMs: Number(raw.total_ms ?? raw.totalMs ?? 0),
    knowledgeUsed: Boolean(raw.knowledge_used ?? raw.knowledgeUsed),
    hazardAction: raw.hazard_action || raw.hazardAction || 'none',
    hazardTicketId: Number(raw.hazard_ticket_id ?? raw.hazardTicketId ?? 0),
    hazardSuggestion: raw.hazard_suggestion ? {
      summary: raw.hazard_suggestion.summary || '',
      severity: raw.hazard_suggestion.severity || 'general',
      dept: raw.hazard_suggestion.dept || '',
      deadlineHours: Number(raw.hazard_suggestion.deadline_hours ?? raw.hazard_suggestion.deadlineHours ?? 0),
      basis: raw.hazard_suggestion.basis || ''
    } : null,
    // 依据引用（P0-B）：命中片段 + 可定位身份。业务面只用 title/section/
    // source/version/snippet；chunkId/contentHash 等指纹留给 ?dev=1 面板（铁律 2）。
    citations: Array.isArray(raw.citations) ? raw.citations.map((c) => ({
      index: Number(c.index || 0),
      title: c.title || '',
      section: c.section || '',
      source: c.source || '',
      version: c.version || '',
      status: c.status || '',
      score: c.score,
      snippet: c.snippet || '',
      docId: c.doc_id || c.docId || '',
      chunkId: c.chunk_id || c.chunkId || '',
      contentHash: c.content_hash || c.contentHash || '',
      chunkIndex: Number(c.chunk_index ?? c.chunkIndex ?? -1),
      totalChunks: Number(c.total_chunks ?? c.totalChunks ?? -1),
      catalogOnly: Boolean(c.catalog_only ?? c.catalogOnly)
    })) : [],
    // 工具轨迹（P0-A）：本次请求真实发生的工具调用（不是"口头声称已查询"）。
    toolsUsed: Array.isArray(raw.tools_used ?? raw.toolsUsed) ? (raw.tools_used ?? raw.toolsUsed) : [],
    raw
  }
}

async function requestJson(baseUrl, path, options = {}) {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  options.headers = { ...(options.headers || {}), 'X-Access-Token': localStorage.getItem('safetymind.accessToken') || '' }
  const response = await fetch(url, options)
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    if (response.status === 401 && String(data && data.detail || '').includes('访问令牌')) {
      throw new Error('需要访问令牌：请点右上角 ⚙ 设置，在"访问令牌"中填入管理员发的口令')
    }
    if (response.status === 401) {
      throw new Error('管理密码无效，请重新输入')
    }
    const detail = typeof data === 'string' ? data : JSON.stringify(data)
    throw new Error(`${response.status} ${response.statusText}: ${detail}`)
  }
  return data
}

function normalizeBaseUrl(value) {
  return String(value || '').replace(/\/+$/, '')
}

function readSettings() {
  try {
    return JSON.parse(localStorage.getItem('safetymind.frontend.settings') || '{}')
  } catch {
    return {}
  }
}
