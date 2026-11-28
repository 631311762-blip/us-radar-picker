# -*- coding: utf-8 -*-
"""
黄金样本模型 · 统一选股流水线
================================
整合主策略V1.2 + 精选池V1.3，全市场候选扫描→分级→生成报告→PushPlus微信推送

用法:
  python3.11 pipeline.py                 # 扫描最新交易日，选股+推送
  python3.11 pipeline.py --date 20261127 # 指定提示日
  python3.11 pipeline.py --dry-run       # 只选股生成报告，不推送（测试用）
  python3.11 pipeline.py --no-fetch      # 用缓存数据，不重新拉取候选
"""
import akshare as ak, pandas as pd, numpy as np, warnings, pickle, json, os, sys, argparse, time, requests
from datetime import datetime, timedelta
warnings.filterwarnings('ignore')

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
CACHE = os.path.join(BASE, 'cand_data.pkl')
LOG = os.path.join(BASE, 'pipeline.log')

def log(msg):
    line = f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f: f.write(line+'\n')

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f: return json.load(f)

# ---------- 交易日历 ----------
def get_trade_dates():
    try:
        df = ak.tool_trade_date_hist_sina()
        return [pd.Timestamp(d).strftime('%Y-%m-%d') for d in df['trade_date']]
    except Exception as e:
        log(f'交易日历获取失败 {e}，用工作日近似')
        return None

def latest_trade_day(tds, ref=None):
    ref = pd.Timestamp(ref or datetime.now()).strftime('%Y-%m-%d')
    if tds:
        past = [d for d in tds if d <= ref]
        return past[-1] if past else ref
    d = pd.Timestamp(ref)
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d.strftime('%Y-%m-%d')

# ---------- 候选池 ----------
def build_candidates(tip_date, tds, cfg):
    """大涨日需落在提示日前3-6个交易日，用涨停池+强势股池覆盖"""
    cands = {}
    if tds and tip_date in tds:
        idx = tds.index(tip_date)
        b_dates = [tds[idx-k] for k in range(3, 7)]   # 前3~6个交易日
    else:
        d = pd.Timestamp(tip_date)
        b_dates = []
        k = 1
        while len(b_dates) < 6:
            d -= timedelta(days=1)
            if d.weekday() < 5: b_dates.append(d.strftime('%Y-%m-%d'))
        b_dates = b_dates[2:6]
    log(f'候选大涨日: {b_dates}')
    for ds in b_dates:
        d8 = ds.replace('-', '')
        for fn in ['stock_zt_pool_em', 'stock_zt_pool_strong_em']:
            for attempt in range(2):
                try:
                    df = getattr(ak, fn)(date=d8)
                    for _, r in df.iterrows():
                        code = str(r['代码']).zfill(6)
                        cands[code] = r.get('名称', '')
                    break
                except Exception as e:
                    if attempt == 0: time.sleep(1)
    log(f'候选集 {len(cands)} 只')
    return cands, b_dates

def fetch_data(cands, tip_date, no_fetch=False):
    data = {}
    if no_fetch and os.path.exists(CACHE):
        with open(CACHE, 'rb') as f: data = pickle.load(f)
        log(f'复用缓存 {len(data)} 只')
        return data
    if os.path.exists(CACHE) and not no_fetch:
        try:
            with open(CACHE, 'rb') as f: cache = pickle.load(f)
        except: cache = {}
    else: cache = {}
    end = tip_date.replace('-', '')
    start = (pd.Timestamp(tip_date) - timedelta(days=100)).strftime('%Y%m%d')
    new = [c for c in cands if c not in cache]
    log(f'需拉取 {len(new)} 只（缓存已有 {len(cache)} 只）')
    for i, code in enumerate(new):
        sym = ('sh' if code.startswith('6') else 'sz') + code
        try:
            df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust='qfq')
            if df is not None and len(df) >= 40:
                df['code'] = code
                cache[code] = df
        except Exception:
            pass
        if (i+1) % 50 == 0: log(f'  拉取进度 {i+1}/{len(new)}')
        time.sleep(0.05)
    with open(CACHE, 'wb') as f: pickle.dump(cache, f)
    # 只保留候选集
    data = {c: cache[c] for c in cands if c in cache}
    log(f'有效数据 {len(data)} 只')
    return data

