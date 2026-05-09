from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HISTORY_CACHE_DIR = PROJECT_ROOT / 'data' / 'processed' / 'recent-history-cache'


def compare_universe(previous_universe: pd.DataFrame, current_universe: pd.DataFrame) -> dict[str, int]:
    previous_codes = set(previous_universe['ticker'].astype(str).str.zfill(6)) if not previous_universe.empty and 'ticker' in previous_universe.columns else set()
    current_codes = set(current_universe['ticker'].astype(str).str.zfill(6)) if not current_universe.empty and 'ticker' in current_universe.columns else set()
    return {
        'retained_count': len(previous_codes & current_codes),
        'added_count': len(current_codes - previous_codes),
        'removed_count': len(previous_codes - current_codes),
    }


def read_cached_history(cache_path: Path) -> pd.DataFrame:
    if not cache_path.exists():
        return pd.DataFrame()
    cached = pd.read_csv(cache_path, parse_dates=['date'], dtype={'ticker': str}, encoding='utf-8-sig')
    if cached.empty:
        return pd.DataFrame()
    cached['ticker'] = cached['ticker'].astype(str).str.zfill(6)
    for column in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        if column in cached.columns:
            cached[column] = pd.to_numeric(cached[column], errors='coerce')
    cached = cached.dropna(subset=['date', 'open', 'high', 'low', 'close', 'volume'])
    if 'name' not in cached.columns:
        cached['name'] = ''
    for column, default in [('is_tradable', True), ('is_limit_up', False), ('is_limit_down', False), ('is_st', False)]:
        if column not in cached.columns:
            cached[column] = default
    cached = cached.sort_values('date').reset_index(drop=True)
    cached['listed_days'] = range(1, len(cached) + 1)
    return cached


def merge_history_frames(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    if cached.empty and fresh.empty:
        return pd.DataFrame()
    if cached.empty:
        merged = fresh.copy()
    elif fresh.empty:
        merged = cached.copy()
    else:
        merged = pd.concat([cached, fresh], ignore_index=True)
    merged = merged.sort_values('date').drop_duplicates(subset=['ticker', 'date'], keep='last').reset_index(drop=True)
    merged['listed_days'] = range(1, len(merged) + 1)
    return merged


def save_cached_history(cache_path: Path, frame: pd.DataFrame, keep_start: pd.Timestamp) -> None:
    if frame.empty:
        return
    trimmed = frame.loc[frame['date'] >= keep_start].copy().sort_values('date').reset_index(drop=True)
    trimmed['listed_days'] = range(1, len(trimmed) + 1)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    trimmed.to_csv(cache_path, index=False, encoding='utf-8-sig')


def fetch_recent_histories_incremental(
    universe: pd.DataFrame,
    start_date: str,
    end_date: str,
    sleep_ms: int,
    fetch_history_batch: Callable[[list[str], str, str], dict[str, pd.DataFrame]],
    normalize_history_frame: Callable[[pd.DataFrame], pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, int]]:
    HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    code_to_name = universe.set_index('ticker')['name'].to_dict()
    codes = universe['ticker'].astype(str).str.zfill(6).tolist()
    all_frames: list[pd.DataFrame] = []
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    keep_start = start_ts - pd.Timedelta(days=30)
    stats = {
        'cache_reused_count': 0,
        'incremental_update_count': 0,
        'full_refresh_count': 0,
    }

    for symbol_index, ticker in enumerate(codes, start=1):
        cache_path = HISTORY_CACHE_DIR / f'{ticker}.csv'
        cached = read_cached_history(cache_path)
        if not cached.empty:
            cached = cached.loc[cached['date'] <= end_ts].copy().reset_index(drop=True)
        merged = cached.copy()
        used_cache_only = False

        if not cached.empty and cached['date'].min() <= start_ts and cached['date'].max() >= end_ts:
            stats['cache_reused_count'] += 1
            used_cache_only = True
        else:
            fetch_start = start_ts
            refresh_mode = 'full'
            if not cached.empty and cached['date'].min() <= start_ts:
                fetch_start = min(end_ts, cached['date'].max() + pd.Timedelta(days=1))
                refresh_mode = 'incremental'

            history_dict: dict[str, pd.DataFrame] = {}
            if fetch_start <= end_ts:
                for attempt in range(3):
                    try:
                        history_dict = fetch_history_batch([ticker], start_date=fetch_start.strftime('%Y-%m-%d'), end_date=end_ts.strftime('%Y-%m-%d'))
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 2:
                            print(f'History symbol {symbol_index}/{len(codes)} failed permanently: {ticker} {exc}')
                        else:
                            wait_seconds = 1.5 * (attempt + 1)
                            print(f'History symbol {symbol_index}/{len(codes)} retry {attempt + 1}: {ticker} {exc}')
                            time.sleep(wait_seconds)

            history = history_dict.get(ticker)
            if history is None and history_dict:
                history = next(iter(history_dict.values()))

            fresh = pd.DataFrame()
            if history is not None and not history.empty:
                fresh = normalize_history_frame(history)
                fresh['ticker'] = ticker
                fresh['name'] = code_to_name.get(ticker, fresh['name'].iloc[0])
                if refresh_mode == 'incremental':
                    stats['incremental_update_count'] += 1
                else:
                    stats['full_refresh_count'] += 1

            merged = merge_history_frames(cached, fresh)
            if not merged.empty:
                save_cached_history(cache_path, merged, keep_start=keep_start)

        usable = merged.loc[merged['date'].between(start_ts, end_ts)].copy() if not merged.empty else pd.DataFrame()
        if not usable.empty:
            all_frames.append(usable)

        print(
            f'Fetched history symbol {symbol_index}/{len(codes)}, symbols with data so far: {len(all_frames)}, '
            f'cache hits: {stats["cache_reused_count"]}, incremental: {stats["incremental_update_count"]}, full refresh: {stats["full_refresh_count"]}'
        )
        if not used_cache_only:
            time.sleep(max(sleep_ms, 0) / 1000.0)

    if not all_frames:
        return pd.DataFrame(), stats
    prices = pd.concat(all_frames, ignore_index=True)
    return prices.sort_values(['ticker', 'date']).reset_index(drop=True), stats
