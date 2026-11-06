import os
import pandas as pd
import numpy as np
import json
import plotly.graph_objects as go
import plotly.utils
from plotly.subplots import make_subplots
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import sys
import re
import warnings
import datetime
import requests
warnings.filterwarnings('ignore')

# Add project root directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from model import Kronos, KronosTokenizer, KronosPredictor
    MODEL_AVAILABLE = True
except ImportError:
    MODEL_AVAILABLE = False
    print("Warning: Kronos model cannot be imported, will use simulated data for demonstration")

app = Flask(__name__)
CORS(app)

# ============================================================
# 登录 / 用户管理 / 操作审计 (webui/auth.py)
# ============================================================
try:
    import auth as kronos_auth

    # 持久化 secret key(重启不丢会话)
    _sk_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    if os.path.exists(_sk_file):
        app.secret_key = open(_sk_file).read().strip()
    else:
        import secrets as _secrets
        app.secret_key = _secrets.token_hex(32)
        with open(_sk_file, 'w') as _f:
            _f.write(app.secret_key)
    app.config['SESSION_COOKIE_HTTPONLY'] = True

    kronos_auth.init_db()
    app.before_request(kronos_auth.before_request_hook)
    app.after_request(kronos_auth.after_request_hook)

    @app.context_processor
    def _inject_user():
        return {'u': kronos_auth.current_user()}

    @app.route('/login', methods=['GET', 'POST'])
    def login_page():
        if request.method == 'POST':
            user = kronos_auth.verify_login(
                (request.form.get('username') or '').strip(),
                request.form.get('password') or '',
                request.remote_addr or '')
            if user:
                session['user'] = user
                session.permanent = True
                nxt = request.form.get('next') or '/'
                if not nxt.startswith('/'):
                    nxt = '/'
                return redirect(nxt)
            return render_template('login.html', error='用户名或密码错误',
                                   next=request.form.get('next', ''))
        return render_template('login.html', error=None, next=request.args.get('next', ''))

    @app.route('/logout')
    def logout():
        u = kronos_auth.current_user()
        if u:
            kronos_auth.log_op(u['username'], '登出', '主动退出', 200)
        session.clear()
        return redirect('/login')

    @app.route('/account')
    def account_page():
        return render_template('account.html')

    @app.route('/api/change-password', methods=['POST'])
    def change_password():
        u = kronos_auth.current_user()
        if not u:
            return jsonify({'error': '未登录'}), 401
        data = request.get_json() or {}
        ok, msg = kronos_auth.change_password(
            u['username'], data.get('old_password'), data.get('new_password'))
        return jsonify({('message' if ok else 'error'): msg, 'success': ok}), (200 if ok else 400)

    @app.route('/admin')
    @kronos_auth.admin_required
    def admin_page():
        return render_template('admin.html')

    @app.route('/api/admin/users')
    @kronos_auth.admin_required
    def admin_users():
        return jsonify({'users': kronos_auth.list_users()})

    @app.route('/api/admin/users', methods=['POST'])
    @kronos_auth.admin_required
    def admin_create_user():
        data = request.get_json() or {}
        ok, msg = kronos_auth.create_user(
            (data.get('username') or '').strip(), data.get('password') or '',
            role=data.get('role', 'user'), remark=data.get('remark', ''),
            by=kronos_auth.current_user()['username'])
        return jsonify({('message' if ok else 'error'): msg, 'success': ok}), (200 if ok else 400)

    @app.route('/api/admin/users/<int:uid>', methods=['POST'])
    @kronos_auth.admin_required
    def admin_update_user(uid):
        data = request.get_json() or {}
        ok, msg = kronos_auth.update_user(uid, data, by=kronos_auth.current_user()['username'])
        return jsonify({('message' if ok else 'error'): msg, 'success': ok}), (200 if ok else 400)

    @app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
    @kronos_auth.admin_required
    def admin_delete_user(uid):
        ok, msg = kronos_auth.delete_user(uid, by=kronos_auth.current_user()['username'])
        return jsonify({('message' if ok else 'error'): msg, 'success': ok}), (200 if ok else 400)

    @app.route('/api/admin/logs')
    @kronos_auth.admin_required
    def admin_logs():
        limit = int(request.args.get('limit', 200) or 200)
        return jsonify({'logs': kronos_auth.list_logs(min(limit, 1000))})

    @app.route('/healthz')
    def healthz():
        return jsonify({'ok': True})

    AUTH_AVAILABLE = True
except Exception as _auth_err:
    AUTH_AVAILABLE = False
    print(f"Warning: auth subsystem unavailable: {_auth_err}")


# Global variables to store models
tokenizer = None
model = None
predictor = None

# 最近一次预测的图表上下文(用于不重跑模型、仅切换下方参考指标时重绘)
LAST_CHART_CTX = {}

# Available model configurations
AVAILABLE_MODELS = {
    'kronos-mini': {
        'name': 'Kronos-mini',
        'model_id': 'NeoQuasar/Kronos-mini',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-2k',
        'context_length': 2048,
        'params': '4.1M',
        'description': 'Lightweight model, suitable for fast prediction'
    },
    'kronos-small': {
        'name': 'Kronos-small',
        'model_id': 'NeoQuasar/Kronos-small',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-base',
        'context_length': 512,
        'params': '24.7M',
        'description': 'Small model, balanced performance and speed'
    },
    'kronos-base': {
        'name': 'Kronos-base',
        'model_id': 'NeoQuasar/Kronos-base',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-base',
        'context_length': 512,
        'params': '102.3M',
        'description': 'Base model, provides better prediction quality'
    }
}

