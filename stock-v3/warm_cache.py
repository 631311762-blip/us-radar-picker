# -*- coding: utf-8 -*-
"""K线缓存预热：把候选池缺的K线用小并发+多轮重试磨满，增量落盘。
用于行情接口被限流时段（如 2026-11-02），保证次日 15:20 定时任务秒级出榜。
用法：python warm_cache.py [最长分钟数，默认90]
"""
import json, os, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
import datafetch

CACHE_F = os.path.join(HERE, "kline_cache.json")
MAX_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 90
deadline = time.time() + MAX_MIN * 60


def load():
    try:
        d = json.load(open(CACHE_F, encoding="utf-8"))
        return d.get("data") or {}, d.get("key")
    except Exception:
        return {}, None


def save(cache):
    last_d = max((v[-1]["date"] for v in cache.values() if v), default=None)
    # 原子写：直接 dump 到目标文件时，进程被 kill 会留下半截 JSON（2026-11-02 发生过，
    # 42MB 缓存整份报废、次日必须全量重拉）
    tmp = CACHE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(dict(key=last_d, data=cache), fp)
    os.replace(tmp, CACHE_F)


def main():
    pool = datafetch.universe()
    codes = [p["code"] for p in pool]
    print(f"[warm] 候选池 {len(codes)} 只，最长运行 {MAX_MIN} 分钟", flush=True)
    rnd = 0
    stall, prev_missing = 0, len(codes) + 1
    while time.time() < deadline:
        cache, _ = load()
        missing = [c for c in codes if c not in cache]
        if not missing:
            print(f"[warm] 第{rnd}轮：缓存已满 {len(cache)} 只，完成", flush=True)
            return 0
        rnd += 1
        print(f"[warm] 第{rnd}轮：缺 {len(missing)} 只，6 路并发补拉...", flush=True)
        got, t0 = 0, time.time()

        def fetch(c):
            return c, datafetch.kline(c, 320)

        batch = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for c, b in ex.map(fetch, missing):
                got += 1
                if b:
                    batch.append((c, b))
                if got % 100 == 0:
                    for cc, bb in batch:
                        cache[cc] = bb
                    batch = []
                    save(cache)
                    print(f"    进度 {got}/{len(missing)}，缓存 {len(cache)} 只，"
                          f"历时 {time.time()-t0:.0f}s", flush=True)
        for cc, bb in batch:
            cache[cc] = bb
        save(cache)
        print(f"[warm] 第{rnd}轮结束：缓存 {len(cache)} 只", flush=True)
        remain = [c for c in codes if c not in cache]
        if not remain:
            print("[warm] 全部补齐", flush=True)
            return 0
        # 连续两轮毫无进展就退出（例如北交所通道被墙时永远补不齐，
        # 原逻辑会空转满 100 分钟并每 100 只重写一次 120MB 缓存）
        if len(remain) >= prev_missing:
            stall += 1
        else:
            stall = 0
        prev_missing = len(remain)
        if stall >= 2:
            print(f"[warm] 连续 {stall} 轮无进展，退出（仍缺 {len(remain)} 只，"
                  f"多为北交所/停牌，不影响出榜）", flush=True)
            return 0
        if time.time() < deadline:
            print(f"[warm] 休息 20s 后进入下一轮（仍缺 {len(remain)}）", flush=True)
            time.sleep(20)
    print("[warm] 到达时限退出，未补齐部分由下次运行继续", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
