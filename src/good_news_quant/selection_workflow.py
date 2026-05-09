from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]

from good_news_quant.chip_concentration import analyze_chip_concentration_for_tickers
from good_news_quant.pattern_scanner import PATTERN_CATALOG

PATTERN_DIR = ROOT / 'pattern'
CHIP_RENAME_MAP = {
    'daily_score': '日线筹码集中度',
    'daily_trend': '日线筹码趋势',
    'daily_slope': '日线筹码斜率',
    'daily_window_start': '日线窗口起点',
    'daily_window_end': '日线窗口终点',
    'm60_score': '60分筹码集中度',
    'm60_trend': '60分筹码趋势',
    'm60_slope': '60分筹码斜率',
    'm60_window_start': '60分窗口起点',
    'm60_window_end': '60分窗口终点',
    'm30_score': '30分筹码集中度',
    'm30_trend': '30分筹码趋势',
    'm30_slope': '30分筹码斜率',
    'm30_window_start': '30分窗口起点',
    'm30_window_end': '30分窗口终点',
    'week_score': '周K筹码集中度',
    'week_trend': '周K筹码趋势',
    'week_slope': '周K筹码斜率',
    'week_window_start': '周K窗口起点',
    'week_window_end': '周K窗口终点',
    'uptrend_count': '筹码上行周期数',
    'alignment': '筹码趋势一致性',
}
TIMEFRAME_LABEL_MAP = {'daily': '日线', 'm60': '60分', 'm30': '30分', 'week': '周K'}


def build_pattern_display_map() -> dict[str, dict[str, str]]:
    available_files = {path.stem: path.name for path in PATTERN_DIR.iterdir() if path.is_file()} if PATTERN_DIR.exists() else {}
    display_map: dict[str, dict[str, str]] = {}
    for item in PATTERN_CATALOG:
        file_name = available_files.get(item['name_cn'], item.get('file_name', ''))
        display_map[item['code']] = {
            'pattern_name': Path(file_name).stem if file_name else item['name_cn'],
            'pattern_file_name': file_name,
        }
    return display_map


PATTERN_DISPLAY_MAP = build_pattern_display_map()
PATTERN_NAME_MAP = {code: meta['pattern_name'] for code, meta in PATTERN_DISPLAY_MAP.items()}
PATTERN_FILE_MAP = {code: meta['pattern_file_name'] for code, meta in PATTERN_DISPLAY_MAP.items()}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='基于历史回测报告生成选股结果，并追加筹码集中度过滤。')
    parser.add_argument('--reports-dir', default=str(ROOT / 'reports' / 'logic_first'))
    parser.add_argument('--top-strategies', type=int, default=5)
    parser.add_argument('--lookback-days', type=int, default=30)
    parser.add_argument('--top-stocks', type=int, default=10)
    parser.add_argument('--signal-date', default='', help='可选，指定买点日期 YYYY-MM-DD。默认取最新信号日期。')
    parser.add_argument('--chip-lookback-periods', type=int, default=60, help='筹码集中度窗口（除周K外）使用的最近周期数量（默认 60）。')
    parser.add_argument('--chip-week-lookback-periods', type=int, default=4, help='周K筹码集中度窗口使用的最近周数（默认 4）。')
    parser.add_argument('--chip-timeframes', default='daily,m60,m30,week', help='筹码集中度分析周期，逗号分隔：daily,m60,m30,week。')
    parser.add_argument('--min-chip-uptrend-cycles', type=int, default=3, help='至少多少个周期处于筹码集中上行，股票才保留到最终名单。')
    parser.add_argument('--skip-chip-analysis', action='store_true', help='跳过筹码集中度分析。')
    parser.add_argument('--offline', action='store_true', help='离线模式：不发起网络请求；优先复用既有筹码结果或缓存。')
    return parser.parse_args(argv)


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f'缺少必要文件: {path}')
    return pd.read_csv(path, encoding='utf-8-sig', dtype={'ticker': str}, **kwargs)


