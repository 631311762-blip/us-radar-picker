# -*- coding: utf-8 -*-
"""_lixin_update.py：立信会计师事务所排除集增量更新。
原版：解析全市场审计机构变更公告正文，判定 2025 审计机构=立信 或 2026 改聘立信 即加入排除集。
重建版：简化为扫描当前全 A 的 F10 审计机构，凡审计机构含"立信"即加入排除集，
        幂等、仅保留 A 股代码；「立信中联」为另一家事务所，按规则自动排除，
        并顺带清洗历史误加入排除集的立信中联客户。功能近似，需在真实标的上核对。
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

    added = removed = 0
    for code, info in f10.items():
        firm = (info.get("firm") or "")
        if "立信" not in firm:
            continue
        if "立信中联" in firm:
            # 「立信中联」是独立事务所，不属于立信排除范围；
            # 历史版本曾误将其客户加入排除集，这里顺带清洗。
            if code in exclude:
                exclude.discard(code)
                removed += 1
            continue
        if code not in exclude:
            exclude.add(code)
            added += 1

    tb.save_lixin_exclude(exclude)
    print("[立信排除集] 累计 %d 只，本次新增 %d，清洗立信中联误加 %d"
          % (len(exclude), added, removed))
    print("[完成]")


if __name__ == "__main__":
    main()
