#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
莲花模型 · 三黄金样本逐条校验
================================
用新浪真实前复权数据，逐条复核城地香江/江南化工/莲花控股是否通过 V1.4 全部闸门。
规则变更后必跑：三只全过、均进精选池，才算没破坏模型。

运行:  python3 verify.py
依赖:  akshare pandas numpy
"""
import akshare as ak, pandas as pd, warnings
warnings.filterwarnings('ignore')

SAMPLES = [
    ('城地香江', '603887', '2026-11-18', '2026-11-23'),
    ('江南化工', '002226', '2026-11-17', '2026-11-23'),
    ('莲花控股', '600186', '2026-11-18', '2026-11-26'),
]
# 与 config.json 保持一致
P_BASE = dict(cv=0.09, amp=0.30, brk=9.0, vr=1.5, gap=(3, 6), vcv=0.50)
P_LEAN = dict(body_min=0.5, depth_lo=20, depth_hi=150, tip_lo=-2, tip_hi=3)
P_WANE = dict(amp15=28.0, cv15=5.0, shrink=1.0)

def ok(c):
    return '✅过' if c else '❌拦'

def verify(name, code, bd, td):
    sym = ('sh' if code.startswith('6') else 'sz') + code
    start = (pd.Timestamp(bd) - pd.Timedelta(days=70)).strftime('%Y%m%d')
    end = (pd.Timestamp(td) + pd.Timedelta(days=10)).strftime('%Y%m%d')
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust='qfq')
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    df['pct'] = df['close'].pct_change() * 100
    df['vma5'] = df['volume'].rolling(5).mean()
    df['vr'] = df['volume'] / df['vma5']
    d = df
    bi = d.index[d['date'] == pd.Timestamp(bd)][0]
    ti = d.index[d['date'] == pd.Timestamp(td)][0]
    br, tr = d.iloc[bi], d.iloc[ti]

    print(f'\n{"=" * 64}\n{name} {code}  大涨日={bd}  提示日={td}  间隔={ti - bi}个交易日')
    print('-' * 64)

    # 阶段1 横盘
    w = d.iloc[bi - 20:bi]
    cv = w['close'].std() / w['close'].mean()
    amp = (w['close'].max() - w['close'].min()) / w['close'].min()
    print('【阶段1 大涨前20日横盘】')
    print(f'  收盘CV = {cv * 100:.2f}%  (≤9%)  {ok(cv <= P_BASE["cv"])}')
    print(f'  区间振幅 = {amp * 100:.2f}%  (≤30%)  {ok(amp <= P_BASE["amp"])}')

    # 阶段2 大涨
    body = abs(br['close'] - br['open'])
    mid = (br['open'] + br['close']) / 2
    print('【阶段2 大涨突破】')
    print(f'  涨幅 = {br["pct"]:.2f}%  (≥9%)  {ok(br["pct"] >= P_BASE["brk"])}')
    print(f'  量比 = {br["vr"]:.2f}  (≥1.5)  {ok(br["vr"] >= P_BASE["vr"])}')
    print(f'  实体 = {body:.3f}元  (>0)  {ok(body > 0)}   实体中位线 = {mid:.3f}')

    # 阶段3 下探
    depth = (mid - tr['low']) / body * 100
    gap_ok = P_BASE['gap'][0] <= ti - bi <= P_BASE['gap'][1]
    print('【阶段3 下探黄金坑】')
    print(f'  间隔 = {ti - bi}日  (3~6日)  {ok(gap_ok)}')
    print(f'  下探最低 = {tr["low"]:.3f} vs 中位线 {mid:.3f} → 深度 {depth:.1f}%  {ok(depth >= 0)}')
    print(f'  回升 收{tr["close"]:.3f} ≥ 开{tr["open"]:.3f}  {ok(tr["close"] >= tr["open"])}')

    # 阶段4 量CV
    m = d.iloc[bi + 1:ti + 1]
    vcv = m['volume'].std() / m['volume'].mean()
    print('【阶段4 量能过滤】')
    print(f'  量CV = {vcv * 100:.1f}%  (≤50%)  {ok(vcv <= P_BASE["vcv"])}')

    base_pass = (cv <= P_BASE['cv'] and amp <= P_BASE['amp'] and br['pct'] >= P_BASE['brk']
                 and br['vr'] >= P_BASE['vr'] and body > 0 and gap_ok and depth >= 0
                 and tr['close'] >= tr['open'] and vcv <= P_BASE['vcv'])
    print(f'  >>> 主策略V1.2: {"✅通过" if base_pass else "❌未通过"}')

    # 精选池四闸门
    tpct = (tr['close'] / d.iloc[ti - 1]['close'] - 1) * 100
    w15 = d.iloc[bi - 15:bi]
    amp15 = (w15['high'].max() - w15['low'].min()) / w15['low'].min() * 100
    cv15 = w15['close'].std() / w15['close'].mean() * 100
    shrink = w15['volume'].iloc[-5:].mean() / w15['volume'].iloc[:5].mean()
    g1 = P_LEAN['tip_lo'] <= tpct <= P_LEAN['tip_hi']
    g2 = body >= P_LEAN['body_min']
    g3 = P_LEAN['depth_lo'] <= depth <= P_LEAN['depth_hi']
    g4 = (amp15 <= P_WANE['amp15'] and cv15 <= P_WANE['cv15'] and shrink < P_WANE['shrink'])
    print('【精选池V1.4 四闸门】')
    print(f'  闸门1 提示日涨幅 = {tpct:.2f}%  (-2%~+3%)  {ok(g1)}')
    print(f'  闸门2 大涨日实体 = {body:.3f}元  (≥0.5)  {ok(g2)}')
    print(f'  闸门3 下探深度 = {depth:.1f}%  (20%~150%)  {ok(g3)}')
    print(f'  闸门4 前15日: 振幅{amp15:.2f}%({ok(amp15 <= P_WANE["amp15"])}) / '
          f'CV{cv15:.2f}%({ok(cv15 <= P_WANE["cv15"])}) / '
          f'萎缩比{shrink:.3f}({ok(shrink < P_WANE["shrink"])})  → {ok(g4)}')
    lean_pass = g1 and g2 and g3 and g4
    print(f'  >>> 精选池V1.4: {"✅四闸门全通过" if lean_pass else "❌未全通过"}')
    print(f'  >>> 最终级别: {"精选池" if lean_pass else ("主策略" if base_pass else "不命中")}')
    return base_pass, lean_pass

def main():
    results = [verify(*s) for s in SAMPLES]
    base_all = all(r[0] for r in results)
    lean_all = all(r[1] for r in results)
    print(f'\n{"=" * 64}')
    print(f'汇总: 主策略全通过 = {base_all}  |  精选池四闸门全通过 = {lean_all}')
    if base_all and lean_all:
        print('✅ 三黄金样本全部通过，模型规则完好。')
    else:
        print('❌ 有样本未通过，规则变更破坏了黄金样本，请检查！')
        raise SystemExit(1)

if __name__ == '__main__':
    main()
