# 选股模型仓库（Git 持久化版）

把各套选股模型脚本与其跨天状态（推荐记录、排除集、跟踪 CSV、归档等）统一放进本仓库，
**通过 GitHub Actions 定时运行**：每次 `checkout` 拿到最新脚本 → 跑模型 → 用 `GITHUB_TOKEN`
把 `state/` 状态目录 commit 回仓库，彻底避免"本地文件缺失 / 第二天执行错误"的问题。

> **仓库是唯一事实源**：沙箱 / 本地文件随时可能丢失，一切以本仓库为准。
> 以后要改规则、阈值或脚本，直接改对应目录里的 `.py` 并 push —— 下一个定时点（或触发的 push）
> 会自动用最新代码运行，无需任何手工配置。

## 目录结构

```
us-radar-picker/
├── us_radar_picker.py          # 美股异动雷达 × 易点启动买点 × 低位上影线（盘前）
├── state/                      # us_radar_picker 的状态目录
├── lingpei/                    # 领湃模型（脚本 + state/ 自动回写）
├── shanshui/                   # 善水模型（脚本 + state/ 自动回写）
├── low_shadow/                 # 低位上影线模型（脚本 + state/ 自动回写）
├── kaizhong/                   # 凯众形态相似股模型 v4（横盘突破提示）
│   ├── daily_v4.py             #   每日入口：抓行情 → 扫描 → 归口 → 台账 → 推送
│   ├── stance_hint.py          #   v4 选股核心（STANCE 阈值）
│   ├── recommend.py            #   推荐归口 / 复盘统计
│   ├── lib.py                  #   行情抓取 / 日线缓存 / 推送
│   ├── requirements.txt
│   └── state/                  #   凯众状态目录（自动回写，含 kline / 台账 / reports）
├── yidian/                     # 易点模型推荐 · 收盘后全市场选股（脚本 + state/ 自动回写）
│   ├── run.py                  #   选股主程序（阈值集中在 CONST 区）
│   ├── requirements.txt        #   仅依赖 Python 标准库
│   └── state/                  #   跨天状态 + 全部合格股归档（自动回写）
│       ├── state.json          #     last_archive / 累计胜率 / history
│       ├── index.jsonl         #     每日索引
│       ├── summary.json        #     当日机器可读摘要
│       └── archive/            #     每日 JSON + CSV + 推送正文
└── .github/workflows/
    ├── us-radar-picker.yml      # 美股雷达（UTC 00:00 = 北京 08:00 盘前）
    ├── shanshui.yml             # 善水（UTC 08:10 = 北京 16:10）
    ├── low_shadow.yml           # 低位上影线（UTC 08:30 = 北京 16:30）
    ├── lingpei.yml              # 领湃（UTC 08:35 = 北京 16:35）
    ├── kaizhong.yml             # 凯众 v4（UTC 08:40 = 北京 16:40）
    └── yidian.yml               # 易点模型（UTC 08:45 = 北京 16:45 收盘后）
```

## 定时时间（北京时间）

| 模型 | Cron（UTC） | 北京时间 | workflow 文件 |
|------|-------------|----------|---------------|
| 美股雷达/易点买点/低位上影线 | `0 0 * * 1-5` | 08:00 盘前 | us-radar-picker.yml |
| 善水 | `10 8 * * 1-5` | 16:10 | shanshui.yml |
| 低位上影线 | `30 8 * * 1-5` | 16:30 | low_shadow.yml |
| 领湃 | `35 8 * * 1-5` | 16:35 | lingpei.yml |
| 凯众 v4 | `40 8 * * 1-5` | 16:40 | kaizhong.yml |
| 易点模型 | `45 8 * * 1-5` | 16:45 | yidian.yml |

凯众除了定时，还配了 `push` 触发：`kaizhong/**` 或自己的 workflow 有改动就自动跑一次，
改完脚本推上去即生效，不用手动点 Run workflow。

易点模型同样配了 `push` 触发，但只监听 `yidian/run.py`、`yidian/requirements.txt`、
`.github/workflows/yidian.yml`（不含 `yidian/state/**`）；且 `GITHUB_TOKEN` 的状态回写 push
不会再触发 workflow，因此不会形成循环。

也可在 GitHub 仓库 **Actions** 页面对每个 workflow 点 **Run workflow** 手动触发。

## 必配密钥（Repository → Settings → Secrets and variables → Actions）

- `PUSHPLUS_TOKEN`：PushPlus 微信推送 token（各模型共用同一渠道，已配置）。
- `FTQQ_KEY`：方糖 SendKey（凯众模型使用；不配置则跳过推送，选股与台账照常）。

> 注意：`GITHUB_TOKEN` 由 GitHub 自动提供（workflow 已声明 `permissions: contents: write`），
> **不需要**把个人 PAT 填进 Secrets，也不要把 PAT 写进脚本或远程 URL。

## 易点模型 · 口径与维护

- 样本：创业板 `300/301/302` + 科创板 `688/689` + 北交所 `43/83/87/88/92` 全量枚举，同一套阈值；
  不设流通市值门槛（市值仅归档展示）。
- 硬条件：90日回撤≥15%、近5日涨幅≤45%、当日涨幅 0.5-22%、换手 2-35%、股价 2-200元、
  量比≥1.4、收盘≥MA20×0.95、距MA20≤12%、MA5 拐头或价低于 MA60；剔除 ST/*ST/退市/N 次新。
- 审计一票否决：会计师事务所为立信或查询缺失 → 剔除。
- 评分取前 6 推送；状态含 已启动(启动第N天)/临界/筑底/观察；买点=收盘×0.975~收盘，
  止损 -5%、目标 +7%；观察池含下轨缩量、上影试盘。
- 全部合格股（非仅前 6）归档 `yidian/state/archive/`（JSON+CSV），同步 `index.jsonl` 与 `state.json`；
  次日自动回溯此前归档候选的次日表现并累计胜率。
- 非交易日（行情日期≠当日）自动退出、不推送；数据获取失败推送告警。
- 数据源：东方财富公开行情/公告接口，仅依赖 Python 3 标准库。

**要改任何口径**：编辑 `yidian/run.py` 顶部的 `CONST` 常量区后 push 即可自动生效。
日期基准固定北京时间（UTC runner 亦正确），可用 `RUN_DATE` 环境变量覆盖以便补跑/验证。

## 安全提醒

- 历史上若把个人 PAT 写进了 `.git/config` 的 remote URL，请到
  GitHub **Settings → Developer settings → Personal access tokens** 将其 Regenerate / Delete 并换新的，
  避免长期暴露。本仓库的推送一律走 Actions 自带的 `GITHUB_TOKEN`。
- 任何 token 都不要写进脚本、README、remote URL 或提交内容。
