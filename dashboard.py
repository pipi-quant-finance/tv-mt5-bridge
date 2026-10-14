# -*- coding: utf-8 -*-
"""
TradingView -> MT5 桥接 —— Dashboard 管理后台
===============================================
读取桥接服务写入的 DuckDB(trading_log.duckdb)，提供：
  - 总览：账户资金/回撤、信号/订单汇总、资金曲线、每日盈亏
  - 信号流：实时查询 + 筛选
  - 订单记录：成交明细
  - 分析：按品种 / 方向 / 每日统计 + 图表
  - 管理：品种映射表增删查
  - 测试：向桥接服务 /webhook 发送模拟信号，验证全链路

说明：
  - Dashboard 为独立进程，运行于不同端口(默认 8080)，只读 DuckDB。
  - 实时账户状态通过 HTTP 调用桥接服务的 /api/account 获取，
    不在本进程内直接访问 MT5，避免与桥接服务抢占 MT5 连接。

依赖：flask duckdb（与 requirements.txt 一致）
启动：python dashboard.py   或  ./run_dashboard.ps1
"""
import os
import json
import urllib.request
import urllib.error

import duckdb
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "trading_log.duckdb")

# 桥接服务的地址（本机），用于实时账户 / 测试信号
BRIDGE_URL = os.getenv("BRIDGE_URL", "http://127.0.0.1:5000")
# 与桥接服务一致的 Webhook TOKEN（测试信号转发时携带，保证能通过鉴权）
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()

app = Flask(__name__)


def _connect():
    return duckdb.connect(DB_PATH)


def ensure_schema():
    """幂等建表（与桥接服务表结构一致），保证 dashboard 先于 bridge 启动也不报错。"""
    con = _connect()
    con.execute("""
    CREATE TABLE IF NOT EXISTS tv_signals (
        signal_id VARCHAR PRIMARY KEY, action VARCHAR, tv_symbol VARCHAR,
        mt5_symbol VARCHAR, lot DOUBLE, sl DOUBLE, tp DOUBLE,
        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        processed BOOLEAN DEFAULT FALSE, note VARCHAR
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS mt5_orders (
        order_ticket BIGINT PRIMARY KEY, signal_id VARCHAR, mt5_symbol VARCHAR,
        action VARCHAR, lot DOUBLE, sl DOUBLE, tp DOUBLE, price DOUBLE,
        retcode INTEGER, comment VARCHAR,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS daily_account (
        dt DATE PRIMARY KEY, balance DOUBLE, equity DOUBLE, daily_pnl DOUBLE,
        max_equity DOUBLE, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""
    CREATE TABLE IF NOT EXISTS symbol_mapping (
        tv_symbol VARCHAR PRIMARY KEY, mt5_symbol VARCHAR NOT NULL, comment VARCHAR
    )""")
    con.commit()
    con.close()


# ---------------- API：总览 ----------------
@app.route("/api/overview")
def api_overview():
    con = _connect()
    try:
        # 信号汇总
        sig_total, sig_processed, sig_open = con.sql(
            "SELECT COUNT(*), SUM(CASE WHEN processed THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN action IN ('buy','sell') AND NOT processed THEN 1 ELSE 0 END) "
            "FROM tv_signals"
        ).fetchone()
        # 订单汇总
        ord_total, ord_ok = con.sql(
            "SELECT COUNT(*), SUM(CASE WHEN retcode=0 THEN 1 ELSE 0 END) FROM mt5_orders"
        ).fetchone()
        # 每日账户
        daily = con.sql(
            "SELECT dt, balance, equity, daily_pnl, max_equity "
            "FROM daily_account ORDER BY dt"
        ).fetchall()
        # 品种映射数量
        mapping_count = con.sql("SELECT COUNT(*) FROM symbol_mapping").fetchone()[0]
        # 按天统计正负（粗略"天胜率"）
        win_days = con.sql(
            "SELECT COUNT(*) FROM daily_account WHERE daily_pnl > 0"
        ).fetchone()[0]
        day_total = con.sql("SELECT COUNT(*) FROM daily_account").fetchone()[0]

        # 最新账户快照
        last = con.sql(
            "SELECT balance, equity, daily_pnl FROM daily_account ORDER BY dt DESC LIMIT 1"
        ).fetchone()

        # 实时账户（桥接服务），失败则回退到快照
        live = _fetch_account()
        overview = {
            "signal_total": sig_total or 0,
            "signal_processed": sig_processed or 0,
            "signal_pending": sig_open or 0,
            "order_total": ord_total or 0,
            "order_ok": ord_ok or 0,
            "order_fail": (ord_total or 0) - (ord_ok or 0),
            "mapping_count": mapping_count,
            "win_days": win_days or 0,
            "day_total": day_total or 0,
            "daily": [{
                "dt": r[0], "balance": r[1], "equity": r[2],
                "daily_pnl": r[3], "max_equity": r[4],
            } for r in daily],
        }
        if live:
            overview["live"] = live
        elif last:
            overview["live"] = {
                "balance": last[0], "equity": last[1], "daily_pnl": last[2],
            }
        return jsonify({"ok": True, **overview})
    finally:
        con.close()


