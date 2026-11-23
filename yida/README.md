# 怡达模型 📈

源自怡达股份（300721）2026年8~11月走势提炼的选股模型，已上线 GitHub 并接入统一前置流水线，每个交易日自动运行 + 微信推送。

**GitHub 仓库**：https://github.com/631311762-blip/us-radar-picker （目录 `yida/`）

## 模型逻辑

| 步骤 | 规则 |
|---|---|
| ① 阴跌前置 | 放量首日(D1)前收盘较过去60日最高价跌幅 ≥ 30%，且股价 < MA60 |
| ② 平台放量 | 连续4天成交量均为前20日均量的 2~7 倍 |
| ③ 量能平稳 | 四天量能变异系数 CV < 20%（排除逐日爆量，怡达原型仅6.8%）|
| ④ 提示日 | 放量**第2天(D2)**收盘价买入 |
| ⑤ 命中判定 | 买入后5日内最高收益 ≥ 3% |

参数见 `yida/config.json`，可随时调整。

## 回溯表现（2026年全A股）

| 放量下限 | 信号数 | 5日命中率 | 5日平均最高收益 |
|---|---|---|---|
| 4倍 | 22 | 90.9% | 12.4% |
| 2倍（当前） | 396 | 88.1% | 10.7% |

原型案例验证：怡达股份 D1=11-17、提示日=11-18（收16.74），阴跌-39.6%、放量5.32倍、CV 6.8%，5日收益 +24.7% 命中 ✅

## 接入方式（已上线）

怡达模型**不是独立 workflow**，而是并入仓库现有的统一流水线：

- **样本源**：读取 `common/state/universe.json`（前置流水线样本池，已剔除 ST/立信/监管立案/次新）
- **K线**：复用 `common/state/klines` 共享缓存（严格离线）
- **调度**：已在 `common/registry.json` 注册，由 `common/run_all.py` 每日统一调度
- **台账**：`ctx.record()` 落盘，自动进入次日统一回溯与参数修正
- **推送**：复用 `common/notify.py` 的 PushPlus 通道（`PUSHPLUS_TOKEN` secret 已配置）

统一流水线 `a-share-pipeline.yml` 每天 **北京时间 15:10** 自动运行，怡达模型随之执行并推送。

## 仓库内相关文件

| 文件 | 说明 |
|---|---|
| `yida/daily_yida.py` | 每日选股主程序 |
| `yida/config.json` | 模型参数 |
| `common/registry.json` | 已加入 yida 注册项 |
| `.github/workflows/a-share-pipeline.yml` | 统一调度（含全部模型）|

## 使用

```bash
# 仓库内运行（正式：选股+台账+推送）
python3 yida/daily_yida.py

# 本地复跑不推送
python3 yida/daily_yida.py --no-push
```

推送 token 由 GitHub Secrets 的 `PUSHPLUS_TOKEN` 提供，本地运行需设置同名环境变量。

## 免责声明

本模型为历史形态统计，不构成投资建议。市场有风险，决策需谨慎。
