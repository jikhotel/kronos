"""实盘交易引擎。

- PaperBroker: 模拟撮合(实时行情价成交, A股约束: 整百股/T+1/涨跌停)
- BrokerBase:  实盘网关接口预留, 后续可接 QMT/PTrade 等券商通道
- LiveEngine:  后台线程轮询 active 策略 -> 计算信号 -> 经 Broker 下单

实盘安全原则: 默认只启用模拟盘(PaperBroker); 接真实券商时实现 BrokerBase
并在 start_engine 显式传入, 引擎不猜测通道类型。
"""

import json
import threading
import time
import datetime

import pandas as pd
import requests

from . import db
from . import strategies as st

UA = {"User-Agent": "Mozilla/5.0"}
QT_API = "https://qt.gtimg.cn/q={symbol}"
KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{n},qfq"

KLINE_CACHE = {}          # symbol -> (fetch_day, df)
KLINE_CACHE_LOCK = threading.Lock()
KRONOS_PRED_CACHE = {}    # strategy_id -> (date, pred_prices)


# ---------------- 行情 ----------------

def get_realtime_quote(symbol):
    """腾讯实时行情 -> dict(price, prev_close, open, name)。失败返回 None。"""
    try:
        r = requests.get(QT_API.format(symbol=symbol), headers=UA, timeout=8)
        r.encoding = 'gbk'
        parts = r.text.split('~')
        if len(parts) < 6 or not parts[3]:
            return None
        return {'name': parts[1], 'price': float(parts[3]),
                'prev_close': float(parts[4]), 'open': float(parts[5])}
    except Exception:
        return None


def get_daily_kline(symbol, n=320, max_age_seconds=3600):
    """日 K 线(前复权), 带内存缓存(1 小时)。返回 DataFrame(OHLCV) 或 None。"""
    with KLINE_CACHE_LOCK:
        cached = KLINE_CACHE.get(symbol)
        if cached is not None:
            ts, df = cached
            if time.time() - ts < max_age_seconds:
                return df
    try:
        r = requests.get(KLINE_API.format(symbol=symbol, n=n), headers=UA, timeout=10)
        data = r.json()['data']
        node = data.get(symbol, {})
        raw = node.get('qfqday') or node.get('day')
        if not raw:
            return None
        rows = []
        for item in raw:
            rows.append({'timestamps': item[0], 'open': float(item[1]),
                         'close': float(item[2]), 'high': float(item[3]),
                         'low': float(item[4]),
                         'volume': int(float(item[5]) * 100) if len(item) > 5 and item[5] else 0})
        df = pd.DataFrame(rows)
        df['timestamps'] = pd.to_datetime(df['timestamps'])
        with KLINE_CACHE_LOCK:
            KLINE_CACHE[symbol] = (time.time(), df)
        return df
    except Exception:
        return None


def limit_range(symbol):
    """涨跌停幅度: 创业板(30x)/科创板(68x) 20%, 其余 A 股 10%, 港股不设限(返回 1.0)。"""
    if symbol.startswith('hk'):
        return 1.0
    code = symbol[2:]
    if code.startswith('30') or code.startswith('68'):
        return 0.20
    return 0.10


# ---------------- Broker 适配层 ----------------

class BrokerBase:
    """实盘交易网关接口。接券商时实现本接口并注入 LiveEngine。"""

    def get_cash(self):
        raise NotImplementedError

    def buy(self, symbol, volume, price, reason):
        """返回订单 dict 或抛异常。"""
        raise NotImplementedError

    def sell(self, symbol, volume, price, reason):
        raise NotImplementedError