# ---------------- API：信号流 ----------------
@app.route("/api/signals")
def api_signals():
    limit = min(int(request.args.get("limit", 50)), 500)
    symbol = request.args.get("symbol", "")
    action = request.args.get("action", "")
    con = _connect()
    try:
        sql = "SELECT signal_id, action, tv_symbol, mt5_symbol, lot, sl, tp, " \
              "received_at, processed, note FROM tv_signals WHERE 1=1"
        params = []
        if symbol:
            sql += " AND (tv_symbol=? OR mt5_symbol=?)"
            params += [symbol, symbol]
        if action:
            sql += " AND action=?"
            params.append(action)
        sql += " ORDER BY received_at DESC, signal_id DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        cols = ["signal_id", "action", "tv_symbol", "mt5_symbol", "lot", "sl",
                "tp", "received_at", "processed", "note"]
        return jsonify({"ok": True, "rows": [dict(zip(cols, r)) for r in rows]})
    finally:
        con.close()


# ---------------- API：订单 ----------------
@app.route("/api/orders")
def api_orders():
    limit = min(int(request.args.get("limit", 50)), 500)
    con = _connect()
    try:
        rows = con.execute(
            "SELECT order_ticket, signal_id, mt5_symbol, action, lot, sl, tp, "
            "price, retcode, comment, created_at FROM mt5_orders "
            "ORDER BY created_at DESC LIMIT ?", [limit]
        ).fetchall()
        cols = ["order_ticket", "signal_id", "mt5_symbol", "action", "lot", "sl",
                "tp", "price", "retcode", "comment", "created_at"]
        return jsonify({"ok": True, "rows": [dict(zip(cols, r)) for r in rows]})
    finally:
        con.close()


# ---------------- API：分析 ----------------
@app.route("/api/analysis")
def api_analysis():
    con = _connect()
    try:
        # 按品种统计
        by_symbol = con.sql(
            "SELECT mt5_symbol AS sym, "
            "COUNT(*) AS cnt, "
            "SUM(CASE WHEN retcode=0 THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN action='buy' THEN 1 ELSE 0 END) AS buys, "
            "SUM(CASE WHEN action='sell' THEN 1 ELSE 0 END) AS sells "
            "FROM mt5_orders GROUP BY 1 ORDER BY cnt DESC"
        ).fetchall()
        # 按方向
        by_action = con.sql(
            "SELECT action, COUNT(*) AS cnt FROM mt5_orders GROUP BY action"
        ).fetchall()
        # 每日盈亏
        by_day = con.sql(
            "SELECT dt, daily_pnl FROM daily_account ORDER BY dt"
        ).fetchall()
        # 品种映射
        mapping = con.sql(
            "SELECT tv_symbol, mt5_symbol, comment FROM symbol_mapping ORDER BY tv_symbol"
        ).fetchall()
        return jsonify({
            "ok": True,
            "by_symbol": [
                {"symbol": r[0], "count": r[1], "ok": r[2], "buys": r[3], "sells": r[4]}
                for r in by_symbol
            ],
            "by_action": [{"action": r[0], "count": r[1]} for r in by_action],
            "by_day": [{"dt": r[0], "daily_pnl": r[1]} for r in by_day],
            "mapping": [
                {"tv_symbol": r[0], "mt5_symbol": r[1], "comment": r[2]} for r in mapping
            ],
        })
    finally:
        con.close()


