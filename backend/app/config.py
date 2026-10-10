"""全局配置 — 从环境变量 / .env 读取。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── 运行环境检测 ──────────────────────────────────────────
# PyInstaller 打包后: __file__ 指向临时解压目录 _MEIPASS, 不能作为路径基准。
# 此时:
#   - 只读资源 (tiers.yaml / 前端 dist) 放在 _MEIPASS 内
#   - 可写用户数据 (data_dir) 放在可执行文件旁的用户目录
# 非 frozen 模式 (开发/Docker): 保持原有 __file__ 推导, 行为完全不变。
_IS_FROZEN = getattr(sys, "frozen", False)


def _user_data_root() -> Path:
    """桌面版用户数据根目录。

    定位策略 (按优先级):
      1. 环境变量 DATA_DIR (pydantic-settings 自动注入到 settings.data_dir, 不在此处理)
      2. 打包桌面版: exe 同级的 data/ 子目录 (<安装目录>/data/)
         —— 与程序同处一个总目录 (用户选择的安装目录), 视觉直观, 便于备份/迁移。
      3. 非 frozen (开发模式): 项目根 data/

    为什么不用 platformdirs 默认 (%LOCALAPPDATA%) 作为主路径:
      - 落在 C 盘系统目录, 用户不易察觉, 占系统盘空间
      - 用户期望「数据跟随程序」(便于备份/迁移)
    为什么放 {app}/data (exe 旁的 data/) 而非 {app} 外的兄弟目录:
      - 用户体验: 用户选了安装目录, 自然期望「程序和数据都在这」, 单一总目录更直观。
      - 数据安全: Inno Setup 覆盖安装(升级)时只往 {app} 写新程序文件, 不会清空
        目录里不在安装清单上的运行时文件 (data/ 即此类), 故覆盖安装不丢数据。
        (注意: 卸载时需在 .iss 中豁免 data/, 见 packaging/tsp.iss 的 [UninstallDelete]。)
    旧版本数据迁移: 见 DataStore._migrate_legacy_data_dir(), 老用户首次启动自动搬迁。
    """
    # 打包桌面版: exe 同级的 data/ 子目录 (与程序同一总目录, 覆盖安装不丢数据)
    if _IS_FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir / "data"

    # 开发模式: 项目根 data/
    return _PROJECT_ROOT / "data"


def _resource_root() -> Path:
    """只读资源根目录。

    frozen: PyInstaller 解压目录 (_MEIPASS)
    非 frozen: 项目根目录 (源码树)
    """
    if _IS_FROZEN:
        # sys._MEIPASS 是 PyInstaller 注入的解压根
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent.parent


def _project_root() -> Path:
    """项目根目录 (非 frozen 用)。"""
    return Path(__file__).resolve().parent.parent.parent


_PROJECT_ROOT = _project_root()
_RESOURCE_ROOT = _resource_root()
_ENV_FILE = Path(
    os.environ.get(
        "TICKFLOW_ENV_FILE",
        str(_RESOURCE_ROOT / ".env") if not _IS_FROZEN else ".env",
    )
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # TickFlow
    tickflow_api_key: str = Field(default="", description="留空启用 free 模式")

    # AI
    ai_provider: str = "openai_compat"
    ai_base_url: str = "https://llm.runninghub.ai/v1"
    ai_api_key: str = ""
    ai_model: str = "openai/gpt-6-astra-saver"
    ai_codex_command: str = "codex"
    ai_codex_reasoning_effort: str = ""
    # 默认浏览器风格 UA,绕过 Cloudflare 等 CDN/WAF 的 Bot 拦截(Issue #8)。
    # 用户可在 AI 设置页按需修改。
    ai_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    # AI 输出上限 (max_tokens) 与输入上下文窗口上限 (约 token)。
    # 任务级 max_tokens 会被钳制到 ai_max_output_tokens; 输入估算超出上下文窗口时给出明确报错。
    # 钳制仅封顶不放大: 现有任务最多请求 4500, 上调默认值只影响用户自行调高任务上限的场景。
    # 上下文 128000 对齐当前主流模型底线 (GPT/Claude/GLM/DeepSeek/Kimi 均 ≥128k); 可在 AI 设置里调整。
    ai_max_output_tokens: int = 16384
    ai_context_window: int = 128000
    # AI 助手工具轮次检查点: 连续 N 轮工具调用未完成时弹「继续/停止」卡; 0=不检查。
    ai_round_checkpoint: int = 100

    # Server
    host: str = "0.0.0.0"
    port: int = 3018
    log_level: str = "INFO"
    backtest_range_guard: bool = False
    backtest_matrix_disk_cache_enabled: bool = True
    backtest_matrix_cache_max_mb: int = 512
    backtest_matrix_cache_prewarm: bool = True
    backtest_matrix_cache_prewarm_years: int = 5

    # polars collect 并发闸 — polars 共享执行器在多线程并发 collect 下存在死锁
    # (上游 #24448/#25754 同族), 限流并发是社区验证的缓解手段。background 限额
    # 保证预热/增量等后台计算不占满闸位饿死页面读请求。
    polars_collect_permits: int = 4
    polars_collect_background_permits: int = 2

    # 后端自愈看门狗 — 探测 collect 闸与全局写锁, 连续失败即退出交由
    # supervisor 拉起 (见 app/watchdog.py)。误伤防护靠保守阈值。
    # 环境变量 WATCHDOG_FAILURE_THRESHOLD / WATCHDOG_INTERVAL_S /
    # WATCHDOG_PROBE_TIMEOUT_S / WATCHDOG_ENABLED 由下面的字段读入。
    watchdog_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("WATCHDOG_ENABLED", "watchdog_enabled"),
    )
    watchdog_interval_s: float = Field(
        default=30.0,
        validation_alias=AliasChoices("WATCHDOG_INTERVAL_S", "watchdog_interval_s"),
    )
    watchdog_probe_timeout_s: float = Field(
        default=15.0,
        validation_alias=AliasChoices("WATCHDOG_PROBE_TIMEOUT_S", "watchdog_probe_timeout_s"),
    )
    watchdog_failure_threshold: int = Field(
        default=2,
        validation_alias=AliasChoices("WATCHDOG_FAILURE_THRESHOLD", "watchdog_failure_threshold"),
    )

    # 策略批量执行 (run_all / 策略页全量跑) 的并发 worker 上限。实测 2026-09-07:
    # polars eager 操作内部已多线程并行, 外层再并发 4 worker 属超订, 41 策略
    # 299.6s 慢于串行 — 默认 1 (串行)。保留开关供配合 POLARS_MAX_THREADS 调优实验。
    strategy_run_all_workers: int = 1

    # run_all 渐进式返回: HTTP 同步等待时限 (秒)。策略按历史耗时升序执行,
    # 到点后已算完的随响应返回, 未算完的转后台继续算并逐个写入策略缓存,
    # 前端轮询 cached-summary 点亮卡片。0 = 关闭 (整段阻塞, 旧行为)。
    strategy_run_all_first_return_s: float = 15.0

    # 资讯采集。来源默认关闭；页面偏好或 NEWS_<来源>_ENABLED 打开。
    # ima 缺凭据、钉钉或知识星球缺群号、SEC 缺带邮箱的 User-Agent 时，即使打开也保持关闭。
    ima_client_id: str = ""
    ima_api_key: str = ""
    ima_kb_id: str = ""
    ima_kb_name: str = "【爱分享】的财经资讯"
    dingtalk_webhook_url: str = ""
    dingtalk_secret: str = ""
    news_dsa_feed_token: str = ""
    news_cls_enabled: str = ""
    news_wscn_enabled: str = ""
    news_ima_enabled: str = ""
    news_dws_enabled: str = ""
    news_zsxq_enabled: str = ""
    news_cnbc_enabled: str = ""
    news_marketwatch_enabled: str = ""
    news_wsj_enabled: str = ""
    news_bloomberg_enabled: str = ""
    news_scmp_enabled: str = ""
    news_reuters_enabled: str = ""
    news_reddit_enabled: str = ""
    # 逗号分隔。留空时采集用 wallstreetbets,stocks,investing。
    news_reddit_subreddits: str = ""
    # 预留。当前采集不换 token，请求里也不带 Authorization。
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    news_sec_enabled: str = ""
    # 例：TSP-News ops@example.com。不含邮箱时 SEC 来源保持未配置。
    sec_user_agent: str = ""
    news_llm_extract: str = ""
    news_dws_group_id: str = ""
    news_zsxq_group_id: str = ""
    news_etf_flow_enabled: str = ""
    # ETF 申赎表格的视觉模型。和上面的 ai_api_key / ai_model 分开，避免把文本模型当成视觉模型。
    vision_ai_api_key: str = ""
    vision_ai_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    vision_ai_model: str = "deepseek/deepseek-v4-flash-vision-exp"
    # 钉钉推送默认全关。总开关和每一类都要打开才会发。
    news_push_enabled: str = ""
    news_push_hot_enabled: str = ""
    news_push_abnormal_enabled: str = ""
    news_push_t_enabled: str = ""
    news_push_abnormal_include_hot: str = ""
    news_push_t_include_positions: str = ""
    news_push_premarket: str = "08:45"
    news_push_postclose: str = "15:40"
    news_push_top_n: int = 5
    news_push_min_stories: int = 2
    news_push_score_jump: float = 0.5
    news_push_hot_cooldown_min: int = 30
    news_push_symbol_cooldown_min: int = 30
    news_push_t_cooldown_min: int = 60
    news_push_t_range_cooldown_min: int = 60
    news_push_t_range_once_per_day: str = ""
    news_push_t_daily_cap: int = 20

    # Auth — 首次启动时预置访问密码(明文, 仅用于初始化, 详见 services/auth.bootstrap_from_env)
    # 公网服务器部署时免去 SSH 端口转发设密码的麻烦。写入 auth.json(哈希)后即不再读取。
    auth_password: str = ""
    # 多用户账号。空 = data/users.json。可为 JSON 文件路径，或内联 JSON（只放哈希）。
    auth_users: str = ""

    # Data — frozen: exe 同级 data/ 子目录; 非 frozen: 项目根 data/
    # (均可被环境变量 DATA_DIR 覆盖, pydantic-settings 自动注入)
    data_dir: Path = _user_data_root()

    # tiers.yaml 路径 — frozen: 资源目录内; 非 frozen: 项目根目录
    tiers_yaml: Path = _RESOURCE_ROOT / "tiers.yaml" if _IS_FROZEN else _PROJECT_ROOT / "tiers.yaml"

    # 静态文件(前端 dist) — frozen: 资源目录的 static/; 非 frozen: frontend/dist
    static_dir: Path = _RESOURCE_ROOT / "static" if _IS_FROZEN else (_PROJECT_ROOT / "frontend" / "dist")

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        """确保 data_dir 是绝对路径（环境变量传入的相对路径基于项目根目录解析）。"""
        if not self.data_dir.is_absolute():
            # 相对路径基于项目根目录解析，而非 CWD
            self.data_dir = (_PROJECT_ROOT / self.data_dir).resolve()
        if self.backtest_matrix_cache_max_mb <= 0:
            raise ValueError("backtest_matrix_cache_max_mb must be positive")
        if self.backtest_matrix_cache_prewarm_years <= 0:
            raise ValueError("backtest_matrix_cache_prewarm_years must be positive")
        if self.ai_max_output_tokens <= 0:
            raise ValueError("ai_max_output_tokens must be positive")
        if self.ai_context_window <= 0:
            raise ValueError("ai_context_window must be positive")
        if self.polars_collect_permits < 2:
            raise ValueError("polars_collect_permits must be >= 2")
        if not 1 <= self.polars_collect_background_permits < self.polars_collect_permits:
            raise ValueError(
                "polars_collect_background_permits must be in [1, polars_collect_permits)"
            )
        if self.watchdog_interval_s <= 0 or self.watchdog_probe_timeout_s <= 0:
            raise ValueError("watchdog intervals must be positive")
        if self.watchdog_failure_threshold < 1:
            raise ValueError("watchdog_failure_threshold must be >= 1")
        if self.strategy_run_all_workers < 1:
            raise ValueError("strategy_run_all_workers must be >= 1")
        if self.strategy_run_all_first_return_s < 0:
            raise ValueError("strategy_run_all_first_return_s must be >= 0")
        return self

    @property
    def use_free_mode(self) -> bool:
        """是否走 Free 模式。优先看 secrets.json,其次看 .env。"""
        from app import secrets_store
        return not secrets_store.get_tickflow_key()


settings = Settings()
