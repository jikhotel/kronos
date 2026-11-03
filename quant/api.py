"""量化交易 Flask 蓝图: 策略管理 / 回测 / 实盘。"""

import os
import sys
import pandas as pd
from flask import Blueprint, jsonify, request

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from quant import db, engine, live
from quant import strategies as st

quant_bp = Blueprint('quant', __name__, url_prefix='/api/quant')

# 模型预测函数 provider(由 webui/app.py 注入, 避免循环导入)
_PREDICT_PROVIDER = None


def set_predict_provider(fn):
    global _PREDICT_PROVIDER
    _PREDICT_PROVIDER = fn


def _get_predict_fn():
    try:
        return _PREDICT_PROVIDER() if _PREDICT_PROVIDER else None
    except Exception:
        return None


def _load_csv(path):
    df = pd.read_csv(path)
    df['timestamps'] = pd.to_datetime(df['timestamps'])
    for col in ('open', 'high', 'low', 'close'):
        if col not in df.columns:
            raise ValueError(f"数据文件缺少 {col} 列")
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if 'volume' in df.columns:
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
    df = df.dropna(subset=['open', 'high', 'low', 'close'])
    df = df.sort_values('timestamps').reset_index(drop=True)
    return df


def _data_dir():
    return os.path.join(_PROJECT_ROOT, 'data')


# ---------------- 策略类型 ----------------

@quant_bp.route('/strategy-types')
def strategy_types():
    return jsonify({'types': st.STRATEGY_TYPES, 'default': st.DEFAULT_TYPE})


# ---------------- 策略 CRUD ----------------

def _validate_symbol(symbol):
    sys.path.insert(0, _PROJECT_ROOT)
    try:
        from fetch_stock_kline import resolve_symbol
        return resolve_symbol(symbol)
    except Exception:
        return None


@quant_bp.route('/strategies', methods=['GET'])
def get_strategies():
    return jsonify({'strategies': db.list_strategies()})


@quant_bp.route('/strategies', methods=['POST'])
def create_strategy():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    stype = data.get('type') or st.DEFAULT_TYPE
    symbol = (data.get('symbol') or '').strip()
    if not name:
        return jsonify({'error': '策略名称不能为空'}), 400
    if stype not in st.STRATEGY_TYPES:
        return jsonify({'error': f'未知策略类型 {stype}'}), 400
    resolved = _validate_symbol(symbol)
    if not resolved:
        return jsonify({'error': f'无效的股票代码: {symbol}'}), 400
    try:
        params = data.get('params') or {}
        if isinstance(params, str):
            import json
            params = json.loads(params)
    except Exception:
        return jsonify({'error': '参数 params 必须是合法 JSON 对象'}), 400

    row = db.create_strategy({
        'name': name, 'type': stype, 'params': params, 'symbol': resolved,
        'freq': data.get('freq') or 'day',
        'initial_capital': data.get('initial_capital', 100000),
        'commission_rate': data.get('commission_rate', 0.0003),
        'slippage': data.get('slippage', 0.001),
        'position_ratio': data.get('position_ratio', 0.95),
        'status': data.get('status') or 'stopped'
    })
    return jsonify({'success': True, 'strategy': row})


@quant_bp.route('/strategies/<int:sid>', methods=['PUT'])
def update_strategy(sid):
    existing = db.get_strategy(sid)
    if not existing:
        return jsonify({'error': '策略不存在'}), 404
    data = request.get_json() or {}
    if 'params' in data and isinstance(data['params'], str):
        try:
            data['params'] = __import__('json').loads(data['params'])
        except Exception:
            return jsonify({'error': '参数 params 必须是合法 JSON 对象'}), 400
    if 'symbol' in data and data['symbol']:
        resolved = _validate_symbol(data['symbol'])
        if not resolved:
            return jsonify({'error': f"无效的股票代码: {data['symbol']}"}), 400
        data['symbol'] = resolved
    row = db.update_strategy(sid, data)
    return jsonify({'success': True, 'strategy': row})


@quant_bp.route('/strategies/<int:sid>', methods=['DELETE'])
def delete_strategy(sid):
    if not db.get_strategy(sid):
        return jsonify({'error': '策略不存在'}), 404
    db.delete_strategy(sid)
    return jsonify({'success': True})


# ---------------- 数据文件 ----------------

