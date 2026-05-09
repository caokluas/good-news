from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


PATTERN_CATALOG: list[dict[str, str]] = [
    {'code': 'shi_zi_zhang_kou', 'name_cn': '狮子张口', 'name_en': 'bottom-breakout-after-compression', 'file_name': '狮子张口.jpg', 'legacy_name_cn': '狮子张口'},
    {'code': 'wa_keng_mai_niu', 'name_cn': '挖坑埋牛', 'name_en': 'oversold-pit-reversal', 'file_name': '挖坑埋牛.jpg', 'legacy_name_cn': '挖坑埋牛'},
    {'code': 'yang_yang_die_gong', 'name_cn': '阴阳兼攻', 'name_en': 'multi-candle-breakout', 'file_name': '阴阳兼攻.jpg', 'legacy_name_cn': '阴阳兼攻'},
    {'code': 'si_xian_yi_lei', 'name_cn': '回眸一笑', 'name_en': 'pullback-smile-breakout', 'file_name': '回眸一笑.jpg', 'legacy_name_cn': '回眸一笑'},
    {'code': 'yu_yue_long_men', 'name_cn': '鱼跃龙门', 'name_en': 'ma30-breakout-after-base', 'file_name': '鱼跃龙门.jpg', 'legacy_name_cn': '鱼跃龙门'},
    {'code': 'kui_hua_xiang_yang', 'name_cn': '葵花向阳', 'name_en': 'uptrend-continuation-expansion', 'file_name': '葵花向阳.jpg', 'legacy_name_cn': '葵花向阳'},
    {'code': 'mei_ren_ti_tui', 'name_cn': '美人长腿', 'name_en': 'long-lower-shadow-bottom', 'file_name': '美人长腿.jpg', 'legacy_name_cn': '美人长腿'},
    {'code': 'xu_ri_dong_sheng', 'name_cn': '旭日东升', 'name_en': 'bullish-piercing-reversal', 'file_name': '旭日东升.jpg', 'legacy_name_cn': '旭日东升'},
    {'code': 'dao_chui_tou_xian', 'name_cn': '倒锤头线', 'name_en': 'inverted-hammer-confirmation', 'file_name': '倒锤头线.jpg', 'legacy_name_cn': '倒锤头线'},
    {'code': 'xi_wang_zhi_xing', 'name_cn': '希望之星', 'name_en': 'morning-star-reversal', 'file_name': '希望之星.jpg', 'legacy_name_cn': '希望之星'},
    {'code': 'dao_xing_fan_zhuan', 'name_cn': '岛形反转', 'name_en': 'island-reversal', 'file_name': '岛形反转.jpg', 'legacy_name_cn': '岛形反转'},
    {'code': 'shang_zhang_fen_shou', 'name_cn': '上涨分手', 'name_en': 'bullish-separating-line', 'file_name': '上涨分手.jpg', 'legacy_name_cn': '上涨分手'},
]
PATTERN_NAME_MAP = {item['code']: item for item in PATTERN_CATALOG}


@dataclass(slots=True)
class PatternScanResult:
    signals: pd.DataFrame
    summary: pd.DataFrame


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return numerator.div(denominator)


def _compute_kdj(group: pd.DataFrame, window: int = 9) -> pd.DataFrame:
    low_n = group['low'].rolling(window, min_periods=window).min()
    high_n = group['high'].rolling(window, min_periods=window).max()
    rsv = (group['close'] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)

    k_values: list[float] = []
    d_values: list[float] = []
    prev_k = 50.0
    prev_d = 50.0
    for value in rsv.to_list():
        prev_k = prev_k * 2 / 3 + float(value) / 3
        prev_d = prev_d * 2 / 3 + prev_k / 3
        k_values.append(prev_k)
        d_values.append(prev_d)
    j_values = [3 * k - 2 * d for k, d in zip(k_values, d_values)]
    return pd.DataFrame({'kdj_k': k_values, 'kdj_d': d_values, 'kdj_j': j_values}, index=group.index)


def _compute_cci(group: pd.DataFrame, window: int = 14) -> pd.Series:
    typical_price = (group['high'] + group['low'] + group['close']) / 3
    moving_average = typical_price.rolling(window, min_periods=window).mean()
    mean_deviation = typical_price.rolling(window, min_periods=window).apply(
        lambda values: np.mean(np.abs(values - values.mean())),
        raw=True,
    )
    return (typical_price - moving_average) / (0.015 * mean_deviation.replace(0, np.nan))


