#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_risk.py —— 定期报告 / 业绩预告「临近披露」标注（推送专用）
========================================================================
用户口径（2026-11-23）：
  9 个模型选出的股票，如果近期要发布定期报告或业绩预告，很可能出现业绩
  不及预期、商誉减值等黑天鹅。推送微信时必须**特殊标识**出来，便于谨慎对待。

判定依据：
  东财「预约披露时间」接口 RPT_PUBLIC_BS_APPOIN，取 APPOINT_PUBLISH_DATE
  落在 [今天, 今天+HORIZON_DAYS] 且尚未实际披露的预约记录；推送时只对
  WARN_DAYS 天内的标的打标（默认 15 个自然日）。

  说明：业绩预告没有公开的「预约日期」数据，交易所只规定披露截止窗口，
  而业绩预告/快报通常在定期报告预约日之前发布。因此以「定期报告预约披露日
  临近」作为统一预警信号，并在提示语中明确覆盖业绩预告风险。

设计约束：
  - 只做标注，不参与选股/剔除，绝不影响任何模型的入选结果。
  - 推送路径严格离线：只读 common/state/report_calendar.json，不发起网络。
  - 日历由 prefilter 每个交易日刷新一次（失败保留旧文件，不阻断流水线）。
  - 日期在读取时按「当天」重算剩余天数，文件陈旧也不会继续误标过期标的。

产物：common/state/report_calendar.json

用法：
  python3 common/report_risk.py              # 刷新并打印 15 天内标的
  python3 common/report_risk.py --days 30    # 自定义预警窗口