def load_data_files():
    """Scan data directory and return available data files"""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    data_files = []
    
    if os.path.exists(data_dir):
        for file in os.listdir(data_dir):
            if file.endswith(('.csv', '.feather')):
                file_path = os.path.join(data_dir, file)
                file_size = os.path.getsize(file_path)
                data_files.append({
                    'name': file,
                    'path': file_path,
                    'size': f"{file_size / 1024:.1f} KB" if file_size < 1024*1024 else f"{file_size / (1024*1024):.1f} MB"
                })
    
    return data_files

def load_data_file(file_path):
    """Load data file"""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith('.feather'):
            df = pd.read_feather(file_path)
        else:
            return None, "Unsupported file format"
        
        # Check required columns
        required_cols = ['open', 'high', 'low', 'close']
        if not all(col in df.columns for col in required_cols):
            return None, f"Missing required columns: {required_cols}"
        
        # Process timestamp column
        if 'timestamps' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamps'])
        elif 'timestamp' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamp'])
        elif 'date' in df.columns:
            # If column name is 'date', rename it to 'timestamps'
            df['timestamps'] = pd.to_datetime(df['date'])
        else:
            # If no timestamp column exists, create one
            df['timestamps'] = pd.date_range(start='2024-01-01', periods=len(df), freq='1H')
        
        # Ensure numeric columns are numeric type
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # Process volume column (optional)
        if 'volume' in df.columns:
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        
        # Process amount column (optional, but not used for prediction)
        if 'amount' in df.columns:
            df['amount'] = pd.to_numeric(df['amount'], errors='coerce')

        # Process turnover (换手率) column (optional)
        if 'turnover' in df.columns:
            df['turnover'] = pd.to_numeric(df['turnover'], errors='coerce')
        
        # Remove rows containing NaN values
        df = df.dropna()
        
        return df, None
        
    except Exception as e:
        return None, f"Failed to load file: {str(e)}"


def build_data_info(df):
    """Construct a data-info dict from a loaded dataframe (shared by load-data & fetch-stock)."""
    def detect_timeframe(df):
        if len(df) < 2:
            return "Unknown"
        time_diffs = []
        for i in range(1, min(10, len(df))):
            diff = df['timestamps'].iloc[i] - df['timestamps'].iloc[i-1]
            time_diffs.append(diff)
        if not time_diffs:
            return "Unknown"
        avg_diff = sum(time_diffs, pd.Timedelta(0)) / len(time_diffs)
        if avg_diff < pd.Timedelta(minutes=1):
            return f"{avg_diff.total_seconds():.0f} seconds"
        elif avg_diff < pd.Timedelta(hours=1):
            return f"{avg_diff.total_seconds() / 60:.0f} minutes"
        elif avg_diff < pd.Timedelta(days=1):
            return f"{avg_diff.total_seconds() / 3600:.0f} hours"
        else:
            return f"{avg_diff.days} days"

    return {
        'rows': len(df),
        'columns': list(df.columns),
        'start_date': df['timestamps'].min().isoformat() if 'timestamps' in df.columns else 'N/A',
        'end_date': df['timestamps'].max().isoformat() if 'timestamps' in df.columns else 'N/A',
        'price_range': {
            'min': float(df[['open', 'high', 'low', 'close']].min().min()),
            'max': float(df[['open', 'high', 'low', 'close']].max().max())
        },
        'prediction_columns': ['open', 'high', 'low', 'close'] + (['volume'] if 'volume' in df.columns else []),
        'timeframe': detect_timeframe(df)
    }

def save_prediction_results(file_path, prediction_type, prediction_results, actual_data, input_data, prediction_params):
    """Save prediction results to file"""
    try:
        # Create prediction results directory
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prediction_results')
        os.makedirs(results_dir, exist_ok=True)
        
        # Generate filename
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'prediction_{timestamp}.json'
        filepath = os.path.join(results_dir, filename)
        
        # Prepare data for saving
        save_data = {
            'timestamp': datetime.datetime.now().isoformat(),
            'file_path': file_path,
            'prediction_type': prediction_type,
            'prediction_params': prediction_params,
            'input_data_summary': {
                'rows': len(input_data),
                'columns': list(input_data.columns),
                'price_range': {
                    'open': {'min': float(input_data['open'].min()), 'max': float(input_data['open'].max())},
                    'high': {'min': float(input_data['high'].min()), 'max': float(input_data['high'].max())},
                    'low': {'min': float(input_data['low'].min()), 'max': float(input_data['low'].max())},
                    'close': {'min': float(input_data['close'].min()), 'max': float(input_data['close'].max())}
                },
                'last_values': {
                    'open': float(input_data['open'].iloc[-1]),
                    'high': float(input_data['high'].iloc[-1]),
                    'low': float(input_data['low'].iloc[-1]),
                    'close': float(input_data['close'].iloc[-1])
                }
            },
            'prediction_results': prediction_results,
            'actual_data': actual_data,
            'analysis': {}
        }
        
        # If actual data exists, perform comparison analysis
        if actual_data and len(actual_data) > 0:
            # Calculate continuity analysis
            if len(prediction_results) > 0 and len(actual_data) > 0:
                last_pred = prediction_results[0]  # First prediction point
            first_actual = actual_data[0]      # First actual point
                
            save_data['analysis']['continuity'] = {
                    'last_prediction': {
                        'open': last_pred['open'],
                        'high': last_pred['high'],
                        'low': last_pred['low'],
                        'close': last_pred['close']
                    },
                    'first_actual': {
                        'open': first_actual['open'],
                        'high': first_actual['high'],
                        'low': first_actual['low'],
                        'close': first_actual['close']
                    },
                    'gaps': {
                        'open_gap': abs(last_pred['open'] - first_actual['open']),
                        'high_gap': abs(last_pred['high'] - first_actual['high']),
                        'low_gap': abs(last_pred['low'] - first_actual['low']),
                        'close_gap': abs(last_pred['close'] - first_actual['close'])
                    },
                    'gap_percentages': {
                        'open_gap_pct': (abs(last_pred['open'] - first_actual['open']) / first_actual['open']) * 100,
                        'high_gap_pct': (abs(last_pred['high'] - first_actual['high']) / first_actual['high']) * 100,
                        'low_gap_pct': (abs(last_pred['low'] - first_actual['low']) / first_actual['low']) * 100,
                        'close_gap_pct': (abs(last_pred['close'] - first_actual['close']) / first_actual['close']) * 100
                    }
                }
        
        # Save to file
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        
        print(f"Prediction results saved to: {filepath}")
        return filepath
        
    except Exception as e:
        print(f"Failed to save prediction results: {e}")
        return None

