# -*- coding: utf-8 -*-
"""聊天式策略创建: 中文自然语言 -> 策略草稿(类型/参数/标的)。

设计为无状态解析器: 前端把解析出的 draft 带回, 用户点「创建」或回复「确认」时
连同 draft 一起提交, 由 /api/quant/chat 的 confirm 分支落库。
"""

import re
import codecs
import requests

SMARTBOX_URL = "https://smartbox.gtimg.cn/s3/"
UA = {"User-Agent": "Mozilla/5.0"}


def _decode(s):
    """smartbox 名称可能以 \\uXXXX 转义返回, 还原为中文。"""
    if '\\u' not in s and '\\x' not in s:
        return s
    try:
        return codecs.decode(s, 'unicode_escape')
    except Exception:
        return s

# ---------------- 股票名称/代码识别 ----------------

_NAME_CACHE = {}


def search_stocks(q, limit=5, timeout=8):
    """腾讯 smartbox 联想搜索, 返回 [{'code': 'sz300505', 'name': '川金诺'}]"""
    try:
        r = requests.get(f"{SMARTBOX_URL}?t=all&q={requests.utils.quote(q)}",
                         timeout=timeout, headers=UA)
        m = re.search(r'v_hint="(.*?)"', r.text, re.S)
        if not m:
            return []
        out = []
        for entry in m.group(1).split("^")[:limit]:
            parts = entry.split("~")
            if len(parts) >= 3 and parts[0] in ('sh', 'sz', 'bj', 'hk'):
                name = _decode(parts[2])
                if name:
                    out.append({'code': parts[0] + parts[1], 'name': name})
        return out
    except Exception:
        return []


def resolve_stock_text(text):
    """从消息中找出股票: ① 6位代码 ② 引号/括号内名称 ③ 紧跟指示词的名称 ④ CJK 片段精确匹配。
    返回 {'code','name'} 或 None。"""
    # ① 6 位代码
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    if m:
        code = m.group(1)
        prefix = 'sh' if code[0] in '69' else ('bj' if code[0] in '84' else 'sz')
        sym = prefix + code
        cached = _NAME_CACHE.get(sym)
        if cached is None:
            hits = search_stocks(code, 1)
            cached = hits[0]['name'] if hits else code
            _NAME_CACHE[sym] = cached
        return {'code': sym, 'name': cached}
    # ② 引号/括号内的名称
    for m in re.finditer(r'[「『"“（(【\[]([^」』"”）)】\]]{2,8})[」』"”）)】\]]', text):
        hit = _exact_match(m.group(1))
        if hit:
            return hit
    # ③ 指示词后紧跟的名称/代码
    m = re.search(r'(?:标的|股票|代码|跟踪|关注|操作|买入|交易)[:：\s]*([0-9A-Za-z]{4,6}|[\u4e00-\u9fa5]{2,6})', text)
    if m:
        seg = m.group(1)
        if seg.isdigit():
            return resolve_stock_text(seg)
        hit = _exact_match(seg)
        if hit:
            return hit
    # ④ CJK 片段 n-gram 精确匹配(常见股票名 2-4 字)
    for run in re.findall(r'[\u4e00-\u9fa5]+', text):
        for n in (4, 3, 2, 5):
            for i in range(0, max(1, len(run) - n + 1)):
                cand = run[i:i + n]
                if len(cand) < n:
                    continue
                hit = _exact_match(cand)
                if hit:
                    return hit
    return None


def _exact_match(name):
    """smartbox 搜索并要求名称完全相等(A股优先)。"""
    for hit in search_stocks(name, 6):
        if hit['name'] == name and not hit['code'].startswith('hk'):
            return hit
    return None


# ---------------- 策略类型识别 ----------------

_TYPE_KEYWORDS = [
    ('kronos_forecast', r'kronos|克罗诺斯|大模型|ai\s*预测|模型预测|预测驱动|预测策略|走势预测'),
    ('macd', r'(?<![a-z])macd(?![a-z])|ema'),
    ('rsi', r'(?<![a-z])rsi(?![a-z])|超买超卖|相对强弱'),
    ('bollinger', r'布林|boll'),
    ('ma_cross', r'均线|金叉|死叉|(?<![a-z])ma(?![a-z])|双线'),
]


def detect_type(text):
    low = text.lower()
    for stype, pat in _TYPE_KEYWORDS:
        if re.search(pat, low):
            return stype
    return None


# ---------------- 参数抽取 ----------------

def _num_after(text, pats, pct=False):
    """在关键词后找数字(pct=True 支持 百分之X / X%)。"""
    for pat in pats:
        m = re.search(r'(?:' + pat + r')\s*[:：为是]?\s*(\d+(?:\.\d+)?)\s*%?', text)
        if m:
            return float(m.group(1))
    if pct:
        m = re.search(r'百分之(\d+(?:\.\d+)?)', text)
        if m:
            return float(m.group(1)) / 100
    return None