def normalize_names(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if 'ticker' in result.columns:
        result['ticker'] = result['ticker'].astype(str).str.zfill(6)
    if 'pattern_code' in result.columns:
        result['策略名称'] = result['pattern_code'].map(PATTERN_NAME_MAP).fillna(result.get('pattern_name_cn', ''))
        result['策略图片'] = result['pattern_code'].map(PATTERN_FILE_MAP).fillna('')
    return result


def load_validation_metadata(reports_dir: Path, signal_events: pd.DataFrame, universe: pd.DataFrame) -> dict[str, object]:
    metadata = {
        'latest_trading_date': signal_events['date'].max(),
        'selected_universe_size': int(len(universe)),
        'downloaded_universe_size': pd.NA,
        'missing_history_count': pd.NA,
        'missing_history_preview': '',
    }
    metadata_path = reports_dir / 'report-metadata.csv'
    if metadata_path.exists():
        metadata_frame = read_csv(metadata_path)
        if not metadata_frame.empty:
            row = metadata_frame.iloc[0]
            if row.get('latest_trading_date'):
                metadata['latest_trading_date'] = pd.Timestamp(row['latest_trading_date'])
            for key in ['selected_universe_size', 'downloaded_universe_size', 'missing_history_count', 'missing_history_preview']:
                if key in row.index:
                    metadata[key] = row[key]
            preview = metadata.get('missing_history_preview', '')
            if pd.isna(preview) or str(preview).strip().lower() == 'nan':
                metadata['missing_history_preview'] = ''
            return metadata
    summary_path = reports_dir / 'summary.md'
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding='utf-8-sig')
        latest_match = re.search(r'Latest available trading date:\s*(\d{4}-\d{2}-\d{2})', summary_text)
        downloaded_match = re.search(r'Universe size with downloaded history:\s*(\d+)', summary_text)
        gap_match = re.search(r'History download gap:\s*(\d+)\s*/\s*(\d+)\s*symbols missing recent history', summary_text)
        missing_match = re.search(r'Missing history symbols:\s*(.+)', summary_text)
        if latest_match:
            metadata['latest_trading_date'] = pd.Timestamp(latest_match.group(1))
        if downloaded_match:
            metadata['downloaded_universe_size'] = int(downloaded_match.group(1))
        if gap_match:
            metadata['missing_history_count'] = int(gap_match.group(1))
            metadata['selected_universe_size'] = int(gap_match.group(2))
        elif not pd.isna(metadata['downloaded_universe_size']) and int(metadata['selected_universe_size']) >= int(metadata['downloaded_universe_size']):
            metadata['missing_history_count'] = int(metadata['selected_universe_size']) - int(metadata['downloaded_universe_size'])
        if missing_match:
            metadata['missing_history_preview'] = missing_match.group(1).strip()
    return metadata


def summarize_recent_strategies(events_recent: pd.DataFrame, trades_recent: pd.DataFrame) -> pd.DataFrame:
    hit_summary = events_recent.groupby('pattern_code', as_index=False).agg(命中次数=('ticker', 'count'))
    executed = trades_recent.loc[trades_recent['trade_executed']].copy()
    trade_summary = (
        executed.groupby('pattern_code', as_index=False)
        .agg(
            执行交易数=('ticker', 'count'),
            胜率=('return_pct', lambda series: float((series > 0).mean())),
            平均收益率=('return_pct', 'mean'),
            中位收益率=('return_pct', 'median'),
        )
        if not executed.empty
        else pd.DataFrame(columns=['pattern_code', '执行交易数', '胜率', '平均收益率', '中位收益率'])
    )
    summary = hit_summary.merge(trade_summary, on='pattern_code', how='left')
    summary['策略名称'] = summary['pattern_code'].map(PATTERN_NAME_MAP)
    summary['策略图片'] = summary['pattern_code'].map(PATTERN_FILE_MAP)
    summary['执行交易数'] = summary['执行交易数'].fillna(0).astype(int)
    ordered = ['pattern_code', '策略名称', '策略图片', '命中次数', '执行交易数', '胜率', '平均收益率', '中位收益率']
    return summary[ordered].sort_values(['平均收益率', '执行交易数', '命中次数'], ascending=[False, False, False]).reset_index(drop=True)


