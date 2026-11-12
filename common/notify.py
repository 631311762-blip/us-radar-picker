#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py —— 统一告警通道 + GitHub Actions 失败通知「按天聚合」
========================================================================
两类调用方：

1) common/prefilter.py —— 前置层降级告警（快照兜底、K 线缓存未全覆盖、
   快照健康校验失败）。每个 run 发一条，不做跨 run 去重。
   用法：  send_alert(title, content)

2) .github/workflows/failure-notify.yml —— 监听所有 workflow_run(completed)，
   把同一天（北京时间）多次失败合并成 **一条** 推送，后续失败只累加计数，
   避免「一天 5 次失败 = 5 封告警」的噪音。
   用法：  python3 common/notify.py --workflow ... --run-id ... --url ...
           --dedup-file state/failure_notify.json

设计约束：
- 只用标准库（urllib），不引入新依赖，也不 import common_lib（避免循环依赖）。
- 所有网络调用都有超时且吞掉异常：**告警失败绝不能让选股流水线挂掉**。
- 渠道按 PUSHPLUS_TOKEN → SERVERCHAN_SENDKEY 顺序尝试，任一成功即返回。
- 未配置任何密钥时只打印日志并返回 False，不报错。

注意：本模块无法关闭 GitHub 自带的失败邮件（那是账户级通知设置），
      它提供的是一条「清晰、去重、带汇总」的替代告警通道。
