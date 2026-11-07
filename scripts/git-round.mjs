#!/usr/bin/env node
/**
 * 一轮闭环的 git 自动化：敏感文件守卫 → 密钥扫描 → 提交 → 推送。
 *
 * 用法（在仓库根目录或任意子目录）:
 *   node scripts/git-round.mjs -m "feat(x): 改了什么 + 实测数字"
 *   node scripts/git-round.mjs -m "..." --no-push     # 只提交不推送
 *   node scripts/git-round.mjs --check                # 只做守卫与密钥扫描
 *
 * 设计原则（2026-10-29 用户授权 commit/push 后固化）：
 *   1. **绝不提交敏感文件**：.env / .env.* / data/ / logs/ / *.xlsx / 评测产物 / 虚拟环境；
 *   2. **代码里不得有密钥**：命中 scripts/secret-patterns.json 任一形态直接中止；
 *   3. 无变更时静默成功（退出码 0），不产生空提交；
 *   4. 推送失败不算成功——退出码非 0，交给调用方处理（不要谎报已同步）。
 *
 * 2026-10-30 安全加固（真实泄露事件驱动）：
 *   - 密钥特征抽到 **scripts/secret-patterns.json**（单一事实源），扫描核心抽到
 *     **scripts/secret-scan.mjs**（无 git 依赖，可被 tests/test_git_guard.py 端到端验证）；
 *   - 扫描范围从「候选文件」扩到 **全部已跟踪文件 + 候选文件**——此前只扫本次变更，
 *     于是**已入库的历史密钥扫不出来**（真实 key 就是这样漏过去的）；
 *   - 补齐此前缺失的形态：中继 32hex.20、sk-ws-、glpat-、AKIA、AIza、xox、微信 AppID；
 *   - 模式本身是 JS 正则字面量，本文件不再内嵌任何密钥形态的样例。
 * 2026-11-07 SM-00 加固：提交前文件 allowlist（--allow <file>）。
 *   背景：add -A 会把工作树里**所有**未提交改动卷进提交（2026-11-06 真实事故），
 *   清单打印是事后诸葛亮。--allow 把"本次提交允许哪些文件"变成**前置硬门禁**：
 *   变更集（staged+unstaged+untracked 逐文件）里任何清单外路径都在 git add 之前
 *   以退出码 8 拒绝；--check 模式同样校验。不带 --allow 时行为与原来完全一致。
 */
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import {
  formatHits, formatPrivacyHits, loadPrivacyRules, loadRules, scanFiles, scanPrivacyFiles,
} from './secret-scan.mjs'

const args = process.argv.slice(2)
const getArg = (name, dflt = '') => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt
}
const message = getArg('-m', getArg('--message', ''))
const noPush = args.includes('--no-push')
const checkOnly = args.includes('--check')
const allowFile = getArg('--allow')

const git = (cmdArgs, opts = {}) =>
  execFileSync('git', cmdArgs, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], ...opts }).trim()

const repoRoot = git(['rev-parse', '--show-toplevel'])
process.chdir(repoRoot)

// ── 1. 敏感路径守卫 ──────────────────────────────────────────────────────────
const FORBIDDEN = [
  /^\.env$/, /^\.env\.[^/]+$/, /^\.env\.bak/,
  /^data\//, /^logs\//, /\.xlsx$/i, /\.sqlite3?$/,
  /^benchmarks\/results\//, /^benchmarks\/intent\/results\//,
  /^\.venv\//, /^hf_cache\//, /^__pycache__\//, /node_modules\//,
  /^_acl_recovery\//, /\.pyc$/,
  // 凭据落盘文件：任何形态都不入库（含本机备份）
  /^\.env\.local$/, /\.pem$/i, /\.p12$/i, /\.pfx$/i, /\.keystore$/i
]
const allowed = (f) => f === '.env.example'

