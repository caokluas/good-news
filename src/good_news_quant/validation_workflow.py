from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]

from good_news_quant.history_cache import compare_universe, fetch_recent_histories_incremental
from good_news_quant.pattern_scanner import PATTERN_CATALOG, detect_patterns, prepare_pattern_frame


SINA_HEADERS = {
    'Referer': 'http://finance.sina.com.cn/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36',
}
SINA_LINE_RE = re.compile(r'var hq_str_(\w+)="(.*)";')
TENCENT_HEADERS = {
    'Referer': 'https://gu.qq.com/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36',
}
TENCENT_LINE_RE = re.compile(r'v_(\w+)="(.*)";')
MAIN_BOARD_PREFIXES = ('000', '001', '002', '003', '600', '601', '603', '605')
PROXY_ENV_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')
PATTERN_DIR = ROOT / 'pattern'
DELISTING_MARKERS = ('退市',)


@dataclass(slots=True)
class BacktestRule:
    max_hold_days: int
    max_entry_gap: float | None


EXIT_RULES: dict[str, BacktestRule] = {
    'shi_zi_zhang_kou': BacktestRule(max_hold_days=8, max_entry_gap=0.03),
    'wa_keng_mai_niu': BacktestRule(max_hold_days=8, max_entry_gap=None),
    'yang_yang_die_gong': BacktestRule(max_hold_days=8, max_entry_gap=0.04),
    'si_xian_yi_lei': BacktestRule(max_hold_days=10, max_entry_gap=None),
    'yu_yue_long_men': BacktestRule(max_hold_days=10, max_entry_gap=None),
    'kui_hua_xiang_yang': BacktestRule(max_hold_days=8, max_entry_gap=None),
    'mei_ren_ti_tui': BacktestRule(max_hold_days=6, max_entry_gap=None),
    'xu_ri_dong_sheng': BacktestRule(max_hold_days=6, max_entry_gap=None),
    'dao_chui_tou_xian': BacktestRule(max_hold_days=6, max_entry_gap=None),
    'xi_wang_zhi_xing': BacktestRule(max_hold_days=7, max_entry_gap=None),
    'dao_xing_fan_zhuan': BacktestRule(max_hold_days=8, max_entry_gap=0.06),
    'shang_zhang_fen_shou': BacktestRule(max_hold_days=8, max_entry_gap=None),
}


def build_pattern_display_map() -> dict[str, dict[str, str]]:
    available_files = {path.stem: path.name for path in PATTERN_DIR.iterdir() if path.is_file()} if PATTERN_DIR.exists() else {}
    display_map: dict[str, dict[str, str]] = {}
    for item in PATTERN_CATALOG:
        file_name = available_files.get(item['name_cn'], item['file_name'])
        display_map[item['code']] = {
            'pattern_name_cn': Path(file_name).stem,
            'pattern_file_name': file_name,
        }
    return display_map


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Validate 12 handwritten A-share patterns on recent main-board data.')
    parser.add_argument('--end-date', default=str(date.today()), help='Backtest end date in YYYY-MM-DD.')
    parser.add_argument('--output-dir', default=str(ROOT / 'reports' / 'logic_first'))
    parser.add_argument('--signal-days', type=int, default=60, help='Recent calendar days to validate.')
    parser.add_argument('--warmup-days', type=int, default=160, help='Extra calendar days for indicator warmup.')
    parser.add_argument('--quote-batch-size', type=int, default=400, help='Universe query batch size.')
    parser.add_argument('--batch-size', type=int, default=80, help='History download batch size.')
    parser.add_argument('--universe-size', type=int, default=200, help='Number of stocks kept after ranking by latest quote volume.')
    parser.add_argument('--top-n', type=int, default=20, help='Top N stocks to keep in the final ranking.')
    parser.add_argument('--sleep-ms', type=int, default=120, help='Pause between batches in milliseconds.')
    parser.add_argument('--offline', action='store_true', help='Use cached universe + histories only (no network calls).')
    return parser.parse_args(argv)


