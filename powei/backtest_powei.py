#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
破位后上涨模型 · 历史回测
============================================================================
针对每个「假破位→干净长阳收复」信号，比较两种买法在未来 3 / 5 / 10 个交易日的
表现（准确率=收益率>0 的胜率；以及平均/中位数收益率等）：

  买法 A「收复日就买入」：收复日(sig_day) 收盘确认后，以当日收盘价买入
                          （无未来函数：信号收盘才出现，当天收盘成交）
  买法 B「第三天开盘买入」：提示日(sig_day) +2 日开盘价买入
                          （即操作策略中"上影/绿阴则第三日开盘低位买"）

持有期 h∈{3,5,10}：从买入日算起，到第 h 个交易日收盘卖出。
  收益 = 卖出收盘 / 买入价 - 1

说明：
  - 仅统计「未来 h 日真实存在」（idx+h 或 buy_idx+h 不越界）的事件，确保无前视。
  - 未含交易手续费（如需可后续加 ~0.1%~0.3%）。
  - 准确率口径与先前研究报告一致：收益率>0 占比（胜率）。
============================================================================
"""
import argparse, json, os, sys, time, datetime, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "common"))
import scan
import common_lib as cm

HORIZONS = [3, 5, 10]
KLINE_COUNT = 330
KLINE_BATCH_SIZE = 200


def _load_market_bars(stocks):
    """按批读取共同前复权行情库，转换为 scan.detect 的字段口径。"""
    codes = [scan._bare(s["symbol"]) for s in stocks]
    stored = cm.MARKET_STORE.load_many(codes, count=KLINE_COUNT)
    out = {}
    for s in stocks:
        code = scan._bare(s["symbol"])
        out[s["symbol"]] = [{
            "day": b["date"], "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"], "volume": b["volume"],
        } for b in stored.get(code, [])]
    return out


def _bars_for(s, bars_map):
    if bars_map is None:
        bars_map = _load_market_bars([s])
    return bars_map.get(s["symbol"])

def worker(s, bars_map=None):
    code = s['symbol']; name = s.get('name', '')
    d = _bars_for(s, bars_map)
    if not d:
        return []
    for x in d:
        for k in ('open', 'high', 'low', 'close', 'volume'):
            try: x[k] = float(x[k])
            except: return []
    N = len(d)
    evs = scan.detect(d)
    out = []
    for e in evs:
        idx = None
        for i, b in enumerate(d):
            if b['day'] == e['sig_day']:
                idx = i; break
        if idx is None:
            continue
        out.append({'code': code, 'name': name, 'idx': idx, 'N': N,
                    'support': e['support'], 'break_day': e['break_day'],
                    'sig_day': e['sig_day'], 'gap': e['break_to_sig'],
                    'vol_ratio': e['vol_ratio']})
    return out

def calc_returns(events):
    """对每事件算两种买法 × 3 个持有期的收益（None=未来K线不足）。"""
    rows = []
    for e in events:
        d = None  # 需要重新取该股票 K 线？——为避免膨胀，改为在 worker 直接算收益
        pass
    return rows

def worker_ret(s, bars_map=None):
    """从预载 K 线检测并直接算两种买法各持有期收益。"""
    code = s['symbol']; name = s.get('name', '')
    d = _bars_for(s, bars_map)
    if not d:
        return []
    for x in d:
        for k in ('open', 'high', 'low', 'close', 'volume'):
            try: x[k] = float(x[k])
            except: return []
    N = len(d)
    evs = scan.detect(d)
    out = []
    for e in evs:
        idx = None
        for i, b in enumerate(d):
            if b['day'] == e['sig_day']:
                idx = i; break
        if idx is None:
            continue
        # 买点量能是否符合（操作策略口径）
        try:
            op = scan.op_plan(d, idx)
            buy_vol_ok = op.get('buy_vol_ok')
        except Exception:
            buy_vol_ok = None
        # 二次确认（二次提示点）
        sc = e.get('second_confirm')
        has_sc = isinstance(sc, dict)
        sc_gap = sc['gap'] if has_sc else None
        rec = {'code': code, 'name': name, 'sig_day': e['sig_day'],
               'support': e['support'], 'gap': e['break_to_sig'],
               'vol_ratio': e['vol_ratio'], 'age': N - 1 - idx,
               'buy_vol_ok': buy_vol_ok,
               'has_sc': has_sc,
               'sc_day': sc['day'] if has_sc else None,
               'sc_gap': sc_gap,
               'sc_chg': sc['chg'] if has_sc else None,
               'sc_vol': sc['vol_ratio'] if has_sc else None}
        # 买法 A：收复日收盘买
        pa = d[idx]['close']
        # 买法 B：第三天开盘买(idx+2)
        pb = d[idx + 2]['open'] if idx + 2 <= N - 1 else None
        # 买法 C：二次确认日收盘买（仅对带确认样本；验证"行动于确认"）
        pc_idx = (idx + sc_gap) if has_sc else None
        pc = d[pc_idx]['close'] if (pc_idx is not None and pc_idx <= N - 1) else None
        for h in HORIZONS:
            # A: 卖出 idx+h
            if idx + h <= N - 1:
                rec['A_ret_%d' % h] = d[idx + h]['close'] / pa - 1
            else:
                rec['A_ret_%d' % h] = None
            # B: 买入 idx+2，卖出 idx+2+h
            if pb is not None and idx + 2 + h <= N - 1:
                rec['B_ret_%d' % h] = d[idx + 2 + h]['close'] / pb - 1
            else:
                rec['B_ret_%d' % h] = None
            # C: 仅带确认样本，买入确认日收盘，卖出 确认日+h
            if pc is not None and pc_idx + h <= N - 1:
                rec['C_ret_%d' % h] = d[pc_idx + h]['close'] / pc - 1
            else:
                rec['C_ret_%d' % h] = None
        out.append(rec)
    return out

def summarize(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    n = len(vals)
    win = sum(1 for v in vals if v > 0)
    gt5 = sum(1 for v in vals if v > 0.05)
    loss = sum(1 for v in vals if v < 0)
    return {
        'n': n,
        'win_rate': win / n,
        'mean': statistics.mean(vals),
        'median': statistics.median(vals),
        'gt5_rate': gt5 / n,
        'loss_rate': loss / n,
        'min': min(vals),
        'max': max(vals),
    }

def fmt(s):
    if s is None:
        return '—'
    return ('%d 个 | 胜率 %.1f%% | 均 %.2f%% | 中位 %.2f%% | >+5%% %.1f%% | 亏损 %.1f%% | 区间 [%.1f%%, %.1f%%]'
            % (s['n'], s['win_rate'] * 100, s['mean'] * 100, s['median'] * 100,
               s['gt5_rate'] * 100, s['loss_rate'] * 100, s['min'] * 100, s['max'] * 100))

def run(limit=None, workers=16, sample_only=False):
    if sample_only:
        # 只用 3 个已知样本做逻辑自测，股票列表也不访问网络。
        stocks = [{'symbol': 'sh688306', 'name': '均普智能'},
                  {'symbol': 'sz300528', 'name': '幸福蓝海'},
                  {'symbol': 'sz301395', 'name': '仁信新材'}]
    else:
        rows = cm.get_market_universe()
        stocks = [{'symbol': cm.tx_secid_of(r['code']), 'name': r.get('name', '')}
                  for r in rows if r.get('code', '').startswith(('0', '3', '6'))]
        lixin = scan.load_lixin()
        stocks = [s for s in stocks if scan._bare(s['symbol']) not in lixin]
        stocks = [s for s in stocks if 'ST' not in s.get('name', '').upper()]
    if limit:
        stocks = stocks[:limit]
    scan.log('回测扫描 %d 只（已剔 ST/立信，行情仅读 market_daily）' % len(stocks))

    all_recs = []
    loaded = 0
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for offset in range(0, len(stocks), KLINE_BATCH_SIZE):
            chunk = stocks[offset:offset + KLINE_BATCH_SIZE]
            bars_map = _load_market_bars(chunk)
            loaded += sum(1 for s in chunk if bars_map.get(s['symbol']))
            futs = [ex.submit(worker_ret, s, bars_map) for s in chunk]
            for f in as_completed(futs):
                done += 1
                all_recs += f.result()
                if done % 500 == 0:
                    scan.log('进度', done, '/', len(stocks), '信号', len(all_recs), '%.0fs' % (time.time() - t0))
    scan.log('共同行情库命中 %d / %d 只' % (loaded, len(stocks)))
    scan.log('检测完成：%d 个信号事件，耗时 %.1fs' % (len(all_recs), time.time() - t0))

    # 有效事件（至少 3 日未来K线，A法可用）
    valid = [r for r in all_recs if any(r.get('A_ret_%d' % h) is not None for h in HORIZONS)]
    scan.log('有效回测事件（含足够未来K线）：%d 个' % len(valid))
    # 二次确认分层的基础统计
    sc_yes = [r for r in valid if r.get('has_sc')]
    sc_no  = [r for r in valid if not r.get('has_sc')]
    scan.log('  其中带二次确认：%d 个；不带确认：%d 个' % (len(sc_yes), len(sc_no)))
    # 买法 C（行动于确认）有效事件：须确认日存在且足够未来K线
    valid_c = [r for r in valid if any(r.get('C_ret_%d' % h) is not None for h in HORIZONS)]
    scan.log('买法C(二次确认日买)可用事件：%d 个' % len(valid_c))

    methods = {'A_收复日收盘买': 'A', 'B_第三天开盘买': 'B', 'C_二次确认日收盘买': 'C'}
    print('\n================ 回测结果（准确率=收益率>0 胜率）================')
    print('持有期        | 信号数 | 胜率   | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 | 收益区间')
    print('-' * 95)
    md = ['# 破位后上涨模型 · 历史回测（收复日买 vs 第三天开盘买 vs 二次确认日买）',
          '',
          '> 准确率口径：持有期收益率 > 0 的占比（胜率）。未含交易手续费。',
          '> 仅统计未来 K 线足够的信号事件。',
          '',
          '## 汇总（胜率 / 平均 / 中位 / 盈利>5% / 亏损占比 / 区间）', '']
    for mname, mp in methods.items():
        md.append('### %s' % mname)
        md.append('')
        md.append('| 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 | 收益区间 |')
        md.append('|---|---|---|---|---|---|---|---|')
        for h in HORIZONS:
            key = '%s_ret_%d' % (mp, h)
            s = summarize([r[key] for r in valid])
            if s is None:
                line = '%s %d日 |  无  |' % (mname, h)
                md.append('| %d日 | - | - | - | - | - | - | - |' % h)
                continue
            print('%-12s | %5d | %5.1f%% | %7.2f%% | %7.2f%% | %5.1f%% | %5.1f%% | [%.1f%%, %.1f%%]' % (
                mname[:12], s['n'], s['win_rate'] * 100, s['mean'] * 100, s['median'] * 100,
                s['gt5_rate'] * 100, s['loss_rate'] * 100, s['min'] * 100, s['max'] * 100))
            md.append('| %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% | %.1f%% | [%.1f%%, %.1f%%] |' % (
                h, s['n'], s['win_rate'] * 100, s['mean'] * 100, s['median'] * 100,
                s['gt5_rate'] * 100, s['loss_rate'] * 100, s['min'] * 100, s['max'] * 100))
        md.append('')
    print('\n说明：A=收复日收盘买；B=提示日+2日(第三天)开盘买；C=二次确认日收盘买(仅带确认样本)。')

    # 写出 markdown
    today = datetime.date.today().strftime('%Y-%m-%d')
    md_path = os.path.join(HERE, '回测_%s.md' % today)
    json_path = os.path.join(HERE, '回测_raw_%s.json' % today)
    hdr = ['## 样本概况',
           '- 扫描股票（剔ST/立信）：%d 只' % len(stocks),
           '- 检测到的信号事件：%d 个' % len(all_recs),
           '- 有效回测事件（含足够未来K线）：%d 个（其中带二次确认 %d 个，不带 %d 个）' % (len(valid), len(sc_yes), len(sc_no)),
           '- 持有期：3 / 5 / 10 个交易日',
           '- 买点 A：收复日(sig_day) 收盘价；买点 B：sig_day+2 开盘价',
           '- 买点 C：二次确认日 收盘价（仅带确认样本，验证"行动于确认"）',
           '- 二次确认：收复后1~5日内再现放量上涨（收涨≥3%）',
           '- 卖出：持有期末交易日收盘价；未含手续费',
           '']

    # ---------- 配对对比：同一信号 A vs B ----------
    pair = ['## 配对对比（同一信号：A 收益 − B 收益）', '']
    pair.append('| 持有期 | 样本数 | A优于B占比 | 平均差(A−B) | 中位差 | 解读 |')
    pair.append('|---|---|---|---|---|---|')
    for h in HORIZONS:
        diffs = []
        for r in valid:
            a = r.get('A_ret_%d' % h); b = r.get('B_ret_%d' % h)
            if a is not None and b is not None:
                diffs.append(a - b)
        if diffs:
            n = len(diffs)
            better = sum(1 for v in diffs if v > 0) / n
            mean_d = statistics.mean(diffs)
            med_d = statistics.median(diffs)
            verdict = 'A(收复日买)更优' if mean_d > 0 else 'B(第三天买)更优'
            pair.append('| %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %s |' % (
                h, n, better * 100, mean_d * 100, med_d * 100, verdict))
    pair.append('')

    # ---------- 分层：按 破位→收复间隔 gap ----------
    def layer(title, keyfn, buckets):
        block = ['## %s' % title, '']
        block.append('| 分层 | 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% |')
        block.append('|---|---|---|---|---|---|---|')
        for blabel, predicate in buckets:
            subset = [r for r in valid if predicate(r)]
            if not subset:
                continue
            for h in HORIZONS:
                s = summarize([r.get('%s_ret_%d' % (mk, h)) for r in subset for mk in ('A',)])
                # 仅展示 A 法分层（两种买法结论一致，以 A 代表）
                if s is None:
                    continue
                block.append('| %s | %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% |' % (
                    blabel, h, s['n'], s['win_rate'] * 100, s['mean'] * 100,
                    s['median'] * 100, s['gt5_rate'] * 100))
        block.append('')
        return block

    layers = []
    layers += layer('分层①：破位→收复间隔 gap（越快越优）',
                    None,
                    [('gap≤3（极快回）', lambda r: (r['gap'] or 99) <= 3),
                     ('gap 4~6（快回）', lambda r: 4 <= (r['gap'] or 99) <= 6),
                     ('gap 7~10（慢回）', lambda r: 7 <= (r['gap'] or 99) <= 10)])
    layers += layer('分层②：收复日量比 vol_ratio',
                    None,
                    [('缩量收复 <1.0', lambda r: (r['vol_ratio'] or 9) < 1.0),
                     ('温和放量 1~2', lambda r: 1.0 <= (r['vol_ratio'] or 9) < 2.0),
                     ('明显放量 ≥2', lambda r: (r['vol_ratio'] or 0) >= 2.0)])
    layers += layer('分层③：买点量能是否符合（操作策略口径）',
                    None,
                    [('量能符合(买点<提示日)', lambda r: r.get('buy_vol_ok') is True),
                     ('量能不符(买点≥提示日)', lambda r: r.get('buy_vol_ok') is False),
                     ('待判(N/A)', lambda r: r.get('buy_vol_ok') is None)])

    # 黄金组合：gap≤3 且 量能符合
    gold = [r for r in valid if (r['gap'] or 99) <= 3 and r.get('buy_vol_ok') is True]
    gold_block = ['## 黄金组合：gap≤3 且 买点量能符合（最接近用户黄金样本）', '']
    gold_block.append('| 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 |')
    gold_block.append('|---|---|---|---|---|---|---|')
    for h in HORIZONS:
        s = summarize([r.get('A_ret_%d' % h) for r in gold])
        if s:
            gold_block.append('| %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% | %.1f%% |' % (
                h, s['n'], s['win_rate'] * 100, s['mean'] * 100, s['median'] * 100,
                s['gt5_rate'] * 100, s['loss_rate'] * 100))
    gold_block.append('')

    # ---------- 二次确认（二次提示点）专项分析 ----------
    sc_block = ['## 二次确认（二次提示点）专项：带确认 vs 不带确认', '']
    sc_block.append('> 假设：收复后 1~5 日内再现「放量上涨（收涨≥3%）」的二次确认，'
                    '该样本后续上涨概率更高，应列为第一优先级。下表以买法 A（收复日收盘买）为基准对比两组。')
    sc_block.append('')
    sc_block.append('### 带二次确认 vs 不带确认（买法 A）')
    sc_block.append('')
    sc_block.append('| 分组 | 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 |')
    sc_block.append('|---|---|---|---|---|---|---|---|')
    for grp_label, grp in [('带二次确认', sc_yes), ('不带确认', sc_no)]:
        for h in HORIZONS:
            s = summarize([r.get('A_ret_%d' % h) for r in grp])
            if s:
                sc_block.append('| %s | %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% | %.1f%% |' % (
                    grp_label, h, s['n'], s['win_rate'] * 100, s['mean'] * 100,
                    s['median'] * 100, s['gt5_rate'] * 100, s['loss_rate'] * 100))
    sc_block.append('')

    # 买法 C：行动于确认日（仅带确认样本）
    sc_block.append('### 买法 C：二次确认日收盘买入（仅对带确认样本；第一优先级的实际买点）')
    sc_block.append('')
    sc_block.append('| 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 | 收益区间 |')
    sc_block.append('|---|---|---|---|---|---|---|---|')
    for h in HORIZONS:
        s = summarize([r.get('C_ret_%d' % h) for r in valid_c])
        if s:
            sc_block.append('| %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% | %.1f%% | [%.1f%%, %.1f%%] |' % (
                h, s['n'], s['win_rate'] * 100, s['mean'] * 100, s['median'] * 100,
                s['gt5_rate'] * 100, s['loss_rate'] * 100, s['min'] * 100, s['max'] * 100))
    sc_block.append('')
    sc_block.append('> 注：买法C在「确认日收盘」买入，已消化确认日的放量上涨本身，'
                    '故其收益 = 确认日之后（T+1起）的延续性，与A/B口径不同，仅供第一优先级实操参考。')
    sc_block.append('')

    # 扩展黄金组合：gap≤3 & 量能符合 & 带二次确认
    gold_sc = [r for r in valid if (r['gap'] or 99) <= 3
               and r.get('buy_vol_ok') is True and r.get('has_sc')]
    sc_block.append('### 扩展黄金组合：gap≤3 且 买点量能符合 且 带二次确认（最强样本）')
    sc_block.append('')
    sc_block.append('| 持有期 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈利>5% | 亏损占比 |')
    sc_block.append('|---|---|---|---|---|---|---|')
    for h in HORIZONS:
        s = summarize([r.get('A_ret_%d' % h) for r in gold_sc])
        if s:
            sc_block.append('| %d日 | %d | %.1f%% | %.2f%% | %.2f%% | %.1f%% | %.1f%% |' % (
                h, s['n'], s['win_rate'] * 100, s['mean'] * 100,
                s['median'] * 100, s['gt5_rate'] * 100, s['loss_rate'] * 100))
    sc_block.append('')

    open(md_path, 'w').write('\n'.join(hdr + md + pair + layers + gold_block + sc_block))
    json.dump(valid, open(json_path, 'w'), ensure_ascii=False)
    scan.log('回测报告：', md_path)
    scan.log('原始数据：', json_path, '(%d 事件)' % len(valid))
    return valid


def run_for_layer_demo():
    pass

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--workers', type=int, default=16)
    ap.add_argument('--sample', action='store_true', help='仅用3个已知样本验证逻辑')
    a = ap.parse_args()
    run(limit=a.limit, workers=a.workers, sample_only=a.sample)
