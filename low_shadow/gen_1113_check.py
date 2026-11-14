#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 2026-11-13 昨日样本核对页：13 只完整清单 + 各改进方案保留标记 + 样本量对比。"""
import json
import html

BASE = "/workspace/us-radar-picker/low_shadow/state"
official = json.load(open(BASE + "/qualified_20261113_171842.json", encoding="utf-8"))
bt = json.load(open(BASE + "/bt_oct_result.json", encoding="utf-8"))
PICKS = bt["picks"]
CANDS = official["candidates"]


def is_bj(s):
    return s.startswith(("4", "8", "92"))


def is_gem_star(s):
    return s.startswith(("300", "301", "688", "689"))


RULES = {
    "基线": lambda p: True,
    "F1": lambda p: p["upper_amp"] >= 0.08,
    "G3": lambda p: (1e8 <= p["amount"] <= 1e9 and not is_bj(p["symbol"])
                     and p["pct_chg"] > -3.0 and p["vol_ratio"] <= 3.0),
    "G4": lambda p: (3e8 <= p["amount"] <= 1e9 and not is_bj(p["symbol"])
                     and p["pct_chg"] > -3.0),
    "H2": lambda p: (not is_bj(p["symbol"]) and p["pct_chg"] > -3.0
                     and p["vol_ratio"] <= 3.0 and p["amount"] <= 1e9),
    "H5": lambda p: (not is_bj(p["symbol"]) and p["pct_chg"] > -3.0
                     and p["vol_ratio"] <= 3.0 and p["amount"] <= 1e9
                     and is_gem_star(p["symbol"])),
    "C8": lambda p: (p["upper_amp"] >= 0.08 and p["amount"] >= 2e8
                     and not is_bj(p["symbol"])),
}
TAGS = list(RULES.keys())
RULE_DESC = {
    "基线": "当前生产模型（上影≥0.045）",
    "F1": "上影 ≥ 0.08",
    "G3": "成交额 1~10 亿 + 排除北交所/跌>3%/量比>3",
    "G4": "成交额 3~10 亿 + 排除北交所/跌>3%",
    "H2": "保留 0.045 + 排除北交所/跌>3%/量比>3/额>10 亿",
    "H5": "H2 + 只留创业板/科创板（推荐）",
    "C8": "上影≥0.08 + 额≥2 亿 + 排除北交所（极致收益）",
}


def stat(ps, n):
    v = [p["rets"].get(str(n)) for p in ps]
    v = [x for x in v if x is not None]
    return (len(v),
            round(100 * sum(1 for x in v if x > 0) / len(v), 1) if v else None,
            round(100 * sum(v) / len(v), 2) if v else None)


win = {}
for t in TAGS:
    ps = [p for p in PICKS if RULES[t](p)]
    win[t] = {"n": len(ps), "days": len(set(p["date"] for p in ps)),
              "r3": stat(ps, 3), "r5": stat(ps, 5)}

yes = {}
for t in TAGS:
    yes[t] = set(p["symbol"] for p in CANDS if RULES[t](p))

# ---------- HTML ----------
def esc(x):
    return html.escape(str(x))


rows = []
for i, p in enumerate(CANDS, 1):
    marks = "".join(
        '<td class="mk %s">%s</td>' % ("ok" if p["symbol"] in yes[t] else "no",
                                      "✓" if p["symbol"] in yes[t] else "—")
        for t in TAGS)
    chg = p["pct_chg"]
    cls = "up" if chg > 0 else ("down" if chg < 0 else "flat")
    rows.append(
        "<tr><td class='idx'>%d</td><td class='code'>%s</td><td class='nm'>%s</td>"
        "<td class='num sc'>%.2f</td><td class='num'>%.4f</td><td class='num'>%.2f</td>"
        "<td class='num'>%.3f</td><td class='num'>%.2f 亿</td><td class='num'>%.2f%%</td>"
        "<td class='num %s'>%+.2f%%</td>%s</tr>" % (
            i, esc(p["symbol"]), esc(p["name"]), p["score"], p["upper_amp"],
            p["upper_to_body"], p["vol_ratio"], p["amount"] / 1e8,
            p["pos_pct"] * 100, cls, chg, marks))

