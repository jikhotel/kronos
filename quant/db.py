"""SQLite 持久层: 策略 / 回测结果 / 账户 / 持仓 / 订单"""

import os
import json
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quant.db')
_lock = threading.RLock()


def _now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


_initialized = False


def get_conn():
    global _initialized
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _initialized:
        # 首次连接自动建表(幂等), 保证独立调用时无需显式 init_db
        conn.executescript(_SCHEMA)
        conn.commit()
        _initialized = True
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    symbol TEXT NOT NULL,
    freq TEXT DEFAULT 'day',
    initial_capital REAL DEFAULT 100000,
    commission_rate REAL DEFAULT 0.0003,
    slippage REAL DEFAULT 0.001,
    position_ratio REAL DEFAULT 0.95,
    status TEXT DEFAULT 'stopped',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER,
    strategy_name TEXT,
    type TEXT,
    params TEXT,
    symbol TEXT,
    file_path TEXT,
    start_date TEXT,
    end_date TEXT,
    metrics TEXT,
    trades TEXT,
    chart TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    initial_cash REAL,
    cash REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    strategy_id INTEGER,
    symbol TEXT,
    volume INTEGER DEFAULT 0,
    available_volume INTEGER DEFAULT 0,
    avg_cost REAL DEFAULT 0,
    today_buy_volume INTEGER DEFAULT 0,
    updated_at TEXT,
    UNIQUE(account_id, strategy_id, symbol)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    strategy_id INTEGER,
    strategy_name TEXT,
    symbol TEXT,
    side TEXT,
    price REAL,
    volume INTEGER,
    amount REAL,
    status TEXT,
    reason TEXT,
    created_at TEXT
);
"""


def init_db():
    with _lock:
        conn = get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()


# ---------------- 策略 CRUD ----------------

def list_strategies():
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM strategies ORDER BY id DESC").fetchall()
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['params'] = json.loads(d.get('params') or '{}')
        out.append(d)
    return out


def get_strategy(sid):
    with _lock:
        conn = get_conn()
        r = conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        conn.close()
    if not r:
        return None
    d = dict(r)
    d['params'] = json.loads(d.get('params') or '{}')
    return d


def create_strategy(data):
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO strategies
               (name, type, params, symbol, freq, initial_capital, commission_rate,
                slippage, position_ratio, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data['name'], data['type'], json.dumps(data.get('params', {}), ensure_ascii=False),
             data['symbol'], data.get('freq', 'day'),
             float(data.get('initial_capital', 100000)),
             float(data.get('commission_rate', 0.0003)),
             float(data.get('slippage', 0.001)),
             float(data.get('position_ratio', 0.95)),
             data.get('status', 'stopped'), _now(), _now()))
        sid = cur.lastrowid
        conn.commit()
        conn.close()
    return get_strategy(sid)


def update_strategy(sid, data):
    fields, vals = [], []
    for col in ('name', 'type', 'symbol', 'freq', 'status'):
        if col in data and data[col] is not None:
            fields.append(f"{col}=?")
            vals.append(data[col])
    if 'params' in data and data['params'] is not None:
        fields.append("params=?")
        vals.append(json.dumps(data['params'], ensure_ascii=False))
    for col in ('initial_capital', 'commission_rate', 'slippage', 'position_ratio'):
        if col in data and data[col] is not None:
            fields.append(f"{col}=?")
            vals.append(float(data[col]))
    if not fields:
        return get_strategy(sid)
    fields.append("updated_at=?")
    vals.append(_now())
    vals.append(sid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE strategies SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
        conn.close()
    return get_strategy(sid)


def delete_strategy(sid):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM strategies WHERE id=?", (sid,))
        # 级联清理该策略的持仓与订单归属
        conn.execute("DELETE FROM positions WHERE strategy_id=?", (sid,))
        conn.commit()
        conn.close()


# ---------------- 回测结果 ----------------

def save_backtest(row):
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO backtest_results
               (strategy_id, strategy_name, type, params, symbol, file_path,
                start_date, end_date, metrics, trades, chart, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.get('strategy_id'), row.get('strategy_name'), row.get('type'),
             json.dumps(row.get('params', {}), ensure_ascii=False), row.get('symbol'),
             row.get('file_path'), row.get('start_date'), row.get('end_date'),
             json.dumps(row.get('metrics', {}), ensure_ascii=False),
             json.dumps(row.get('trades', []), ensure_ascii=False),
             row.get('chart') or '', _now()))
        rid = cur.lastrowid
        conn.commit()
        conn.close()
    return rid


def list_backtests(limit=30):
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            """SELECT id, strategy_id, strategy_name, type, symbol, file_path,
                      start_date, end_date, metrics, created_at
               FROM backtest_results ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['metrics'] = json.loads(d.get('metrics') or '{}')
        out.append(d)
    return out


