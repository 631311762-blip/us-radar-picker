# stock-v3 云端版说明

这是主升浪 v3.1 每日闭环的**云端可移植副本**，与本地 `D:/stock-v3` 同一套代码与状态。

## 与本地版的差异（仅两处）
1. **取数层**：`datafetch.py` 走公网 HTTP（腾讯行情 `web.ifzq.gtimg.cn` 前复权日K + 东财 `push2.eastmoney.com` 粗筛），
   不再依赖已被 WorkBuddy 更新移除的 `westock-tool / westock-data` 本地 CLI。云端可直接联网取数。
2. **推送层**：`daily_v3.py` 在找不到本地 `wechat-push` 技能时，回落到同目录 `wechat_push.py`，
   SendKey 读 `sckey.txt`（或环境变量 `SERVERCHAN_SENDKEY`）。

## 目录内文件
- 代码：`daily_v3.py`（主流程）、`每日推荐.py`（打分选股）、`features.py`、`model2.py`、
  `feedback.py`（回溯/结算/归因/有界进化）、`datafetch.py`、`audit_check.py`、`wechat_push.py`
- 状态（必须跨日保留，丢了闭环就断）：`predictions.json`、`factor_fit.json`、`holdout.json`、
  `feedback_state.json`、`push_log.jsonl`、`universe_cache.json`、`audit_result.json`、`audit_notices.json`
- 密钥：`sckey.txt`（Server酱 SendKey）
- **不带** `kline_cache.json`：约 41MB/2500 只，云端首跑会自动重建（约 10~30 分钟），之后同日复用。

## 运行
```bash
cd /workspace/stock-v3
python3 daily_v3.py --top 6          # 完整闭环（回溯→进化→选股→推微信）
python3 daily_v3.py --top 6 --nopush # 只跑不推，用于验证
```
脚本自己 `os.chdir` 到所在目录，状态文件都按相对路径读写，**放哪个目录都能跑**。
只依赖 Python 3 标准库，无需 pip 安装。

## 状态持久化
云端沙箱若在两次运行间保留 `/workspace`，状态自动延续，无需额外处理。
若发现每次运行 `predictions.json` 都被重置（即沙箱不持久），则每次跑完把
`predictions.json / factor_fit.json / feedback_state.json / push_log.jsonl / holdout.json`
回传到资料库，开跑前再拉回来覆盖。

## 云端备份（2026-11-05 起，每日闭环自动执行）
`daily_v3.py` 在推送完成后自动调 `cloud_backup.py`，做两件事（失败只告警不中断）：
1. **stock-v3-cloud.zip**（代码 12 个 + 状态 10 个，共 22 文件）覆盖上传到资料库固定节点：
   https://www.workbuddy.cn/space/d/xXnKH6dpFgeOacF8p8PM5E
   —— 这就是"最新模型"的云端权威副本，聊天记录/沙箱丢了就从这里恢复。
2. **主升浪V4_<基准日>.txt**（当日推送原文）每天新建一份，按日期归档在资料库，回溯时直接翻。

### 恢复步骤（沙箱重置 / 换环境）
```bash
# 1. 从资料库下载 stock-v3-cloud.zip（节点 xXnKH6dpFgeOacF8p8PM5E）解压到 /workspace/
# 2. 校验关键状态在位：predictions.json（≥98 条）、factor_fit.json、holdout.json、sckey.txt
# 3. 直接跑：cd /workspace/stock-v3 && python3 daily_v3.py
#    kline_cache.json 不在包内，首跑自动重建（约 10~30 分钟）
# 4. 手动补一次备份：python3 cloud_backup.py
```

## 版式备忘（2026-11-04 用户定稿）
- 单榜 6 只（新鲜度优先：启动 1~2 天的票先取，不足退 3~4 天；同层按模型分排序）；
  dsc=0 标「★启动第1天」，每只标注距起涨累计涨幅，≥20% 标 ⚠追高风险。
- 推送标题「主升浪 V4 · <日期> · 主推6只」；市值 ≥25 亿不封顶；换手 0.3~25%。