def _next_weekday(d):
    """返回 d 之后的下一个工作日(仅跳过周六周日, 不含法定节假日)"""
    d = d + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + datetime.timedelta(days=1)
    return d


def infer_step(timestamps):
    """取历史相邻时间戳间隔的众数, 作为外推步长"""
    ts = pd.to_datetime(pd.Series(timestamps)).reset_index(drop=True)
    deltas = ts.diff().dropna()
    deltas = deltas[deltas > pd.Timedelta(0)]
    if len(deltas) == 0:
        return pd.Timedelta(days=1)
    return deltas.mode().iloc[0]


def generate_future_timestamps(timestamps, n):
    """
    生成超出数据末尾的 n 个未来时间戳, 使预测真正落在未知区间。

    - 日内数据(步长 < 1 天): 按历史每日交易时段模板外推, 自动跳过非交易日
    - 日线数据(步长 < 4 天): 按步长外推并跳过周末
    - 周线/月线: 直接按步长外推

    注意: 无法感知法定节假日, 春节/国庆等长假会多出少量冗余日期。
    """
    ts = pd.to_datetime(pd.Series(timestamps)).reset_index(drop=True)
    step = infer_step(ts)
    last = ts.iloc[-1]

    if step < pd.Timedelta(days=1):
        last_day = last.date()
        day_slots = [t.time() for t in ts if t.date() == last_day]
        if len(day_slots) < 2:
            seen, day_slots = set(), []
            for t in ts:
                if t.time() not in seen:
                    seen.add(t.time())
                    day_slots.append(t.time())
            day_slots = sorted(day_slots)
        try:
            pos = day_slots.index(last.time())
        except ValueError:
            pos = len(day_slots) - 1
        out, d = [], last_day
        while len(out) < n:
            pos += 1
            if pos >= len(day_slots):
                pos = 0
                d = _next_weekday(d)
            out.append(pd.Timestamp.combine(d, day_slots[pos]))
        return pd.DatetimeIndex(out[:n])

    out, cur = [], last
    skip_weekend = step < pd.Timedelta(days=4)
    while len(out) < n:
        cur = cur + step
        if skip_weekend:
            while cur.weekday() >= 5:
                cur = cur + pd.Timedelta(days=1)
        out.append(cur)
    return pd.DatetimeIndex(out[:n])


# 同花顺风格: 涨=红, 跌=绿 (实心阳/阴线, 实体与影线同色)
UP_COLOR = '#EF4136'      # 涨 / 阳线 (同花顺红)
DOWN_COLOR = '#00A152'    # 跌 / 阴线 (同花顺绿)


def _vol_colors(frame):
    """按 收>=开 为涨(红), 否则跌(绿), 返回每根 K 对应的颜色"""
    return [UP_COLOR if c >= o else DOWN_COLOR
            for o, c in zip(frame['open'], frame['close'])]


def _fmt_label(ts):
    """时间戳转成连续 K 线所需的字符串标签(去除周末/节假日空白)。

    日/周/月线只保留日期; 分钟/小时线含时间。用于 X 轴改为 category 类型,
    使每根 K 线等距排列, 不再因非交易日产生空白断档。"""
    ts = pd.Timestamp(ts)
    if ts == ts.normalize():
        return ts.strftime('%Y-%m-%d')
    return ts.strftime('%Y-%m-%d %H:%M')


def _to_labels(seq):
    """将 x 坐标序列(时间戳或整数索引)统一转为绘图用标签"""
    return [_fmt_label(t) if not isinstance(t, (int, np.integer)) else t for t in seq]