# ---------------- API：品种映射管理 ----------------
@app.route("/api/mapping", methods=["GET", "POST", "DELETE"])
def api_mapping():
    con = _connect()
    try:
        if request.method == "GET":
            rows = con.execute(
                "SELECT tv_symbol, mt5_symbol, comment FROM symbol_mapping ORDER BY tv_symbol"
            ).fetchall()
            return jsonify({"ok": True, "rows": [
                {"tv_symbol": r[0], "mt5_symbol": r[1], "comment": r[2]} for r in rows
            ]})
        if request.method == "POST":
            data = request.get_json() or {}
            tv = (data.get("tv_symbol") or "").strip().upper()
            mt = (data.get("mt5_symbol") or "").strip().upper()
            if not tv or not mt:
                return jsonify({"ok": False, "msg": "tv_symbol 与 mt5_symbol 不能为空"}), 400
            comment = (data.get("comment") or "").strip()
            con.execute(
                "INSERT INTO symbol_mapping(tv_symbol, mt5_symbol, comment) VALUES (?,?,?) "
                "ON CONFLICT(tv_symbol) DO UPDATE SET mt5_symbol=excluded.mt5_symbol, "
                "comment=excluded.comment",
                [tv, mt, comment],
            )
            con.commit()
            return jsonify({"ok": True, "msg": f"已保存映射 {tv} -> {mt}"})
        if request.method == "DELETE":
            tv = (request.args.get("tv_symbol") or "").strip().upper()
            if not tv:
                return jsonify({"ok": False, "msg": "缺少 tv_symbol"}), 400
            con.execute("DELETE FROM symbol_mapping WHERE tv_symbol=?", [tv])
            con.commit()
            return jsonify({"ok": True, "msg": f"已删除 {tv}"})
    finally:
        con.close()


# ---------------- API：测试信号（转发到桥接服务 webhook） ----------------
@app.route("/api/test-signal", methods=["POST"])
def api_test_signal():
    data = request.get_json() or {}
    action = (data.get("action") or "").lower()
    symbol = (data.get("symbol") or "").upper()
    if action not in ("buy", "sell", "close"):
        return jsonify({"ok": False, "msg": "action 只能为 buy/sell/close"}), 400
    if not symbol:
        return jsonify({"ok": False, "msg": "symbol 不能为空"}), 400
    payload = {
        "signal_id": f"test-{int(os.times().elapsed * 1000)}-{action}",
        "action": action,
        "symbol": symbol,
        "lot": float(data.get("lot") or 0),
        "sl": float(data.get("sl") or 0),
        "tp": float(data.get("tp") or 0),
    }
    if WEBHOOK_TOKEN:
        payload["token"] = WEBHOOK_TOKEN
    # 转发到桥接服务 webhook，走真实下单链路
    req = urllib.request.Request(
        f"{BRIDGE_URL}/webhook",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return jsonify({"ok": True, "bridge": body, "payload": payload})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"桥接服务返回 {e.code}", "payload": payload}), 502
    except Exception as e:
        return jsonify({"ok": False, "msg": f"无法连接桥接服务: {e}", "payload": payload}), 502


# ---------------- API：实时持仓 / 历史盈亏 / 按ticket平仓（转发桥接服务） ----------------
@app.route("/api/positions")
def api_positions():
    return _bridge_get("/api/positions")


@app.route("/api/history")
def api_history():
    days = request.args.get("days", "7")
    return _bridge_get(f"/api/history?days={days}")


@app.route("/api/news")
def api_news():
    return _bridge_get("/api/news")


@app.route("/api/rules")
def api_rules():
    return _bridge_get("/api/rules")


@app.route("/api/close-ticket", methods=["POST"])
def api_close_ticket():
    data = request.get_json() or {}
    try:
        ticket = int(data.get("ticket") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "ticket 必须为整数"}), 400
    return _bridge_post("/api/close-ticket", {"ticket": ticket})


# ---------------- 内部：转发桥接服务 ----------------
def _bridge_get(path):
    try:
        req = urllib.request.Request(f"{BRIDGE_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return jsonify(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"桥接服务返回 {e.code}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "msg": f"无法连接桥接服务: {e}"}), 502


def _bridge_post(path, payload):
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "msg": f"桥接服务返回 {e.code}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "msg": f"无法连接桥接服务: {e}"}), 502


# ---------------- 内部：实时账户 ----------------
def _fetch_account():
    try:
        req = urllib.request.Request(f"{BRIDGE_URL}/api/account", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return render_template("dashboard.html")


if __name__ == "__main__":
    ensure_schema()
    host = os.getenv("DASH_HOST", "0.0.0.0")
    port = int(os.getenv("DASH_PORT", "8080"))
    print(f"[DASHBOARD] 启动于 http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)
