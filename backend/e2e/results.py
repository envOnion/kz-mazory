"""Verify durable evidence from a completed real Playwright process."""
import json
import time
from pathlib import Path


def verify(directory, run_id, expected=None):
    directory = Path(directory)
    deadline = time.monotonic() + 2
    while True:
        try:
            exit_record = json.loads((directory/'browser-exit.json').read_text())
            report = json.loads((directory/'results.json').read_text())
            break
        except (FileNotFoundError, json.JSONDecodeError):
            if time.monotonic() >= deadline:
                raise RuntimeError('Browser did not leave a complete durable result') from None
            time.sleep(0.02)
    if exit_record['run_id'] != run_id or exit_record['exit_code'] != 0:
        raise RuntimeError(f'Browser failed: {exit_record}')
    stats = report['stats']
    if stats['expected'] < 1 or stats['unexpected'] or stats['flaky'] or report['errors']:
        raise RuntimeError(f'Playwright failed or did not execute tests: {stats}')
    if expected is not None and stats['expected'] != expected:
        raise RuntimeError(f'Expected {expected} successful tests, got {stats}')
    print('Verified browser result:', stats)
    return stats
