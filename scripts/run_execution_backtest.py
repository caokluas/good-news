from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / 'src'))

from good_news_quant.validation_workflow import main as run_validation_main

LOGIC_SECOND_DIR = ROOT / 'reports' / 'logic_second'


@dataclass(frozen=True)
class Fees:
    commission_rate: float
    stamp_rate: float


@dataclass(frozen=True)
class BuyExecution:
    shares: int
    buy_value: float
    buy_avg: float | None
    samples: dict[str, float | None]
    limit_up_price: float | None
    limit_up_times: list[str]
    executed_times: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Backtest staged buy/sell execution based on pattern selection outputs.')
    parser.add_argument('--reports-dir', default=str(LOGIC_SECOND_DIR))
    parser.add_argument('--start-date', required=True, help='Backtest start date (buy dates) YYYY-MM-DD.')
    parser.add_argument('--end-date', required=True, help='Backtest end date (buy dates) YYYY-MM-DD.')
    parser.add_argument('--initial-cash', type=float, default=1_000_000.0)
    parser.add_argument('--commission-bps', type=float, default=3.0, help='Commission per side in bps (0.01%%). Default=3bps.')
    parser.add_argument('--stamp-bps', type=float, default=10.0, help='Stamp duty on sell in bps (0.01%%). Default=10bps.')
    parser.add_argument('--max-stocks', type=int, default=10)
    parser.add_argument('--selection-mode', choices=['basket', 'single'], default='basket', help='basket buys up to max-stocks; single buys only the top hit-count + volume candidate.')
    parser.add_argument('--out-prefix', default='', help='Optional output filename prefix.')
    return parser.parse_args()


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f'Missing required file: {path}')
    return pd.read_csv(path, encoding='utf-8-sig', dtype={'ticker': str}, **kwargs)


def _find_trade_calendar(reports_dir: Path, start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    universe = read_csv(reports_dir / 'universe.csv')
    tickers = universe['ticker'].astype(str).str.zfill(6).tolist()
    cache_dir = ROOT / 'data' / 'processed' / 'recent-history-cache'
    trade_dates: set[pd.Timestamp] = set()
    scanned = 0
    for ticker in tickers:
        path = cache_dir / f'{ticker}.csv'
        if not path.exists():
            continue
        try:
            frame = pd.read_csv(path, encoding='utf-8-sig', dtype={'ticker': str}, parse_dates=['date'])
        except Exception:  # noqa: BLE001
            continue
        if frame.empty or 'date' not in frame.columns:
            continue
        scanned += 1
        dates = pd.to_datetime(frame['date'], errors='coerce').dt.normalize().dropna().drop_duplicates()
        window = dates.loc[(dates >= start.normalize()) & (dates <= end.normalize())]
        trade_dates.update(set(window.tolist()))
        # Trade calendar should be identical across most liquid stocks; keep scanning until we likely cover the range end.
        if trade_dates:
            if scanned >= 50 and max(trade_dates) >= end.normalize():
                break
        if scanned >= 200 and len(trade_dates) >= 10:
            break
    calendar = sorted(trade_dates)
    if calendar:
        return calendar
    raise RuntimeError(f'Unable to infer trade calendar from recent-history-cache for {start:%Y-%m-%d}..{end:%Y-%m-%d}.')


def _next_trade_date(calendar: list[pd.Timestamp], day: pd.Timestamp) -> pd.Timestamp | None:
    normalized = pd.Timestamp(day).normalize()
    for idx, item in enumerate(calendar):
        if pd.Timestamp(item).normalize() == normalized:
            if idx + 1 < len(calendar):
                return calendar[idx + 1]
            # Calendar truncated: fallback to next weekday (skip weekends).
            candidate = normalized + pd.Timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate += pd.Timedelta(days=1)
            return candidate
    # If not in calendar (e.g. holiday/weekend), pick next later.
    later = [d for d in calendar if pd.Timestamp(d).normalize() > normalized]
    if later:
        return later[0]
    # Fallback: next weekday (skip weekends) when calendar is truncated.
    candidate = normalized + pd.Timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += pd.Timedelta(days=1)
    return candidate


def _time_targets_for_buy(day: pd.Timestamp) -> list[pd.Timestamp]:
    base = pd.Timestamp(day).normalize()
    return [
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(13, 30))),
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(14, 0))),
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(14, 30))),
    ]