cmp_rows = []
for t in TAGS:
    w, y = win[t], len(yes[t])
    keep = round(100 * w["n"] / win["基线"]["n"], 1)
    cmp_rows.append(
        "<tr class='%s'><td class='nm'><b>%s</b><span class='desc'>%s</span></td>"
        "<td class='num'>%d</td><td class='num'>%d</td><td class='num'>%s%%</td>"
        "<td class='num'>%s%%</td><td class='num'>%+.2f%%</td><td class='num'>%+.2f%%</td>"
        "<td class='num hi'>%d</td><td class='num'>%d → %d</td></tr>" % (
            "rec" if t == "H5" else "", esc(t), esc(RULE_DESC[t]),
            w["n"], w["days"], keep, w["r3"][1], w["r3"][2], w["r5"][2],
            y, 13, y))

keep_names = []
for t in TAGS:
    ns = "、".join("%s %s" % (p["symbol"], p["name"]) for p in CANDS
                  if p["symbol"] in yes[t]) or "（空仓）"
    keep_names.append("<div class='chip'><b>%s</b> <em>%d 只</em><br><span>%s</span></div>" % (
        esc(t), len(yes[t]), esc(ns)))

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>低位上影线 · 昨日样本核对 2026-11-13</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--ink:#1f2328;--sub:#6b7280;--line:#e5e2d9;
--acc:#b8893c;--ok:#c0392b;--no:#b9b5aa;--rec:#fdf6e6;--up:#c0392b;--down:#1e8449}
*{box-sizing:border-box}body{margin:0;padding:28px;background:var(--bg);color:var(--ink);
font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px;letter-spacing:.5px}
.sub{color:var(--sub);font-size:13px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.03)}
h2{font-size:16px;margin:0 0 4px;border-left:3px solid var(--acc);padding-left:10px}
.note{color:var(--sub);font-size:12.5px;margin:6px 0 14px}
table{width:100%%;border-collapse:collapse;font-size:13px}
th{background:#faf9f5;color:var(--sub);font-weight:600;font-size:12px;
padding:9px 8px;border-bottom:1px solid var(--line);text-align:center;white-space:nowrap}
td{padding:8px;border-bottom:1px solid #f0eee7;text-align:center}
tr:last-child td{border-bottom:none}
.idx{color:var(--sub);width:32px}.code{font-family:ui-monospace,Menlo,monospace;color:#334}
.nm{text-align:left;white-space:nowrap}.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.sc{font-weight:700;color:var(--acc)}
.up{color:var(--up);font-weight:600}.down{color:var(--down);font-weight:600}.flat{color:var(--sub)}
.mk{font-weight:700;width:36px}.mk.ok{color:var(--ok);background:#fdf1ef}.mk.no{color:var(--no)}
.grp th:nth-child(-n+10){background:#faf9f5}
tr.rec{background:var(--rec)}
.desc{display:block;color:var(--sub);font-size:11.5px;font-weight:400;margin-top:2px}
.hi{font-weight:700;color:var(--acc)}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.chip{flex:1 1 250px;background:#faf9f5;border:1px solid var(--line);border-radius:8px;
padding:10px 12px;font-size:12px}
.chip b{color:var(--acc);font-size:13px}.chip em{color:var(--sub);font-style:normal;float:right}
.chip span{display:block;color:#444;margin-top:5px;line-height:1.6}
.warn{background:#fff8ec;border:1px solid #ecd9ae;border-radius:8px;padding:14px 16px;
font-size:13px;color:#6b5327;margin-top:14px}
.big{font-size:20px;font-weight:700;color:var(--acc)}
.kv{display:flex;gap:26px;flex-wrap:wrap;margin:10px 0 4px}
.kv div{background:#faf9f5;border:1px solid var(--line);border-radius:8px;padding:10px 16px}
.kv b{display:block;font-size:18px;color:var(--acc)}
.kv span{font-size:12px;color:var(--sub)}
footer{color:var(--sub);font-size:12px;text-align:center;padding:10px 0 24px}
</style></head><body><div class="wrap">
<h1>低位上影线模型 · 昨日样本核对</h1>
<div class="sub">运行日 <b>2026-11-13（周四）</b> · 云端扫描 %(scanned)d 只 · 立信审计剔除 %(veto)d 只 · 符合 8 道硬条件 <b>%(n)d 只</b> · 清单来源：云端 CI 官方归档（东财真实成交额）</div>
<div class="warn"><b>⚠ 扫描覆盖不全：</b>云端自 11-10 晚起扫描数稳定在 <b>4605~4607</b> 只，而真实全市场 A 股约 <b>5578</b> 只（新浪 <code>hs_a</code> 全量），少约 <b>1000 只</b>。源头是远端「全市场列表降级时回退完整快照」实际回退到了子集。因此这 13 只<b>可能不是真正的全市场完整清单</b>，存在系统性漏号风险，需修复列表降级逻辑后再复核。</div>

<div class="card">
<h2>一、结论：改进后样本确实更低</h2>
<div class="kv">
<div><b>322 → 238</b><span>温和方案 H2（−26%%）</span></div>
<div><b>322 → 123</b><span>推荐方案 H5（−62%%）</span></div>
<div><b>322 → 60</b><span>G4 / F1（−81%%）</span></div>
<div><b>322 → 30</b><span>极致 C8（−91%%）</span></div>
</div>
<div class="note">口径：2026-10-08 ~ 11-13 共 27 个交易日、全市场逐日扫描，基线 322 条入选样本；下表各方案为在同一批样本上叠加过滤，规则已复现并与回测报告数字逐项校验一致。</div>
<table>
<thead><tr><th style="text-align:left">改进方案</th><th>全窗样本</th><th>开仓天数</th><th>保留率</th>
<th>3日命中</th><th>3日均收益</th><th>5日均收益</th><th>昨日留存</th><th>昨日变化</th></tr></thead>
<tbody>%(cmp)s</tbody></table>
<div class="warn"><b>⚠ 参数口径提醒：</b>归档 <code>run_20261113_171842.json</code> 里写的 <code>upper_amp_min=0.055 / vol_ratio_min=1.6 / low_pos_max_pct=0.13</code> 是<b>运行末尾</b>被 <code>correct_params()</code> 就地收紧后的值 —— 而筛选发生在收紧之前。所以这 13 只实际仍按 <b>0.045 / 1.5 / 0.15</b> 筛出（清单里概伦电子 0.047、智莱科技 0.050 均低于 0.055 可证）。<b>下一次运行才会真正用 0.055</b>。归档参数与本次筛选口径不一致，属于需修复的记录缺陷。</div>
</div>

<div class="card">
<h2>二、昨日 13 只完整清单（按评分降序）</h2>
<div class="note">右侧为各改进方案是否保留该股：✓ 保留 / — 剔除。评分是当日候选池内分位排名，跨日不可比。本表成交额采用<b>云端官方归档（东财真实值）</b>；10 月回测用的是新浪备用源估算值（成交量 × 典型价，约 +2%% 偏差），因此边界票判定略有差异 —— 例如理工导航官方 0.984 亿 &lt; 1 亿门槛，在 G3 下被剔除，而回测估算 1.01 亿会保留。<b>本表以官方真实值为准</b>。</div>
<table class="grp">
<thead><tr><th>#</th><th>代码</th><th style="text-align:left">名称</th><th>评分</th><th>上影/开</th>
<th>上影/实体</th><th>量比</th><th>成交额</th><th>距20日低</th><th>当日涨跌</th>
%(th)s</tr></thead>
<tbody>%(rows)s</tbody></table>
</div>

<div class="card">
<h2>三、若改进方案落地，昨天会推哪些</h2>
<div class="chips">%(chips)s</div>
<div class="note" style="margin-top:14px">H5（推荐）在昨日保留 10 只，仅剔除海达尔（北交所）、恒丰纸业（沪主板）、华大九天（成交额 11.27 亿 > 10 亿）。C8 在昨日为 <b>空仓</b>：唯一上影≥0.08 且非北交所的艾布鲁成交额 1.75 亿 &lt; 2 亿门槛，被同时剔除。</div>
</div>

<footer>低位上影线样本 · 收盘后模型 | 核对页生成于 2026-11-14 | 证据：qualified_20261113_171842.json / bt_oct_result.json / bt_oct_report.md</footer>
</div></body></html>
""" % {
    "scanned": official["total_scanned"], "veto": 2, "n": official["qualified_count"],
    "cmp": "\n".join(cmp_rows), "rows": "\n".join(rows),
    "th": "".join("<th>%s</th>" % t for t in TAGS),
    "chips": "\n".join(keep_names),
}

out = "/workspace/昨日样本核对_20261113.html"
open(out, "w", encoding="utf-8").write(HTML)
print("已生成:", out)
print("全窗口校验:", {t: win[t]["n"] for t in TAGS})
print("昨日留存:", {t: len(yes[t]) for t in TAGS})
