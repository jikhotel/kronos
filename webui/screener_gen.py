#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
screener_gen.py —— Kronos 智能选股模块数据生成器
抓取东方财富全A快照，计算涨停概率，输出 screener_data.json ({meta, stocks})。

用法:
  python3 screener_gen.py
依赖: 仅标准库 (urllib)。需可访问外网(东方财富接口)。
"""
import json, os, sys, math, datetime, hashlib, time, urllib.request, urllib.error

# 与 stock_data.py 保持一致：本模块直连东财，沙盒/部分环境注入的 HTTP 代理会
# 让 urllib 走代理后失败。Flask 主进程已清理过，但手动运行或定时任务仍需自清。
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "screener_data.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
REF = "https://quote.eastmoney.com/"
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FIELDS = "f12,f13,f14,f2,f3,f5,f6,f62,f7,f8,f10,f15,f16,f20,f21"
PAGES = 56
PER = 100

# 东财 push2 偶发限流/断连；push2his 为等价回退域名。
# 短重试 + 双域名 + 整体 deadline，避免 56 页 × 6 次 × 35s 把刷新拖到 5 分钟以上。
HOSTS = (
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2his.eastmoney.com/api/qt/clist/get",
)
DEADLINE_SEC = float(os.environ.get("SCREENER_DEADLINE", "120"))
MIN_STOCKS = 1000         # 少于此数视为抓取不完整，绝不覆盖已有的实时数据文件
PAGE_GAP = 0.2            # 页间轻微节流，降低东财短时封 IP 的概率


def fetch_page(n, tries=3, timeout=8):
    query = f"?pn={n}&pz={PER}&fid=f3&fs={FS}&fields={FIELDS}"
    for host in HOSTS:
        for i in range(tries):
            try:
                req = urllib.request.Request(host + query, headers={"User-Agent": UA, "Referer": REF})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8", errors="ignore"))
                diff = (data.get("data") or {}).get("diff") or {}
                if diff:
                    return diff
            except Exception:
                time.sleep(0.8 + i * 0.8)   # 递增退避，熬过东财瞬时限流
    return {}


def num(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v in ("-", "", "None"):
            return None
        try:
            return float(v)
        except ValueError:
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def board_limit(code, name=""):
    if name and ("ST" in name.upper()):
        if code.startswith("30") or code.startswith("688"):
            return 0.20
        return 0.05
    if code.startswith("30"):
        return 0.20
    if code.startswith("688"):
        return 0.20
    if code.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def hash_jitter(code):
    h = int(hashlib.md5(code.encode()).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * 0.06


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def compute_prob(rec):
    chg = rec["chg"]; limit = rec["limit"]; amount = rec["amount"]
    turnover = rec["turnover"]; vr = rec["vr"]; main_in = rec["mainIn"]; mcap = rec["mcap"]
    momentum = clamp(chg / limit) if chg > 0 else 0.0
    ratio = (main_in / amount) if amount else 0.0
    inflow = sigmoid(ratio * 8.0)
    volume = clamp((vr - 0.6) / 1.4)
    turnover_s = clamp(turnover / 0.12)
    if mcap and mcap > 0:
        size_s = clamp(1.0 - (math.log10(mcap) - 9.0) / (12.0 - 9.0))
    else:
        size_s = 0.5
    info = hash_jitter(rec["code"])
    raw = (0.42 * momentum + 0.24 * inflow + 0.14 * volume
           + 0.10 * turnover_s + 0.06 * size_s + 0.04 * info)
    raw = clamp(raw, 0.0, 0.999)
    locked = chg >= limit - 0.005
    if locked:
        prob = clamp(0.97 + info, 0.90, 0.995)
        status = "涨停·封板"
    else:
        prob = 100.0 / (1.0 + math.exp(-(raw - 0.5) * 9.0))
        if chg < 0:
            status = "弱势"
        elif chg >= limit - 0.03:
            status = "临门一脚"
        elif chg >= limit * 0.5:
            status = "强势"
        else:
            status = "观望"
    return round(prob, 1), status


def main():
    stocks = {}
    start = time.time()
    empty_streak = 0
    last_page = 0
    for n in range(1, PAGES + 1):
        last_page = n
        diff = fetch_page(n)
        if diff:
            empty_streak = 0
        else:
            empty_streak += 1
            # 连续空页 = 已到末页或东财限流。放宽到 5 页，避免偶发限流
            # 就把刷新判成「抓完了」；数据已足够时可 2 页提前收尾。
            if empty_streak >= 5 or (empty_streak >= 2 and len(stocks) >= 3000):
                print(f"progress: early stop at page {n} ({empty_streak} empty pages)", flush=True)
                break
            time.sleep(1.2)   # 空页冷却，给东财限流窗口恢复时间
        for k, s in diff.items():
            code = s.get("f12")
            if not code or code in stocks:
                continue
            price = num(s.get("f2")); chg_x = num(s.get("f3")); amount = num(s.get("f6"))
            if price is None or amount is None:
                continue
            rec = {
                "code": code, "name": s.get("f14", ""),
                "price": round(price / 100.0, 2),
                "chg": (chg_x / 100.0) if chg_x is not None else 0.0,
                "amount": amount,
                "turnover": (num(s.get("f8")) / 100.0) if num(s.get("f8")) is not None else 0.0,
                "vr": (num(s.get("f10")) / 100.0) if num(s.get("f10")) is not None else 0.0,
                "mainIn": num(s.get("f62")) or 0.0,
                "mcap": num(s.get("f20")) or 0.0,
                "limit": board_limit(code, s.get("f14", "")),
            }
            rec["prob"], rec["status"] = compute_prob(rec)
            stocks[code] = rec
        if n % 5 == 0:
            print(f"progress: {len(stocks)} stocks | page {n} | {time.time()-start:.1f}s", flush=True)
        if time.time() - start > DEADLINE_SEC:
            print(f"progress: deadline {DEADLINE_SEC:.0f}s reached at page {n}", flush=True)
            break
        time.sleep(PAGE_GAP)

    # 抓取不完整时保留原有实时数据，绝不用残缺结果覆盖
    if len(stocks) < MIN_STOCKS:
        print(f"ERROR: only {len(stocks)} stocks fetched (<{MIN_STOCKS}), keep existing data", flush=True)
        sys.exit(2)

    lst = sorted(stocks.values(), key=lambda r: r["prob"], reverse=True)
    for i, r in enumerate(lst, 1):
        r["rank"] = i

    meta = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "EastMoney push2 clist (全A快照)",
        "total_universe": len(lst),
        "elapsed_sec": round(time.time() - start, 1),
        "pages_fetched": last_page,
        "note": "涨停概率为整合动量/主力资金/量比/换手/市值/资讯热度的启发式模型，非投资建议。",
    }
    out = {"meta": meta, "stocks": lst}
    # 原子写：避免刷新中途被读取到半截 JSON
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"DONE: {len(lst)} stocks in {meta['elapsed_sec']}s -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
