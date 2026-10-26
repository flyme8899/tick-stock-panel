"""FastAPI 入口。"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    abnormal,
    alerts,
    analysis,
    auction_scan,
    backtest,
    buy_sell_signals_hits,
    chip,
    data,
    ext_data,
    events,
    factors,
    financials,
    fund_flow,
    indices,
    intraday,
    kline,
    limit_board,
    lots,
    market_recap,
    mining,
    monitor_rules,
    news,
    overview,
    paper,
    picker,
    pipeline,
    regime,
    research,
    rps,
    screener,
    sector_rotation,
    signals,
    stock_analysis,
    strategy,
    trade_signals,
    watchlist,
)
from app.api import auth as auth_api
from app.api import settings as settings_api
from app.api.routes import router as core_router
from app.config import settings
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions.loader import (
    configure_backend_extensions,
    current_extension_context,
    start_backend_extensions,
)
from app.jobs import daily_pipeline
from app.services.json_safe import SafeJSONResponse
from app.services.matrix_prewarm_owner import MatrixCachePrewarmOwner
from app.services.mining_process_lock import MiningProcessLock
from app.services.quote_service import QuoteService
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 追加文件日志: uvicorn (含 --reload 开发模式) 默认只有 StreamHandler, 同步/管道等
# 运行时日志仅出现在 dev 终端, 关掉或滚屏后即丢失, 排查「同步后日志没落」时无处可查。
# 落盘到 data/backend.log 与桌面版 (desktop.py:_setup_logging → desktop.log) 行为对齐,
# 事后可查。桌面版 (frozen) 已由 desktop.py 写 desktop.log, 此处跳过避免重复落盘。
# RotatingFileHandler 防止长期运行/频繁 reload 导致文件无限增长。
if not getattr(sys, "frozen", False):
    try:
        from logging.handlers import RotatingFileHandler

        _log_path = settings.data_dir / "backend.log"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            _log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
            mode="a", encoding="utf-8", errors="replace",
        )
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    except Exception as _e:  # noqa: BLE001
        logger.warning("文件日志初始化失败, 仅输出到终端: %s", _e)


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    logger.info(
        "Tick Stock Panel v%s starting (mode=%s)",
        __version__, tf_client.current_mode(),
    )

    # 首次启动: 若配置了 AUTH_PASSWORD 环境变量且未设过密码, 用它初始化。
    # 公网部署免 SSH 端口转发; 已设过密码则不覆盖 (改密码走 UI)。
    try:
        from app.services import auth as auth_service
        auth_service.bootstrap_from_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("auth bootstrap failed: %s", e)

    # 数据层
    store = DataStore()
    repo = KlineRepository(store)
    app.state.datastore = store
    app.state.repo = repo
    # 自定义/复合因子载入注册表 (P3); 单个失败只跳过该因子 (fail-隔离)
    from app.factors.store import load_into_registry

    try:
        loaded_factors = load_into_registry(store.data_dir)
        if loaded_factors:
            logger.info("custom factors loaded: %s", len(loaded_factors))
    except Exception as exc:  # noqa: BLE001
        logger.warning("custom factors load failed: %s", exc)
    from app.services.mining_manager import MiningJobManager

    mining_manager = MiningJobManager(store.data_dir)
    recovered_mining_runs = mining_manager.recover_interrupted()
    app.state.mining_manager = mining_manager
    if recovered_mining_runs:
        logger.warning("recovered %d interrupted mining runs", recovered_mining_runs)
    from app.services.factor_backtest_runs import FactorBacktestJobManager

    factor_backtest_jobs = FactorBacktestJobManager(store.data_dir)
    recovered_factor_runs = factor_backtest_jobs.recover_interrupted()
    app.state.factor_backtest_jobs = factor_backtest_jobs
    if recovered_factor_runs:
        logger.warning("recovered %d interrupted factor backtest runs", recovered_factor_runs)
    # 在接受回测请求前固定 managed generation，避免首批并发 worker 各自创建版本。
    if settings.backtest_matrix_disk_cache_enabled:
        try:
            repo.get_matrix_data_generation("stock")
        except EnrichedGenerationUnavailableError as exc:
            logger.warning("enriched generation requires a full rebuild: %s", exc)
    # 指标异步预热标志: enriched 缓存在后台线程构建, 完成后置 True
    app.state.indicators_ready = False
    repo._on_warmup_done = lambda: setattr(app.state, "indicators_ready", True)  # noqa: SLF001

    # Polars 缓存预热 — enriched 的重计算 (107万行 compute_indicators) 推后台,
    # instruments/index/ETF 仍同步 (毫秒级)。应用立即 ready, 指标算完后自动替换。
    repo.refresh_cache(background=True)

    # 自定义数据源配置(可选): 失败只记录错误, 不影响 TickFlow 基准路径。
    try:
        from app.data_providers import custom as custom_sources
        custom_sources.load_all()
        logger.info("custom data sources loaded: %d", len(custom_sources.list_sources()))
    except Exception as e:  # noqa: BLE001
        logger.warning("custom data sources init failed: %s", e)

    # 自定义源必须先注册,能力探测才能补充其数据集能力。
    capset = detect_capabilities()
    app.state.capabilities = capset
    logger.info("ready; %d capabilities active", len(capset.all()))

    # 全局行情服务
    qs = QuoteService()
    app.state.quote_service = qs
    qs.set_repo(repo)
    qs.boot_check()

    # QuoteService 需要访问 strategy_monitor 等单例
    # 先创建 strategy_monitor，再注入 app.state
    from app.strategy.monitor import StrategyMonitorService
    strategy_monitor = StrategyMonitorService()
    app.state.strategy_monitor = strategy_monitor
    qs.set_app_state(app.state)

    # 五档盘口 sealed 服务(真假涨停/跌停, 独立旁路线)
    from app.services.depth_service import DepthService
    depth_service = DepthService()
    depth_service.set_repo(repo)
    depth_service.set_app_state(app.state)
    app.state.depth_service = depth_service

    # 启动调度器(若 enriched 数据为空,首次启动可手动 POST /api/pipeline/run)
    try:
        daily_pipeline.set_app_state(app.state)  # 供 depth_finalize job 访问 depth_service
        scheduler = daily_pipeline.start_scheduler(repo, capset)
        app.state.scheduler = scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler not started: %s", e)
        app.state.scheduler = None

    # depth sealed: 启动补跑(当天文件不存在) + 盘中轮询(有能力时)
    try:
        depth_service.boot_check()
        depth_service.start_polling()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_service init failed: %s", e)

    # 做T池盘中五档快照(研究旁路线, 只写 data/depth5_intraday, fail-soft)
    try:
        from app.services.depth_intraday import DepthIntradayCollector
        depth_intraday = DepthIntradayCollector(repo.store.data_dir, depth_service)
        app.state.depth_intraday = depth_intraday
        depth_intraday.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_intraday init failed: %s", e)

    # 盘中分钟增量刷新 (Expert 专有): 线程常驻, 开关/时段/能力门控在循环内每轮判断
    try:
        from app.services.minute_refresh import MinuteRefreshService
        minute_refresh = MinuteRefreshService(repo)
        minute_refresh.set_app_state(app.state)
        app.state.minute_refresh = minute_refresh
        minute_refresh.start()
    except Exception as e:
        logger.warning("minute_refresh init failed: %s", e)

    # 停机缺口自检: 延迟后台扫描, 发现最近交易日的盘中快照/缺口时自动创建
    # 修复任务 (盘中停机→次日开实时场景, 不修则坏数据被"只刷今天"分支永久留存)
    try:
        import threading

        from app.services.data_integrity import boot_integrity_check

        timer = threading.Timer(30.0, boot_integrity_check, args=(app.state,))
        timer.daemon = True  # 不阻塞进程退出
        timer.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("integrity boot check scheduling failed: %s", e)

    # 企业微信智能机器人长连接(可选通道, 失败不阻断启动)
    try:
        from app.services.wecom_bot_service import WecomBotService
        wecom_bot_service = WecomBotService()
        wecom_bot_service.set_app_state(app.state)
        app.state.wecom_bot_service = wecom_bot_service
        wecom_bot_service.boot_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("wecom_bot_service init failed: %s", e)

    # 买卖信号注册表 (批次 20261025gp): 只经工厂建 store 并存进 app.state。
    # 未设 BUY_SELL_SIGNALS_PERSIST → 内存实现; 设了才落 data/buy_sell_signals/。
    # 这里不挂任何路由, 也不在日管道里自动 upsert。
    # 失败只警告, 不影响启动。
    try:
        from app.buy_sell_signals.store import build_signal_registry_store, persist_env_enabled

        app.state.buy_sell_signal_store = build_signal_registry_store(store.data_dir)
        if persist_env_enabled():
            logger.info(
                "buy_sell_signals 注册表已落盘(可选开启): %s",
                getattr(app.state.buy_sell_signal_store, "path", "?"),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("buy_sell_signals 注册表初始化失败 (不影响启动): %s", e)

    # 买卖信号命中层 (批次 20261026gbsshits): **只读**店, 挂进 app.state 供命中 API 用。
    #
    # 这里只建一个读取器, 不做任何评估、不做影子调度、不扫表、不写文件 —— 命中层的
    # 写入属于另一拍(评估器 + P-calib)。文件不存在是合法状态: 命中数 0, 监控走空态。
    # 与上面的 PERSIST env **无关**: 那个管定义注册表落不落盘, 命中看不看得见只取决于
    # hits.json 在不在。
    try:
        from app.buy_sell_signals.hits_store import build_hit_store

        app.state.buy_sell_signal_hit_store = build_hit_store(store.data_dir)
        if app.state.buy_sell_signal_hit_store.exists():
            logger.info(
                "buy_sell_signals 命中层只读店: %s",
                app.state.buy_sell_signal_hit_store.path,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("buy_sell_signals 命中层读取器初始化失败 (不影响启动): %s", e)

    # 买卖信号校准登记 (批次 20261026gbsscalib): 同样**只读**店, 挂进 app.state。
    #
    # 与命中层同一个纪律: 不评估、不调度、不扫表、不写文件。文件不存在是合法状态 ——
    # 含义是「一条 passed 都没有」, 于是所有命中不可见, 监控走空态。这**不是**缺陷,
    # 是诚实: 没有通过校准证据的时候, 系统确实什么都不能担保。
    #
    # 注意: 这里**不**读 BUY_SELL_SIGNALS_PERSIST。校准看不看得见只取决于 calib.json
    # 在不在, 与「定义注册表落不落盘」是两件事。
    try:
        from app.buy_sell_signals.calib_store import build_calib_store

        app.state.buy_sell_signal_calib_store = build_calib_store(store.data_dir)
        if app.state.buy_sell_signal_calib_store.exists():
            logger.info(
                "buy_sell_signals 校准登记只读店: %s",
                app.state.buy_sell_signal_calib_store.path,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("buy_sell_signals 校准登记读取器初始化失败 (不影响启动): %s", e)

    # 买卖信号推送出口 (批次 20261025gpush / gding): 只在 BUY_SELL_SIGNALS_PUSH 开时挂 helper。
    # 通道 = SSE(QuoteService.push_alerts) + 站内(alert_store); 外部钉钉另受
    # BUY_SELL_SIGNALS_DINGTALK 与既有 DINGTALK_WEBHOOK_URL 二次门控(与关系, 默认关)。
    # 飞书已取消(批次 20261025gding 起从路线图去掉, 不再排期)。
    # 这里**不调用** try_push —— 启动路径零真实推送, 也没有任何"扫全表自动推"的逻辑。
    # 与上面的 persist env 正交: 落盘管"活得久", 推送管"播出去", 互不推导。
    # 未开时不挂该属性(而不是挂一个 enabled=False), 让"没开"这件事在 state 上直接可见。
    try:
        from app.buy_sell_signals.push import (
            dingtalk_env_enabled,
            dingtalk_webhook_ready,
            push_env_enabled,
            try_push,
        )

        if push_env_enabled():
            app.state.buy_sell_signal_push = {
                "enabled": True,
                "try_push": try_push,
                "quote_service": getattr(app.state, "quote_service", None),
                "data_dir": store.data_dir,
                # 只读标志: 告诉运维"外部通道是否也已开门", 不代表本次启动发过任何消息。
                "dingtalk_env": dingtalk_env_enabled(),
                "dingtalk_webhook": dingtalk_webhook_ready(),
            }
            logger.info(
                "buy_sell_signals 推送门已开(SSE/站内); 钉钉另受 %s 门控(当前%s, webhook%s)",
                "BUY_SELL_SIGNALS_DINGTALK",
                "开" if dingtalk_env_enabled() else "关",
                "已配置" if dingtalk_webhook_ready() else "未配置",
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("buy_sell_signals 推送门初始化失败 (不影响启动): %s", e)

    # 内置扩展表 (概念/行业): 先创建 config (含拉取配置), 默认开启定时拉取。
    # 必须在 pull_scheduler.refresh() 之前执行, 否则全新部署时 scheduler 读不到
    # 刚创建的预设, 定时任务不会启动。
    try:
        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("内置扩展表初始化失败 (不影响启动): %s", e)

    # 扩展数据定时拉取: 在预设配置就绪后启动, 自动调度 enabled 的预设。
    from app.services.ext_pull import pull_scheduler
    pull_scheduler.start(store.data_dir)
    pull_scheduler.refresh(store.data_dir)
    app.state.pull_scheduler = pull_scheduler

    # 财务数据 (需 Expert 套餐): 仅初始化调度器供 /api/financials/sync/* 手动同步,
    # 不启动自动调度——用户在「财务分析」页点「同步」手动拉取。
    from app.services.financial_sync import financial_scheduler
    financial_scheduler.start(store.data_dir, capset)
    app.state.financial_scheduler = financial_scheduler

    # 多源资讯采集。除格隆汇默认打开外，来源默认关闭，线程只在开关打开后出网。
    try:
        from app.news.scheduler import NewsScheduler
        news_scheduler = NewsScheduler()
        news_scheduler.start()
        app.state.news_scheduler = news_scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning("news scheduler init failed: %s", e)

    # 资金进出。开关默认关，线程起来也不出网。不占界面上的数据任务槽。
    try:
        from app.fund_flow.scheduler import get_worker

        fund_flow_worker = get_worker()
        fund_flow_worker.start()
        app.state.fund_flow_worker = fund_flow_worker
    except Exception as e:  # noqa: BLE001
        logger.warning("fund-flow worker init failed: %s", e)

    # 打板和研报。开关默认开，但测试进程里线程直接空转，不出网。
    try:
        from app.market_feeds.scheduler import get_worker as get_market_feed_worker

        market_feed_worker = get_market_feed_worker()
        market_feed_worker.start()
        app.state.market_feed_worker = market_feed_worker
    except Exception as e:  # noqa: BLE001
        logger.warning("market-feeds worker init failed: %s", e)

    # ima 券商研报全文。开关默认关，测试进程里线程起来也不出网。
    try:
        from app.research_reports.ima_schedule import get_worker as get_ima_research_worker

        ima_research_worker = get_ima_research_worker()
        ima_research_worker.start()
        app.state.ima_research_worker = ima_research_worker
    except Exception as e:  # noqa: BLE001
        logger.warning("ima research worker init failed: %s", e)

    # 选股补充字段。开关默认关，线程起来也不出网，不占重任务槽。
    try:
        from app.selection_fields.scheduler import get_worker as get_selection_worker

        selection_worker = get_selection_worker()
        selection_worker.start()
        app.state.selection_worker = selection_worker
    except Exception as e:  # noqa: BLE001
        logger.warning("selection-fields worker init failed: %s", e)

    # 龙虎榜、两融、互动问答、公告和盘口异动。开关默认关，线程起来也不出网。
    try:
        from app.event_context.scheduler import get_worker as get_event_worker

        event_worker = get_event_worker()
        event_worker.start()
        app.state.event_worker = event_worker
    except Exception as e:  # noqa: BLE001
        logger.warning("event-context worker init failed: %s", e)

    # 自愈看门狗: 探测 polars 闸与写锁, 僵死时退出交由 supervisor 拉起 (兜底层)。
    from app.watchdog import start_watchdog
    app.state.watchdog = start_watchdog(app.state, repo)

    # 策略引擎
    from app.strategy.engine import StrategyEngine
    from app.strategy import config as strategy_config
    from app.strategy.monitor import StrategyMonitorService
    from app.services.screener import ScreenerService

    _screener_svc = ScreenerService(repo)
    _etf_screener_svc = ScreenerService(repo, asset_type="etf")
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        store.data_dir / "strategies" / "custom",
        store.data_dir / "strategies" / "ai",
        store.data_dir / "strategies" / "composite",
    ]
    strategy_engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        override_loader=lambda sid: strategy_config.load_override(store.data_dir, sid),
    )
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    matrix_prewarm_owner = MatrixCachePrewarmOwner()

    def _schedule_matrix_cache_prewarm() -> None:
        if (
            not settings.backtest_matrix_disk_cache_enabled
            or not settings.backtest_matrix_cache_prewarm
        ):
            return

        def _prewarm() -> None:
            from app.backtest.engine import BacktestEngine
            from app.backtest.matrix import MatrixPrewarmCancelledError
            from app.backtest.strategy import prewarm_matrix_cache
            from app.services.heavy_job_limiter import (
                HeavyJobCancelledError,
                shared_heavy_job_limiter,
            )

            try:
                latest = repo.latest_enriched_date("stock")
                if latest is None:
                    logger.info("matrix cache prewarm skipped: no stock enriched data")
                    return

                with shared_heavy_job_limiter.slot(
                    "exclusive",
                    cancel_event=matrix_prewarm_owner.cancel_event,
                ):
                    result = prewarm_matrix_cache(
                        BacktestEngine(repo),
                        strategy_engine,
                        asset_type="stock",
                        latest_date=latest,
                        years=settings.backtest_matrix_cache_prewarm_years,
                        cancel_event=matrix_prewarm_owner.cancel_event,
                    )
                logger.info("matrix cache prewarm done: %s", result)
            except (HeavyJobCancelledError, MatrixPrewarmCancelledError):
                logger.info("matrix cache prewarm cancelled")
            except Exception:  # noqa: BLE001
                logger.exception("matrix cache prewarm failed")

        if not matrix_prewarm_owner.schedule(_prewarm):
            logger.info("matrix cache prewarm already running or shutting down, skip")

    repo._on_refresh_done = _schedule_matrix_cache_prewarm  # noqa: SLF001
    if repo.enriched_ready:
        _schedule_matrix_cache_prewarm()

    # 通用监控规则引擎: 启动时 reload 规则到内存态 (修复重启后告警失效)
    from app.strategy.monitor import MonitorRuleEngine
    from app.strategy import monitor_rules as mr_store
    from app.services import preferences
    from app.services.sector_monitor import SectorMonitorService
    monitor_engine = MonitorRuleEngine()
    sector_monitor_service = SectorMonitorService(repo)
    monitor_engine.set_strategy_engine(strategy_engine)
    monitor_engine.set_data_dir(store.data_dir)
    monitor_engine.set_sector_monitor_service(sector_monitor_service)
    # 复用 ScreenerService 的历史窗口加载器 (三级缓存, 启动预计算命中 ~0ms),
    # 让声明 filter_history 的策略 (如反包) 也能在实时监控里跑选股 → 盘中触发通知。
    monitor_engine.set_history_loader(_screener_svc._load_enriched_history)
    # ETF 版历史加载器: asset_type=etf 的 strategy 型规则用 (读 kline_etf_enriched)。
    monitor_engine.set_history_loader_etf(_etf_screener_svc._load_enriched_history)

    # 自动迁移: 把旧 strategy_monitor_ids 同步为 type=strategy 规则 (统一到监控页)
    try:
        if preferences.get_strategy_monitor_enabled():
            ids = preferences.get_strategy_monitor_ids()
            if ids:
                names = {s["id"]: s["name"] for s in strategy_engine.list_strategies()}
                mr_store.migrate_strategy_monitors(store.data_dir, ids, names)
                logger.info("strategy monitor migrated: %d strategies", len(ids))
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy monitor migration failed: %s", e)

    try:
        rules = mr_store.load_all(store.data_dir)
        monitor_engine.set_rules(rules)
        logger.info("monitor engine loaded: %d rules", monitor_engine.rule_count)
    except Exception as e:  # noqa: BLE001
        logger.warning("monitor engine load failed: %s", e)
    app.state.monitor_engine = monitor_engine
    app.state.sector_monitor_service = sector_monitor_service

    # 源码内二次开发启动钩子: 仅暴露稳定只读上下文, 单个扩展失败不影响核心启动。
    extension_registry = app.state.extension_registry
    start_backend_extensions(
        current_extension_context(data_dir=store.data_dir, repository=repo),
        extension_registry,
    )

    # regime 口径自检: 口径常量变过而历史表没重算, 表里就会混着两套口径的行,
    # 下游(回测 regime_filter / AI 助手 / Regime 页)读到的是混合序列。
    # 这里只做**明确拦截**: 比对 stamp 与当前常量指纹, 不一致就大声报出来并挂到
    # app.state 上由 /api/regime/caliber 暴露给前端, 但**不自动重算** —— 全量重算要
    # 扫全 A enriched, 在启动路径里同步做会把启动拖到分钟级, 放后台则无法告知用户
    # 结果。前置条件不在也无法判断(enriched 可能还没同步), 交给用户显式触发。
    try:
        from app.services import regime_caliber

        caliber = regime_caliber.check_caliber(store.data_dir)
        app.state.regime_caliber = caliber
        message = regime_caliber.describe_drift(caliber)
        if caliber["match"]:
            logger.info(message)
        elif caliber["reason"] == "missing":
            logger.warning("%s — 建议执行一次 POST /api/regime/recompute 全量重算", message)
        else:
            logger.warning("%s — 必须执行一次 POST /api/regime/recompute 全量重算", message)
    except Exception as e:  # noqa: BLE001
        # 自检失败不阻断启动: 它只是提示, 不该让整个服务起不来。
        logger.warning("regime caliber check failed (soft): %s", e)
        app.state.regime_caliber = None

    try:
        yield
    finally:
        repo._on_refresh_done = None  # noqa: SLF001
        wd = getattr(app.state, "watchdog", None)
        if wd:
            await wd.stop()
        if not matrix_prewarm_owner.shutdown(timeout=5.0):
            logger.warning("matrix cache prewarm did not stop within 5 seconds")
        mmanager = getattr(app.state, "mining_manager", None)
        if mmanager:
            mmanager.shutdown()
        factor_jobs = getattr(app.state, "factor_backtest_jobs", None)
        if factor_jobs:
            factor_jobs.shutdown()
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)
        ps = getattr(app.state, "pull_scheduler", None)
        if ps:
            ps.stop()
        fsc = getattr(app.state, "financial_scheduler", None)
        if fsc:
            fsc.stop()
        news_sched = getattr(app.state, "news_scheduler", None)
        if news_sched:
            news_sched.stop()
        fund_flow_worker = getattr(app.state, "fund_flow_worker", None)
        if fund_flow_worker:
            fund_flow_worker.stop()
        market_feed_worker = getattr(app.state, "market_feed_worker", None)
        if market_feed_worker:
            market_feed_worker.stop()
        ima_research_worker = getattr(app.state, "ima_research_worker", None)
        if ima_research_worker:
            ima_research_worker.stop()
        selection_worker = getattr(app.state, "selection_worker", None)
        if selection_worker:
            selection_worker.stop()
        event_worker = getattr(app.state, "event_worker", None)
        if event_worker:
            event_worker.stop()
        qs = getattr(app.state, "quote_service", None)
        if qs:
            qs.stop()
        dsvc = getattr(app.state, "depth_service", None)
        if dsvc:
            dsvc.stop_polling()
        dintra = getattr(app.state, "depth_intraday", None)
        if dintra:
            dintra.stop()
        wbot = getattr(app.state, "wecom_bot_service", None)
        if wbot:
            wbot.stop()
        mrs = getattr(app.state, "minute_refresh", None)
        if mrs:
            mrs.stop()
        # webhook 投递线程池: 放弃排队投递并关闭, 避免通知静默丢失在 daemon 线程里
        from app.services.quote_service import shutdown_webhook_executor

        shutdown_webhook_executor()
        logger.info("shutdown")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mining_process_lock = MiningProcessLock(settings.data_dir)
    mining_process_lock.acquire()
    try:
        async with _application_lifespan(app):
            yield
    finally:
        mining_process_lock.release()


def _api_docs_enabled() -> bool:
    """交互式 API 文档默认关闭, 需要时用环境变量显式打开。

    默认开着的三个端点在生产里是**不认证就能拿到的资产清单**:
    - ``/openapi.json`` 一份就能列出全部路由、参数、请求体 schema (实测约 300KB,
      300+ 条 ``/api/`` 路由), 攻击者不必猜路径;
    - ``/docs`` / ``/redoc`` 直接在上面套一层可点可试的 UI。

    而认证中间件只拦 ``/api/`` 前缀, 这三个路径本来就不在拦截范围内, 只能靠
    白名单把它们放行 —— 现在改成默认不挂载, 白名单也就一并清掉。
    自托管调试需要时: ``TSP_ENABLE_API_DOCS=1`` 启动即可恢复原行为。
    """
    raw = os.environ.get("TSP_ENABLE_API_DOCS", "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


_DOCS_ON = _api_docs_enabled()


def _split_origins(raw: str) -> list[str]:
    """逗号分隔的来源列表 → 去空白、去空项。

    允许 ``https://a.example.com, https://b.example.com`` 这种带空格的写法 —— 环境变量
    里手写空格是常态, 多一个空格就让整条 CORS 静默失效太难查。
    """
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _cors_origins() -> list[str]:
    """CORS 允许的来源: ``TSP_CORS_ORIGINS`` 未设时保持 ``*``(自托管局域网场景)。

    设了就**只**放行列出的来源。收紧到具体来源时顺手打开 credentials: 浏览器规范
    禁止 ``allow_origins=["*"]`` 与 ``allow_credentials=True`` 共存, 但具体来源没有这个
    限制 —— 收紧来源的人通常正是想用 cookie/凭据的那类部署。

    不改成"默认只允许同源": 这是自托管面板, 用户常从局域网另一台机器开页面, 默认收紧
    会让一大批现有部署的前端直接白屏。安全边界的收紧应当是显式动作, 不是升级副作用。
    """
    raw = os.environ.get("TSP_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    origins = _split_origins(raw)
    return origins or ["*"]


_CORS_ORIGINS = _cors_origins()

app = FastAPI(
    title="Tick Stock Panel",
    version=__version__,
    description="A 股选股 + 回测面板 — TickFlow 适配",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url="/redoc" if _DOCS_ON else None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
    # 出口统一消毒: polars/pandas 除零、空集 mean 会产出 inf/NaN, Starlette 的
    # JSONResponse 用 allow_nan=False 渲染 —— 漏一个非有限浮点就是**整段响应 500**,
    # 而不是那一行变 null。默认响应类换成 SafeJSONResponse, 渲染前先过 json_safe,
    # 于是任何走默认 JSONResponse 的路由都不可能吐出 NaN/Infinity, 也不会因此 500。
    default_response_class=SafeJSONResponse,
)

# CORS: 默认放开所有来源 (自托管场景, 局域网访问)。
# 注: allow_credentials=True 与 allow_origins=['*'] 不能共存 (浏览器规范),
# 所以通配时关闭 credentials, 认证本就走 cookie/header 二选一 —— 需要凭据的部署
# 请设 TSP_CORS_ORIGINS 收窄到具体来源, 那时 credentials 自动打开。
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=_CORS_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# 访问认证中间件
# ================================================================
# 拦截所有 /api/ 请求, 三种状态:
#   1. 未设密码 + 本机/内网 → 放行(让本机用户访问面板 + 调 /api/auth/setup 设密码)
#   2. 未设密码 + 公网       → 拒绝(403, 防裸奔也防抢占; 引导本机设密码)
#   3. 已设密码              → 检查 session, 无效则 401(前端跳登录)
# 白名单: /api/auth/* (设密码/登录本身)、/health 等探活。
_AUTH_WHITELIST_PREFIX = ("/api/auth/",)
_AUTH_WHITELIST_EXACT = (
    "/health",
    # SSE 事件流: EventSource 带不了 Authorization 头, 凭证即 query 里的
    # 一次性票据, 端点内校验 (api/events.py); POST /api/events/ticket 不在
    # 白名单, 仍走网关 Bearer 通道
    "/api/events",
)
# 注: `/api/health` 曾在这份白名单里, 但 main.py 从来没有注册过这条路由 ——
# 它是一个"看起来放行了、其实不存在"的条目, 会让人误以为探活有两条路可走。
# 各业务 router 的 `/api/<域>/health` 是另一回事, 它们走认证, 不该进白名单。
# tests/test_unauthorized_surface.py 有一条测试要求白名单里的每一项都有真实路由。
#
# 注: /openapi.json、/api/openapi.json、/docs、/redoc 原先在这份白名单里, 为的是让
# 未登录也能看交互式文档。现在文档默认不挂载 (见 `_api_docs_enabled`), 白名单随之清掉;
# 用 TSP_ENABLE_API_DOCS=1 打开后它们仍可匿名访问, 开启文档就等于接受公开 schema。


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # 仅 /api/ 走认证; 静态资源(前端页面/assets)放行, 由前端处理跳转
    if not path.startswith("/api/"):
        return await call_next(request)
    # CORS 预检不带凭据, 直接放行 (CORSMiddleware 在外层应答)
    if request.method == "OPTIONS":
        return await call_next(request)
    # 白名单放行(设密码/登录/探活本身不拦)
    if path.startswith(_AUTH_WHITELIST_PREFIX) or path in _AUTH_WHITELIST_EXACT:
        return await call_next(request)

    # DSA 旁路拉资讯。令牌不对时 404，避免未登录的 401 把内部地址暴露成可探测接口。
    if path == "/api/news/dsa-feed":
        from app.news.config import feed_matches

        if feed_matches(request.headers.get("x-news-feed-token", "")):
            return await call_next(request)
        return JSONResponse(status_code=404, content={"detail": "未启用"})

    # DSA 读取本地资金进出摘要。令牌与资讯 feed 相同，不对就 404。
    if path == "/api/fund-flow/dsa-context":
        from app.news.config import feed_matches

        if feed_matches(request.headers.get("x-news-feed-token", "")):
            return await call_next(request)
        return JSONResponse(status_code=404, content={"detail": "未启用"})

    # DSA 拉取个股上下文，并回写分析价位。令牌不对时 404。
    if path in {"/api/dsa/stock-context", "/api/dsa/sniper-lines"}:
        from app.news.config import feed_matches

        if feed_matches(request.headers.get("x-news-feed-token", "")):
            return await call_next(request)
        return JSONResponse(status_code=404, content={"detail": "未启用"})

    # ── API Token 通道 (外部调用方; 与密码会话并行, 见 open-platform-plan §4) ──
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        from app.services import api_gateway
        verdict = api_gateway.evaluate(
            settings.data_dir, request.method, path, authz[len("Bearer "):].strip(),
        )
        if verdict["status"] is not None:
            return JSONResponse(
                status_code=verdict["status"], content={"detail": verdict["detail"]},
                headers=verdict["headers"],
            )
        response = await call_next(request)
        for k, v in verdict["headers"].items():
            response.headers[k] = v
        return response

    from app.services import auth as auth_service
    # 情况 1+2: 未设密码
    if not auth_service.is_configured():
        # 本机/内网 → 放行(服务器主人可访问, 并去 /login 设密码)
        if auth_api._is_local_network(auth_api._client_ip(request)):
            return await call_next(request)
        # 公网 → 拒绝。不裸奔, 也不给公网设密码的机会(防抢占)
        return JSONResponse(
            status_code=403,
            content={
                "detail": "面板尚未初始化访问密码,请通过 SSH/本机浏览器访问以设置密码",
                "code": "NOT_INITIALIZED",
            },
        )

    # 情况 3: 已设密码, 检查会话
    token = request.cookies.get(auth_api.COOKIE_NAME)
    if token and auth_service.is_valid_session(token):
        # 账号之间权限相同, 这里只标明是谁的会话。
        request.state.auth_user = auth_service.session_username(token)
        return await call_next(request)
    # 未登录: 401(前端跳登录页)
    return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})


# 路由
app.include_router(core_router)
app.include_router(auth_api.router)
app.include_router(kline.router)
app.include_router(chip.router)
app.include_router(watchlist.router)
app.include_router(screener.router)
app.include_router(backtest.router)
app.include_router(factors.router)
app.include_router(mining.router)
app.include_router(intraday.router)
app.include_router(indices.router)
app.include_router(overview.router)
app.include_router(paper.router)
app.include_router(picker.router)
app.include_router(abnormal.router)
app.include_router(buy_sell_signals_hits.router)
app.include_router(auction_scan.router)
app.include_router(regime.router)
app.include_router(analysis.router)
app.include_router(pipeline.router)
app.include_router(data.router)
app.include_router(ext_data.router)
app.include_router(financials.router)
app.include_router(stock_analysis.router)
app.include_router(market_recap.router)
app.include_router(news.router)
app.include_router(fund_flow.router)
app.include_router(limit_board.router)
app.include_router(research.router)
app.include_router(settings_api.router)
app.include_router(strategy.router)
app.include_router(signals.router)
app.include_router(trade_signals.router)
app.include_router(monitor_rules.router)
app.include_router(lots.router)
app.include_router(alerts.router)
app.include_router(events.router)
app.include_router(rps.router)
app.include_router(sector_rotation.router)

# 二次开发路由与小粒度策略在所有核心路由后注册, 禁止覆盖核心路径。
extension_registry, extension_load_errors = configure_backend_extensions(app)
app.state.extension_registry = extension_registry
app.state.extension_load_errors = extension_load_errors


# 能力门控异常 → 403(而非默认 500)
# 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
# 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
from fastapi import Request
from fastapi.responses import JSONResponse
from app.tickflow.capabilities import CapabilityDenied


@app.get("/api/openapi.json", include_in_schema=False)
async def openapi_contract_view(tier: str = "a"):
    """Tier A 契约视图: 只保留对外开放 (Token 可达) 的端点 = 稳定承诺面。

    开放清单的权威来源是 api_gateway 的规则表 — 规则表即契约, 单源维护。
    二开方以此生成客户端; 未出现在此视图的端点属内部实现, 随时变化。
    """
    spec = app.openapi()
    if tier == "a":
        from app.services import api_gateway

        kept_paths: dict = {}
        for path, ops in spec.get("paths", {}).items():
            kept_ops = {}
            for method, op in ops.items():
                if method in ("get", "post", "put", "delete", "patch") and api_gateway.required_scope(
                    method.upper(), path,
                ):
                    kept_ops[method] = op
            if kept_ops:
                kept_paths[path] = kept_ops
        spec["paths"] = kept_paths
        spec["x-tier"] = "a"
    return JSONResponse(spec)


@app.exception_handler(CapabilityDenied)
async def capability_denied_handler(request: Request, exc: CapabilityDenied) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "suggestion": exc.suggestion},
    )

# 生产期静态文件(前端 dist)
_static = Path(settings.static_dir)
if _static.exists():
    if (_static / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):  # noqa: ARG001
        """所有未匹配路径回退到 index.html — React Router 接管。

        index.html 禁止缓存 (Cache-Control: no-store), 确保浏览器每次拿到
        最新版本引用的 JS/CSS 文件名 (assets 带 hash, 可长缓存)。
        """
        index = _static / "index.html"
        if index.exists():
            return FileResponse(
                index,
                headers={"Cache-Control": "no-store, must-revalidate"},
            )
        return {"error": "frontend not built"}
