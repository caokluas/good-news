from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / 'src'))

from good_news_quant.selection_workflow import main as run_selection_main
from good_news_quant.validation_workflow import main as run_validation_main

LOGIC_FIRST_DIR = ROOT / 'reports' / 'logic_first'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run latest stock selection with Tencent + Sina KLine.')
    parser.add_argument('--end-date', required=True, help='Validation end date in YYYY-MM-DD.')
    parser.add_argument('--signal-days', type=int, default=60)
    parser.add_argument('--warmup-days', type=int, default=160)
    parser.add_argument('--universe-size', type=int, default=200)
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--top-strategies', type=int, default=5)
    parser.add_argument('--lookback-days', type=int, default=30)
    parser.add_argument('--top-stocks', type=int, default=10)
    parser.add_argument('--chip-lookback-periods', type=int, default=60, help='Chip lookback periods for daily/m60/m30 (default: 60).')
    parser.add_argument('--chip-week-lookback-periods', type=int, default=4, help='Chip lookback weeks for week timeframe (default: 4).')
    parser.add_argument('--chip-timeframes', default='daily,m60,m30,week', help='Chip timeframes, e.g. daily,m60,m30,week.')
    parser.add_argument('--min-chip-uptrend-cycles', type=int, default=3)
    parser.add_argument('--offline', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    validation_args = [
        '--end-date',
        args.end_date,
        '--output-dir',
        str(LOGIC_FIRST_DIR),
        '--signal-days',
        str(args.signal_days),
        '--warmup-days',
        str(args.warmup_days),
        '--universe-size',
        str(args.universe_size),
        '--top-n',
        str(args.top_n),
    ]
    selection_args = [
        '--reports-dir',
        str(LOGIC_FIRST_DIR),
        '--top-strategies',
        str(args.top_strategies),
        '--lookback-days',
        str(args.lookback_days),
        '--top-stocks',
        str(args.top_stocks),
        '--chip-lookback-periods',
        str(args.chip_lookback_periods),
        '--chip-week-lookback-periods',
        str(args.chip_week_lookback_periods),
        '--chip-timeframes',
        str(args.chip_timeframes),
        '--min-chip-uptrend-cycles',
        str(args.min_chip_uptrend_cycles),
    ]
    if args.offline:
        validation_args.append('--offline')
        selection_args.append('--offline')
    run_validation_main(validation_args)
    run_selection_main(selection_args)
    _cleanup_selection_reports(LOGIC_FIRST_DIR)


def _cleanup_selection_reports(reports_dir: Path) -> None:
    keep = {
        'selection-summary.md',
        'selection-top10-stocks.csv',
        'selection-final-chip-filtered.csv',
    }
    if not reports_dir.exists():
        return
    for path in reports_dir.iterdir():
        if not path.is_file():
            continue
        if path.name not in keep:
            path.unlink()


if __name__ == '__main__':
    main()