@quant_bp.route('/data-files')
def quant_data_files():
    out = []
    d = _data_dir()
    if os.path.exists(d):
        for f in sorted(os.listdir(d)):
            if f.endswith('.csv'):
                out.append({'name': f, 'path': os.path.join(d, f),
                            'size_kb': round(os.path.getsize(os.path.join(d, f)) / 1024, 1)})
    return jsonify({'files': out})


# ---------------- 回测 ----------------

@quant_bp.route('/backtest', methods=['POST'])
def run_backtest():
    data = request.get_json() or {}
    sid = data.get('strategy_id')
    strat = db.get_strategy(sid) if sid else None
    if not strat:
        return jsonify({'error': '策略不存在, 请先创建策略'}), 400

    # 数据: 显式文件 or data 目录中该 symbol 的最新 csv
    file_path = data.get('file_path')
    if not file_path:
        candidates = [f for f in os.listdir(_data_dir())
                      if f.endswith('.csv') and strat['symbol'] in f]
        if not candidates:
            return jsonify({'error': f"data 目录中没有 {strat['symbol']} 的数据文件, "
                                     f"请先在首页搜索导入或指定 file_path"}), 400
        file_path = os.path.join(_data_dir(), sorted(candidates)[-1])

    if not os.path.exists(file_path):
        return jsonify({'error': f'数据文件不存在: {file_path}'}), 400

    try:
        df = _load_csv(file_path)
    except Exception as e:
        return jsonify({'error': f'数据加载失败: {e}'}), 400

    start_date, end_date = data.get('start_date'), data.get('end_date')
    if start_date:
        df = df[df['timestamps'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['timestamps'] <= pd.to_datetime(end_date)]
    df = df.reset_index(drop=True)
    if len(df) < 60:
        return jsonify({'error': f'区间数据不足(仅 {len(df)} 根, 至少 60 根)'}), 400

    predict_fn = _get_predict_fn() if strat['type'] == 'kronos_forecast' else None
    try:
        result = engine.run_backtest(
            df, strat['type'], strat['params'],
            initial_capital=strat['initial_capital'],
            commission_rate=strat['commission_rate'],
            slippage=strat['slippage'],
            position_ratio=strat['position_ratio'],
            predict_fn=predict_fn
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'回测失败: {e}'}), 500

    chart = engine.plot_backtest(df, result['equity'], result['trades'])
    rid = db.save_backtest({
        'strategy_id': strat['id'], 'strategy_name': strat['name'],
        'type': strat['type'], 'params': strat['params'], 'symbol': strat['symbol'],
        'file_path': file_path,
        'start_date': str(df['timestamps'].iloc[0])[:10],
        'end_date': str(df['timestamps'].iloc[-1])[:10],
        'metrics': result['metrics'], 'trades': result['trades'], 'chart': chart
    })
    return jsonify({
        'success': True, 'backtest_id': rid,
        'metrics': result['metrics'], 'trades': result['trades'],
        'chart': chart, 'n_bars': len(df),
        'file': os.path.basename(file_path)
    })


@quant_bp.route('/backtests')
def backtest_history():
    return jsonify({'backtests': db.list_backtests()})


@quant_bp.route('/backtests/<int:rid>')
def backtest_detail(rid):
    row = db.get_backtest(rid)
    if not row:
        return jsonify({'error': '回测记录不存在'}), 404
    return jsonify(row)


# ---------------- 实盘(模拟盘) ----------------

@quant_bp.route('/live/start', methods=['POST'])
def live_start():
    data = request.get_json() or {}
    initial_cash = data.get('initial_cash')
    reset = bool(data.get('reset', False))
    if initial_cash is not None:
        initial_cash = float(initial_cash)
    ok, msg = live.start_engine(
        initial_cash=initial_cash if reset else None,
        predict_fn=_get_predict_fn()
    )
    return jsonify({'success': ok, 'message': msg})


@quant_bp.route('/live/stop', methods=['POST'])
def live_stop():
    ok, msg = live.stop_engine()
    return jsonify({'success': ok, 'message': msg})


@quant_bp.route('/live/status')
def live_status():
    return jsonify(live.engine_status())


@quant_bp.route('/live/reset', methods=['POST'])
def live_reset():
    data = request.get_json() or {}
    cash = float(data.get('initial_cash', 1000000))
    db.reset_account(cash)
    return jsonify({'success': True, 'message': f'账户已重置, 初始资金 {cash:,.0f}'})
