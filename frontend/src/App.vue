<template>
  <!--
    移动优先的用户端结构（2026-09-26 重构）：
    - 用户默认只见一个全屏对话页（header + 消息流 + 快捷场景 + 输入栏 + 转人工按钮）
    - 文档入库收进"文档库"第二页；开发者信息（trace/监控/评测/Skills）整体移入
      开发者面板（?dev=1 或菜单开启），用户端默认不可见
    - 升级/紧急消息在对话流内给操作卡：直拨值班电话（tel:）+ 复制上报编号
    - 桌面端同一套代码：对话区限宽居中，开发者面板变为右侧栏
  -->
  <main :class="['m-app', devMode ? 'dev-on' : '', isDesktopChat ? 'chat-desktop' : '', historyOpen ? 'hist-open' : 'hist-closed']">
    <!-- ══════════ 对话页（用户主页） ══════════ -->
    <template v-if="activeView === 'chat'">
      <header class="m-header">
        <div class="m-brand"><span class="m-logo">S</span><span>SafetyMind</span></div>
        <div class="m-header-right">
          <span class="m-status" :class="healthOk ? 'ok' : 'bad'" :title="healthOk ? '服务正常' : '服务不可达'"></span>
          <button :class="['m-icon-btn', historyOpen ? 'active' : '']" aria-label="历史会话" title="历史会话 开/关" @click="historyOpen = !historyOpen">☰</button>
          <button class="m-icon-btn" aria-label="设置" title="设置" @click="openSettings">⚙</button>
          <button class="m-icon-btn" aria-label="开始新对话" title="新对话" @click="newConversation">✎</button>
          <button class="m-lib-btn" aria-label="打开文档库" @click="activeView = 'library'">
            <span>📚</span><span>文档库</span>
          </button>
        </div>
      </header>

      <div class="m-messages" ref="messageList">
        <div v-if="messages.length === 0" class="m-empty">
          <div class="m-empty-icon">⛑</div>
          <h2>你好，我是安全助手</h2>
          <p>隐患、报警、作业票、法规，直接问；紧急情况秒回应急步骤。</p>
          <div class="m-suggest">
            <button v-for="p in STARTERS" :key="p.label" @click="usePrompt(p.text)">{{ p.label }}</button>
          </div>
        </div>

        <article v-for="item in messages" :key="item.id" :class="['m-msg', item.role, { critical: item.critical }]">
          <!-- 助手消息：Markdown 渲染。parseMarkdown/inlineTokens 只产数据块，
               全部经 {{ }} 插值转义，无 v-html/innerHTML，XSS 面为零 -->
          <div v-if="item.role === 'assistant'" class="m-msg-body md-body">
            <template v-for="(block, bi) in parseMarkdown(item.content)" :key="bi">
              <pre v-if="block.t === 'code'" class="md-code"><code>{{ block.lines.join('\n') }}</code></pre>
              <component :is="'h' + block.level" v-else-if="block.t === 'h'" :class="'md-h md-h' + block.level"><template v-for="(seg, si) in inlineTokens(block.text)" :key="si"><strong v-if="seg.t === 'b'">{{ seg.v }}</strong><code v-else-if="seg.t === 'c'" class="md-inline-code">{{ seg.v }}</code><em v-else-if="seg.t === 'i'" class="md-em">{{ seg.v }}</em><br v-else-if="seg.t === 'br'" /><template v-else>{{ seg.v }}</template></template></component>
              <hr v-else-if="block.t === 'hr'" class="md-hr" />
              <ul v-else-if="block.t === 'ul'" class="md-list">
                <li v-for="(li, ix) in block.items" :key="ix">
                  <template v-for="(seg, si) in inlineTokens(li)" :key="si"><strong v-if="seg.t === 'b'">{{ seg.v }}</strong><code v-else-if="seg.t === 'c'" class="md-inline-code">{{ seg.v }}</code><em v-else-if="seg.t === 'i'" class="md-em">{{ seg.v }}</em><br v-else-if="seg.t === 'br'" /><template v-else>{{ seg.v }}</template></template>
                </li>
              </ul>
              <ol v-else-if="block.t === 'ol'" class="md-list">
                <li v-for="(li, ix) in block.items" :key="ix">
                  <template v-for="(seg, si) in inlineTokens(li)" :key="si"><strong v-if="seg.t === 'b'">{{ seg.v }}</strong><code v-else-if="seg.t === 'c'" class="md-inline-code">{{ seg.v }}</code><em v-else-if="seg.t === 'i'" class="md-em">{{ seg.v }}</em><br v-else-if="seg.t === 'br'" /><template v-else>{{ seg.v }}</template></template>
                </li>
              </ol>
              <p v-else>
                <template v-for="(seg, si) in inlineTokens(block.text)" :key="si"><strong v-if="seg.t === 'b'">{{ seg.v }}</strong><code v-else-if="seg.t === 'c'" class="md-inline-code">{{ seg.v }}</code><em v-else-if="seg.t === 'i'" class="md-em">{{ seg.v }}</em><br v-else-if="seg.t === 'br'" /><template v-else>{{ seg.v }}</template></template>
              </p>
            </template>
          </div>
          <!-- 用户消息与旧行为一致：纯文本逐行插值 -->
          <div v-else class="m-msg-body">
            <p v-for="(line, i) in item.content.split('\n')" :key="i">{{ line }}</p>
          </div>
          <div v-if="item.role === 'assistant' && item.escalated" class="m-msg-actions">
            <a v-if="config.escalationPhone" class="m-action primary" :href="`tel:${config.escalationPhone}`">📞 拨打安全值班</a>
            <button v-if="item.requestId" class="m-action" @click="copyText(item.requestId, '上报编号已复制')">复制上报编号 {{ item.requestId }}</button>
            <span v-else class="m-action muted-note">升级已留痕</span>
          </div>
          <div v-else-if="item.role === 'assistant' && item.requestId && !item.feedbackSent" class="m-msg-actions">
            <span class="m-action muted-note">这条回答有帮助吗？</span>
            <button class="m-action" :disabled="feedbackBusy" @click="sendFeedback(item, 'up')">👍</button>
            <button class="m-action" :disabled="feedbackBusy" @click="sendFeedback(item, 'down')">👎</button>
          </div>
          <div v-else-if="item.role === 'assistant' && item.feedbackSent" class="m-msg-actions">
            <span class="m-action muted-note">已记录，感谢反馈</span>
          </div>
        </article>

        <div v-if="busy" class="m-msg assistant typing"><div class="m-msg-body"><span></span><span></span><span></span></div></div>
      </div>

      <div v-if="messages.length" class="m-quick">
        <button v-for="p in STARTERS" :key="'q' + p.label" @click="usePrompt(p.text)">{{ p.emoji }} {{ p.label }}</button>
      </div>

      <footer class="m-composer">
        <button class="m-call" aria-label="转人工" @click="escalationOpen = true">
          <span class="m-call-icon">☎</span><span class="m-call-text">转人工</span>
        </button>
        <textarea
          v-model="draft"
          rows="1"
          placeholder="描述你的安全问题…"
          maxlength="2000"
          @input="autoGrow"
          @keydown.enter.exact.prevent="onEnter"
        ></textarea>
        <button class="m-send" :disabled="busy || !draft.trim()" @click="sendMessage" aria-label="发送">➤</button>
      </footer>

      <!-- ══════════ 知识库右栏（桌面 ≥1200px 常驻，参考 IDE 多栏面板） ══════════ -->
      <aside class="kb-rail">
        <div class="kb-head">
          <h3>知识库</h3>
          <span class="kb-count">{{ knowledgeCount }} 条</span>
        </div>
        <div class="kb-search">
          <input v-model="searchQuery" placeholder="检索规程 / 制度…" @keydown.enter="searchKnowledge" />
          <button @click="searchKnowledge" :disabled="busy">检索</button>
        </div>
        <div class="kb-results">
          <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="kb-result">
            <strong>{{ item.title || '未命名文档' }}</strong>
            <p>{{ item.content }}</p>
          </article>
          <div v-if="!searchResults.length" class="kb-tip">输入关键词即可检索安全规程，例如"受限空间"、"动火"。</div>
        </div>
        <button class="kb-goto" @click="activeView = 'library'">📚 打开文档库添加文档</button>
      </aside>
    </template>

    <!-- ══════════ 文档库页 ══════════ -->
    <template v-else-if="activeView === 'library'">
      <header class="m-header">
        <button class="m-icon-btn back" aria-label="返回对话" @click="activeView = 'chat'">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
        <div class="m-brand"><span class="m-logo">S</span><span>文档库</span></div>
        <span class="m-chunks">{{ knowledgeCount }} 条知识</span>
      </header>

      <div class="m-page">
        <section class="m-card">
          <div class="m-card-head"><h3>加入文档</h3><span>入库后助手即可检索</span></div>
          <label class="m-upload">
            <input type="file" accept=".txt,.md,.json" @change="handleUpload" />
            <div class="m-upload-box">
              <div class="m-upload-icon">📄</div>
              <strong>点击选择文件</strong>
              <small>支持 .txt / .md / .json，单文件 ≤10MB</small>
            </div>
          </label>
          <details class="m-manual">
            <summary>手动粘贴一段规程 / 制度</summary>
            <label><span>标题</span><input v-model="docTitle" placeholder="如：动火作业管理规定" /></label>
            <label><span>内容</span><textarea v-model="docContent" rows="5" placeholder="粘贴文本内容"></textarea></label>
            <button class="m-btn" :disabled="busy || !docTitle.trim() || !docContent.trim()" @click="submitKnowledge">添加到知识库</button>
          </details>
        </section>

        <section class="m-card">
          <div class="m-card-head"><h3>检索测试</h3><span>验证文档能否被找到</span></div>
          <div class="m-search-line">
            <input v-model="searchQuery" placeholder="如：受限空间作业注意事项" @keydown.enter="searchKnowledge" />
            <button class="m-btn" :disabled="busy || !searchQuery.trim()" @click="searchKnowledge">搜</button>
          </div>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <div class="result-title"><strong>{{ item.title || '未命名文档' }}</strong><small>score {{ item.score ?? '-' }}</small></div>
                <p>{{ item.content }}</p>
              </div>
            </article>
          </div>
        </section>

        <section v-if="devMode" class="m-card">
          <div class="m-card-head"><h3>已加载能力</h3><button class="link-button" @click="reloadSkillSet">重新加载</button></div>
          <div class="skill-table">
            <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
              <span class="skill-dot"></span><strong>{{ skill.name }}</strong><small>{{ skill.content_chars || 0 }} chars</small>
            </div>
            <div v-if="!skillsData.skills.length" class="workspace-empty">暂无已加载 Skill。</div>
          </div>
        </section>
      </div>
    </template>

    <!-- ══════════ 设置（管理令牌 + .env 白名单配置）══════════ -->
    <div v-if="settingsOpen" class="sheet-mask" @click.self="settingsOpen = false">
      <div class="sheet cfg-sheet">
        <h3>系统设置</h3>
        <p class="cfg-intro">这里修改 LLM 密钥、值班电话等系统配置。<b>本机自用无需密码</b>；只有把系统部署到服务器给多人用时，才需要设置管理密码保护这些配置。</p>
        <div v-if="generatedToken" class="cfg-generated">
          <b>你的管理密码（仅显示这一次，请抄录保存）：</b>
          <code>{{ generatedToken }}</code>
          <button @click="copyText(generatedToken, '已复制')">复制</button>
          <button @click="confirmGenerated">我已保存，进入配置</button>
        </div>
        <label class="cfg-row">
          <span>访问令牌<small>管理员发给你的口令；本机自用留空即可</small></span>
          <input v-model="accessToken" placeholder="无令牌则留空" @change="persist" />
        </label>
        <div v-if="!adminCfg.length && !adminInputOpen" class="cfg-first">
          <button @click="adminInputOpen = true">我已有管理密码，点击输入</button>
          <button @click="bootstrapAdmin">首次使用：一键生成管理密码</button>
        </div>
        <label v-else class="cfg-token">
          <span>管理密码</span>
          <input v-model="adminToken" :type="showPw ? 'text' : 'password'" placeholder="输入管理密码" @change="persist" />
          <button class="pw-eye" @click="showPw = !showPw" aria-label="显示或隐藏密码">{{ showPw ? '🙈' : '👁' }}</button>
          <button @click="loadAdminConfig">重新加载</button>
          <button class="pw-logout" @click="forgetAdmin">退出管理</button>
        </label>
        <div v-if="adminCfg.length" class="cfg-list">
          <label v-for="c in adminCfg" :key="c.key" class="cfg-row">
            <span>{{ c.label }}<small>{{ c.key }}<template v-if="c.restart"> · 重启生效</template></small></span>
            <select v-if="c.enum" v-model="cfgDraft[c.key]">
              <option v-for="opt in c.enum" :key="opt" :value="opt">{{ opt }}</option>
            </select>
            <input v-else :type="c.secret ? 'password' : 'text'" v-model="cfgDraft[c.key]"
                   :placeholder="c.configured ? '已配置（留空保持不变）' : '未配置'" autocomplete="off" />
          </label>
        </div>
        <button v-if="adminCfg.length" class="m-btn" :disabled="busy" @click="saveAdminConfig">保存配置</button>
        <p class="sheet-note">密钥只显示掩码；LLM 相关配置保存后需重启生效。手机版窗口关闭后请重新打开。</p>
        <button class="quiet" @click="settingsOpen = false">关闭</button>
      </div>
    </div>

    <!-- ══════════ 转人工 ══════════ -->
    <div v-if="escalationOpen" class="sheet-mask" @click.self="escalationOpen = false">
      <div class="sheet escalation">
        <h3>转人工</h3>
        <a v-if="config.escalationPhone" class="sheet-call" :href="`tel:${config.escalationPhone}`">
          <strong>拨打安全值班电话</strong><span>{{ config.escalationPhone }}</span>
        </a>
        <button @click="escalateInChat">在对话中要求人工处理</button>
        <p class="sheet-note">紧急情况（着火 / 泄漏 / 人员被困）请直接拨打电话，同时按助手给出的应急步骤行动。</p>
        <button class="quiet" @click="escalationOpen = false">取消</button>
      </div>
    </div>

    <!-- ══════════ 历史会话（手机：抽屉；桌面 ≥980px：常驻左栏） ══════════ -->
    <aside :class="['hist-panel', historyOpen ? 'open' : '']">
      <div class="hist-head">
        <h3>历史会话</h3>
        <button class="m-icon-btn hist-close" aria-label="关闭历史列表" @click="historyOpen = false">✕</button>
      </div>
      <div class="hist-list">
        <div v-for="c in conversations" :key="c.conv_id" :class="['hist-item', c.conv_id === settings.conversationId ? 'active' : '']" role="button" tabindex="0" @click="openConversation(c)" @keydown.enter="openConversation(c)">
          <strong>{{ c.title }}</strong>
          <small>{{ formatHistTime(c.updated_at) }} · {{ c.message_count }} 条</small>
          <button class="hist-del" aria-label="删除会话" @click.stop="removeConversation(c)">✕</button>
        </div>
        <div v-if="!conversations.length" class="hist-empty">还没有历史会话</div>
      </div>
    </aside>
    <div v-if="historyOpen" class="hist-mask" @click="historyOpen = false"></div>

    <!-- ══════════ 开发者面板（dev 模式） ══════════ -->
    <aside v-if="devMode" class="dev-panel" ref="sidebarRef">
      <div class="dev-panel-head">
        <h2>开发者面板</h2>
        <button class="m-icon-btn" @click="devMode = false">✕</button>
      </div>
      <div class="dev-panel-scroll">
        <section class="side-card trace-card">
          <div class="card-heading">
            <div><span class="kicker">Last trace</span><h2>最近一次请求</h2></div>
            <span class="trace-status" :class="lastResponse ? 'has-data' : ''"></span>
          </div>
          <div v-if="lastResponse" class="trace-body">
            <div class="latency"><span>处置耗时</span><strong>{{ lastResponse.totalMs || lastResponse.latencyMs || '-' }}<small> ms</small></strong></div>
            <p v-if="lastResponse.totalMs && lastResponse.latencyMs && lastResponse.totalMs > lastResponse.latencyMs * 1.5" class="routing-reason">全链路 {{ lastResponse.totalMs }}ms（含排队/意图/检索；生成 {{ lastResponse.latencyMs }}ms）</p>
            <dl class="detail-list">
              <div><dt>主 Agent</dt><dd>{{ lastResponse.primaryAgent || lastResponse.agentType || '-' }}</dd></div>
              <div><dt>意图</dt><dd>{{ lastResponse.intent || '-' }}</dd></div>
              <div><dt>置信度</dt><dd>{{ formatPercent(lastResponse.routingConfidence) }}</dd></div>
              <div><dt>知识库</dt><dd :class="lastResponse.knowledgeUsed ? 'success' : 'muted'">{{ lastResponse.knowledgeUsed ? '已使用' : '未使用' }}</dd></div>
              <div><dt>升级处置</dt><dd :class="lastResponse.escalated ? 'danger' : 'muted'">{{ lastResponse.escalated ? '是' : '否' }}</dd></div>
            </dl>
            <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
          </div>
          <p v-else class="side-empty">发送消息后，这里会显示安全 Agent 路由、风险意图和耗时。</p>
        </section>

        <section class="side-card connection-card">
          <div class="card-heading">
            <div><span class="kicker">Connection</span><h2>连接配置</h2></div>
            <span class="status-copy" :class="healthOk ? 'success' : 'muted'">{{ healthLabel }}</span>
          </div>
          <div class="connection-endpoint"><span>接口地址</span><code>{{ currentBackend.baseUrl }}</code></div>
          <label><span>用户 ID</span><input v-model="settings.userId" @change="persist" placeholder="自动生成（每浏览器唯一）" /></label>
          <label><span>会话 ID</span><input v-model="settings.conversationId" @change="persist" placeholder="自动生成" /></label>
          <div class="side-actions">
            <button @click="checkHealth">检查连接</button>
            <button class="quiet-button" @click="refreshConsole">刷新</button>
            <button class="quiet-button" @click="openDocs">API 文档</button>
          </div>
        </section>

        <section class="side-card monitor-card">
          <div class="card-heading">
            <div><span class="kicker">Runtime</span><h2>运行状态</h2></div>
            <button class="link-button" @click="loadMonitor">刷新</button>
          </div>
          <div class="mini-stats">
            <div><strong>{{ totalRequests }}</strong><span>请求</span></div>
            <div><strong>{{ agentCount }}</strong><span>Agent</span></div>
            <div><strong>{{ activeAlerts.length }}</strong><span>告警</span></div>
          </div>
          <div v-if="activeAlerts.length" class="alert-note">{{ activeAlerts[0].detail || activeAlerts[0].title }}</div>
          <p v-else class="healthy-note">当前没有活跃告警。</p>
        </section>

        <section class="side-card eval-card">
          <div class="card-heading">
            <div><span class="kicker">Evaluation</span><h2>评测</h2></div>
            <button @click="runEvaluation" :disabled="busy">{{ busy ? '运行中...' : '运行评测' }}</button>
          </div>
          <div v-if="evalData" class="evaluation-summary">
            <div class="score-hero"><span>通过率</span><strong>{{ formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.total }}</small></div>
            <div><span>回归</span><strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong></div>
          </div>
          <p v-else class="side-empty">尚未运行评测。</p>
        </section>
      </div>
    </aside>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import {
  addKnowledge,
  backendMeta,
  createInitialSettings,
  deleteConversation as requestDeleteConversation,
  reloadSkills,
  requestChat,
  requestConfig,
  requestConversationMessages,
  requestAdminConfig,
  requestBootstrapAdmin,
  requestConversations,
  requestHealth,
  saveAdminConfig as requestSaveAdmin,
  requestKnowledgeStats,
  requestLastEval,
  requestFeedback,
  requestMonitor,
  requestSearch,
  requestSkills,
  runEvaluation as requestEvaluation,
  saveSettings,
  uploadKnowledge
} from './lib/backends'
import { inlineTokens, parseMarkdown } from './lib/markdown'

