#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日扫描：破位后上涨（假破位洗盘 / False-Breakdown Reclaim）选股脚本
============================================================================
逻辑：长期横盘形成水平支撑 S → 某日收盘破位(诱空) → 1~12 日内重新站回 S(收复)
      → 视为"刚收复支撑"的博反弹候选信号。

用法：
  全市场扫描(近3交易日收复)：   python3 scan.py
  限定股票数(快速测试)：         python3 scan.py --limit 600
  仅跑已知样本自检：             python3 scan.py --selftest
  自定义近期窗口/线程：          python3 scan.py --max-age 5 --workers 20
  输出目录：                     python3 scan.py --out /workspace

说明：
  - 数据来自新浪日线接口（免费、无需 token）。
  - 首次运行会拉取全 A 股票列表并缓存到 stocklist.json，之后复用。
  - 结果写入 Markdown + CSV（按日期命名），并在控制台打印摘要。
  - 可直接加入 crontab：收盘后每日运行一次（如 16:30）。
    例：30 16 * * 1-5 cd /workspace && /usr/bin/python3 scan.py >> scan.log 2>&1
============================================================================
"""
import argparse, json, os, sys, time, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE      = os.path.dirname(os.path.abspath(__file__))
LIST_CACHE= os.path.join(HERE, 'stocklist.json')
CACHE_DIR = os.path.join(HERE, '.scan_cache')
KLINE_URL = ('http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
             'CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=330')

# ===================== 模型参数（与研究报告 + 最新校准一致）=====================
WIN        = 50     # 支撑观察窗（交易日，约 3 个月，兼容 3 月级横盘）
RANGE_MAX  = 0.40   # 横盘最大振幅 (hi-lo)/mid
TOUCH_MIN  = 3      # 支撑最小触点次数
BREAK_PCT  = 0.98   # 破位阈值：收盘 < 支撑 * 0.98
RECLAIM_WIN= 12     # 破位后收复窗口（交易日）
RECLAIM_PCT= 1.00   # 收复阈值：收盘 >= 支撑 * 1.00
MAX_GAP    = 10     # 破位→收复间隔硬上限（交易日，不得大于10日=两周内必回）
RECLAIM_BODY_MIN = 0.03  # 收复日实体最小涨幅（长阳底线；排除小短线/十字/阴线收复）
RECLAIM_UPPER_MAX= 0.40  # 收复日上影/振幅 上限（排除长上影冲高回落假收复）
RECLAIM_FAIL_DROP = 0.05 # 【已废弃·兜底保留】旧规则：收盘较收复日收盘跌超5%剔除
RECLAIM_FAIL_FIB  = 0.618 # 收复后失效门槛（现行）：以「破位低点→收复高点」为一波，收盘跌破该波0.618黄金回撤位→收复失败，剔除
# ── 二次确认（二次提示点）── 收复后 1~WIN 日内出现「收涨≥UP + 放量」确认K线，
#    表明非单纯反弹、后续上涨概率更高（锚点：幸福蓝海11-06 / 仁信11-09 / 均普10-23）。
#    放量基准取并集：≥收复日量 或 ≥破位前20日均量×VOL（兼容收复日本身缩量/巨量两种情形）。
SECOND_CONFIRM_WIN = 5    # 二次确认扫描窗（收复后交易日）
SECOND_CONFIRM_UP  = 0.03 # 二次确认日收涨阈值（≥3%）
SECOND_CONFIRM_VOL = 1.2  # 二次确认放量基准（相对破位前20日均量倍数）
SECOND_CONFIRM_FRESH = 1  # 二次确认时效：确认日距今≤1交易日才列第一优先级（回测：确认日追高为负期望）
# ==========================================================================

# ---------------- 立信审计排除集（与流水线共同前置层口径一致）----------------
# 流水线已物理剔除 ST/退市/次新/立信；本地扫描器需自行消费该名单，避免把立信股推给用户。
import re as _re
_LIXIN_PATHS = [
    os.path.join(HERE, 'us-radar-picker', 'common', 'state', 'lixin_exclude.json'),
    os.path.join(HERE, 'common', 'state', 'lixin_exclude.json'),
    '/workspace/us-radar-picker/common/state/lixin_exclude.json',
]
def load_lixin():
    """返回立信审计排除集（bare 6 位代码集合）；名单缺失则空集合（仅告警，不阻断）。"""
    for p in _LIXIN_PATHS:
        if os.path.exists(p):
            try:
                codes = set(json.load(open(p)).get('codes', []))
                log('立信排除集已加载：%d 只（%s）' % (len(codes), os.path.basename(p)))
                return codes
            except Exception as e:
                log('[警告] 立信排除集读取失败 %s：%s' % (p, e))
    log('[警告] 立信排除集未找到，本次不做立信剔除（请检查 common/state/lixin_exclude.json）')
    return set()
def _bare(code):
    return _re.sub(r'^(sh|sz|bj)', '', code).strip()

def log(*a):
    print('[{}]'.format(datetime.datetime.now().strftime('%H:%M:%S')), *a, flush=True)

# ---------------- 股票列表 ----------------
def get_stocklist():
    if os.path.exists(LIST_CACHE):
        try:
            return json.load(open(LIST_CACHE))
        except: pass
    log('拉取全 A 股票列表...')
    items=[]
    for page in range(1, 60):
        url=('http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
             'Market_Center.getHQNodeData?page={}&num=100&sort=symbol&asc=1&node=hs_a&symbol='.format(page))
        try:
            r=requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=20)
            txt=r.text.strip()
            if txt in ('null','[]',''): break
            j=json.loads(txt)
            if not j: break
            items+=j
        except Exception as e:
            log('  列表页', page, '失败:', e); break
        if page % 10 == 0: log('  已获取', len(items))
    json.dump(items, open(LIST_CACHE,'w'))
    log('股票列表缓存:', len(items), '只 ->', LIST_CACHE)
    return items

# ---------------- 行情抓取（带缓存）----------------
import requests
def fetch_kline(code, use_cache=True):
    # 回测/缓存读取优先使用统一历史库；use_cache=True 时严格离线。
    if use_cache:
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', 'common')))
            import common_lib as cm
            bars = cm.load_kline(_bare(code), count=330, offline=True)
            if bars:
                return [{'day': b['date'], 'open': b['open'], 'high': b['high'],
                         'low': b['low'], 'close': b['close'],
                         'volume': b['volume'] * 100.0} for b in bars]
        except Exception:
            pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache=os.path.join(CACHE_DIR, code+'.json')
    if use_cache and os.path.exists(cache):
        try:
            d=json.load(open(cache)); 
            if d and len(d) >= 130: return d
        except: pass
    if use_cache:
        return None  # 回测缺数据时明确跳过，禁止回退外网
    try:
        r=requests.get(KLINE_URL.format(code=code), timeout=8)
        d=json.loads(r.text)
        if not d or len(d) < 130: return None
        if use_cache: json.dump(d, open(cache,'w'))
        return d
    except:
        return None

# ---------------- 形态检测 ----------------
def _support(win):
    """从一段 K 线窗口识别「多触点水平支撑地板」。返回 (S, 是否有效)。
    要求：窗口振幅 <= RANGE_MAX（真横盘）且 low 在地板 [S*0.98,S*1.03] 触点数 >= TOUCH_MIN，
    且有一定天数曾在地板上方 10% 以上成交（区分纯单触底 / 阴跌）。"""
    lows=[x['low'] for x in win]; highs=[x['high'] for x in win]
    lo_all=min(lows); hi_all=max(highs); mid=(lo_all+hi_all)/2
    if mid<=0 or (hi_all-lo_all)/mid > RANGE_MAX:
        return None, False
    best=None; bestcnt=0
    for k in range(0,26):
        cand=lo_all+(hi_all-lo_all)*0.02*k
        if cand > lo_all+(hi_all-lo_all)*0.55: break
        cnt=sum(1 for x in win if cand*0.98 <= x['low'] <= cand*1.03)
        if cnt>bestcnt: bestcnt=cnt; best=cand
    if best is None or bestcnt < TOUCH_MIN:
        return None, False
    above=sum(1 for x in win if x['high']>=best*1.10)
    if above < len(win)*0.05:   # 放宽：真横盘须有少量时间明显高于地板（区分纯单触底）
        return None, False
    return best, True

def detect(d):
    """返回该股票所有 '假破位+收复' 事件；每个事件含支撑/破位日/信号日/量比/各周期收益。
    关键：支撑 S 一律用「破位之前 WIN 日」窗口锚定（window before the break），
          杜绝破位后大涨把横盘振幅拉爆、算不出支撑；步进改为越过破位而非越过收复，
          避免把"同一支撑上的连续破位-收复"成段跳过。"""
    for x in d:
        for k in ('open','high','low','close','volume'):
            try: x[k]=float(x[k])
            except: return []
    N=len(d); events=[]; t=130
    # 循环上限放宽到 N-13：破位后最多 12 日收复，须留到末根以支持「当日刚收复」信号
    while t < N-13:
        # 先用 t 之前窗口定位横盘地板候选
        S0, ok0 = _support(d[t-WIN:t])
        if not ok0:
            t+=1; continue
        # 破位（扫描放宽到末根，以识别"刚发生且已收复"的近期破位）
        break_idx=None
        for i in range(t, min(t+15, N)):
            if d[i]['close'] < S0*BREAK_PCT:
                break_idx=i; break
        if break_idx is None:
            t+=1; continue
        # ── 用「破位之前的窗口」重新锚定支撑（核心修复）──
        pre=d[break_idx-WIN:break_idx]
        S, ok = _support(pre)
        if not ok:
            t = break_idx + 1; continue   # 破位前不是干净横盘 → 跳过该破位
        if not (d[break_idx]['close'] < S*BREAK_PCT):
            t = break_idx + 1; continue   # 破位相对新支撑不成立 → 跳过
        # 收复（可到末根，以支持"刚收复"的当日信号）
        reclaim_idx=None
        for i in range(break_idx+1, min(break_idx+RECLAIM_WIN+1, N)):
            if d[i]['close'] >= S*RECLAIM_PCT:
                reclaim_idx=i; break
        if reclaim_idx is None:
            t = break_idx + 1; continue
        # 破位→收复间隔硬上限：不得大于 MAX_GAP 个交易日（≈两周内必须回来）
        gap = reclaim_idx - break_idx
        if gap > MAX_GAP:
            t = break_idx + 1; continue
        sig=reclaim_idx; sig_close=d[sig]['close']
        # 收复日形态质量：必须「实体长阳 + 上影短」，排除小短线/十字/阴线/长上引冲高回落假收复
        rb=d[sig]; rb_open,rb_close,rb_high,rb_low=rb['open'],rb['close'],rb['high'],rb['low']
        rb_body=rb_close-rb_open; rb_rng=rb_high-rb_low
        rb_up=rb_high-max(rb_open,rb_close)
        if rb_rng<=0 or rb_open<=0:
            t = break_idx + 1; continue
        if rb_body/rb_open < RECLAIM_BODY_MIN:
            t = break_idx + 1; continue
        if rb_up/rb_rng > RECLAIM_UPPER_MAX:
            t = break_idx + 1; continue
        # ── 收复后失效门槛（0.618 黄金分割回撤）──
        # 以「破位低点→收复日最高点」为一波拉升，取 0.618 回撤位作防线：
        #   floor = H - 0.618*(H-L)，L=破位日前后最低、H=收复日最高
        # 收复日后任一交易日收盘跌破该防线 → 有效跌破黄金支撑 → 收复失败，剔除。
        wave_lo = min(x['low'] for x in d[max(0,break_idx-1):sig+1])
        wave_hi = d[sig]['high']
        fib_floor = wave_hi - RECLAIM_FAIL_FIB * (wave_hi - wave_lo) if wave_hi > wave_lo else sig_close*(1-RECLAIM_FAIL_DROP)
        fail = any(d[j]['close'] < fib_floor for j in range(sig+1, N))
        if fail:
            t = sig + 1; continue
        avgvol=sum(x['volume'] for x in pre[-20:])/20   # 量能基准用破位前 20 日均量
        def ret(n):
            j=min(sig+n, N-1); return (d[j]['close']/sig_close-1) if j>sig else None
        def peak(n):
            seg=d[sig+1:sig+n+1]; return (max(x['high'] for x in seg)/sig_close-1) if seg else None
        def dd(n):
            seg=d[sig+1:sig+n+1]; return (min(x['low'] for x in seg)/sig_close-1) if seg else None
        age = (N-1) - sig   # 距最新交易日多少个交易日
        # ── 二次确认（二次提示点）── 收复后 1~SECOND_CONFIRM_WIN 交易日内出现「收涨≥SECOND_CONFIRM_UP + 放量」确认K线
        second_confirm=None
        for j in range(sig+1, min(sig+1+SECOND_CONFIRM_WIN, N)):
            cj=d[j]
            chg=cj['close']/d[j-1]['close']-1 if j>0 else 0
            vol_ok=(cj['volume']>=d[sig]['volume']) or (avgvol and cj['volume']>=avgvol*SECOND_CONFIRM_VOL)
            if chg>=SECOND_CONFIRM_UP and vol_ok:
                second_confirm={'day':cj['day'],'gap':j-sig,'chg':round(chg,4),
                                'vol_ratio':round(cj['volume']/avgvol,2) if avgvol else None,
                                'sc_age':(N-1)-j}   # 确认日距最新交易日数（时效判断用）
                break
        op = op_plan(d, sig)   # 操作策略（管三天）
        events.append({
            'support':round(S,2),'break_day':d[break_idx]['day'],'sig_day':d[sig]['day'],
            'age':age,'break_to_sig':gap,'vol_ratio':round(d[sig]['volume']/avgvol,2),
            'r5':ret(5),'r10':ret(10),'r20':ret(20),
            'peak10':peak(10),'peak20':peak(20),'dd20':dd(20),
            'op':op,'hold3_ret':op.get('hold3_ret'),'second_confirm':second_confirm})
        t = sig + 1   # 步进越过收复日：避免同一结构里相邻破位日重复映射同一收复（消重），又不漏掉后续独立破位
    return events

def score(e):
    """评分：近期 + 触点质量 + 量价，越大越优先。
    量能分层（回测结论，非硬过滤）：
      · 买点放量 + 破位→收复间隔≤3 → 最强组合（回测3日胜率71.5%），+6；
      · 买点放量（间隔偏大）→ 优选层，+3；
      · 买点缩量承接 → 稳健型，保留但排序靠后，+1；
      缩量/放量均不剔除，仅影响排序，三只样板仍能进池。"""
    s=0
    s += max(0, 6 - e['age']) * 3          # 越近期越高
    if e['vol_ratio']>=1.2: s+=4           # 收复日放量长阳
    if e['vol_ratio']<1.0:  s+=1           # 缩量收复也认可（弱加分，避免误杀）
    if e['peak20'] is not None and e['peak20']>0.10: s+=3
    opd=e.get('op') or {}
    g=e.get('break_to_sig')
    vr_buy=opd.get('vol_ratio_buy') if isinstance(opd,dict) else None
    if vr_buy is not None and vr_buy > 1.0:
        # 买点放量：回测显示胜率更高，做精选层加分（非硬过滤）
        s += 6 if (isinstance(g,int) and g<=3) else 3
    else:
        s += 1   # 买点缩量承接：稳健型仍保留，排序靠后
    # 破位→收复间隔越短越优：1日回 +9，10日回 +0
    if g is not None:
        s += max(0, MAX_GAP - g)
    # 二次确认（二次提示点）：仅「新鲜」确认（确认日距今≤1交易日）列第一优先级 +15；
    # 过期确认（已跌数日）不再享优先级加分，降级为观察样本，避免旧闻霸榜。
    sc=e.get('second_confirm')
    if isinstance(sc,dict) and sc.get('sc_age',99)<=SECOND_CONFIRM_FRESH:
        s+=15
    return s

# ============================ 操作策略（本模型「管三天」） ============================
# 每次推荐都附此策略；提示日=收复信号日，依据 T+1 形态定买点，买入后第 3 日强制卖出。
OPER_RULE = """**操作策略（本模型「管三天」，每次推荐必附）**
- 提示日 = 收复信号日（重新站回支撑之日）。
- 提示日后第二天（T+1）判断买入与否：
  · 若 **缩量小涨** → 当天可买；
  · 若 **上影线 / 绿阴线**（诱空）→ 等第三天（T+2）**开盘低位**买入。
