"""券商实盘网关。

支持的通道:
  - qmt       : miniQMT (迅投, xtquant Python API; 需本机运行已登录的 QMT 客户端, 仅 Windows)
  - qmt_remote: 远程 QMT 通道(HTTP 桥接; 云端 Windows 跑 bridge/qmt_bridge.py + QMT,
                Mac 通过网络远程下单, 见 bridge/部署指南.md)
  - ths       : 同花顺客户端自动化 (easytrader; 需本机安装并登录同花顺客户端)
  - paper     : 模拟盘(默认, 无需任何外部依赖)

安全设计:
  - 密码加密存储(机器密钥派生 XOR+base64, 防明文直读); 真实资金请知悉风险
  - 实盘 Broker 均为惰性导入: 未安装对应依赖时返回清晰错误而不是崩溃
  - 实盘下单带安全阀: 单笔最大金额 / 总仓位比例上限(在 LiveEngine 层执行)
"""

import base64
import hashlib
import json
import os
import platform
import urllib.request
import urllib.error


# ---------------- 密码加密(本地机器密钥派生) ----------------

def _machine_key():
    raw = f"{platform.node()}|{platform.system()}|{platform.machine()}|kronos-quant"
    return hashlib.sha256(raw.encode()).digest()


def encrypt_secret(plain):
    """对称混淆: 机器密钥 XOR + base64。防 sqlite 明文直读; 非高强度加密。"""
    if not plain:
        return ''
    key = _machine_key()
    data = plain.encode('utf-8')
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.b64encode(out).decode('ascii')


def decrypt_secret(token):
    if not token:
        return ''
    try:
        key = _machine_key()
        data = base64.b64decode(token.encode('ascii'))
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode('utf-8')
    except Exception:
        return ''


# ---------------- Broker 工厂与实现 ----------------

class BrokerError(Exception):
    """券商通道错误(依赖缺失/连接失败等)。"""


class QmtBroker:
    """miniQMT 适配器。

    使用方式: 本机安装并登录 QMT 客户端(国金/国盛/华鑫等券商版), 启用独立交易模式后,
    `pip install xtquant`, 配置 userdata 路径与资金账号即可。密码由 QMT 客户端管理,
    此处 password 字段仅作备注存储(可选)。
    """

    name = 'QMT/miniQMT'

    def __init__(self, config):
        self.config = config
        self.account_id = config.get('account_id') or config.get('account', '')
        self.userdata = config.get('userdata_path', r'D:\国金QMT交易端\userdata_mini')
        self.session_id = int(config.get('session_id', 888888))
        self._trader = None
        self._stock_acc = None

    def _ensure(self):
        if self._trader is not None:
            return
        try:
            from xtquant import xttrader, xtconstant
        except ImportError as e:
            raise BrokerError(
                "未安装 xtquant (miniQMT Python 库)。请在运行本系统的电脑上: "
                "1) 安装并登录券商 QMT 客户端; 2) pip install xtquant; "
                "3) 在账户配置中填写 userdata_mini 路径与资金账号。") from e
        if not os.path.isdir(self.userdata):
            raise BrokerError(f"QMT userdata 路径不存在: {self.userdata} (请填写 QMT 客户端 userdata_mini 目录)")
        self._xtconstant = xtconstant
        trader = xttrader.XtQuantTrader(self.userdata, self.session_id)
        trader.start()
        connect_result = trader.connect()
        if connect_result != 0:
            raise BrokerError(f"连接 QMT 客户端失败(code={connect_result}), 请确认客户端已启动并登录")
        if not trader.subscribe(self.account_id):
            raise BrokerError(f"订阅资金账号 {self.account_id} 失败, 请检查账号已在 QMT 客户端登录")
        self._stock_acc = xttrader.StockAccount(self.account_id)
        self._trader = trader

    # ---- BrokerBase 兼容接口 ----
    def get_cash(self):
        self._ensure()
        return float(self._trader.query_stock_asset(self._stock_acc).cash)

    def test_connection(self):
        self._ensure()
        asset = self._trader.query_stock_asset(self._stock_acc)
        return {'ok': True, 'cash': float(asset.cash),
                'message': f'QMT 连接成功, 可用资金 {asset.cash:,.0f}'}

    def buy(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'buy')

    def sell(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'sell')

    def _order(self, symbol, volume, price, reason, strategy, side):
        self._ensure()
        xt_type = (self._xtconstant.STOCK_BUY if side == 'buy'
                   else self._xtconstant.STOCK_SELL)
        # QMT 代码: sz300505 / sh600519 / 港股暂不支持
        order_id = self._trader.order_stock(
            self._stock_acc, symbol, xt_type, int(volume),
            self._xtconstant.FIX_PRICE, float(price),
            strategy_name=strategy.get('name', 'kronos-quant') if strategy else 'kronos-quant',
            order_remark=reason)
        if order_id < 0:
            return {'status': 'rejected', 'reason': f'QMT 下单失败(code={order_id})'}
        return {'status': 'submitted', 'order_id': order_id,
                'volume': volume, 'price': price, 'reason': reason}

    def positions(self):
        self._ensure()
        out = []
        for p in self._trader.query_stock_positions(self._stock_acc) or []:
            if p.m_nVolume:
                out.append({'symbol': p.stockcode.split('.')[0] if '.' in str(p.stockcode) else p.stockcode,
                            'volume': int(p.m_nVolume), 'available_volume': int(p.m_nCanUseVolume),
                            'avg_cost': float(p.m_dOpenPrice or 0)})
        return out


