# SPDX-License-Identifier: Apache-2.0
"""Shared fail-closed policy for requirements recycle-bin retention."""
import math
import os
from datetime import datetime


def retention_days(environ=None):
    value = (os.environ if environ is None else environ).get('SWTOOL_REQUIREMENTS_TRASH_RETENTION_DAYS', '7')
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError('invalid requirements retention days')
    return int(value)


def parse_timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        else:
            result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def expiry_reason(timestamp, now, days):
    if timestamp is None:
        return 'missing_timestamp'
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return 'invalid_timestamp'
    if parsed > now:
        return 'future_timestamp'
    return 'expired' if parsed <= now - days * 86400 else 'not_due'


def history_withdrawn_timestamp(history):
    if not isinstance(history, list) or not history:
        return None
    # An invalid newest event must never fall back to an older valid withdrawal.
    event = history[-1]
    return event.get('at') if isinstance(event, dict) and event.get('status') == 'withdrawn' else None
