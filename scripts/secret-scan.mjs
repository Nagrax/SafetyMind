/**
 * 密钥扫描核心（**无 git 依赖**，可独立测试）。
 *
 * 为什么要拆出来：`scripts/git-round.mjs` 里的扫描逻辑原本和 git 操作缠在一起，
 * 而 git 操作在受限环境里无法被测试覆盖（子进程捕获管道被禁），
 * 于是「守卫是否真的拦得住」从来没被验证过。拆成独立模块后：
 *   - 核心扫描逻辑可被 tests/test_git_guard.py 端到端验证（不需要 git）；
 *   - git-round.mjs 只负责编排，两边职责清晰。
 *
 * 用法（CLI，便于从任何语言调用）：
 *   node scripts/secret-scan.mjs <文件...>     # 扫描指定文件，命中则退出码 3
 *   node scripts/secret-scan.mjs --repo-root . # 扫描整个仓库（跳过 .git 等）
 *
 * 退出码：0 = 未命中；3 = 命中；6 = 规则文件缺失/非法（守卫形同虚设，必须中止）。
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const RULES_FILE = path.join('scripts', 'secret-patterns.json')
export const PRIVACY_RULES_FILE = path.join('scripts', 'privacy-patterns.json')

/** 已知**非凭据**的豁免字面量（确属测试夹具，不含任何真实密钥）。 */
export const ALLOW_LITERALS = new Set([
  'sk-original-key-123456',
  'sk-new-key-987654',
])

/** 守卫自身与规则文件：含形态字面量，扫描时跳过。 */
export const SELF_FILES = new Set([
  path.join('scripts', 'git-round.mjs'),
  path.join('scripts', 'secret-scan.mjs'),
  path.join('scripts', 'secret-patterns.json'),
  path.join('scripts', 'privacy-patterns.json'),
])

const SKIP_DIRS = new Set([
  '.git', '__pycache__', 'node_modules', '.venv', 'venv', 'data', 'logs',
  'frontend/dist', 'dist', 'build', '.pytest_cache', 'hf_cache',
])

const MAX_BYTES = 4 * 1024 * 1024

/** 读取规则文件；失败时抛错（调用方必须中止，不能放行）。 */
export function loadRules(repoRoot = process.cwd()) {
  const abs = path.join(repoRoot, RULES_FILE)
  if (!existsSync(abs)) {
    throw new Error(`缺少密钥特征规则文件：${RULES_FILE}`)
  }
  let data
  try {
    data = JSON.parse(readFileSync(abs, 'utf8'))
  } catch (e) {
    throw new Error(`密钥特征规则文件不是合法 JSON：${RULES_FILE}（${e.message}）`)
  }
  const rules = []
  for (const p of data.patterns || []) {
    let re
    try {
      re = new RegExp(p.pattern, p.flags || 'g')
    } catch (e) {
      throw new Error(`密钥特征规则编译失败（${p.id}）：${e.message}`)
    }
    rules.push({ id: p.id, why: p.why || '', re })
  }
  if (!rules.length) {
    throw new Error('密钥特征规则为空：守卫形同虚设')
  }
  return rules
}

/**
 * 读取**隐私**规则文件。与密钥规则分开，因为处置方式不同：
 * 密钥必须拦停；隐私多数只需「公开前改写」（本机路径在评测脚本里常有其用途）。
 */
export function loadPrivacyRules(repoRoot = process.cwd()) {
  const abs = path.join(repoRoot, PRIVACY_RULES_FILE)
  if (!existsSync(abs)) {
    throw new Error(`缺少隐私特征规则文件：${PRIVACY_RULES_FILE}`)
  }
  let data
  try {
    data = JSON.parse(readFileSync(abs, 'utf8'))
  } catch (e) {
    throw new Error(`隐私特征规则文件不是合法 JSON：${PRIVACY_RULES_FILE}（${e.message}）`)
  }
  const rules = []
  for (const p of data.patterns || []) {
    let re
    try {
      re = new RegExp(p.pattern, p.flags || 'g')
    } catch (e) {
      throw new Error(`隐私特征规则编译失败（${p.id}）：${e.message}`)
    }
    rules.push({
      id: p.id,
      why: p.why || '',
      severity: p.severity === 'block' ? 'block' : 'warn',
      allowPaths: new Set(p.allow_paths || []),
      re,
    })
  }
  if (!rules.length) {
    throw new Error('隐私特征规则为空：隐私守卫形同虚设')
  }
  return rules
}

/**
 * 扫描一段文本，返回隐私命中列表。
 * severity=block → 必须处理；severity=warn → 建议改写。
 * allow_paths 命中的文件内允许出现该形态。
 */
export function scanPrivacyText(text, file, rules) {
  const hits = []
  const norm = String(file).replace(/\\/g, '/')
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue
    for (const rule of rules) {
      if (rule.allowPaths.has(norm)) continue
      rule.re.lastIndex = 0
      const m = rule.re.exec(line)
      if (!m) continue
      hits.push({
        file: norm,
        rule: rule.id,
        why: rule.why,
        severity: rule.severity,
        sample: masked(m[0]),
      })
    }
    if (hits.length) break
  }
  return hits
}

/** 扫描一组文件的隐私命中。 */
export function scanPrivacyFiles(files, repoRoot, rules) {
  const hits = []
  for (const f of files) {
    const norm = String(f).replace(/\\/g, '/')
    if (SELF_FILES.has(norm)) continue
    const text = readTextSafe(repoRoot, norm)
    if (text === null) continue
    hits.push(...scanPrivacyText(text, norm, rules))
  }
  return hits
}