def build_candidate_table(candidate_signals: pd.DataFrame, trades_recent: pd.DataFrame, strategy_recent_summary: pd.DataFrame, universe: pd.DataFrame, top_stocks: int) -> pd.DataFrame:
    if candidate_signals.empty:
        return pd.DataFrame()
    candidate_detail = candidate_signals[['ticker', 'name', 'pattern_code', '策略名称', '策略图片']].copy()
    strategy_scores = strategy_recent_summary[['pattern_code', '平均收益率', '胜率']].rename(columns={'平均收益率': '策略预期收益率', '胜率': '策略预期胜率'})
    stock_strategy_scores = (
        trades_recent.loc[trades_recent['trade_executed']]
        .groupby(['ticker', 'pattern_code'], as_index=False)
        .agg(
            个股历史平均收益率=('return_pct', 'mean'),
            个股历史胜率=('return_pct', lambda series: float((series > 0).mean())),
            个股历史交易数=('return_pct', 'count'),
        )
    )
    candidate_detail = candidate_detail.merge(strategy_scores, on='pattern_code', how='left')
    candidate_detail = candidate_detail.merge(stock_strategy_scores, on=['ticker', 'pattern_code'], how='left')
    aggregated = (
        candidate_detail.groupby(['ticker', 'name'], as_index=False)
        .agg(
            命中策略数=('pattern_code', 'count'),
            命中不同策略数=('pattern_code', 'nunique'),
            命中策略名称=('策略名称', lambda values: ' / '.join(sorted(set(values)))),
            命中策略图片=('策略图片', lambda values: ' / '.join(sorted(set(values)))),
            策略预期收益率=('策略预期收益率', 'mean'),
            策略预期胜率=('策略预期胜率', 'mean'),
            个股历史平均收益率=('个股历史平均收益率', 'mean'),
            个股历史胜率=('个股历史胜率', 'mean'),
            个股历史交易数=('个股历史交易数', 'sum'),
        )
    )
    aggregated = aggregated.merge(
        universe[['ticker', 'quote_volume', 'quote_amount', 'quote_price', 'quote_date', 'quote_time']],
        on='ticker',
        how='left',
    )
    aggregated['个股历史交易数'] = aggregated['个股历史交易数'].fillna(0).astype(int)
    aggregated = aggregated.sort_values(
        ['命中策略数', '个股历史平均收益率', '策略预期收益率', 'quote_volume', 'ticker'],
        ascending=[False, False, False, False, True],
        na_position='last',
    ).head(top_stocks)
    return aggregated.reset_index(drop=True)


def format_pct(value: object) -> str:
    return 'NA' if pd.isna(value) else f'{float(value):.2%}'


def build_final_chip_filtered(top_candidates: pd.DataFrame, min_chip_uptrend_cycles: int) -> pd.DataFrame:
    if top_candidates.empty or '筹码上行周期数' not in top_candidates.columns:
        return pd.DataFrame()
    filtered = top_candidates.loc[top_candidates['筹码上行周期数'].fillna(-1) >= min_chip_uptrend_cycles].copy()
    if filtered.empty:
        return filtered
    filtered = filtered.sort_values(
        ['筹码上行周期数', '日线筹码集中度', '60分筹码集中度', '30分筹码集中度', '策略预期收益率', 'ticker'],
        ascending=[False, False, False, False, False, True],
        na_position='last',
    )
    return filtered.reset_index(drop=True)


def parse_chip_timeframes(raw: str) -> list[str]:
    parts = [part.strip() for part in re.split(r'[,，\\s]+', str(raw or '').strip()) if part.strip()]
    if not parts:
        parts = ['daily', 'm60', 'm30']
    seen: set[str] = set()
    normalized: list[str] = []
    for part in parts:
        if part in seen:
            continue
        if part not in TIMEFRAME_LABEL_MAP:
            raise ValueError(f'Unsupported chip timeframe: {part}')
        seen.add(part)
        normalized.append(part)
    return normalized


