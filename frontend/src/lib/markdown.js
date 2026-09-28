// 极简 Markdown 解析（仅助手消息展示用）。
// 安全模型：本模块只产出**纯数据块/token**，绝不拼 HTML 字符串；
// App.vue 用 Vue 插值（{{ }}）渲染这些数据，框架自动转义，
// 因此不引入 v-html / innerHTML，从根上杜绝 XSS。
// 支持：# 标题(1-6级)、**加粗**、`行内代码`、``` 围栏代码块、无序/有序列表、
//       ---/*** 分隔线、段落与换行。
const FENCE_RE = /^\s*```+\w*\s*$/
const HEADING_RE = /^(#{1,6})\s+(.*)$/
const HR_RE = /^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/   // --- / *** / ___（含空格分隔）
const UL_RE = /^\s*[-*•]\s+(.+)$/
const OL_RE = /^\s*\d+[.、)]\s+(.+)$/

// 块级解析：文本 → [{t:'h'|'p'|'ul'|'ol'|'code', ...}]，全部为普通对象
export function parseMarkdown(text) {
  const lines = String(text ?? '').split(/\r?\n/)
  const blocks = []
  let para = []
  let list = null
  let code = null

  const flushPara = () => { if (para.length) { blocks.push({ t: 'p', text: para.join('\n') }); para = [] } }
  const flushList = () => { if (list) { blocks.push(list); list = null } }

  for (const line of lines) {
    if (code) {                      // 围栏内内容原样保留，不做任何解析
      if (FENCE_RE.test(line)) { blocks.push(code); code = null }
      else code.lines.push(line)
      continue
    }
    if (FENCE_RE.test(line)) { flushPara(); flushList(); code = { t: 'code', lines: [] }; continue }

    const heading = line.match(HEADING_RE)
    if (heading) { flushPara(); flushList(); blocks.push({ t: 'h', level: heading[1].length, text: heading[2].trim() }); continue }

    if (HR_RE.test(line)) { flushPara(); flushList(); blocks.push({ t: 'hr' }); continue }

    const ul = line.match(UL_RE)
    if (ul) {
      flushPara()
      if (!list || list.t !== 'ul') { flushList(); list = { t: 'ul', items: [] } }
      list.items.push(ul[1]); continue
    }
    const ol = line.match(OL_RE)
    if (ol) {
      flushPara()
      if (!list || list.t !== 'ol') { flushList(); list = { t: 'ol', items: [] } }
      list.items.push(ol[1]); continue
    }
    if (!line.trim()) { flushPara(); flushList(); continue }
    flushList()
    para.push(line)
  }
  if (code) blocks.push(code)        // 未闭合围栏按代码块收尾
  flushPara(); flushList()
  return blocks
}

// 行内解析：**加粗** / `行内代码`，换行输出 br 占位 token。
// 返回 [{t:'b'|'c'|'br'|'text', v?}]，文本一律交由 {{ }} 插值转义。
function lineTokens(text) {
  const tokens = []
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g
  let last = 0, m
  while ((m = re.exec(text))) {
    if (m.index > last) tokens.push({ t: 'text', v: text.slice(last, m.index) })
    tokens.push(m[1] !== undefined ? { t: 'b', v: m[1] } : { t: 'c', v: m[2] })
    last = m.index + m[0].length
  }
  if (last < text.length) tokens.push({ t: 'text', v: text.slice(last) })
  return tokens
}

export function inlineTokens(text) {
  const out = []
  String(text ?? '').split('\n').forEach((line, idx) => {
    if (idx) out.push({ t: 'br' })
    out.push(...lineTokens(line))
  })
  return out
}
