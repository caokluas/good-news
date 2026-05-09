# Good News Quant

A股形态回测、选股与执行计划工程。当前沉淀了两套固定逻辑：

1. **筛选股票逻辑**：从最新成交量股票池开始，经过 12 个形态策略回归、TOP5 策略复核、最新买点 TOP10、3/4 筹码集中度过滤，产出候选与最终过滤名单。
2. **当天买入股票逻辑**：从最新买点候选中按“命中策略数 + 成交量”选择单票或 TOP2，下午三段买入、次日上午三段卖出，并剔除涨停买不到的分笔。

> 所有用户可见策略名称，一律使用 `pattern/` 目录下的中文图片文件名。

## 项目结构

- `pattern/`：12 张形态参考图，中文图片文件名就是策略正式名称。
- `src/good_news_quant/`：形态识别、回测、筹码集中度、报告生成等核心模块。
- `scripts/`：可直接运行的验证、选股、执行回测脚本。
- `reports/pattern-validation/`：最近一轮验证、选股、执行回测结果输出目录。
- `data/processed/recent-history-cache/`：股票日线历史缓存，供 universe 复用与增量补齐。
- `data/processed/chip-concentration-cache/`：筹码集中度所需分时/周期缓存。
- `docs/`：项目说明与补充文档。

## 3 分钟出结果原则

- 优先读取 `reports/pattern-validation/` 下已有报告和缓存，能回答就不重新跑全流程。
- 只有当用户明确要求“今天 / 最新 / 刷新”且报告已过期时，才运行刷新脚本。
- 刷新或回测命令建议设置 180 秒超时；超时或失败时，返回最新缓存报告，并明确说明信号日期和缓存状态。
- 当前两个 Codex skills 已按这个原则编排：
  - `$a-share-stock-screening`：筛选股票逻辑。
  - `$a-share-execution-buy-plan`：当天买入股票逻辑。

## 逻辑一：筛选股票

### 固定流程

1. **股票池筛选**：按最新成交量筛选主板、非 ST、非创业板、10% 涨跌幅限制股票，取前 `200`。
2. **历史缓存复用**：先对比上一轮 `universe.csv`；未变化股票优先复用 `data/processed/recent-history-cache/`，新增股票全量下载，保留股票只补最新缺失日期。
3. **12 策略回归**：对最近窗口内 12 个形态策略做买卖点回测，统计命中数、执行交易数、胜率、平均收益率。
4. **60 天 TOP5**：按近 60 天平均收益率选出 TOP5 策略。
5. **30 天复核**：只对这 5 个策略做近 30 天复核，形成最终参与选股的 TOP 策略集合。
6. **最新买点 TOP10**：取最新买点日命中这些策略的股票，生成候选 TOP10。
7. **筹码集中度过滤**：只对 TOP10 做 `日K / 60分 / 30分 / 周K` 筹码分析；至少 `3/4` 周期为 `集中上行` 才进入最终过滤名单。

### 筹码趋势口径

- 窗口：`日K / 60分 / 30分` 使用近 `60` 个周期，`周K` 使用近 `4` 周。
- 趋势判断点数：`30分` 看最近 `20` 个筹码点，`60分` 看最近 `10` 个，`日K / 周K` 看最近 `4` 个。
- 判断标准：线性斜率 `> 0.001` 且首尾差为正是 `集中上行`；斜率 `< -0.001` 且首尾差为负是 `集中下行`；其他为 `横向整理`。
- 严格过滤：最终保留条件是至少 `3/4` 周期集中上行。

### 触发方式

- Codex skill：`$a-share-stock-screening`
- 直接命令：

```powershell
.\.venv\Scripts\python.exe scripts\run_latest_stock_selection.py --end-date <YYYY-MM-DD> --signal-days 100 --warmup-days 220
```

### 重点看哪些报告

优先阅读顺序：

1. `reports/pattern-validation/selection-summary.md`：最重要，包含信号日期、TOP5 策略、TOP10 股票、筹码趋势和最终过滤结果。
2. `reports/pattern-validation/selection-top10-stocks.csv`：最新买点 TOP10 明细，包含命中策略数、策略名称、策略预期收益率、成交量、各周期筹码趋势。
3. `reports/pattern-validation/selection-final-chip-filtered.csv`：严格通过 `3/4` 筹码过滤的最终股票。
4. `reports/pattern-validation/selection-chip-concentration.csv`：TOP10 的筹码集中度快照。
5. `reports/pattern-validation/selection-chip-concentration-history.csv`：TOP10 的筹码集中度历史序列。

### 主要输出文件

- `reports/pattern-validation/summary.md`：最近窗口形态回测总报告。
- `reports/pattern-validation/selection-summary.md`：固定选股流程摘要。
- `reports/pattern-validation/selection-top5-strategies.csv`：近 60 天平均收益率 TOP5 策略。
- `reports/pattern-validation/selection-top5-last30d-summary.csv`：TOP5 策略近 30 天复核结果。
- `reports/pattern-validation/selection-latest-signals.csv`：最新买点日策略命中明细。
- `reports/pattern-validation/selection-top10-stocks.csv`：最新买点 TOP10 候选股票。
- `reports/pattern-validation/selection-final-chip-filtered.csv`：满足 `3/4` 筹码过滤的最终股票。
- `reports/pattern-validation/selection-chip-concentration.csv`：TOP10 筹码集中度快照。
- `reports/pattern-validation/selection-chip-concentration-history.csv`：TOP10 筹码集中度历史。

## 逻辑二：当天买入股票

### 交易候选来源