# ---------- 选股核心 ----------
def enrich(df):
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    c = df['close']
    df['pct'] = c.pct_change() * 100
    df['vma5'] = df['volume'].rolling(5).mean()
    df['vr'] = df['volume'] / df['vma5']
    return df

P_BASE = dict(cv=0.09, amp=0.30, brk=9.0, vr=1.5, gap=(3,6), vcv=0.50)
P_LEAN = dict(body_min=0.5, depth_lo=20, depth_hi=150, tip_lo=-2, tip_hi=3)
# V1.4 第4闸门：大涨日前15日箱体震荡（含双底）+ 下跌萎靡
P_WANE = dict(amp15=28.0, cv15=5.0, shrink=1.0)

def wane_metrics(d, b):
    """大涨日前15日形态：箱体振幅/CV + 量能萎缩比"""
    if b < 15: return None
    w = d.iloc[b-15:b]
    if len(w) < 15: return None
    c, v = w['close'], w['volume']
    amp15 = (w['high'].max()-w['low'].min())/w['low'].min()*100
    cv15 = c.std()/c.mean()*100
    shrink = v.iloc[-5:].mean()/v.iloc[:5].mean() if v.iloc[:5].mean() > 0 else 9
    return dict(amp15=amp15, cv15=cv15, shrink=shrink)

def wane_pass(wm):
    if wm is None: return False
    return wm['amp15'] <= P_WANE['amp15'] and wm['cv15'] <= P_WANE['cv15'] and wm['shrink'] < P_WANE['shrink']

def scan_stock(df, tip_date, name=''):
    """返回该股在tip_date的命中信号（含分级），无则None"""
    d = enrich(df); n = len(d)
    if n < 25: return None
    for b in range(20, n):
        br = d.iloc[b]
        if not (br['pct'] >= P_BASE['brk'] and br['vr'] >= P_BASE['vr']): continue
        body = abs(br['close'] - br['open'])
        if body <= 0: continue
        mid = (br['open'] + br['close']) / 2
        w = d.iloc[b-20:b]
        if len(w) < 20: continue
        if w['close'].std()/w['close'].mean() > P_BASE['cv']: continue
        if (w['close'].max()-w['close'].min())/w['close'].min() > P_BASE['amp']: continue
        for t in range(b+3, b+7):
            if t >= n: continue
            tr = d.iloc[t]
            if tr['date'].strftime('%Y-%m-%d') != tip_date: continue
            depth = (mid - tr['low']) / body * 100
            if depth < 0 or not (tr['close'] >= tr['open']): continue
            m = d.iloc[b+1:t+1]
            if len(m) < 3 or m['volume'].std()/m['volume'].mean() > P_BASE['vcv']: continue
            tpct = (tr['close']/d.iloc[t-1]['close']-1)*100
            # 四闸门判定（V1.3三闸门 + V1.4前15日箱体萎靡）
            g1 = P_LEAN['tip_lo'] <= tpct <= P_LEAN['tip_hi']
            g2 = body >= P_LEAN['body_min']
            g3 = P_LEAN['depth_lo'] <= depth <= P_LEAN['depth_hi']
            wm = wane_metrics(d, b)
            g4 = wane_pass(wm)
            is_lean = g1 and g2 and g3 and g4
            # 追高风险分级（按提示日涨幅）
            risk = '🟢低' if tpct < 5 else ('🟡中' if tpct < 8 else '🔴高')
            return dict(
                代码=str(d['code'].iloc[0]).zfill(6), 名称=name,
                大涨日=d.iloc[b]['date'].strftime('%m-%d'),
                大涨涨幅=round(br['pct'], 1), 量比=round(br['vr'], 2),
                间隔=t-b, 下探深度=round(depth, 1), 实体=round(body, 2),
                提示日涨幅=round(tpct, 2), 量CV=round(m['volume'].std()/m['volume'].mean()*100, 1),
                提示日收=round(tr['close'], 2), 提示日开=round(tr['open'], 2),
                前15振幅=round(wm['amp15'], 1) if wm else None,
                前15CV=round(wm['cv15'], 2) if wm else None,
                萎缩比=round(wm['shrink'], 3) if wm else None,
                级别='精选池' if is_lean else '主策略', 风险=risk,
                闸门1='过' if g1 else '拦', 闸门2='过' if g2 else '拦', 闸门3='过' if g3 else '拦',
                闸门4='过' if g4 else '拦',
            )
    return None

