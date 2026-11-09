# -*- coding: utf-8 -*-
"""Server酱 微信推送（云端版）—— 与本地 wechat-push 技能同签名：push(title, markdown) -> (ok, msg)

SendKey 读取优先级：环境变量 SERVERCHAN_SENDKEY > 同目录 sckey.txt
"""
import os, json, urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "sckey.txt")
API_TPL = "https://sctapi.ftqq.com/{key}.send"


def load_sendkey():
    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        try:
            return open(KEY_FILE, encoding="utf-8").read().strip()
        except Exception:
            pass
    return ""


def push(title, markdown, key=None):
    key = (key or load_sendkey()).strip()
    if not key:
        return False, "未配置 SendKey（sckey.txt 或环境变量 SERVERCHAN_SENDKEY）"
    data = urllib.parse.urlencode({"title": title[:100], "desp": markdown}).encode("utf-8")
    req = urllib.request.Request(
        API_TPL.format(key=key), data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Mozilla/5.0 wechat-push/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore") if hasattr(e, "read") else ""
        return False, "HTTP %s: %s" % (e.code, detail[:300])
    except Exception as e:
        return False, "网络错误: %s" % e
    try:
        j = json.loads(body)
        if j.get("code") not in (0, None):
            return False, "接口返回: %s" % body[:300]
    except Exception:
        pass
    return True, "推送成功"