def prepare_pattern_frame(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    required = {'date', 'ticker', 'open', 'high', 'low', 'close', 'volume'}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"pattern prices missing columns: {sorted(missing)}")

    frame['date'] = pd.to_datetime(frame['date'])
    frame['ticker'] = frame['ticker'].astype(str)
    frame = frame.sort_values(['ticker', 'date']).reset_index(drop=True)

    defaults: dict[str, object] = {
        'amount': np.nan,
        'is_tradable': True,
        'is_limit_up': False,
        'is_limit_down': False,
        'listed_days': 9999,
        'is_st': False,
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
        else:
            frame[column] = frame[column].fillna(default)

    frame['amount'] = pd.to_numeric(frame['amount'], errors='coerce')
    frame['listed_days'] = pd.to_numeric(frame['listed_days'], errors='coerce').fillna(9999)
    frame['prev_close'] = frame.groupby('ticker')['close'].shift(1)
    frame['ret_1d'] = frame.groupby('ticker')['close'].pct_change()
    frame['ret_3d'] = frame.groupby('ticker')['close'].pct_change(3)
    frame['ret_5d'] = frame.groupby('ticker')['close'].pct_change(5)
    frame['ret_10d'] = frame.groupby('ticker')['close'].pct_change(10)
    frame['ret_20d'] = frame.groupby('ticker')['close'].pct_change(20)

    frame['range_pct'] = _safe_divide(frame['high'] - frame['low'], frame['close'])
    frame['body_pct'] = _safe_divide((frame['close'] - frame['open']).abs(), frame['open'])
    frame['close_open_ret'] = _safe_divide(frame['close'] - frame['open'], frame['open'])
    frame['upper_shadow'] = frame['high'] - frame[['open', 'close']].max(axis=1)
    frame['lower_shadow'] = frame[['open', 'close']].min(axis=1) - frame['low']
    full_range = (frame['high'] - frame['low']).replace(0, np.nan)
    frame['body_top_position'] = (frame[['open', 'close']].max(axis=1) - frame['low']) / full_range
    frame['close_in_top_20pct'] = ((frame['close'] - frame['low']) / full_range) >= 0.8

    grouped_close = frame.groupby('ticker')['close']
    grouped_high = frame.groupby('ticker')['high']
    grouped_low = frame.groupby('ticker')['low']
    grouped_volume = frame.groupby('ticker')['volume']
    grouped_range = frame.groupby('ticker')['range_pct']
    for window in [5, 10, 20, 30]:
        frame[f'ma{window}'] = grouped_close.transform(lambda series: series.rolling(window, min_periods=window).mean())
    frame['vol_ma5'] = grouped_volume.transform(lambda series: series.rolling(5, min_periods=5).mean())
    frame['vol_ma10'] = grouped_volume.transform(lambda series: series.rolling(10, min_periods=10).mean())
    frame['high_5'] = grouped_high.transform(lambda series: series.rolling(5, min_periods=5).max())
    frame['high_10'] = grouped_high.transform(lambda series: series.rolling(10, min_periods=10).max())
    frame['high_20'] = grouped_high.transform(lambda series: series.rolling(20, min_periods=20).max())
    frame['low_10'] = grouped_low.transform(lambda series: series.rolling(10, min_periods=10).min())
    frame['low_20'] = grouped_low.transform(lambda series: series.rolling(20, min_periods=20).min())
    frame['median_range_20'] = grouped_range.transform(lambda series: series.rolling(20, min_periods=20).median())

    frame['large_bull'] = (frame['close'] > frame['open']) & (frame['close_open_ret'] >= 0.05)
    frame['large_bear'] = (frame['close'] < frame['open']) & (frame['close_open_ret'] <= -0.05)
    frame['medium_bull'] = (frame['close'] > frame['open']) & (frame['close_open_ret'] >= 0.02)
    frame['small_real_body'] = frame['body_pct'] <= 0.015
    frame['volume_expand'] = frame['volume'] >= 1.5 * frame['vol_ma5']
    frame['volume_shrink'] = frame['volume'] <= 0.8 * frame['vol_ma5']
    frame['gap_up'] = frame['low'] > frame.groupby('ticker')['high'].shift(1) * 1.003
    frame['gap_down'] = frame['high'] < frame.groupby('ticker')['low'].shift(1) * 0.997
    frame['long_lower_shadow'] = frame['lower_shadow'] >= 2 * (frame['close'] - frame['open']).abs()
    frame['long_upper_shadow'] = frame['upper_shadow'] >= 2 * (frame['close'] - frame['open']).abs()
    frame['trend_up'] = (frame['close'] > frame['ma30']) & (frame['ma30'] > frame.groupby('ticker')['ma30'].shift(3))
    frame['trend_down'] = (frame['close'] < frame['ma30']) & (frame['ma30'] < frame.groupby('ticker')['ma30'].shift(3))

    frame['kdj_k'] = np.nan
    frame['kdj_d'] = np.nan
    frame['kdj_j'] = np.nan
    frame['cci_14'] = np.nan
    for _, group in frame.groupby('ticker'):
        kdj = _compute_kdj(group)
        cci = _compute_cci(group)
        frame.loc[group.index, ['kdj_k', 'kdj_d', 'kdj_j']] = kdj[['kdj_k', 'kdj_d', 'kdj_j']].to_numpy()
        frame.loc[group.index, 'cci_14'] = cci.to_numpy()

    frame['one_price_board'] = frame['high'] == frame['low']
    frame['valid_scan'] = (
        ~frame['is_st'].astype(bool)
        & frame['is_tradable'].astype(bool)
        & (frame['listed_days'] >= 60)
        & ~frame['one_price_board'].astype(bool)
    )
    return frame


def detect_patterns(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    grouped = data.groupby('ticker')

    prev_open = grouped['open'].shift(1)
    prev_close = grouped['close'].shift(1)
    prev_high = grouped['high'].shift(1)
    prev_low = grouped['low'].shift(1)
    prev_volume = grouped['volume'].shift(1)
    prev_ma20 = grouped['ma20'].shift(1)
    prev_ma30 = grouped['ma30'].shift(1)
    prev_large_bear = grouped['large_bear'].shift(1).fillna(False)
    prev_gap_down = grouped['gap_down'].shift(1).fillna(False)
    prev_small_body = grouped['small_real_body'].shift(1).fillna(False)
    prev_long_upper_shadow = grouped['long_upper_shadow'].shift(1).fillna(False)
    prev_body_top_position = grouped['body_top_position'].shift(1)
    prev_low_20 = grouped['low_20'].shift(1)
    prev_low_10 = grouped['low_10'].shift(1)
    prev_close_open_ret = grouped['close_open_ret'].shift(1)
    prev2_open = grouped['open'].shift(2)
    prev2_close = grouped['close'].shift(2)
    prev2_low = grouped['low'].shift(2)
    prev2_large_bear = grouped['large_bear'].shift(2).fillna(False)
    prev2_gap_down = grouped['gap_down'].shift(2).fillna(False)

    highest_prev5 = grouped['high'].transform(lambda series: series.shift(1).rolling(5, min_periods=5).max())
    highest_prev10 = grouped['high'].transform(lambda series: series.shift(1).rolling(10, min_periods=10).max())
    highest_prev20 = grouped['high'].transform(lambda series: series.shift(1).rolling(20, min_periods=20).max())
    lowest_prev10 = grouped['low'].transform(lambda series: series.shift(1).rolling(10, min_periods=10).min())
    lowest_prev20 = grouped['low'].transform(lambda series: series.shift(1).rolling(20, min_periods=20).min())
    prior3_small_count = grouped['small_real_body'].transform(lambda series: series.shift(1).rolling(3, min_periods=3).sum())
    prior3_range_mean = grouped['range_pct'].transform(lambda series: series.shift(1).rolling(3, min_periods=3).mean())
    below_ma20_count_8 = ((data['close'] <= data['ma20']).astype(float)).groupby(data['ticker']).transform(
        lambda series: series.shift(1).rolling(8, min_periods=8).sum()
    )
    above_ma20_count_10 = ((data['close'] >= data['ma20']).astype(float)).groupby(data['ticker']).transform(
        lambda series: series.shift(1).rolling(10, min_periods=10).sum()
    )
    reclaim_ma20 = ((prev_close <= prev_ma20) & (data['close'] > data['ma20'])).astype(float)
    recent_reclaim_ma20 = reclaim_ma20.groupby(data['ticker']).transform(lambda series: series.shift(1).rolling(10, min_periods=1).max()).fillna(0) > 0

    bullish_count_4 = (data['close'] > data['open']).astype(float).groupby(data['ticker']).transform(
        lambda series: series.shift(1).rolling(4, min_periods=4).sum()
    )
    selloff_4d = grouped['ret_1d'].transform(
        lambda series: (1 + series.fillna(0)).shift(1).rolling(4, min_periods=4).apply(np.prod, raw=True) - 1
    )
    prior_volume_increase = (grouped['volume'].shift(2) <= grouped['volume'].shift(1)) & (grouped['volume'].shift(1) <= data['volume'])
    prior_bull_steps = grouped['close_open_ret'].shift(1).between(0.005, 0.035) & grouped['close_open_ret'].shift(2).between(0.005, 0.035)

    kdj_recent_negative = grouped['kdj_j'].transform(lambda series: series.shift(1).rolling(2, min_periods=1).min()) < 0
    cci_recent_oversold = (data['cci_14'] < -200) | (grouped['cci_14'].shift(1) < -200)
    midpoint_prev_body = (prev_open + prev_close) / 2
    midpoint_prev2_body = (prev2_open + prev2_close) / 2
    local_low_prev_star = grouped['low'].shift(1) <= grouped['low'].transform(lambda series: series.shift(1).rolling(5, min_periods=5).min())

    base_below_ma30_count = ((data['close'] < data['ma30']).astype(float)).groupby(data['ticker']).transform(
        lambda series: series.shift(1).rolling(20, min_periods=10).sum()
    )
    golden_cross_recent = ((data['ma5'] > data['ma10']) & (grouped['ma5'].shift(1) <= grouped['ma10'].shift(1))) | (data['ma5'] > data['ma10'])

    island_single = prev2_gap_down & data['gap_up'] & (prev_high < prev2_low) & (data['low'] > prev_high)
    prev_inverted_hammer = prev_long_upper_shadow & (prev_body_top_position <= 0.35) & (grouped['ret_5d'].shift(1) < -0.05)

    signal_map: dict[str, pd.Series] = {
        'shi_zi_zhang_kou': (
            data['valid_scan']
            & (below_ma20_count_8 >= 4)
            & ((prior3_small_count >= 2) | (prior3_range_mean <= grouped['median_range_20'].shift(1)))
            & (data['large_bull'] | (data['close_open_ret'] >= 0.04))
            & (data['close'] > highest_prev5)
            & data['volume_expand']
        ),
        'wa_keng_mai_niu': (
            data['valid_scan']
            & data['ret_10d'].between(-0.12, 0.05)
            & (selloff_4d <= -0.08)
            & kdj_recent_negative
            & cci_recent_oversold
            & (data['medium_bull'] | data['large_bull'])
            & ((data['close'] > prev_high) | (data['close'] > midpoint_prev_body))
            & (data['volume'] >= 1.2 * data['vol_ma5'])
        ),
        'yang_yang_die_gong': (
            data['valid_scan']
            & (bullish_count_4 >= 2)
            & prior_bull_steps
            & prior_volume_increase.fillna(False)
            & (data['close'] > highest_prev10)
            & (data['large_bull'] | (grouped['ret_1d'].transform(lambda series: (1 + series.fillna(0)).rolling(3, min_periods=3).apply(np.prod, raw=True) - 1) >= 0.08))
        ),
        'si_xian_yi_lei': (
            data['valid_scan']
            & recent_reclaim_ma20
            & (above_ma20_count_10 >= 8)
            & (data['close'] > data['ma5'])
            & (data['ma5'] >= data['ma10'])
            & (data['ma10'] >= data['ma20'])
            & (data['ma30'] >= prev_ma30.fillna(data['ma30']))
            & (data['volume'] >= 1.3 * data['vol_ma5'])
        ),
        'yu_yue_long_men': (
            data['valid_scan']
            & (base_below_ma30_count >= 5)
            & golden_cross_recent.fillna(False)
            & (data['close'] > data['ma30'])
            & (data['close'] > highest_prev20)
            & data['large_bull']
            & data['volume_expand']
            & data['close_in_top_20pct'].fillna(False)
        ),
        'kui_hua_xiang_yang': (
            data['valid_scan']
            & data['trend_up']
            & (grouped['volume_shrink'].shift(1).fillna(False) | grouped['small_real_body'].shift(1).fillna(False))
            & data['large_bull']
            & data['volume_expand']
            & (data['low'] >= data['ma10'])
            & (data['close'] > highest_prev5)
        ),
        'mei_ren_ti_tui': (
            data['valid_scan']
            & (data['ret_5d'] <= -0.05)
            & data['long_lower_shadow']
            & (data['low'] <= lowest_prev10)
            & ((data['close'] > data['open']) | (data['close'] > prev_close))
        ),
        'xu_ri_dong_sheng': (
            data['valid_scan']
            & (data['trend_down'] | (grouped['ret_5d'].shift(1) < 0))
            & prev_large_bear
            & (data['medium_bull'] | data['large_bull'])
            & (data['close'] > midpoint_prev_body)
            & ((data['volume'] >= prev_volume) | (data['volume'] >= 1.2 * data['vol_ma5']))
        ),
        'dao_chui_tou_xian': (
            data['valid_scan']
            & prev_inverted_hammer.fillna(False)
            & (data['close'] > prev_high)
        ),
        'xi_wang_zhi_xing': (
            data['valid_scan']
            & prev2_large_bear
            & prev_small_body
            & local_low_prev_star.fillna(False)
            & (data['medium_bull'] | data['large_bull'])
            & (data['close'] > midpoint_prev2_body)
            & (data['volume'] >= 1.2 * data['vol_ma5'])
        ),
        'dao_xing_fan_zhuan': (
            data['valid_scan']
            & island_single.fillna(False)
            & ((data['close'] > data['open']) | (data['close'] > ((prev_high + prev_low) / 2)))
            & ((data['volume'] >= 1.2 * data['vol_ma5']) | data['gap_up'])
        ),
        'shang_zhang_fen_shou': (
            data['valid_scan']
            & data['trend_up']
            & (prev_close < prev_open)
            & ((data['open'] / prev_open - 1).abs() <= 0.005)
            & (data['close'] > prev_high)
            & (data['volume'] >= 0.8 * prev_volume)
        ),
    }

    for code, signal in signal_map.items():
        data[f'signal_{code}'] = signal.fillna(False)
    return data


def scan_pattern_signals(
    prices: pd.DataFrame,
    strategies: list[str] | None = None,
    latest_only: bool = False,
) -> PatternScanResult:
    prepared = prepare_pattern_frame(prices)
    detected = detect_patterns(prepared)

    selected_codes = strategies or [item['code'] for item in PATTERN_CATALOG]
    signal_frames: list[pd.DataFrame] = []
    for code in selected_codes:
        column = f'signal_{code}'
        if column not in detected.columns:
            continue
        meta = PATTERN_NAME_MAP[code]
        subset = detected.loc[detected[column], ['date', 'ticker', 'open', 'high', 'low', 'close', 'volume']].copy()
        if subset.empty:
            continue
        subset['pattern_code'] = code
        subset['pattern_name_cn'] = meta['name_cn']
        subset['pattern_name_en'] = meta['name_en']
        subset['signal_price'] = subset['close']
        signal_frames.append(subset)

    if signal_frames:
        signals = pd.concat(signal_frames, ignore_index=True)
        signals = signals.sort_values(['date', 'pattern_code', 'ticker']).reset_index(drop=True)
    else:
        signals = pd.DataFrame(
            columns=['date', 'ticker', 'open', 'high', 'low', 'close', 'volume', 'pattern_code', 'pattern_name_cn', 'pattern_name_en', 'signal_price']
        )

    if latest_only and not signals.empty:
        latest_date = signals['date'].max()
        signals = signals.loc[signals['date'] == latest_date].reset_index(drop=True)

    if signals.empty:
        summary = pd.DataFrame(columns=['pattern_code', 'pattern_name_cn', 'signal_count', 'latest_signal_date'])
    else:
        summary = (
            signals.groupby(['pattern_code', 'pattern_name_cn'], as_index=False)
            .agg(signal_count=('ticker', 'count'), latest_signal_date=('date', 'max'))
            .sort_values(['signal_count', 'pattern_code'], ascending=[False, True])
            .reset_index(drop=True)
        )
    return PatternScanResult(signals=signals, summary=summary)