def create_prediction_chart(df, pred_df, lookback, pred_len, actual_df=None,
                            historical_start_idx=0, pred_timestamps=None, mode='forecast',
                            lower_indicator='volume'):
    """Create prediction chart (同花顺风格 K线 + 下方可选参考指标子图)"""
    # 取 lookback 根历史数据; forecast 模式下 historical_start_idx 指向末尾, 即最新的一段
    start = max(0, min(historical_start_idx, max(0, len(df) - 1)))
    historical_df = df.iloc[start:start + lookback]

    # ---- 下方参考指标选择 (成交量 / 成交额 / 换手率) ----
    IND_MAP = {'volume': '成交量', 'amount': '成交额', 'turnover': '换手率'}
    ind_col = lower_indicator if lower_indicator in IND_MAP else 'volume'
    has_lower = ind_col in historical_df.columns and bool(historical_df[ind_col].notna().any())
    if not has_lower:
        # 该列缺失(旧数据/港股估算)时退回成交量
        ind_col = 'volume'
        has_lower = 'volume' in historical_df.columns and bool(historical_df['volume'].notna().any())
    ind_label = IND_MAP.get(ind_col, '成交量')

    if has_lower:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.72, 0.28], vertical_spacing=0.04
        )
    else:
        fig = go.Figure()

    n_hist = len(historical_df)
    n_pred = len(pred_df) if pred_df is not None else 0

    # 用「连续整数交易日索引」作为 X 轴(数值轴)——缩放/平移极顺滑;
    # 日期只作为刻度文本与 hover 提示, 不进入坐标轴, 因此既无周末空白、K 线连续,
    # 又避免了 category 轴缩放时一段段跳变导致的卡顿。
    hist_idx = list(range(n_hist))
    if 'timestamps' in historical_df.columns:
        hist_labels = [_fmt_label(t) for t in historical_df['timestamps']]
    else:
        hist_labels = [str(i) for i in hist_idx]

    # 预测 / 实际段索引接续在历史段之后
    pred_idx = list(range(n_hist, n_hist + n_pred))
    if pred_timestamps is not None:
        pred_timestamps = list(pred_timestamps)[:n_pred] if n_pred else []
        pred_labels = [_fmt_label(t) for t in pred_timestamps] if pred_timestamps else [str(i) for i in pred_idx]
    else:
        pred_labels = [str(i) for i in pred_idx]

    def _ohlc_texts(labels, o, h, l, c):
        # candlestick 不支持 hovertemplate, 改用预格式化 hovertext + hoverinfo='text'
        # 直接在数值索引轴上显示「日期 + 开高低收」, 既保留信息又让缩放顺滑
        return [f'{lab}<br>开:{ov:.2f} 高:{hv:.2f} 低:{lv:.2f} 收:{cv:.2f}'
                for lab, ov, hv, lv, cv in zip(labels, o, h, l, c)]

    # ---- 主图: K 线 (row=1) ----
    fig.add_trace(go.Candlestick(
        x=hist_idx,
        open=historical_df['open'], high=historical_df['high'],
        low=historical_df['low'], close=historical_df['close'],
        hovertext=_ohlc_texts(hist_labels, historical_df['open'], historical_df['high'],
                              historical_df['low'], historical_df['close']),
        hoverinfo='text',
        name='Historical Data',
        increasing_line_color=UP_COLOR, increasing_fillcolor=UP_COLOR,
        decreasing_line_color=DOWN_COLOR, decreasing_fillcolor=DOWN_COLOR
    ), row=1, col=1)

    # 预测数据 (candlestick)
    if pred_df is not None and len(pred_df) > 0:
        pred_label = 'Future Forecast' if mode == 'forecast' else 'Prediction Data'
        fig.add_trace(go.Candlestick(
            x=pred_idx,
            open=pred_df['open'], high=pred_df['high'],
            low=pred_df['low'], close=pred_df['close'],
            hovertext=_ohlc_texts(pred_labels, pred_df['open'], pred_df['high'],
                                  pred_df['low'], pred_df['close']),
            hoverinfo='text',
            name=f'{pred_label} ({len(pred_df)} points)',
            increasing_line_color=UP_COLOR, increasing_fillcolor=UP_COLOR,
        decreasing_line_color=DOWN_COLOR, decreasing_fillcolor=DOWN_COLOR
        ), row=1, col=1)

        # 历史与未来之间画一条分界线(最后一根历史 K 线右侧)
        if n_hist > 0:
            fig.add_vline(
                x=n_hist - 0.5,
                line_width=1, line_dash='dash', line_color='#9E9E9E',
                row=1, col=1
            )

    # 实际数据 (回测比对) — 与预测段同位置对齐
    if actual_df is not None and len(actual_df) > 0:
        actual_idx = pred_idx[:len(actual_df)]
        actual_labels = pred_labels[:len(actual_df)]
        fig.add_trace(go.Candlestick(
            x=actual_idx,
            open=actual_df['open'], high=actual_df['high'],
            low=actual_df['low'], close=actual_df['close'],
            hovertext=_ohlc_texts(actual_labels, actual_df['open'], actual_df['high'],
                                  actual_df['low'], actual_df['close']),
            hoverinfo='text',
            name=f'Actual Data ({len(actual_df)} points)',
            increasing_line_color=UP_COLOR, increasing_fillcolor=UP_COLOR,
        decreasing_line_color=DOWN_COLOR, decreasing_fillcolor=DOWN_COLOR
        ), row=1, col=1)

    # ---- 副图: 可选参考指标 (row=2) ----
    if has_lower:
        if ind_col == 'amount':
            hovertpl = '%{customdata}<br>成交额: %{y:,.0f}<extra></extra>'
        elif ind_col == 'turnover':
            hovertpl = '%{customdata}<br>换手率: %{y:.2f}%<extra></extra>'
        else:
            hovertpl = '%{customdata}<br>成交量: %{y:,.0f}<extra></extra>'

        fig.add_trace(go.Bar(
            x=hist_idx,
            y=historical_df[ind_col],
            customdata=hist_labels,
            marker_color=_vol_colors(historical_df),
            name=ind_label, showlegend=False,
            hovertemplate=hovertpl
        ), row=2, col=1)

        if actual_df is not None and len(actual_df) > 0 and ind_col in actual_df.columns:
            fig.add_trace(go.Bar(
                x=actual_idx,
                y=actual_df[ind_col],
                customdata=actual_labels,
                marker_color=_vol_colors(actual_df),
                name=f'Actual {ind_label}', showlegend=False,
                hovertemplate=hovertpl
            ), row=2, col=1)

    # ---- 布局 ----
    if mode == 'forecast':
        title = f'Kronos 未来走势预测 — 最新 {n_hist} 根历史 + 未来 {n_pred} 根预测'
    else:
        title = f'Kronos 历史回测 — {n_hist} 根输入 + {n_pred} 根预测 vs 实际'

    fig.update_layout(
        title=title,
        yaxis_title='Price',
        template='plotly_white',
        height=720,
        showlegend=True,
        dragmode='pan',          # 左键拖动 = 左右平移
        uirevision='kronos-chart',  # 切换指标时保留用户已做的缩放/平移
        margin=dict(l=30, r=65, t=60, b=45)   # 右轴留宽: 价格/成交量刻度置于右侧(同花顺风格)
    )
    # 同花顺风格: 价格轴与参考指标轴均置于右侧
    fig.update_yaxes(side='right')
    if has_lower:
        fig.update_yaxes(title_text=ind_label, row=2, col=1)

    # X 轴: 数值索引轴(连续整数) -> 缩放/平移连续顺滑; 非交易日已无空白;
    # 仅取少量索引点显示日期刻度, 避免上百个标签堆叠重叠。
    total = n_hist + n_pred
    tick_step = max(1, total // 12)
    tick_vals, tick_text = [], []
    for i in range(0, total, tick_step):
        if i < n_hist:
            tick_vals.append(i); tick_text.append(hist_labels[i])
        else:
            j = i - n_hist
            if j < len(pred_labels):
                tick_vals.append(i); tick_text.append(pred_labels[j])
    # 确保最后一根 K 线也带日期标签
    last = total - 1
    if last >= 0 and (last not in tick_vals):
        if last < n_hist:
            tick_vals.append(last); tick_text.append(hist_labels[last])
        elif (last - n_hist) < len(pred_labels):
            tick_vals.append(last); tick_text.append(pred_labels[last - n_hist])

    fig.update_xaxes(
        rangeslider_visible=False,
        title_text='交易日',
        title_standoff=20,
        tickmode='array',
        tickvals=tick_vals,
        ticktext=tick_text,
        tickangle=-45
    )

    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)

