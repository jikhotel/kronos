# -*- coding: utf-8 -*-
"""Kronos · 每日推荐策略引擎
=====================================
两套定时策略，各自 10 只，滚动验证成功率：

  上午场 morning  —— 10:00 依据开盘 30 分钟表现选股并给出买入参考价，
                       当日收盘用收盘价验证收益（收盘价 vs 参考价）。
  尾盘场 afternoon —— 14:35 依据全天走势选股并给出买入参考价，
                       次日开盘用开盘价验证收益（次日开盘价 vs 参考价）。

纯标准库 + 复用 stock_data 的数据层，零新增依赖。所有记录落盘到
cache/daily_picks.json，页面刷新/服务重启都不丢历史。
"""
import os
import json
import math
import time
import fcntl
import datetime
import threading
import urllib.request

import stock_data as sd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
STORE = os.path.join(CACHE_DIR, "daily_picks.json")
LOCK = os.path.join(CACHE_DIR, "daily_picks.lock")

N_PICKS = 10
MORNING_HHMM = "10:00"
AFTERNOON_HHMM = "14:35"

# 选股过滤阈值（可按市场微调）
MIN_AMOUNT = 3e7        # 成交额 >= 3000 万，保证流动性
MAX_PCT = 9.5           # 排除接近涨停（买不进 / 追高风险）
MIN_PCT_M = 0.0         # 上午场最低涨幅
MIN_PCT_A = 0.5         # 尾盘场最低涨幅
MAX_MV = 3000.0         # 总市值上限（亿）


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _load():
    if not os.path.exists(STORE):
        return {"days": {}, "updated_at": ""}
    try:
        return json.load(open(STORE, encoding="utf-8"))
    except Exception:
        return {"days": {}, "updated_at": ""}


def _save(data):
    data["updated_at"] = _now()
    tmp = STORE + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)


class _FileLock:
    def __init__(self, path):
        self.f = open(path, "a+")

    def __enter__(self):
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        self.f.close()