- **买入后第三天必须卖出**（固定持有 ≤3 日，不恋战）。
- **买点量能（分层参考·非硬过滤）**：回测显示「买点放量 + 破位→收复间隔≤3」组合未来 3 日胜率最高（71.5%），列为**量能精选层（优先关注）**；缩量承接型仍保留但排序靠后。两者均不剔除。
- **二次确认（二次提示点）**：收复后 1~5 日内再现「放量上涨（收涨≥3%）」即确认非单纯反弹；**仅当确认日距今 ≤1 交易日**才列为**第一优先级**推送，确认日距今 >1 交易日（涨势已兑现/回落）则**自动降级为观察样本**，避免旧信号霸榜。**二次确认是"优先级提示"而非"买点"**：回测确认日追高买入为负期望（3日胜率44.5%／均收−0.57%），正期望买点仍在收复日 T+1/T+2；未达确认条件的作为**观察样本**继续跟踪。
- **收复后失效剔除（0.618 黄金分割）**：以「破位低点→收复日最高」为一波拉升，取 0.618 黄金回撤位作防线（floor = 收复日最高 − 0.618×(收复日最高−破位低点)）；收复日后任一交易日**收盘跌破该防线**即视为有效跌破黄金支撑、收复失败，直接剔除，不再推送、不再"管三天"（如北方长龙/内蒙一机/华联股份）。仅盘中插针不算，以收盘为准。
- 风控：跌破支撑下 3~5% 止损；到达 +8~12% 或放量滞涨可提前止盈。"""

# 模型原始样本（用于回忆模型来源/标定口径；每条推送末尾附，便于对照）
ORIGINAL_SAMPLES=('📌 **模型原始样本**（建模标尺，供回忆对照）：'
                  '均普智能 sh688306、幸福蓝海 sz300528、仁信新材 sz301395。')

UPPER_SHADOW_RATIO=0.5
UPPER_SHADOW_MIN=0.008
SMALL_UP_MAX=0.03

def op_plan(d, sig):
    """依据提示日(sig)后第一根K线(T+1)给出买入决策、卖出日与「管三天」收益。"""
    n=len(d); t1=sig+1
    if t1>=n:
        return {"buy_offset":1,"buy_day":"提示日+1（第二天）","trigger":"待 T+1 成型",
                "sell_day":"买入后第 3 日","t1_shape":"N/A","vol_ratio_t1":None,
                "vol_ratio_buy":None,"buy_vol_ok":None,
                "hold3_ret":None,"note":"T+1 尚未出现，次日再判。"}
    b1,bs=d[t1],d[sig]
    vol_shrink=b1['volume']<bs['volume']
    vol_ratio_t1=round(b1['volume']/bs['volume'],2) if bs['volume'] else None
    up=b1['close']>b1['open']
    pct=(b1['close']-b1['open'])/b1['open'] if b1['open'] else 0
    small_up=up and (0<pct<=SMALL_UP_MAX)
    body_hi=max(b1['open'],b1['close']); upper=b1['high']-body_hi
    body=abs(b1['close']-b1['open'])
    has_upper=upper>=UPPER_SHADOW_RATIO*body and upper>=UPPER_SHADOW_MIN*b1['open']
    is_green=b1['close']<b1['open']
    if vol_shrink and small_up: buy_offset,trigger=1,"缩量小涨"
    elif has_upper or is_green: buy_offset,trigger=2,"上影线/绿阴线"
    else: buy_offset,trigger=1,"其它（次日观察，满足缩量小涨即买；若收上影/阴线则转第三日低开买）"
    shape=[]
    shape.append("缩量" if vol_shrink else "放量")
    if is_green: shape.append("绿阴")
    elif small_up: shape.append("小涨")
    elif up: shape.append("上涨")
    else: shape.append("小跌")
    if has_upper: shape.append("带上影")
    t1_shape="、".join(shape)
    sell_offset=buy_offset+3
    buy_i=min(sig+buy_offset,n-1); sell_i=min(sig+sell_offset,n-1)
    # 买点量能校验：买点当天量能须低于提示日（缩量承接）
    b_buy=d[buy_i]; vol_ratio_buy=round(b_buy['volume']/bs['volume'],2) if bs['volume'] else None
    buy_vol_ok=(vol_ratio_buy is not None and vol_ratio_buy<1.0)
    if not buy_vol_ok:
        trigger+="；⚠买点量能≥提示日·量能不符·谨慎/放弃"
    hold3_ret=None
    if sell_i>buy_i:
        entry=d[buy_i]['open'] if buy_offset==2 else d[buy_i]['close']
        hold3_ret=round(d[sell_i]['close']/entry-1,4)
    return {"buy_offset":buy_offset,
            "buy_day":"提示日+%d（%s）"%(buy_offset,"第二天" if buy_offset==1 else "第三天开盘"),
            "trigger":trigger,"sell_day":"买入后第 3 日（提示日+%d）"%sell_offset,
            "t1_shape":t1_shape,"t1_day":b1['day'],"vol_ratio_t1":vol_ratio_t1,
            "vol_ratio_buy":vol_ratio_buy,"buy_vol_ok":buy_vol_ok,
            "hold3_ret":hold3_ret,
            "note":"T+1(%s): %s；%s → 买点%s(量比%.2f%s)；买入后第 3 日卖出。"%(b1['day'],t1_shape,trigger,"提示日+%d"%buy_offset,vol_ratio_buy if vol_ratio_buy is not None else 0,"·量能不符" if not buy_vol_ok else "")}

# ---------------- 主流程 ----------------
def run(limit=None, workers=16, max_age=3, use_cache=True, selftest=False):
    if selftest:
        named=[('sh688306','均普智能'),('sz300528','幸福蓝海'),('sz301395','仁信新材')]
        log('=== 自检验证（已知样本全周期事件）===')
        for code,name in named:
            d=fetch_kline(code, use_cache=False)
            if not d: log(name,'无数据'); continue
            evs=detect(d)
            log(f'{name}({code}) 检出事件 {len(evs)} 个')
            for e in evs[-4:]:
                log('   支撑=%.2f 破位=%s 信号=%s 量比=%.2f R20=%.1f%% PEAK20=%.1f%% age=%d'%(
                    e['support'],e['break_day'],e['sig_day'],e['vol_ratio'],
                    (e['r20'] or 0)*100,(e['peak20'] or 0)*100,e['age']))
        return

    stocks=get_stocklist()
    if limit: stocks=stocks[:limit]
    # 立信审计一票否决：消费流水线排除集，剔除全部立信股（不区分名次）
    lixin=load_lixin()
    if lixin:
        before=len(stocks)
        stocks=[s for s in stocks if _bare(s['symbol']) not in lixin]
        log('立信剔除：%d 只 → 剩余 %d 只' % (before-len(stocks), len(stocks)))
    # ST / *ST 一票否决（与流水线 universe 口径一致，避免把风险警示股推给用户）
    before=len(stocks)
    stocks=[s for s in stocks if 'ST' not in s.get('name','').upper()]
    if before-len(stocks):
        log('ST剔除：%d 只 → 剩余 %d 只' % (before-len(stocks), len(stocks)))
    log(f'待扫描 {len(stocks)} 只, 线程 {workers}, 仅输出近 {max_age} 交易日收复信号')

    candidates=[]
    def worker(s):
        code=s['symbol']; name=s.get('name','')
        if _bare(code) in lixin: return None   # 双重保险
        d=fetch_kline(code, use_cache=use_cache)
        if not d: return None
        for e in detect(d):
            if e['age'] <= max_age:
                e['code']=code; e['name']=name
                return e
        return None

    t0=time.time(); done=0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs=[ex.submit(worker,s) for s in stocks]
        for f in as_completed(futs):
            done+=1
            r=f.result()
            if r: candidates.append(r)
            if done % 500 == 0: log('进度',done,'/',len(stocks),'命中',len(candidates),f'{time.time()-t0:.0f}s')
    log('扫描完成，耗时 %.1fs，命中 %d 只'%(time.time()-t0, len(candidates)))

    candidates.sort(key=score, reverse=True)
    write_outputs(candidates, max_age)
    return candidates

def write_outputs(cands, max_age):
    today=datetime.date.today().strftime('%Y-%m-%d')
    md_path=os.path.join(HERE, f'扫描结果_{today}.md')
    csv_path=os.path.join(HERE, f'scan_results_{today}.csv')
    # Markdown
    confirmed=[e for e in cands if isinstance(e.get('second_confirm'),dict)
               and e['second_confirm'].get('sc_age',99)<=SECOND_CONFIRM_FRESH]
    watch=[e for e in cands if not (isinstance(e.get('second_confirm'),dict)
           and e['second_confirm'].get('sc_age',99)<=SECOND_CONFIRM_FRESH)]
    lines=[f'# 破位后上涨模型 · 每日扫描结果（{today}）', '']
    lines.append(f'> 近 **{max_age}** 个交易日发生"收复支撑"的候选共 **{len(cands)}** 只：'
                 f'其中 **第一优先级·二次确认 {len(confirmed)}** 只（收复后放量上涨确认、且确认日距今 ≤{SECOND_CONFIRM_FRESH} 交易日），'
                 f'**观察样本 {len(watch)}** 只（待确认或确认已过时效）。模型为博反弹信号，须严格止盈止损，非投资建议。')
    lines.append(f'> ⚠️ **二次确认是"优先级提示"而非"买点"**：回测显示确认日追高买入为负期望（3日胜率 44.5%／均收 −0.57%，涨停确认更差）；'
                 f'真正正期望买点仍在收复日 T+1/T+2。**确认日距今 >{SECOND_CONFIRM_FRESH} 交易日的信号自动降级为观察样本、不再列第一优先级。**')
    # ── 第一优先级：二次确认 ──
    if confirmed:
        lines += ['','## 第一优先级 · 二次确认（收复后放量上涨确认）',
                  '| 排名 | 代码 | 名称 | 支撑 | 破位日 | 收复信号日 | 二次确认日 | 距今 | 量比 | 评分 |',
                  '|---|---|---|---|---|---|---|---|---|---|']
        for i,e in enumerate(confirmed,1):
            sc=e['second_confirm']
            lines.append('| {} | {} | {} | {} | {} | {} | {} (间隔{}日·涨{:.1f}%) | {} | {} | {} |'.format(
                i, e['code'], e['name'], e['support'], e['break_day'], e['sig_day'],
                sc['day'], sc['gap'], sc['chg']*100, e['age'], e['vol_ratio'], score(e)))
    # ── 观察样本 ──
    if watch:
        lines += ['', '## 观察样本（待二次确认）',
                  '| 排名 | 代码 | 名称 | 支撑 | 破位日 | 收复信号日 | 距今(交易日) | 量比 | 评分 |',
                  '|---|---|---|---|---|---|---|---|---|']
        for i,e in enumerate(watch,1):
            lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} |'.format(
                i, e['code'], e['name'], e['support'], e['break_day'], e['sig_day'],
                e['age'], e['vol_ratio'], score(e)))
    # 量能精选层（买点放量 + 破位→收复间隔≤3，回测3日胜率71.5%）：优先关注视图
    elite=[e for e in cands
           if isinstance(e.get('op'),dict) and (e['op'].get('vol_ratio_buy') or 0) > 1.0
           and (e.get('break_to_sig') or 99) <= 3]
    if elite:
        lines += ['', '### 量能精选层（买点放量 + 破位→收复间隔≤3，回测3日胜率 71.5%，优先关注）']
        for e in elite:
            lines.append('- %s %s：支撑 %.2f ｜ 破位 %s → 收复 %s ｜ 量比 %s ｜ 买点量比 %.2f ｜ 评分 %d' % (
                e['code'], e['name'], e['support'], e['break_day'], e['sig_day'],
                e['vol_ratio'], (e['op'].get('vol_ratio_buy') or 0), score(e)))
    lines += ['', OPER_RULE,
              '','## 字段说明','- **支撑**：检测到的水平支撑线（多触点地板）',
              '- **破位日**：收盘跌破支撑的诱空日','- **收复信号日**：重新站回支撑之日（买点参考）',
              '- **量比**：收复日成交量 / 破位前20日均量（>1 为放量长阳）。量能分层：买点放量+间隔≤3 为精选层（回测3日胜率71.5%），缩量承接仍保留但排序靠后，均不剔除。',
              '- **评分**：近期度+量价+破位→收复间隔 综合，越大越优先筛选（间隔越短分越高）',
              '', ORIGINAL_SAMPLES]
    open(md_path,'w').write('\n'.join(lines))
    # CSV
    import csv
    with open(csv_path,'w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f)
        w.writerow(['排名','代码','名称','支撑','破位日','收复信号日','距今交易日','量比','评分','R5','R10','R20','PEAK20','DD20','操作买点','触发条件','卖出日','买点量比(提示日)','管三天收益%'])
        for i,e in enumerate(cands,1):
            op=e.get('op') or {}
            w.writerow([i,e['code'],e['name'],e['support'],e['break_day'],e['sig_day'],e['age'],
                        e['vol_ratio'],score(e),
                        round((e['r5'] or 0)*100,1),round((e['r10'] or 0)*100,1),
                        round((e['r20'] or 0)*100,1),round((e['peak20'] or 0)*100,1),
                        round((e['dd20'] or 0)*100,1),
                        op.get('buy_day',''),op.get('trigger',''),op.get('sell_day',''),
                        ('%.2f'%op['vol_ratio_buy']) if op.get('vol_ratio_buy') is not None else '',
                        round((e.get('hold3_ret') or 0)*100,1) if e.get('hold3_ret') is not None else ''])
    log('报告已写出：')
    log('  ', md_path)
    log('  ', csv_path)
    # 控制台摘要
    log('--- 候选清单（前 30，按评分）---')
    for i,e in enumerate(cands[:30],1):
        log(f"  {i:2d}. {e['code']} {e['name']:<8} 支撑{e['support']:<7} 破位{e['break_day']} 收复{e['sig_day']} 距今{e['age']} 量比{e['vol_ratio']} 评分{score(e)}")

if __name__=='__main__':
    ap=argparse.ArgumentParser(description='破位后上涨 每日扫描')
    ap.add_argument('--limit', type=int, default=None, help='仅扫描前 N 只（测试用）')
    ap.add_argument('--workers', type=int, default=16)
    ap.add_argument('--max-age', type=int, default=3, help='仅输出近 N 交易日收复的信号')
    ap.add_argument('--no-cache', action='store_true', help='不使用行情缓存')
    ap.add_argument('--selftest', action='store_true', help='用已知 3 样本验证检测逻辑')
    ap.add_argument('--out', default=HERE)
    a=ap.parse_args()
    HERE=a.out
    run(limit=a.limit, workers=a.workers, max_age=a.max_age,
        use_cache=not a.no_cache, selftest=a.selftest)