class RemoteQmtBroker:
    """远程 QMT 通道适配器 (HTTP 桥接)。

    架构: 云端 Windows 机器运行 bridge/qmt_bridge.py(与 QMT 客户端同机),
    本适配器通过 HTTP 远程调用其下单/查询接口, 使 Mac 等非 Windows 设备
    也能接入 miniQMT 实盘。

    config 字段:
      - url   : 桥接服务地址, 如 http://1.2.3.4:17890 (必填)
      - token : 桥接服务访问令牌 (必填)
      - account_id : 资金账号(仅备注用途, 实际账号在云端配置)
    """

    name = 'QMT远程通道'

    def __init__(self, config):
        self.config = config
        self.url = (config.get('url') or '').strip().rstrip('/')
        self.token = config.get('token', '')
        self.account_id = config.get('account_id', '')
        if not self.url:
            raise BrokerError('远程通道缺少 url 配置, 请填写云端桥接服务地址(如 http://IP:17890)')

    # ---- HTTP 基础 ----
    def _call(self, method, path, body=None, timeout=15):
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode('utf-8') if body is not None else None,
            method=method,
            headers={'Content-Type': 'application/json',
                     'X-Bridge-Token': self.token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode('utf-8'))
            except Exception:
                raise BrokerError(f'桥接服务返回 HTTP {e.code}')
        except urllib.error.URLError as e:
            raise BrokerError(
                f'无法连接云端桥接服务 {self.url}: {e.reason}。'
                '请确认: 1) 云服务器已启动 qmt_bridge.py; 2) 安全组已放行端口; '
                '3) token 正确。部署说明见 bridge/部署指南.md')
        except Exception as e:
            raise BrokerError(f'桥接服务通信异常: {e}')

    # ---- BrokerBase 兼容接口 ----
    def get_cash(self):
        r = self._call('GET', '/account')
        if not r.get('ok'):
            raise BrokerError(r.get('error', '查询资金失败'))
        return float(r['cash'])

    def test_connection(self):
        r = self._call('GET', '/ping')
        if not r.get('ok'):
            return {'ok': False, 'message': r.get('error', '桥接服务不可达')}
        if not r.get('qmt_connected'):
            return {'ok': False,
                    'message': f"桥接在线但 QMT 未就绪: {r.get('message', '')} "
                               '(云端需启动并登录 QMT 客户端)'}
        cash = ''
        try:
            cash = f", 可用资金 {self.get_cash():,.0f}"
        except Exception:
            pass
        mode = 'mock' if r.get('mode') == 'mock' else '实盘'
        acct = r.get('account') or self.account_id or '-'
        return {'ok': True, 'mode': r.get('mode'),
                'message': f"远程通道连接成功({mode}, 账号 {acct}{cash})"}

    def buy(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'buy')

    def sell(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'sell')

    def _order(self, symbol, volume, price, reason, strategy, side):
        try:
            r = self._call('POST', '/order', {
                'symbol': symbol, 'side': side, 'volume': int(volume),
                'price': float(price), 'price_type': 'fix',
                'strategy_name': (strategy or {}).get('name', 'kronos-quant'),
                'remark': reason})
        except BrokerError as e:
            return {'status': 'rejected', 'reason': str(e)}
        if not r.get('ok'):
            return {'status': 'rejected', 'reason': r.get('error', '下单失败')}
        return {'status': 'submitted', 'order_id': r.get('order_id'),
                'volume': volume, 'price': price, 'reason': reason}

    def positions(self):
        r = self._call('GET', '/positions')
        if not r.get('ok'):
            raise BrokerError(r.get('error', '查询持仓失败'))
        return r.get('positions') or []