const settings = reactive(createInitialSettings())
const STARTERS = [
  { emoji: '🔥', label: '应急处置', text: '车间着火了，怎么应急处置？' },
  { emoji: '⚠️', label: '隐患上报', text: '现场发现配电箱门未关闭且周围有积水，怎么上报这个隐患？' },
  { emoji: '🎫', label: '作业票', text: '明天要动火焊接，动火作业票怎么办、谁来审批？' },
  { emoji: '📖', label: '查法规', text: '受限空间作业的气体检测有什么标准要求？' },
]

const activeView = ref('chat')
const devMode = ref(new URLSearchParams(window.location.search).get('dev') === '1'
  || localStorage.getItem('safetymind.dev') === '1')
const escalationOpen = ref(false)
// 桌面端默认展开历史列；手机端默认收起（抽屉）
const historyOpen = ref(window.innerWidth >= 980)
const settingsOpen = ref(false)
const showPw = ref(false)
const generatedToken = ref('')
const adminCfg = ref([])
// 管理密码输入框显式开关：入口按钮直接开输入框，不再依赖"已加载成功"才显示
// （历史 bug：显示条件挂在 adminCfg.length 上，密码无效时输入框永远不出现=入口死锁）
const adminInputOpen = ref(false)
const cfgDraft = reactive({})

