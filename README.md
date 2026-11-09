# 选股模型仓库（Git 持久化版）

把各套选股模型脚本与其跨天状态（推荐记录、排除集、跟踪 CSV 等）统一放进本仓库，
**通过 GitHub Actions 定时运行**：每次 `checkout` 拿到最新脚本 → 跑模型 → 用 `GITHUB_TOKEN`
把 `state/` 状态目录 commit 回仓库，彻底避免"本地文件缺失 / 第二天执行错误"的问题。

## 目录结构

```
us-radar-picker/
├── us_radar_picker.py          # 美股异动雷达 × 易点启动买点 × 低位上影线（已上线）
├── state/                      # us_radar_picker 的状态目录
├── lingpei/                    # 【待放入脚本】领湃模型
│   ├── _lixin_update.py        #   立信排除集增量更新
│   ├── 领湃模型_每日.py         #   每日扫描推送
│   ├── 领湃跟踪修正.py          #   到期推荐 T+10 跟踪修正
│   ├── requirements.txt
│   └── state/                  #   领湃状态目录（自动回写）
├── shanshui/                   # 【待放入脚本】善水模型
│   ├── daily_shanshui.py       #   善水模型 v10 选股
│   ├── requirements.txt
│   └── state/                  #   善水状态目录（自动回写）
├── kaizhong/                   # 凯众形态相似股模型 v4（横盘突破提示）
│   ├── daily_v4.py             #   每日入口：抓行情 → 扫描 → 归口 → 台账 → 推送
│   ├── stance_hint.py          #   v4 选股核心（STANCE 阈值）
│   ├── recommend.py            #   推荐归口 / 复盘统计
│   ├── lib.py                  #   行情抓取 / 日线缓存 / 推送
│   ├── requirements.txt
│   └── state/                  #   凯众状态目录（自动回写，含 kline / 台账 / reports）
└── .github/workflows/
    ├── us-radar-picker.yml      # 美股雷达（UTC 00:00 = 北京时间 08:00 盘前）
    ├── lingpei.yml              # 领湃（UTC 08:35 = 北京时间 16:35）
    ├── shanshui.yml             # 善水（UTC 08:10 = 北京时间 16:10）
    └── kaizhong.yml             # 凯众 v4（UTC 08:40 = 北京时间 16:40）
```

## 定时时间（北京时间）

| 模型 | Cron（UTC） | 北京时间 | workflow 文件 |
|------|-------------|----------|---------------|
| 美股雷达/易点/低位上影线 | `0 0 * * 1-5` | 08:00 盘前 | us-radar-picker.yml |
| 善水 | `10 8 * * 1-5` | 16:10 | shanshui.yml |
| 领湃 | `35 8 * * 1-5` | 16:35 | lingpei.yml |
| 凯众 v4 | `40 8 * * 1-5` | 16:40 | kaizhong.yml |

凯众除了定时，还配了 `push` 触发：`kaizhong/**` 或自己的 workflow 有改动就自动跑一次，
改完脚本推上去即生效，不用手动点 Run workflow。

也可在 GitHub 仓库 **Actions** 页面对每个 workflow 点 **Run workflow** 手动触发。

## 必配密钥（Repository → Settings → Secrets and variables → Actions → New repository secret）

- `PUSHPLUS_TOKEN`：PushPlus 微信推送 token（三个模型共用同一渠道）。
- `FTQQ_KEY`：方糖 SendKey（凯众模型使用；不配置则跳过推送，选股与台账照常）。

> 注意：`GITHUB_TOKEN` 由 GitHub 自动提供（workflow 已声明 `permissions: contents: write`），
> **不需要**把个人 PAT 填进 Secrets，也不要把 PAT 写进脚本或远程 URL。

## 待办：放入领湃 / 善水 脚本

`lingpei/` 与 `shanshui/` 目前只有骨架，**脚本还需补上**：

1. 把 `_lixin_update.py`、`领湃模型_每日.py`、`领湃跟踪修正.py` 放进 `lingpei/`；
2. 把 `daily_shanshui.py` 放进 `shanshui/`；
3. 脚本里**读取/写入数据文件请使用相对路径或 `STATE_DIR` 环境变量**（已设为 `lingpei/state`、`shanshui/state`），
   不要写死 `/workspace/...`，否则在 GitHub Actions 环境里依旧会"文件缺失"；
4. 若脚本依赖了 `requests/pandas/numpy` 之外的库，在对应 `requirements.txt` 里追加；
5. 推送后，GitHub Actions 会在下一个定时点或手动触发时自动运行。

## 安全提醒

- 历史上若把个人 PAT 写进了 `.git/config` 的 remote URL，请到
  GitHub **Settings → Developer settings → Personal access tokens** 将其 Regenerate / Delete 并换新的，
  避免长期暴露。本仓库的推送一律走 Actions 自带的 `GITHUB_TOKEN`。