@app.route('/')
def index():
    """平台首页: 预测 / 量化 双入口"""
    return render_template('home.html')

@app.route('/predict')
def predict_page():
    """K 线预测页面(原首页)"""
    return render_template('index.html')

@app.route('/api/data-files')
def get_data_files():
    """Get available data file list"""
    data_files = load_data_files()
    return jsonify(data_files)

@app.route('/api/load-data', methods=['POST'])
def load_data():
    """Load data file"""
    try:
        data = request.get_json()
        file_path = data.get('file_path')
        
        if not file_path:
            return jsonify({'error': 'File path cannot be empty'}), 400
        
        df, error = load_data_file(file_path)
        if error:
            return jsonify({'error': error}), 400
        
        # Return data information
        data_info = build_data_info(df)
        
        return jsonify({
            'success': True,
            'data_info': data_info,
            'message': f'Successfully loaded data, total {len(df)} rows'
        })
        
    except Exception as e:
        return jsonify({'error': f'Failed to load data: {str(e)}'}), 500


# ---------------------------------------------------------------------------
# 股票模糊搜索 + 实时抓取 (A股 / 港股)
# ---------------------------------------------------------------------------
import fetch_stock_kline as _fsk  # noqa: E402  (Kronos root already on sys.path)

MARKET_CN = {"sh": "上交所", "sz": "深交所", "bj": "北交所", "hk": "港股"}
_SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"


def _decode_unicode_escape(s: str) -> str:
    """smartbox 名称字段为中文的 \\uXXXX 转义, 还原为正常中文"""
    try:
        return s.encode("latin-1").decode("unicode_escape")
    except Exception:
        return s


def _parse_smartbox(text: str):
    """解析腾讯 smartbox 联想结果, 过滤出 A股 + 港股。
    格式: v_hint="market~code~name~pinyin~type^market~..."  (条目以 ^ 分隔)"""
    m = re.search(r'v_hint="(.*?)"', text, re.S)
    if not m:
        return []
    payload = m.group(1)
    items, seen = [], set()
    for entry in payload.split("^"):
        parts = entry.split("~")
        if len(parts) < 3:
            continue
        market, code, name = parts[0], parts[1], parts[2]
        pinyin = parts[3] if len(parts) > 3 else ""
        typ = parts[4] if len(parts) > 4 else ""
        if market not in MARKET_CN:
            continue  # 仅保留 A股(sh/sz/bj) 与 港股(hk)
        name = _decode_unicode_escape(name)
        key = (market, code)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "market": market,
            "market_cn": MARKET_CN[market],
            "code": code,
            "name": name,
            "pinyin": pinyin,
            "type": typ,
        })
    return items