function openSettings() {
  historyOpen.value = false
  settingsOpen.value = true
  // 没存过管理密码就不自动请求：普通用户打开设置页不该被 401 弹"配置加载失败"惊扰
  if (adminToken.value) loadAdminConfig()
}

async function loadAdminConfig() {
  try {
    const data = await requestAdminConfig(settings.backend, settings, adminToken.value)
    adminCfg.value = data.config || []
    for (const c of adminCfg.value) cfgDraft[c.key] = c.secret ? '' : c.value
  } catch (e) {
    adminCfg.value = []
    const m = e.message || ''
    // requestJson 已把 401 转成中文（不含"401"字样）——按内容匹配，并顺带打开输入框供重输
    if (m.includes('管理密码无效') || m.includes('401')) {
      showToast('管理密码无效，请重新输入')
      adminInputOpen.value = true
    } else if (m.includes('需要访问令牌')) {
      showToast('需要访问令牌：请先在上方"访问令牌"填入管理员发的口令')
    } else if (m.includes('403')) {
      showToast('尚未设置管理密码：点击下方一键生成（仅本机可用）')
    } else {
      showToast('配置加载失败：' + m.slice(0, 60))
    }
  }
}

async function bootstrapAdmin() {
  busy.value = true
  try {
    const r = await requestBootstrapAdmin(settings.backend, settings)
    generatedToken.value = r.admin_token
    adminToken.value = r.admin_token
    localStorage.setItem('safetymind.adminToken', adminToken.value)
    adminInputOpen.value = true
    showToast('管理密码已生成，请在下方抄录保存')
  } catch (e) {
    const m = e.message || ''
    if (m.includes('409')) showToast('已配置过管理密码，请直接输入')
    else if (m.includes('403')) showToast(m.split(':').slice(1).join(':').slice(0, 80) || '本机首次使用才可一键生成；线上部署请向管理员索取口令')
    else showToast('生成失败：' + m.slice(0, 60))
  }
  finally { busy.value = false }
}

