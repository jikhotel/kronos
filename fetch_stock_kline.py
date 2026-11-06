#!/usr/bin/env python3
"""
抓取 A 股历史 K 线, 输出为 Kronos WebUI 可直接读取的 CSV。

数据源: 腾讯财经(支持前复权)。单次请求上限 640 根, 脚本按 2 年一段自动翻页。
历史数据默认取前复权(qfq), 避免分红送股造成的价格断层。

用法:
    python fetch_stock_kline.py 300505                 # 川金诺 日线 前复权(默认)
    python fetch_stock_kline.py 600519 --fq hfq        # 后复权
    python fetch_stock_kline.py 000001 --fq none       # 不复权
    python fetch_stock_kline.py 300505 --freq week     # 周线
    python fetch_stock_kline.py 300505 --freq m30      # 30分钟线(仅近期)

--freq 可选: day(默认) week month m1 m5 m15 m30 m60
--fq   可选: qfq(默认,前复权) hfq(后复权) none(不复权)

输出列: timestamps,open,high,low,close,volume,amount,turnover
(成交额/换手率默认 0, 由东方财富接口补齐; 失败则按 成交量×均价 估算)
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, timedelta

import requests

API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = {"User-Agent": "Mozilla/5.0"}
PAGE_LIMIT = 640          # 腾讯单次返回上限, 触顶即说明被截断

FREQ_MAP = {"day": "day", "week": "week", "month": "month",
            "m1": "m1", "m5": "m5", "m15": "m15", "m30": "m30", "m60": "m60"}
FREQ_CN = {"day": "日线", "week": "周线", "month": "月线",
           "m1": "1分钟", "m5": "5分钟", "m15": "15分钟", "m30": "30分钟", "m60": "60分钟"}
FQ_CN = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}

# 东方财富历史 K 线(补齐腾讯不提供的 成交额 / 换手率)
EM_API = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_KLT = {"day": "101", "week": "102", "month": "103",
          "m1": "1", "m5": "5", "m15": "15", "m30": "30", "m60": "60"}
EM_FQT = {"qfq": "1", "hfq": "2", "none": "0"}   # 不复权=0
EM_MARKET = {"sh": "1", "sz": "0", "bj": "0"}      # 港股走近似估算, 不走东财


def symbol_of(code: str) -> str:
    """6 位代码 -> 腾讯代码。沪市 6/9 开头为 sh, 其余(深市/创业板/北交所)为 sz/bj"""
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def resolve_symbol(code: str) -> str:
    """
    将用户输入解析为腾讯代码(带市场前缀)。
      - 已带前缀: hk00700 / sh600519 / sz000001 / bj8xxxxx -> 原样返回
      - 5 位数字(不足补零): 视为港股 -> hk00700
      - 6 位数字: 走 A 股规则
    """
    code = code.strip().lower()
    if code.startswith(("hk", "sh", "sz", "bj")):
        return code
    digits = code
    if not digits.isdigit():
        # 容错: 去掉非数字(用户可能粘贴了 'HK00700')
        digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) == 5:
        return f"hk{digits:0>5}"
    if len(digits) == 4:  # 港股 4 位也补零到 5
        return f"hk{digits:0>5}"
    return symbol_of(digits)


def _fetch_tencent_window(symbol: str, freq: str, fq: str, start: date, end: date):
    """腾讯窗口拉取, 带重试(应对 CDN/WAF 偶发 501)。成功返回 rows, 失败返回 None。"""
    param = (f"{symbol},{freq},{start.isoformat()},{end.isoformat()},{PAGE_LIMIT},{fq}")
    last_err = "unknown"
    for attempt in range(3):
        try:
            r = requests.get(API, params={"param": param}, timeout=30, headers=UA)
            if r.status_code == 200:
                data = r.json().get("data")
                if isinstance(data, dict) and symbol in data:
                    node = data[symbol]
                    # 前/后复权结果在 qfqday / hfqday 键下, 不复权在 day / <freq> 键下
                    for key in (f"{fq}{freq}", freq, f"{fq}day", "day"):
                        if node.get(key):
                            return node[key]
                    return []
                return []  # 200 但无数据
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)[:80]
        time.sleep(2 + attempt * 3)
    print(f"[warn] 腾讯接口失败({last_err}), 切换东方财富兜底: {symbol} {freq}")
    return None


def _fetch_em_window(symbol: str, freq: str, fq: str, start: date, end: date):
    """东方财富兜底 K 线, 输出对齐腾讯行格式 [日期,开,收,高,低,成交量(手)]。
    一次返回区间内全部数据(不分页), 港股不支持返回空。"""
    secid = _eastmoney_secid(symbol)
    if not secid:
        return []
    try:
        r = requests.get(EM_API, params={
            "secid": secid, "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56",
            "klt": EM_KLT.get(freq, "101"), "fqt": EM_FQT.get(fq, "1"),
            "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "lmt": "1000000",
        }, timeout=30, headers=UA)
        r.raise_for_status()
        data = r.json().get("data")
        if not data or not data.get("klines"):
            return []
        rows = []
        for line in data["klines"]:
            p = line.split(",")
            if len(p) >= 6:
                rows.append([p[0], p[1], p[2], p[3], p[4], p[5]])
        return rows
    except Exception as e:
        print(f"[warn] 东方财富兜底也失败: {e}")
        return []


def fetch_window(symbol: str, freq: str, fq: str, start: date, end: date):
    """拉取单个时间窗口: 腾讯(重试) -> 东方财富兜底。"""
    rows = _fetch_tencent_window(symbol, freq, fq, start, end)
    if rows is not None:
        return rows
    return _fetch_em_window(symbol, freq, fq, start, end)


def fetch_range(symbol: str, freq: str, fq: str, start: date, end: date, depth: int = 0):
    """
    拉取 [start, end] 区间的 K 线。
    腾讯源: 若单次返回达到 PAGE_LIMIT, 说明被服务端截断, 则对半递归再分;
    东财兜底: 一次给全区间数据, 无需递归。
    """
    rows = _fetch_tencent_window(symbol, freq, fq, start, end)
    if rows is None:
        return _fetch_em_window(symbol, freq, fq, start, end)
    if len(rows) >= PAGE_LIMIT and start < end and depth < 8:
        mid = start + (end - start) // 2
        if start < mid < end:
            left = fetch_range(symbol, freq, fq, start, mid, depth + 1)
            right = fetch_range(symbol, freq, fq, mid + timedelta(days=1), end, depth + 1)
            return left + right
    return rows


def fetch_all(symbol: str, freq: str, fq: str, start: date, end: date):
    """全量拉取, 按日期去重排序"""
    rows, seen = [], set()
    for item in fetch_range(symbol, freq, fq, start, end):
        day = item[0]
        if day not in seen:
            seen.add(day)
            rows.append(item)
    rows.sort(key=lambda x: x[0])
    return rows


def _eastmoney_secid(symbol: str) -> str | None:
    """腾讯代码 -> 东方财富 secid; 港股等不支持时返回 None(改用估算)"""
    mkt = symbol[:2]
    code = symbol[2:]
    if mkt in EM_MARKET:
        return f"{EM_MARKET[mkt]}.{code}"
    return None


def fetch_em_indicators(symbol: str, freq: str, fq: str) -> dict:
    """抓取东方财富历史 K 线, 返回 {datetime_str: (成交额元, 换手率%)}。
    用于补全省花钱线接口不提供的成交额/换手率; 失败返回空 dict。"""
    secid = _eastmoney_secid(symbol)
    if not secid:
        return {}
    klt = EM_KLT.get(freq, "101")
    fqt = EM_FQT.get(fq, "1")
    try:
        r = requests.get(EM_API, params={
            "secid": secid,
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt, "fqt": fqt, "beg": "0", "end": "20500101"
        }, timeout=30, headers=UA)
        r.raise_for_status()
        data = r.json().get("data")
        if not data or not data.get("klines"):
            return {}
        out = {}
        for line in data["klines"]:
            parts = line.split(",")
            if len(parts) < 11:
                continue
            dt, amount, turnover = parts[0], parts[6], parts[10]
            try:
                out[dt] = (float(amount), float(turnover))
            except ValueError:
                out[dt] = (0.0, 0.0)
        return out
    except Exception as e:
        print(f"[warn] 东财成交额抓取失败, 将使用估算: {e}")
        return {}


def fetch_and_save(code: str, freq: str = "day", fq: str = "qfq",
                   start: str = "1990-01-01", outdir: str | None = None,
                   end: date | None = None) -> str:
    """
    解析代码 -> 拉取 K 线 -> 保存为 Kronos 兼容 CSV, 返回文件路径。
    可被命令行与 Web API 共用。

    腾讯返回: [日期, 开, 收, 高, 低, 成交量(手)]
    Kronos 需要: timestamps,open,high,low,close,volume,amount
    """
    symbol = resolve_symbol(code)
    frequency = FREQ_MAP.get(freq, "day")
    intraday = frequency.startswith("m") and frequency != "month"
    end_date = end or date.today()

    if intraday:
        print("[提示] 分钟线数据源仅保留近期数据, 将只取最新一段。")
        raw = fetch_window(symbol, frequency, fq, end_date - timedelta(days=120), end_date)
    else:
        raw = fetch_all(symbol, frequency, fq, date.fromisoformat(start), end_date)

    if not raw:
        raise SystemExit(f"未取到数据, 请检查代码 {code} (解析为 {symbol}) 是否正确")

    # 补充真实成交额/换手率(腾讯日线接口不提供, 改由东方财富补齐; 失败则按成交量×均价估算)
    em_ind = fetch_em_indicators(symbol, frequency, fq)

    out_rows = []
    for item in raw:
        day = item[0]
        o, c, h, l = float(item[1]), float(item[2]), float(item[3]), float(item[4])
        vol_lots = float(item[5]) if len(item) > 5 and item[5] else 0.0
        volume = int(vol_lots * 100)            # 手 -> 股
        em_row = em_ind.get(day)
        if em_row and em_row[0] > 0:
            amount, turnover = em_row[0], em_row[1]
        else:
            # 估算: 成交额 ≈ 成交量(股) × 成交均价; 换手率缺省 0
            amount = volume * (h + l + c) / 3
            turnover = 0.0
        out_rows.append([day, o, h, l, c, volume, amount, turnover])

    outdir = outdir or "/Users/ccb/dev/Kronos/data"
    os.makedirs(outdir, exist_ok=True)
    name_cn = FREQ_CN.get(freq, "日线")
    fq_cn = FQ_CN.get(fq, "前复权")
    code_disp = symbol[2:] if symbol[:2] in ("hk", "sh", "sz", "bj") else symbol
    path = os.path.join(outdir, f"{code_disp}_{symbol}_{name_cn}_{fq_cn}.csv")

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamps", "open", "high", "low", "close", "volume", "amount", "turnover"])
        w.writerows(out_rows)

    print(f"代码 : {code} ({symbol})")
    print(f"频率 : {name_cn} / {fq_cn}")
    print(f"行数 : {len(out_rows):,}")
    print(f"区间 : {out_rows[0][0]}  →  {out_rows[-1][0]}")
    print(f"输出 : {path}")
    if len(out_rows) < 520:
        print(f"\n[警告] 仅 {len(out_rows)} 行, Kronos 时间窗需 520 点(400回看+120预测), 数据不足。")
    else:
        print(f"可选窗口起点: {len(out_rows) - 520:,} 个")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", help="股票代码: 6位A股(300505) / 5位港股(00700) / 或带前缀(hk00700,sh600519)")
    ap.add_argument("--freq", default="day", choices=list(FREQ_MAP))
    ap.add_argument("--fq", default="qfq", choices=["qfq", "hfq", "none"])
    ap.add_argument("--start", default="1990-01-01", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--out", help="输出目录, 默认 Kronos 的 data 目录")
    args = ap.parse_args()

    fetch_and_save(args.code, args.freq, args.fq, args.start, args.out)


if __name__ == "__main__":
    sys.exit(main())