@app.route('/api/search-stock', methods=['POST'])
def search_stock():
    """按代码 / 拼音首字母 / 中文名模糊搜索 A股与港股"""
    try:
        data = request.get_json(silent=True) or {}
        q = (data.get('q') or '').strip()
        if not q:
            return jsonify([])
        url = f"{_SMARTBOX_URL}?t=all&q={requests.utils.quote(q)}"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        items = _parse_smartbox(r.text)
        limit = int(data.get('limit', 30))
        return jsonify(items[:limit])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/fetch-stock', methods=['POST'])
def fetch_stock():
    """抓取指定股票的实时 K 线并保存为数据文件, 返回路径与数据信息"""
    try:
        from urllib.parse import unquote
        data = request.get_json()
        code = (data.get('code') or '').strip()
        freq = data.get('freq', 'day')
        fq = data.get('fq', 'qfq')
        if not code:
            return jsonify({'error': 'code required'}), 400
        outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
        path = _fsk.fetch_and_save(code, freq, fq, "1990-01-01", outdir)
        df, err = load_data_file(path)
        if err:
            return jsonify({'error': err}), 400
        data_info = build_data_info(df)
        return jsonify({
            'success': True,
            'file_path': path,
            'file_name': os.path.basename(path),
            'data_info': data_info,
            'message': f'已导入 {os.path.basename(path)}'
        })
    except SystemExit as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'抓取失败: {str(e)}'}), 500


