#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
善水模型 v10 收盘后选股（沙箱重建版）
- 数据源：新浪不复权日线（全 A 扫描），巨潮资讯全文检索（审计一票否决）
- 选股条件：试盘线前高（仅取 vr20>=1.2 放量日中的最高价）、收盘距前高 -3%~0%、
  回调段均量/试盘日量<=0.70；回调段内前高不可被突破、试盘前高须高于60日均线>=10%
  （均为 2026-11-10 修正）；沿用 vr20>=1.05、非涨停、跨度 20-35 交易日、
  回调 10-30%、同股 20 日去重
- 一票否决：ST/*ST/退市；2025 年度审计涉立信（含改聘）、审计查询缺失
- 排序：与善水科技(301190) 2026-09-23 原型形态贴合度
- 输出：/workspace/善水模型_推荐跟踪.csv（自动回填 T+10 收益）+ PushPlus 推送
说明：原 daily_shanshui.py 随沙箱重置丢失，本脚本按任务口径重建；
PushPlus token 未随脚本恢复，可写入 /workspace/.pushplus_token 或环境变量
PUSHPLUS_TOKEN，缺失时改为落盘推送内容文件。
"""
import argparse, csv, datetime as dt, json, os, re, shutil, subprocess, sys, time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://finance.sina.com.cn"}
CN_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "Referer": "http://www.cninfo.com.cn/",
         "X-Requested-With": "XMLHttpRequest"}

WS = "/workspace"
REPO_DIR = os.path.join(WS, "repo")
GIT_CFG = os.path.join(WS, ".git_sync.json")
# 仓库内本模型的子目录（与仓库中其他模型并存，互不干扰）
SUBDIR = "shanshui/sina"
CSV_PATH = os.path.join(WS, "善水模型_推荐跟踪.csv")
PROTO_SYM, PROTO_DATE = "sz301190", "2026-09-23"
# ---- 阈值（任务口径，勿改） ----
VR_SURGE = 1.2      # 试盘放量倍数（v10 新条件，用于取前高）
VR_LEGACY = 1.05    # 沿用条件
SPAN_LO, SPAN_HI = 20, 35        # 试盘日->信号日 跨度（交易日）
DD_LO, DD_HI = 0.10, 0.30        # 回调幅度区间
DIST_LO, DIST_HI = -0.03, 0.0    # 收盘距前高
VOLR_MAX = 0.70                  # 回调段均量/试盘日量
MA60_PREMIUM = 0.10              # 试盘前高须高于60日均线的最小幅度（2026-11-10 修正：
                                 # 新凤鸣/川宁生物前高仅高出MA60 1.8%/4.3%，形态贴线、
                                 # 选入后即跌破60日线；原型善水科技为+19.5%）
DEDUP_DAYS = 20                  # 同股去重交易日
TOP_N = 10


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- git 自动同步（配置 /workspace/.git_sync.json 后生效） ----------
# 格式: {"url": "https://github.com/xx/yy.git", "token": "ghp_xxx", "branch": "main"}
def _sh(cmd, cwd=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)


def _git_url(url, token):
    if url.startswith("https://"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url


def sync_pull():
    """配置了仓库则克隆/拉取（CSV 等结果以仓库为准）。返回仓库目录或 None。"""
    if not os.path.exists(GIT_CFG):
        return None
    try:
        cfg = json.load(open(GIT_CFG, encoding="utf-8"))
        url, token = cfg["url"].strip(), cfg["token"].strip()
        if os.path.isdir(os.path.join(REPO_DIR, ".git")):
            _sh(f"git config user.name 'shanshui-bot'; git config user.email 'bot@shanshui.local'; "
                f"git remote set-url origin {_git_url(url, token)}; git pull --rebase", cwd=REPO_DIR)
        else:
            if os.path.isdir(REPO_DIR):
                shutil.rmtree(REPO_DIR)
            _sh(f"git clone {_git_url(url, token)} {REPO_DIR}")
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            raise RuntimeError("仓库不可用")
        log("git 拉取完成: " + REPO_DIR)
        return REPO_DIR
    except Exception as e:
        log(f"git 同步失败(按本地继续): {e}")
        return None


def sync_push(out_files):
    """运行结束后提交推送：结果文件 + 本地脚本副本（本地为准，便于模型更新）。"""
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        return
    sub = os.path.join(REPO_DIR, SUBDIR)
    try:
        for f in out_files:
            if f and os.path.exists(f) and os.path.dirname(f) == sub:
                _sh(f"git add '{SUBDIR}/{os.path.basename(f)}'", cwd=REPO_DIR)
        script, dst = os.path.abspath(__file__), os.path.join(sub, "daily_shanshui.py")
        if not os.path.exists(dst) or open(dst, "rb").read() != open(script, "rb").read():
            shutil.copy(script, dst)
        _sh(f"git add {SUBDIR}/daily_shanshui.py", cwd=REPO_DIR)
        r = _sh("git commit -m 'shanshui(sina): 模型运行结果同步 %s'" % dt.date.today(), cwd=REPO_DIR)
        if "nothing to commit" not in (r.stdout or ""):
            p = _sh("git push", cwd=REPO_DIR)
            if p.returncode == 0:
                log("git 推送完成")
            else:
                log("git 推送失败: " + ((p.stderr or p.stdout) or "")[:200])
        else:
            log("git 无变更，跳过推送")
    except Exception as e:
        log(f"git 推送失败: {e}")


def http_get(url, timeout=15, tries=3, session=None):
    for i in range(tries):
        try:
            r = (session or requests).get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    return None


def parse_kline(text):
    m = re.search(r"\((\[.*\])\)", text, re.S)
    if not m:
        return []
    out = []
    for d in json.loads(m.group(1)):
        try:
            out.append({"day": d["day"], "open": float(d["open"]),
                        "high": float(d["high"]), "low": float(d["low"]),
                        "close": float(d["close"]), "volume": float(d["volume"])})
        except Exception:
            continue
    return out


def get_kline(sym, n=160, session=None):
    url = ("http://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
           f"CN_MarketDataService.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    r = http_get(url, session=session)
    return parse_kline(r.text) if r else []


def limit_pct(code):
    return 0.20 if code[:3] in ("300", "301", "688") else 0.10


def is_limit_up(prev_close, close, code):
    if prev_close <= 0:
        return False
    return (close / prev_close - 1) >= limit_pct(code) * 0.995


def stock_metrics(bars, sig_idx, code):
    """以 bars[sig_idx] 为信号日，按 v10 规则求解形态；不满足返回 None。"""
    if sig_idx < 40 or len(bars) < 50:
        return None
    lo_i = max(0, sig_idx - SPAN_HI)   # 试盘日允许窗口 [sig-35, sig-20]
    hi_i = sig_idx - SPAN_LO
    if hi_i < lo_i or lo_i < 20:
        return None
    surge = []
    for i in range(lo_i, hi_i + 1):
        prev = bars[i - 20:i]
        mv = sum(b["volume"] for b in prev) / 20.0
        if mv <= 0:
            continue
        vr = bars[i]["volume"] / mv
        if vr >= VR_SURGE and vr >= VR_LEGACY:
            surge.append((i, vr))
    if not surge:
        return None
    H = max(bars[i]["high"] for i, _ in surge)
    # 试盘日：取创出前高的放量日（并列取最近）
    cand = [(i, vr) for i, vr in surge if abs(bars[i]["high"] - H) < 1e-9]
    t_i, t_vr = max(cand, key=lambda x: x[0])
    # 试盘日非涨停
    if is_limit_up(bars[t_i - 1]["close"], bars[t_i]["close"], code):
        return None
    span = sig_idx - t_i
    if not (SPAN_LO <= span <= SPAN_HI):
        return None
    # 回调段：试盘日次日至区间最低价日
    seg = bars[t_i + 1: sig_idx + 1]
    if not seg:
        return None
    # 前高不可被提前突破（v10 修正 2026-11-10）：信号日之前的回调段内任何一日
    # 最高价不得超过试盘前高 H。否则前高已失效，信号日收盘只是突破失败后
    # 的回落低位（如华昌达 2026-11-09，回调段 10-26 摸 3.95 > 前高 3.71），
    # 不符合"回踩不破、信号日重新逼近试盘线前高"的原型形态
    # （善水科技 2026-09-23：回调段最高 21.58 < 前高 21.80）。
    # 信号日当天不设此限制：盘中逼近/微破而收盘仍距前高 -3%~0 属突破进行时。
    seg_prev = bars[t_i + 1: sig_idx]
    if seg_prev and max(b["high"] for b in seg_prev) > H + 1e-9:
        return None
    min_low = min(b["low"] for b in seg)
    dd = (H - min_low) / H
    if not (DD_LO <= dd <= DD_HI):
        return None
    dd_i = t_i + 1 + [b["low"] for b in seg].index(min_low)
    if dd_i <= t_i:
        return None
    mv_seg = sum(b["volume"] for b in bars[t_i + 1: dd_i + 1]) / (dd_i - t_i)
    volr = mv_seg / bars[t_i]["volume"] if bars[t_i]["volume"] > 0 else 9
    if volr > VOLR_MAX:
        return None
    close = bars[sig_idx]["close"]
    dist = close / H - 1
    if not (DIST_LO <= dist <= DIST_HI):
        return None
    # 试盘前高须明显高于60日均线（v10 修正 2026-11-10）：前高贴着 MA60 的票
    # （如新凤鸣 +1.8%、川宁生物 +4.3%）选入后极易回落跌破 60 日线，
    # 不符合原型（善水科技前高高出 MA60 +19.5%）。
    # 上市不足 60 日无法计算 MA60，按查询缺失保守剔除。
    if sig_idx < 59:
        return None
    ma60 = sum(b["close"] for b in bars[sig_idx - 59: sig_idx + 1]) / 60.0
    if ma60 > 0 and H < ma60 * (1 + MA60_PREMIUM):
        return None
    if is_limit_up(bars[sig_idx - 1]["close"], close, code):
        return None
    return {"dist": dist, "dd": dd, "volr": volr, "vr": t_vr, "span": span,
            "high": H, "close": close}


def fetch_list():
    """全 A 股票列表（沪深，剔除北交所）。"""
    out, page = [], 1
    while True:
        url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page")
        r = http_get(url)
        if not r:
            break
        try:
            data = json.loads(r.text)
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        page += 1
        if page > 120:
            break
    return out


def audit_veto(sec_name, sec_code, session):
    """返回 (是否剔除, 原因)。涉及立信/查询缺失/接口失败 -> 剔除。"""
    today = dt.date.today().strftime("%Y-%m-%d")
    sdate = f"{dt.date.today().year - 1}-01-01"
    def search(key, isfull="true", page=1, size=30):
        q = urllib.parse.urlencode({"searchkey": key, "sdate": sdate, "edate": today,
                                    "isfulltext": isfull, "sortName": "pubdate",
                                    "sortType": "desc", "pageNum": str(page), "pageSize": str(size)})
        r = session.get("http://www.cninfo.com.cn/new/fulltextSearch/full?" + q, timeout=20)
        if not r.text.strip():
            raise RuntimeError("empty")
        return json.loads(r.text)
    try:
        total, seen = None, 0
        page = 1
        while True:
            j = search(f"{sec_name} 立信", page=page)
            total = j.get("totalAnnouncement", 0)
            anns = j.get("announcements") or []
            for a in anns:
                if str(a.get("secCode")) == str(sec_code):
                    return True, "公告涉及立信"
            seen += len(anns)
            if seen >= min(total, 300) or not anns:
                break
            page += 1
            time.sleep(0.2)
    except Exception:
        return True, "审计接口失败"
    try:
        j = search(f"{sec_name} 审计机构")
        anns = j.get("announcements") or []
        hit = any(str(a.get("secCode")) == str(sec_code) for a in anns)
        if not hit and (j.get("totalAnnouncement") or 0) > 0:
            # 可能前几页是别家，翻页确认
            t = j.get("totalAnnouncement", 0)
            for p in range(2, min(4, (t // 30) + 2)):
                j2 = search(f"{sec_name} 审计机构", page=p)
                if any(str(a.get("secCode")) == str(sec_code) for a in (j2.get("announcements") or [])):
                    hit = True
                    break
        if not hit:
            return True, "审计查询缺失"
    except Exception:
        return True, "审计接口失败"
    return False, ""


def fit_score(m, p):
    """与原型的贴合度：各指标归一化绝对差之和，越小越贴合。"""
    return (abs(m["dist"] - p["dist"]) / 0.03 + abs(m["dd"] - p["dd"]) / 0.20
            + abs(m["volr"] - p["volr"]) / 0.70 + abs(m["vr"] - p["vr"]) / 2.0
            + abs(m["span"] - p["span"]) / 15.0)


def read_csv():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(rows, fields):
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def t10_backfill(rows, calendar):
    """回填到期记录的 T+10 收益（信号日收盘买入，T+10 收盘卖出）。"""
    filled = 0
    for r in rows:
        if r.get("T+10收益%"):
            continue
        sym = r.get("sym") or ""
        if not sym:
            continue
        bars = get_kline(sym, 80)
        days = [b["day"] for b in bars]
        try:
            i = days.index(r["信号日"])
        except ValueError:
            continue
        if i + 10 >= len(days):
            continue
        c0 = float(r.get("信号日收盘") or bars[i]["close"])
        ret = (bars[i + 10]["close"] / c0 - 1) * 100
        r["T+10日期"] = days[i + 10]
        r["T+10收益%"] = f"{ret:.2f}"
        filled += 1
    return filled


def main():
    global CSV_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="v10")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--date", default=None,
                    help="回放模式：强制指定信号日（须为最新已收盘行情日），如 2026-11-09")
    args = ap.parse_args()
    today = args.date or dt.date.today().strftime("%Y-%m-%d")

    # 0) git 自动同步（配置 .git_sync.json 后生效；结果与脚本持久化到仓库 shanshui/sina/）
    repo_dir = sync_pull()
    if repo_dir:
        sub = os.path.join(repo_dir, SUBDIR)
        os.makedirs(sub, exist_ok=True)
        CSV_PATH = os.path.join(sub, "善水模型_推荐跟踪.csv")

    # 1) 交易日/数据更新校验
    ref = get_kline("sh000001", 80)
    cal_days = [b["day"] for b in ref]
    if today not in cal_days:
        log(f"指定日({today})非交易日，自动退出不推送")
        sys.exit(2)
    replay_off = len(cal_days) - 1 - cal_days.index(today)  # 信号日距最新行情日的交易日偏移
    if args.date is None and replay_off != 0:
        last = cal_days[-1]
        log(f"最新行情日({last}) != 今日({today})：非交易日或收盘数据未更新，自动退出不推送")
        sys.exit(2)
    if replay_off:
        log(f"回放模式: 以 {today} 为信号日复算（距今 {replay_off} 个交易日）")

    # 2) 原型指标
    p_bars = get_kline(PROTO_SYM, 120)
    p_days = [b["day"] for b in p_bars]
    if PROTO_DATE not in p_days:
        log("原型日数据缺失，退出")
        sys.exit(3)
    p_idx = p_days.index(PROTO_DATE)
    proto = stock_metrics(p_bars, p_idx, "301190")
    if proto is None:
        log("原型日未通过形态规则复算，退出")
        sys.exit(3)
    log(f"原型复算: 距前高{proto['dist']*100:.2f}% 回调{proto['dd']*100:.2f}% "
        f"缩量比{proto['volr']:.2f} vr20={proto['vr']:.2f} 跨度{proto['span']}")

    # 3) 全 A 列表 + 剔除 ST/退市/北交所
    universe = fetch_list()
    log(f"全A列表获取: {len(universe)} 只")
    st_cnt = 0
    stocks = []
    for s in universe:
        sym, name, code = s["symbol"], s["name"], s["code"]
        if not sym.startswith(("sh", "sz")):
            continue
        if "ST" in name.upper() or "退" in name:
            st_cnt += 1
            continue
        stocks.append((sym, name, code))
    log(f"待扫描(剔除ST/退市{st_cnt}只): {len(stocks)} 只")

    # 4) 并发拉K线并解形态
    raw, errs = [], 0
    def work(item):
        sym, name, code = item
        bars = get_kline(sym, 160 + replay_off)
        if len(bars) < 50:
            return None
        days = [b["day"] for b in bars]
        if today not in days:
            return None
        si = days.index(today)
        if replay_off == 0 and si != len(bars) - 1:
            return None   # 实时模式要求数据最新
        return (sym, name, code, stock_metrics(bars[:si + 1], si, code))
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(work, it) for it in stocks]
        done = 0
        for f in as_completed(futs):
            done += 1
            if done % 500 == 0:
                log(f"扫描进度 {done}/{len(stocks)}")
            try:
                res = f.result()
            except Exception:
                errs += 1
                continue
            if res and res[3]:
                raw.append(res)
    log(f"原始信号数: {len(raw)} (K线获取失败 {errs} 只)")

    # 5) 同股 20 日去重（单日内每股仅取最近试盘日信号，已天然唯一；
    #    再对历史 CSV 中 20 个交易日内的同股信号去重）
    old_rows = read_csv()
    # 同股20日去重：只对早于今日的历史信号去重（当日重复运行视为覆盖更新）
    cal = [b["day"] for b in get_kline("sh000001", 80)]
    recent = set()
    for r in old_rows:
        d = r.get("信号日")
        if d in cal:
            # 与本次运行信号日同日的记录视为覆盖更新，不参与去重
            # （否则回放/重跑同一天时会把上一次的同日结果误去重）
            if d == today:
                continue
            days_ago = cal_days.index(today) - cal_days.index(d)  # 早于信号日的交易日数
            if 0 < days_ago < DEDUP_DAYS:
                recent.add(r["代码"])
    dedup_cnt = 0
    cands = []
    for sym, name, code, m in raw:
        if code in recent:
            dedup_cnt += 1
            continue
        cands.append({"sym": sym, "代码": code, "名称": name, **m})
    log(f"20日去重剔除: {dedup_cnt} 只，待审计核验: {len(cands)} 只")

    # 6) 审计一票否决（巨潮全文检索）
    ses = requests.Session(); ses.headers.update(CN_UA)
    veto_audit, veto_missing, keep = 0, 0, []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(audit_veto, c["名称"], c["代码"], ses): c for c in cands}
        for f in as_completed(futs):
            c = futs[f]
            try:
                bad, why = f.result()
            except Exception:
                bad, why = True, "审计接口失败"
            if bad:
                if why == "公告涉及立信":
                    veto_audit += 1
                else:
                    veto_missing += 1
                c["_why"] = why
            else:
                keep.append(c)
    log(f"审计剔除: 涉立信 {veto_audit} 只, 查询缺失/接口失败 {veto_missing} 只, 保留 {len(keep)} 只")

    # 7) 贴合度排序取前 N
    for c in keep:
        c["贴合度分"] = round(fit_score(c, proto), 4)
    keep.sort(key=lambda c: c["贴合度分"])
    top = keep[:args.top]
    log(f"入选前{len(top)}")

    # 8) 更新 CSV（回填 T+10 + 追加今日）
    fields = ["信号日", "代码", "名称", "距前高%", "回调%", "缩量比", "vr20", "跨度",
              "贴合度分", "信号日收盘", "T+10日期", "T+10收益%", "备注"]
    backfilled = t10_backfill(old_rows, cal)
    # 当日重跑覆盖（先移除今日旧行再追加）
    old_rows = [r for r in old_rows if r.get("信号日") != today]
    for r in top:
        old_rows.append({"信号日": today, "代码": r["代码"], "名称": r["名称"],
                         "距前高%": f"{r['dist']*100:.2f}", "回调%": f"{r['dd']*100:.2f}",
                         "缩量比": f"{r['volr']:.2f}", "vr20": f"{r['vr']:.2f}",
                         "跨度": r["span"], "贴合度分": r["贴合度分"],
                         "信号日收盘": f"{r['close']:.2f}", "备注": ""})
    write_csv(old_rows, fields)

    # 9) T+10 复盘统计
    dones = [r for r in old_rows if r.get("T+10收益%")]
    review = ""
    if dones:
        rets = [float(r["T+10收益%"]) for r in dones]
        win = sum(1 for x in rets if x > 0) / len(rets) * 100
        rets_sorted = sorted(rets)
        mid = rets_sorted[len(rets_sorted)//2] if len(rets_sorted) % 2 else \
              (rets_sorted[len(rets_sorted)//2 - 1] + rets_sorted[len(rets_sorted)//2]) / 2
        review = (f"T+10复盘(样本{len(rets)}): 胜率{win:.0f}%, "
                  f"收益中位{mid:.2f}%, 均值{sum(rets)/len(rets):.2f}%")
        log(review)

    # 10) 推送内容（<=900字）+ PushPlus
    lines = [f"**善水模型v10 信号 {today}**", "",
             f"原始信号{len(raw)}｜ST剔除{st_cnt}｜涉立信{veto_audit}｜审计缺失/失败{veto_missing}｜去重{dedup_cnt}"]
    if review:
        lines.append(review)
    lines.append("")
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. {r['名称']}({r['代码']}) 距前高{r['dist']*100:.2f}% "
                     f"回调{r['dd']*100:.2f}% 缩量比{r['volr']:.2f} "
                     f"vr20={r['vr']:.2f} 跨度{r['span']}分{r['贴合度分']}")
    content = "\n".join(lines)
    out_dir = os.path.join(repo_dir, SUBDIR) if repo_dir else WS
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, f"善水模型_推送内容_{today.replace('-', '')}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)

    token = os.environ.get("PUSHPLUS_TOKEN") or ""
    tk_file = os.path.join(WS, ".pushplus_token")
    if not token and os.path.exists(tk_file):
        token = open(tk_file).read().strip()
    if token:
        try:
            pr = requests.post("http://www.pushplus.plus/send", json={
                "token": token, "title": f"善水模型v10 {today}", "template": "markdown",
                "content": content}, timeout=15)
            log(f"PushPlus推送: {pr.text[:120]}")
        except Exception as e:
            log(f"推送失败: {e}")
    else:
        log("未配置 PushPlus token（原脚本内置token未随沙箱恢复），推送内容已落盘: " + md_path)
    log(f"CSV已更新(回填T+10 {backfilled} 条): {CSV_PATH}")
    sync_push([CSV_PATH, md_path])
    print("RESULT_JSON=" + json.dumps({
        "date": today, "raw": len(raw), "st": st_cnt, "veto_lx": veto_audit,
        "veto_missing": veto_missing, "dedup": dedup_cnt, "keep": len(keep),
        "review": review, "csv": CSV_PATH, "md": md_path,
        "top": [{k: r[k] for k in ("名称", "代码")} | {"dist": round(r["dist"]*100,2),
                "dd": round(r["dd"]*100,2), "volr": round(r["volr"],2),
                "vr": round(r["vr"],2), "span": r["span"], "score": r["贴合度分"]} for r in top],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