class ThsBroker:
    """同花顺客户端自动化适配器 (easytrader)。

    需本机安装同花顺下单客户端, pip install easytrader, 配置客户端路径。
    首次连接由 easytrader 自动唤起客户端并登录。
    """

    name = '同花顺(easytrader)'

    def __init__(self, config):
        self.config = config
        self.account = config.get('account_id') or config.get('account', '')
        self.password = decrypt_secret(config.get('password_enc') or '')
        self.client_path = config.get('client_path', '')
        self._user = None

    def _ensure(self):
        if self._user is not None:
            return
        try:
            import easytrader
        except ImportError as e:
            raise BrokerError(
                "未安装 easytrader。请在运行本系统的电脑上: "
                "1) 安装同花顺下单客户端; 2) pip install easytrader; "
                "3) 账户配置中填写账号/密码(加密存储)与客户端 exe 路径。") from e
        user = easytrader.use('ths')
        if self.client_path:
            user.connect(self.client_path)
        prepare = {'user': self.account}
        if self.password:
            prepare['password'] = self.password
        user.prepare(prepare)
        self._user = user

    def get_cash(self):
        self._ensure()
        return float(self._user.balance[0].get('可用金额', 0))

    def test_connection(self):
        self._ensure()
        bal = self._user.balance
        return {'ok': True, 'cash': float(bal[0].get('可用金额', 0)),
                'message': f"同花顺连接成功, 可用资金 {bal[0].get('可用金额', 0):,.0f}"}

    def _to_ths_code(self, symbol):
        # sz300505 -> 300505 (同花顺客户端按市场自动匹配)
        return symbol[2:] if symbol[:2] in ('sh', 'sz', 'bj', 'hk') else symbol

    def buy(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'buy')

    def sell(self, symbol, volume, price, reason, strategy=None):
        return self._order(symbol, volume, price, reason, strategy, 'sell')

    def _order(self, symbol, volume, price, reason, strategy, side):
        self._ensure()
        code = self._to_ths_code(symbol)
        try:
            if side == 'buy':
                r = self._user.buy(code, price=round(price, 2), amount=int(volume))
            else:
                r = self._user.sell(code, price=round(price, 2), amount=int(volume))
            return {'status': 'submitted', 'result': r, 'volume': volume, 'price': price}
        except Exception as e:
            return {'status': 'rejected', 'reason': f'同花顺下单失败: {e}'}


class PaperBrokerStub:
    """工厂兼容壳: 模拟盘 broker 由 live.PaperBroker 实现。"""

    name = '模拟盘'


BROKER_TYPES = {
    'qmt': {'label': 'QMT / miniQMT', 'class': QmtBroker,
            'desc': '券商 QMT 客户端 + xtquant Python 库; 支持完整订单回报, 最稳定的实盘通道',
            'fields': ['account_id', 'userdata_path', 'session_id']},
    'qmt_remote': {'label': '远程 QMT 通道 (云端桥接)', 'class': RemoteQmtBroker,
                   'desc': '云服务器 Windows 跑 qmt_bridge.py + QMT 客户端, 本机 HTTP 远程下单; '
                           'Mac 可用。配置 url + token, 部署见 bridge/部署指南.md',
                   'fields': ['url', 'token', 'account_id']},
    'ths': {'label': '同花顺客户端 (easytrader)',
            'class': ThsBroker,
            'desc': '同花顺下单客户端 UI 自动化; 门槛低但依赖客户端界面, 稳定性一般',
            'fields': ['account_id', 'password', 'client_path']},
    'paper': {'label': '模拟盘 (Paper)', 'class': PaperBrokerStub,
              'desc': '本地模拟撮合, 无需任何外部依赖, 默认通道',
              'fields': []},
}


def create_broker(broker_type, config):
    """按类型创建 broker 实例。paper 类型返回 None(由 LiveEngine 使用内置 PaperBroker)。"""
    entry = BROKER_TYPES.get(broker_type)
    if not entry:
        raise BrokerError(f"未知券商通道: {broker_type} (可选: {', '.join(BROKER_TYPES)})")
    if broker_type == 'paper':
        return None
    return entry['class'](config or {})