@app.route('/api/predict', methods=['POST'])
def predict():
    """Perform prediction"""
    global LAST_CHART_CTX
    try:
        data = request.get_json()
        file_path = data.get('file_path')
        lookback = int(data.get('lookback', 400))
        pred_len = int(data.get('pred_len', 120))
        lower_indicator = data.get('lower_indicator', 'volume')
        
        # Get prediction quality parameters
        temperature = float(data.get('temperature', 1.0))
        top_p = float(data.get('top_p', 0.9))
        sample_count = int(data.get('sample_count', 1))
        
        if not file_path:
            return jsonify({'error': 'File path cannot be empty'}), 400
        
        # Load data
        df, error = load_data_file(file_path)
        if error:
            return jsonify({'error': error}), 400

        # 可用的下方参考指标(旧数据/港股估算可能缺成交额、换手率)
        available_indicators = ['volume']
        if 'amount' in df.columns and float(df['amount'].abs().sum()) > 0:
            available_indicators.append('amount')
        if 'turnover' in df.columns and float(df['turnover'].abs().sum()) > 0:
            available_indicators.append('turnover')
        
        if len(df) < lookback:
            return jsonify({'error': f'Insufficient data length, need at least {lookback} rows'}), 400
        
        # Perform prediction
        if MODEL_AVAILABLE and predictor is not None:
            try:
                # Use real Kronos model
                # Only use necessary columns: OHLCV, excluding amount
                required_cols = ['open', 'high', 'low', 'close']
                if 'volume' in df.columns:
                    required_cols.append('volume')
                
                # Process time period selection
                start_date = data.get('start_date')
                # forecast(默认): 以最新一段为输入, 预测数据末尾之后的真实未来
                # backtest: 从 start_date 起取一段为输入, 与紧随其后的真实数据比对
                # 前端可显式指定; 未指定时按是否传 start_date 推断
                mode = data.get('mode') or ('backtest' if start_date else 'forecast')
                if mode not in ('forecast', 'backtest'):
                    mode = 'forecast'
                if mode == 'backtest' and not start_date:
                    return jsonify({'error': '回测模式需要提供 start_date'}), 400
                
                if mode == 'backtest':
                    # 历史回测: 所选窗口内前 lookback 根为输入, 其后 pred_len 根为比对基准
                    start_dt = pd.to_datetime(start_date)
                    
                    mask = df['timestamps'] >= start_dt
                    time_range_df = df[mask]
                    
                    # Ensure sufficient data: lookback + pred_len
                    if len(time_range_df) < lookback + pred_len:
                        return jsonify({'error': f'Insufficient data from start time {start_dt.strftime("%Y-%m-%d %H:%M")}, need at least {lookback + pred_len} data points, currently only {len(time_range_df)} available'}), 400
                    
                    x_df = time_range_df.iloc[:lookback][required_cols]
                    x_timestamp = time_range_df.iloc[:lookback]['timestamps']
                    y_timestamp = time_range_df.iloc[lookback:lookback+pred_len]['timestamps']
                    historical_start_idx = int(df[mask].index[0])
                    
                    time_span = time_range_df['timestamps'].iloc[lookback+pred_len-1] - time_range_df['timestamps'].iloc[0]
                    prediction_type = (
                        f"历史回测：以 {start_dt.strftime('%Y-%m-%d')} 起 {lookback} 根为输入，"
                        f"预测其后 {pred_len} 根并与实际对比（跨度 {time_span}）"
                    )
                else:
                    # 未来预测: 输入取最新的 lookback 根, 预测区间落在数据末尾之后
                    historical_start_idx = max(0, len(df) - lookback)
                    x_df = df.iloc[historical_start_idx:historical_start_idx + lookback][required_cols]
                    x_timestamp = df.iloc[historical_start_idx:historical_start_idx + lookback]['timestamps']
                    y_timestamp = pd.Series(
                        generate_future_timestamps(df['timestamps'], pred_len), name='timestamps'
                    )
                    
                    last_ts = pd.to_datetime(df['timestamps'].iloc[-1])
                    prediction_type = (
                        f"未来预测：以最新 {lookback} 根（截至 {last_ts.strftime('%Y-%m-%d %H:%M')}）为输入，"
                        f"预测后续 {pred_len} 根"
                    )
                
                # Ensure timestamps are Series format, not DatetimeIndex, to avoid .dt attribute error in Kronos model
                if isinstance(x_timestamp, pd.DatetimeIndex):
                    x_timestamp = pd.Series(x_timestamp, name='timestamps')
                if isinstance(y_timestamp, pd.DatetimeIndex):
                    y_timestamp = pd.Series(y_timestamp, name='timestamps')
                
                pred_df = predictor.predict(
                    df=x_df,
                    x_timestamp=x_timestamp,
                    y_timestamp=y_timestamp,
                    pred_len=pred_len,
                    T=temperature,
                    top_p=top_p,
                    sample_count=sample_count
                )
                
            except Exception as e:
                return jsonify({'error': f'Kronos model prediction failed: {str(e)}'}), 500
        else:
            return jsonify({'error': 'Kronos model not loaded, please load model first'}), 400
        
        # Prepare actual data for comparison (if exists)
        actual_data = []
        actual_df = None
        
        if mode == 'backtest':
            # 回测模式: 取输入窗口之后的真实数据作为比对基准
            start_dt = pd.to_datetime(start_date)
            time_range_df = df[df['timestamps'] >= start_dt]
            
            if len(time_range_df) >= lookback + pred_len:
                actual_df = time_range_df.iloc[lookback:lookback+pred_len]
                for _, row in actual_df.iterrows():
                    actual_data.append({
                        'timestamp': row['timestamps'].isoformat(),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': float(row['volume']) if 'volume' in row else 0,
                        'amount': float(row['amount']) if 'amount' in row else 0
                    })
        # forecast 模式不填充 actual_data, 因为未来尚未发生
        
        # 预测时间戳统一取自 y_timestamp, 保证图表、明细表、落盘文件三者完全一致
        future_timestamps = list(pd.to_datetime(pd.Series(y_timestamp)))
        
        chart_json = create_prediction_chart(
            df, pred_df, lookback, pred_len, actual_df,
            historical_start_idx, pred_timestamps=future_timestamps, mode=mode,
            lower_indicator=lower_indicator
        )

        # 记录上下文, 供 /api/render-chart 免重算切换下方指标
        LAST_CHART_CTX = {
            'df': df,
            'pred_df': pred_df,
            'actual_df': actual_df,
            'historical_start_idx': historical_start_idx,
            'pred_timestamps': future_timestamps,
            'mode': mode,
            'lookback': lookback,
            'pred_len': pred_len,
            'lower_indicator': lower_indicator,
            'available': available_indicators,
        }
        
        prediction_results = []
        for i, (_, row) in enumerate(pred_df.iterrows()):
            prediction_results.append({
                'timestamp': future_timestamps[i].isoformat() if i < len(future_timestamps) else f"T{i}",
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row['volume']) if 'volume' in row else 0,
                'amount': float(row['amount']) if 'amount' in row else 0
            })
        
        # Save prediction results to file
        try:
            save_prediction_results(
                file_path=file_path,
                prediction_type=prediction_type,
                prediction_results=prediction_results,
                actual_data=actual_data,
                input_data=x_df,
                prediction_params={
                    'mode': mode,
                    'lookback': lookback,
                    'pred_len': pred_len,
                    'temperature': temperature,
                    'top_p': top_p,
                    'sample_count': sample_count,
                    'start_date': start_date if start_date else None
                }
            )
        except Exception as e:
            print(f"Failed to save prediction results: {e}")
        
        first_future = future_timestamps[0].strftime('%Y-%m-%d %H:%M') if len(future_timestamps) else '-'
        last_future = future_timestamps[-1].strftime('%Y-%m-%d %H:%M') if len(future_timestamps) else '-'
        
        if mode == 'forecast':
            message = f'已生成未来 {len(prediction_results)} 根预测：{first_future} → {last_future}（该区间无实际数据可比对）'
        else:
            message = f'回测完成：生成 {len(prediction_results)} 根预测，与 {len(actual_data)} 根实际数据比对'
        
        return jsonify({
            'success': True,
            'mode': mode,
            'prediction_type': prediction_type,
            'chart': chart_json,
            'prediction_results': prediction_results,
            'actual_data': actual_data,
            'has_comparison': len(actual_data) > 0,
            'available_indicators': available_indicators,
            'lower_indicator': lower_indicator,
            'message': message
        })
        
    except Exception as e:
        return jsonify({'error': f'Prediction failed: {str(e)}'}), 500

@app.route('/api/render-chart', methods=['POST'])
def render_chart():
    """在不重新调用模型的前提下, 仅切换下方参考指标并重绘图表"""
    global LAST_CHART_CTX
    if not LAST_CHART_CTX:
        return jsonify({'error': '尚无预测结果, 请先运行一次预测'}), 400
    try:
        data = request.get_json(silent=True) or {}
        indicator = data.get('lower_indicator', LAST_CHART_CTX.get('lower_indicator', 'volume'))
        chart_json = create_prediction_chart(
            LAST_CHART_CTX['df'], LAST_CHART_CTX['pred_df'],
            LAST_CHART_CTX['lookback'], LAST_CHART_CTX['pred_len'],
            LAST_CHART_CTX['actual_df'],
            LAST_CHART_CTX['historical_start_idx'],
            pred_timestamps=LAST_CHART_CTX['pred_timestamps'],
            mode=LAST_CHART_CTX['mode'],
            lower_indicator=indicator
        )
        return jsonify({'success': True, 'chart': chart_json, 'lower_indicator': indicator})
    except Exception as e:
        return jsonify({'error': f'重绘失败: {str(e)}'}), 500