# --------------------------------------------------------------------------
# 批量实时行情（腾讯 q= 一次多只）：拿现价 / 今开 / 昨收 / 最高 / 最低
# --------------------------------------------------------------------------
def _batch_quotes(codes):
    out = {}
    codes = list(dict.fromkeys(codes))
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        q = ",".join(sd._full_code(c) for c in chunk)
        url = "http://qt.gtimg.cn/q=" + q
        try:
            req = urllib.request.Request(url, headers={"User-Agent": sd.UA, "Referer": "http://gu.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", errors="ignore")
        except Exception:
            continue
        for line in raw.split(";"):
            if '="' not in line:
                continue
            try:
                f = line.split('"')[1].split("~")
                if len(f) < 40:
                    continue
                code = f[2].strip()
                price = sd._num(f[3])
                prev = sd._num(f[4])
                openp = sd._num(f[5])
                high = sd._num(f[33]) if len(f) > 33 else None
                low = sd._num(f[34]) if len(f) > 34 else None
                # f[33]/f[34] 在部分返回里是最高/最低；用现价兜底校验
                if high is None or (price and high < price):
                    high = price
                if low is None or (price and low > price):
                    low = price
                out[code] = {"price": price, "prev": prev, "open": openp,
                             "high": high, "low": low}
            except Exception:
                continue
    return out


def _gap(openp, prev):
    """开盘跳空幅度 %"""
    if not openp or not prev:
        return None
    return (openp - prev) / prev * 100.0


# --------------------------------------------------------------------------
# 打分：动量 / 主力资金 / 量能 / 开盘质量 / 抗跌位置
# --------------------------------------------------------------------------
def _score(rec, q, slot):
    chg = rec.get("pct_change") or 0.0
    vr = rec.get("volume_ratio") or 0.0
    to = rec.get("turnover") or 0.0
    amount = rec.get("amount") or 0.0
    main_net = rec.get("main_net") or 0.0
    mcap = rec.get("total_mv") or 0.0
    main_ratio = main_net / amount if amount > 0 else 0.0

    # 当前价在当日振幅中的位置（0=最低 1=最高），越高说明越抗跌/强势
    pos = 0.5
    price, high, low = (q or {}).get("price"), (q or {}).get("high"), (q or {}).get("low")
    if price and high and low and high > low:
        pos = (price - low) / (high - low)

    # 开盘质量：上午场看重「温和高开 0.5~4% 且回落有承接」；尾盘场权重降低
    gap = _gap((q or {}).get("open"), (q or {}).get("prev"))
    if gap is None:
        gap_q = 0.4
    elif 0.5 <= gap <= 4.0:
        gap_q = 1.0
    elif 0.0 <= gap < 0.5:
        gap_q = 0.7
    elif -1.0 <= gap < 0:
        gap_q = 0.5
    else:
        gap_q = 0.25

    def norm(x, lo, hi):
        return max(0.0, min(1.0, (x - lo) / (hi - lo)))

    mom = norm(chg, 0, 8)
    has_fund = rec.get("main_net") is not None
    fund = norm(main_ratio, -0.05, 0.20) if has_fund else None
    vol = norm(vr, 0.5, 3.0)
    turn = norm(to, 0.5, 12.0)
    size = 1.0 - 0.5 * norm(mcap, 50, 3000)   # 中小盘略加分

    if slot == "morning":
        parts = [(0.30, mom), (0.24, fund), (0.16, vol), (0.12, pos),
                 (0.12, gap_q), (0.06, turn), (0.04, size)]
    else:  # afternoon：更看全天资金与收盘强度，弱化开盘
        parts = [(0.30, mom), (0.28, fund), (0.16, vol), (0.18, pos),
                 (0.04, gap_q), (0.04, size)]
    usable = [(w, v) for w, v in parts if v is not None]
    total_w = sum(w for w, _ in usable)
    s = sum(w * v for w, v in usable) / total_w if total_w else 0.0
    return round(s * 100, 2)


def _reason(rec, q, slot):
    bits = []
    if rec.get("pct_change") and rec["pct_change"] > 0:
        bits.append("涨%.1f%%" % rec["pct_change"])
    mr = (rec.get("main_net") or 0) / (rec.get("amount") or 1) * 100
    if mr > 3:
        bits.append("主力净流入占比%.0f%%" % mr)
    elif rec.get("main_net") and rec["main_net"] > 0:
        bits.append("主力净流入%.0f万" % (rec["main_net"] / 1e4))
    if rec.get("volume_ratio") and rec["volume_ratio"] >= 1.5:
        bits.append("量比%.1f" % rec["volume_ratio"])
    if slot == "morning":
        g = _gap((q or {}).get("open"), (q or {}).get("prev"))
        if g is not None:
            bits.append("开盘%+.1f%%" % g)
    return "，".join(bits) if bits else "综合动量与资金评分入选"


def _tencent_fullmarket():
    """东财限流时的兜底：用选股快照里的全A代码表 + 腾讯批量报价重建实时行情。
    腾讯 qt.gtimg.cn 支持一次 80+ 只，含现价/涨幅/换手/量比，稳定不限流。"""
    universe_file = os.path.join(HERE, "screener_data.json")
    if not os.path.exists(universe_file):
        return []
    codes = [x["code"] for x in json.load(open(universe_file, encoding="utf-8")).get("stocks", [])]
    rows = []
    for i in range(0, len(codes), 70):
        chunk = codes[i:i + 70]
        q = ",".join(sd._full_code(c) for c in chunk)
        url = "http://qt.gtimg.cn/q=" + q
        try:
            req = urllib.request.Request(url, headers={"User-Agent": sd.UA, "Referer": "http://gu.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", errors="ignore")
        except Exception:
            continue
        for line in raw.split(";"):
            if '="' not in line:
                continue
            try:
                f = line.split('"')[1].split("~")
                if len(f) < 45:
                    continue
                price = sd._num(f[3])
                if not price or price <= 0:
                    continue
                rows.append({
                    "code": f[2].strip(), "name": f[1].strip(), "price": price,
                    "pct_change": sd._num(f[32]),
                    "amount": sd._num(f[37]) * 10000 if sd._num(f[37]) else None,  # 万元->元
                    "main_net": None,
                    "turnover": sd._num(f[38]),
                    "volume_ratio": sd._num(f[49]) if len(f) > 49 else None,
                    "total_mv": sd._num(f[45]) if len(f) > 45 else None,
                })
            except Exception:
                continue
    return rows


def _candidates(slot):
    spot = sd.get_spot(refresh=True, deadline_sec=45)
    rows = spot.get("rows", [])
    source = spot.get("source", "")
    # 完整度不足（限流/失败）时，按优先级回退：完整缓存 -> 腾讯全市场兜底
    if len(rows) < 2000:
        try:
            cached = json.load(open(sd.SPOT_CACHE, encoding="utf-8"))
            if cached.get("source") == "eastmoney" and len(cached.get("rows", [])) >= 2000:
                rows = cached["rows"]
                source = "eastmoney-cache"
        except Exception:
            pass
    if len(rows) < 2000:
        rows = _tencent_fullmarket()
        source = "tencent"
    if len(rows) < 100:
        raise RuntimeError("全A快照获取失败（仅 %d 只）" % len(rows))
    spot = {"source": source, "rows": rows}
    min_pct = MIN_PCT_M if slot == "morning" else MIN_PCT_A
    cand = []
    for r in rows:
        name = r.get("name") or ""
        code = r.get("code") or ""
        if "ST" in name.upper() or "退" in name or name.startswith("N"):
            continue
        if code.startswith(("4", "8", "9")):     # 排除北交所/京市
            continue
        pct = r.get("pct_change")
        amt = r.get("amount") or 0
        mv = r.get("total_mv") or 0
        if pct is None or pct < min_pct or pct > MAX_PCT:
            continue
        if amt < MIN_AMOUNT or (mv and mv > MAX_MV):
            continue
        if slot == "morning" and (r.get("volume_ratio") or 0) < 1.2:
            continue
        if slot == "afternoon" and (r.get("volume_ratio") or 0) < 1.0:
            continue
        cand.append(r)
    return cand, spot.get("updated_at", "")


def generate(slot, date=None, force=False):
    """生成某日某场的 10 只推荐。返回该场记录 dict。"""
    date = date or _today()
    with _FileLock(LOCK):
        data = _load()
        day = data["days"].setdefault(date, {})
        if slot in day and day[slot].get("picks") and not force:
            return day[slot]

        cand, spot_at = _candidates(slot)
        if len(cand) < N_PICKS:
            raise RuntimeError("候选池不足（%d 只），放弃生成" % len(cand))
        codes = [c["code"] for c in cand]
        quotes = _batch_quotes(codes)

        scored = []
        for r in cand:
            q = quotes.get(r["code"])
            if not q or not q.get("price"):
                continue
            price = q["price"]
            # 开盘跳空异常过滤：昨收/复权错位会产生 -10% 这种假信号，剔除
            g = _gap(q.get("open"), q.get("prev"))
            if g is not None and not (-5.0 <= g <= 9.5):
                continue
            scored.append({
                "code": r["code"], "name": r["name"],
                "ref_price": price,                       # 推荐参考买入价
                "pct_change": round(r.get("pct_change") or 0, 2),
                "volume_ratio": round(r.get("volume_ratio") or 0, 2),
                "turnover": round(r.get("turnover") or 0, 2),
                "main_net": round(r.get("main_net") or 0, 0),
                "amount": round(r.get("amount") or 0, 0),
                "total_mv": round(r.get("total_mv") or 0, 2),
                "open": q.get("open"), "prev": q.get("prev"),
                "high": q.get("high"), "low": q.get("low"),
                "gap": round(_gap(q.get("open"), q.get("prev")) or 0, 2),
                "score": _score(r, q, slot),
                "reason": _reason(r, q, slot),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        picks = scored[:N_PICKS]
        for i, p in enumerate(picks, 1):
            p["rank"] = i

        day[slot] = {
            "slot": slot, "date": date,
            "generated_at": _now(), "spot_at": spot_at,
            "picks": picks,
            "verify": None,           # 验证后填充
        }
        _save(data)
        return day[slot]


def verify(slot, date=None, force=False):
    """验证某日某场成功率。morning 用当日收盘价，afternoon 用次日开盘价。"""
    date = date or _today()
    with _FileLock(LOCK):
        data = _load()
        day = data["days"].get(date, {})
        rec = day.get(slot)
        if not rec or not rec.get("picks"):
            return None
        if rec.get("verify") and not force:
            return rec["verify"]

        codes = [p["code"] for p in rec["picks"]]
        quotes = _batch_quotes(codes)
        details = []
        for p in rec["picks"]:
            q = quotes.get(p["code"], {})
            if slot == "morning":
                exit_price = q.get("price")          # 收盘后=当日收盘价
                exit_kind = "收盘价"
            else:
                exit_price = q.get("open")           # 次日开盘价
                exit_kind = "次日开盘"
            ref = p["ref_price"]
            if exit_price and ref:
                pnl = (exit_price - ref) / ref * 100.0
                win = exit_price > ref
            else:
                pnl, win, exit_price = None, False, None
            details.append({
                "code": p["code"], "name": p["name"],
                "ref_price": ref, "exit_price": exit_price,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "win": win,
            })
        wins = sum(1 for d in details if d["win"])
        total = len(details)
        rec["verify"] = {
            "verified_at": _now(), "exit_kind": exit_kind,
            "wins": wins, "total": total,
            "rate": round(wins / total * 100, 1) if total else 0.0,
            "avg_pnl": round(sum(d["pnl"] for d in details if d["pnl"] is not None) /
                             max(1, sum(1 for d in details if d["pnl"] is not None)), 2),
            "details": details,
        }
        _save(data)
        return rec["verify"]


def _prev_trading_day(d):
    """粗略取前一自然日（跳过周末）；节假日不影响「是否有记录」判断。"""
    dt = datetime.datetime.strptime(d, "%Y-%m-%d") - datetime.timedelta(days=1)
    while dt.weekday() >= 5:
        dt -= datetime.timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def catch_up():
    """页面加载 / 定时线程调用：按当前时间补做生成与验证。返回动作列表。"""
    actions = []
    now = datetime.datetime.now()
    hm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:
        return actions            # 周末不生成（历史仍可查看/验证）
    data = _load()
    days = data["days"]

    # 上午场：到点生成；收盘后验证
    if hm >= MORNING_HHMM:
        try:
            if today not in days or "morning" not in days[today]:
                generate("morning", today)
                actions.append("生成今日上午场 10 只")
        except Exception as e:
            actions.append("上午场生成失败: %s" % e)
    if hm >= "15:05":
        if today in days and "morning" in days[today] and not days[today]["morning"].get("verify"):
            try:
                verify("morning", today)
                actions.append("验证上午场收盘成功率")
            except Exception as e:
                actions.append("上午场验证失败: %s" % e)

    # 尾盘场：到点生成；次日开盘后验证前一日
    if hm >= AFTERNOON_HHMM:
        try:
            if today not in days or "afternoon" not in days[today]:
                generate("afternoon", today)
                actions.append("生成今日尾盘场 10 只")
        except Exception as e:
            actions.append("尾盘场生成失败: %s" % e)
    if hm >= "09:35":
        pd = _prev_trading_day(today)
        if pd in days and "afternoon" in days[pd] and not days[pd]["afternoon"].get("verify"):
            try:
                verify("afternoon", pd)
                actions.append("验证前一日尾盘场次日开盘成功率")
            except Exception as e:
                actions.append("尾盘场验证失败: %s" % e)
    return actions


def state():
    """给页面用的完整状态：最近 30 天记录 + 累计成功率统计。"""
    data = _load()
    days = data["days"]
    ordered = sorted(days.keys(), reverse=True)[:30]
    records = []
    stat = {"morning": {"n": 0, "wins": 0}, "afternoon": {"n": 0, "wins": 0}}
    for d in ordered:
        item = {"date": d, "morning": None, "afternoon": None}
        for slot in ("morning", "afternoon"):
            rec = days[d].get(slot)
            if not rec:
                continue
            v = rec.get("verify")
            view = {
                "slot": slot, "generated_at": rec.get("generated_at"),
                "picks": rec.get("picks", []), "verify": v,
            }
            item[slot] = view
            if v:
                stat[slot]["n"] += 1
                stat[slot]["wins"] += v["wins"]
                stat[slot]["trades"] = stat[slot].get("trades", 0) + v["total"]
        records.append(item)
    summary = {}
    for slot in ("morning", "afternoon"):
        n = stat[slot]["n"]
        trades = stat[slot].get("trades", 0)
        summary[slot] = {
            "sessions": n,
            "trades": trades,
            "wins": stat[slot]["wins"],
            "rate": round(stat[slot]["wins"] / trades * 100, 1) if trades else None,
        }
    return {"records": records, "summary": summary, "today": _today(),
            "updated_at": data.get("updated_at", "")}


def start_scheduler():
    """后台守护线程：每 60 秒按时间触发一次补做。"""
    def loop():
        while True:
            try:
                catch_up()
            except Exception:
                pass
            time.sleep(60)
    t = threading.Thread(target=loop, daemon=True, name="daily-pick-scheduler")
    t.start()
    return t