"""
import os
import sys
import json
import argparse
import datetime
import urllib.request
import urllib.parse

BJT = datetime.timezone(datetime.timedelta(hours=8))

PUSHPLUS_URL = "http://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/%s.send"
HTTP_TIMEOUT = 10
KEEP_DAYS = 30

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEDUP_FILE = os.path.join(_REPO_ROOT, "state", "failure_notify.json")


def _now():
    return datetime.datetime.now(BJT).isoformat(timespec="seconds")


def today_key():
    """按北京时间取当天去重键。"""
    return datetime.datetime.now(BJT).strftime("%Y-%m-%d")


def _post(url, payload, content_type="application/x-www-form-urlencoded"):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": content_type,
                 "User-Agent": "us-radar-picker/notify"},
        method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "ignore")


def pushplus(title, content):
    token = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if not token:
        return False, "未配置 PUSHPLUS_TOKEN"
    body = _post(PUSHPLUS_URL, {
        "token": token, "title": title, "content": content, "template": "txt",
    })
    try:
        code = json.loads(body).get("code")
    except Exception:
        code = None
    if code == 200:
        return True, "code=200"
    return False, "响应异常: %s" % body[:120]


def serverchan(title, content):
    key = (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()
    if not key:
        return False, "未配置 SERVERCHAN_SENDKEY"
    body = _post(SERVERCHAN_URL % key, {"title": title, "desp": content})
    try:
        code = json.loads(body).get("code")
    except Exception:
        code = None
    if code == 0 or '"code":0' in body.replace(" ", ""):
        return True, "code=0"
    return False, "响应异常: %s" % body[:120]


def send_alert(title, content):
    """尽最大努力发一条告警，返回 (是否成功, 明细)。绝不抛异常。"""
    details = []
    for name, fn in (("pushplus", pushplus), ("serverchan", serverchan)):
        try:
            ok, msg = fn(title, content)
        except Exception as exc:               # 网络/解析异常一律降级为「未送达」
            ok, msg = False, "异常 %s" % exc
        details.append("%s=%s(%s)" % (name, ok, msg))
        if ok:
            return True, "; ".join(details)
    return False, "; ".join(details)


# ------------------------------------------------------------------ 按天聚合去重
def _load_dedup(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # 文件缺失 / 损坏都不影响主流程，重建即可
        return {}


def _save_dedup(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def aggregate_failure(workflow="", run_id="", url="", dedup_file=None, day=None):
    """登记一次失败，返回 (是否应当推送, 当日记录, 日期)。

    同一天只有第一次返回 True；之后仅累加 count / 记录来源，不再推送。
    去重文件按天存 key，并只保留最近 KEEP_DAYS 天，避免无限增长。
    """
    path = dedup_file or os.environ.get("NOTIFY_DEDUP_FILE") or DEFAULT_DEDUP_FILE
    day = day or today_key()
    data = _load_dedup(path)
    rec = data.get(day)
    if not isinstance(rec, dict):
        rec = {
            "date": day,
            "count": 0,
            "first_at": _now(),
            "last_at": _now(),
            "workflows": [],
            "runs": [],
            "sent": False,
        }
        data[day] = rec
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["last_at"] = _now()
    wf = rec.setdefault("workflows", [])
    if workflow and workflow not in wf:
        wf.append(workflow)
    runs = rec.setdefault("runs", [])
    if run_id and len(runs) < 20:
        runs.append({"id": str(run_id), "url": url, "workflow": workflow})
    should_push = not bool(rec.get("sent"))
    if should_push:
        rec["sent"] = True
        rec["sent_at"] = _now()
    # 只保留最近 KEEP_DAYS 天
    for old in sorted(data.keys())[:-KEEP_DAYS]:
        data.pop(old, None)
    try:
        _save_dedup(path, data)
    except Exception as exc:
        print("[notify] 去重文件写入失败（不影响告警）: %s" % exc, file=sys.stderr)
    return should_push, rec, day


def _write_step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="GitHub Actions 失败通知按天聚合")
    ap.add_argument("--workflow", default="", help="失败的工作流名称")
    ap.add_argument("--run-id", default="", help="失败的 run id")
    ap.add_argument("--url", default="", help="失败 run 的链接")
    ap.add_argument("--dedup-file", default=None, help="去重状态文件路径")
    ap.add_argument("--dry-run", action="store_true", help="登记去重但不真正发送（本地测试用）")
    args = ap.parse_args()

    should, rec, day = aggregate_failure(
        workflow=args.workflow, run_id=args.run_id, url=args.url,
        dedup_file=args.dedup_file)

    wf_list = "、".join(rec.get("workflows") or []) or "（未知）"
    print("[notify] %s 累计失败 %d 次；涉及工作流：%s"
          % (day, rec["count"], wf_list))
    print("PUSH=%d" % (1 if should else 0))

    if not should:
        msg = ("当日已聚合 %d 次失败，跳过重复推送（首次告警已发出）。" % rec["count"])
        print("[notify] " + msg)
        _write_step_summary("### 失败通知聚合\n\n- 日期：%s\n- 累计失败：%d 次\n- %s\n"
                            % (day, rec["count"], msg))
        return 0

    title = "【%s】GitHub Actions 失败告警" % day
    content = "\n".join([
        "日期：%s（北京时间）" % day,
        "状态：当日首次失败，后续失败将合并到本条，不再重复推送。",
        "工作流：%s" % (args.workflow or "（未知）"),
        "Run ID：%s" % (args.run_id or "-"),
        "链接：%s" % (args.url or "-"),
        "",
        "说明：本告警由 failure-notify.yml 聚合产生；GitHub 自带的失败邮件"
        "需在账户 Settings → Notifications 中另行调整。",
    ])
    _write_step_summary("### 失败通知聚合\n\n- 日期：%s\n- 当日首次失败，已发送聚合告警\n- 工作流：%s\n- 链接：%s\n"
                        % (day, args.workflow or "-", args.url or "-"))

    if args.dry_run:
        print("[notify] dry-run：跳过实际推送")
        return 0

    ok, detail = send_alert(title, content)
    print("SEND_OK=%d DETAIL=%s" % (1 if ok else 0, detail))
    # 推送失败不返回非 0：聚合 workflow 不应因为告警渠道故障而标红，
    # 否则会产生「告警自己失败 → 又一条失败」的循环噪音。
    return 0


if __name__ == "__main__":
    sys.exit(main())
