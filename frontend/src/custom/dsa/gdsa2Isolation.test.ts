// @vitest-environment node
/**
 * 批次 20261025gdsa2 的隔离 / 契约红线。
 *
 * 与 `gdsaIsolation.test.ts`（#211 的结构边界）分工：
 * - 那份守**包边界**（核心不许 import DSA、三 env 不许赋值）；
 * - 这份守**本拍新增的交付物**（向导清单、登记封装、前缀、compose profile）。
 *
 * 全部零网络、零密码、零真实 sidecar —— 只读源码与配置文件。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, it } from 'vitest'

const HERE = __dirname
const FRONTEND_ROOT = resolve(HERE, '../../..')
const REPO_ROOT = resolve(FRONTEND_ROOT, '..')

function readFrontend(rel: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, rel), 'utf8')
}

function readRepo(rel: string): string {
  return readFileSync(resolve(REPO_ROOT, rel), 'utf8')
}

/**
 * 只取字符串字面量内容（单/双引号），跳过注释。
 *
 * 红线要防的是**展示给用户的字**，注释里写「禁止出现 xx」是合法的、
 * 也是必要的（否则连禁令都写不了）。这套剥注释逻辑与 gdsaIsolation 同源。
 */
function stringLiterals(source: string): string[] {
  let code = ''
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
    code += ch
    i += 1
  }
  return [...code.matchAll(/'([^'\\\n]*)'|"([^"\\\n]*)"/g)].map(m => m[1] ?? m[2] ?? '')
}

// ── ① 启用向导检查清单 ────────────────────────────────────────

it('向导清单存在，且四步都钉在源码里', () => {
  const body = readFrontend('src/custom/dsa/DsaEnablementChecklist.tsx')
  expect(body).toContain('data-dsa-enablement-checklist')
  for (const id of ["id: 'env'", "id: 'compose'", "id: 'reachable'", "id: 'schedule'"]) {
    expect(body, `缺步骤 ${id}`).toContain(id)
  }
  // 第 1 步必须是 DSA_BASE_URL
  expect(body).toContain('DSA_BASE_URL')
})

it('向导给出的 compose 命令带 --profile dsa（生产默认关的落地说法）', () => {
  // 必须扫**字符串字面量**而不是原文：模块头注释里也引了这条命令做示例，
  // 子串搜索会在变异掉真正的常量之后仍然命中注释，变成假绿（变异 M17 抓到过）。
  const literals = stringLiterals(readFrontend('src/custom/dsa/DsaEnablementChecklist.tsx'))
  expect(literals).toContain('docker compose --profile dsa up --build')
})

it('「关闭后不应预期推送」必须带完整前提，不能只说关 profile', () => {
  const body = readFrontend('src/custom/dsa/DsaEnablementChecklist.tsx')
  // JSX 文本会被切进不同的 span，所以这里扫**渲染块原文**而不是字符串字面量
  const start = body.indexOf('生产默认关。')
  expect(start, '找不到「生产默认关」那段说明').toBeGreaterThan(-1)
  const block = body.slice(start - 200, start + 900)

  expect(block).toContain('不应再预期收到')
  // 关键：前提是两个 —— 只关 profile 不够（schedule.enabled 还开着照样会推）
  expect(block, '少了「关掉 profile」这个前提').toContain('关掉 profile')
  expect(block, '少了 schedule.enabled 这个前提').toContain('schedule.enabled')
  // 措辞必须是「不应预期」而不是「保证不会」
  expect(block).not.toContain('保证不会')
})

it('向导的自检只看 reachable，不拿 enabled 冒充就绪', () => {
  const body = readFrontend('src/custom/dsa/DsaEnablementChecklist.tsx')
  expect(body).toContain('status.data?.reachable')
  expect(body).not.toMatch(/enabled\s*===?\s*true/)
})

it('向导是只读的：不 fetch 写接口、不改 .env、不调 compose', () => {
  const body = readFrontend('src/custom/dsa/DsaEnablementChecklist.tsx')
  // 只允许通过 fetchDsaStatus 读状态；不得出现写动作
  for (const banned of [
    "method: 'POST'",
    "method: 'PUT'",
    "method: 'PATCH'",
    "method: 'DELETE'",
    'writeFile',
    'docker compose up"',
    'child_process',
  ]) {
    expect(body, `向导里不该出现 ${banned}`).not.toContain(banned)
  }
})

// ── ② 两个插槽降级（结构证据在 gdsaIsolation，这里钉文案不越界） ──

it('插槽降级文案不承诺「已就绪」类结论', () => {
  const ext = readFrontend('src/custom/dsa/extension.tsx')
  const literals = stringLiterals(ext)
  for (const text of literals) {
    for (const banned of ['已就绪', '已连接可用', '服务正常']) {
      expect(text).not.toContain(banned)
    }
  }
})

// ── ③ 登记狙击线客户端封装 ────────────────────────────────────

it('client 有 registerSniperLines，打到 /api/dsa/sniper-lines', () => {
  const client = readFrontend('src/custom/dsa/client.ts')
  expect(client).toContain('export function registerSniperLines')
  expect(client).toContain("'/api/dsa/sniper-lines'")
  expect(client).toContain("method: 'POST'")
})

it('SniperLineInput 字段与后端 SniperLineRequest 对齐', () => {
  const client = readFrontend('src/custom/dsa/client.ts')
  // 从接口声明里抠字段名，避免只靠易漂的行数
  const block = client.slice(
    client.indexOf('export interface SniperLineInput'),
    client.indexOf('export interface SniperLineResult'),
  )
  for (const field of ['symbol', 'name', 'analysis_id', 'analyzed_at', 'ideal_buy', 'stop_loss', 'take_profit']) {
    expect(block, `SniperLineInput 缺 ${field}`).toContain(field)
  }
})