def _time_targets_for_sell(day: pd.Timestamp) -> list[pd.Timestamp]:
    base = pd.Timestamp(day).normalize()
    return [
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(10, 0))),
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(10, 30))),
        pd.Timestamp(datetime.combine(base.to_pydatetime().date(), time(11, 0))),
    ]


def _chip_cache_path(ticker: str, klt: int) -> Path:
    cache_dir = ROOT / 'data' / 'processed' / 'chip-concentration-cache'
    return cache_dir / f'{str(ticker).zfill(6)}_{int(klt)}.csv'


def _daily_history_path(ticker: str) -> Path:
    cache_dir = ROOT / 'data' / 'processed' / 'recent-history-cache'
    return cache_dir / f'{str(ticker).zfill(6)}.csv'


def _load_daily_history(ticker: str) -> pd.DataFrame:
    path = _daily_history_path(ticker)
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path, encoding='utf-8-sig', dtype={'ticker': str}, parse_dates=['date'])
    if frame.empty or 'date' not in frame.columns or 'close' not in frame.columns:
        return pd.DataFrame()
    if 'ticker' in frame.columns:
        frame['ticker'] = frame['ticker'].astype(str).str.zfill(6)
    else:
        frame['ticker'] = str(ticker).zfill(6)
    frame['close'] = pd.to_numeric(frame['close'], errors='coerce')
    frame = frame.dropna(subset=['date', 'close']).sort_values('date').reset_index(drop=True)
    return frame


def _previous_close_for_day(ticker: str, day: pd.Timestamp) -> float | None:
    history = _load_daily_history(ticker)
    if history.empty:
        return None
    before = history.loc[history['date'].dt.normalize() < pd.Timestamp(day).normalize()]
    if before.empty:
        return None
    close = float(before['close'].iloc[-1])
    return close if close > 0 else None