async function saveAdminConfig() {
  const values = {}
  for (const c of adminCfg.value) {
    const v = (cfgDraft[c.key] ?? '').trim()
    if (c.secret && v === '') continue        // 密钥留空 = 保持原值
    values[c.key] = v
  }
  if (!Object.keys(values).length) { showToast('没有需要保存的修改'); return }
  busy.value = true
  try {
    const r = await requestSaveAdmin(settings.backend, settings, adminToken.value, values)
    showToast(r.hint || '已保存')
    localStorage.setItem('safetymind.adminToken', adminToken.value)
    await loadAdminConfig()
  } catch (e) { showToast('保存失败: ' + e.message.slice(0, 60)) }
  finally { busy.value = false }
}
const adminToken = ref(localStorage.getItem('safetymind.adminToken') || '')
const accessToken = ref(localStorage.getItem('safetymind.accessToken') || '')
const conversations = ref([])
const config = ref({ escalationPhone: '' })

const messages = ref([])
const draft = ref('')
const busy = ref(false)
const feedbackBusy = ref(false)
const healthOk = ref(false)
const healthLabel = ref('未检查')
const knowledgeCount = ref('-')
const searchQuery = ref('受限空间作业注意事项')
const searchResults = ref([])
const docTitle = ref('')
const docContent = ref('')
const messageList = ref(null)
const sidebarRef = ref(null)
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [] })
const lastResponse = ref(null)
const evalData = ref(null)
const toast = ref('')
let toastTimer
let messageSequence = 0

