# Good News Quant

项目只保留两条固定逻辑：

1. `筛选股票`：从最新主板高成交量股票池出发，完成形态验证、TOP 策略复核、最新买点筛选和 `3/4` 筹码集中度过滤。
2. `当天买入股票`：从最新候选中选单票或 TOP2，按下午三段买入、次日上午三段卖出做执行回测，并过滤涨停买不到的分笔。

## 最小项目结构

- `scripts/run_latest_stock_selection.py`：逻辑一入口。
- `scripts/run_execution_backtest.py`：逻辑二入口。
- `src/good_news_quant/`：核心实现。
- `pattern/`：策略中文名称来源。
- `reports/logic_first/`：逻辑一输出目录。
- `reports/logic_second/`：逻辑二输出目录。
- `data/processed/recent-history-cache/`：历史行情缓存。
- `data/processed/chip-concentration-cache/`：筹码集中度缓存。

## 环境

要求：`Python 3.11+`

```powershell
uv sync
```

运行脚本默认使用 `uv sync` 生成的 `.venv`。

## 逻辑一：筛选股票

固定流程：

1. 最新成交量股票池。
2. 12 个形态策略回归。
3. 近 60 天选 TOP5 策略。
4. 近 30 天复核 TOP5。
5. 生成最新买点 TOP10。
6. 做 `日K / 60分 / 30分 / 周K` 筹码过滤，至少 `3/4` 为 `集中上行`。

运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_latest_stock_selection.py --end-date <YYYY-MM-DD> --signal-days 100 --warmup-days 220
```

最重要输出：

- `reports/logic_first/selection-summary.md`
- `reports/logic_first/selection-top10-stocks.csv`
- `reports/logic_first/selection-final-chip-filtered.csv`

## 逻辑二：当天买入股票

固定规则：

1. 先在 `reports/logic_second/` 内重建验证层结果，再按固定规则生成当天候选。
2. 单票模式按 `命中策略数 -> 成交量 -> ticker` 排序取 TOP1。
3. TOP2 模式按同样排序取前 2，只股票均分资金。
4. 买入时间：当天 `13:30 / 14:00 / 14:30`。
5. 卖出时间：下一交易日 `10:00 / 10:30 / 11:00`。
6. 若对应买点涨停，则该分笔跳过。
7. 运行结束后自动清理中间验证文件，仅保留当前输出及最近 2 天的历史回测结果。

单票回测：

```powershell
.\.venv\Scripts\python.exe scripts\run_execution_backtest.py --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --selection-mode single --out-prefix <prefix>
```

TOP2 回测：

```powershell
.\.venv\Scripts\python.exe scripts\run_execution_backtest.py --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --selection-mode basket --max-stocks 2 --out-prefix <prefix>
```

最重要输出：

- `reports/logic_second/<prefix>-summary.md`
- `reports/logic_second/<prefix>-days.csv`
- `reports/logic_second/<prefix>-legs.csv`