function listCandidates() {
  const out = git(['ls-files', '--others', '--exclude-standard', '--cached', '--modified'])
  return [...new Set(out.split('\n').map(s => s.trim()).filter(Boolean))]
}

const candidates = listCandidates()
const bad = candidates.filter(f => !allowed(f) && FORBIDDEN.some(re => re.test(f)))
if (bad.length) {
  console.error('✖ 拒绝提交敏感路径：\n  ' + bad.join('\n  '))
  process.exit(2)
}

// ── 1b. 文件 allowlist（--allow <file>）：变更超出清单就在 git add 之前失败 ──
// 清单格式：# 注释；每行一个仓库根相对路径；目录以 / 结尾（前缀匹配）。
// 变更集用 status --porcelain -uall（逐文件枚举 untracked，含 staged 与 rename 两端）。
if (allowFile) {
  let entries
  try {
    entries = readFileSync(allowFile, 'utf8').split(/\r?\n/)
      .map(s => s.trim()).filter(s => s && !s.startsWith('#'))
  } catch (e) {
    console.error(`✖ --allow 清单不可读: ${allowFile}`)
    process.exit(8)
  }
  // core.quotepath 默认把非 ASCII 路径转成八进制转义，allowlist（UTF-8 原名）会匹配不上；
  // git() 帮助函数会 trim 整个输出，porcelain 首行 " M path" 的前导空格被吃掉后，
  // 行内就没有"状态位+分隔空格"的形态了（踩过：'d' 被砍成 'ocs/'、整行被丢弃）。
  // 因此这里必须用原始 execFileSync（不 trim）+ 两位状态正则逐行解析。
  const statusOut = execFileSync('git', ['-c', 'core.quotepath=off', 'status', '--porcelain', '-uall'],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  const changed = statusOut.split('\n').map(l => {
    const m = /^(..)\s+(.+)$/.exec(l)
    return m ? m[2].replace(/^"|"$/g, '') : null
  }).filter(Boolean)
  const changedFiles = [...new Set(changed.flatMap(f => f.includes(' -> ') ? f.split(' -> ') : [f]))]
  const offenders = changedFiles.filter(f => {
    if (f.startsWith('..') || path.isAbsolute(f)) return true
    return !entries.some(e => e.endsWith('/') ? f.startsWith(e) : f === e)
  })
  if (offenders.length) {
    console.error('✖ 变更超出允许清单（--allow），已在 git add 之前拒绝：\n  ' + offenders.join('\n  '))
    console.error('  （把本卡要提交的文件写进清单；清单外改动请留给它的归属任务）')
    process.exit(8)
  }
  console.log(`• allowlist 校验通过：${changedFiles.length} 个变更文件都在清单内（${entries.length} 条规则）`)
}

// ── 2. 密钥扫描（全部已跟踪文件 + 候选文件）──────────────────────────────
// 关键：不能只扫「本次变更」——已入库的历史密钥正是这样漏过去的。
let RULES
try {
  RULES = loadRules(repoRoot)
} catch (e) {
  console.error(`✖ ${e.message}`)
  process.exit(6)
}

const tracked = git(['ls-files']).split('\n').map(s => s.trim()).filter(Boolean)
const scanTargets = [...new Set([...tracked, ...candidates])]
const secretHits = scanFiles(scanTargets, repoRoot, RULES)
if (secretHits.length) {
  console.error(formatHits(secretHits))
  process.exit(3)
}

// ── 2b. 隐私形态扫描（真实身份信息必须拦停；本机路径/弱默认口令只提示）──────
// 与密钥分开：隐私形态常在文档与评测脚本里有正当用途，一刀切会逼人绕过守卫。
let PRIVACY_RULES
try {
  PRIVACY_RULES = loadPrivacyRules(repoRoot)
} catch (e) {
  console.error(`✖ ${e.message}`)
  process.exit(6)
}
const privacyHits = scanPrivacyFiles(scanTargets, repoRoot, PRIVACY_RULES)
const privacyBlock = privacyHits.filter(h => h.severity === 'block')
const privacyWarn = privacyHits.filter(h => h.severity !== 'block')
if (privacyBlock.length) {
  console.error(formatPrivacyHits(privacyBlock))
  process.exit(7)
}

if (checkOnly) {
  console.log(`✔ 守卫通过：扫描 ${scanTargets.length} 个文件（全部已跟踪 + 候选），无敏感路径、无密钥、无隐私信息`)
  if (privacyWarn.length) {
    console.log(`⚠ 隐私提示 ${privacyWarn.length} 处（不拦停，公开前建议改写）：`)
    for (const h of privacyWarn.slice(0, 10)) {
      console.log(`  ${h.file}  [${h.rule}] ${h.sample}`)
    }
    if (privacyWarn.length > 10) console.log(`  …还有 ${privacyWarn.length - 10} 处`)
  }
  process.exit(0)
}

// ── 3. 提交（无变更也继续，可能是上次推送失败的积压提交） ──────────────────
git(['add', '-A'])
const staged = git(['diff', '--cached', '--name-only'])
if (staged) {
  if (!message) {
    console.error('✖ 有变更但缺少提交信息：请用 -m "..."')
    process.exit(4)
  }
  // 提交前把本次会进提交的文件打印出来（2026-11-06 教训：add -A 会把上一个会话的
  // 未提交改动一起卷进"文档提交"，提交信息与实际内容不符）。超过 15 个文件时提示。
  const files = staged.split('\n').filter(Boolean)
  console.log(`• 本次提交 ${files.length} 个文件：`)
  for (const f of files.slice(0, 15)) console.log(`    ${f}`)
  if (files.length > 15) console.log(`    …还有 ${files.length - 15} 个`)
  const suspicious = files.filter(f => /^(core|api|agents|mcp|frontend\/src|tests)\//.test(f))
  if (suspicious.length && !/^feat|^fix|^test|^refactor|^perf/.test(message)) {
    console.log(`• 提示：提交信息以「${message.split(/[:(]/)[0]}」开头，但含 ${suspicious.length} 个代码/测试文件；`)
    console.log('  若不是有意的，先 git restore --staged 再提交（不会自动中止）')
  }
  git(['commit', '-m', message])
  const sha = git(['rev-parse', '--short', 'HEAD'])
  console.log(`✔ 已提交 ${sha}：${message.split('\n')[0]}`)
} else {
  console.log('• 无新变更，继续检查未推送提交')
}

// ── 4. 推送（本机 DNS 偶发解析失败，重试两次；失败必须非 0 退出）────────────
if (noPush) {
  console.log('• --no-push：仅本地提交')
  process.exit(0)
}
let ahead = 0
try {
  ahead = Number(git(['rev-list', '--count', '@{u}..HEAD']) || 0)
} catch {
  ahead = 1   // 没有上游分支时也要推
}
if (ahead === 0) {
  console.log('✔ 已与远端同步（无待推送提交）')
  process.exit(0)
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
let lastErr = null
for (let attempt = 1; attempt <= 3; attempt++) {
  try {
    git(['push', 'origin', 'HEAD'])
    const upstream = git(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'])
    const sha = git(['rev-parse', '--short', 'HEAD'])
    console.log(`✔ 已推送 ${sha} → ${upstream}（待推送 ${ahead} 个提交）`)
    process.exit(0)
  } catch (e) {
    lastErr = e
    if (attempt < 3) {
      console.log(`• 第 ${attempt} 次推送失败，3 秒后重试（本机 DNS 偶发）`)
      await sleep(3000)
    }
  }
}
console.error('✖ 提交已在本地但推送失败（不要当作已同步）：')
console.error((lastErr?.stderr || lastErr?.stdout || lastErr?.message || '').toString().trim().slice(0, 500))
process.exit(5)
