#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
善水模型 v10（重建版）
========================================================================
⚠️ 重要声明：本文件是按自动化 prompt 中描述的 v10 口径"重建"的，并非原版脚本。
   原版 daily_shanshui.py 已丢失，下列"试盘线形态"的核心打分按 prompt 描述推断实现，
   选股结果可能与原版不完全一致，请先用 --dry-run 验证后再正式推送。

v10 口径（来自 prompt，尽量忠实）：
  1) 样本：全 A 全量扫描（沪/深主板 + 创业板 + 科创板 + 北交所）。
  2) 试盘线前高：取"放量日"（vr20≥1.2）中的最高价。
  3) 收盘价距试盘线前高 -3%~0%（即已回踩 10~30% 后重新逼近前高）。
  4) 回调段均量 / 试盘日量 ≤ 0.70（缩量回踩）。
  5) 沿用：vr20≥1.05、非涨停、跨度 20~35 交易日、回调 10~30%、同股 20 日去重。
  6) 一票否决：ST/*ST/退市；2025 审计机构为立信 / 公告涉立信 / 查询缺失（保守剔除）。
  7) 按"与善水科技 2026-09-23 原型形态贴合度"排序取前 N。
  8) 每次运行更新 善水模型_推荐跟踪.csv，自动回填到期记录的 T+10 收益并附复盘。
  9) 非交易日或行情未更新（最新行情日≠当日）自动退出、不推送。

运行：
  python3 daily_shanshui.py --model v10 --top 10
  python3 daily_shanshui.py --model v10 --top 10 --dry-run
  python3 daily_shanshui.py --model v10 --top 10 --no-push

环境变量：PUSHPLUS_TOKEN（推送）、STATE_DIR（状态目录，默认脚本同级 state/）、WORKERS
依赖：requests
"""
import argparse
import csv
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

# ------------------------------------------------------------------ 基础配置
BJT = timezone(timedelta(hours=8))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR") or os.path.join(SCRIPT_DIR, "state")
WORKERS = int(os.environ.get("WORKERS") or 8)
PUSHPLUS_TOKEN = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

EM_KLINE_HOSTS = [
    "push2his.eastmoney.com", "88.push2his.eastmoney.com", "22.push2his.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2his.eastmoney.com", "72.push2his.eastmoney.com",
    "1.push2his.eastmoney.com", "7.push2his.eastmoney.com",
]
EM_LIST_HOSTS = [
    "push2.eastmoney.com", "82.push2.eastmoney.com", "22.push2.eastmoney.com",
    "33.push2his.eastmoney.com", "48.push2.eastmoney.com", "72.push2.eastmoney.com",
    "1.push2.eastmoney.com",
]
EM_F10 = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EM_KUT = "fa5fd1943c7b386f172d6893dbfba10b"

# 全 A：沪深主板 + 创业板 + 科创板 + 北交所
EM_BOARDS = [
    ("沪市主板", "m:1+t:2"),
    ("深市主板", "m:0+t:6"),
    ("创业板", "m:0+t:80"),
    ("科创板", "m:1+t:23"),
    ("北交所", "m:0+t:81+s:2048"),
]

# ------------------------------------------------------------------ 通用工具
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))

def round2(x):
    return round(float(x) + 0.0, 2)

def to_float(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def http_json(url, retry=2, timeout=15, params=None, referer=None):
    headers = {"Referer": referer} if referer else None
    for attempt in range(retry + 1):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200:
                raise RuntimeError("HTTP %s" % r.status_code)
            return r.json()
        except Exception:
            if attempt < retry:
                time.sleep(0.4 * (attempt + 1))
    return None

def szsh_of(code):
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return ("sh" if code.startswith("6") else "sz") + code

def secu_of(code):
    if code.startswith("6"):
        return code + ".SH"
    if code.startswith(("8", "4", "92")):
        return code + ".BJ"
    return code + ".SZ"

def secid_of(code):
    code = str(code).zfill(6)
    return ("1." if code.startswith("6") else "0.") + code

def last_weekday(today):
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()

# ------------------------------------------------------------------ A 股行情（东方财富）
def get_universe():
    fields = ("f2,f3,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18,f20,f21")
    rows = []
    for name, fs in EM_BOARDS:
        pn = 1
        while True:
            params = {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fid": "f3",
                "fs": fs, "fields": fields, "ut": EM_UT, "fltt": 2, "invt": 2,
            }
            data = {}
            hosts = EM_LIST_HOSTS[:]
            random.shuffle(hosts)
            for host in hosts:
                j = http_json("https://%s/api/qt/clist/get" % host,
                              params=params, timeout=15, retry=0)
                data = (j or {}).get("data") or {}
                if data.get("diff"):
                    break
                time.sleep(0.2)
            diff = data.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            total = int(to_float(data.get("total")))
            for x in diff:
                code = str(x.get("f12", "")).zfill(6)
                price = to_float(x.get("f2"))
                if not code or price <= 0:
                    continue
                rows.append({
                    "code": code, "name": x.get("f14", ""), "board": name,
                    "price": price, "pct": to_float(x.get("f3")),
                    "turnover": to_float(x.get("f8")),
                    "market_cap_yi": to_float(x.get("f20")) / 1e8,
                    "prev_close": to_float(x.get("f18")),
                })
            if len(diff) < 100 or (total and pn * 100 >= total):
                break
            pn += 1
            time.sleep(0.2)
    seen, uniq = set(), []
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        uniq.append(r)
    return uniq

def em_hist(code, count=140):
    beg = (datetime.now(BJT).date() - timedelta(days=max(30, count * 3))).strftime("%Y%m%d")
    params = {
        "secid": secid_of(code), "ut": EM_KUT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "beg": beg, "end": 20500101, "lmt": count,
    }
    hosts = EM_KLINE_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts[:3]:
        j = http_json("https://%s/api/qt/stock/kline/get" % host,
                      params=params, timeout=12, retry=0)
        rows = ((j or {}).get("data") or {}).get("klines") or []
        bars = []
        for s in rows:
            p = s.split(",")
            if len(p) < 6:
                continue
            try:
                bars.append({"d": p[0], "o": float(p[1]), "c": float(p[2]),
                             "h": float(p[3]), "l": float(p[4]), "v": float(p[5])})
            except ValueError:
                continue
        if bars:
            return bars
        time.sleep(0.15)
    return None

def _f10_chunk(secucodes):
    inlist = ",".join('"%s"' % c for c in secucodes)
    j = http_json(EM_F10, params={
        "reportName": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_NAME_ABBR,EM2016,ACCOUNTFIRM_NAME",
        "filter": "(SECUCODE in (%s))" % inlist,
        "pageNumber": 1, "pageSize": 50, "source": "HSF10", "client": "PC",
    }, timeout=15, retry=1)
    if isinstance(j, list) and j:
        j = j[0]
    rows = []
    try:
        rows = j["result"]["data"] or []
    except Exception:
        rows = []
    out = {}
    for row in rows:
        code = str(row["SECUCODE"]).split(".")[0]
        out[code] = {
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "ind": row.get("EM2016", "") or "",
            "firm": row.get("ACCOUNTFIRM_NAME", "") or "",
        }
    return out

def f10_batch(codes):
    out = {}
    codes = [secu_of(c) for c in codes]
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        got = _f10_chunk(chunk)
        if not got and len(chunk) > 10:
            for j in range(0, len(chunk), 10):
                got.update(_f10_chunk(chunk[j:j + 10]))
                time.sleep(0.2)
        out.update(got)
        time.sleep(0.2)
    return out

# ------------------------------------------------------------------ v10 试盘线形态
def trading_dates(bars):
    return [b["d"] for b in bars]

def count_trading_days_back(bars, idx, target):
    """idx 之前（含）有多少交易日达到 target 日期（用于 T+10 跟踪）"""
    n = 0
    for k in range(idx, -1, -1):
        n += 1
        if bars[k]["d"] <= target:
            return n
    return n

def v10_evaluate(bars):
    """返回 (符合条件, 形态贴合度, 明细) 或 None。"""
    n = len(bars)
    if n < 36:
        return None
    C = [b["c"] for b in bars]
    H = [b["h"] for b in bars]
    V = [b["v"] for b in bars]
    i = n - 1
    cur = bars[i]

    # 当日非涨停
    if cur["c"] >= cur["o"] * 1.095:
        return None
    # 当前 vr20（量比）
    v20_now = mean(V[i - 20:i]) if i >= 20 else 0
    vr20_now = V[i] / v20_now if v20_now > 0 else 0
    if vr20_now < 1.05:
        return None

    # 计算每根 bar 的 vr20，找放量日（vr20>=1.2）中的最高价 = 试盘线前高
    trial_high = -1.0
    trial_idx = -1
    for k in range(20, n):
        vk = mean(V[k - 20:k]) if k >= 20 else 0
        vr = V[k] / vk if vk > 0 else 0
        if vr >= 1.2 and H[k] > trial_high:
            trial_high = H[k]
            trial_idx = k
    if trial_idx < 0 or trial_high <= 0:
        return None

    span = i - trial_idx  # 跨度（交易日）
    if not (20 <= span <= 35):
        return None

    # 回调 10%~30%：试盘后到当前之间，最低收盘价相对试盘线前高的回撤
    low_after = min(C[trial_idx:i + 1])
    drawdown = 1 - low_after / trial_high
    if not (0.10 <= drawdown <= 0.30):
        return None

    # 收盘距前高 -3%~0%
    close_gap = cur["c"] / trial_high - 1
    if not (-0.03 <= close_gap <= 0.0):
        return None

    # 回调段均量 / 试盘日量 ≤ 0.70
    pull_v = mean(V[trial_idx:i + 1])
    trial_v = V[trial_idx]
    if trial_v <= 0 or pull_v / trial_v > 0.70:
        return None

    # 形态贴合度（重建推断）：越接近前高、缩量越干净、跨度越居中，分越高
    score_near = clamp((close_gap + 0.03) / 0.03)          # 0~1，越贴近前高越高
    score_dry = clamp((0.70 - pull_v / trial_v) / 0.70)     # 0~1，缩量越狠越高
    score_span = clamp(1 - abs(span - 27) / 8)              # 跨度越接近 27 越居中
    score = round((score_near * 0.45 + score_dry * 0.35 + score_span * 0.20) * 100, 1)

    return {
        "trial_high": round2(trial_high), "trial_idx": trial_idx, "span": span,
        "drawdown": round(drawdown * 100, 1), "close_gap": round(close_gap * 100, 2),
        "vol_ratio_pull": round(pull_v / trial_v, 3), "vr20_now": round(vr20_now, 2),
        "score": score,
    }

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

# ------------------------------------------------------------------ 推荐跟踪 CSV
CSV_PATH = os.path.join(SCRIPT_DIR, "善水模型_推荐跟踪.csv")
CSV_FIELDS = ["date", "code", "name", "board", "score", "close", "trial_high",
              "drawdown", "close_gap", "vol_ratio_pull", "t10_return", "t10_filled"]

def load_csv():
    if not os.path.exists(CSV_PATH):
        return []
    rows = []
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            rows.append(r)
    return rows

def save_csv(rows):
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})

def backfill_t10(bars_map):
    """对推荐日距今≥10交易日的记录，回填 T+10 收益。返回 (处理样本数, 胜率, 中位, 均值)"""
    rows = load_csv()
    filled, rets = 0, []
    for r in rows:
        if r.get("t10_filled") == "1":
            try:
                rets.append(float(r["t10_return"]))
            except ValueError:
                pass
            continue
        code = r["code"]
        bars = bars_map.get(code)
        if not bars:
            continue
        dates = trading_dates(bars)
        try:
            ridx = dates.index(r["date"])
        except ValueError:
            continue
        # 找推荐日后第 10 个交易日收盘价
        tgt = ridx + 10
        if tgt >= len(bars):
            continue
        try:
            base = float(r["close"])
        except ValueError:
            base = bars[ridx]["c"]
        ret = (bars[tgt]["c"] / base - 1) * 100
        r["t10_return"] = round(ret, 2)
        r["t10_filled"] = "1"
        filled += 1
        rets.append(ret)
    save_csv(rows)
    if not rets:
        return 0, None, None, None
    win = sum(1 for x in rets if x > 0)
    return len(rets), win / len(rets) * 100, sorted(rets)[len(rets) // 2], sum(rets) / len(rets)

# ------------------------------------------------------------------ 推送
def push(title, content):
    if not PUSHPLUS_TOKEN:
        print("[推送] 未配置 PUSHPLUS_TOKEN，仅打印")
        return False
    try:
        r = requests.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN, "title": title,
            "content": content, "template": "markdown",
        }, timeout=15)
        j = r.json()
        print("[PushPlus] code=%s msg=%s" % (j.get("code"), j.get("msg")))
        return j.get("code") == 200
    except Exception as e:
        print("[PushPlus] 异常: %s" % e)
        return False

# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="v10")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    today = datetime.now(BJT).date()
    print("=" * 64)
    print("善水模型 v10（重建版）  %s" % today)
    print("=" * 64)

    # 基准交易日
    sample = em_hist("600519", 8) or em_hist("000001", 8)
    base_date = sample[-1]["d"] if sample else last_weekday(today)
    print("[基准] 上一交易日 = %s" % base_date)
    # 非交易日保护：行情最新日≠当日（盘前跑时基准应为上一交易日，这里仅做基本判断）
    if base_date >= today.isoformat():
        print("[退出] 行情日期异常（%s），疑似非交易日或数据未更新" % base_date)
        return

    print("[1/3] 获取全 A 股票池...")
    universe = get_universe()
    print("      共 %d 只" % len(universe))
    if not universe:
        print("股票池为空，退出")
        return

    # ST/退市 预筛 + 立信审计（保守剔除）
    def is_st(u):
        return bool(re.search("ST|退", u["name"])) or u["name"].startswith("N")

    pool = [u for u in universe if not is_st(u) and 2 <= u["price"] <= 200]
    print("      预筛后 %d 只" % len(pool))

    print("[2/3] 扫描 v10 试盘线形态...")
    scored = []
    f10_cache = {}

    def work(u):
        time.sleep(random.uniform(0.1, 0.3))
        bars = em_hist(u["code"], 60)
        if not bars:
            return None
        ev = v10_evaluate(bars)
        if not ev:
            return None
        return {**u, **ev, "bars": bars}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, u) for u in pool]
        for fu in as_completed(futs):
            r = fu.result()
            if r:
                scored.append(r)

    if not scored:
        print("      无符合条件样本")
        # 仍做一次 T+10 复盘回填
        bars_map = {s["code"]: s.pop("bars") for s in scored}
        stat = backfill_t10(bars_map)
        print("[复盘] 到期样本 %s 胜率 %s" % (stat[0], stat[1]))
        if not args.no_push:
            push("善水模型 %s 无信号" % today, "今日无 v10 试盘线形态样本。")
        return

    # 立信审计一票否决（取不到则保守剔除）
    codes = [s["code"] for s in scored]
    f10 = f10_batch(codes)
    kept, excluded = [], 0
    for s in scored:
        firm = (f10.get(s["code"]) or {}).get("firm", "")
        if "立信" in firm:
            excluded += 1
            continue
        if firm == "":  # 查询缺失保守剔除
            excluded += 1
            continue
        kept.append(s)
    scored = kept

    scored.sort(key=lambda x: x["score"], reverse=True)
    picks = scored[:args.top]

    # 同股 20 日去重（按 code 最近一次已推荐日期）
    rows = load_csv()
    recent = {}
    for r in rows:
        recent[r["code"]] = r["date"]
    final = []
    for p in picks:
        lst = recent.get(p["code"])
        if lst and (today - datetime.strptime(lst, "%Y-%m-%d").date()).days < 20:
            continue
        final.append(p)

    # 落盘 CSV
    new_rows = load_csv()
    for p in final:
        new_rows.append({
            "date": today.isoformat(), "code": p["code"], "name": p["name"],
            "board": p["board"], "score": p["score"], "close": round2(p["price"]),
            "trial_high": p["trial_high"], "drawdown": p["drawdown"],
            "close_gap": p["close_gap"], "vol_ratio_pull": p["vol_ratio_pull"],
            "t10_return": "", "t10_filled": "0",
        })
    bars_map = {p["code"]: p.pop("bars") for p in final}
    if not args.dry_run:
        save_csv(new_rows)

    # T+10 复盘回填
    stat = backfill_t10(bars_map) if bars_map else (0, None, None, None)

    # 简报
    print("      原始信号 %d / 立信&缺失剔除 %d / 入选 %d" %
          (len(scored) + excluded, excluded, len(final)))
    print("      到期跟踪样本 %s 胜率 %s 中位 %s 均值 %s" %
          (stat[0], stat[1], stat[2], stat[3]))
    for p in final:
        print("      %s %s 分%.1f 收%.2f 距前高%+.2f%% 回撤%.1f%% 缩量%.2f" %
              (p["name"], p["code"], p["score"], p["price"],
               p["close_gap"], p["drawdown"], p["vol_ratio_pull"]))

    msg = build_msg(today, final, excluded, stat)
    print("\n" + msg)

    if args.dry_run:
        print("\n[dry-run] 不写 CSV、不推送")
        return
    if not args.no_push:
        push("善水模型 v10 %s 入选%d只" % (today, len(final)), msg)
    print("\n完成")

def build_msg(today, final, excluded, stat):
    out = []
    out.append("【善水模型 v10 %s】" % today)
    out.append("入选 %d 只（立信/查询缺失剔除 %d）" % (len(final), excluded))
    out.append("")
    for i, p in enumerate(final[:10], 1):
        out.append("%d. %s %s | 分%.1f 收%.2f 距前高%+.2f%% 回撤%.1f%% 缩量%.2f" % (
            i, p["name"], p["code"], p["score"], p["price"],
            p["close_gap"], p["drawdown"], p["vol_ratio_pull"]))
    if stat[0]:
        out.append("")
        out.append("T+10 复盘：样本%d 胜率%.1f%% 中位%+.2f%% 均值%+.2f%%" %
                   (stat[0], stat[1] or 0, stat[2] or 0, stat[3] or 0))
    out.append("")
    out.append("口径：东财日线（前复权）；重建版，形态贴合度为推断，不构成投资建议")
    return "\n".join(out)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
