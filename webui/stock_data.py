"""
Kronos · 当日看板 / 龙虎榜 数据层
=================================
零额外依赖：仅用标准库 urllib 直连 腾讯财经 / 东方财富 公开接口。
与 stock-platform 的 data_source.py 逻辑一致，但彻底去掉 AKShare 依赖：
  - 全A快照 / 板块排名 / 板块资金流 / 龙虎榜 全部走东财公开接口
  - 大盘指数 / 个股实时行情 / K线 / 分时       走腾讯财经
所有对外函数返回 dict，含 source / updated_at 字段，便于前端标注数据来源；
失败时优雅降级（返回 source='none' 或 估算值），不让页面整体崩溃。

解码约定（与 screener_gen.py 对齐）：
  f2 最新价 = 元×100  -> /100
  f3 涨跌幅 = 万分之一 -> /100
  f8 换手率 = 万分之一 -> /100
  f10 量比  = ×100     -> /100
  f6 成交额 / f62 主力净流入 / f20 总市值 / f21 流通市值 = 元（原值）
"""
import os
import json
import math
import time
import datetime
import urllib.request
import urllib.error
import urllib.parse
from collections import defaultdict

# 本机无代理；沙盒内 pop 代理以便直连外网（对本机无副作用）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REF = "https://quote.eastmoney.com/"

# ---- 东财 push2 clist 字段模板 ----
_FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"          # 沪深京A股
_F_ALL = "f12,f13,f14,f2,f3,f6,f62,f8,f10,f20,f21"
_FS_BOARD = "m:90+t:2"                                  # 行业板块
_F_BOARD = "f12,f14,f3,f104,f105,f128,f140,f137,f20,f8,f62"
_F_BOARD_FLOW = "f12,f14,f3,f62,f184"

SPOT_CACHE = os.path.join(CACHE_DIR, "spot.json")
SPOT_TTL = 60          # 全A快照缓存 60 秒（仪表盘频繁刷新时复用）
SPOT_PAGES = 56        # 全A约 5500+ 只，pz=100 分 56 页（与 screener 一致）
SPOT_PZ = 100


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _num(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v in ("-", "", "None", "NaN"):
            return None
        try:
            v = float(v)
        except ValueError:
            return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_json(url, timeout=20, tries=6):
    """GET 一个 URL 并返回解析后的 JSON；带重试（东财偶发断连）；失败返回 None。"""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REF})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception as e:          # 断连 / 超时 -> 重试
            last = e
            time.sleep(2.0 + i)         # 递增退避，缓解东财瞬时限流
    return None


def _em_clist(fs, fields, pz, pn, tries=6, timeout=20):
    """东财 push2 clist 单页 -> diff 字典（{序号: 字段dict}）。
    主域名 push2 偶发被限流，自动回退 push2his。"""
    base = ("?pn=%d&pz=%d&fid=f3&fs=%s&fields=%s" % (pn, pz, fs, fields))
    for host in ("https://push2.eastmoney.com/api/qt/clist/get",
                 "https://push2his.eastmoney.com/api/qt/clist/get"):
        j = _get_json(host + base, timeout=timeout, tries=tries)
        if j and (j.get("data") or {}).get("diff"):
            try:
                return (j.get("data") or {}).get("diff") or {}
            except Exception:
                return {}
    return {}