@app.route('/api/load-model', methods=['POST'])
def load_model():
    """Load Kronos model"""
    global tokenizer, model, predictor
    
    try:
        if not MODEL_AVAILABLE:
            return jsonify({'error': 'Kronos model library not available'}), 400
        
        data = request.get_json()
        model_key = data.get('model_key', 'kronos-small')
        device = data.get('device', 'cpu')
        
        if model_key not in AVAILABLE_MODELS:
            return jsonify({'error': f'Unsupported model: {model_key}'}), 400
        
        model_config = AVAILABLE_MODELS[model_key]
        
        # Load tokenizer and model
        tokenizer = KronosTokenizer.from_pretrained(model_config['tokenizer_id'])
        model = Kronos.from_pretrained(model_config['model_id'])
        
        # Create predictor
        predictor = KronosPredictor(model, tokenizer, device=device, max_context=model_config['context_length'])
        
        return jsonify({
            'success': True,
            'message': f'Model loaded successfully: {model_config["name"]} ({model_config["params"]}) on {device}',
            'model_info': {
                'name': model_config['name'],
                'params': model_config['params'],
                'context_length': model_config['context_length'],
                'description': model_config['description']
            }
        })
        
    except Exception as e:
        return jsonify({'error': f'Model loading failed: {str(e)}'}), 500

@app.route('/api/available-models')
def get_available_models():
    """Get available model list"""
    return jsonify({
        'models': AVAILABLE_MODELS,
        'model_available': MODEL_AVAILABLE
    })

@app.route('/api/model-status')
def get_model_status():
    """Get model status"""
    if MODEL_AVAILABLE:
        if predictor is not None:
            return jsonify({
                'available': True,
                'loaded': True,
                'message': 'Kronos model loaded and available',
                'current_model': {
                    'name': predictor.model.__class__.__name__,
                    'device': str(next(predictor.model.parameters()).device)
                }
            })
        else:
            return jsonify({
                'available': True,
                'loaded': False,
                'message': 'Kronos model available but not loaded'
            })
    else:
        return jsonify({
            'available': False,
            'loaded': False,
            'message': 'Kronos model library not available, please install related dependencies'
        })

# ============================================================
# 量化交易子系统: 策略管理 / 回测 / 实盘(模拟盘)
# ============================================================
try:
    import quant.api as quant_api
    from quant import db as quant_db

    def _kronos_predict_provider():
        """为量化子系统提供 Kronos 预测函数(模型未加载时返回 None)。"""
        if predictor is None:
            return None

        def predict_fn(window_df, horizon):
            required_cols = [c for c in ('open', 'high', 'low', 'close', 'volume', 'amount')
                             if c in window_df.columns]
            x_df = window_df[required_cols].copy()
            x_ts = pd.Series(window_df['timestamps'].values, name='timestamps')
            y_ts = pd.Series(generate_future_timestamps(window_df['timestamps'], horizon),
                             name='timestamps')
            pred_df = predictor.predict(
                df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
                pred_len=horizon, T=1.0, top_p=0.95, sample_count=1
            )
            return [float(v) for v in pred_df['close']]

        return predict_fn

    quant_api.set_predict_provider(_kronos_predict_provider)
    app.register_blueprint(quant_api.quant_bp)
    quant_db.init_db()

    @app.route('/quant')
    def quant_page():
        """量化交易系统页面"""
        return render_template('quant.html')

    QUANT_AVAILABLE = True
except Exception as _quant_err:
    QUANT_AVAILABLE = False
    print(f"Warning: quant subsystem unavailable: {_quant_err}")


# ============================================================
# 启动自动加载模型(可 KRONOS_AUTOLOAD=0 关闭, KRONOS_AUTO_MODEL 换模型)
# 服务重启后无需手动 Load Model 即可直接预测/回测
# ============================================================
import threading


def _auto_load_model():
    import time as _time
    _time.sleep(8)
    try:
        if not MODEL_AVAILABLE:
            return
        key = os.environ.get('KRONOS_AUTO_MODEL', 'kronos-small')
        cfg = AVAILABLE_MODELS.get(key)
        if not cfg:
            print(f"[auto-load] 未知模型 {key}, 跳过")
            return
        global tokenizer, model, predictor
        tokenizer = KronosTokenizer.from_pretrained(cfg['tokenizer_id'])
        model = Kronos.from_pretrained(cfg['model_id'])
        predictor = KronosPredictor(model, tokenizer, device='cpu',
                                    max_context=cfg['context_length'])
        print(f"[auto-load] {cfg['name']} 已自动加载(cpu)")
    except Exception as _e:
        print(f"[auto-load] 模型自动加载失败(可在页面手动加载): {_e}")


if os.environ.get('KRONOS_AUTOLOAD', '1') != '0':
    threading.Thread(target=_auto_load_model, daemon=True).start()


if __name__ == '__main__':
    print("Starting Kronos Web UI...")
    print(f"Model availability: {MODEL_AVAILABLE}")
    if MODEL_AVAILABLE:
        print("Tip: You can load Kronos model through /api/load-model endpoint")
    else:
        print("Tip: Will use simulated data for demonstration")
    
    app.run(debug=True, host='0.0.0.0', port=7070)