/** 人类可读的隐私命中报告。 */
export function formatPrivacyHits(hits) {
  const lines = ['✖ 检测到隐私信息形态：']
  for (const h of hits) {
    lines.push(`  ${h.file}  [${h.rule}/${h.severity}] ${h.sample}`)
    if (h.why) lines.push(`      ${h.why}`)
  }
  lines.push('  → block 级：公开前必须改写（换环境变量/占位符/相对路径）。')
  lines.push('    warn 级：本机绝对路径与弱默认口令，建议改为相对仓库根或环境变量。')
  return lines.join('\n')
}

/** 打码：让用户认得出是哪个 key，又不把密钥明文写进日志或 CI 输出。 */
export function masked(s) {
  if (s.length <= 8) return `${s.slice(0, 2)}***`
  return `${s.slice(0, 4)}…${s.slice(-2)}（len=${s.length}）`
}

/**
 * 扫描一段文本，返回命中列表（每条规则最多报一处，避免刷屏）。
 * 命中样例一律打码。
 */
export function scanText(text, file, rules, allow = ALLOW_LITERALS) {
  const hits = []
  for (const line of String(text).split('\n')) {
    if (!line.trim()) continue
    for (const rule of rules) {
      rule.re.lastIndex = 0
      const m = rule.re.exec(line)
      if (!m) continue
      const matched = m[0]
      if (allow.has(matched)) continue
      if (matched.startsWith('sk-original-')) continue
      hits.push({ file, rule: rule.id, why: rule.why, sample: masked(matched) })
    }
    if (hits.length) break
  }
  return hits
}

/** 读文件为文本；二进制/超大/读不了返回 null（跳过，不当成干净）。 */
export function readTextSafe(repoRoot, file) {
  try {
    const abs = path.join(repoRoot, file)
    if (!existsSync(abs)) return null
    const st = statSync(abs)
    if (!st.isFile()) return null
    if (st.size > MAX_BYTES) return null
    const buf = readFileSync(abs)
    if (buf.includes(0)) return null
    return buf.toString('utf8')
  } catch {
    return null
  }
}

/** 扫描一组文件（相对 repoRoot 的 posix 路径）。 */
export function scanFiles(files, repoRoot, rules, allow = ALLOW_LITERALS) {
  const hits = []
  for (const f of files) {
    const norm = String(f).replace(/\\/g, '/')
    if (SELF_FILES.has(norm)) continue
    const text = readTextSafe(repoRoot, norm)
    if (text === null) continue
    hits.push(...scanText(text, norm, rules, allow))
  }
  return hits
}

/** 递归列出仓库里可扫描的文件（跳过 .git/依赖/产物目录）。 */
export function listRepoFiles(repoRoot, subdir = '') {
  const out = []
  const walk = (rel) => {
    const abs = path.join(repoRoot, rel)
    let entries
    try {
      entries = readdirSync(abs, { withFileTypes: true })
    } catch {
      return
    }
    for (const e of entries) {
      const r = rel ? path.join(rel, e.name) : e.name
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name)) continue
        walk(r)
      } else if (e.isFile()) {
        out.push(r.replace(/\\/g, '/'))
      }
    }
  }
  walk(subdir)
  return out
}

/** 人类可读的命中报告。 */
export function formatHits(hits) {
  const lines = ['✖ 检测到疑似密钥：']
  for (const h of hits) {
    lines.push(`  ${h.file}  [${h.rule}] ${h.sample}`)
    if (h.why) lines.push(`      ${h.why}`)
  }
  lines.push('  → 处理：把凭据移到环境变量/仓库 .env（.env 已在 .gitignore），不要写进代码或文档。')
  return lines.join('\n')
}

// ── CLI ─────────────────────────────────────────────────────────────────────
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  const argv = process.argv.slice(2)
  let repoRoot = process.cwd()
  let mode = 'secret'
  const files = []
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--repo-root' && argv[i + 1]) { repoRoot = path.resolve(argv[i + 1]); i++; continue }
    if (argv[i] === '--privacy') { mode = 'privacy'; continue }
    files.push(argv[i])
  }

  if (mode === 'privacy') {
    let rules
    try {
      rules = loadPrivacyRules(repoRoot)
    } catch (e) {
      console.error(`✖ ${e.message}`)
      process.exit(6)
    }
    const targets = files.length ? files : listRepoFiles(repoRoot)
    const hits = scanPrivacyFiles(targets, repoRoot, rules)
    const block = hits.filter(h => h.severity === 'block')
    const warn = hits.filter(h => h.severity !== 'block')
    if (block.length) {
      console.error(formatPrivacyHits(block))
      process.exit(7)
    }
    console.log(`✔ 未命中隐私 block 级形态：扫描 ${targets.length} 个文件（warn 级 ${warn.length} 处）`)
    if (warn.length) {
      for (const h of warn.slice(0, 10)) console.log(`  ⚠ ${h.file}  [${h.rule}] ${h.sample}`)
      if (warn.length > 10) console.log(`  …还有 ${warn.length - 10} 处`)
    }
    process.exit(0)
  }

  let rules
  try {
    rules = loadRules(repoRoot)
  } catch (e) {
    console.error(`✖ ${e.message}`)
    process.exit(6)
  }
  const targets = files.length ? files : listRepoFiles(repoRoot)
  const hits = scanFiles(targets, repoRoot, rules)
  if (hits.length) {
    console.error(formatHits(hits))
    process.exit(3)
  }
  console.log(`✔ 未命中密钥形态：扫描 ${targets.length} 个文件`)
  process.exit(0)
}