class PaperBroker(BrokerBase):
    """模拟盘: SQLite 账户, 实时行情价成交, 含 A 股约束。"""

    def __init__(self, commission_rate=0.0003, slippage=0.001):
        self.commission_rate = commission_rate
        self.slippage = slippage
        self.aid = db.ensure_account()

    def get_cash(self):
        return db.get_account()['cash']

    # ---- 内部 ----
    def _check_limit(self, symbol, side, price, prev_close):
        lim = limit_range(symbol)
        if lim >= 1.0:
            return None
        cap = prev_close * (1 + lim)
        floor_ = prev_close * (1 - lim)
        if side == 'BUY' and price >= cap * 0.999:
            return f"接近/达到涨停({price:.2f} >= {cap:.2f}), 放弃追涨"
        if side == 'SELL' and price <= floor_ * 1.001:
            return f"接近/达到跌停({price:.2f} <= {floor_:.2f}), 无法卖出"
        return None

    def buy(self, symbol, volume, price, reason, strategy=None):
        strat = strategy or {}
        quote = get_realtime_quote(symbol)
        if not quote:
            return {'status': 'rejected', 'reason': '行情获取失败'}
        prev_close = quote['prev_close']
        exec_price = round(quote['price'] * (1 + self.slippage), 3)
        blocked = self._check_limit(symbol, 'BUY', exec_price, prev_close)
        if blocked:
            db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'BUY',
                         exec_price, volume, volume * exec_price, 'rejected', blocked)
            return {'status': 'rejected', 'reason': blocked}

        # 整百股
        cash = self.get_cash()
        budget = cash * 0.98
        lots = int(budget // (exec_price * 100))
        vol = min(lots * 100, volume // 100 * 100)
        if vol <= 0:
            db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'BUY',
                         exec_price, volume, 0, 'rejected', '资金不足一手')
            return {'status': 'rejected', 'reason': '资金不足一手'}
        amount = vol * exec_price
        fee = max(amount * self.commission_rate, 5.0)
        if amount + fee > cash:
            vol -= 100
            if vol <= 0:
                db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'BUY',
                             exec_price, volume, 0, 'rejected', '资金不足(含手续费)')
                return {'status': 'rejected', 'reason': '资金不足(含手续费)'}
            amount = vol * exec_price
            fee = max(amount * self.commission_rate, 5.0)

        db.update_cash(self.aid, -(amount + fee))
        # 更新持仓(摊薄成本)
        pos_rows = [p for p in db.get_positions(self.aid)
                    if p['symbol'] == symbol and p['strategy_id'] == strat.get('id')]
        if pos_rows:
            p = pos_rows[0]
            new_vol = p['volume'] + vol
            new_cost = (p['avg_cost'] * p['volume'] + amount + fee) / new_vol
            db.upsert_position(self.aid, strat.get('id'), symbol, new_vol,
                               p['available_volume'], new_cost, p['today_buy_volume'] + vol)
        else:
            db.upsert_position(self.aid, strat.get('id'), symbol, vol, 0,
                               (amount + fee) / vol, vol)
        oid = db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'BUY',
                           exec_price, vol, amount, 'filled', reason)
        return {'status': 'filled', 'order_id': oid, 'volume': vol,
                'price': exec_price, 'fee': fee}

    def sell(self, symbol, volume, price, reason, strategy=None):
        strat = strategy or {}
        quote = get_realtime_quote(symbol)
        if not quote:
            return {'status': 'rejected', 'reason': '行情获取失败'}
        exec_price = round(quote['price'] * (1 - self.slippage), 3)
        blocked = self._check_limit(symbol, 'SELL', exec_price, quote['prev_close'])
        if blocked:
            db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'SELL',
                         exec_price, volume, volume * exec_price, 'rejected', blocked)
            return {'status': 'rejected', 'reason': blocked}

        pos_rows = [p for p in db.get_positions(self.aid)
                    if p['symbol'] == symbol and p['strategy_id'] == strat.get('id')]
        if not pos_rows:
            db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'SELL',
                         exec_price, volume, 0, 'rejected', '无持仓')
            return {'status': 'rejected', 'reason': '无持仓'}
        p = pos_rows[0]
        if p['available_volume'] <= 0:
            db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'SELL',
                         exec_price, volume, 0, 'rejected', 'T+1: 今日买入不可卖')
            return {'status': 'rejected', 'reason': 'T+1: 今日买入不可卖'}
        vol = min(volume, p['available_volume'])
        amount = vol * exec_price
        fee = max(amount * self.commission_rate, 5.0)
        db.update_cash(self.aid, amount - fee)

        remain = p['volume'] - vol
        if remain > 0:
            db.upsert_position(self.aid, strat.get('id'), symbol, remain,
                               p['available_volume'] - vol, p['avg_cost'],
                               p['today_buy_volume'])
        else:
            db.delete_position(self.aid, strat.get('id'), symbol)
        oid = db.add_order(self.aid, strat.get('id'), strat.get('name'), symbol, 'SELL',
                           exec_price, vol, amount, 'filled', reason)
        return {'status': 'filled', 'order_id': oid, 'volume': vol,
                'price': exec_price, 'fee': fee}


# ---------------- Live Engine ----------------