def run_selection(data, cands, tip_date, cfg):
    sigs = []
    for code, df in data.items():
        r = scan_stock(df, tip_date, cands.get(code, ''))
        if r: sigs.append(r)
    df = pd.DataFrame(sigs)
    if len(df):
        # 精选池优先，其次按下探深度
        df['_o'] = df['级别'].map({'精选池':0, '主策略':1})
        df = df.sort_values(['_o','下探深度'], ascending=[True,False]).reset_index(drop=True)
        df = df.drop(columns=['_o'])
    return df

# ---------- 报告生成 ----------
def gen_html(df, tip_date, cfg):
    n_lean = (df['级别']=='精选池').sum() if len(df) else 0
    n_base = (df['级别']=='主策略').sum() if len(df) else 0
    buy_day = ''
    if len(df):
        try:
            tds = get_trade_dates()
            if tds and tip_date in tds:
                buy_day = tds[tds.index(tip_date)+1]
        except: pass
    def rows_of(level):
        if not len(df): return ''
        sub = df[df['级别']==level]
        out = ''
        for _, r in sub.iterrows():
            gold = ' <span style="background:#fef3c7;color:#b45309;font-size:10px;padding:1px 5px;border-radius:4px">黄金样本</span>' if r['代码'] in ['603887','002226','600186'] else ''
            base = (f'<tr><td>{r["代码"]}</td><td style="text-align:left;font-weight:600">{r["名称"]}{gold}</td>'
                    f'<td>{r["大涨日"]}</td><td>{r["大涨涨幅"]}%</td><td style="color:#dc2626">{r["提示日涨幅"]}%</td>'
                    f'<td>{r["下探深度"]}%</td><td>{r["实体"]}元</td><td>{r["间隔"]}</td>')
            if level == '精选池':
                base += f'<td>{r.get("前15振幅","-")}%/{r.get("前15CV","-")}%</td><td>{r.get("萎缩比","-")}</td>'
            else:
                base += f'<td>{r["量CV"]}%</td>'
            out += base + f'<td>{r["风险"]}</td></tr>'
        return out
    lean_table = rows_of('精选池')
    base_table = rows_of('主策略')
    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>选股推送 {tip_date}</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f7fa;color:#1f2937;padding:16px;line-height:1.6}}