def _round_price_to_cent(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _limit_up_price(prev_close: float | None) -> float | None:
    if prev_close is None or prev_close <= 0:
        return None
    return _round_price_to_cent(float(prev_close) * 1.10)


def _is_limit_up_sample(price: float | None, limit_up_price: float | None) -> bool:
    if price is None or limit_up_price is None:
        return False
    return float(price) >= float(limit_up_price) - 0.005


def _load_or_fetch_30m(ticker: str, *, required_end: pd.Timestamp | None = None) -> pd.DataFrame:
    # Reuse chip-concentration cache format (timestamp/open/high/low/close/volume/trade_date).
    from good_news_quant.chip_concentration import fetch_sina_kline, normalize_quote_history  # local import for speed

    cache_path = _chip_cache_path(ticker, 30)
    if cache_path.exists():
        cached = pd.read_csv(cache_path, encoding='utf-8-sig', parse_dates=['timestamp', 'trade_date'], dtype={'ticker': str})
        if not cached.empty and 'timestamp' in cached.columns and 'open' in cached.columns:
            cached['ticker'] = cached['ticker'].astype(str).str.zfill(6)
            cached = cached.sort_values('timestamp').reset_index(drop=True)
            if required_end is None:
                return cached
            latest = pd.to_datetime(cached['timestamp'], errors='coerce').max()
            if pd.isna(latest) or pd.Timestamp(latest) < pd.Timestamp(required_end):
                # Cache is stale for the required window; refresh.
                pass
            else:
                return cached
    raw = fetch_sina_kline(ticker, klt=30, datalen=3000)
    normalized = normalize_quote_history(raw)
    if normalized.empty:
        return normalized
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(cache_path, index=False, encoding='utf-8-sig')
    return normalized.sort_values('timestamp').reset_index(drop=True)


def _pick_bar_open(frame: pd.DataFrame, target: pd.Timestamp) -> float | None:
    if frame.empty:
        return None
    target = pd.Timestamp(target)
    day_start = target.normalize()
    day_end = day_start + pd.Timedelta(days=1)
    same_day = frame.loc[(frame['timestamp'] >= day_start) & (frame['timestamp'] < day_end)].copy()
    if same_day.empty:
        return None
    exact = same_day.loc[same_day['timestamp'] == target]
    if not exact.empty:
        return float(exact['open'].iloc[0])
    # Prefer the first bar after target within the same day, else the latest before.
    after = same_day.loc[same_day['timestamp'] > target].sort_values('timestamp')
    if not after.empty:
        return float(after['open'].iloc[0])
    before = same_day.loc[same_day['timestamp'] < target].sort_values('timestamp')
    if not before.empty:
        return float(before['open'].iloc[-1])
    return None


def _avg_price(frame: pd.DataFrame, targets: list[pd.Timestamp]) -> tuple[float | None, dict[str, float | None]]:
    samples: dict[str, float | None] = {}
    values: list[float] = []
    for ts in targets:
        value = _pick_bar_open(frame, ts)
        samples[str(ts)] = value
        if value is not None and value > 0:
            values.append(float(value))
    if not values:
        return None, samples
    return float(sum(values) / len(values)), samples


def _staged_buy_execution(frame: pd.DataFrame, targets: list[pd.Timestamp], cash_per: float, prev_close: float | None) -> BuyExecution:
    samples: dict[str, float | None] = {}
    limit_up_times: list[str] = []
    executed_times: list[str] = []
    limit_price = _limit_up_price(prev_close)
    slice_cash = cash_per / max(len(targets), 1)
    shares_total = 0
    buy_value = 0.0

    for target in targets:
        value = _pick_bar_open(frame, target)
        samples[str(target)] = value
        label = pd.Timestamp(target).strftime('%H:%M')
        if value is None or value <= 0:
            continue
        if _is_limit_up_sample(value, limit_price):
            limit_up_times.append(label)
            continue
        shares = int((slice_cash / float(value)) // 100) * 100
        if shares <= 0:
            continue
        shares_total += shares
        buy_value += shares * float(value)
        executed_times.append(label)

    buy_avg = (buy_value / shares_total) if shares_total > 0 else None
    return BuyExecution(
        shares=shares_total,
        buy_value=buy_value,
        buy_avg=buy_avg,
        samples=samples,
        limit_up_price=limit_price,
        limit_up_times=limit_up_times,
        executed_times=executed_times,
    )


def _top_strategies_60d(strategy_summary: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    usable = strategy_summary.loc[strategy_summary['executed_trades'] > 0].copy()
    if usable.empty:
        return usable
    return (
        usable.sort_values(['avg_return_pct', 'win_rate', 'executed_trades', 'hit_count'], ascending=[False, False, False, False])
        .head(top_n)
        .reset_index(drop=True)
    )


def _best_strategy_by_win_rate(strategy_summary: pd.DataFrame) -> pd.Series | None:
    usable = strategy_summary.loc[strategy_summary['executed_trades'] > 0].copy()
    if usable.empty:
        return None
    ordered = usable.sort_values(['win_rate', 'avg_return_pct', 'executed_trades', 'hit_count'], ascending=[False, False, False, False])
    return ordered.iloc[0]


def _top_strategies_30d(trades: pd.DataFrame, top_codes_60d: list[str], asof: pd.Timestamp, top_n: int = 5) -> list[str]:
    if not top_codes_60d:
        return []
    window_start = (pd.Timestamp(asof).normalize() - pd.Timedelta(days=29)).normalize()
    recent = trades.loc[(trades['pattern_code'].isin(top_codes_60d)) & (trades['signal_date'] >= window_start) & (trades['signal_date'] <= pd.Timestamp(asof))].copy()
    if recent.empty:
        return top_codes_60d[:top_n]
    summary = (
        recent.groupby('pattern_code', as_index=False)
        .agg(executed_trades=('return_pct', 'size'), win_rate=('return_pct', lambda s: float((s > 0).mean())), avg_return_pct=('return_pct', 'mean'))
        .sort_values(['avg_return_pct', 'win_rate', 'executed_trades'], ascending=[False, False, False])
        .head(top_n)
    )
    return summary['pattern_code'].astype(str).tolist()


def _select_candidates_for_day(
    day: pd.Timestamp,
    signal_events: pd.DataFrame,
    trades: pd.DataFrame,
    universe: pd.DataFrame,
    strategy_summary: pd.DataFrame,
    *,
    max_stocks: int,
) -> tuple[pd.DataFrame, str, str]:
    top5_60d = _top_strategies_60d(strategy_summary, top_n=5)
    top_codes_60d = top5_60d['pattern_code'].astype(str).tolist() if not top5_60d.empty else []
    top_codes_30d = _top_strategies_30d(trades, top_codes_60d, asof=day, top_n=5) if top_codes_60d else []

    events_day = signal_events.loc[signal_events['date'].dt.normalize() == pd.Timestamp(day).normalize()].copy()
    # Primary: signals from 30d-reviewed top strategies.
    primary = events_day.loc[events_day['pattern_code'].isin(top_codes_30d)].copy() if top_codes_30d else pd.DataFrame()
    if not primary.empty:
        reason = 'selected_top_strategies'
        used_strategy = 'TOP5(60d)->TOP5(30d)'
        grouped = (
            primary.groupby(['ticker', 'name'], as_index=False)
            .agg(命中策略数=('pattern_code', 'size'), 命中策略名称=('pattern_name_cn', lambda s: ' / '.join(sorted(set(map(str, s))))))
        )
        grouped = grouped.rename(columns={'命中策略名称': 'hit_patterns'})
        merged = grouped.merge(universe[['ticker', 'quote_volume']].copy(), on='ticker', how='left')
        merged = merged.sort_values(['命中策略数', 'quote_volume', 'ticker'], ascending=[False, False, True]).head(max_stocks).reset_index(drop=True)
        return merged, used_strategy, reason

    # Fallback: pick the single best win-rate strategy and use its signals of the day.
    best = _best_strategy_by_win_rate(strategy_summary)
    if best is None:
        return pd.DataFrame(), '', 'no_strategy_available'
    best_code = str(best['pattern_code'])
    fallback = events_day.loc[events_day['pattern_code'] == best_code].copy()
    if fallback.empty:
        return pd.DataFrame(), best.get('pattern_name_cn', ''), 'fallback_no_signals'
    grouped = (
        fallback.groupby(['ticker', 'name'], as_index=False)
        .agg(命中策略数=('pattern_code', 'size'), 命中策略名称=('pattern_name_cn', lambda s: ' / '.join(sorted(set(map(str, s))))))
    )
    grouped = grouped.rename(columns={'命中策略名称': 'hit_patterns'})
    merged = grouped.merge(universe[['ticker', 'quote_volume']].copy(), on='ticker', how='left')
    merged = merged.sort_values(['quote_volume', 'ticker'], ascending=[False, True]).head(max_stocks).reset_index(drop=True)
    return merged, str(best.get('pattern_name_cn', '')), 'fallback_best_win_rate'


def _fees_from_args(args: argparse.Namespace) -> Fees:
    return Fees(commission_rate=float(args.commission_bps) / 10_000.0, stamp_rate=float(args.stamp_bps) / 10_000.0)


def main() -> None:
    args = parse_args()
    reports_dir = Path(args.reports_dir)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    if end < start:
        raise ValueError('end-date must be >= start-date')

    signal_days = max(60, (end.normalize() - start.normalize()).days + 60)
    validation_args = [
        '--end-date',
        end.strftime('%Y-%m-%d'),
        '--output-dir',
        str(reports_dir),
        '--signal-days',
        str(signal_days),
        '--warmup-days',
        '220',
        '--universe-size',
        '200',
        '--top-n',
        '20',
    ]
    run_validation_main(validation_args)

    # Load regression outputs produced by run_recent_pattern_validation.py.
    signal_events = read_csv(reports_dir / 'signal-events.csv', parse_dates=['date'])
    signal_trades = read_csv(reports_dir / 'signal-trades.csv', parse_dates=['signal_date', 'entry_date', 'exit_date'])
    universe = read_csv(reports_dir / 'universe.csv')
    strategy_summary = read_csv(reports_dir / 'strategy-summary.csv')
    for col in ['ticker', 'name', 'quote_volume']:
        if col not in universe.columns:
            raise RuntimeError(f'Universe missing column: {col}')
    universe['ticker'] = universe['ticker'].astype(str).str.zfill(6)
    signal_events['ticker'] = signal_events['ticker'].astype(str).str.zfill(6)
    signal_trades['ticker'] = signal_trades['ticker'].astype(str).str.zfill(6)

    # Ensure pattern names exist in signal events.
    if 'pattern_name_cn' not in signal_events.columns:
        raise RuntimeError('signal-events.csv missing pattern_name_cn; please regenerate reports.')

    # Infer trading calendar including the day after end for exits.
    calendar = _find_trade_calendar(reports_dir, start, end + pd.Timedelta(days=7))
    trade_days = [d for d in calendar if pd.Timestamp(d).normalize() >= start.normalize() and pd.Timestamp(d).normalize() <= end.normalize()]

    fees = _fees_from_args(args)
    cash = float(args.initial_cash)
    equity = cash

    per_leg_rows: list[dict[str, object]] = []
    per_day_rows: list[dict[str, object]] = []

    for day in trade_days:
        sell_day = _next_trade_date(calendar, day)
        if sell_day is None:
            break

        candidates, used_strategy, reason = _select_candidates_for_day(
            day=day,
            signal_events=signal_events,
            trades=signal_trades,
            universe=universe,
            strategy_summary=strategy_summary,
            max_stocks=1 if args.selection_mode == 'single' else int(args.max_stocks),
        )
        if candidates.empty:
            per_day_rows.append(
                {
                    'buy_date': pd.Timestamp(day).strftime('%Y-%m-%d'),
                    'sell_date': pd.Timestamp(sell_day).strftime('%Y-%m-%d'),
                    'strategy': used_strategy,
                    'reason': reason,
                    'tickers': '',
                    'num_stocks': 0,
                    'pnl_net': 0.0,
                    'equity_after': equity,
                    'win': False,
                }
            )
            continue

        tickers = candidates['ticker'].astype(str).str.zfill(6).tolist()
        patterns = candidates.set_index('ticker')['hit_patterns'].to_dict() if 'hit_patterns' in candidates.columns else {}
        cash_per = cash / max(len(tickers), 1)
        day_pnl_net = 0.0
        invested_total = 0.0
        executed_tickers: list[str] = []
        day_limit_up_skipped_tickers: list[str] = []
        day_limit_up_skipped_slices = 0

        for row in candidates.itertuples(index=False):
            ticker = str(row.ticker).zfill(6)
            name = str(row.name)
            hit_patterns = getattr(row, 'hit_patterns', '') if hasattr(row, 'hit_patterns') else str(patterns.get(ticker, ''))
            buy_targets = _time_targets_for_buy(day)
            sell_targets = _time_targets_for_sell(sell_day)
            required_end = sell_targets[-1]
            bar30 = _load_or_fetch_30m(ticker, required_end=required_end)
            prev_close = _previous_close_for_day(ticker, day)
            buy_execution = _staged_buy_execution(bar30, buy_targets, cash_per, prev_close)
            buy_avg = buy_execution.buy_avg
            buy_samples = buy_execution.samples
            sell_avg, sell_samples = _avg_price(bar30, sell_targets)
            day_limit_up_skipped_slices += len(buy_execution.limit_up_times)
            if buy_execution.limit_up_times:
                day_limit_up_skipped_tickers.append(ticker)

            if buy_execution.shares <= 0:
                if buy_execution.limit_up_times:
                    note = f"buy_limit_up_no_fill:{','.join(buy_execution.limit_up_times)}"
                else:
                    note = 'missing_30m_buy_price' if buy_avg is None else 'insufficient_cash_for_lot'
                per_leg_rows.append(
                    {
                        'buy_date': pd.Timestamp(day).strftime('%Y-%m-%d'),
                        'sell_date': pd.Timestamp(sell_day).strftime('%Y-%m-%d'),
                        'ticker': ticker,
                        'name': name,
                        'strategy': used_strategy,
                        'reason': reason,
                        'hit_patterns': hit_patterns,
                        'shares': 0,
                        'buy_price_1330': buy_samples.get(str(buy_targets[0])),
                        'buy_price_1400': buy_samples.get(str(buy_targets[1])),
                        'buy_price_1430': buy_samples.get(str(buy_targets[2])),
                        'buy_price_avg': buy_avg,
                        'sell_price_avg': sell_avg,
                        'limit_up_price': buy_execution.limit_up_price,
                        'buy_executed_times': ' / '.join(buy_execution.executed_times),
                        'buy_limit_up_times': ' / '.join(buy_execution.limit_up_times),
                        'buy_skipped_limit_up_count': len(buy_execution.limit_up_times),
                        'pnl_net': 0.0,
                        'win': False,
                        'note': note,
                    }
                )
                continue

            if sell_avg is None or sell_avg <= 0:
                per_leg_rows.append(
                    {
                        'buy_date': pd.Timestamp(day).strftime('%Y-%m-%d'),
                        'sell_date': pd.Timestamp(sell_day).strftime('%Y-%m-%d'),
                        'ticker': ticker,
                        'name': name,
                        'strategy': used_strategy,
                        'reason': reason,
                        'hit_patterns': hit_patterns,
                        'shares': buy_execution.shares,
                        'buy_price_1330': buy_samples.get(str(buy_targets[0])),
                        'buy_price_1400': buy_samples.get(str(buy_targets[1])),
                        'buy_price_1430': buy_samples.get(str(buy_targets[2])),
                        'buy_price_avg': buy_avg,
                        'sell_price_avg': sell_avg,
                        'limit_up_price': buy_execution.limit_up_price,
                        'buy_executed_times': ' / '.join(buy_execution.executed_times),
                        'buy_limit_up_times': ' / '.join(buy_execution.limit_up_times),
                        'buy_skipped_limit_up_count': len(buy_execution.limit_up_times),
                        'pnl_net': 0.0,
                        'win': False,
                        'note': 'missing_30m_sell_price',
                    }
                )
                continue

            shares = buy_execution.shares
            buy_value = buy_execution.buy_value
            sell_value = shares * sell_avg
            buy_fee = buy_value * fees.commission_rate
            sell_fee = sell_value * fees.commission_rate + sell_value * fees.stamp_rate
            pnl_gross = sell_value - buy_value
            pnl_net = pnl_gross - buy_fee - sell_fee

            invested_total += buy_value + buy_fee
            day_pnl_net += pnl_net
            executed_tickers.append(ticker)

            per_leg_rows.append(
                {
                    'buy_date': pd.Timestamp(day).strftime('%Y-%m-%d'),
                    'sell_date': pd.Timestamp(sell_day).strftime('%Y-%m-%d'),
                    'ticker': ticker,
                    'name': name,
                    'strategy': used_strategy,
                    'reason': reason,
                    'hit_patterns': hit_patterns,
                    'shares': shares,
                    'buy_price_1330': buy_samples.get(str(_time_targets_for_buy(day)[0])),
                    'buy_price_1400': buy_samples.get(str(_time_targets_for_buy(day)[1])),
                    'buy_price_1430': buy_samples.get(str(_time_targets_for_buy(day)[2])),
                    'buy_price_avg': buy_avg,
                    'sell_price_1000': sell_samples.get(str(_time_targets_for_sell(sell_day)[0])),
                    'sell_price_1030': sell_samples.get(str(_time_targets_for_sell(sell_day)[1])),
                    'sell_price_1100': sell_samples.get(str(_time_targets_for_sell(sell_day)[2])),
                    'sell_price_avg': sell_avg,
                    'return_pct_gross': (sell_avg / buy_avg) - 1.0,
                    'pnl_gross': pnl_gross,
                    'fees': buy_fee + sell_fee,
                    'pnl_net': pnl_net,
                    'win': pnl_net > 0,
                    'limit_up_price': buy_execution.limit_up_price,
                    'buy_executed_times': ' / '.join(buy_execution.executed_times),
                    'buy_limit_up_times': ' / '.join(buy_execution.limit_up_times),
                    'buy_skipped_limit_up_count': len(buy_execution.limit_up_times),
                    'note': f"buy_limit_up_partial_fill:{','.join(buy_execution.limit_up_times)}" if buy_execution.limit_up_times else '',
                }
            )

        # Update equity/cash: all positions closed next morning, so equity changes only by realized pnl.
        equity = equity + day_pnl_net
        cash = equity
        per_day_rows.append(
            {
                'buy_date': pd.Timestamp(day).strftime('%Y-%m-%d'),
                'sell_date': pd.Timestamp(sell_day).strftime('%Y-%m-%d'),
                'strategy': used_strategy,
                'reason': reason,
                'candidate_tickers': ','.join(tickers),
                'candidate_hit_patterns': ' | '.join(sorted(set([str(patterns.get(t, '') or '').strip() for t in tickers if str(patterns.get(t, '') or '').strip()]))),
                'tickers': ','.join(executed_tickers),
                'hit_patterns': ' | '.join(sorted(set([str(patterns.get(t, '') or '').strip() for t in executed_tickers if str(patterns.get(t, '') or '').strip()]))),
                'num_stocks': len(executed_tickers),
                'invested_estimated': invested_total,
                'limit_up_skipped_tickers': ','.join(sorted(set(day_limit_up_skipped_tickers))),
                'limit_up_skipped_slices': day_limit_up_skipped_slices,
                'pnl_net': day_pnl_net,
                'equity_after': equity,
                'win': day_pnl_net > 0,
            }
        )

    out_prefix = args.out_prefix.strip()
    if not out_prefix:
        out_prefix = f'execution-backtest_{start:%Y%m%d}_{end:%Y%m%d}'
    out_dir = reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    legs = pd.DataFrame(per_leg_rows)
    days = pd.DataFrame(per_day_rows)
    legs.to_csv(out_dir / f'{out_prefix}-legs.csv', index=False, encoding='utf-8-sig')
    days.to_csv(out_dir / f'{out_prefix}-days.csv', index=False, encoding='utf-8-sig')

    executed_days = days.loc[days['num_stocks'] > 0].copy()
    win_rate = float((executed_days['pnl_net'] > 0).mean()) if not executed_days.empty else float('nan')
    limit_up_skipped_slices = int(legs['buy_skipped_limit_up_count'].fillna(0).sum()) if 'buy_skipped_limit_up_count' in legs.columns else 0
    limit_up_no_fill_legs = int(legs['note'].fillna('').astype(str).str.startswith('buy_limit_up_no_fill').sum()) if 'note' in legs.columns else 0
    limit_up_partial_legs = int(legs['note'].fillna('').astype(str).str.startswith('buy_limit_up_partial_fill').sum()) if 'note' in legs.columns else 0
    summary_lines = [
        '# 分批买卖执行回测摘要',
        '',
        f'- Buy date range: {start:%Y-%m-%d} .. {end:%Y-%m-%d}',
        f'- Initial cash: {args.initial_cash:.2f}',
        f'- Ending equity: {equity:.2f}',
        f'- Net P&L: {equity - float(args.initial_cash):.2f}',
        f'- Selection mode: {args.selection_mode}',
        f'- Executed trade days: {int(len(executed_days))}',
        f'- Win rate (by day): {"NA" if pd.isna(win_rate) else f"{win_rate:.2%}"}',
        f'- Fees: commission {fees.commission_rate:.4%} per side, stamp {fees.stamp_rate:.4%} on sell',
        '- Buy limit-up filter: enabled; each 13:30/14:00/14:30 tranche is skipped when sampled open price is at the 10% limit-up price.',
        f'- Buy limit-up skipped slices: {limit_up_skipped_slices}',
        f'- Buy limit-up no-fill legs: {limit_up_no_fill_legs}',
        f'- Buy limit-up partial-fill legs: {limit_up_partial_legs}',
        '',
        '## 输出文件',
        '',
        f'- {out_prefix}-days.csv：每天选股/策略/总盈亏',
        f'- {out_prefix}-legs.csv：每只股票的买卖分时与盈亏明细',
    ]
    (out_dir / f'{out_prefix}-summary.md').write_text('\n'.join(summary_lines), encoding='utf-8-sig')
    _cleanup_execution_reports(reports_dir, out_prefix)


def _cleanup_execution_reports(reports_dir: Path, out_prefix: str) -> None:
    keep = {
        f'{out_prefix}-summary.md',
        f'{out_prefix}-days.csv',
        f'{out_prefix}-legs.csv',
    }
    transient = {
        'universe.csv',
        'top20-stocks.csv',
        'top20-stock-strategy-returns.csv',
        'summary.md',
        'strategy-top10-by-avg-return.csv',
        'strategy-summary.csv',
        'signal-trades.csv',
        'signal-events.csv',
        'report-metadata.csv',
    }
    cutoff = datetime.now() - timedelta(days=2)
    if not reports_dir.exists():
        return
    for path in reports_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in keep:
            continue
        if path.name in transient:
            path.unlink()
            continue
        if path.suffix.lower() not in {'.md', '.csv'}:
            continue
        if not (
            path.name.endswith('-summary.md')
            or path.name.endswith('-days.csv')
            or path.name.endswith('-legs.csv')
        ):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        if modified < cutoff:
            path.unlink()


if __name__ == '__main__':
    main()