def extract_params(text, stype):
    """按策略类型抽取参数, 返回 (params, missing) —— missing 是用户没给而该类型必需的参数。"""
    p = {}
    if stype == 'ma_cross':
        v = _num_after(text, [r'快线|短周期|快速|短期均线|快均线'])
        if v:
            p['fast'] = int(v)
        v = _num_after(text, [r'慢线|长周期|慢速|长期均线|慢均线'])
        if v:
            p['slow'] = int(v)
    elif stype == 'macd':
        for key, pats in (('fast', [r'快线|快ema|快周期']), ('slow', [r'慢线|慢ema|慢周期']),
                          ('signal', [r'信号线|dea|平滑'])):
            v = _num_after(text, pats)
            if v:
                p[key] = int(v)
    elif stype == 'rsi':
        v = _num_after(text, [r'周期|rsi周期'])
        if v:
            p['period'] = int(v)
        v = _num_after(text, [r'超卖'])
        if v:
            p['oversold'] = int(v)
        v = _num_after(text, [r'超买'])
        if v:
            p['overbought'] = int(v)
    elif stype == 'bollinger':
        v = _num_after(text, [r'周期|均线周期'])
        if v:
            p['period'] = int(v)
        v = _num_after(text, [r'带宽|标准差|倍数'])
        if v:
            p['std'] = float(v)
    elif stype == 'kronos_forecast':
        v = _num_after(text, [r'预测|展望|看?未来'])
        if v:
            p['horizon'] = int(v)
        v = _num_after(text, [r'阈值|涨跌幅|触发|涨'], pct=True)
        if v is not None:
            v = v / 100 if v > 1 else v
            p['threshold'] = round(float(v), 4)
        v = _num_after(text, [r'每隔|调仓|频率|每\d*天'])
        if v:
            p['rebalance_every'] = int(v)
        v = _num_after(text, [r'回看|lookback|历史长度|输入'])
        if v:
            p['lookback'] = int(v)
    return p


def extract_capital(text):
    m = re.search(r'(?:本金|资金|初始资金|用)\s*(\d+(?:\.\d+)?)\s*万', text)
    if m:
        return float(m.group(1)) * 10000
    m = re.search(r'(?:本金|初始资金)[:：\s]*(\d{5,})', text)
    if m:
        return float(m.group(1))
    return None


def extract_name(text):
    m = re.search(r'(?:叫|命名为|名字叫|名称)\s*[「『"“]?(.{2,20})[」』"”]?', text)
    if m:
        return m.group(1).strip()
    return None


# ---------------- 意图识别 ----------------

CONFIRM_WORDS = re.compile(r'^(确认|确定|创建|建|好的|可以|就这样|ok|yes|好)\s*$')
LIST_WORDS = re.compile(r'(我的策略|策略列表|看看策略|有哪些策略|列出策略|当前策略)')
HELP_WORDS = re.compile(r'(怎么用|帮助|能做什么|你会什么|使用说明)')


def is_confirm(text):
    return bool(CONFIRM_WORDS.match(text.strip().lower()))


def classify(text):
    if HELP_WORDS.search(text):
        return 'help'
    if LIST_WORDS.search(text):
        return 'list'
    return 'parse'


HELP_TEXT = (
    "我可以帮你用一句话创建量化策略，例如：\n"
    "• 帮我创建一个双均线策略，快线10慢线30，标的川金诺\n"
    "• 用 Kronos 预测茅台，预测未来60根，涨5%买入跌5%清仓，本金20万\n"
    "• 创建一个 MACD 策略跟踪 赛微电子\n"
    "支持五种策略：双均线 / MACD / RSI / 布林带 / Kronos预测。\n"
    "说明标的可以直接写股票名或6位代码，参数用中文描述即可，我会自动解析。\n"
    "解析完成后点「创建策略」或回复「确认」即可保存。"
)

MAX_NGRAM_CALLS = 40  # smartbox 精确匹配的调用上限, 防止长文本拖慢


def parse_message(text):
    """解析用户消息 -> {'intent': 'draft'|'help'|'list', 'reply': str, 'draft': dict|None}"""
    global MAX_NGRAM_CALLS
    text = (text or '').strip()
    if not text:
        return {'intent': 'help', 'reply': '请描述你想创建的策略~\n' + HELP_TEXT, 'draft': None}
    intent = classify(text)
    if intent == 'help':
        return {'intent': 'help', 'reply': HELP_TEXT, 'draft': None}
    if intent == 'list':
        return {'intent': 'list', 'reply': '__LIST__', 'draft': None}

    stype = detect_type(text)
    if stype is None:
        return {'intent': 'draft', 'draft': None,
                'reply': "没听出策略类型呢。目前支持：双均线 / MACD / RSI / 布林带 / Kronos预测。\n"
                         "例如：「创建一个双均线策略，快线10慢线30，标的川金诺」"}

    stock = resolve_stock_text(text)
    params = extract_params(text, stype)
    capital = extract_capital(text)
    name = extract_name(text)

    missing = []
    if stock is None:
        missing.append('标的(股票名或6位代码)')

    from . import strategies as st
    meta = st.STRATEGY_TYPES[stype]
    full_params = dict(meta['params'])
    full_params.update({k: v for k, v in params.items() if v is not None})
    # 基本合法性
    if stype == 'ma_cross' and full_params.get('fast', 0) >= full_params.get('slow', 999):
        full_params['slow'] = int(full_params['fast']) * 4

    stock_desc = f"{stock['name']}({stock['code']})" if stock else "❓未识别"
    param_desc = '，'.join(f"{meta['param_doc'].get(k, k)}={v}" for k, v in full_params.items())
    draft = {
        'name': name or f"{meta['label']}·{stock['name'] if stock else '未命名'}",
        'type': stype,
        'params': full_params,
        'symbol': stock['code'] if stock else '',
        'initial_capital': capital or 100000,
        'description': f"聊天创建：{text[:120]}",
    }

    reply = f"已解析你的策略：\n• 类型：{meta['label']}\n• 标的：{stock_desc}\n• 参数：{param_desc}\n" \
            f"• 初始资金：{(capital or 100000):,.0f} 元"
    if missing:
        reply += "\n⚠️ " + '、'.join(missing) + " 还没给全，可以补充说明，或点「创建策略」前在表单里改。"
    reply += "\n确认无误就点「创建策略」，或继续补充要求。"
    return {'intent': 'draft', 'reply': reply, 'draft': draft}