"""
import os
import re
import sys
import json
import datetime
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import common_lib as cm
    BJT = cm.BJT
    STATE_DIR = cm.STATE_DIR
except Exception:                       # 独立运行兜底
    BJT = datetime.timezone(datetime.timedelta(hours=8))
    STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")

CAL_JSON = os.path.join(STATE_DIR, "report_calendar.json")
API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_PUBLIC_BS_APPOIN"
HORIZON_DAYS = int(os.environ.get("REPORT_RISK_HORIZON_DAYS", "60"))
WARN_DAYS = int(os.environ.get("REPORT_RISK_DAYS", "15"))
PAGE_SIZE = 500
MAX_PAGES = 20
TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
TITLE_PREFIX = "⚠️财报/预告临近 "

_CAUTION = ("定期报告披露前后，业绩预告/业绩快报可能先行发布，存在业绩不及预期、"
            "商誉减值等黑天鹅风险，建议控制仓位，待事件落地后再评估。")


def _today():
    return datetime.datetime.now(BJT).date()


def _parse(d):
    try:
        return datetime.date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _request(params, retries=2):
    url = API_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:          # 网络/解析异常重试
            last = e
    raise RuntimeError("东财预约披露接口失败: %s" % last)


def fetch_appointments(begin, end, verbose=False):
    """拉取 [begin, end] 区间全市场定期报告预约披露记录。"""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        j = _request({
            "reportName": REPORT_NAME,
            "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_TYPE_NAME,"
                        "APPOINT_PUBLISH_DATE,ACTUAL_PUBLISH_DATE"),
            "pageNumber": page,
            "pageSize": PAGE_SIZE,
            "sortColumns": "APPOINT_PUBLISH_DATE",
            "sortTypes": "1",
            "filter": "(APPOINT_PUBLISH_DATE>='%s')(APPOINT_PUBLISH_DATE<='%s')"
                      % (begin, end),
        })
        result = j.get("result") or {}
        data = result.get("data") or []
        rows.extend(data)
        pages = int(result.get("pages") or 0)
        if verbose:
            print("[report_risk] 第 %d/%d 页：%d 条" % (page, pages, len(data)))
        if not data or page >= pages:
            break
    return rows


def refresh(days_ahead=None, verbose=True, path=None):
    """刷新预约披露日历并落盘。异常向上抛出，由 prefilter 降级处理。"""
    today = _today()
    horizon = int(days_ahead if days_ahead is not None else HORIZON_DAYS)
    begin = today.isoformat()
    end = (today + datetime.timedelta(days=horizon)).isoformat()
    rows = fetch_appointments(begin, end, verbose=verbose)

    entries = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "").zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        date = str(r.get("APPOINT_PUBLISH_DATE") or "")[:10]
        actual = str(r.get("ACTUAL_PUBLISH_DATE") or "")[:10]
        if not date or date < begin or date > end:
            continue
        if actual and actual <= begin:          # 已实际披露，不再是未来风险
            continue
        old = entries.get(code)
        if old and old.get("date", "") <= date:  # 只保留最近一期
            continue
        entries[code] = {
            "name": str(r.get("SECURITY_NAME_ABBR") or ""),
            "report": str(r.get("REPORT_TYPE_NAME") or ""),
            "date": date,
        }

    warn = sum(1 for e in entries.values()
               if (_parse(e["date"]) - today).days <= WARN_DAYS)
    payload = {
        "generated_at": datetime.datetime.now(BJT).isoformat(timespec="seconds"),
        "as_of": begin,
        "horizon_days": horizon,
        "warn_days": WARN_DAYS,
        "source": "eastmoney:" + REPORT_NAME,
        "entries": {c: entries[c] for c in sorted(entries)},
        "stats": {
            "fetched": len(rows),
            "codes": len(entries),
            "in_window": warn,
            "scan_empty": len(rows) == 0,
            "window": "%s~%s" % (begin, end),
        },
    }
    target = path or CAL_JSON
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    if verbose:
        print("[report_risk] 预约披露日历：未来 %d 天共 %d 只，其中 %d 天内的 %d 只 → %s"
              % (horizon, len(entries), WARN_DAYS, warn, target))
    return payload


def load(today=None, path=None):
    """读取日历并按当天重算剩余天数，过期条目自动失效。"""
    target = path or CAL_JSON
    if not os.path.exists(target):
        return {}
    try:
        with open(target, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    today = today or _today()
    out = {}
    for code, e in (payload.get("entries") or {}).items():
        d = _parse(e.get("date"))
        if d is None or d < today:
            continue
        out[code] = {
            "name": e.get("name") or "",
            "report": e.get("report") or "",
            "date": d.isoformat(),
            "days": (d - today).days,
        }
    return out


def _match(text, entries):
    """先按 6 位代码匹配；正文完全没有代码时再按名称兜底。"""
    codes = set(re.findall(r"(?<!\d)(\d{6})(?!\d)", text))
    hits = []
    if codes:
        for code, e in entries.items():
            if code in codes:
                hits.append((code, e))
        return hits
    for code, e in entries.items():
        name = e.get("name") or ""
        if len(name) >= 3 and name in text:
            hits.append((code, e))
    return hits


def decorate(title, content, template="markdown", today=None, path=None):
    """给推送正文追加「临近财报」醒目标识，返回 (新标题, 新正文)。

    无命中时原样返回；任何异常都由调用方吞掉，绝不影响推送本身。
    """
    entries = load(today=today, path=path)
    if not entries:
        return title, content
    text = "%s\n%s" % (title or "", content or "")
    hits = [(c, e) for c, e in _match(text, entries) if e["days"] <= WARN_DAYS]
    if not hits:
        return title, content
    hits.sort(key=lambda x: (x[1]["days"], x[0]))

    if template == "html":
        items = "".join(
            "<li><b>%s</b> %s：%s 预约 %s（%d 天后）</li>"
            % (_esc(c), _esc(e["name"]), _esc(e["report"]), e["date"], e["days"])
            for c, e in hits)
        block = ("<hr><p style=\"color:#c00\"><b>⚠️ 临近定期报告/业绩预告"
                 "（请谨慎对待）</b></p><ul>%s</ul><p>%s</p>" % (items, _esc(_CAUTION)))
    else:
        items = "\n".join(
            "- %s %s：%s 预约 %s（%d 天后）"
            % (c, e["name"], e["report"], e["date"], e["days"])
            for c, e in hits)
        block = ("\n\n---\n\n**⚠️ 临近定期报告/业绩预告（请谨慎对待）**\n\n%s\n\n> %s\n"
                 % (items, _CAUTION))

    if not str(title or "").startswith(TITLE_PREFIX.strip()):
        title = TITLE_PREFIX + (title or "")
    return title, (content or "") + block


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def main():
    days = WARN_DAYS
    args = sys.argv[1:]
    if "--days" in args:
        try:
            days = int(args[args.index("--days") + 1])
        except (IndexError, ValueError):
            pass
    print("=" * 64)
    print("定期报告/业绩预告 临近披露日历")
    print("=" * 64)
    payload = refresh(verbose=True)
    got = load()
    near = sorted(((c, e) for c, e in got.items() if e["days"] <= days),
                  key=lambda x: x[1]["days"])
    print("[report_risk] %d 天内（含）共 %d 只：" % (days, len(near)))
    for c, e in near[:30]:
        print("  %s %s  %s  %s（%d 天后）"
              % (c, e["name"], e["report"], e["date"], e["days"]))
    print("[report_risk] stats=%s" % json.dumps(payload.get("stats"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
