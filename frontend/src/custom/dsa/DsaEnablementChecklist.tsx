/**
 * DSA 启用向导 —— 检查清单（批次 20261025gdsa2 · 使用率后排）。
 *
 * 仅供研究，不构成投资建议；执行须人工确认。
 *
 * ## 它是什么，不是什么
 *
 * 这是一份**可点开的检查清单**：告诉你开 DSA 要改哪一行、跑哪一条命令、看哪个接口。
 * 它**不是**一键开关 —— 点击这里不会去动 `.env`、不会 `docker compose up`、
 * 不会替你把 sidecar 拉起来。理由很直白：
 *
 * - 生产**默认关**是刻意的。写个「一键开」按钮，等于给「误开」修了一条大路；
 * - 前端没有权限改后端 `.env`，也没有权限跑 compose（能跑才是事故）。
 *
 * 所以本组件只做两件正事：**给可复制的命令** + **就地自检 reachable**。
 *
 * ## 三步 + 一个可选
 *
 * | # | 步骤 | 判定 |
 * |---|---|---|
 * | 1 | 配 `DSA_BASE_URL` | 人工确认（页面不回显密钥） |
 * | 2 | `docker compose --profile dsa up --build` | 人工确认 |
 * | 3 | 看 `/api/dsa/status` 的 `reachable` | **由本页实时探活给出** |
 * | 4 | （可选）定时 18:00 Asia/Shanghai | 与盘中 BSS 推送时段不同，别混为一谈 |
 *
 * ## 第 3 步为什么必须看 `reachable` 而不是 `enabled`
 *
 * `enabled()` 约等于 `bool(DSA_BASE_URL)`（`backend/app/custom/dsa/proxy.py`）。
 * 只要 `.env` 里写了这一行，`enabled` 就恒真 —— 哪怕容器根本没起来。
 * 拿 `enabled` 当「装好了」的判据，是这套东西最容易犯的错（软提醒 1）。
 *
 * ## 关掉之后会怎样 —— 这一条必须写清
 *
 * 关 profile（容器不再起）/ 清掉或停用 `schedule.enabled` 之后，
 * **不应该再预期收到 DSA 的定时研报或狙击价位提醒**。剩下两条要自己去关：
 *
 * - `schedule.enabled`（默认就是 `false`，但别人可能给你打开过）；
 * - 狙击价位线的钉钉推送开关（`sniper_alerts` 那侧）。
 *
 * 反过来说：**只要没关这两处，容器停了也可能会看到残留的旧定时配置**——
 * 页面读得到 `schedule`，但服务不在，任务不会真的跑。这个「看得见跑不了」最容易
 * 被误读成「还在推」，所以这里的措辞是「不应预期」，不是「保证不会」（软提醒 7）。
 */

import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Circle, Loader2 } from 'lucide-react'
import { QK } from '@/lib/queryKeys'
import { fetchDsaStatus } from './client'

/** 可复制的启动命令。与 `docker-compose.yml` 的 `profiles: ["dsa"]` 对应。 */
export const DSA_ENABLE_COMMAND = 'docker compose --profile dsa up --build'

/** 自检用的状态接口。 */
export const DSA_STATUS_ENDPOINT = '/api/dsa/status'

/** 默认定时时刻（`backend/app/custom/dsa/schedule.py` 的默认值）。 */
export const DSA_DEFAULT_SCHEDULE_TIME = '18:00 Asia/Shanghai'

export interface EnableStep {
  id: string
  title: string
  body: string
  /** 该步骤是否可由页面自动判定（目前只有第 3 步）。 */
  auto?: boolean
  command?: string
}

/** 检查清单的步骤定义。顺序即执行顺序，别乱排。 */
export const DSA_ENABLE_STEPS: readonly EnableStep[] = [
  {
    id: 'env',
    title: '① 配置 DSA_BASE_URL',
    body: '在仓库根目录 .env 写 DSA_BASE_URL（指向决策服务的地址）。页面不回显密钥，也不替改文件。',
  },
  {
    id: 'compose',
    title: '② 用 profile 起容器',
    body: '生产默认不拉起决策服务；compose 里是 profiles: ["dsa"] 绑定的，必须显式带 profile 才会起。',
    command: DSA_ENABLE_COMMAND,
  },
  {
    id: 'reachable',
    title: '③ 确认 reachable',
    body: `打开 ${DSA_STATUS_ENDPOINT}，看 reachable 是否为 true。注意 enabled 恒真，不能当就绪判据。`,
    auto: true,
    command: DSA_STATUS_ENDPOINT,
  },
  {
    id: 'schedule',
    title: '④（可选）定时推送',
    body: `默认时刻 ${DSA_DEFAULT_SCHEDULE_TIME}，与盘中买卖信号推送时段不同。不需要就别开。`,
  },
] as const

