# -*- coding: utf-8 -*-
"""
云端备份 · 每日产出 + 最新模型 → 资料库网盘
================================================================================
用户要求（2026-11-05）：每天的产出保存到云端，后续回溯可直接获取；
最新的模型不能因为聊天记录/沙箱重置就丢了。

每次闭环结束自动做两件事：
  ① stock-v3-cloud.zip —— 覆盖上传（固定节点），含模型代码 + 全部状态文件
     （predictions.json 历史预测、factor_fit/holdout 权重、feedback_state、
      push_log、今日推荐等）。恢复：下载 zip 解压到 /workspace/ 即可跑通。
  ② 主升浪V4_<基准日>.md —— 每天新建一份当日推送原文，按日期可直接翻阅回溯。

不打包：kline_cache.json（160MB，可重建）、audit_detail/audit_notices（20MB，
可由 audit_check 重跑生成）。上传失败只告警不中断主流程。
"""
import os, sys, json, zipfile, subprocess, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
UP = "/root/.codebuddy/skills/library/drive/upload_drive_file.py"
SPACE_ID = "ojjdOfqUEapz1RHD0W3lKV"
ZIP_NODE = "xXnKH6dpFgeOacF8p8PM5E"      # stock-v3-cloud.zip 固定覆盖该节点

ZIP_FILES = [
    # 模型与代码（最新模型本体，聊天没了也在云端）
    "每日推荐.py", "daily_v3.py", "features.py", "datafetch.py", "model2.py",
    "feedback.py", "audit_check.py", "wechat_push.py", "cloud_backup.py",
    "warm_cache.py", "CLOUD_README.md", "sckey.txt",
    # 禁改的模型参数与回测基准
    "factor_fit.json", "holdout.json",
    # 运行状态 / 历史产出（回溯要用的核心数据）
    "predictions.json", "feedback_state.json", "feedback_result.json",
    "push_log.jsonl", "今日推荐.json", "universe_cache.json",
    "audit_result.json", "audit_notices.json",
]


def make_zip(path):
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ZIP_FILES:
            p = os.path.join(HERE, f)
            if os.path.exists(p):
                z.write(p, f)
                n += 1
            else:
                print(f"  [备份] 跳过不存在的文件：{f}")
    return n


def upload(local, name, node_id=""):
    """调资料库上传脚本；返回 (ok, msg)。网关偶发 502/501，重试一次。"""
    for attempt in (1, 2):
        cmd = ["python3", UP, local, "--file-name", name,
               "--space-id", SPACE_ID]
        if node_id:
            cmd += ["--node-id", node_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", timeout=180)
            out = (r.stdout or "") + (r.stderr or "")
            if "KS_DRIVE_UPLOAD_OK" in out or "KS_USER_REPLY" in out:
                return True, out.strip().splitlines()[-1]
            if attempt == 1:
                continue
            return False, out.strip()[-300:]
        except Exception as e:
            if attempt == 1:
                continue
            return False, str(e)
    return False, "unreachable"


def main(base_date=""):
    if not os.path.exists(UP):
        print("[备份] 本环境无资料库脚本，跳过云端备份")
        return False
    print("\n" + "=" * 78)
    print("【第三步】云端备份（模型 + 每日产出 → 资料库）")
    print("=" * 78)
    ok_all = True

    # ① 全量 zip 覆盖
    zpath = "/tmp/stock-v3-cloud.zip"
    n = make_zip(zpath)
    ok, msg = upload(zpath, "stock-v3-cloud.zip", ZIP_NODE)
    print(f"  {'✓' if ok else '✗'} stock-v3-cloud.zip（{n} 个文件，覆盖上传）{': ' + msg if not ok else ''}")
    ok_all &= ok

    # ② 当日推送原文（按日期独立留档，方便回溯翻阅）
    if not base_date:
        try:
            base_date = json.load(open(os.path.join(HERE, "今日推荐.json"),
                                       encoding="utf-8")).get("base_date", "")
        except Exception:
            base_date = ""
    body = os.path.join(HERE, "push_body.md")
    if base_date and os.path.exists(body):
        name = f"主升浪V4_{base_date}.txt"   # 网盘不收 .md（要走文档模块），用 .txt 留档
        tmp = os.path.join("/tmp", name)
        with open(body, "r", encoding="utf-8") as f, \
             open(tmp, "w", encoding="utf-8") as g:
            g.write(f.read())
        ok2, msg2 = upload(tmp, name)
        # 同名文件已存在时会新建重复节点，可接受（每天一份，天然按日期归档）
        print(f"  {'✓' if ok2 else '✗'} {name}（当日推送原文）{': ' + msg2 if not ok2 else ''}")
        ok_all &= ok2
    else:
        print("  [备份] 无当日推送原文（push_body.md），跳过日报上传")
    print(f"  备份{'完成' if ok_all else '存在失败项'}"
          f"（zip 固定节点：https://www.workbuddy.cn/space/d/{ZIP_NODE}）")
    return ok_all


if __name__ == "__main__":
    ok = main(sys.argv[1] if len(sys.argv) > 1 else "")
    sys.exit(0 if ok else 1)