// 响应式窗口宽度：缩放窗口时桌面/手机布局实时切换
const windowWidth = ref(window.innerWidth)
window.addEventListener('resize', () => { windowWidth.value = window.innerWidth })
const isDesktopChat = computed(() => activeView.value === 'chat' && windowWidth.value >= 980)
const currentBackend = computed(() => backendMeta(settings.backend, settings))
const activeAlerts = computed(() => monitorData.value.active_alerts || [])
const agentCount = computed(() => Object.keys(monitorData.value.agent_stats || {}).length)
const totalRequests = computed(() => Object.values(monitorData.value.agent_stats || {}).reduce((sum, item) => sum + Number(item.total || 0), 0))

watch(devMode, value => localStorage.setItem('safetymind.dev', value ? '1' : '0'))
watch(accessToken, v => localStorage.setItem('safetymind.accessToken', v || ''))
watch(() => settings.conversationId, persist)

onMounted(async () => {
  refreshConsole()
  loadConversations()
  // 刷新后自动恢复当前会话的消息（conv_id 已持久化，消息从服务端历史拉回）
  if (settings.conversationId) {
    try {
      const data = await requestConversationMessages(settings.backend, settings, settings.userId, settings.conversationId)
      const msgs = data.messages || []
      if (msgs.length) {
        messages.value = msgs.map((m, i) => ({
          id: 'restore-' + i,
          role: m.role === 'assistant' ? 'assistant' : 'user',
          content: m.content,
          escalated: Boolean(m.escalated),
          requestId: m.request_id || '',
          critical: Boolean(m.escalated),
        }))
        scrollToBottom()
      }
    } catch { /* 历史接口失败则保持空态，不阻塞 */ }
  }
})