class LiveEngine:
    """单例后台引擎: 轮询策略 -> 信号 -> 下单。"""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        if LiveEngine._instance is not None:
            raise RuntimeError("LiveEngine 是单例, 请用 LiveEngine.instance()")
        self.broker = None
        self.predict_fn = None          # kronos 预测函数(可选注入)
        self.thread = None
        self.running = False
        self.poll_interval = 60
        self.last_cycle_at = None
        self.last_error = None
        self._cur_day = None

    @classmethod
    def instance(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = LiveEngine()
            return cls._instance

    def start(self, initial_cash=None, predict_fn=None):
        if self.running:
            return True, "引擎已在运行"
        if initial_cash is not None:
            db.reset_account(initial_cash)
        self.broker = PaperBroker()
        self.predict_fn = predict_fn
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name='quant-live')
        self.thread.start()
        return True, "实盘引擎已启动(模拟盘)"

    def stop(self):
        self.running = False
        return True, "引擎已停止(持仓保留)"

    def status(self):
        acct = db.get_account()
        positions = db.get_positions()
        market_value, pnl = 0.0, 0.0
        for p in positions:
            quote = get_realtime_quote(p['symbol'])
            px = quote['price'] if quote else p['avg_cost']
            market_value += px * p['volume']
            pnl += (px - p['avg_cost']) * p['volume']
        total = acct['cash'] + market_value
        return {
            'running': self.running,
            'broker': 'paper(模拟盘)',
            'poll_interval': self.poll_interval,
            'last_cycle_at': self.last_cycle_at,
            'last_error': self.last_error,
            'account': {
                'cash': round(acct['cash'], 2),
                'market_value': round(market_value, 2),
                'total': round(total, 2),
                'initial_cash': round(acct['initial_cash'], 2),
                'total_pnl': round(total - acct['initial_cash'], 2),
                'total_pnl_pct': round((total / acct['initial_cash'] - 1) * 100, 2),
                'position_pnl': round(pnl, 2)
            },
            'positions': self._positions_view(positions),
            'orders': db.list_orders(limit=50)
        }

    def _positions_view(self, positions):
        out = []
        for p in positions:
            quote = get_realtime_quote(p['symbol'])
            px = quote['price'] if quote else p['avg_cost']
            out.append({
                'symbol': p['symbol'], 'name': quote['name'] if quote else p['symbol'],
                'strategy_id': p['strategy_id'],
                'volume': p['volume'], 'available_volume': p['available_volume'],
                'avg_cost': round(p['avg_cost'], 3), 'price': px,
                'market_value': round(px * p['volume'], 2),
                'pnl': round((px - p['avg_cost']) * p['volume'], 2),
                'pnl_pct': round((px / p['avg_cost'] - 1) * 100, 2) if p['avg_cost'] else 0
            })
        return out

    # ---- 主循环 ----
    def _loop(self):
        while self.running:
            try:
                self._cycle()
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            time.sleep(self.poll_interval)

    def _cycle(self):
        today = datetime.date.today().isoformat()
        if self._cur_day != today:
            # 新的一天: 昨日买入解禁可卖(T+1)
            db.clear_today_buy_flags(db.ensure_account())
            self._cur_day = today

        strategies = [s for s in db.list_strategies() if s.get('status') == 'active']
        for strat in strategies:
            try:
                self._run_strategy(strat)
            except Exception as e:
                self.last_error = f"策略[{strat['name']}]执行失败: {e}"
        self.last_cycle_at = time.strftime('%Y-%m-%d %H:%M:%S')

    def _run_strategy(self, strat):
        symbol = strat['symbol']
        df = get_daily_kline(symbol)
        if df is None or len(df) < 30:
            return
        params = strat.get('params') or {}

        # 目标仓位
        if strat['type'] == 'kronos_forecast':
            target = self._kronos_target(strat, df)
            if target is None:
                return
        else:
            sig = st.compute_signals(df, strat['type'], params)
            if sig is None or len(sig) == 0:
                return
            target = int(sig.iloc[-1])

        # 当前持仓
        aid = db.ensure_account()
        pos_rows = [p for p in db.get_positions(aid)
                    if p['symbol'] == symbol and p['strategy_id'] == strat['id']]
        cur_vol = pos_rows[0]['volume'] if pos_rows else 0

        if target == 1 and cur_vol == 0:
            quote = get_realtime_quote(symbol)
            if not quote:
                return
            cash = self.broker.get_cash()
            vol = int(cash * strat.get('position_ratio', 0.95) // (quote['price'] * 100)) * 100
            if vol > 0:
                self.broker.buy(symbol, vol, quote['price'], f"{strat['type']} 信号", strategy=strat)
        elif target == 0 and cur_vol > 0:
            quote = get_realtime_quote(symbol)
            if quote:
                self.broker.sell(symbol, cur_vol, quote['price'], f"{strat['type']} 平仓", strategy=strat)

    def _kronos_target(self, strat, df):
        """kronos 策略: 每天最多预测一次(结果缓存), 避免高频推理。"""
        today = datetime.date.today().isoformat()
        cached = KRONOS_PRED_CACHE.get(strat['id'])
        params = strat.get('params') or {}
        horizon = int(params.get('horizon', 20))
        threshold = float(params.get('threshold', 0.02))
        lookback = int(params.get('lookback', 256))

        if cached is None or cached[0] != today:
            if self.predict_fn is None:
                return None
            window = df.iloc[-lookback:]
            try:
                pred = self.predict_fn(window, horizon)
                KRONOS_PRED_CACHE[strat['id']] = (today, list(pred))
            except Exception:
                return None
        pred = KRONOS_PRED_CACHE[strat['id']][1]
        return st.kronos_signal_from_pred(df['close'].iloc[-1], pred, threshold)


def start_engine(initial_cash=None, predict_fn=None):
    return LiveEngine.instance().start(initial_cash=initial_cash, predict_fn=predict_fn)


def stop_engine():
    return LiveEngine.instance().stop()


def engine_status():
    return LiveEngine.instance().status()
