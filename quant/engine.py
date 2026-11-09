"""回测引擎: 信号 -> 逐根撮合 -> 净值曲线与绩效指标。

撮合规则(尽量贴近 A 股日线实盘):
- T 根收盘产生信号, T+1 根以开盘价 ± 滑点成交
- 佣金双边收取, 单笔最低 5 元; 买入整百股; 只做多
- 买入动用可用资金 * position_ratio
"""

import json
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.utils

from . import strategies as st

TRADING_DAYS = 252
MIN_COMMISSION = 5.0


def _simulate(df, signals, initial_capital, commission_rate, slippage, position_ratio):
    """逐根撮合。返回 (trades, equity 序列)。"""
    n = len(df)
    cash = float(initial_capital)
    volume = 0
    avg_cost = 0.0
    trades = []
    equity = []
    entry_idx = None

    opens = df['open'].values
    closes = df['close'].values

    # 第 i 根收盘后目标仓位 target[i]; 在第 i+1 根开盘执行
    for i in range(n):
        # ---- 开盘: 先执行上一根产生的信号 ----
        if i > 0:
            target = int(signals.iloc[i - 1])
            if target == 1 and volume == 0 and cash > MIN_COMMISSION * 10:
                price = opens[i] * (1 + slippage)
                budget = cash * position_ratio
                lots = int(budget // (price * 100))   # 整百股
                vol = lots * 100
                if vol > 0:
                    amount = vol * price
                    fee = max(amount * commission_rate, MIN_COMMISSION)
                    cash -= (amount + fee)
                    volume = vol
                    avg_cost = (amount + fee) / vol
                    entry_idx = i
                    trades.append({
                        'idx': i, 'date': str(df['timestamps'].iloc[i]),
                        'side': 'BUY', 'price': round(price, 3), 'volume': vol,
                        'amount': round(amount, 2), 'fee': round(fee, 2),
                        'reason': 'signal'
                    })
            elif target == 0 and volume > 0:
                price = opens[i] * (1 - slippage)
                amount = volume * price
                fee = max(amount * commission_rate, MIN_COMMISSION)
                cash += (amount - fee)
                # 盈亏 = 卖出净额 - 买入总成本(avg_cost 已含买入手续费)
                trades.append({
                    'idx': i, 'date': str(df['timestamps'].iloc[i]),
                    'side': 'SELL', 'price': round(price, 3), 'volume': volume,
                    'amount': round(amount, 2), 'fee': round(fee, 2),
                    'pnl': round((price - avg_cost) * volume - fee, 2),
                    'ret_pct': round(((price / avg_cost) - 1) * 100, 2),
                    'hold_bars': i - (entry_idx if entry_idx is not None else i),
                    'reason': 'signal'
                })
                volume = 0
                avg_cost = 0.0
                entry_idx = None

        # ---- 收盘: 记净值 ----
        equity.append(cash + volume * closes[i])

    # 期末仍持仓 -> 以最后收盘价虚拟平仓(仅计入指标, 不追加交易记录)
    equity = pd.Series(equity, index=df.index)
    return trades, equity


def _metrics(equity, initial_capital, n):
    total = equity.iloc[-1] / initial_capital - 1
    annual = (1 + total) ** (TRADING_DAYS / max(n, 1)) - 1 if n > 0 else 0
    roll_max = equity.cummax()
    dd = (equity / roll_max - 1)
    max_dd = float(dd.min()) if len(dd) else 0.0
    daily = equity.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)) if len(daily) > 2 and daily.std() > 0 else 0.0
    return {
        'total_return_pct': round(total * 100, 2),
        'annual_return_pct': round(annual * 100, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'sharpe': round(sharpe, 2),
    }


def _trade_metrics(trades):
    sells = [t for t in trades if t['side'] == 'SELL']
    wins = [t for t in sells if t.get('pnl', 0) > 0]
    losses = [t for t in sells if t.get('pnl', 0) <= 0]
    win_rate = len(wins) / len(sells) * 100 if sells else 0
    gross_win = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0)
    return {
        'n_trades': len(sells),
        'n_buys': len([t for t in trades if t['side'] == 'BUY']),
        'win_rate_pct': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
    }