const CMD_CLS = 'mt-1 block w-full overflow-x-auto rounded-btn border border-border bg-base px-2 py-1 font-mono text-[11px] text-accent'

/** 单条命令 + 一键复制。复制失败就静默 —— 命令本身还看得见，手抄也行。 */
function CommandLine({ command }: { command: string }) {
  const copy = () => {
    void navigator.clipboard?.writeText(command).catch(() => {})
  }
  return (
    <div className="relative">
      <code className={CMD_CLS}>{command}</code>
      <button
        type="button"
        onClick={copy}
        className="absolute right-1 top-1 rounded px-1.5 py-0.5 text-[10px] text-muted hover:text-foreground"
      >
        复制
      </button>
    </div>
  )
}

type StepState = 'done' | 'todo' | 'unknown' | 'checking'

function StepIcon({ state }: { state: StepState }) {
  if (state === 'done') return <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-bear" />
  if (state === 'checking') return <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-muted" />
  if (state === 'unknown') return <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
  return <Circle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted/60" />
}

/**
 * 启用向导检查清单。
 *
 * 只读：不改 `.env`、不起容器、不写任何持久化。
 * **不 import 任何核心模块**，也不被核心 import —— 它是 DSA 包里的一屏。
 */
export function DsaEnablementChecklist() {
  const status = useQuery({
    queryKey: QK.dsaStatus,
    queryFn: fetchDsaStatus,
    refetchInterval: 30_000,
    retry: false,
  })

  const reachable: boolean | null = typeof status.data?.reachable === 'boolean'
    ? status.data.reachable
    : null

  // 第 3 步的三态：只有 reachable===true 才算 done；===false 算 unknown（该去看日志）；
  // 还没结果算 checking。**不看 enabled**。
  const stepState = (step: EnableStep): StepState => {
    if (!step.auto) return 'todo'
    if (reachable === true) return 'done'
    if (reachable === false) return 'unknown'
    return status.isLoading ? 'checking' : 'unknown'
  }

  return (
    <div data-dsa-enablement-checklist className="space-y-2">
      <ol className="space-y-2">
        {DSA_ENABLE_STEPS.map(step => (
          <li key={step.id} className="grid grid-cols-[14px_1fr] gap-2">
            <StepIcon state={stepState(step)} />
            <div>
              <p className="text-xs text-foreground">{step.title}</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{step.body}</p>
              {step.command && <CommandLine command={step.command} />}
            </div>
          </li>
        ))}
      </ol>

      <div className="rounded-lg border border-border bg-base px-3 py-2">
        <p className="text-[11px] leading-relaxed text-secondary">
          <span className="text-foreground">当前自检：</span>
          {reachable === true && 'reachable = true，服务可连通。'}
          {reachable === false && 'reachable = false，服务未连通。先看容器是否带 --profile dsa 起来，再看 detail。'}
          {reachable === null && '尚未拿到结果（等待 /status 返回）。'}
        </p>
        {status.data?.detail && (
          <p className="mt-1 break-all font-mono text-[10px] text-muted">{status.data.detail}</p>
        )}
      </div>

      <p className="text-[10px] leading-relaxed text-muted">
        <span className="text-foreground">生产默认关。</span>
        关掉 profile（容器不再起）、并清掉或停用 schedule.enabled 之后，
        <span className="text-foreground">不应再预期收到</span> DSA 的定时研报或狙击价位提醒。
        注意默认时刻是 {DSA_DEFAULT_SCHEDULE_TIME}，和盘中买卖信号推送时段不同，两者别混为一谈。
        本清单只做指引与自检，不会替你去改 .env 或启容器。
      </p>
    </div>
  )
}

export default DsaEnablementChecklist