def write_summary(
    path: Path,
    latest_trading_date: pd.Timestamp,
    latest_signal_date: pd.Timestamp,
    selection_window_start: pd.Timestamp,
    validation_metadata: dict[str, object],
    top5_strategies: pd.DataFrame,
    strategy_30d: pd.DataFrame,
    top10_stocks: pd.DataFrame,
    final_filtered: pd.DataFrame,
    chip_lookback_periods: int,
    chip_week_lookback_periods: int,
    chip_timeframes: list[str],
    min_chip_uptrend_cycles: int,
) -> None:
    lines = [
        '# A股选股摘要',
        '',
        f'- 实际最新交易日: {latest_trading_date:%Y-%m-%d}',
        f'- 本次买点信号日: {latest_signal_date:%Y-%m-%d}',
        f'- 入选策略数: {len(top5_strategies)}',
        f'- 入选股票数: {len(top10_stocks)}',
        f'- 最终通过筹码筛选股票数: {len(final_filtered)}',
        f'- 筹码筛选条件: 至少 {min_chip_uptrend_cycles}/{len(chip_timeframes)} 周期集中上行',
        '',
        '## TOP5策略',
        '',
        '| 策略名称 | 近30天平均收益率 |',
        '| --- | --- |',
    ]
    for row in strategy_30d.to_dict('records'):
        lines.append(f"| {row['策略名称']} | {format_pct(row['平均收益率'])} |")

    lines.extend(['', '## TOP10候选', '', '| 股票代码 | 股票名称 | 命中策略数 | 命中策略 |', '| --- | --- | --- | --- |'])
    if top10_stocks.empty:
        lines.append('| None | None | 0 | None |')
    else:
        for row in top10_stocks.to_dict('records'):
            lines.append(f"| {row['ticker']} | {row['name']} | {row['命中策略数']} | {row['命中策略名称']} |")

    lines.extend(['', '## 最终买入名单', '', '| 股票代码 | 股票名称 | 命中策略 |', '| --- | --- | --- |'])
    if final_filtered.empty:
        lines.append('| None | None | 本轮无股票通过最终筹码筛选 |')
    else:
        for row in final_filtered.to_dict('records'):
            lines.append(f"| {row['ticker']} | {row['name']} | {row['命中策略名称']} |")

    path.write_text('\n'.join(lines), encoding='utf-8-sig')


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    reports_dir = Path(args.reports_dir)
    chip_timeframes = parse_chip_timeframes(args.chip_timeframes)

    strategy_summary = normalize_names(read_csv(reports_dir / 'strategy-summary.csv'))
    signal_events = normalize_names(read_csv(reports_dir / 'signal-events.csv', parse_dates=['date']))
    signal_trades = normalize_names(read_csv(reports_dir / 'signal-trades.csv', parse_dates=['signal_date', 'entry_date', 'exit_date']))
    universe = read_csv(reports_dir / 'universe.csv')
    if strategy_summary.empty or signal_events.empty:
        raise RuntimeError('历史验证结果为空，请先运行近60天验证。')

    validation_metadata = load_validation_metadata(reports_dir, signal_events, universe)
    latest_trading_date = pd.Timestamp(validation_metadata.get('latest_trading_date') or signal_events['date'].max())
    top5_strategies = (
        strategy_summary.loc[strategy_summary['executed_trades'] > 0]
        .rename(columns={'hit_count': '命中次数', 'executed_trades': '执行交易数', 'win_rate': '胜率', 'avg_return_pct': '平均收益率', 'median_return_pct': '中位收益率', 'pattern_name_cn': '原始策略名称'})
        .sort_values(['平均收益率', '执行交易数', '命中次数'], ascending=[False, False, False])
        .head(args.top_strategies)
        .reset_index(drop=True)
    )

    latest_signal_date = pd.Timestamp(args.signal_date) if args.signal_date else signal_events['date'].max()
    selection_window_start = latest_signal_date - pd.Timedelta(days=args.lookback_days - 1)
    top_codes = top5_strategies['pattern_code'].tolist()
    events_recent = signal_events.loc[signal_events['pattern_code'].isin(top_codes) & (signal_events['date'] >= selection_window_start)].copy()
    trades_recent = signal_trades.loc[signal_trades['pattern_code'].isin(top_codes) & (signal_trades['signal_date'] >= selection_window_start)].copy()
    strategy_30d = summarize_recent_strategies(events_recent, trades_recent).head(args.top_strategies)

    candidate_signals = events_recent.loc[events_recent['date'] == latest_signal_date].copy()
    top10_stocks = build_candidate_table(candidate_signals, trades_recent, strategy_30d, universe, args.top_stocks)
    if not top10_stocks.empty:
        top10_stocks.insert(0, 'signal_date', latest_signal_date.strftime('%Y-%m-%d'))

    chip_summary = pd.DataFrame()
    chip_history = pd.DataFrame(columns=['ticker', 'timeframe', 'timestamp', 'trade_date', 'score'])
    if not args.skip_chip_analysis and not top10_stocks.empty:
        chip_summary, chip_history = analyze_chip_concentration_for_tickers(
            tickers=top10_stocks['ticker'].astype(str).str.zfill(6).tolist(),
            end_date=latest_signal_date,
            lookback_periods=args.chip_lookback_periods,
            week_lookback_periods=args.chip_week_lookback_periods,
            timeframes=chip_timeframes,
            offline=args.offline,
        )
        alignment_col = 'alignment' if 'alignment' in chip_summary.columns else '筹码趋势一致性' if '筹码趋势一致性' in chip_summary.columns else ''
        is_unusable_offline = chip_summary.empty
        if alignment_col:
            alignment_values = chip_summary[alignment_col].fillna('无数据').astype(str)
            is_unusable_offline = is_unusable_offline or alignment_values.eq('无数据').all() or alignment_values.str.startswith('0/').all()
        if args.offline and is_unusable_offline:
            fallback_summary_path = reports_dir / 'selection-chip-concentration.csv'
            fallback_history_path = reports_dir / 'selection-chip-concentration-history.csv'
            if fallback_summary_path.exists():
                chip_summary = read_csv(fallback_summary_path)
            if fallback_history_path.exists():
                try:
                    chip_history = read_csv(fallback_history_path, parse_dates=['时间', '交易日'])
                except Exception:  # noqa: BLE001
                    chip_history = read_csv(fallback_history_path)
        if not chip_summary.empty:
            if 'daily_score' in chip_summary.columns:
                chip_summary = chip_summary.rename(columns=CHIP_RENAME_MAP)
            top10_stocks = top10_stocks.merge(chip_summary, on='ticker', how='left')
        if not chip_history.empty:
            if 'timeframe' in chip_history.columns:
                chip_history['周期'] = chip_history['timeframe'].map(TIMEFRAME_LABEL_MAP).fillna(chip_history['timeframe'])
                chip_history = chip_history.rename(columns={'score': '筹码集中度', 'timestamp': '时间', 'trade_date': '交易日'})
                chip_history = chip_history[['ticker', '周期', '时间', '交易日', '筹码集中度']]

    final_filtered = build_final_chip_filtered(top10_stocks, min_chip_uptrend_cycles=args.min_chip_uptrend_cycles)

    top5_output = top5_strategies.drop(columns=['pattern_code'])
    strategy_30d_output = strategy_30d.drop(columns=['pattern_code'])
    top5_output.to_csv(reports_dir / 'selection-top5-strategies.csv', index=False, encoding='utf-8-sig')
    strategy_30d_output.to_csv(reports_dir / 'selection-top5-last30d-summary.csv', index=False, encoding='utf-8-sig')
    candidate_signals.to_csv(reports_dir / 'selection-latest-signals.csv', index=False, encoding='utf-8-sig')
    top10_stocks.to_csv(reports_dir / 'selection-top10-stocks.csv', index=False, encoding='utf-8-sig')
    final_filtered.to_csv(reports_dir / 'selection-final-chip-filtered.csv', index=False, encoding='utf-8-sig')
    chip_summary.to_csv(reports_dir / 'selection-chip-concentration.csv', index=False, encoding='utf-8-sig')
    chip_history.to_csv(reports_dir / 'selection-chip-concentration-history.csv', index=False, encoding='utf-8-sig')
    write_summary(
        reports_dir / 'selection-summary.md',
        latest_trading_date=latest_trading_date,
        latest_signal_date=latest_signal_date,
        selection_window_start=selection_window_start,
        validation_metadata=validation_metadata,
        top5_strategies=top5_output,
        strategy_30d=strategy_30d_output,
        top10_stocks=top10_stocks,
        final_filtered=final_filtered,
        chip_lookback_periods=args.chip_lookback_periods,
        chip_week_lookback_periods=args.chip_week_lookback_periods,
        chip_timeframes=chip_timeframes,
        min_chip_uptrend_cycles=args.min_chip_uptrend_cycles,
    )

    print(f'最新信号日期: {latest_signal_date:%Y-%m-%d}')
    print('近60天收益率TOP5策略:')
    print(top5_output.to_string(index=False))
    print('最新买点TOP10股票:')
    print(top10_stocks.to_string(index=False))
    print('最终通过筹码筛选股票:')
    print(final_filtered.to_string(index=False) if not final_filtered.empty else '本轮无股票通过最终筹码筛选')


if __name__ == '__main__':
    main()
