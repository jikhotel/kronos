#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QMT 桥接服务 — 部署在云端 Windows 机器(与 QMT 客户端同机)。

用途: Mac 本地无法运行 Windows 版 QMT 客户端。本服务跑在装有 QMT 的
Windows/云主机上, 把 miniQMT(xtquant) 暴露为 HTTP 接口; Mac 端 Kronos
实盘引擎通过「远程 QMT 通道」适配器远程下单。

零第三方依赖: 仅 Python 3 标准库 + xtquant(云端机器 pip install xtquant)。

快速启动 (Windows 云主机上):
    python qmt_bridge.py --userdata "D:\\国金QMT交易端\\userdata_mini" --account 40735103 --token 你的密钥

无 QMT 环境联调(任意电脑):
    python qmt_bridge.py --mock --token test

建议搭配 nssm / 任务计划程序 注册为开机自启服务。

安全:
  - 所有请求需带 token (HTTP 头 X-Bridge-Token 或 ?token=)
  - 强烈建议: 云防火墙/安全组仅放行 Mac 出口 IP, 端口默认 17890
  - 仅暴露下单/查询, 不暴露任何密码
"""

import argparse
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    'mode': 'qmt',          # qmt | mock
    'token': '',
    'userdata': '',
    'account': '',
    'session_id': 888888,
    'started_at': '',
    'last_order_at': None,
    'orders': [],           # 今日订单回报(内存)
}

_TRADER = None
_STOCK_ACC = None
_XT = None
_LOCK = threading.Lock()


# ---------------- xtquant 惰性初始化 ----------------

def ensure_trader():
    """连接 miniQMT; 失败抛 RuntimeError。"""
    global _TRADER, _STOCK_ACC, _XT
    if _TRADER is not None:
        return
    if STATE['mode'] == 'mock':
        return                       # mock 模式不需要 trader
    try:
        from xtquant import xttrader, xtconstant
    except ImportError as e:
        raise RuntimeError("云端未安装 xtquant: pip install xtquant") from e
    import os
    if not os.path.isdir(STATE['userdata']):
        raise RuntimeError(f"userdata 路径不存在: {STATE['userdata']}")
    trader = xttrader.XtQuantTrader(STATE['userdata'], STATE['session_id'])
    trader.start()
    rc = trader.connect()
    if rc != 0:
        raise RuntimeError(f"连接 QMT 客户端失败(code={rc}), 请确认客户端已启动并登录")
    if not trader.subscribe(STATE['account']):
        raise RuntimeError(f"订阅资金账号 {STATE['account']} 失败")
    _TRADER = trader
    _STOCK_ACC = xttrader.StockAccount(STATE['account'])
    _XT = xtconstant


# ---------------- mock 撮合(联调用) ----------------

MOCK = {
    'cash': 1000000.0,
    'positions': {},   # symbol -> {volume, can_use, cost}
}


def _mock_order(side, symbol, volume, price):
    key = symbol
    pos = MOCK['positions'].setdefault(key, {'volume': 0, 'can_use': 0, 'cost': 0.0})
    if side == 'buy':
        cost = volume * price
        if cost > MOCK['cash']:
            return None, '资金不足'
        MOCK['cash'] -= cost
        total = pos['volume'] + volume
        pos['cost'] = (pos['cost'] * pos['volume'] + cost) / total if total else 0
        pos['volume'] = total
        pos['can_use'] = total          # mock 不模拟 T+1
        return int(time.time() * 1000) % 100000000, None
    else:
        if pos['volume'] < volume:
            return None, '持仓不足'
        pos['volume'] -= volume
        pos['can_use'] = pos['volume']
        MOCK['cash'] += volume * price
        return int(time.time() * 1000) % 100000000, None


# ---------------- HTTP Handler ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'KronosQmtBridge/1.0'

    # ---- 基础 ----
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        token = self.headers.get('X-Bridge-Token') or ''
        if not token:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            token = (q.get('token') or [''])[0]
        if STATE['token'] and token != STATE['token']:
            self._send(401, {'ok': False, 'error': 'token 无效'})
            return False
        return True

    def log_message(self, fmt, *args):
        sys.stdout.write('[bridge] %s %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    def _read_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip('/')
        if path in ('', '/ping'):
            # /ping 不需要连接 QMT, 用于探活
            if not self._authed():
                return
            qmt_ok = True
            detail = ''
            if STATE['mode'] == 'mock':
                detail = '(mock 模式)'
            else:
                try:
                    ensure_trader()
                except Exception as e:
                    qmt_ok = False
                    detail = str(e)
            self._send(200, {'ok': True, 'bridge': 'kronos-qmt-bridge',
                             'mode': STATE['mode'], 'qmt_connected': qmt_ok,
                             'account': STATE['account'],
                             'started_at': STATE['started_at'],
                             'message': '桥接服务在线' + (f' | QMT: {detail}' if detail else '')})
            return

        if not self._authed():
            return
        try:
            if path == '/account':
                self._send(200, self._account())
            elif path == '/positions':
                self._send(200, {'ok': True, 'positions': self._positions()})
            elif path == '/orders':
                self._send(200, {'ok': True, 'orders': STATE['orders'][-50:]})
            else:
                self._send(404, {'ok': False, 'error': f'未知路径 {path}'})
        except Exception as e:
            self._send(500, {'ok': False, 'error': str(e)})

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path.rstrip('/')
        if not self._authed():
            return
        if path != '/order':
            self._send(404, {'ok': False, 'error': f'未知路径 {path}'})
            return
        data = self._read_json()
        try:
            self._send(200, self._order(data))
        except Exception as e:
            self._send(500, {'ok': False, 'error': str(e)})

    # ---- 业务 ----
    def _account(self):
        if STATE['mode'] == 'mock':
            return {'ok': True, 'cash': MOCK['cash'],
                    'market_value': 0.0, 'total': MOCK['cash']}
        ensure_trader()
        a = _TRADER.query_stock_asset(_STOCK_ACC)
        if a is None:
            raise RuntimeError('查询资产失败(QMT 未就绪)')
        return {'ok': True, 'cash': float(a.cash),
                'market_value': float(a.market_value),
                'total': float(a.total_asset)}

    def _positions(self):
        if STATE['mode'] == 'mock':
            return [{'symbol': s, 'volume': p['volume'],
                     'available_volume': p['can_use'], 'avg_cost': round(p['cost'], 3)}
                    for s, p in MOCK['positions'].items() if p['volume']]
        ensure_trader()
        out = []
        for p in _TRADER.query_stock_positions(_STOCK_ACC) or []:
            if p.m_nVolume:
                code = str(p.stockcode)
                # 上交所代码形如 600519.SH -> sh600519; 深交所 000001.SZ -> sz000001
                if '.' in code:
                    num, mkt = code.split('.', 1)
                    code = ('sh' if mkt.upper().startswith('SH') else 'sz') + num
                out.append({'symbol': code, 'volume': int(p.m_nVolume),
                            'available_volume': int(p.m_nCanUseVolume),
                            'avg_cost': float(p.m_dOpenPrice or 0)})
        return out

    def _order(self, data):
        symbol = str(data.get('symbol') or '').strip().lower()
        side = str(data.get('side') or '').strip().lower()
        volume = int(data.get('volume') or 0)
        price = float(data.get('price') or 0)
        price_type = str(data.get('price_type') or 'fix')          # fix | market
        remark = str(data.get('remark') or '')
        strategy_name = str(data.get('strategy_name') or 'kronos')

        if side not in ('buy', 'sell'):
            return {'ok': False, 'error': 'side 必须是 buy/sell'}
        if not symbol or volume <= 0:
            return {'ok': False, 'error': 'symbol/volume 参数非法'}
        if price_type != 'market' and price <= 0:
            return {'ok': False, 'error': '限价单 price 必须 > 0'}

        with _LOCK:
            if STATE['mode'] == 'mock':
                oid, err = _mock_order(side, symbol, volume, price)
                result = ({'ok': True, 'status': 'submitted', 'order_id': oid,
                           'symbol': symbol, 'side': side, 'volume': volume, 'price': price}
                          if oid is not None else {'ok': False, 'error': err})
            else:
                ensure_trader()
                xt_side = (_XT.STOCK_BUY if side == 'buy' else _XT.STOCK_SELL)
                xt_pricetype = _XT.FIX_PRICE if price_type == 'fix' else _XT.LATEST_PRICE
                oid = _TRADER.order_stock(
                    _STOCK_ACC, symbol, xt_side, volume, xt_pricetype, price,
                    strategy_name=strategy_name, order_remark=remark)
                result = ({'ok': True, 'status': 'submitted', 'order_id': oid,
                           'symbol': symbol, 'side': side, 'volume': volume, 'price': price}
                          if oid and oid > 0
                          else {'ok': False, 'error': f'QMT 下单失败(code={oid})'})
            STATE['last_order_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            STATE['orders'].append({'at': STATE['last_order_at'], 'side': side,
                                    'symbol': symbol, 'volume': volume, 'price': price,
                                    'result': result.get('status') or result.get('error')})
        return result


# ---------------- 启动 ----------------

def main():
    ap = argparse.ArgumentParser(description='Kronos QMT 桥接服务')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=17890)
    ap.add_argument('--token', default='',
                    help='访问令牌; 不填则自动生成并打印')
    ap.add_argument('--userdata', default=r'D:\国金QMT交易端\userdata_mini',
                    help='QMT 客户端 userdata_mini 目录')
    ap.add_argument('--account', default='', help='资金账号')
    ap.add_argument('--session-id', type=int, default=888888)
    ap.add_argument('--mock', action='store_true', help='mock 模式(无 QMT 联调用)')
    args = ap.parse_args()

    STATE['mode'] = 'mock' if args.mock else 'qmt'
    STATE['token'] = args.token or secrets.token_urlsafe(16)
    STATE['userdata'] = args.userdata
    STATE['account'] = args.account
    STATE['session_id'] = args.session_id
    STATE['started_at'] = time.strftime('%Y-%m-%d %H:%M:%S')

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print('=' * 60)
    print(f'  Kronos QMT 桥接服务  mode={"MOCK" if args.mock else "QMT"}')
    print(f'  监听: {args.host}:{args.port}')
    print(f'  token: {STATE["token"]}')
    print(f'  资金账号: {args.account or "(未填)"}')
    print(f'  userdata: {STATE["userdata"]}')
    print('  Mac 端配置: broker_type=qmt_remote, '
          f'config={{"url": "http://<本机IP>:{args.port}", "token": "<token>"}}')
    print('  安全提醒: 云安全组建议仅放行你的出口 IP')
    print('=' * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')


if __name__ == '__main__':
    main()
