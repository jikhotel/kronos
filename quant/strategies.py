"""内置策略库。

信号语义: signals.iloc[i] 表示第 i 根 K 线收盘后应持有的目标仓位(1=满仓, 0=空仓),
回测/实盘在下一根 K 线开盘执行, 严格避免未来函数。
"""

import pandas as pd
import numpy as np

# 策略类型注册表: label + 交易逻辑描述 + 默认参数 + 参数说明
STRATEGY_TYPES = {
    'ma_cross': {
        'label': '双均线交叉',
        'desc': '趋势跟随。快均线(fast, 默认5日)上穿慢均线(slow, 默认20日)形成金叉 → 买入持有；'
                '快线下穿慢线形成死叉 → 全部卖出空仓。适合单边趋势行情，震荡市中易频繁止损。',
        'params': {'fast': 5, 'slow': 20},
        'param_doc': {'fast': '快线周期', 'slow': '慢线周期(须大于快线)'}
    },
    'macd': {
        'label': 'MACD 金叉死叉',
        'desc': '动量趋势。DIF(快慢EMA之差) 上穿 DEA(信号线) 为金叉 → 买入；'
                'DIF 下穿 DEA 为死叉 → 卖出。默认 12/26/9 参数组合，比双均线更平滑、信号更少但更稳。',
        'params': {'fast': 12, 'slow': 26, 'signal': 9},
        'param_doc': {'fast': 'EMA 快线', 'slow': 'EMA 慢线', 'signal': 'DEA 平滑周期'}
    },
    'rsi': {
        'label': 'RSI 超买超卖',
        'desc': '均值回归。RSI 跌破超卖线(oversold, 默认30)视为超跌 → 买入；'
                'RSI 升破超买线(overbought, 默认70)视为过热 → 卖出。适合震荡市，强趋势中会逆势抄底需谨慎。',
        'params': {'period': 14, 'oversold': 30, 'overbought': 70},
        'param_doc': {'period': 'RSI 周期', 'oversold': '超卖阈值(买入)', 'overbought': '超买阈值(卖出)'}
    },
    'bollinger': {
        'label': '布林带突破',
        'desc': '波动率突破。收盘价向上突破布林带上轨(均线+2倍标准差) → 认为突破启动 → 买入；'
                '收盘价跌破下轨 → 跳出下跌通道 → 卖出。适合捕捉波动放大后的单边行情。',
        'params': {'period': 20, 'std': 2.0},
        'param_doc': {'period': '均线周期', 'std': '带宽(标准差倍数)'}
    },
    'kronos_forecast': {
        'label': 'Kronos 预测驱动',
        'desc': 'AI 长期展望。每隔 rebalance_every(默认120根≈半年) 用 Kronos K线基础模型基于最近 lookback(默认256) 根历史'
                '预测未来 horizon(默认120根≈半年) 走势：预测期末涨幅 > threshold(默认+5%) → 买入持有；'
                '预测跌幅 < -threshold → 清仓空仓；介于两者之间维持原仓位。需要在预测页先加载模型。',
        'params': {'horizon': 20, 'threshold': 0.02, 'rebalance_every': 20, 'lookback': 256},
        'param_doc': {'horizon': '预测根数(半年≈120)', 'threshold': '涨跌幅触发阈值',
                      'rebalance_every': '每隔多少根重新预测', 'lookback': '模型输入长度'}
    },
}

DEFAULT_TYPE = 'ma_cross'


def _state_signal(entries, exits):
    """用 (入场条件, 出场条件) 生成保持型信号: 入场后置 1 直到出场。"""
    entries = entries.fillna(False).astype(bool)
    exits = exits.fillna(False).astype(bool)
    sig = pd.Series(0, index=entries.index, dtype=int)
    holding = 0
    for i in range(len(sig)):
        if exits.iloc[i]:
            holding = 0
        if entries.iloc[i]:
            holding = 1
        sig.iloc[i] = holding
    return sig


def _cross_up(a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def add_indicators(df, stype, params):
    """在 df 副本上追加策略所需指标列。"""
    out = df.copy()
    close = out['close']
    if stype == 'ma_cross':
        fast, slow = int(params.get('fast', 5)), int(params.get('slow', 20))
        out['fast_ma'] = close.rolling(fast).mean()
        out['slow_ma'] = close.rolling(slow).mean()
    elif stype == 'macd':
        f, s, g = int(params.get('fast', 12)), int(params.get('slow', 26)), int(params.get('signal', 9))
        ema_f = close.ewm(span=f, adjust=False).mean()
        ema_s = close.ewm(span=s, adjust=False).mean()
        out['dif'] = ema_f - ema_s
        out['dea'] = out['dif'].ewm(span=g, adjust=False).mean()
    elif stype == 'rsi':
        period = int(params.get('period', 14))
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        out['rsi'] = (100 - 100 / (1 + rs)).fillna(50)
    elif stype == 'bollinger':
        period, k = int(params.get('period', 20)), float(params.get('std', 2.0))
        mid = close.rolling(period).mean()
        std = close.rolling(period).std()
        out['boll_mid'] = mid
        out['boll_up'] = mid + k * std
        out['boll_low'] = mid - k * std
    return out


def compute_signals(df, stype, params):
    """
    计算目标仓位序列(0/1)。kronos_forecast 不在此计算(需模型, 由回测/实盘引擎注入)。
    """
    params = params or {}
    if stype == 'kronos_forecast':
        return None
    ind = add_indicators(df, stype, params)
    if stype == 'ma_cross':
        entries = _cross_up(ind['fast_ma'], ind['slow_ma'])
        exits = _cross_down(ind['fast_ma'], ind['slow_ma'])
    elif stype == 'macd':
        entries = _cross_up(ind['dif'], ind['dea'])
        exits = _cross_down(ind['dif'], ind['dea'])
    elif stype == 'rsi':
        lo, hi = float(params.get('oversold', 30)), float(params.get('overbought', 70))
        entries = ind['rsi'] < lo
        exits = ind['rsi'] > hi
    elif stype == 'bollinger':
        entries = _cross_up(ind['close'], ind['boll_up'])
        exits = _cross_down(ind['close'], ind['boll_low'])
    else:
        raise ValueError(f"未知策略类型: {stype}")
    return _state_signal(entries, exits)


def kronos_signal_from_pred(current_price, pred_prices, threshold):
    """根据 Kronos 预测序列生成目标仓位: 预测期末涨幅超阈值→1, 低于负阈值→0, 否则 None(维持)。"""
    if pred_prices is None or len(pred_prices) == 0 or not current_price:
        return None
    target = float(pred_prices[-1])
    chg = target / float(current_price) - 1
    if chg > threshold:
        return 1
    if chg < -threshold:
        return 0
    return None