def run_backtest(df, stype, params, initial_capital=100000, commission_rate=0.0003,
                 slippage=0.001, position_ratio=0.95, predict_fn=None):
    """
    运行回测。kronos_forecast 需要 predict_fn(df_window, horizon) -> 预测收盘价序列。
    返回 dict: trades / equity / metrics / signals
    """
    params = params or {}
    if stype == 'kronos_forecast':
        if predict_fn is None:
            raise ValueError("Kronos 预测策略需要已加载的模型, 请先在首页 Load Model")
        signals = _rolling_kronos_signals(df, params, predict_fn)
    else:
        signals = st.compute_signals(df, stype, params)
        if signals is None:
            raise ValueError("不支持的策略类型")

    signals = signals.shift(0)  # 语义已定义: i 收盘信号, i+1 开盘执行(撮合内部处理)
    trades, equity = _simulate(df, signals, initial_capital, commission_rate, slippage, position_ratio)

    # 基准: 买入持有(同期)
    bh = (df['close'].iloc[-1] / df['open'].iloc[0] - 1) * 100 if len(df) > 1 else 0

    metrics = _metrics(equity, initial_capital, len(df))
    metrics.update(_trade_metrics(trades))
    metrics['buy_hold_pct'] = round(float(bh), 2)
    metrics['final_equity'] = round(float(equity.iloc[-1]), 2)
    metrics['initial_capital'] = round(float(initial_capital), 2)
    return {'trades': trades, 'equity': equity, 'metrics': metrics, 'signals': signals}


def _rolling_kronos_signals(df, params, predict_fn):
    """滚动调用 Kronos 预测生成信号: 每 rebalance_every 根用 lookback 历史预测 horizon 根。"""
    horizon = int(params.get('horizon', 20))
    threshold = float(params.get('threshold', 0.02))
    every = max(1, int(params.get('rebalance_every', horizon)))
    lookback = int(params.get('lookback', 256))

    n = len(df)
    sig = pd.Series(0, index=df.index, dtype=int)
    holding = 0
    next_check = lookback
    for i in range(n):
        if i >= next_check:
            window = df.iloc[max(0, i - lookback + 1): i + 1]
            if len(window) >= 30:  # 至少 30 根才预测
                try:
                    pred = predict_fn(window, horizon)
                    target = st.kronos_signal_from_pred(window['close'].iloc[-1], pred, threshold)
                    if target is not None:
                        holding = target
                except Exception:
                    pass  # 预测失败维持原仓位
                next_check = i + every
        sig.iloc[i] = holding
    return sig


# ---------------- Plotly 可视化 ----------------

UP_COLOR = '#EF4136'
DOWN_COLOR = '#00A152'
EQUITY_COLOR = '#2D7DD2'
BUY_MARK = 'triangle-up'
SELL_MARK = 'triangle-down'


def chart_json(fig):
    """fig -> 纯数组 JSON。PlotlyJSONEncoder 会把 numpy/pandas 数组编码为
    {dtype, bdata} 二进制格式, 前端 plotly-2.27 不解码导致图表空白;
    深度转普通列表后再序列化, 保证任何浏览器可渲染。"""
    import numpy as np
    obj = fig.to_plotly_json()

    def _decode_binary(d):
        """plotly>=6 的 to_plotly_json() 直接把数组编码为 {dtype, bdata}(base64, 小端);
        浏览器端 plotly-2.27 不识别, 必须解码回纯数组。"""
        import base64 as _b64
        try:
            buf = _b64.b64decode(d['bdata'])
            return np.frombuffer(buf, dtype=np.dtype('<' + str(d['dtype']))).tolist()
        except Exception:
            return None

    def conv(v):
        if isinstance(v, dict):
            if 'bdata' in v and 'dtype' in v and isinstance(v['bdata'], (str, bytes)):
                decoded = _decode_binary(v)
                if decoded is not None:
                    return decoded
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [conv(x) for x in v]
        if isinstance(v, np.ndarray) or (hasattr(v, 'tolist') and not isinstance(v, str)):
            return v.tolist()
        if isinstance(v, (np.floating, np.integer, np.bool_)):
            return v.item()
        if isinstance(v, float) and v != v:  # NaN -> null(合法 JSON)
            return None
        if hasattr(v, 'isoformat'):
            return str(v)
        return v

    return json.dumps(conv(obj), ensure_ascii=False)


