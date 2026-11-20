#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按天微信推送去重（防止主流水线 / 兜底 / 手动补跑同日重复推送）。
========================================================================
背景（2026-11-20）：善水、高位上影当天被多条路径重复触发（统一流水线多次
run + 高位上影独立 cron + 超时重试），用户微信收到好几条相同推送。

设计：
  - 标记落在 common/state/push_dedup/<model>_<date>.json，该目录随 workflow
    回写入库，跨 runner 生效（与领湃「推送去重.json」同一思路）。
  - 推送成功后才写标记（失败允许补推）；同一自然日同模型只放行一次。
  - PUSH_FORCE=1 可强制绕过（紧急补发用）；仅保留最近 7 天标记，不膨胀。
  - 不依赖 CWD：路径基于 __file__ 绝对路径。
"""
import json
import os
from datetime import datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8))
_DEDUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "state", "push_dedup")
_KEEP_DAYS = 7


def _today():
    return datetime.now(BJT).strftime("%Y-%m-%d")


def _path(model, date):
    safe = "".join(c for c in str(model) if c.isalnum() or c in "-_") or "model"
    return os.path.join(_DEDUP_DIR, "%s_%s.json" % (safe, date))


def already_pushed(model, date=None):
    """今天该模型是否已成功推送过。"""
    p = _path(model, date or _today())
    if not os.path.exists(p):
        return False
    try:
        rec = json.load(open(p, encoding="utf-8"))
        return bool(rec.get("ok"))
    except Exception:
        return False


def mark_pushed(model, date=None, note=""):
    """标记今天该模型已成功推送，并清理过期标记。"""
    day = date or _today()
    p = _path(model, day)
    try:
        os.makedirs(_DEDUP_DIR, exist_ok=True)
        json.dump({"ok": True, "date": day,
                   "at": datetime.now(BJT).isoformat(timespec="seconds"),
                   "note": note},
                  open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        print("[push_guard] 标记写入失败（不影响推送）: %s" % e)
    _gc()


def _gc():
    """只保留最近 N 天的标记文件。"""
    try:
        cutoff = (datetime.now(BJT) - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
        for fn in os.listdir(_DEDUP_DIR):
            if not fn.endswith(".json"):
                continue
            # 文件名形如 <model>_<date>.json
            date = fn[:-5].rsplit("_", 1)[-1]
            if date < cutoff:
                try:
                    os.unlink(os.path.join(_DEDUP_DIR, fn))
                except OSError:
                    pass
    except Exception:
        pass


def should_push(model, date=None, force=None):
    """是否应当推送：未被强制且今天没推过 = True。"""
    if force is None:
        force = os.environ.get("PUSH_FORCE", "") == "1"
    return force or not already_pushed(model, date)
