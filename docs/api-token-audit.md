# API Token 审计与轮换

面向**已经发过 Token 的部署**。本页只讲怎么发现"该处理的 Token"和怎么换掉它，
不涉及网关规则表（那是 `docs/open-platform-plan.md` §4 的内容）。

> 本仓当前**没有**自动轮换，也不会在轮换时通知调用方。Token 的生命周期由使用方的
> 运维流程决定 —— 这一页给的是判据和步骤，不是承诺。

---

## 1. 先看现状

```bash
curl -s -b 'tf_session=...' http://127.0.0.1:8000/api/settings/api-tokens/audit | jq
```

只看名单用 `GET /api/settings/api-tokens`；审计视图多给出三个东西：

| 字段 | 含义 |
|---|---|
| `age_days` | 距创建天数（按 `created_at`） |
| `idle_days` | 距上次使用天数（按 `last_used_at`）。**从未用过时为 null** |
| `flags` | 命中的判据，见下表 |

两个端点都只挂在 `/api/settings` 前缀下，只走 UI 会话；Token 通道对它们没有 scope，
Bearer 调用会被网关 403 拒绝。

## 2. 判据

| flag | 条件 | 建议动作 |
|---|---|---|
| `rotate_overdue` | 未吊销 且 `age_days ≥ 90` | 轮换：新建 → 调用方切换 → 吊销旧的 |
| `unused_stale` | 未吊销 且 `idle_days ≥ 30` | 先确认调用方，确认不用就吊销 |
| `never_used` | 未吊销 且 `last_used_at` 为空 且 `age_days ≥ 90` | 多半是接完就忘的残留，直接吊销 |
| `revoked` | 已吊销 | 什么都不做。记录留着是审计痕迹 |

**为什么"从未使用"要单独一条**：`idle_days` 对从未用过的 Token 是 null，若把它当成
"0 天没用"，这些 Token 永远不会出现在任何报警里 —— 恰恰是它们最需要清理。
反过来也不能直接算进 `unused_stale`：刚创建还没接入的 Token 也会被误报，所以只在
年龄超过轮换周期时才标 `never_used`。

两个阈值是常量（`backend/app/services/api_token_audit.py` 的 `STALE_UNUSED_DAYS` /
`STALE_ROTATE_DAYS`），**不是**可配置项 —— 它们只是审计提示，改它们不改变任何
鉴权行为。真要按部署调，改常量并在 PR 里写清理由。

> **旁注（P3 起）**：如果审计报告里出现仍带 `paper:trade` 的历史 Token，**不需要为了
> 撤销交易能力而紧急吊销** —— 网关规则表已剥离 `/api/paper*`，这类 Token 的模拟盘访问权
> 早已是 403，其余只读 scope 不受影响。按上面的 90/30 天节奏正常轮换即可。

## 3. 轮换步骤（手工，无自动化）

1. **新建**：`POST /api/settings/api-tokens`，scope 与旧 Token 保持一致（多给不必要）。
   响应里的 `plaintext` 只出现这一次，立即交给调用方。
2. **切换**：让调用方换上新 Token。新旧可以**并存**一段 —— 网关按 Token 独立校验，
   没有"同一 name 只能有一个"的约束。
3. **确认**：`GET /api/settings/api-tokens/audit`，看到新 Token 的 `last_used_at`
   有值（说明真的接上了）再往下走。
4. **吊销旧的**：`DELETE /api/settings/api-tokens/{token_id}`。吊销是幂等的。

第 3 步不要跳。跳过它最坏的结果是：旧 Token 已吊销、新 Token 没接上，调用方直接
全线 401，而且从服务端看不出是哪一环断的。

## 4. 本轮没做的事（有意为之）

| 没做 | 原因 |
|---|---|
| 自动轮换 | 轮换必须协调调用方，服务端单方面换 key 只会制造一次全量中断 |
| Token 过期时间（TTL） | 加 TTL 会让所有存量 Token 在某天集体失效；要做得先有"提前告警 + 调用方配合"的流程，那是另一个 PR |
| 改哈希算法 / 存储字段 | 一次改动就能让全部已发 Token 静默失效，而失效表现是 401，分不清是过期还是改坏了 |
| 审计落盘（谁在何时用了哪个 Token） | 需要 `api_gateway` 里加写路径与日志轮转策略，属于要单独设计的东西 |
| 前端页面 | 审计视图先以接口形式提供；UI 等上面这些稳定后再做 |

`api_tokens.py` 与 `api_gateway.py` 本轮**一行未改** —— 审计模块只 `import` 它们现有的
`list_tokens` / `cn_now`，不碰读写路径。

## 5. 相关配置（本轮新增）

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `TSP_CORS_ORIGINS` | 未设 = `*` | 逗号分隔的来源白名单。设了就只放列出来源，并自动打开 `allow_credentials` |
| `TSP_COOKIE_SECURE` | 未设 = 关 | 会话 cookie 是否带 `Secure`。反代上了 HTTPS 就设 `1` |

**默认值都保持宽松，这是刻意的**：这个面板的典型部署是「自托管 + 局域网 http」，
默认收紧会让现有部署升级后前端白屏、登录成功却跳回登录页，而这两种故障都不报错。
收紧安全边界应当是显式动作。