it('成功判据只有一处定义：replaced===true 或 lines.length>0', () => {
  const client = readFrontend('src/custom/dsa/client.ts')
  expect(client).toContain('export function isSniperRegisterSuccess')
  expect(client).toContain('result.replaced === true || result.lines.length > 0')
})

it('后端 SniperLineRequest 与本拍对齐（路由契约零改）', () => {
  const routes = readRepo('backend/app/custom/dsa/routes.py')
  const block = routes.slice(routes.indexOf('class SniperLineRequest'), routes.indexOf('class ScheduleRequest'))
  for (const field of ['symbol', 'name', 'analysis_id', 'analyzed_at', 'ideal_buy', 'stop_loss', 'take_profit']) {
    expect(block, `后端 SniperLineRequest 缺 ${field}`).toContain(field)
  }
})

// ── ④ L4 三动作接到成功回调 ──────────────────────────────────

it('DecisionWorkspace 在成功回调里 bump，不在渲染或失败里 bump', () => {
  const ws = readFrontend('src/custom/dsa/DecisionWorkspace.tsx')
  // 三个 action 都要被 bump
  for (const action of ["bumpCoreAction('report_submit')", "bumpCoreAction('chat_send')", "bumpCoreAction('sniper_register')"]) {
    expect(ws, `缺 ${action}`).toContain(action)
  }
})

it('诊断区显示真实 L4 次数与枚举明细，不再写「槽位空」', () => {
  const ws = readFrontend('src/custom/dsa/DecisionWorkspace.tsx')
  expect(ws).toContain('data-dsa-funnel-l4-actions')
  expect(ws).toContain('readActionCount')
  expect(ws).not.toContain("'槽位空'")
})

// ── ⑤ alert_text 来源前缀 ─────────────────────────────────────

it('后端 alert_text 标题带可区分来源前缀', () => {
  const py = readRepo('backend/app/custom/dsa/sniper_alerts.py')
  expect(py).toContain('ALERT_SOURCE_PREFIX')
  expect(py).toMatch(/ALERT_SOURCE_PREFIX\s*=\s*"DSA价位"/)
  // 标题与正文都用到前缀
  const fnStart = py.indexOf('def alert_text(')
  const fnBody = py.slice(fnStart, py.indexOf('def drain('))
  expect(fnBody).toContain('f"{ALERT_SOURCE_PREFIX} {label} {name}"')
  expect(fnBody).toContain('ALERT_SOURCE_PREFIX')
})

it('限流合并明确标 design_pending，未被当成硬交付', () => {
  const py = readRepo('backend/app/custom/dsa/sniper_alerts.py')
  expect(py).toContain('design_pending')
})

// ── ⑥ compose profile 保留 ────────────────────────────────────

it('docker-compose 仍保留 profiles: ["dsa"]（未改成生产常开）', () => {
  const compose = readRepo('docker-compose.yml')
  const start = compose.indexOf('\n  dsa:')
  expect(start, 'docker-compose.yml 里找不到 dsa service').toBeGreaterThan(-1)

  // 从 `  dsa:` 起，到下一个**对齐到 2 空格的顶层键**（volumes:/networks:）为止。
  // 注意 `\w[\w-]*:` 会先撞上 `    user:` 这类 4 空格的行 —— 所以这里必须锚 `${'\n'}  `
  // （正好两个空格），而不是「两个空格开头即可」。变异 M21 就是这么漏掉的。
  const after = compose.slice(start + '\n  dsa:'.length)
  const stop = after.search(/\n {2}[a-z][\w-]*:\s*(?:\n|$)/)
  const block = stop === -1 ? after : after.slice(0, stop)
  expect(block.length).toBeGreaterThan(0)

  // 断言的是**这一行本身**，不是整篇 diff 里某处还有 dsa 字样
  expect(block).toMatch(/^\s+profiles:\s*\[\s*"dsa"\s*\]\s*$/m)
})

// ── ⑦ 假数红线（本拍新增文件也扫） ────────────────────────────

const BANNED = ['可达率', '打开率已', '使用率已', '已测通', '已达标', '正式可推']

it('本拍新增/改动的 DSA 文件里没有假数肯定句', () => {
  const targets = [
    'src/custom/dsa/funnelMetrics.ts',
    'src/custom/dsa/DsaEnablementChecklist.tsx',
    'src/custom/dsa/extension.tsx',
    'src/custom/dsa/client.ts',
    'src/custom/dsa/DecisionWorkspace.tsx',
  ]
  const offenders: string[] = []
  for (const rel of targets) {
    for (const text of stringLiterals(readFrontend(rel))) {
      if (BANNED.some(word => text.includes(word))) offenders.push(`${rel}: ${text}`)
    }
  }
  expect(offenders).toEqual([])
})

// ── ⑧ 合规脚注 ───────────────────────────────────────────────

it('本拍新增模块带合规脚注', () => {
  for (const rel of [
    'src/custom/dsa/DsaEnablementChecklist.tsx',
    'src/custom/dsa/extension.tsx',
  ]) {
    expect(readFrontend(rel), `${rel} 缺脚注`).toContain('仅供研究，不构成投资建议；执行须人工确认')
  }
  // 后端模块头也带
  expect(readRepo('backend/app/custom/dsa/sniper_alerts.py')).toContain('仅供研究，不构成投资建议；执行须人工确认')
})
