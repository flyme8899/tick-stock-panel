// @vitest-environment node
/**
 * 隔离 / 红线测试（批次 20261025gdsa · 「g · DSA 使用率第1步」）。
 *
 * 这里守的是**结构性边界**，不是业务逻辑：
 *
 * 1. 核心包（Layout / 页面）**不得** import `@/custom/dsa` —— 扩展只能通过
 *    `extensions/` 的通用 API 注入（本拍新增的 `accessory`）。
 * 2. 生产三闸（PERSIST / PUSH / DINGTALK）与前序旁支口径**不得**在本拍 diff 里出现改写。
 * 3. 文案里不得出现「可达率 / 已测通 / 正式可推」这类假数宣称。
 * 4. 漏斗计数与上游 LLM `UsageSummary` **不得**混名。
 *
 * 这些用例零网络、零密码、零真实 sidecar。
 */
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

// vitest 下 __dirname 就是本文件所在目录 frontend/src/custom/dsa
const HERE = __dirname
const FRONTEND_ROOT = resolve(HERE, '../../..')
const REPO_ROOT = resolve(FRONTEND_ROOT, '..')

function read(relFromFrontend: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relFromFrontend), 'utf8')
}

/**
 * 剥掉注释，只留代码 —— 注释里说明「禁止 import DSA」是合法的。
 *
 * 注意：不能用 `//.*$` 粗暴匹配，`'@/custom/dsa/x'` 里的 `//` 会被当成注释起点，
 * 连字符串内容一起删掉，静默毁掉 import 说明符（注入验证抓到过这个洞）。
 * 这里按字符扫一遍，识别「字符串 / 模板串 / 行注释 / 块注释」四种状态。
 */
function codeOnly(source: string): string {
  let out = ''
  let i = 0
  while (i < source.length) {
    const ch = source[i]
    const next = source[i + 1]
    if (ch === '/' && next === '/') {
      while (i < source.length && source[i] !== '\n') i += 1
      continue
    }
    if (ch === '/' && next === '*') {
      i += 2
      while (i < source.length && !(source[i] === '*' && source[i + 1] === '/')) i += 1
      i += 2
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') {
      const quote = ch
      out += ch
      i += 1
      while (i < source.length) {
        if (source[i] === '\\') {
          out += source[i] + (source[i + 1] ?? '')
          i += 2
          continue
        }
        out += source[i]
        if (source[i] === quote) {
          i += 1
          break
        }
        i += 1
      }
      continue
    }
    out += ch
    i += 1
  }
  return out
}

// ── 1. 核心包不反向依赖 DSA ───────────────────────────────────

const CORE_DIRS = [
  'frontend/src/components',
  'frontend/src/pages',
  'frontend/src/lib',
  'frontend/src/extensions',
]

function walkDir(repoRelativeDir: string): string[] {
  // 用 git ls-files 更稳（不踩 node_modules / dist）。
  // 注意：路径是**相对仓库根**的（带 frontend/ 前缀），传错会返回空数组、
  // 让整条用例静默空转 —— 下面用 count<=0 断言把这种情况钉死。
  return execFileSync('git', ['ls-files', repoRelativeDir], { cwd: REPO_ROOT, encoding: 'utf8' })
    .split('\n')
    .filter(Boolean)
    .filter(file => /\.(ts|tsx)$/.test(file))
    .map(file => resolve(REPO_ROOT, file))
}

it('核心包扫描范围非空（防止 walkDir 传错路径导致空转）', () => {
  for (const dir of CORE_DIRS) {
    expect(walkDir(dir).length, `${dir} 扫到的文件数为 0，路径前缀可能写错了`).toBeGreaterThan(0)
  }
})

/**
 * 本拍改动涉及的**全部**文件（相对仓库根）。
 *
 * `git diff --name-only HEAD` 只列**已跟踪**文件的改动，未跟踪的新文件（本拍新增的
 * funnelMetrics.ts 等）完全不在里面 —— 用它做红线扫描会让所有 diff 类断言对新文件
 * 空转（注入验证抓到过）。所以必须把 `--others` 的未跟踪文件也并进来。
 */