def disable_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    # Requests on Windows may still pick up system proxy settings (WinINet).
    # Setting NO_PROXY='*' forces direct connections for all hosts.
    os.environ['NO_PROXY'] = '*'
    os.environ['no_proxy'] = '*'


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def fetch_text_via_curl(url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    header_args: list[str] = []
    if headers:
        for key, value in headers.items():
            header_args.extend(['-H', f'{key}: {value}'])
    result = subprocess.run(
        ['curl.exe', '-sS', '-L', '--max-time', str(max(timeout, 1)), *header_args, url],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        raise RuntimeError(f'curl failed ({result.returncode}) for {url}: {stderr}')
    return result.stdout


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((message + '\n').encode('utf-8', errors='backslashreplace'))


def fetch_text(session: requests.Session, url: str, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    try:
        response = session.get(url, timeout=timeout, headers=headers)
        if response.status_code == 403:
            raise requests.HTTPError('403 Forbidden', response=response)
        response.raise_for_status()
        if response.encoding is None or response.encoding.lower() == 'iso-8859-1':
            if 'qt.gtimg.cn' in url or 'hq.sinajs.cn' in url:
                response.encoding = 'gbk'
        return response.text
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 10013:
            return fetch_text_via_curl(url, headers=headers, timeout=timeout)
        raise
    except requests.RequestException as exc:
        if 'WinError 10013' in str(exc):
            return fetch_text_via_curl(url, headers=headers, timeout=timeout)
        raise


def generate_candidate_codes() -> list[str]:
    sh_prefixes = ('600', '601', '603', '605')
    sz_prefixes = ('000', '001', '002', '003')
    candidates: list[str] = []
    for prefix in sh_prefixes:
        candidates.extend([f'sh{prefix}{suffix:03d}' for suffix in range(1000)])
    for prefix in sz_prefixes:
        candidates.extend([f'sz{prefix}{suffix:03d}' for suffix in range(1000)])
    return candidates


def safe_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def is_excluded_stock_name(name: str) -> bool:
    if not name:
        return True
    normalized = str(name).strip()
    if not normalized:
        return True
    if 'ST' in normalized.upper():
        return True
    if normalized.endswith('退'):
        return True
    if any(marker in normalized for marker in DELISTING_MARKERS):
        return True
    return False


def get_universe_tencent(target_date: date, quote_batch_size: int, sleep_ms: int, universe_size: int) -> pd.DataFrame:
    _ = target_date
    session = requests.Session()
    candidates = generate_candidate_codes()
    rows: list[dict[str, object]] = []
    total_batches = math.ceil(len(candidates) / quote_batch_size)

    for batch_index, batch in enumerate(chunked(candidates, quote_batch_size), start=1):
        url = 'https://qt.gtimg.cn/q=' + ','.join(batch)
        text = fetch_text(session, url, headers=TENCENT_HEADERS, timeout=30)
        for line in text.splitlines():
            match = TENCENT_LINE_RE.match(line.strip())
            if not match:
                continue
            market_code, payload = match.groups()
            if not payload:
                continue
            fields = payload.split('~')
            if len(fields) < 7:
                continue
            name = fields[1].strip()
            if is_excluded_stock_name(name):
                continue
            ticker = str(fields[2]).strip().zfill(6)
            if not ticker.startswith(MAIN_BOARD_PREFIXES):
                continue
            price = safe_float(fields[3]) if len(fields) > 3 else float('nan')
            prev_close = safe_float(fields[4]) if len(fields) > 4 else float('nan')
            volume = safe_float(fields[6]) if len(fields) > 6 else float('nan')
            amount = safe_float(fields[37]) if len(fields) > 37 else float('nan')
            if price <= 0 or prev_close <= 0 or volume <= 0:
                continue
            rows.append(
                {
                    'ticker': ticker,
                    'name': name,
                    'market_code': market_code,
                    'quote_price': price,
                    'quote_volume': volume,
                    'quote_amount': amount,
                    'quote_date': '',
                    'quote_time': '',
                }
            )

        print(f'Swept universe batch {batch_index}/{total_batches}, active symbols so far: {len(rows)}')
        time.sleep(max(sleep_ms, 0) / 1000.0)

    if not rows:
        return pd.DataFrame(columns=['ticker', 'name', 'market_code', 'quote_price', 'quote_volume', 'quote_amount', 'quote_date', 'quote_time'])
    universe = pd.DataFrame(rows).drop_duplicates(subset=['ticker']).sort_values(['quote_volume', 'quote_amount', 'ticker'], ascending=[False, False, True]).reset_index(drop=True)
    return universe.head(universe_size).reset_index(drop=True)


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(
        columns={
            '股票代码': 'ticker',
            '股票名称': 'name',
            '日期': 'date',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
        }
    )
    renamed = renamed[['ticker', 'name', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']].copy()
    renamed['ticker'] = renamed['ticker'].astype(str).str.zfill(6)
    renamed['date'] = pd.to_datetime(renamed['date'])
    for column in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        renamed[column] = pd.to_numeric(renamed[column], errors='coerce')
    renamed = renamed.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
    renamed = renamed.sort_values('date').reset_index(drop=True)
    renamed['is_tradable'] = True
    renamed['is_limit_up'] = False
    renamed['is_limit_down'] = False
    renamed['listed_days'] = range(1, len(renamed) + 1)
    renamed['is_st'] = False
    return renamed


def fetch_history_batch_tencent(batch: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
    disable_proxy_env()
    if not batch:
        return {}

    session = requests.Session()
    histories: dict[str, pd.DataFrame] = {}
    for item in batch:
        ticker = str(item).zfill(6)
        symbol = f'{"sh" if ticker.startswith("6") else "sz"}{ticker}'
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start_date},{end_date},640,qfq'
        text = fetch_text(session, url, headers=TENCENT_HEADERS, timeout=30)
        payload = json.loads(text)
        data = (payload.get('data') or {}).get(symbol) or {}
        series = data.get('qfqday') or data.get('day') or []
        if not series:
            continue
        records: list[dict[str, object]] = []
        for row in series:
            if not isinstance(row, list) or len(row) < 6:
                continue
            records.append(
                {
                    '股票代码': ticker,
                    '股票名称': '',
                    '日期': row[0],
                    '开盘': row[1],
                    '收盘': row[2],
                    '最高': row[3],
                    '最低': row[4],
                    '成交量': row[5],
                    '成交额': row[6] if len(row) > 6 else float('nan'),
                }
            )
        if records:
            histories[ticker] = pd.DataFrame.from_records(records)
    return histories


def fetch_recent_histories(universe: pd.DataFrame, start_date: str, end_date: str, batch_size: int, sleep_ms: int) -> pd.DataFrame:
    code_to_name = universe.set_index('ticker')['name'].to_dict()
    all_frames: list[pd.DataFrame] = []
    codes = universe['ticker'].tolist()
    total_batches = math.ceil(len(codes) / batch_size)

    for batch_index, batch in enumerate(chunked(codes, batch_size), start=1):
        history_dict: dict[str, pd.DataFrame] = {}
        for attempt in range(3):
            try:
                history_dict = fetch_history_batch(batch, start_date=start_date, end_date=end_date)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f'History batch {batch_index}/{total_batches} failed permanently: {exc}')
                else:
                    wait_seconds = 1.5 * (attempt + 1)
                    print(f'History batch {batch_index}/{total_batches} retry {attempt + 1}: {exc}')
                    time.sleep(wait_seconds)
        if not history_dict:
            continue

        for code, history in history_dict.items():
            if history is None or history.empty:
                continue
            normalized = normalize_history_frame(history)
            ticker = str(code).zfill(6)
            normalized['ticker'] = ticker
            normalized['name'] = code_to_name.get(ticker, normalized['name'].iloc[0])
            all_frames.append(normalized)

        print(f'Fetched history batch {batch_index}/{total_batches}, symbols with data so far: {len(all_frames)}')
        time.sleep(max(sleep_ms, 0) / 1000.0)

    if not all_frames:
        return pd.DataFrame()
    prices = pd.concat(all_frames, ignore_index=True)
    return prices.sort_values(['ticker', 'date']).reset_index(drop=True)


def build_signal_events(detected: pd.DataFrame, signal_start: pd.Timestamp, pattern_display_map: dict[str, dict[str, str]]) -> pd.DataFrame:
    event_frames: list[pd.DataFrame] = []
    for pattern in PATTERN_CATALOG:
        column = f"signal_{pattern['code']}"
        subset = detected.loc[detected[column] & (detected['date'] >= signal_start)].copy()
        if subset.empty:
            continue
        subset['pattern_code'] = pattern['code']
        subset['pattern_name_cn'] = pattern_display_map[pattern['code']]['pattern_name_cn']
        subset['pattern_file_name'] = pattern_display_map[pattern['code']]['pattern_file_name']
        event_frames.append(subset)
    if not event_frames:
        return pd.DataFrame()
    return pd.concat(event_frames, ignore_index=True).sort_values(['date', 'pattern_code', 'ticker']).reset_index(drop=True)


def stop_price_for_event(group: pd.DataFrame, pos: int, pattern_code: str) -> float:
    signal = group.iloc[pos]
    if pattern_code == 'dao_chui_tou_xian' and pos >= 1:
        return float(group.iloc[pos - 1]['low'])
    if pattern_code == 'xi_wang_zhi_xing':
        lows = [float(signal['low'])]
        if pos >= 1:
            lows.append(float(group.iloc[pos - 1]['low']))
        if pos >= 2:
            lows.append(float(group.iloc[pos - 2]['low']))
        return min(lows)
    if pattern_code in {'xu_ri_dong_sheng', 'shang_zhang_fen_shou'} and pos >= 1:
        return min(float(signal['low']), float(group.iloc[pos - 1]['low']))
    return float(signal['low'])


def evaluate_signal_trade(group: pd.DataFrame, pos: int, pattern_code: str) -> dict[str, object] | None:
    rule = EXIT_RULES[pattern_code]
    if pos + 1 >= len(group):
        return None

    signal = group.iloc[pos]
    entry = group.iloc[pos + 1]
    signal_close = float(signal['close'])
    entry_price = float(entry['open'])
    if signal_close <= 0 or entry_price <= 0:
        return None

    entry_gap = entry_price / signal_close - 1.0
    if rule.max_entry_gap is not None and entry_gap > rule.max_entry_gap:
        return {
            'trade_executed': False,
            'skip_reason': 'entry_gap_too_high',
            'entry_date': pd.NaT,
            'entry_price': float('nan'),
            'exit_date': pd.NaT,
            'exit_price': float('nan'),
            'holding_days': 0,
            'return_pct': float('nan'),
            'exit_reason': 'skipped_gap',
        }

    stop_price = stop_price_for_event(group, pos, pattern_code)
    last_pos = min(pos + rule.max_hold_days, len(group) - 1)
    below_ma30_count = 0

    for exit_pos in range(pos + 1, last_pos + 1):
        row = group.iloc[exit_pos]
        row_close = float(row['close'])
        exit_reason = None

        if pattern_code in {
            'shi_zi_zhang_kou',
            'wa_keng_mai_niu',
            'yang_yang_die_gong',
            'mei_ren_ti_tui',
            'xu_ri_dong_sheng',
            'dao_chui_tou_xian',
            'xi_wang_zhi_xing',
            'shang_zhang_fen_shou',
        }:
            if row_close < stop_price:
                exit_reason = 'stop_loss'
        elif pattern_code == 'si_xian_yi_lei':
            if pd.notna(row['ma20']) and row_close < float(row['ma20']):
                exit_reason = 'close_below_ma20'
        elif pattern_code == 'yu_yue_long_men':
            if row_close < stop_price:
                exit_reason = 'close_below_signal_low'
            if pd.notna(row['ma30']) and row_close < float(row['ma30']):
                below_ma30_count += 1
            else:
                below_ma30_count = 0
            if below_ma30_count >= 2:
                exit_reason = 'two_closes_below_ma30'
        elif pattern_code == 'kui_hua_xiang_yang':
            if row_close < stop_price:
                exit_reason = 'close_below_signal_low'
            if pd.notna(row['ma10']) and row_close < float(row['ma10']):
                exit_reason = exit_reason or 'close_below_ma10'
        elif pattern_code == 'dao_xing_fan_zhuan':
            island_upper = float(group.iloc[pos - 1]['high']) if pos >= 1 else float(signal['high'])
            if row_close <= island_upper:
                exit_reason = 'back_into_island_range'

        if exit_reason is not None:
            return {
                'trade_executed': True,
                'skip_reason': '',
                'entry_date': entry['date'],
                'entry_price': entry_price,
                'exit_date': row['date'],
                'exit_price': row_close,
                'holding_days': int(exit_pos - pos),
                'return_pct': row_close / entry_price - 1.0,
                'exit_reason': exit_reason,
            }

    final_row = group.iloc[last_pos]
    final_reason = 'mark_to_market' if last_pos == len(group) - 1 else 'max_hold'
    return {
        'trade_executed': True,
        'skip_reason': '',
        'entry_date': entry['date'],
        'entry_price': entry_price,
        'exit_date': final_row['date'],
        'exit_price': float(final_row['close']),
        'holding_days': int(last_pos - pos),
        'return_pct': float(final_row['close']) / entry_price - 1.0,
        'exit_reason': final_reason,
    }


def backtest_signal_events(signal_events: pd.DataFrame, detected: pd.DataFrame) -> pd.DataFrame:
    if signal_events.empty:
        return pd.DataFrame()

    grouped_map = {ticker: group.sort_values('date').reset_index(drop=True) for ticker, group in detected.groupby('ticker')}
    trade_rows: list[dict[str, object]] = []

    for event in signal_events.itertuples(index=False):
        group = grouped_map.get(event.ticker)
        if group is None:
            continue
        matches = group.index[group['date'] == event.date]
        if len(matches) == 0:
            continue
        trade = evaluate_signal_trade(group, int(matches[0]), event.pattern_code)
        if trade is None:
            continue
        trade_rows.append(
            {
                'signal_date': event.date,
                'ticker': event.ticker,
                'name': event.name,
                'pattern_code': event.pattern_code,
                'pattern_name_cn': event.pattern_name_cn,
                'signal_close': float(event.close),
                **trade,
            }
        )

    if not trade_rows:
        return pd.DataFrame()
    return pd.DataFrame(trade_rows).sort_values(['signal_date', 'pattern_code', 'ticker']).reset_index(drop=True)


def summarize_strategy_results(
    signal_events: pd.DataFrame,
    trades: pd.DataFrame,
    pattern_display_map: dict[str, dict[str, str]],
) -> pd.DataFrame:
    if signal_events.empty:
        return pd.DataFrame()

    hit_summary = (
        signal_events.groupby(['pattern_code', 'pattern_name_cn'], as_index=False)
        .agg(hit_count=('ticker', 'count'))
        .sort_values(['hit_count', 'pattern_code'], ascending=[False, True])
    )
    pattern_file_map = {code: meta['pattern_file_name'] for code, meta in pattern_display_map.items()}
    executed = trades.loc[trades['trade_executed']].copy() if not trades.empty else pd.DataFrame()
    skipped = trades.loc[~trades['trade_executed']].copy() if not trades.empty else pd.DataFrame()
    if executed.empty:
        result = hit_summary.copy()
        result['executed_trades'] = 0
        result['skipped_trades'] = 0 if skipped.empty else result['pattern_code'].map(skipped['pattern_code'].value_counts()).fillna(0).astype(int)
        result['win_rate'] = float('nan')
        result['avg_return_pct'] = float('nan')
        result['median_return_pct'] = float('nan')
        result['pattern_file_name'] = result['pattern_code'].map(pattern_file_map)
        return result

    trade_summary = (
        executed.groupby(['pattern_code', 'pattern_name_cn'], as_index=False)
        .agg(
            executed_trades=('ticker', 'count'),
            win_rate=('return_pct', lambda series: float((series > 0).mean())),
            avg_return_pct=('return_pct', 'mean'),
            median_return_pct=('return_pct', 'median'),
        )
    )
    skipped_counts = skipped['pattern_code'].value_counts() if not skipped.empty else pd.Series(dtype='int64')
    result = hit_summary.merge(trade_summary, on=['pattern_code', 'pattern_name_cn'], how='left')
    result['executed_trades'] = result['executed_trades'].fillna(0).astype(int)
    result['skipped_trades'] = result['pattern_code'].map(skipped_counts).fillna(0).astype(int)
    result['pattern_file_name'] = result['pattern_code'].map(pattern_file_map)
    return result.sort_values(['hit_count', 'avg_return_pct'], ascending=[False, False]).reset_index(drop=True)


def summarize_top_stocks(signal_events: pd.DataFrame, trades: pd.DataFrame, top_n: int) -> pd.DataFrame:
    if signal_events.empty:
        return pd.DataFrame()

    hit_summary = (
        signal_events.groupby(['ticker', 'name'], as_index=False)
        .agg(
            total_hits=('pattern_code', 'count'),
            distinct_strategies=('pattern_code', 'nunique'),
            strategies_hit=('pattern_name_cn', lambda values: ' / '.join(sorted(set(values)))),
        )
    )

    executed = trades.loc[trades['trade_executed']].copy() if not trades.empty else pd.DataFrame()
    if executed.empty:
        result = hit_summary.copy()
        result['executed_trades'] = 0
        result['avg_return_pct'] = float('nan')
        result['median_return_pct'] = float('nan')
        result['win_rate'] = float('nan')
        return result.sort_values(['total_hits', 'distinct_strategies', 'ticker'], ascending=[False, False, True]).head(top_n)

    trade_summary = (
        executed.groupby(['ticker', 'name'], as_index=False)
        .agg(
            executed_trades=('pattern_code', 'count'),
            avg_return_pct=('return_pct', 'mean'),
            median_return_pct=('return_pct', 'median'),
            win_rate=('return_pct', lambda series: float((series > 0).mean())),
        )
    )
    result = hit_summary.merge(trade_summary, on=['ticker', 'name'], how='left')
    result['executed_trades'] = result['executed_trades'].fillna(0).astype(int)
    return result.sort_values(['total_hits', 'distinct_strategies', 'avg_return_pct', 'ticker'], ascending=[False, False, False, True]).head(top_n)


def summarize_top_stock_strategy_returns(trades: pd.DataFrame, top_stocks: pd.DataFrame) -> pd.DataFrame:
    if trades.empty or top_stocks.empty:
        return pd.DataFrame()
    top_codes = set(top_stocks['ticker'].astype(str))
    executed = trades.loc[trades['trade_executed'] & trades['ticker'].astype(str).isin(top_codes)].copy()
    if executed.empty:
        return pd.DataFrame()
    return (
        executed.groupby(['ticker', 'name', 'pattern_code', 'pattern_name_cn'], as_index=False)
        .agg(
            trade_count=('return_pct', 'count'),
            avg_return_pct=('return_pct', 'mean'),
            median_return_pct=('return_pct', 'median'),
            win_rate=('return_pct', lambda series: float((series > 0).mean())),
        )
        .sort_values(['ticker', 'trade_count', 'avg_return_pct'], ascending=[True, False, False])
        .reset_index(drop=True)
    )


def write_markdown_report(
    path: Path,
    latest_date: pd.Timestamp,
    signal_start: pd.Timestamp,
    universe: pd.DataFrame,
    downloaded_universe_size: int,
    missing_history: pd.DataFrame,
    signal_events: pd.DataFrame,
    strategy_summary: pd.DataFrame,
    top_stocks: pd.DataFrame,
) -> None:
    quote_date = universe['quote_date'].mode().iloc[0] if 'quote_date' in universe.columns and not universe.empty and universe['quote_date'].notna().any() else 'unknown'
    missing_count = len(missing_history)
    lines = [
        '# Recent Pattern Validation',
        '',
        '- Universe: A-share main-board only, no ST, no ChiNext, 10% limit boards only.',
        '- Validation datasource: Tencent daily snapshot + price history.',
        f'- Universe selection: top {len(universe)} stocks by latest quote volume from Tencent snapshot ({quote_date}).',
        '- Buy rule: enter at the next trading day open after a signal.',
        '- Sell rule: exit follows the pattern-specific stop or max holding days.',
        f'- Latest available trading date: {latest_date:%Y-%m-%d}',
        f'- Validation window start: {signal_start:%Y-%m-%d}',
        f'- Universe size with downloaded history: {downloaded_universe_size}',
        f'- History download gap: {missing_count} / {len(universe)} symbols missing recent history.',
        f'- Total pattern hits in window: {0 if signal_events.empty else len(signal_events)}',
        '',
    ]
    if missing_count:
        preview = '、'.join(f"{row.ticker} {row.name}" for row in missing_history.head(10).itertuples(index=False))
        suffix = ' 等' if missing_count > 10 else ''
        lines.extend([
            '## Partial Data Download Gap',
            '',
            f'- Missing history symbols: {preview}{suffix}',
            '',
        ])

    has_trades = not strategy_summary.empty
    lines.extend(['## TOP10 Strategies By Avg Return (盈利幅度排名)', ''])
    if not has_trades:
        lines.extend(['No executable trades were generated in this run.', ''])
    else:
        ranked = (
            strategy_summary.loc[strategy_summary['executed_trades'] > 0]
            .sort_values(['avg_return_pct', 'win_rate', 'executed_trades', 'hit_count'], ascending=[False, False, False, False])
            .head(10)
            .reset_index(drop=True)
        )
        lines.extend([
            '| Rank | Strategy | Pattern File | Avg Return | Win Rate | Executed | Hits |',
            '| --- | --- | --- | --- | --- | --- | --- |',
        ])
        for idx, row in enumerate(ranked.itertuples(index=False), start=1):
            win_rate = 'NA' if pd.isna(row.win_rate) else f'{row.win_rate:.2%}'
            avg_return = 'NA' if pd.isna(row.avg_return_pct) else f'{row.avg_return_pct:.2%}'
            lines.append(
                f'| {idx} | {row.pattern_name_cn} | {row.pattern_file_name} | {avg_return} | {win_rate} | {row.executed_trades} | {row.hit_count} |'
            )
        lines.append('')

    lines.extend(['## Strategy Summary', ''])
    if not has_trades:
        lines.append('No executable trades were generated in this run.')
    else:
        lines.extend([
            '| Strategy | Pattern File | Hits | Executed | Skipped | Win Rate | Avg Return | Median Return |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |',
        ])
        for row in strategy_summary.itertuples(index=False):
            win_rate = 'NA' if pd.isna(row.win_rate) else f'{row.win_rate:.2%}'
            avg_return = 'NA' if pd.isna(row.avg_return_pct) else f'{row.avg_return_pct:.2%}'
            median_return = 'NA' if pd.isna(row.median_return_pct) else f'{row.median_return_pct:.2%}'
            lines.append(
                f'| {row.pattern_name_cn} | {row.pattern_file_name} | {row.hit_count} | {row.executed_trades} | {row.skipped_trades} | {win_rate} | {avg_return} | {median_return} |'
            )

    lines.extend(['', '## Top Stocks By Pattern Hits', ''])
    if top_stocks.empty:
        lines.append('No stock hit any pattern in the validation window.')
    else:
        lines.extend([
            '| Ticker | Name | Total Hits | Distinct Strategies | Executed Trades | Avg Return | Win Rate | Strategies Hit |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |',
        ])
        for row in top_stocks.itertuples(index=False):
            avg_return = 'NA' if pd.isna(row.avg_return_pct) else f'{row.avg_return_pct:.2%}'
            win_rate = 'NA' if pd.isna(row.win_rate) else f'{row.win_rate:.2%}'
            lines.append(
                f'| {row.ticker} | {row.name} | {row.total_hits} | {row.distinct_strategies} | {row.executed_trades} | {avg_return} | {win_rate} | {row.strategies_hit} |'
            )
    path.write_text('\n'.join(lines), encoding='utf-8-sig')


def build_missing_history(universe: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    downloaded = set(prices['ticker'].astype(str).str.zfill(6).unique()) if not prices.empty else set()
    missing = universe.loc[~universe['ticker'].astype(str).str.zfill(6).isin(downloaded), ['ticker', 'name']].copy()
    return missing.sort_values('ticker').reset_index(drop=True)


def write_metadata(
    path: Path,
    latest_date: pd.Timestamp,
    signal_start: pd.Timestamp,
    universe: pd.DataFrame,
    downloaded_universe_size: int,
    missing_history: pd.DataFrame,
) -> None:
    quote_date = universe['quote_date'].mode().iloc[0] if 'quote_date' in universe.columns and not universe.empty and universe['quote_date'].notna().any() else ''
    missing_preview = ' / '.join(f"{row.ticker} {row.name}" for row in missing_history.head(10).itertuples(index=False))
    metadata = pd.DataFrame(
        [
            {
                'latest_trading_date': latest_date.strftime('%Y-%m-%d'),
                'validation_window_start': signal_start.strftime('%Y-%m-%d'),
                'quote_snapshot_date': quote_date,
                'selected_universe_size': len(universe),
                'downloaded_universe_size': downloaded_universe_size,
                'missing_history_count': len(missing_history),
                'has_partial_download_gap': bool(len(missing_history)),
                'missing_history_preview': missing_preview,
            }
        ]
    )
    metadata.to_csv(path, index=False, encoding='utf-8-sig')


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    disable_proxy_env()
    end_date = datetime.strptime(args.end_date, '%Y-%m-%d').date()
    signal_start = pd.Timestamp(end_date - timedelta(days=args.signal_days - 1))
    warmup_start = end_date - timedelta(days=args.warmup_days)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern_display_map = build_pattern_display_map()
    previous_universe_path = output_dir / 'universe.csv'
    if previous_universe_path.exists():
        previous_universe = pd.read_csv(previous_universe_path, dtype={'ticker': str}, encoding='utf-8-sig')
    else:
        previous_universe = pd.DataFrame(columns=['ticker'])

    universe = pd.DataFrame()
    if args.offline:
        universe = previous_universe.copy()
        if universe.empty:
            raise RuntimeError(f'Offline mode requires cached universe at {previous_universe_path}')
        print(f'[offline] Using cached universe: {previous_universe_path}')
        if 'name' in universe.columns:
            universe = universe.loc[~universe['name'].astype(str).map(is_excluded_stock_name)].copy()
    else:
        try:
            universe = get_universe_tencent(target_date=end_date, quote_batch_size=args.quote_batch_size, sleep_ms=args.sleep_ms, universe_size=args.universe_size)
        except Exception as exc:  # noqa: BLE001
            if not previous_universe.empty:
                universe = previous_universe.copy()
                safe_print(f'[warning] Universe sweep failed, falling back to cached universe: {exc}')
            else:
                raise
    if universe.empty:
        raise RuntimeError('No valid A-share main-board universe was discovered from quotes.')
    universe_change = compare_universe(previous_universe, universe)
    universe.to_csv(output_dir / 'universe.csv', index=False, encoding='utf-8-sig')

    def fetch_history_batch_offline(batch: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        _ = (batch, start_date, end_date)
        return {}

    prices, cache_stats = fetch_recent_histories_incremental(
        universe=universe,
        start_date=warmup_start.isoformat(),
        end_date=end_date.isoformat(),
        sleep_ms=args.sleep_ms,
        fetch_history_batch=fetch_history_batch_offline if args.offline else fetch_history_batch_tencent,
        normalize_history_frame=normalize_history_frame,
    )
    if prices.empty:
        raise RuntimeError('No A-share history data was downloaded.')
    missing_history = build_missing_history(universe, prices)

    prepared = prepare_pattern_frame(prices)
    detected = detect_patterns(prepared)
    latest_available = detected['date'].max()
    filtered_signal_start = max(signal_start, latest_available - pd.Timedelta(days=args.signal_days - 1))
    signal_events = build_signal_events(detected, filtered_signal_start, pattern_display_map)
    trades = backtest_signal_events(signal_events, detected)
    strategy_summary = summarize_strategy_results(signal_events, trades, pattern_display_map)
    top_stocks = summarize_top_stocks(signal_events, trades, top_n=args.top_n)
    top_stock_strategy_returns = summarize_top_stock_strategy_returns(trades, top_stocks)

    signal_events.to_csv(output_dir / 'signal-events.csv', index=False, encoding='utf-8-sig')
    trades.to_csv(output_dir / 'signal-trades.csv', index=False, encoding='utf-8-sig')
    strategy_summary.to_csv(output_dir / 'strategy-summary.csv', index=False, encoding='utf-8-sig')
    (
        strategy_summary.loc[strategy_summary['executed_trades'] > 0]
        .sort_values(['avg_return_pct', 'win_rate', 'executed_trades', 'hit_count'], ascending=[False, False, False, False])
        .head(10)
        .reset_index(drop=True)
        .to_csv(output_dir / 'strategy-top10-by-avg-return.csv', index=False, encoding='utf-8-sig')
    )
    top_stocks.to_csv(output_dir / 'top20-stocks.csv', index=False, encoding='utf-8-sig')
    top_stock_strategy_returns.to_csv(output_dir / 'top20-stock-strategy-returns.csv', index=False, encoding='utf-8-sig')
    write_metadata(output_dir / 'report-metadata.csv', latest_available, filtered_signal_start, universe, prices['ticker'].nunique(), missing_history)
    write_markdown_report(
        output_dir / 'summary.md',
        latest_available,
        filtered_signal_start,
        universe,
        prices['ticker'].nunique(),
        missing_history,
        signal_events,
        strategy_summary,
        top_stocks,
    )

    print(f'Latest available trading date: {latest_available:%Y-%m-%d}')
    print(f'Validation window start: {filtered_signal_start:%Y-%m-%d}')
    print(f'Universe size: {prices["ticker"].nunique()}')
    print(f'Missing recent history symbols: {len(missing_history)} / {len(universe)}')
    print(f"Universe change vs previous run: retained {universe_change['retained_count']}, added {universe_change['added_count']}, removed {universe_change['removed_count']}")
    print(f"History cache usage: reused {cache_stats['cache_reused_count']}, incremental {cache_stats['incremental_update_count']}, full refresh {cache_stats['full_refresh_count']}")
    print(f'Signal events: {len(signal_events)}')
    print('Top stocks:')
    print(top_stocks.to_string(index=False))
    print('Strategy summary:')
    print(strategy_summary.to_string(index=False))


if __name__ == '__main__':
    main()





