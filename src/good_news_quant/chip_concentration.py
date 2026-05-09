from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHIP_CACHE_DIR = PROJECT_ROOT / 'data' / 'processed' / 'chip-concentration-cache'
RECENT_HISTORY_CACHE_DIR = PROJECT_ROOT / 'data' / 'processed' / 'recent-history-cache'

@dataclass(frozen=True)
class TimeframeConfig:
    label: str
    klt: int
    min_points: int
    trend_points: int


TIMEFRAME_CONFIGS = [
    TimeframeConfig(label='daily', klt=101, min_points=4, trend_points=4),
    TimeframeConfig(label='m60', klt=60, min_points=8, trend_points=10),
    TimeframeConfig(label='m30', klt=30, min_points=12, trend_points=20),
    TimeframeConfig(label='week', klt=102, min_points=1, trend_points=4),
]
TIMEFRAME_CONFIG_MAP = {config.label: config for config in TIMEFRAME_CONFIGS}
PROXY_ENV_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')


def disable_proxy_env() -> None:
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ['NO_PROXY'] = '*'
    os.environ['no_proxy'] = '*'


def normalize_quote_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    renamed = frame.rename(
        columns={
            '股票名称': 'name',
            '股票代码': 'ticker',
            '日期': 'timestamp',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
            '振幅': 'amplitude_pct',
            '涨跌幅': 'change_pct',
            '涨跌额': 'change_amount',
            '换手率': 'turnover_pct',
        }
    ).copy()
    renamed['timestamp'] = pd.to_datetime(renamed['timestamp'])
    renamed['trade_date'] = renamed['timestamp'].dt.normalize()
    for column in ['open', 'close', 'high', 'low', 'volume', 'amount', 'amplitude_pct', 'change_pct', 'change_amount', 'turnover_pct']:
        if column in renamed.columns:
            renamed[column] = pd.to_numeric(renamed[column], errors='coerce')
    renamed = renamed.dropna(subset=['timestamp', 'open', 'close', 'high', 'low', 'volume'])
    return renamed.sort_values('timestamp').reset_index(drop=True)


SINA_KLINE_URL = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData'
SINA_HEADERS = {
    'Referer': 'https://finance.sina.com.cn/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36',
}


def _sina_symbol(ticker: str) -> str:
    code = str(ticker).zfill(6)
    exchange = 'sh' if code.startswith(('6',)) else 'sz'
    return f'{exchange}{code}'


def _sina_scale_from_klt(klt: int) -> int:
    if int(klt) == 101:
        return 240
    if int(klt) == 60:
        return 60
    if int(klt) == 30:
        return 30
    raise ValueError(f'Unsupported klt={klt} for Sina KLine API.')


