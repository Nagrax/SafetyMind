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
 *   2. **代码里不得有密钥**：命中 tlJIT…/sk-…/PRIVATE KEY/API_KEY=… 直接中止；
 *   3. 无变更时静默成功（退出码 0），不产生空提交；
 *   4. 推送失败不算成功——退出码非 0，交给调用方处理（不要谎报已同步）。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const getArg = (name, dflt = '') => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt
}
const message = getArg('-m', getArg('--message', ''))
const noPush = args.includes('--no-push')
const checkOnly = args.includes('--check')

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
  /^_acl_recovery\//, /\.pyc$/
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

// ── 2. 密钥扫描（候选文件 + 已暂存 diff） ────────────────────────────────────
// 密钥特征：真实中继 key 是「32 位 hex + . + 20 位」，测试夹具（sk-original-…）要放行
const SECRET = /tlJIT51|sk-(?!original-)[A-Za-z0-9]{24,}|BEGIN [A-Z ]*PRIVATE KEY|ANTHROPIC_API_KEY=[0-9a-f]{32}\.|ghp_[A-Za-z0-9]{30,}/
const SELF = 'scripts/git-round.mjs'   // 扫描器自身含正则字面量，跳过
function scanFiles(files) {
  const hits = []
  for (const f of files) {
    if (f === SELF) continue
    try {
      if (!existsSync(f) || !statSync(f).isFile()) continue
      const text = readFileSync(f, 'utf8')
      for (const line of text.split('\n')) {
        if (SECRET.test(line)) { hits.push(`${f}: ${line.trim().slice(0, 90)}`); break }
      }
    } catch { /* 二进制/无权限：跳过 */ }
  }
  return hits
}
const secretHits = scanFiles(candidates.filter(f => !allowed(f)))
if (secretHits.length) {
  console.error('✖ 检测到疑似密钥，已中止：\n  ' + secretHits.join('\n  '))
  process.exit(3)
}

if (checkOnly) {
  console.log(`✔ 守卫通过：${candidates.length} 个候选文件，无敏感路径、无密钥`)
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

