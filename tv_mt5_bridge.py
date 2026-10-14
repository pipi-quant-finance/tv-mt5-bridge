# -*- coding: utf-8 -*-
"""
TradingView Webhook -> Flask -> MetaTrader5 -> DuckDB 自动交易桥接服务
===================================================================
适用：FTMO 账户 + TradingView 付费版 Webhook 信号，实现量化自动下单。

特性：
1. DuckDB 本地嵌入式数据库，无需单独数据库服务，单文件存储，记录信号、订单日志、风控状态。
2. 单线程任务队列，规避 MetaTrader5 库的非线程安全问题（IPC 通道单线程）。
3. 信号去重：TradingView alert 携带唯一 signal_id。
4. MT5 断线自动重连（每次任务前 ensure init + login）。
5. FTMO 前置风控：总回撤超过阈值直接拦截开单。
6. 开仓（buy/sell） + 平仓（close，按品种全部平仓）双指令支持。
7. 品种映射表存于 DuckDB：TradingView 品种名 -> MT5 FTMO 品种名，避免硬编码。

环境要求：
- Windows（MetaTrader5 Python 库仅支持 Windows）
- Python 3.10+
- 已安装并登录 FTMO 的 MT5 终端，且【自动交易】按钮打开
- 依赖：pip install flask MetaTrader5 duckdb python-dotenv
"""

import os
import time
import queue
import threading

import duckdb
import MetaTrader5 as mt5
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ========== 基础配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "trading_log.duckdb")

MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASS = os.getenv("MT5_PASS", "")
MT5_SERVER = os.getenv("MT5_SERVER", "FTMO-MT5")

# ========== FTMO 1-Step 风控配置 ==========
# 官方规则（2026-02 版 1-Step Challenge）：
#   - Maximum Daily Loss (MDL) = 初始余额的 3%，每日服务器日界(23:59:59 CET)重置
#   - Maximum Loss (ML) 采用 EOD Trailing = 历史最高日终余额 - 初始余额的 10%
# 可在此或 .env 中覆盖（FTMO_DAILY_LOSS_PCT / FTMO_TOTAL_LOSS_PCT / FTMO_INITIAL_BALANCE）
FTMO_DAILY_LOSS_PCT = float(os.getenv("FTMO_DAILY_LOSS_PCT", "0.03"))   # 单日亏损红线 3%
FTMO_TOTAL_LOSS_PCT = float(os.getenv("FTMO_TOTAL_LOSS_PCT", "0.10"))   # 总亏损红线 10%
# 账户初始余额；留空则首次运行从 MT5 账户自动记录
FTMO_INITIAL_BALANCE = os.getenv("FTMO_INITIAL_BALANCE", "").strip()

# ========== 新闻过滤配置 ==========
# 数据源：Forex Factory 公开经济日历 JSON（可替换为其它兼容端点）
NEWS_FILTER_ENABLED = os.getenv("NEWS_FILTER_ENABLED", "true").lower() in ("1", "true", "yes")
NEWS_API_URL = os.getenv(
    "NEWS_API_URL",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
)
NEWS_IMPACT = [x.strip() for x in os.getenv("NEWS_IMPACT", "High,Medium").split(",") if x.strip()]
NEWS_MINUTES_BEFORE = int(os.getenv("NEWS_MINUTES_BEFORE", "5"))  # 发布前 N 分钟
NEWS_MINUTES_AFTER = int(os.getenv("NEWS_MINUTES_AFTER", "5"))    # 发布后 N 分钟
# 拦截的币种；留空=全部。可配合品种自动匹配（见 symbol_countries）
NEWS_COUNTRIES = [x.strip().upper() for x in os.getenv("NEWS_COUNTRIES", "").split(",") if x.strip()]
NEWS_REFRESH_SEC = int(os.getenv("NEWS_REFRESH_SEC", "1800"))     # 日历刷新周期(秒)

# ========== 交易时段 / 单日开仓次数（风控增强） ==========
# 停用时段只约束开仓(buy/sell)，平仓(close)始终允许（用于紧急离场）。
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").lower() in ("1", "true", "yes")  # 总开关
# 允许开仓的星期（1=周一 .. 7=周日），默认周一至周五
TRADING_OPEN_DAYS = [int(x) for x in os.getenv("TRADING_OPEN_DAYS", "1,2,3,4,5").split(",") if x.strip()]
# 允许开仓的 UTC 时间窗口（HH:MM-HH:MM），默认全天
TRADING_OPEN_HOURS = os.getenv("TRADING_OPEN_HOURS", "00:00-23:59")
# 节假日（UTC 日期，YYYY-MM-DD，逗号分隔）
TRADING_HOLIDAYS = [x.strip() for x in os.getenv("TRADING_HOLIDAYS", "").split(",") if x.strip()]
# 单日最大开仓次数（0=不限），按 UTC 日重置
MAX_OPEN_PER_DAY = int(os.getenv("MAX_OPEN_PER_DAY", "0"))

