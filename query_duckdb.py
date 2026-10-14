# -*- coding: utf-8 -*-
"""
查询 trading_log.duckdb 日志的辅助脚本。
用法: python query_duckdb.py [--signals] [--orders] [--mapping]
"""
import sys
import duckdb

DB = "trading_log.duckdb"


def main():
    con = duckdb.connect(DB)
    args = sys.argv[1:]
    want_signals = (not args) or "--signals" in args
    want_orders = (not args) or "--orders" in args
    want_mapping = (not args) or "--mapping" in args

    if want_mapping:
        print("==== 品种映射表 ====")
        print(con.sql("SELECT * FROM symbol_mapping;"))
    if want_signals:
        print("\n==== 最近20条TV信号 ====")
        print(con.sql(
            "SELECT signal_id, action, tv_symbol, mt5_symbol, lot, sl, tp, received_at, processed, note "
            "FROM tv_signals ORDER BY received_at DESC LIMIT 20;"
        ))
    if want_orders:
        print("\n==== 最近20条MT5订单 ====")
        print(con.sql(
            "SELECT order_ticket, signal_id, mt5_symbol, action, lot, sl, tp, price, retcode, comment, created_at "
            "FROM mt5_orders ORDER BY created_at DESC LIMIT 20;"
        ))
    con.close()


if __name__ == "__main__":
    main()