watch(() => settings.userId, loadConversations)
// 手机端每次打开历史抽屉都刷新列表
watch(historyOpen, open => { if (open) loadConversations() })
// 离开对话页（如去文档库）时收起历史面板，避免残留遮挡
watch(activeView, v => { if (v !== 'chat') historyOpen.value = false })

function persist() { saveSettings(settings) }

async function refreshConsole() {
  await Promise.allSettled([checkHealth(), loadStats(), loadMonitor(), loadSkills(), loadConfig(), loadLastEval()])
}

// 评测页加载时拉取最近一次评测报告（后端持久化于 data/eval/last_report.json），
// 页面不再空白；后端返回空态结构（available=false）时保持空态展示。
async function loadLastEval() {
  try {
    const data = await requestLastEval(settings.backend, settings)
    evalData.value = (data && data.available === false) ? null : data
  } catch { /* 后端不可用时保持空态 */ }
}

async function loadConfig() {
  try {
    const data = await requestConfig(settings.backend, settings)
    config.value = { escalationPhone: String(data.escalation_phone || '').trim() }
  } catch { config.value = { escalationPhone: '' } }
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings.backend, settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
  } catch {
    healthOk.value = false
    healthLabel.value = '不可用'
  }
}

async function loadStats() {
  try {
    const data = await requestKnowledgeStats(settings.backend, settings)
    knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch { knowledgeCount.value = '-' }
}

async function loadMonitor() {
  try { monitorData.value = await requestMonitor(settings.backend, settings) }
  catch { monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] } }
}

