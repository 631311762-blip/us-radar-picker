# -*- coding: utf-8 -*-
"""_lixin_update.py：立信会计师事务所排除集增量更新。
原版：解析全市场审计机构变更公告正文，判定 2025 审计机构=立信 或 2026 改聘立信 即加入排除集。
重建版：简化为扫描当前全 A 的 F10 审计机构，凡审计机构含"立信"（含"立信中联"）即加入排除集，
        幂等、仅保留 A 股代码。功能近似，未做公告增量解析，需在真实标的上核对。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 领湃工具箱 as tb


def main():
    print("=" * 64)
    print("_lixin_update（重建版）")
    print("=" * 64)

    u = tb.get_universe()
    print("[股票池] %d 只" % len(u))
    exclude = tb.load_lixin_exclude()

    codes = [x["code"] for x in u]
    f10 = tb.f10_batch(codes)

    added = 0
    for code, info in f10.items():
        firm = (info.get("firm") or "")
        if "立信" in firm:
            if code not in exclude:
                exclude.add(code)
                added += 1

    tb.save_lixin_exclude(exclude)
    print("[立信排除集] 累计 %d 只，本次新增 %d" % (len(exclude), added))
    print("[完成]")


if __name__ == "__main__":
    main()
