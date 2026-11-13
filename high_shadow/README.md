# 高位上影线模型 · 仓库接入契约

> 状态：**待迁移**（2026-11-13）。GitHub 仓库原本没有这个模型的代码，它一直在云盘自动化里独立定时运行。
> 你已确认采用「迁移代码进仓库再纳入合并流水线」。本目录是接入骨架，不含任何选股算法。

## 为什么要迁移

原来的运行方式有两个问题：

1. 它在云盘另行定时，与 GitHub 合并流水线**各跑各的全市场抓取**，容易与东财行情网关对云 IP 的限流叠加；
2. 它只消费流水线回写入库的 `common/state/universe.json`，一旦流水线当天失败，样本池就是隔夜旧数据，模型会静默用错数据产出。

迁入仓库后，它由 `common/registry.json` 统一调度，享受同一份前置层产物、ST/立信剔除、统一回溯与台账，且 23 点兜底也能覆盖它。

## 接入后会自动生效的东西

| 位置 | 已就绪 | 说明 |
|---|---|---|
| `.github/workflows/high_shadow.yml` | ✅ | 23 点兜底的「自身代码」路径，检测入口脚本存在即运行 |
| `common/registry.json` → `high_shadow` | ✅ | 已写好 `cmd`，当前 `enabled=false`，代码到位后改 `true` 即纳入 16:05 流水线 |
| `common/state/universe.json` | ✅ | 流水线每日回写入库的样本池（ST + 立信剔除后，约 4600 只） |
| `common/state/recommendations/high_shadow/` | ✅ | 统一推荐台账落盘目录，写入即自动回写 |
| `fallback-2300.yml` | ✅ | 已把本模型列入 23 点兜底的 dispatch 清单 |

## 需要你提供的文件

把云盘上该模型的代码放进本目录，最少需要：

```
high_shadow/
├── README.md                    ← 本文件（已存在）
├── high_shadow_screener.py      ← 选股入口（必须，见下方契约）
├── config.json                  ← 模型参数（可选；注意仓库 .gitignore 忽略 config.json，
│                                   如需入库请用 git add -f 或改名）
├── requirements.txt             ← 依赖（可选，缺省装 requests/numpy/pandas）
└── model_core.py                ← 原有算法（保持原样即可，不要改指标与阈值）
```

**重要：我不会改动你的选股逻辑、指标或阈值** —— 只做数据入口与落盘的适配。

## 入口脚本契约

`high_shadow_screener.py` 需满足：

1. **命令行**：支持 `--mode review`（由 registry 与兜底 workflow 传入），退出码 0 表示本模型执行完成。
2. **样本池**：从 `common/state/universe.json` 读取（数组，每条至少含 `code` / `name` / `price` / `pct`）。
   环境变量 `UNIVERSE_JSON` 会指向该文件；**不要**自己再去抓全市场列表。
   > 该文件已是 ST + 立信剔除后的结果，模型内部无需重复剔除，但保留原有兜底判断不冲突。
3. **K 线**：优先读 `common/state/klines/`（流水线前置层当日生成，`code.csv`）。
   若希望完全复用共享缓存，可参照 `low_shadow2/low_shadow2_screener.py` 的
   `adapter.install()` 写法；也可以保留你原有的抓数函数作为回退。
4. **产出**：把结果写入 `common/state/recommendations/high_shadow/`
   （推荐，可享统一台账与回溯），或写入 `high_shadow/state/`。两者都会被回写步骤提交。
5. **推送**：沿用 `PUSHPLUS_TOKEN` / `SERVERCHAN_SENDKEY` 环境变量，workflow 已透传。

## 启用步骤（代码到位后）

1. 把上述文件提交到 `high_shadow/`（若有 `config.json`，记得 `git add -f`）。
2. 把 `common/registry.json` 中 `high_shadow` 的 `enabled` 由 `false` 改为 `true`。
3. 手动触发一次 `高位上影线模型 独立流程` 验证自身路径；再触发一次主流水线验证 registry 路径。
4. 确认云盘上原来的独立定时**停掉**，避免同一模型再次重复运行。

在此之前，`high_shadow.yml` 检测到入口不存在会正常跳过（不标红、不误报），
`registry.json` 里 `enabled=false` 也不会让主流水线去执行一个不存在的文件。