- 默认从最新买点日命中 `TOP5(60天收益率) -> TOP5(30天复核)` 的候选股票中选择。
- 如果当天没有 TOP 策略候选，则回退到胜率最高策略，并用该策略当天信号筛选股票。
- 单票 / TOP2 交易口径不强制要求通过 `3/4` 筹码过滤；筹码过滤作为风险提示。若用户明确要求“筹码硬过滤”，则只允许买 `selection-final-chip-filtered.csv` 中的股票。

### 排序与选股

- **单票模式**：按 `命中策略数` 降序、`quote_volume` 成交量降序、`ticker` 升序，取 TOP1。
- **TOP2 模式**：同样排序，取 TOP2，并把资金均分到两只股票。
- **注意**：如果直接读取 `selection-top10-stocks.csv`，仍要显式按上述交易排序重排，不要默认文件顺序就是买入顺序。

### 买卖执行规则

- 买入：当天 `13:30 / 14:00 / 14:30` 各买计划仓位的 `1/3`。
- 卖出：下一交易日 `10:00 / 10:30 / 11:00` 各卖实际成交持仓的 `1/3`。
- 手数：A 股按 100 股一手，买入股数向下取整到 100 股。
- 费用：默认佣金双边 `3 bps`，卖出印花税 `10 bps`。

### 涨停买不到过滤

- 每个买入时间点单独判断是否到涨停。
- 涨停价按前收盘价 `+10%` 并四舍五入到分计算。
- 若该时点 30 分钟采样开盘价已到涨停价，则这 `1/3` 仓位视为买不到并跳过。
- 若三个买入时间点都涨停，则当天该票无成交，次日也没有卖出。
- 若部分时间点成交，则次日只卖出实际成交股数。

### 触发方式

- Codex skill：`$a-share-execution-buy-plan`
- 单票回测：

```powershell
.\.venv\Scripts\python.exe scripts\run_execution_backtest.py --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --selection-mode single --out-prefix <prefix>
```

- TOP2 回测：

```powershell
.\.venv\Scripts\python.exe scripts\run_execution_backtest.py --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --selection-mode basket --max-stocks 2 --out-prefix <prefix>
```

### 重点看哪些报告

1. `<prefix>-summary.md`：最重要，包含期末权益、净盈利、执行交易日数、胜率、涨停跳过分笔数。
2. `<prefix>-days.csv`：每天选中的股票、使用策略、当日净盈亏、交易后权益。
3. `<prefix>-legs.csv`：每只股票的三段买入、三段卖出、成交股数、费用、净盈亏、涨停跳过时点。

当前常用示例文件：

- `reports/pattern-validation/execution-backtest_20260201_20260427-single-no-limitup-summary.md`
- `reports/pattern-validation/execution-backtest_20260201_20260427-single-no-limitup-days.csv`
- `reports/pattern-validation/execution-backtest_20260201_20260427-single-no-limitup-legs.csv`
- `reports/pattern-validation/execution-backtest_20260201_20260427-top2-no-limitup-summary.md`
- `reports/pattern-validation/execution-backtest_20260201_20260427-top2-no-limitup-days.csv`
- `reports/pattern-validation/execution-backtest_20260201_20260427-top2-no-limitup-legs.csv`

## 策略回归明细

- `reports/pattern-validation/signal-events.csv`：每次策略命中的事件明细。
- `reports/pattern-validation/signal-trades.csv`：每次命中后按策略买卖点回测得到的交易明细。
- `reports/pattern-validation/strategy-summary.csv`：每个策略的命中数、执行交易数、胜率、平均收益率。
- `reports/pattern-validation/strategy-top10-by-avg-return.csv`：12 个形态策略按平均收益率排名的 TOP10。
- `reports/pattern-validation/top20-stocks.csv`：按形态命中数统计的股票排行。
- `reports/pattern-validation/top20-stock-strategy-returns.csv`：TOP 股票按“股票-策略”拆分的收益表现。

## 股票池与基础数据

- `reports/pattern-validation/universe.csv`：最近一轮实际参与验证的股票池。
- `reports/pattern-validation/report-metadata.csv`：最近一轮验证的交易日、股票池规模、历史下载缺口等元数据。
- `data/raw/`：本地样例原始 CSV。
- `data/processed/`：中间层缓存目录。

## 数据源口径

- **历史策略回归 / 买卖点验证 / 收益率统计**：优先使用 **JoinQuant / JQData**，但仅在账号权限覆盖的历史窗口内使用；适合验证 12 个形态策略的买点、卖点与收益分布，不作为当前日期选股主源。
- **当前选股所需最新行情**：依赖可覆盖到最新交易日的行情源；当前实际落地的是 **Tencent**（股票池成交量快照与日K历史）与 **Sina KLine**（30 分 / 60 分 / 日K 筹码集中度，周K由本地日线缓存聚合生成）。

已确认不可用/不再使用：

- `hq.sinajs.cn`：返回 `403 Forbidden`。
- Eastmoney K 线：在当前环境不稳定/不可用，流程不再依赖它。

## 二开约定

- 用户可见策略名称必须来自 `pattern/` 中文图片文件名。
- Markdown 报告统一写成 `UTF-8 with BOM`，便于 Windows 直接打开。
- 实际“最新买点日”以数据里的最新交易日为准，不直接用自然日。
- 下游扩展优先读取 `signal-events.csv`、`signal-trades.csv`、`strategy-summary.csv`、`selection-top10-stocks.csv`、`selection-final-chip-filtered.csv` 和执行回测的 `*-days.csv` / `*-legs.csv`。
