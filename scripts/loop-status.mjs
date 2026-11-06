#!/usr/bin/env node
/**
 * 闭环状态一眼看清：台账 / 门禁 / git 同步 / 敏感守卫。
 *
 * 用法:
 *   node scripts/loop-status.mjs              # 人读摘要
 *   node scripts/loop-status.mjs --json       # 机器读（供 agent 解析）
 *
 * 设计要点（给接手者）：
 *   - 台账在**仓库外**（.zcode/workflows/safetymind-closed-loop），因此用相对路径
 *     `../.zcode/...` 定位，不写死本机绝对路径；可用 SAFETYMIND_LOOP_DIR 覆盖。
 *   - 任何一项读不到都如实输出 `unknown`，不要假装有数据（本脚本是给判断用的）。
 */
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'

const JSON_OUT = process.argv.includes('--json')

const run = (cmd, args, opts = {}) => {
  try {
    return execFileSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], ...opts }).trim()
  } catch {
    return ''
  }
}

const repoRoot = run('git', ['rev-parse', '--show-toplevel']) || process.cwd()
process.chdir(repoRoot)

const loopDir = process.env.SAFETYMIND_LOOP_DIR || path.resolve(repoRoot, '..', '.zcode', 'workflows', 'safetymind-closed-loop')
const scratch = process.env.SAFETYMIND_SCRATCH || path.resolve(repoRoot, '..', 'scratch', 'sm-loop')

const out = {
  repo: repoRoot,
  loop_dir: loopDir,
  ledger: { ok: false },
  gate: { ok: false },
  git: {},
  guard: {},
}

// ── 台账 ────────────────────────────────────────────────────────────────────
const statePath = path.join(loopDir, 'STATE.json')
if (existsSync(statePath)) {
  try {
    const s = JSON.parse(readFileSync(statePath, 'utf8'))
    const findings = s.findings || []
    const count = (st) => findings.filter(f => (f.status || '') === st).length
    out.ledger = {
      ok: true,
      round: s.round ?? null,
      max_rounds: s.max_rounds ?? null,
      total: findings.length,
      done: count('done'),
      blocked: count('blocked'),
      pending: count('pending'),
      blocked_items: findings.filter(f => f.status === 'blocked')
        .map(f => ({ signature: f.signature || f.id, why: (f.blocked_reason || f.evidence || '').slice(0, 160) })),
      pending_items: findings.filter(f => f.status === 'pending')
        .map(f => ({ signature: f.signature || f.id, score: f.score ?? null })),
      updated_at: s.updated_at || null,
      converged: Boolean(s.convergence && s.convergence.converged),
    }
  } catch (e) {
    out.ledger = { ok: false, error: String(e).slice(0, 120) }
  }
}

// ── 最近一次门禁 ────────────────────────────────────────────────────────────
const runsDir = path.join(scratch, 'runs')
if (existsSync(runsDir)) {
  const runs = readdirSync(runsDir)
    .filter(f => f.endsWith('.json'))
    .map(f => ({ f, t: statSync(path.join(runsDir, f)).mtimeMs }))
    .sort((a, b) => b.t - a.t)
  if (runs.length) {
    try {
      const data = JSON.parse(readFileSync(path.join(runsDir, runs[0].f), 'utf8'))
      const results = data.results || data.scripts || []
      const failed = results.filter(r => (r.exit_code ?? r.code ?? 0) !== 0)
      out.gate = {
        ok: failed.length === 0,
        run: runs[0].f,
        when: new Date(runs[0].t).toISOString(),
        total: results.length || null,
        failed: failed.map(r => r.script || r.name || '?'),
        tag: data.tag || null,
      }
    } catch (e) {
      out.gate = { ok: false, error: String(e).slice(0, 120) }
    }
  }
}

// ── git ────────────────────────────────────────────────────────────────────
out.git = {
  branch: run('git', ['rev-parse', '--abbrev-ref', 'HEAD']),
  head: run('git', ['rev-parse', '--short', 'HEAD']),
  dirty: run('git', ['status', '--porcelain']).split('\n').filter(Boolean).length,
  remote_head: run('git', ['rev-parse', '--short', 'origin/main']),
  ahead: Number(run('git', ['rev-list', '--count', 'origin/main..HEAD']) || 0),
  behind: Number(run('git', ['rev-list', '--count', 'HEAD..origin/main']) || 0),
}

// ── 守卫（读规则文件，不发起网络/LLM） ──────────────────────────────────────
const patterns = path.join(repoRoot, 'scripts', 'secret-patterns.json')
const privacy = path.join(repoRoot, 'scripts', 'privacy-patterns.json')
out.guard = {
  secret_rules: existsSync(patterns) ? (JSON.parse(readFileSync(patterns, 'utf8')).patterns || []).length : 0,
  privacy_rules: existsSync(privacy) ? (JSON.parse(readFileSync(privacy, 'utf8')).patterns || []).length : 0,
  hint: 'node scripts/git-round.mjs --check',
}

if (JSON_OUT) {
  console.log(JSON.stringify(out, null, 2))
  process.exit(0)
}

const L = out.ledger
const G = out.gate
const g = out.git
console.log(`\n闭环状态  ${new Date().toISOString()}`)
console.log(`  台账     ${L.ok ? `第 ${L.round}/${L.max_rounds} 轮 · 共 ${L.total} 条（done ${L.done} / blocked ${L.blocked} / pending ${L.pending}）` : '不可读（检查 SAFETYMIND_LOOP_DIR）'}`)
if (L.ok && L.blocked) for (const b of L.blocked_items) console.log(`    ⛔ ${b.signature}\n       ${b.why}`)
if (L.ok && L.pending) for (const p of L.pending_items) console.log(`    ⏳ ${p.signature}`)
console.log(`  门禁     ${G.ok ? `全绿 ${G.total ?? ''} 个脚本（${G.run}）` : `未通过/未知：${G.run || '无记录'} 失败 ${G.failed?.length ?? '?'}`}`)
console.log(`  git      ${g.branch} @ ${g.head} · 未提交 ${g.dirty} · 与远端 ahead ${g.ahead} / behind ${g.behind}`)
console.log(`  守卫     ${out.guard.secret_rules} 类密钥规则 · ${out.guard.privacy_rules} 类隐私规则（提交前跑 ${out.guard.hint}）`)
console.log('')