def fetch_sina_kline(ticker: str, klt: int, datalen: int) -> pd.DataFrame:
    disable_proxy_env()
    symbol = _sina_symbol(ticker)
    scale = _sina_scale_from_klt(klt)
    params = {'symbol': symbol, 'scale': str(scale), 'ma': 'no', 'datalen': str(int(datalen))}

    def fetch_json_via_curl() -> object:
        prepared = requests.Request('GET', SINA_KLINE_URL, params=params, headers=SINA_HEADERS).prepare()
        url = prepared.url or SINA_KLINE_URL
        header_args: list[str] = []
        for key, value in SINA_HEADERS.items():
            header_args.extend(['-H', f'{key}: {value}'])
        result = subprocess.run(
            ['curl.exe', '-sS', '-L', '--max-time', '30', *header_args, url],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed ({result.returncode}) for {url}: {(result.stderr or '').strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else []

    try:
        response = requests.get(SINA_KLINE_URL, params=params, headers=SINA_HEADERS, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 10013:
            payload = fetch_json_via_curl()
        else:
            raise
    except requests.RequestException:
        payload = fetch_json_via_curl()

    if not payload:
        return pd.DataFrame()
    frame = pd.DataFrame(payload)
    if frame.empty or 'day' not in frame.columns:
        return pd.DataFrame()
    frame = frame.rename(columns={'day': 'timestamp'}).copy()
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], errors='coerce')
    frame['trade_date'] = frame['timestamp'].dt.normalize()
    for column in ['open', 'close', 'high', 'low', 'volume']:
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
    frame['ticker'] = str(ticker).zfill(6)
    frame = frame.dropna(subset=['timestamp', 'open', 'close', 'high', 'low', 'volume'])
    return frame.sort_values('timestamp').reset_index(drop=True)


def _chip_cache_path(ticker: str, klt: int) -> Path:
    return CHIP_CACHE_DIR / f'{str(ticker).zfill(6)}_{int(klt)}.csv'


def read_recent_daily_history(ticker: str, end_date: pd.Timestamp) -> pd.DataFrame:
    cache_path = RECENT_HISTORY_CACHE_DIR / f'{str(ticker).zfill(6)}.csv'
    if not cache_path.exists():
        return pd.DataFrame()
    cached = pd.read_csv(cache_path, encoding='utf-8-sig', parse_dates=['date'], dtype={'ticker': str})
    if cached.empty:
        return pd.DataFrame()
    cached = cached.rename(columns={'date': 'timestamp'}).copy()
    cached['timestamp'] = pd.to_datetime(cached['timestamp'], errors='coerce')
    cached['trade_date'] = cached['timestamp'].dt.normalize()
    cached = cached.loc[cached['timestamp'] <= pd.Timestamp(end_date)].copy().reset_index(drop=True)
    for column in ['open', 'close', 'high', 'low', 'volume']:
        if column in cached.columns:
            cached[column] = pd.to_numeric(cached[column], errors='coerce')
    cached['ticker'] = cached['ticker'].astype(str).str.zfill(6)
    cached = cached.dropna(subset=['timestamp', 'open', 'close', 'high', 'low', 'volume'])
    return cached.sort_values('timestamp').reset_index(drop=True)


def build_weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    normalized = daily.sort_values('trade_date').copy()
    week_key = normalized['trade_date'].dt.to_period('W-FRI')
    grouped = normalized.groupby(week_key, sort=True)
    weekly = grouped.agg(
        open=('open', 'first'),
        close=('close', 'last'),
        high=('high', 'max'),
        low=('low', 'min'),
        volume=('volume', 'sum'),
        trade_date=('trade_date', 'max'),
    )
    weekly = weekly.dropna(subset=['open', 'close', 'high', 'low', 'volume', 'trade_date']).reset_index(drop=True)
    weekly['timestamp'] = pd.to_datetime(weekly['trade_date'])
    weekly.insert(0, 'ticker', normalized['ticker'].iloc[0])
    weekly = weekly[['ticker', 'timestamp', 'trade_date', 'open', 'close', 'high', 'low', 'volume']].copy()
    return weekly.sort_values('timestamp').reset_index(drop=True)


def read_cached_timeframe_history(ticker: str, klt: int, end_date: pd.Timestamp, lookback_periods: int) -> pd.DataFrame: 
    cache_path = _chip_cache_path(ticker, klt) 
    if not cache_path.exists(): 
        return pd.DataFrame() 
    cached = pd.read_csv(cache_path, encoding='utf-8-sig', parse_dates=['timestamp', 'trade_date'], dtype={'ticker': str}) 
    if cached.empty: 
        return pd.DataFrame() 
    cached['ticker'] = cached['ticker'].astype(str).str.zfill(6) 
    cached = cached.loc[cached['timestamp'] <= pd.Timestamp(end_date)].copy().reset_index(drop=True) 
    cached = keep_last_periods(cached, lookback_periods=lookback_periods) 
    return cached 


def save_cached_timeframe_history(ticker: str, klt: int, frame: pd.DataFrame, end_date: pd.Timestamp) -> None:
    if frame.empty:
        return
    CHIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _chip_cache_path(ticker, klt)
    keep_start = (pd.Timestamp(end_date) - pd.Timedelta(days=180)).normalize()
    trimmed = frame.loc[frame['trade_date'] >= keep_start].copy().reset_index(drop=True)
    trimmed.to_csv(cache_path, index=False, encoding='utf-8-sig')


def fetch_timeframe_history(ticker: str, end_date: pd.Timestamp, klt: int, lookback_periods: int, *, offline: bool = False) -> pd.DataFrame: 
    disable_proxy_env() 
    if int(klt) == 102: 
        cached = read_cached_timeframe_history(ticker, klt, end_date=end_date, lookback_periods=lookback_periods) 
        if not cached.empty: 
            return cached 
        daily = read_recent_daily_history(ticker, end_date=end_date) 
        weekly = build_weekly_bars(daily) 
        if not weekly.empty: 
            save_cached_timeframe_history(ticker, klt, weekly, end_date=end_date) 
        weekly = keep_last_periods(weekly, lookback_periods=lookback_periods) 
        return weekly 
    if offline: 
        return read_cached_timeframe_history(ticker, klt, end_date=end_date, lookback_periods=lookback_periods) 
    normalized = pd.DataFrame() 
    last_error: Exception | None = None 
    for attempt in range(3): 
        try: 
            datalen = max(lookback_periods * 20, 400) 
            normalized = fetch_sina_kline(ticker, klt=klt, datalen=datalen) 
            last_error = None 
            break 
        except Exception as exc:  # noqa: BLE001 
            last_error = exc 
            time.sleep(1.2 * (attempt + 1)) 
    if last_error is not None: 
        print(f'筹码集中度历史下载失败: {ticker} klt={klt} error={last_error}') 
        return read_cached_timeframe_history(ticker, klt, end_date=end_date, lookback_periods=lookback_periods) 
    if not normalized.empty and 'ticker' in normalized.columns: 
        normalized['ticker'] = normalized['ticker'].astype(str).str.zfill(6) 
        save_cached_timeframe_history(ticker, klt, normalized, end_date=end_date) 
    normalized = keep_last_periods(normalized, lookback_periods=lookback_periods) 
    return normalized 
 
 
def keep_last_periods(frame: pd.DataFrame, lookback_periods: int) -> pd.DataFrame: 
    if frame.empty: 
        return frame 
    try: 
        periods = int(lookback_periods) 
    except (TypeError, ValueError): 
        return frame 
    if periods <= 0: 
        return frame.iloc[:0].copy().reset_index(drop=True) 
    ordered = frame.sort_values('timestamp').reset_index(drop=True) 
    return ordered.tail(periods).copy().reset_index(drop=True) 


def _allocate_uniform_volume(histogram: np.ndarray, bin_edges: np.ndarray, low: float, high: float, volume: float) -> None:
    if not np.isfinite(low) or not np.isfinite(high) or not np.isfinite(volume) or volume <= 0:
        return
    if high <= low:
        index = int(np.clip(np.searchsorted(bin_edges, low, side='right') - 1, 0, len(histogram) - 1))
        histogram[index] += volume
        return
    total_range = high - low
    for index in range(len(histogram)):
        left = bin_edges[index]
        right = bin_edges[index + 1]
        overlap = min(high, right) - max(low, left)
        if overlap > 0:
            histogram[index] += volume * (overlap / total_range)


def estimate_concentration_score(window: pd.DataFrame, bin_count: int = 60) -> float:
    if window.empty:
        return float('nan')
    price_min = float(window['low'].min())
    price_max = float(window['high'].max())
    reference_price = float(window['close'].iloc[-1])
    if not np.isfinite(reference_price) or reference_price <= 0:
        return float('nan')
    if price_max <= price_min:
        return 1.0
    bin_edges = np.linspace(price_min, price_max, bin_count + 1)
    histogram = np.zeros(bin_count, dtype=float)
    for row in window.itertuples(index=False):
        _allocate_uniform_volume(histogram, bin_edges, float(row.low), float(row.high), float(row.volume))
    total_volume = histogram.sum()
    if total_volume <= 0:
        return float('nan')
    density = histogram / total_volume
    cumulative = np.cumsum(density)
    centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    low_band = np.interp(0.15, cumulative, centers)
    high_band = np.interp(0.85, cumulative, centers)
    band_width = max(high_band - low_band, 0.0)
    score = 1.0 - band_width / reference_price
    return float(np.clip(score, 0.0, 1.0))


def build_concentration_series(frame: pd.DataFrame, min_points: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(len(frame)):
        window = frame.iloc[: index + 1]
        if len(window) < min_points:
            continue
        rows.append(
            {
                'timestamp': frame.iloc[index]['timestamp'],
                'trade_date': frame.iloc[index]['trade_date'],
                'score': estimate_concentration_score(window),
            }
        )
    return pd.DataFrame(rows)


def classify_trend(series: pd.Series, trend_points: int) -> tuple[str, float]:
    clean = series.dropna()
    if len(clean) < max(3, trend_points):
        return '数据不足', float('nan')
    recent = clean.tail(trend_points)
    slope = float(np.polyfit(np.arange(len(recent), dtype=float), recent.to_numpy(dtype=float), 1)[0])
    delta = float(recent.iloc[-1] - recent.iloc[0])
    if slope > 0.001 and delta > 0:
        return '集中上行', slope
    if slope < -0.001 and delta < 0:
        return '集中下行', slope
    return '横向整理', slope


def analyze_timeframe(ticker: str, frame: pd.DataFrame, config: TimeframeConfig) -> tuple[dict[str, object], pd.DataFrame]:
    if frame.empty:
        return {
            'ticker': ticker,
            f'{config.label}_score': float('nan'),
            f'{config.label}_trend': '无数据',
            f'{config.label}_slope': float('nan'),
            f'{config.label}_window_start': pd.NaT,
            f'{config.label}_window_end': pd.NaT,
        }, pd.DataFrame(columns=['ticker', 'timeframe', 'timestamp', 'trade_date', 'score'])
    series = build_concentration_series(frame, min_points=config.min_points)
    trend_label, slope = classify_trend(series['score'], trend_points=config.trend_points) if not series.empty else ('无数据', float('nan'))
    summary = {
        'ticker': ticker,
        f'{config.label}_score': float(series['score'].iloc[-1]) if not series.empty else float('nan'),
        f'{config.label}_trend': trend_label,
        f'{config.label}_slope': slope,
        f'{config.label}_window_start': frame['trade_date'].iloc[0],
        f'{config.label}_window_end': frame['trade_date'].iloc[-1],
    }
    history = series.copy()
    history.insert(0, 'timeframe', config.label)
    history.insert(0, 'ticker', ticker)
    return summary, history


def _select_timeframe_configs(timeframes: list[str] | None) -> list[TimeframeConfig]:
    if not timeframes:
        return list(TIMEFRAME_CONFIGS)
    resolved: list[TimeframeConfig] = []
    for raw in timeframes:
        label = str(raw).strip()
        if not label:
            continue
        config = TIMEFRAME_CONFIG_MAP.get(label)
        if config is None:
            raise ValueError(f'Unsupported timeframe label: {label}')
        resolved.append(config)
    if not resolved:
        raise ValueError('No valid chip timeframes were provided.')
    return resolved


def analyze_chip_concentration_for_tickers(
    tickers: list[str],
    end_date: pd.Timestamp,
    lookback_periods: int = 60,
    week_lookback_periods: int = 4,
    timeframes: list[str] | None = None,
    *,
    offline: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    disable_proxy_env()
    configs = _select_timeframe_configs(timeframes)
    total_cycles = len(configs)
    summary_rows: list[dict[str, object]] = []
    history_frames: list[pd.DataFrame] = []
    for ticker in tickers:
        merged_summary: dict[str, object] = {'ticker': ticker}
        for config in configs:
            resolved_periods = week_lookback_periods if config.label == 'week' else lookback_periods
            history = fetch_timeframe_history(ticker, end_date=end_date, klt=config.klt, lookback_periods=resolved_periods, offline=offline)
            timeframe_summary, timeframe_history = analyze_timeframe(ticker, history, config)
            merged_summary.update(timeframe_summary)
            history_frames.append(timeframe_history)
        uptrend_count = int(sum([merged_summary.get(f'{config.label}_trend') == '集中上行' for config in configs]))
        merged_summary['uptrend_count'] = uptrend_count
        merged_summary['alignment'] = f'{uptrend_count}/{total_cycles} 周期集中上行'
        summary_rows.append(merged_summary)
    summary_frame = pd.DataFrame(summary_rows)
    history_frame = pd.concat(history_frames, ignore_index=True) if history_frames else pd.DataFrame(columns=['ticker', 'timeframe', 'timestamp', 'trade_date', 'score'])
    return summary_frame, history_frame