# ========== Webhook 安全校验 ==========
# 唯一 TOKEN，配置在 .env；TradingView 调用 /webhook 时需携带（body 的 "token" 字段，
# 或请求头 X-Webhook-Token）。留空则关闭校验（不建议）。
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()

# 下单任务队列：所有 MT5 调用串行执行
task_queue = queue.Queue(maxsize=50)


# =====================================================================
# DuckDB 初始化（含品种映射表）
# =====================================================================
def init_duckdb():
    con = duckdb.connect(DB_PATH)
    # TradingView 原始信号表
    con.execute("""
    CREATE TABLE IF NOT EXISTS tv_signals (
        signal_id   VARCHAR PRIMARY KEY,
        action      VARCHAR,
        tv_symbol   VARCHAR,
        mt5_symbol  VARCHAR,
        lot         DOUBLE,
        sl          DOUBLE,
        tp          DOUBLE,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed   BOOLEAN  DEFAULT FALSE,
        note        VARCHAR
    )
    """)
    # MT5 订单日志
    con.execute("""
    CREATE TABLE IF NOT EXISTS mt5_orders (
        order_ticket BIGINT PRIMARY KEY,
        signal_id    VARCHAR,
        mt5_symbol   VARCHAR,
        action       VARCHAR,
        lot          DOUBLE,
        sl           DOUBLE,
        tp           DOUBLE,
        price        DOUBLE,
        retcode      INTEGER,
        comment      VARCHAR,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # 每日账户状态表
    con.execute("""
    CREATE TABLE IF NOT EXISTS daily_account (
        dt           DATE PRIMARY KEY,
        balance      DOUBLE,
        equity       DOUBLE,
        daily_pnl    DOUBLE,
        max_equity   DOUBLE,
        updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # 品种映射表：TV_SYMBOL -> MT5_SYMBOL
    con.execute("""
    CREATE TABLE IF NOT EXISTS symbol_mapping (
        tv_symbol   VARCHAR PRIMARY KEY,
        mt5_symbol  VARCHAR NOT NULL,
        comment     VARCHAR
    )
    """)
    # 预置常用映射，可在 DuckDB 中增删
    con.execute("""
    INSERT OR IGNORE INTO symbol_mapping(tv_symbol, mt5_symbol, comment) VALUES
    ('XAUUSD','GOLD','黄金'),
    ('XAGUSD','SILVER','白银'),
    ('EURUSD','EURUSD','欧美'),
    ('GBPUSD','GBPUSD','镑美'),
    ('USDJPY','USDJPY','美日'),
    ('USDCAD','USDCAD','美加'),
    ('AUDUSD','AUDUSD','澳美'),
    ('NZDUSD','NZDUSD','纽美')
    """)
    # 运行态配置表：存 FTMO 风控基线(初始余额/最高日终余额/当前交易日)
    con.execute("""
    CREATE TABLE IF NOT EXISTS runtime_config (
        key    VARCHAR PRIMARY KEY,
        value  DOUBLE
    )
    """)
    # 新闻日历表：存本周经济日历(新闻过滤用)
    con.execute("""
    CREATE TABLE IF NOT EXISTS news_events (
        id             VARCHAR PRIMARY KEY,
        title          VARCHAR,
        country        VARCHAR,
        impact         VARCHAR,
        event_time_utc TIMESTAMP,
        fetched_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # 若 .env 指定了初始余额，写入作为风控基线（仅首次，不覆盖已有）
    if FTMO_INITIAL_BALANCE:
        try:
            initial = float(FTMO_INITIAL_BALANCE)
            con.execute(
                "INSERT INTO runtime_config(key, value) VALUES ('initial_balance', ?) "
                "ON CONFLICT(key) DO NOTHING", [initial]
            )
        except ValueError:
            pass
    con.commit()
    con.close()


# =====================================================================
# DuckDB 辅助函数
# =====================================================================
def get_mt5_symbol(tv_symbol: str):
    con = duckdb.connect(DB_PATH)
    row = con.execute(
        "SELECT mt5_symbol FROM symbol_mapping WHERE tv_symbol = ?", [tv_symbol]
    ).fetchone()
    con.close()
    return row[0] if row else None


def is_signal_duplicated(signal_id: str) -> bool:
    con = duckdb.connect(DB_PATH)
    r = con.execute(
        "SELECT 1 FROM tv_signals WHERE signal_id = ?", [signal_id]
    ).fetchone()
    con.close()
    return r is not None


def insert_signal(signal: dict, mt5_symbol, note=""):
    con = duckdb.connect(DB_PATH)
    con.execute("""
    INSERT INTO tv_signals(signal_id, action, tv_symbol, mt5_symbol, lot, sl, tp, note)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(signal_id) DO NOTHING
    """, [
        signal["signal_id"], signal["action"], signal["symbol"], mt5_symbol,
        signal.get("lot", 0), signal.get("sl", 0), signal.get("tp", 0), note,
    ])
    con.commit()
    con.close()


def mark_signal_processed(signal_id: str):
    con = duckdb.connect(DB_PATH)
    con.execute(
        "UPDATE tv_signals SET processed = true WHERE signal_id = ?", [signal_id]
    )
    con.commit()
    con.close()


def insert_order_result(signal_id, res, mt5_symbol, action, lot, sl, tp):
    con = duckdb.connect(DB_PATH)
    ticket = res.order if res.retcode == 0 else -1
    con.execute("""
    INSERT INTO mt5_orders(
        order_ticket, signal_id, mt5_symbol, action, lot, sl, tp, price, retcode, comment
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(order_ticket) DO NOTHING
    """, [
        ticket, signal_id, mt5_symbol, action, lot, sl, tp, res.price, res.retcode, res.comment,
    ])
    con.commit()
    con.close()


# =====================================================================
# MetaTrader5 底层函数
# =====================================================================
# MT5 库非线程安全：Flask 请求线程 / Worker 线程 / 快照线程都会访问 mt5，
# 用可重入锁(RLock)串行化所有 mt5 调用。
MT5_LOCK = threading.RLock()


def mt5_locked(fn):
    def wrapper(*args, **kwargs):
        with MT5_LOCK:
            return fn(*args, **kwargs)
    return wrapper


@mt5_locked
def mt5_ensure_init() -> bool:
    """初始化 + 登录，断线自动重试"""
    if mt5.terminal_info() is not None:
        return True
    print("[MT5] 尝试初始化...")
    if not mt5.initialize():
        print(f"[MT5] init failed, err: {mt5.last_error()}")
        return False
    if not mt5.login(MT5_LOGIN, password=MT5_PASS, server=MT5_SERVER):
        print(f"[MT5] login failed, err: {mt5.last_error()}")
        mt5.shutdown()
        return False
    print("[MT5] 登录成功")
    return True


@mt5_locked
def get_account_risk_status():
    """读取账户资金，计算回撤（FTMO 前置风控）"""
    if not mt5_ensure_init():
        return None
    acc = mt5.account_info()
    if acc is None:
        return None
    balance = acc.balance
    equity = acc.equity
    drawdown_pct = (balance - equity) / balance if balance > 0 else 0
    return {"balance": balance, "equity": equity, "drawdown_pct": drawdown_pct}


@mt5_locked
def send_open_order(mt5_symbol: str, action: str, lot: float, sl: float, tp: float):
    """开仓：buy / sell"""
    if not mt5_ensure_init():
        return {"ok": False, "msg": "MT5 未连接"}
    tick = mt5.symbol_info_tick(mt5_symbol)
    if tick is None:
        return {"ok": False, "msg": f"MT5 品种不存在: {mt5_symbol}"}
    action = action.lower()
    if action == "buy":
        order_type, price = mt5.ORDER_TYPE_BUY, tick.ask
    elif action == "sell":
        order_type, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        return {"ok": False, "msg": "开仓 action 只能为 buy/sell"}

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 10,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    return {"ok": res.retcode == 0, "result": res}


# =====================================================================
# FTMO 1-Step 风控（双红线：MDL 3%初始 / ML 10%初始·EOD Trailing）
# =====================================================================
def _get_config(key: str, default: float = 0.0):
    con = duckdb.connect(DB_PATH)
    r = con.execute("SELECT value FROM runtime_config WHERE key=?", [key]).fetchone()
    con.close()
    return r[0] if r else default


def _set_config(key: str, value: float):
    con = duckdb.connect(DB_PATH)
    con.execute(
        "INSERT INTO runtime_config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [key, value],
    )
    con.commit()
    con.close()


def maintain_risk_state():
    """维护 FTMO 1-Step 风控基线：
    - initial_balance：账户初始余额（首次自动记录，或来自 .env）
    - max_eod_balance：历史最高日终余额（EOD Trailing 的追踪锚点）
    """
    risk = get_account_risk_status()
    if risk is None:
        return
    balance, equity = risk["balance"], risk["equity"]
    con = duckdb.connect(DB_PATH)
    # 初始余额
    init = con.execute("SELECT value FROM runtime_config WHERE key='initial_balance'").fetchone()
    if not init:
        con.execute(
            "INSERT INTO runtime_config(key, value) VALUES ('initial_balance', ?) "
            "ON CONFLICT(key) DO NOTHING", [balance]
        )
    init = con.execute("SELECT value FROM runtime_config WHERE key='initial_balance'").fetchone()
    base = init[0] if init else balance
    # 最高日终余额：取历史最大 balance（近似历史最高日终余额，EOD Trailing 锚点）
    # 必须与已有锚点取较大者 —— 锚点只上移、不回撤（官方 EOD Trailing 语义）
    prev_max = con.execute("SELECT value FROM runtime_config WHERE key='max_eod_balance'").fetchone()
    prev = prev_max[0] if prev_max else base
    mx = con.execute("SELECT MAX(balance) FROM daily_account").fetchone()[0]
    max_eod = max(base, prev, mx or 0)
    con.execute(
        "INSERT INTO runtime_config(key, value) VALUES ('max_eod_balance', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [max_eod]
    )
    con.commit()
    con.close()


def get_ftmo_risk(risk: dict) -> dict:
    """计算 FTMO 1-Step 双红线占用率，返回是否允许下单。

    MDL：单日亏损 = (初始余额 - 当前权益) / 初始余额，>= FTMO_DAILY_LOSS_PCT 拦截
    ML ：总亏损 = (历史最高日终余额 - 当前权益) / 初始余额，>= FTMO_TOTAL_LOSS_PCT 拦截
    """
    init = _get_config("initial_balance", risk.get("balance", 0.0))
    max_eod = _get_config("max_eod_balance", init)
    equity = risk.get("equity", 0.0)
    daily_loss_pct = (init - equity) / init if init > 0 else 0.0
    total_loss_pct = (max_eod - equity) / init if init > 0 else 0.0
    daily_ok = daily_loss_pct < FTMO_DAILY_LOSS_PCT
    total_ok = total_loss_pct < FTMO_TOTAL_LOSS_PCT
    return {
        "initial_balance": init,
        "max_eod_balance": max_eod,
        "equity": equity,
        "daily_loss_pct": daily_loss_pct,
        "total_loss_pct": total_loss_pct,
        "daily_ok": daily_ok,
        "total_ok": total_ok,
        "allow_trade": daily_ok and total_ok,
    }


def is_trading_allowed():
    """交易时段检查（只约束开仓）。返回 (allowed, reason)。"""
    import datetime as _dt
    if not TRADING_ENABLED:
        return False, "全局交易停用(TRADING_ENABLED=false)"
    now = _dt.datetime.now(_dt.timezone.utc)
    wd = now.isoweekday()
    if wd not in TRADING_OPEN_DAYS:
        return False, f"今日星期{wd} 不在允许开仓日{TRADING_OPEN_DAYS}"
    today = now.strftime("%Y-%m-%d")
    if TRADING_HOLIDAYS and today in TRADING_HOLIDAYS:
        return False, f"节假日停用:{today}"
    try:
        a, b = TRADING_OPEN_HOURS.split("-")

        def _mm(x):
            h, m = x.split(":")
            return int(h) * 60 + int(m)

        lo, hi = _mm(a), _mm(b)
    except Exception:
        lo, hi = 0, 1439
    cur = now.hour * 60 + now.minute
    if not (lo <= cur <= hi):
        return False, f"当前UTC {now.strftime('%H:%M')} 不在开仓窗口 {TRADING_OPEN_HOURS}"
    return True, ""


def _open_count_day() -> int:
    """返回 UTC 日的 YYYYMMDD 整数，用于按日重置开仓计数。"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    return int(now.strftime("%Y%m%d"))


def get_open_count_today() -> int:
    """今日(UTC)已成功开仓次数；跨日自动按 0 计。"""
    day_num = _open_count_day()
    con = duckdb.connect(DB_PATH)
    try:
        d = con.execute("SELECT value FROM runtime_config WHERE key='open_count_date'").fetchone()
        if not d or int(d[0]) != day_num:
            return 0
        c = con.execute("SELECT value FROM runtime_config WHERE key='open_count'").fetchone()
        return int(c[0] or 0) if c else 0
    finally:
        con.close()


def incr_open_count():
    """开仓成功后计数 +1（跨日自动重置后从 1 开始）。"""
    day_num = _open_count_day()
    con = duckdb.connect(DB_PATH)
    try:
        d = con.execute("SELECT value FROM runtime_config WHERE key='open_count_date'").fetchone()
        if not d or int(d[0]) != day_num:
            con.execute(
                "INSERT INTO runtime_config(key,value) VALUES('open_count_date',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [float(day_num)]
            )
            con.execute(
                "INSERT INTO runtime_config(key,value) VALUES('open_count',1) "
                "ON CONFLICT(key) DO UPDATE SET value=1"
            )
        else:
            c = con.execute("SELECT value FROM runtime_config WHERE key='open_count'").fetchone()
            n = int(c[0] or 0) + 1
            con.execute(
                "INSERT INTO runtime_config(key,value) VALUES('open_count',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [float(n)]
            )
        con.commit()
    finally:
        con.close()


def write_daily_snapshot(risk: dict):
    """把当前账户状态写入 daily_account（供 Dashboard 资金曲线使用）。
    同一 Python 进程（bridge）写入，避免多进程抢占 MT5。"""
    import datetime
    today = datetime.date.today().isoformat()
    balance, equity = risk["balance"], risk["equity"]
    con = duckdb.connect(DB_PATH)
    # 当日起始余额：取当天第一条记录的 balance 作为基线
    row = con.execute(
        "SELECT balance FROM daily_account WHERE dt = ? ORDER BY updated_at LIMIT 1",
        [today],
    ).fetchone()
    baseline = row[0] if row else balance
    daily_pnl = equity - baseline
    con.execute("""
    INSERT INTO daily_account(dt, balance, equity, daily_pnl, max_equity)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(dt) DO UPDATE SET
        balance     = excluded.balance,
        equity      = excluded.equity,
        daily_pnl   = excluded.daily_pnl,
        max_equity  = GREATEST(max_equity, excluded.equity),
        updated_at  = CURRENT_TIMESTAMP
    """, [today, balance, equity, daily_pnl, equity])
    con.commit()
    con.close()


def account_snapshot_loop(interval: int = 300):
    """后台线程：定期读取 MT5 账户状态写入 daily_account。
    interval 单位秒，默认 5 分钟。MT5 不可用时静默跳过。"""
    print(f"[SNAPSHOT] 账户快照线程启动，每 {interval}s 写入一次")
    while True:
        try:
            risk = get_account_risk_status()
            if risk is not None:
                write_daily_snapshot(risk)
                maintain_risk_state()   # 同步维护 FTMO 1-Step 风控基线
        except Exception as e:
            print(f"[SNAPSHOT] 异常: {e}")
        time.sleep(interval)


# =====================================================================
# 新闻过滤（Forex Factory 公开经济日历）
# =====================================================================
_MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]
_SYMBOL_CURRENCY = {
    "GOLD": "USD", "XAUUSD": "USD", "SILVER": "USD", "XAGUSD": "USD",
    "OIL": "USD", "WTI": "USD", "USOIL": "USD", "BRENT": "GBP",
    "BTC": "USD", "ETH": "USD", "#US500": "USD", "US30": "USD", "NAS100": "USD",
}


def symbol_countries(symbol: str):
    """从 MT5 品种名推断涉及的币种（用于精准新闻过滤）"""
    s = symbol.upper()
    if s in _SYMBOL_CURRENCY:
        return [_SYMBOL_CURRENCY[s]]
    return [c for c in _MAJOR_CURRENCIES if c in s]


def fetch_news_calendar() -> bool:
    """从 Forex Factory 公开 JSON 拉取本周经济日历并写入 news_events。
    失败返回 False（不阻塞交易，仅记录）。"""
    import json as _json
    import datetime as _dt
    import urllib.request as _url
    try:
        req = _url.Request(NEWS_API_URL, headers={"User-Agent": "Mozilla/5.0"})
        with _url.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        rows = []
        for ev in data:
            title = (ev.get("title") or "")[:200]
            country = (ev.get("country") or "").upper()
            impact = (ev.get("impact") or "").capitalize()
            date_raw = ev.get("date")
            if not date_raw:
                continue
            try:
                ts = _dt.datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                ts = ts.astimezone(_dt.timezone.utc)
            except Exception:
                continue
            key = f"{ts.isoformat()}|{country}|{title}"[:300]
            rows.append((key, title, country, impact, ts))
        con = duckdb.connect(DB_PATH)
        con.execute("DELETE FROM news_events")
        con.executemany(
            "INSERT INTO news_events(id, title, country, impact, event_time_utc) "
            "VALUES (?,?,?,?,?)", rows
        )
        con.commit()
        con.close()
        print(f"[NEWS] 日历已刷新: {len(rows)} 条事件")
        return True
    except Exception as e:
        print(f"[NEWS] 拉取失败: {e}")
        return False


def news_fetch_loop():
    """后台线程：定期刷新经济日历。"""
    print(f"[NEWS] 新闻过滤线程启动，每 {NEWS_REFRESH_SEC}s 刷新")
    fetch_news_calendar()
    while True:
        time.sleep(NEWS_REFRESH_SEC)
        try:
            fetch_news_calendar()
        except Exception as e:
            print(f"[NEWS] 刷新异常: {e}")


def is_news_window(symbol: str = None):
    """判断当前(UTC)是否处于配置的新闻窗口内。
    返回 (blocked, 匹配事件列表)。
    过滤优先级：NEWS_COUNTRIES(显式配置) > 品种相关币种(symbol) > 全量。
    """
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    win_start = now - _dt.timedelta(minutes=NEWS_MINUTES_BEFORE)
    win_end = now + _dt.timedelta(minutes=NEWS_MINUTES_AFTER)
    con = duckdb.connect(DB_PATH)
    rows = con.execute(
        "SELECT title, country, impact, event_time_utc FROM news_events "
        "WHERE event_time_utc BETWEEN ? AND ?",
        [win_start, win_end],
    ).fetchall()
    con.close()
    matched = []
    sym_ccys = symbol_countries(symbol) if symbol else []
    for title, country, impact, ts in rows:
        if impact not in NEWS_IMPACT:
            continue
        if NEWS_COUNTRIES:
            if country not in NEWS_COUNTRIES:
                continue
        elif sym_ccys:
            if country not in sym_ccys:
                continue
        matched.append({
            "title": title, "country": country, "impact": impact,
            "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return bool(matched), matched


@mt5_locked
def close_all_by_symbol(mt5_symbol: str):
    """平仓：指定品种全部持仓清仓"""
    if not mt5_ensure_init():
        return {"ok": False, "msg": "MT5 未连接"}
    positions = mt5.positions_get(symbol=mt5_symbol)
    if positions is None or len(positions) == 0:
        return {"ok": True, "msg": f"{mt5_symbol} 无持仓，无需平仓"}
    results = []
    for pos in positions:
        tick = mt5.symbol_info_tick(mt5_symbol)
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type, close_price = mt5.ORDER_TYPE_SELL, tick.bid
        else:
            close_type, close_price = mt5.ORDER_TYPE_BUY, tick.ask
        close_req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": close_price,
            "deviation": 10,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(close_req)
        results.append({"ticket": pos.ticket, "retcode": res.retcode, "comment": res.comment})
    return {"ok": True, "msg": "平仓指令执行完成", "details": results}


@mt5_locked
def close_position_by_ticket(ticket: int):
    """精确平仓：按持仓 ticket 平掉指定一笔持仓"""
    if not mt5_ensure_init():
        return {"ok": False, "msg": "MT5 未连接"}
    positions = mt5.positions_get(ticket=ticket)
    if positions is None or len(positions) == 0:
        return {"ok": False, "msg": f"未找到持仓 ticket={ticket}"}
    pos = positions[0]
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return {"ok": False, "msg": f"品种无行情: {pos.symbol}"}
    if pos.type == mt5.ORDER_TYPE_BUY:
        close_type, close_price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        close_type, close_price = mt5.ORDER_TYPE_BUY, tick.ask
    close_req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": close_type,
        "position": pos.ticket,
        "price": close_price,
        "deviation": 10,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(close_req)
    return {"ok": res.retcode == 0, "result": res}


def get_open_positions():
    """读取当前全部持仓（Dashboard 展示用）"""
    with MT5_LOCK:
        if not mt5_ensure_init():
            return None
        positions = mt5.positions_get()
        if positions is None:
            return []
        rows = []
        for p in positions:
            rows.append({
                "ticket": p.ticket, "symbol": p.symbol,
                "type": "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume": p.volume, "open_price": p.price_open,
                "sl": p.sl, "tp": p.tp, "profit": p.profit,
                "open_time": p.time,
            })
        return rows


def get_history_profit(days: int = 7):
    """读取最近 N 天历史成交(deal)，统计已实现盈亏（Dashboard 展示用）"""
    from datetime import datetime, timedelta
    dt_from = datetime.now() - timedelta(days=days)
    dt_to = datetime.now() + timedelta(days=1)
    with MT5_LOCK:
        if not mt5_ensure_init():
            return None
        deals = mt5.history_deals_get(dt_from, dt_to)
        if deals is None:
            return []
        rows = []
        for d in deals:
            if abs(d.profit) < 1e-9:
                continue  # 只统计平仓产生的已实现盈亏
            rows.append({
                "ticket": d.ticket, "order": d.order, "symbol": d.symbol,
                "type": "buy" if d.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume": d.volume, "price": d.price, "profit": d.profit,
                "time": datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S"),
            })
        return rows


# =====================================================================
# Worker 任务消费线程（单线程串行处理订单，规避 mt5 线程不安全）
# =====================================================================
def worker_loop():
    print("[WORKER] 线程启动，串行处理订单")
    while True:
        try:
            task = task_queue.get(timeout=1)
            signal = task["signal"]
            signal_id = signal["signal_id"]
            tv_symbol = signal["symbol"]
            action = signal["action"].lower()
            print(f"[WORKER] 收到信号 {signal_id}, action={action}, tv_symbol={tv_symbol}")

            # 1) 品种映射翻译
            mt5_symbol = get_mt5_symbol(tv_symbol)
            if mt5_symbol is None:
                insert_signal(signal, mt5_symbol=None, note=f"品种映射不存在: {tv_symbol}")
                mark_signal_processed(signal_id)
                task_queue.task_done()
                continue

            # 2) FTMO 1-Step 风控校验（双红线：MDL 3%初始 / ML 10%初始·EOD Trailing）
            risk = get_account_risk_status()
            if risk is None:
                insert_signal(signal, mt5_symbol, note="MT5 账户读取失败，拦截")
                mark_signal_processed(signal_id)
                task_queue.task_done()
                continue
            maintain_risk_state()   # 下单前刷新基线（初始余额 / 最高日终余额）
            fr = get_ftmo_risk(risk)
            if not fr["allow_trade"]:
                insert_signal(
                    signal, mt5_symbol,
                    note=(
                        f"FTMO 1-Step 风控拦截: "
                        f"单日亏损{fr['daily_loss_pct']:.2%}/{FTMO_DAILY_LOSS_PCT:.0%}, "
                        f"总亏损{fr['total_loss_pct']:.2%}/{FTMO_TOTAL_LOSS_PCT:.0%}"
                    ),
                )
                mark_signal_processed(signal_id)
                task_queue.task_done()
                continue

            # 3) 新闻过滤（高影响事件前后窗口内拦截）
            if NEWS_FILTER_ENABLED and action in ("buy", "sell"):
                blocked, events = is_news_window(mt5_symbol)
                if blocked:
                    evs = "; ".join(
                        f"{e['country']} {e['title']}({e['impact']})@{e['time']}"
                        for e in events[:5]
                    )
                    insert_signal(signal, mt5_symbol, note=f"新闻过滤拦截: {evs}")
                    mark_signal_processed(signal_id)
                    task_queue.task_done()
                    continue

            # 4) 交易时段 / 单日开仓次数（仅开仓）
            if action in ("buy", "sell"):
                allowed, reason = is_trading_allowed()
                if not allowed:
                    insert_signal(signal, mt5_symbol, note=f"交易时段拦截: {reason}")
                    mark_signal_processed(signal_id)
                    task_queue.task_done()
                    continue
                if MAX_OPEN_PER_DAY > 0 and get_open_count_today() >= MAX_OPEN_PER_DAY:
                    insert_signal(
                        signal, mt5_symbol,
                        note=f"单日开仓次数限制: 今日已达 {MAX_OPEN_PER_DAY} 笔",
                    )
                    mark_signal_processed(signal_id)
                    task_queue.task_done()
                    continue

            # 5) 分支：开仓 / 平仓
            if action in ("buy", "sell"):
                lot = float(signal["lot"])
                sl = float(signal["sl"])
                tp = float(signal["tp"])
                resp = send_open_order(mt5_symbol, action, lot, sl, tp)
                insert_signal(signal, mt5_symbol, note=str(resp))
                if resp["ok"]:
                    incr_open_count()   # 计入单日开仓次数
                    r = resp["result"]
                    insert_order_result(signal_id, r, mt5_symbol, action, lot, sl, tp)

            elif action == "close":
                resp = close_all_by_symbol(mt5_symbol)
                insert_signal(signal, mt5_symbol, note=str(resp))

            else:
                insert_signal(signal, mt5_symbol, note=f"未知 action: {action}")

            mark_signal_processed(signal_id)
            task_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            print(f"[WORKER] 异常: {e}")
            time.sleep(1)


# =====================================================================
# Flask Webhook 接口
# =====================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(silent=True) or {}
        # 0) TOKEN 校验（唯一鉴权；WEBHOOK_TOKEN 为空时不校验，但不建议）
        if WEBHOOK_TOKEN:
            got = (request.headers.get("X-Webhook-Token") or "").strip() or \
                (payload.get("token") or "").strip()
            if got != WEBHOOK_TOKEN:
                return jsonify({"ok": False, "msg": "TOKEN 校验失败，拒绝执行"}), 401
        required = ["signal_id", "action", "symbol"]
        for k in required:
            if k not in payload:
                return jsonify({"ok": False, "msg": f"缺少字段 {k}"}), 400
        signal_id = payload["signal_id"]
        if is_signal_duplicated(signal_id):
            return jsonify({"ok": False, "msg": "重复信号，跳过"}), 200
        insert_signal(payload, mt5_symbol=None, note="收到信号，入队")
        task_queue.put({"signal": payload})
        return jsonify({"ok": True, "msg": "信号入队成功"}), 200
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/account", methods=["GET"])
def api_account():
    """实时账户状态 + FTMO 1-Step 风控占用率（Dashboard 调用，只读）"""
    risk = get_account_risk_status()
    if risk is None:
        return jsonify({"ok": False, "msg": "MT5 不可用或未登录"}), 502
    fr = get_ftmo_risk(risk)
    return jsonify({"ok": True, **risk, "risk": fr})


@app.route("/api/rules", methods=["GET"])
def api_rules():
    """风控规则配置与实时状态（Dashboard 管理页展示）"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    allowed, reason = is_trading_allowed()
    return jsonify({
        "ok": True,
        "now_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ftmo": {
            "daily_loss_pct": FTMO_DAILY_LOSS_PCT,
            "total_loss_pct": FTMO_TOTAL_LOSS_PCT,
        },
        "news_filter_enabled": NEWS_FILTER_ENABLED,
        "news_impact": NEWS_IMPACT,
        "news_window": [NEWS_MINUTES_BEFORE, NEWS_MINUTES_AFTER],
        "trading_enabled": TRADING_ENABLED,
        "trading_open_days": TRADING_OPEN_DAYS,
        "trading_open_hours": TRADING_OPEN_HOURS,
        "trading_holidays": TRADING_HOLIDAYS,
        "trading_allowed_now": allowed,
        "trading_reason": reason,
        "open_count": get_open_count_today(),
        "open_count_max": MAX_OPEN_PER_DAY,
    })


@app.route("/api/positions", methods=["GET"])
def api_positions():
    """当前全部持仓（Dashboard 展示）"""
    rows = get_open_positions()
    if rows is None:
        return jsonify({"ok": False, "msg": "MT5 不可用或未登录"}), 502
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/close-ticket", methods=["POST"])
def api_close_ticket():
    """按 ticket 精确平仓"""
    data = request.get_json() or {}
    try:
        ticket = int(data.get("ticket") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "ticket 必须为整数"}), 400
    if ticket <= 0:
        return jsonify({"ok": False, "msg": "缺少有效的 ticket"}), 400
    resp = close_position_by_ticket(ticket)
    if not resp["ok"]:
        return jsonify({"ok": False, "msg": resp["msg"]}), 400
    r = resp["result"]
    return jsonify({"ok": True, "retcode": r.retcode, "comment": r.comment, "ticket": ticket})


@app.route("/api/news", methods=["GET"])
def api_news():
    """新闻过滤状态 + 最近经济日历（Dashboard 展示）"""
    import datetime as _dt
    blocked, matched = is_news_window()
    con = duckdb.connect(DB_PATH)
    recent = con.execute(
        "SELECT title, country, impact, event_time_utc, fetched_at "
        "FROM news_events ORDER BY event_time_utc DESC LIMIT 50"
    ).fetchall()
    con.close()
    return jsonify({
        "ok": True,
        "enabled": NEWS_FILTER_ENABLED,
        "impact": NEWS_IMPACT,
        "window": f"发布前后 {NEWS_MINUTES_BEFORE}/{NEWS_MINUTES_AFTER} 分钟",
        "blocked": blocked,
        "matched": matched,
        "recent": [
            {"title": r[0], "country": r[1], "impact": r[2], "time": str(r[3])}
            for r in recent
        ],
    })


@app.route("/api/history", methods=["GET"])
def api_history():
    """最近 N 天历史成交盈亏（Dashboard 分析）"""
    days = request.args.get("days", "7")
    try:
        days = int(days)
    except ValueError:
        days = 7
    rows = get_history_profit(days)
    if rows is None:
        return jsonify({"ok": False, "msg": "MT5 不可用或未登录"}), 502
    total = sum(r["profit"] for r in rows)
    wins = sum(1 for r in rows if r["profit"] > 0)
    return jsonify({"ok": True, "rows": rows, "total": total, "wins": wins, "trades": len(rows)})


# =====================================================================
# 入口
# =====================================================================
if __name__ == "__main__":
    init_duckdb()
    # 启动单线程 worker
    threading.Thread(target=worker_loop, daemon=True).start()
    # 启动账户快照线程（供 Dashboard 资金曲线）
    threading.Thread(
        target=account_snapshot_loop,
        kwargs={"interval": int(os.getenv("SNAPSHOT_INTERVAL", "300"))},
        daemon=True,
    ).start()
    # 启动新闻日历刷新线程（新闻过滤）
    if NEWS_FILTER_ENABLED:
        threading.Thread(target=news_fetch_loop, daemon=True).start()
    # 所有 mt5 调用已由 MT5_LOCK 串行化，可安全开启多线程以支持 Dashboard 并发查询
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    app.run(host=host, port=port, threaded=True)