async function loadSkills() {
  try { skillsData.value = await requestSkills(settings.backend, settings) }
  catch { skillsData.value = { count: 0, skills: [], errors: [] } }
}

async function reloadSkillSet() {
  busy.value = true
  try {
    skillsData.value = await reloadSkills(settings.backend, settings)
    showToast('Skills 已重新加载')
  } catch { showToast('Skills 加载失败') }
  finally { busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  resetGrow()
  busy.value = true
  try {
    // 等待响应期间刷新页面也不丢会话：发请求前先本地生成并持久化 conv_id，
    // 否则响应未返回时 conversationId 尚未落 localStorage，刷新后会话失联。
    // 必须在 try 内：newConvId/persist 一旦抛错，finally 才能解除 busy（否则永久卡死）
    if (!settings.conversationId) {
      settings.conversationId = newConvId()
      persist()
    }
    const response = await requestChat(settings.backend, settings, content)
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    lastResponse.value = response
    messages.value.push({
      id: createMessageId(),
      role: 'assistant',
      content: response.response,
      escalated: response.escalated,
      requestId: response.requestId || '',
      critical: response.escalated,
    })
    loadMonitor()
    loadConversations()   // 新会话建立/新消息落库后，历史会话列表实时刷新（无需手动刷新页面）
  } catch (error) {
    messages.value.push({ id: createMessageId(), role: 'assistant', content: `请求失败：${error.message}\n请检查网络后重试；紧急情况请直接拨打值班电话。` })
  } finally {
    busy.value = false
    scrollToBottom()
  }
}

function usePrompt(prompt) { draft.value = prompt; autoGrow({ target: null }) }

function confirmGenerated() {
  generatedToken.value = ''
  loadAdminConfig()
}

function forgetAdmin() {
  localStorage.removeItem('safetymind.adminToken')
  adminToken.value = ''
  adminCfg.value = []
  adminInputOpen.value = false
  showToast('已退出管理：此浏览器不再记住管理密码')
}

function newConversation() {
  messages.value = []
  lastResponse.value = null
  settings.conversationId = ''
  persist()
  historyOpen.value = false
  showToast('已开始新对话')
}

async function loadConversations() {
  try {
    const data = await requestConversations(settings.backend, settings, settings.userId)
    conversations.value = data.conversations || []
  } catch { conversations.value = [] }
}

async function openConversation(item) {
  historyOpen.value = false
  try {
    const data = await requestConversationMessages(settings.backend, settings, settings.userId, item.conv_id)
    settings.conversationId = item.conv_id
    persist()
    lastResponse.value = null
    messages.value = (data.messages || []).map((m, i) => ({
      id: `hist-${item.conv_id}-${i}`,
      role: m.role === 'assistant' ? 'assistant' : 'user',
      content: m.content,
      escalated: Boolean(m.escalated),
      requestId: m.request_id || '',
      critical: Boolean(m.escalated),
    }))
    activeView.value = 'chat'
    scrollToBottom()
    showToast(`已恢复：${item.title}`)
  } catch { showToast('历史会话读取失败') }
}

async function removeConversation(item) {
  try {
    await requestDeleteConversation(settings.backend, settings, settings.userId, item.conv_id)
    if (item.conv_id === settings.conversationId) newConversation()
    loadConversations()
    showToast('已删除该会话')
  } catch { showToast('删除失败') }
}

function formatHistTime(ts) {
  const n = Number(ts || 0)
  if (!n) return ''
  const d = new Date(n * 1000)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (sameDay) return `今天 ${hm}`
  const days = Math.floor((today - d) / 86400000)
  if (days === 0) return `昨天 ${hm}`
  if (days < 7) return `${days} 天前`
  return `${d.getMonth() + 1}/${d.getDate()}`
}

function escalateInChat() {
  escalationOpen.value = false
  draft.value = '这个问题我需要人工处理，请转人工。'
  activeView.value = 'chat'
}

function openDocs() {
  const url = `${currentBackend.value.baseUrl}/docs`
  if (window.pywebview?.api?.open_external) window.pywebview.api.open_external(url)
  else window.open(url, '_blank', 'noopener')
}

async function searchKnowledge() {
  busy.value = true
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value, 5)
    searchResults.value = data.results || []
    if (!searchResults.value.length) showToast('没有检索到相关内容')
  } catch (error) { showToast(error.message || '检索失败，请检查连接') }
  finally { busy.value = false }
}