# --------------------------------------------------------------------------
# 全A实时快照（盘面宽度 / 个股榜单 / 个股资金流 的数据源）
# --------------------------------------------------------------------------
def get_spot(refresh=False, deadline_sec=55):
    """全A行情快照（东方财富 push2）。返回 {source, updated_at, count, rows:[...]}。

    deadline_sec 为整体超时上限：东财偶发限流/断连时，避免仪表盘长时间挂起；
    超时则中断抓取，返回已积累的数据（不足 100 条视为失败）。"""
    if not refresh and os.path.exists(SPOT_CACHE):
        try:
            cache = json.load(open(SPOT_CACHE, encoding="utf-8"))
            age = time.time() - cache.get("saved_at", 0)
            if cache.get("source") == "eastmoney" and age < SPOT_TTL:
                return cache
        except Exception:
            pass

    rows = []
    start_ts = time.time()
    for pn in range(1, SPOT_PAGES + 1):
        diff = _em_clist(_FS_ALL, _F_ALL, SPOT_PZ, pn, tries=2, timeout=10)
        if not diff:
            # 连续空页 = 东财已放弃，提前结束（不再逐页重试 56 页）
            break
        for k, s in diff.items():
            code = s.get("f12")
            price = _num(s.get("f2"))
            if not code or price is None:
                continue
            rows.append({
                "code": str(code),
                "name": s.get("f14") or "",
                "market": int(s["f13"]) if _num(s.get("f13")) is not None else None,
                "price": round(price / 100.0, 2),
                "pct_change": (_num(s.get("f3")) / 100.0) if _num(s.get("f3")) is not None else None,
                "amount": _num(s.get("f6")),                 # 成交额（元）
                "main_net": _num(s.get("f62")),              # 主力净流入（元）
                "turnover": (_num(s.get("f8")) / 100.0) if _num(s.get("f8")) is not None else None,
                "volume_ratio": (_num(s.get("f10")) / 100.0) if _num(s.get("f10")) is not None else None,
                "total_mv": (_num(s.get("f20")) / 1e8) if _num(s.get("f20")) is not None else None,   # 亿
                "float_mv": (_num(s.get("f21")) / 1e8) if _num(s.get("f21")) is not None else None,   # 亿
            })
        if time.time() - start_ts > deadline_sec:
            break
    if len(rows) > 100:
        out = {"source": "eastmoney", "updated_at": _now(), "count": len(rows), "rows": rows}
        try:
            json.dump({**out, "saved_at": time.time()}, open(SPOT_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return out
    return {"source": "none", "updated_at": _now(), "count": len(rows), "rows": rows,
            "note": "全A行情获取失败（东财接口不可用或超时），请稍后刷新"}


# --------------------------------------------------------------------------
# 大盘指数（腾讯财经，盘口级，无需东财）
# --------------------------------------------------------------------------
_IDX = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000300", "沪深300"), ("sh000688", "科创50"), ("sh000016", "上证50"),
    ("sh000905", "中证500"),
]


def get_indices():
    """大盘指数实时行情。返回 {source, updated_at, rows:[{code,name,price,pct_change}]}。"""
    q = ",".join(c for c, _ in _IDX)
    url = "http://qt.gtimg.cn/q=" + q
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "http://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
    except Exception:
        return {"source": "none", "updated_at": "", "rows": []}
    name_map = {c: n for c, n in _IDX}
    rows = []
    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 33:
                continue
            code = f[2].strip()
            name = f[1].strip() or name_map.get(code, code)
            price = _num(f[3])
            pct = _num(f[32])
            if not code or price is None:
                continue
            rows.append({"code": code, "name": name, "price": price, "pct_change": pct})
        except Exception:
            continue
    if rows:
        return {"source": "realtime", "updated_at": _now(), "rows": rows}
    return {"source": "none", "updated_at": "", "rows": []}


# --------------------------------------------------------------------------
# 个股主力资金流 Top（由全A快照按主力净流入排序派生）
# --------------------------------------------------------------------------
def get_fund_flow(top=50):
    """个股主力净流入榜（派生自全A快照）。返回 {source, rows:[...]}。"""
    spot = get_spot()
    rows0 = [r for r in spot.get("rows", []) if r.get("main_net") is not None]
    rows0.sort(key=lambda x: x["main_net"], reverse=True)
    rows = []
    for r in rows0[:top]:
        amount = r.get("amount")
        main_pct = (r["main_net"] / amount * 100.0) if (amount and amount > 0) else None
        rows.append({
            "code": r["code"], "name": r["name"], "price": r["price"],
            "pct_change": r.get("pct_change"), "main_net": round(r["main_net"], 0),
            "main_pct": main_pct,
        })
    return {"source": spot.get("source", "none"), "updated_at": spot.get("updated_at", ""), "rows": rows}


# --------------------------------------------------------------------------
# 板块排名（行业涨跌）
# --------------------------------------------------------------------------
def get_sector_rank():
    """行业板块涨跌排名（东财）。返回 {source, rows:[...]}。"""
    diff = _em_clist(_FS_BOARD, _F_BOARD, 500, 1, tries=2, timeout=10)
    rows = []
    for k, s in diff.items():
        name = s.get("f14")
        pct = _num(s.get("f3"))
        if name is None or pct is None:
            continue
        rows.append({
            "sector": str(name),
            "avg_pct": pct / 100.0,
            "up": _num(s.get("f104")),
            "down": _num(s.get("f105")),
            "limit_up": None,
            "leader": s.get("f128") or "",
            "leader_code": s.get("f140") or "",
            "leader_pct": (_num(s.get("f137")) / 100.0) if _num(s.get("f137")) is not None else None,
            "total_mv": (_num(s.get("f20")) / 1e8) if _num(s.get("f20")) is not None else None,
            "turnover": (_num(s.get("f8")) / 100.0) if _num(s.get("f8")) is not None else None,
            "count": None,
        })
    if rows:
        rows.sort(key=lambda x: (x["avg_pct"] if x["avg_pct"] is not None else -999), reverse=True)
        return {"source": "eastmoney", "updated_at": _now(), "rows": rows}
    return {"source": "none", "rows": [], "note": "板块排名获取失败（东财接口不可用）"}


# --------------------------------------------------------------------------
# 板块资金流向（主力净流入）
# --------------------------------------------------------------------------
def get_sector_capital_flow(top=50):
    """板块主力净流入排名（东财）。返回 {source, rows:[...]}。"""
    diff = _em_clist(_FS_BOARD, _F_BOARD_FLOW, 500, 1, tries=2, timeout=10)
    rows = []
    for k, s in diff.items():
        name = s.get("f14")
        net = _num(s.get("f62"))
        if name is None or net is None:
            continue
        rows.append({
            "sector": str(name),
            "main_net": round(net, 0),
            "main_pct": (_num(s.get("f184")) / 100.0) if _num(s.get("f184")) is not None else None,
            "pct_change": (_num(s.get("f3")) / 100.0) if _num(s.get("f3")) is not None else None,
        })
    if rows:
        rows.sort(key=lambda x: x["main_net"], reverse=True)
        return {"source": "eastmoney", "updated_at": _now(), "rows": rows[:top]}
    return {"source": "none", "rows": [], "note": "板块资金流获取失败（东财接口不可用）"}


# --------------------------------------------------------------------------
# 当日龙虎榜（东财 datacenter）
# --------------------------------------------------------------------------
def get_dragon_tiger(days=12):
    """当日龙虎榜（东财）。取最近上榜日的明细。返回 {source, date, rows:[...]}。"""
    today = datetime.datetime.now()
    start = (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    raw_filter = "(TRADE_DATE>='%s')(TRADE_DATE<='%s')" % (start, end)
    flt = urllib.parse.quote(raw_filter)
    cols = ("SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,"
            "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_DEAL_AMT,"
            "DEAL_NET_RATIO,TURNOVERRATE,FREE_MARKET_CAP,EXPLANATION,EXPLAIN,"
            "D1_CLOSE_ADJCHRATE,D2_CLOSE_ADJCHRATE,D5_CLOSE_ADJCHRATE,D10_CLOSE_ADJCHRATE")
    data = []
    for pn in range(1, 6):   # 最多翻 5 页（每页 1000）
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS"
               "&columns=" + urllib.parse.quote(cols) +
               "&pageSize=1000&pageNumber=%d&sortColumns=TRADE_DATE,BILLBOARD_NET_AMT"
               "&sortTypes=-1,-1&source=WEB&client=WEB&filter=" % pn + flt)
        j = _get_json(url, timeout=25)
        if not j:
            break
        batch = (j.get("result", {}) or {}).get("data") or []
        if not batch:
            break
        data.extend(batch)
        total = (j.get("result", {}) or {}).get("pages")
        if total and pn >= int(total):
            break

    if data:
        dates = sorted(set(str(r.get("TRADE_DATE", ""))[:10] for r in data), reverse=True)
        latest = dates[0]
        rows = []
        for r in data:
            if str(r.get("TRADE_DATE", ""))[:10] != latest:
                continue
            code = str(r.get("SECURITY_CODE") or "")
            if not code:
                continue
            after = {}
            for n, key in ((1, "D1_CLOSE_ADJCHRATE"), (2, "D2_CLOSE_ADJCHRATE"),
                           (5, "D5_CLOSE_ADJCHRATE"), (10, "D10_CLOSE_ADJCHRATE")):
                v = _num(r.get(key))
                after[n] = v
            rows.append({
                "code": code,
                "name": str(r.get("SECURITY_NAME_ABBR") or ""),
                "date": latest,
                "close": _num(r.get("CLOSE_PRICE")),
                "pct_change": _num(r.get("CHANGE_RATE")),
                "net": _num(r.get("BILLBOARD_NET_AMT")),
                "buy": _num(r.get("BILLBOARD_BUY_AMT")),
                "sell": _num(r.get("BILLBOARD_SELL_AMT")),
                "amount": _num(r.get("BILLBOARD_DEAL_AMT")),
                "net_pct": _num(r.get("DEAL_NET_RATIO")),
                "turnover": _num(r.get("TURNOVERRATE")),
                "float_mv": (_num(r.get("FREE_MARKET_CAP")) / 1e8) if _num(r.get("FREE_MARKET_CAP")) is not None else None,
                "reason": str(r.get("EXPLANATION") or ""),
                "interp": str(r.get("EXPLAIN") or ""),
                "after": after,
            })
        rows.sort(key=lambda x: (x["net"] if x["net"] is not None else -9e18), reverse=True)
        return {"source": "eastmoney", "date": latest, "rows": rows}

    # 降级：实时异动榜（满足龙虎榜风格条件的个股，由全A快照派生）
    spot = get_spot()
    rows0 = [r for r in spot.get("rows", []) if r.get("pct_change") is not None]
    out = []
    for r in rows0:
        pct = r["pct_change"]; to = r.get("turnover") or 0
        flag = None
        if pct >= 9.5:
            flag = "涨停"
        elif pct <= -9.5:
            flag = "跌停"
        elif pct >= 7 and to >= 10:
            flag = "大涨+高换手"
        elif to >= 20:
            flag = "超高换手"
        if flag:
            out.append({"code": r["code"], "name": r.get("name", ""), "date": "", "close": r.get("price"),
                        "pct_change": pct, "net": None, "buy": None, "sell": None, "amount": None,
                        "net_pct": None, "turnover": to, "float_mv": None,
                        "reason": flag + "（龙虎榜候选）", "interp": "", "after": {}})
    out.sort(key=lambda x: (x["pct_change"] or 0), reverse=True)
    return {"source": spot.get("source", "none"), "date": "", "rows": out,
            "note": "东财龙虎榜不可用，已降级为实时异动榜"}


# --------------------------------------------------------------------------
# 个股实时行情 / K线 / 分时（代码点击 -> 弹窗）
# --------------------------------------------------------------------------
def _full_code(code):
    """返回带交易所前缀的完整代码（幂等）：
    已带 sh/sz/bj 前缀则原样返回；否则按首位数字推断（6/9->sh, 4/8->bj, 其余->sz）。"""
    code = (code or "").strip().lower()
    if code.startswith(("sh", "sz", "bj")):
        return code
    if not code or not code[0].isdigit():
        return code
    if code[0] in "69":
        return "sh" + code
    if code[0] in "48":
        return "bj" + code
    return "sz" + code


def get_quote(code):
    """单只个股实时行情（腾讯财经）。返回 {source, row:{...}}。"""
    if not code or len(code) < 6:
        return {"source": "none", "row": None}
    full = _full_code(code)
    url = "http://qt.gtimg.cn/q=" + full
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "http://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
    except Exception:
        return {"source": "none", "row": None}
    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            body = line.split("=", 1)[1].strip().strip('"')
            f = body.split("~")
            if len(f) < 45:
                continue
            return {"source": "realtime", "row": {
                "code": f[2].strip(), "name": f[1].strip(),
                "price": _num(f[3]), "prev_close": _num(f[4]), "open": _num(f[5]),
                "high": _num(f[6]), "low": _num(f[7]),
                "volume": _num(f[8]), "amount": _num(f[9]),
                "pct_change": _num(f[32]), "amplitude": _num(f[33]),
                "turnover": _num(f[37]), "pe": _num(f[38]), "pb": _num(f[39]),
                "total_mv": (_num(f[45]) / 1e8) if _num(f[45]) else None,
                "float_mv": (_num(f[44]) / 1e8) if _num(f[44]) else None,
                "volume_ratio": _num(f[46]),
                "time": f[30].strip() if len(f) > 30 else "",
                "date": f[31].strip() if len(f) > 31 else "",
            }}
        except Exception:
            continue
    return {"source": "none", "row": None}


def get_kline(symbol, period="day", count=120):
    """个股历史 K 线（腾讯财经 qfq 前复权）。返回 {source, bars:[{date,open,close,high,low,volume}]}。"""
    full = _full_code(symbol)
    pmap = {"day": "day", "week": "week", "month": "month"}
    p = pmap.get(period, "day")
    param = full + "," + p + ",,," + str(count) + ",qfq"
    _KLINE_HOSTS = [
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    ]
    d = None
    for _h in _KLINE_HOSTS:
        d = _get_json(_h + "?param=" + param)
        if d:
            break
    if not d:
        return {"source": "none", "bars": []}
    try:
        s = d.get("data", {}).get(full, {})
        series = (s.get("qfq" + p) or s.get(p) or s.get("data") or [])
        out = []
        for r in series[-count:]:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            out.append({
                "date": str(r[0]), "open": _num(r[1]), "close": _num(r[2]),
                "high": _num(r[3]), "low": _num(r[4]), "volume": _num(r[5]),
            })
        return {"source": "realtime", "bars": out}
    except Exception:
        return {"source": "none", "bars": []}


def get_intraday(symbol):
    """当日分时走势（腾讯财经）。返回 {source, date, points:[{t, price}]}。"""
    full = _full_code(symbol)
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + full
    d = _get_json(url)
    if not d:
        return {"source": "none", "date": "", "points": []}
    try:
        inner = d.get("data", {}).get(full, {}).get("data", {})
        arr = inner.get("data", []) if isinstance(inner, dict) else []
        date = inner.get("date", "") if isinstance(inner, dict) else ""
        pts = []
        for it in arr:
            parts = it.split() if isinstance(it, str) else (it if isinstance(it, (list, tuple)) else None)
            if not parts or len(parts) < 2:
                continue
            t = str(parts[0])
            price = _num(parts[1])
            if price is None:
                continue
            if t.isdigit() and len(t) == 4:
                t = t[:2] + ":" + t[2:]
            pts.append({"t": t, "price": price})
        return {"source": "realtime", "date": date, "points": pts}
    except Exception:
        return {"source": "none", "date": "", "points": []}


if __name__ == "__main__":
    import pprint
    pprint.pprint(get_indices())
    pprint.pprint({k: len(v) if isinstance(v, list) else v for k, v in get_spot().items()})
    pprint.pprint(get_sector_rank()["rows"][:3])
    pprint.pprint(get_sector_capital_flow()["rows"][:3])
    pprint.pprint(get_dragon_tiger(12)["rows"][:3])