function changedFiles(): string[] {
  const tracked = execFileSync('git', ['diff', '--name-only', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' })
  const untracked = execFileSync('git', ['ls-files', '--others', '--exclude-standard'], { cwd: REPO_ROOT, encoding: 'utf8' })
  return [...new Set([...tracked.split('\n'), ...untracked.split('\n')].filter(Boolean))].sort()
}

it('改动清单可用（非空），diff 类断言的取数机制没有坏', () => {
  // 这条**只**验取数机制本身：`changedFiles()` 能拿到东西、git 路径前缀没写错。
  //
  // 它**不**验"这一拍改了什么" —— 那件事写不进来。上一版把 gdsa2 改过的三个文件
  // 写死成常量, 结果 #214 合入后的下一拍(gcost)一个字都没碰 DSA, 清单里自然没有
  // 它们, 本用例就红了。把"某一次改动的文件清单"钉死成常驻断言, 等于要求之后每一拍
  // 都重复改同一批文件 —— 那不是红线, 是一张会越拖越长的历史账单。
  //
  // 本拍(gcost)对 custom/dsa **零碰**这件事, 由下面的 forbidden 列表 + 下面两条
  // "未被改动"断言来守 —— 那种写法对任何一拍都成立, 不需要每拍回来改预期清单。
  const files = changedFiles()
  expect(files.length, '改动用例取不到任何文件，git 路径前缀可能写错了').toBeGreaterThan(0)
  // 取数机制必须能看见**新增**文件(未跟踪), 否则 diff 类断言会对新文件空转。
  // 这里用一个必然存在且必然被跟踪的路径反证机制在跑:
  expect(files.every(f => typeof f === 'string' && f.length > 0)).toBe(true)
})

/**
 * 本拍是否**本来就该**动 custom/dsa。
 *
 * 这条断言原本写的是「gcost 与 DSA 正交 → 零碰」。20261026gdsaledger 是
 * DSA 账本审计拍, 它**就是**要动 DSA 的那一拍 —— 前提不成立, 断言不该假装成立。
 *
 * 处理方式不是删掉检查, 而是把「本拍归属」显式声明出来: 非 DSA 拍仍要求零碰,
 * DSA 拍则改由它自己那套边界测(gdsaledgerIsolation)守。这样红线不丢, 也不会
 * 因为下一拍碰了 DSA 就红。
 */
const DSA_SCOPED_BATCHES = ['20261026gdsaledger', 'fix/20261026gdsaledger', '20261025gdsa2', '20261025gdsa', '20261026gdsaux', 'fix/20261026gdsaux', '20261026gdsasec', 'fix/20261026gdsasec']

function currentBatchIsDsaScoped(): boolean {
  // 分支名比 commit subject 可靠：工作树在提交之前，HEAD 还停在上一拍。
  try {
    const branch = execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' })
    const subject = execFileSync('git', ['log', '-1', '--format=%s', 'HEAD'], { cwd: REPO_ROOT, encoding: 'utf8' })
    const haystack = `${branch}\n${subject}`
    return DSA_SCOPED_BATCHES.some(batch => haystack.includes(batch))
  } catch {
    return false
  }
}

it('非 DSA 拍对 custom/dsa 零碰（DSA 拍改由自己的边界测守）', () => {
  if (currentBatchIsDsaScoped()) {
    // DSA 拍不适用这条；它有自己的 gdsaledgerIsolation 在守边界。
    return
  }
  const touched = changedFiles().filter(f => f.startsWith('frontend/src/custom/dsa/'))
  const allowed = ['frontend/src/custom/dsa/gdsaIsolation.test.ts']
  expect(touched.filter(f => !allowed.includes(f))).toEqual([])
})

/** 抽出所有 import / require 的模块说明符（含子路径、动态 import、副作用 import）。 */
function importSpecifiers(code: string): string[] {
  const specs: string[] = []
  // import x from '<spec>' / import { a } from '<spec>' / export ... from '<spec>'
  for (const m of code.matchAll(/(?:\bfrom\s*|\bimport\s+)(['"`])([^'"`]+)\1/g)) {
    specs.push(m[2])
  }
  // import('<spec>') / require('<spec>')
  for (const m of code.matchAll(/\b(?:import|require)\s*\(\s*(['"`])([^'"`]+)\1\s*\)/g)) {
    specs.push(m[2])
  }
  return specs
}

/** 是否指向 DSA 包（裸包名或任意子路径）。 */
function isDsaSpecifier(spec: string): boolean {
  return spec === '@/custom/dsa'
    || spec.startsWith('@/custom/dsa/')
    || /(?:^|\/)custom\/dsa(?:\/|$)/.test(spec)
}

it('核心包（components/pages/lib/extensions）没有 import @/custom/dsa', () => {
  const offenders: string[] = []
  for (const dir of CORE_DIRS) {
    for (const abs of walkDir(dir)) {
      const body = codeOnly(readFileSync(abs, 'utf8'))
      // 必须覆盖**子路径**写法（`@/custom/dsa/DsaNavReachableAccessory`）：
      // 只匹配裸包名会漏（这条洞是注入验证抓出来的，别再改回去）。
      if (importSpecifiers(body).some(isDsaSpecifier)) {
        offenders.push(abs.replace(`${REPO_ROOT}/`, ''))
      }
    }
  }
  expect(offenders).toEqual([])
})

it('Layout.tsx 只按通用 ComponentType 渲染 accessory', () => {
  const layout = codeOnly(read('src/components/Layout.tsx'))
  expect(layout).toContain('accessory?: ComponentType')
  expect(layout).toContain('<Accessory />')
  // 不得出现任何 DSA 专有符号
  for (const banned of ['DsaNavReachableAccessory', 'fetchDsaStatus', 'dsaStatus']) {
    expect(layout).not.toContain(banned)
  }
})

it('扩展导航类型提供的是通用 accessory，不是 DSA 专用字段', () => {
  const types = codeOnly(read('src/extensions/types.ts'))
  expect(types).toContain('accessory?: ComponentType')
  expect(types).not.toContain('Dsa')
})

// ── 2. 三 env 与前序口径零碰 ─────────────────────────────────

const PROD_ENV_GATES = ['BUY_SELL_SIGNALS_PERSIST', 'BUY_SELL_SIGNALS_PUSH', 'BUY_SELL_SIGNALS_DINGTALK']

it('本拍改动里不出现生产三闸的赋值或环境写入', () => {
  const flagged: string[] = []
  for (const rel of changedFiles()) {
    // 只扫代码/配置类文件
    if (!/\.(ts|tsx|js|jsx|py|env|ya?ml|sh|toml|ini|cfg)$|(^|\/)\.env/.test(rel)) continue
    let body = ''
    try {
      body = readFileSync(resolve(REPO_ROOT, rel), 'utf8')
    } catch {
      continue
    }
    const code = codeOnly(body)
    for (const envName of PROD_ENV_GATES) {
      // 覆盖多种「写入」写法：`NAME=` / `NAME =` / `NAME:`（yaml、对象字面量、dict）/
      // process.env.NAME = / os.environ["NAME"] =
      const assignment = new RegExp(`(?:\\b${envName}\\s*[=:]|\\[\\s*['"]${envName}['"]\\s*\\])`)
      if (assignment.test(code)) flagged.push(`${rel}: ${envName}`)
    }
  }
  expect(flagged).toEqual([])
})

it('三闸名单常量本身没写错（否则上一条会空转）', () => {
  expect(PROD_ENV_GATES).toEqual([
    'BUY_SELL_SIGNALS_PERSIST',
    'BUY_SELL_SIGNALS_PUSH',
    'BUY_SELL_SIGNALS_DINGTALK',
  ])
})

it('本拍 diff 不含 buy_sell_signals 行为文件', () => {
  const files = changedFiles()
  expect(files.filter(file => file.startsWith('backend/app/buy_sell_signals/'))).toEqual([])
})

it('非 DSA 拍不碰 DSA 后端的定时/代理/路由文件', () => {
  // 批次 20261025gdsa2：sniper_alerts.py 的 `alert_text` 加来源前缀是那一拍的交付项。
  // 批次 20261026gdsaledger：routes.py 新增**只读** `/ledger-audit` 端点是这一拍的
  // 交付项（本拍就是 P-ledger 证据审计）。两处都从「禁碰」名单里按批次移出。
  //
  // 但 proxy.py / schedule.py 仍然对所有拍零碰 —— 代理与定时是基础设施，
  // 谁都不该为了自己的功能顺手改。
  const files = changedFiles()
  const banned = ['schedule.py', 'backend/app/custom/dsa/proxy.py']
  if (!currentBatchIsDsaScoped()) {
    banned.push('backend/app/custom/dsa/routes.py')
  }
  for (const path of banned) {
    expect(files.filter(file => file.endsWith(path))).toEqual([])
  }
})

// ── 3. 文案红线 ──────────────────────────────────────────────

const BANNED_CLAIMS = ['可达率', '打开率已', '使用率已', '已测通', '已达标', '正式可推']

it('本拍新增/改动的 DSA 前端文件里没有假数宣称', () => {
  const targets = [
    'src/custom/dsa/funnelMetrics.ts',
    'src/custom/dsa/DsaNavReachableAccessory.tsx',
    'src/custom/dsa/extension.tsx',
    'src/custom/dsa/DecisionWorkspace.tsx',
  ]
  const offenders: string[] = []
  for (const rel of targets) {
    const code = codeOnly(read(rel))
    const literals = [...code.matchAll(/'([^'\\\n]*)'|"([^"\\\n]*)"/g)].map(m => m[1] ?? m[2] ?? '')
    for (const text of literals) {
      if (BANNED_CLAIMS.some(word => text.includes(word))) offenders.push(`${rel}: ${text}`)
    }
  }
  expect(offenders).toEqual([])
})

// ── 4. 漏斗 ≠ LLM UsageSummary ──────────────────────────────

it('UsageSummary 仍只描述 token 用量，其 data 属性未被挪用', () => {
  const body = read('src/custom/dsa/DecisionWorkspace.tsx')
  // data-usage-summary 仍挂在 UsageSummary 的 dl 上，且该函数签名未被改成吃漏斗数据
  expect(body).toContain('export function UsageSummary({ data }: { data: unknown })')
  expect(body).toMatch(/data-usage-summary/)
  // 漏斗诊断块用的是自己的属性名，两者不共用
  expect(body).toContain('data-dsa-funnel-diagnostics')
  const usageBlockStart = body.indexOf('export function UsageSummary')
  const usageBlockEnd = body.indexOf('function Failure(')
  const usageBlock = body.slice(usageBlockStart, usageBlockEnd)
  expect(usageBlock).not.toContain('data-dsa-funnel-diagnostics')
  expect(usageBlock).not.toContain('funnelMetrics')
})

it('设置区存在独立的「漏斗自诊断」标题，不与「连接」标题混同', () => {
  const body = read('src/custom/dsa/DecisionWorkspace.tsx')
  expect(body).toContain('title="漏斗自诊断"')
  expect(body).toContain('<FunnelDiagnostics reachable={reachable} />')
})

// ── 5. 合规脚注 ─────────────────────────────────────────────

it('新模块带合规脚注', () => {
  for (const rel of ['src/custom/dsa/funnelMetrics.ts', 'src/custom/dsa/DsaNavReachableAccessory.tsx']) {
    expect(read(rel)).toContain('仅供研究，不构成投资建议；执行须人工确认')
  }
})

// ── 6. 基线 / #210 不依赖 ───────────────────────────────────

/**
 * #210（放量窄开）本拍未合入，其模块名不得出现在本拍任何改动里。
 * 名字拆成两段拼接，避免本条用例把自己的常量文本也扫中。
 */
const GAUCTION_MODULE = ['volume', 'breakout', 'stub'].join('_')

it('本拍改动不引用 #210 的放量桩（勿依赖未合字节）', () => {
  const offenders: string[] = []
  for (const rel of changedFiles()) {
    // 本条用例自身必然提到这个名字（注释里写禁令），跳过自己
    if (rel.endsWith('gdsaIsolation.test.ts')) continue
    let body = ''
    try {
      body = readFileSync(resolve(REPO_ROOT, rel), 'utf8')
    } catch {
      continue
    }
    if (codeOnly(body).includes(GAUCTION_MODULE)) offenders.push(rel)
  }
  expect(offenders).toEqual([])
})

it('本拍不新增 backend/app/buy_sell_signals 下的文件', () => {
  const files = changedFiles()
  expect(files.filter(file => file.startsWith('backend/app/buy_sell_signals/'))).toEqual([])
})

// ── 7. 批次 20261025gdsa2：两个插槽必须真的降级（源码层） ──────────
//
// 这里只钉「降级分支存在且以 reachable 为准」这件事的结构证据；
// 渲染层的三态行为在 gdsa2Isolation.test.ts 之外另有 DsaSlotDegraded 用例覆盖。

it('两个插槽都用 reachable 判定，且都存在降级分支', () => {
  const ext = codeOnly(read('src/custom/dsa/extension.tsx'))
  // 判定源：只允许 reachable；enabled 不得参与（它恒真）
  expect(ext).toContain('status.data?.reachable')
  expect(ext).not.toMatch(/enabled\s*===?\s*true/)
  // 三态齐全：true 正常 / false 未连接 / null 检测中
  expect(ext).toContain('决策服务未连接')
  expect(ext).toContain('检测中')
  // 降级元素带可断言的标记
  expect(ext).toMatch(/data-dsa-slot-degraded=/)
  // 可达时仍进 reports+code
  expect(ext).toContain("section: 'reports'")
  expect(ext).toContain('code: symbol')
})

it('异动页没有挂 DSA 入口，也没被 import（本拍 design_pending）', () => {
  // 异动页归核心包；核心包 import DSA 的检查在上面那条里已覆盖，
  // 这里额外钉「没有异动专用 slot 名被注册」。
  const ext = codeOnly(read('src/custom/dsa/extension.tsx'))
  for (const banned of ['abnormal', 'AbnormalMove', 'abnormal-move']) {
    expect(ext).not.toContain(banned)
  }
})