.wrap{{max-width:1000px;margin:0 auto}}h1{{font-size:20px;margin-bottom:4px}}h2{{font-size:16px;margin:16px 0 8px;padding-left:8px;border-left:4px solid #dc2626}}
.sub{{color:#6b7280;font-size:12px;margin-bottom:14px}}.card{{background:#fff;border-radius:10px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
table{{width:100%;border-collapse:collapse;font-size:12px}}th{{background:#f3f4f6;padding:7px 4px;text-align:center;border-bottom:2px solid #e5e7eb;white-space:nowrap}}
td{{padding:6px 4px;text-align:center;border-bottom:1px solid #f0f1f3;white-space:nowrap}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}}
.kpi{{background:#f8fafc;border-radius:8px;padding:10px;text-align:center}}.kpi .v{{font-size:20px;font-weight:700;color:#dc2626}}.kpi .l{{font-size:11px;color:#6b7280}}
.note{{background:#eff6ff;border-left:4px solid #3b82f6;padding:10px 12px;border-radius:6px;font-size:12px;color:#1e40af;margin-bottom:12px}}
.empty{{background:#fef2f2;border-left:4px solid #dc2626;padding:12px;border-radius:6px;font-size:13px;color:#991b1b}}
.foot{{font-size:11px;color:#9ca3af;text-align:center;margin-top:12px}}
</style></head><body><div class="wrap">
<h1>黄金样本模型 · 每日选股推送<span style="background:#dc2626;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px">{tip_date}</span></h1>
<p class="sub">提示日={tip_date}（下探回升日）· 主策略V1.2 + 精选池V1.4（四闸门）· T+1开盘买入 → T+2高点卖出{f' · 买入日={buy_day}' if buy_day else ''}</p>
<div class="kpis">
<div class="kpi"><div class="v">{len(df)}</div><div class="l">总信号</div></div>
<div class="kpi"><div class="v">{n_lean}</div><div class="l">精选池</div></div>
<div class="kpi"><div class="v">{n_base}</div><div class="l">主策略</div></div>
<div class="kpi"><div class="v">{(df['风险'].str.contains('低')).sum() if len(df) else 0}</div><div class="l">低风险</div></div>
</div>'''
    if len(df) == 0:
        html += '<div class="empty"><b>今日无信号。</b>全市场扫描后没有符合模型的标的——满足条件的股票本就不多，空仓也是一种操作。请勿为凑数而追高。</div>'
    else:
        if n_lean:
            html += f'<h2>精选池 V1.4（四闸门全过 · 低位未启动 · 优先）</h2><div class="card"><table><thead><tr><th>代码</th><th>名称</th><th>大涨日</th><th>大涨涨幅</th><th>提示日涨幅</th><th>下探深度</th><th>实体</th><th>间隔</th><th>前15振幅/CV</th><th>萎缩比</th><th>追高风险</th></tr></thead><tbody>{lean_table}</tbody></table></div>'
        if n_base:
            html += f'<h2>主策略 V1.2（放宽版 · 覆盖更广）</h2><div class="card"><table><thead><tr><th>代码</th><th>名称</th><th>大涨日</th><th>大涨涨幅</th><th>提示日涨幅</th><th>下探深度</th><th>实体</th><th>间隔</th><th>量CV</th><th>追高风险</th></tr></thead><tbody>{base_table}</tbody></table></div>'
    html += '''<div class="note"><b>说明：</b>精选池=四闸门全过（提示日涨幅-2~3%、实体≥0.5元、深度20-150%、大涨前15日箱体振幅≤28%+CV≤5%+量能萎缩比&lt;1），是低位未启动、主力吸筹的干净信号；主策略=V1.2放宽版覆盖更广。三个黄金样本（含莲花双底）规则下全部通过。追高风险按提示日当日涨幅分级，🔴高=当天已大涨需谨慎追高。<b>买入为次日开盘价，表中收盘价仅为参考。</b></div>
<div class="foot">黄金样本模型 V1.4 · 统一选股流水线自动生成</div></div></body></html>'''
    return html

def gen_push_content(df, tip_date):
    """生成微信友好的内联样式推送内容（PushPlus HTML模板兼容）"""
    n_lean = (df['级别']=='精选池').sum() if len(df) else 0
    n_base = (df['级别']=='主策略').sum() if len(df) else 0
    def table(level, title, color):
        if not len(df): return ''
        sub = df[df['级别']==level]
        if len(sub)==0: return ''
        h = f'<div style="font-size:15px;font-weight:bold;color:{color};margin:14px 0 6px;border-left:4px solid {color};padding-left:8px">{title}（{len(sub)}只）</div>'
        h += '<table style="width:100%;border-collapse:collapse;font-size:12px"><tr style="background:#f3f4f6">'
        for c in ['代码','名称','提示日涨幅','深度','实体','风险']:
            h += f'<th style="padding:5px 3px;border-bottom:2px solid #e5e7eb">{c}</th>'
        h += '</tr>'
        for _, r in sub.iterrows():
            rc = '#16a34a' if '低' in r['风险'] else ('#d97706' if '中' in r['风险'] else '#dc2626')
            h += f'<tr style="border-bottom:1px solid #f0f1f3"><td style="padding:5px 3px;text-align:center">{r["代码"]}</td>'
            h += f'<td style="padding:5px 3px;text-align:center">{r["名称"]}</td>'
            h += f'<td style="padding:5px 3px;text-align:center;color:#dc2626">{r["提示日涨幅"]}%</td>'
            h += f'<td style="padding:5px 3px;text-align:center">{r["下探深度"]}%</td>'
            h += f'<td style="padding:5px 3px;text-align:center">{r["实体"]}元</td>'
            h += f'<td style="padding:5px 3px;text-align:center;color:{rc}">{r["风险"]}</td></tr>'
        h += '</table>'
        return h
    parts = [f'''<div style="font-family:-apple-system,'PingFang SC',sans-serif;max-width:600px;margin:0 auto;color:#1f2937">
<div style="background:#dc2626;color:#fff;padding:12px 14px;border-radius:8px 8px 0 0">
<span style="font-size:17px;font-weight:bold">黄金样本模型 · 每日选股</span>
<div style="font-size:12px;margin-top:3px;opacity:.9">{tip_date} · 精选池{n_lean}只 / 主策略{n_base}只</div>
</div>
<div style="background:#fff;padding:14px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px">''']
    if len(df)==0:
        parts.append('<div style="background:#fef2f2;border-left:4px solid #dc2626;padding:12px;font-size:13px;color:#991b1b"><b>今日无信号。</b>没有符合模型的标的，空仓也是一种操作，勿为凑数追高。</div>')
    else:
        parts.append(table('精选池','精选池 V1.4（四闸门全过·优先）','#dc2626'))
        parts.append(table('主策略','主策略 V1.2（放宽版·覆盖广）','#d97706'))
    parts.append('''<div style="background:#eff6ff;border-left:4px solid #3b82f6;padding:10px;margin-top:12px;font-size:12px;color:#1e40af">
买入=提示日次日开盘价，表中收盘价仅供参考；追高风险按提示日当日涨幅分级。最高价为理想化卖点，实盘会打折。
</div>
<div style="font-size:11px;color:#9ca3af;text-align:center;margin-top:10px">黄金样本模型 V1.4 · 自动选股推送</div>
</div></div>''')
    return ''.join(parts)

# ---------- PushPlus 推送 ----------
def push_wechat(cfg, title, content, html_path):
    token = cfg['推送配置'].get('pushplus_token', '')
    if not token:
        log('⚠ PushPlus token 未配置，跳过推送（报告已本地生成）')
        return False, 'token未配置'
    api = cfg['推送配置'].get('pushplus_api', 'http://www.pushplus.plus/send')
    payload = {
        'token': token,
        'title': title,
        'content': content,
        'template': 'html',
    }
    try:
        resp = requests.post(api, json=payload, timeout=20)
        res = resp.json()
        if res.get('code') == 200:
            log(f'✅ PushPlus推送成功: {title}')
            return True, res
        else:
            log(f'❌ PushPlus推送失败: {res}')
            return False, res
    except Exception as e:
        log(f'❌ PushPlus推送异常: {e}')
        return False, str(e)

# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='提示日 YYYYMMDD 或 YYYY-MM-DD')
    ap.add_argument('--dry-run', action='store_true', help='不推送')
    ap.add_argument('--no-fetch', action='store_true', help='用缓存数据')
    args = ap.parse_args()
    cfg = load_config()
    log('=' * 60)
    log('黄金样本模型 · 统一选股流水线启动')

    tds = get_trade_dates()
    if args.date:
        tip_date = pd.Timestamp(args.date).strftime('%Y-%m-%d')
    else:
        tip_date = latest_trade_day(tds)
    log(f'提示日: {tip_date}')

    # 非交易日检查
    if cfg['推送配置'].get('非交易日跳过', True) and tds and tip_date not in tds:
        log(f'{tip_date} 非交易日，跳过')
        return

    cands, _ = build_candidates(tip_date, tds, cfg)
    if not cands:
        log('候选集为空，结束'); return
    data = fetch_data(cands, tip_date, args.no_fetch)
    df = run_selection(data, cands, tip_date, cfg)
    n_lean = (df['级别']=='精选池').sum() if len(df) else 0
    n_base = (df['级别']=='主策略').sum() if len(df) else 0

    # 保存结构化结果
    csv_path = os.path.join(BASE, f'selection_{tip_date.replace("-","")}.csv')
    df.to_csv(csv_path, index=False, encoding='utf-8-sig')

    # 生成报告
    html = gen_html(df, tip_date, cfg)
    html_path = os.path.join(BASE, f'选股推送_{tip_date.replace("-","")}.html')
    with open(html_path, 'w', encoding='utf-8') as f: f.write(html)
    log(f'选股完成: 总{len(df)}只 (精选池{(df["级别"]=="精选池").sum() if len(df) else 0} / 主策略{(df["级别"]=="主策略").sum() if len(df) else 0})')
    log(f'报告: {html_path}')

    # 推送（微信内联样式内容）
    if not args.dry_run:
        title = f'选股推送 {tip_date} · 精选池{n_lean}只/主策略{n_base}只'
        push_content = gen_push_content(df, tip_date)
        ok, res = push_wechat(cfg, title, push_content, html_path)
    else:
        log('dry-run 模式，未推送')
    log('流水线结束')

if __name__ == '__main__':
    main()