def plot_backtest(df, equity, trades):
    """回测结果图: 上图 K线+买卖点, 下图策略净值 vs 买入持有基准。数值索引轴(缩放顺滑)。"""
    from plotly.subplots import make_subplots
    n = len(df)
    x = list(range(n))
    labels = [str(t)[:10] for t in df['timestamps']]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.62, 0.38], vertical_spacing=0.05)

    fig.add_trace(go.Candlestick(
        x=x, open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        name='K线',
        increasing_line_color=UP_COLOR, increasing_fillcolor=UP_COLOR,
        decreasing_line_color=DOWN_COLOR, decreasing_fillcolor=DOWN_COLOR
    ), row=1, col=1)

    buys = [t for t in trades if t['side'] == 'BUY']
    sells = [t for t in trades if t['side'] == 'SELL']
    if buys:
        fig.add_trace(go.Scatter(
            x=[t['idx'] for t in buys], y=[t['price'] * 0.985 for t in buys],
            mode='markers', name='买入',
            marker=dict(symbol=BUY_MARK, size=12, color=UP_COLOR),
            hovertext=[f"{t['date']}<br>买入 {t['volume']}股 @ {t['price']}" for t in buys],
            hoverinfo='text'
        ), row=1, col=1)
    if sells:
        fig.add_trace(go.Scatter(
            x=[t['idx'] for t in sells], y=[t['price'] * 1.015 for t in sells],
            mode='markers', name='卖出',
            marker=dict(symbol=SELL_MARK, size=12, color=DOWN_COLOR),
            hovertext=[f"{t['date']}<br>卖出 {t['volume']}股 @ {t['price']}<br>盈亏 {t.get('pnl', 0)}" for t in sells],
            hoverinfo='text'
        ), row=1, col=1)

    # 下图: 策略净值 vs 买入持有(都以首根开盘价为基准, 直接可比)
    eq_norm = (equity / equity.iloc[0] * df['open'].iloc[0]).values
    bh = df['close'] / df['open'].iloc[0] * df['open'].iloc[0]
    fig.add_trace(go.Scatter(
        x=x, y=eq_norm, name='策略净值', mode='lines',
        line=dict(color=EQUITY_COLOR, width=1.8)
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=x, y=bh, name='买入持有', mode='lines',
        line=dict(color='#F5A623', width=1.2, dash='dot')
    ), row=2, col=1)

    tick_step = max(1, n // 12)
    tick_vals = list(range(0, n, tick_step))
    if (n - 1) not in tick_vals and n > 0:
        tick_vals.append(n - 1)
    tick_text = [labels[i] for i in tick_vals]

    fig.update_layout(
        title='回测结果 — K线 · 买卖点 · 净值 vs 基准',
        template='plotly_white', height=640,
        dragmode='pan', uirevision='quant-backtest',
        margin=dict(l=55, r=65, t=60, b=45),
        legend=dict(orientation='h', y=1.08, x=0)
    )
    fig.update_xaxes(
        rangeslider_visible=False, tickmode='array',
        tickvals=tick_vals, ticktext=tick_text, tickangle=-45, title_text='交易日'
    )
    fig.update_yaxes(side='right')
    return chart_json(fig)