async function submitKnowledge() {
  busy.value = true
  try {
    await addKnowledge(settings.backend, settings, [{ title: docTitle.value.trim(), content: docContent.value.trim() }])
    await loadStats()
    docTitle.value = ''
    docContent.value = ''
    showToast('文档已添加，助手现在可以检索它')
  } catch (error) { showToast(error.message || '文档导入失败') }
  finally { busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  busy.value = true
  try {
    await uploadKnowledge(settings.backend, settings, file)
    await loadStats()
    showToast(`${file.name} 已入库`)
  } catch (error) { showToast(error.message || '文件导入失败（支持 txt / md / json）') }
  finally { busy.value = false }
}

async function runEvaluation() {
  busy.value = true
  try {
    evalData.value = await requestEvaluation(settings.backend, settings)
    showToast('评测完成')
  } catch (error) { showToast(error.message || '评测运行失败') }
  finally { busy.value = false }
}

function copyText(text, tip) {
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(() => showToast(tip), () => showToast(text))
  else showToast(text)
}

// 回答反馈（Skill 自进化一期的证据源）：👍/👎 即 POST /feedback，负反馈后台提炼候选
async function sendFeedback(item, rating) {
  if (feedbackBusy.value) return
  feedbackBusy.value = true
  try {
    await requestFeedback(settings.backend, settings, {
      request_id: item.requestId || '',
      rating,
      user_id: settings.userId || 'anonymous',
      conv_id: settings.conversationId || '',
      message_head: (messages.value[Math.max(0, messages.value.indexOf(item) - 1)]?.content || '').slice(0, 120),
      answer_head: (item.content || '').slice(0, 200),
    })
    item.feedbackSent = true
    showToast(rating === 'up' ? '感谢认可，已记录' : '已记录，会用来改进回答规则')
  } catch (e) {
    showToast('反馈提交失败，请稍后重试')
  } finally {
    feedbackBusy.value = false
  }
}

function autoGrow(event) {
  const el = event?.target
  const area = el || document.querySelector('.m-composer textarea')
  if (!area) return
  area.style.height = 'auto'
  area.style.height = Math.min(area.scrollHeight, 132) + 'px'
}

function resetGrow() {
  const area = document.querySelector('.m-composer textarea')
  if (area) area.style.height = 'auto'
}

function onEnter(event) {
  if (event.isComposing) return  // 中文输入法回车选词不发送
  sendMessage()
}

function scrollToBottom() {
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

// conv_id 生成：crypto.randomUUID 仅安全上下文（HTTPS/localhost）可用，
// 线上纯 HTTP+IP 部署没有该函数（实测 TypeError）——必须带 RFC4122 v4 手写兜底
function newConvId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
