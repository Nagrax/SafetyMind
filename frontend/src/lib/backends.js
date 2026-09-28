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