def get_backtest(rid):
    with _lock:
        conn = get_conn()
        r = conn.execute("SELECT * FROM backtest_results WHERE id=?", (rid,)).fetchone()
        conn.close()
    if not r:
        return None
    d = dict(r)
    d['metrics'] = json.loads(d.get('metrics') or '{}')
    d['trades'] = json.loads(d.get('trades') or '[]')
    return d


# ---------------- 账户 / 持仓 / 订单 ----------------

DEFAULT_ACCOUNT = 'paper'

def ensure_account(initial_cash=1000000.0):
    with _lock:
        conn = get_conn()
        r = conn.execute("SELECT * FROM accounts WHERE name=?", (DEFAULT_ACCOUNT,)).fetchone()
        if r is None:
            conn.execute(
                "INSERT INTO accounts (name, initial_cash, cash, created_at) VALUES (?,?,?,?)",
                (DEFAULT_ACCOUNT, float(initial_cash), float(initial_cash), _now()))
            conn.commit()
            aid = conn.execute("SELECT id FROM accounts WHERE name=?", (DEFAULT_ACCOUNT,)).fetchone()[0]
        else:
            aid = r['id']
        conn.close()
    return aid


def reset_account(initial_cash=1000000.0):
    """清空账户并重置资金(清持仓/订单)"""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM orders")
        r = conn.execute("SELECT id FROM accounts WHERE name=?", (DEFAULT_ACCOUNT,)).fetchone()
        if r is None:
            conn.execute(
                "INSERT INTO accounts (name, initial_cash, cash, created_at) VALUES (?,?,?,?)",
                (DEFAULT_ACCOUNT, float(initial_cash), float(initial_cash), _now()))
        else:
            conn.execute(
                "UPDATE accounts SET initial_cash=?, cash=? WHERE id=?",
                (float(initial_cash), float(initial_cash), r['id']))
        conn.commit()
        conn.close()
    return ensure_account(initial_cash)


def get_account():
    aid = ensure_account()
    with _lock:
        conn = get_conn()
        r = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        conn.close()
    return dict(r) if r else None


def update_cash(aid, delta):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE accounts SET cash = cash + ? WHERE id=?", (delta, aid))
        conn.commit()
        conn.close()


def get_positions(aid=None):
    aid = aid or ensure_account()
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM positions WHERE account_id=? ORDER BY id DESC", (aid,)).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def upsert_position(aid, strategy_id, symbol, volume, available, avg_cost, today_buy):
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT INTO positions (account_id, strategy_id, symbol, volume,
                                      available_volume, avg_cost, today_buy_volume, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, strategy_id, symbol) DO UPDATE SET
                 volume=excluded.volume,
                 available_volume=excluded.available_volume,
                 avg_cost=excluded.avg_cost,
                 today_buy_volume=excluded.today_buy_volume,
                 updated_at=excluded.updated_at""",
            (aid, strategy_id, symbol, volume, available, avg_cost, today_buy, _now()))
        conn.commit()
        conn.close()


def delete_position(aid, strategy_id, symbol):
    with _lock:
        conn = get_conn()
        conn.execute(
            "DELETE FROM positions WHERE account_id=? AND strategy_id=? AND symbol=?",
            (aid, strategy_id, symbol))
        conn.commit()
        conn.close()


def clear_today_buy_flags(aid):
    """新交易日开盘调用: 昨日买入今晨解禁(T+1)"""
    with _lock:
        conn = get_conn()
        conn.execute(
            """UPDATE positions SET
                 available_volume = available_volume + today_buy_volume,
                 today_buy_volume = 0, updated_at=?
               WHERE account_id=?""", (_now(), aid))
        conn.commit()
        conn.close()


def add_order(aid, strategy_id, strategy_name, symbol, side, price, volume,
              amount, status, reason):
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            """INSERT INTO orders (account_id, strategy_id, strategy_name, symbol, side,
                                   price, volume, amount, status, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, strategy_id, strategy_name, symbol, side, price, volume,
             amount, status, reason, _now()))
        conn.commit()
        oid = cur.lastrowid
        conn.close()
    return oid


def list_orders(aid=None, limit=50):
    aid = aid or ensure_account()
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (aid, limit)).fetchall()
        conn.close()
    return [dict(r) for r in rows]
