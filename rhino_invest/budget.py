"""Hot-loaded daily limits, separate from model identity and durable usage."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import sqlite3

from .common import decimal_value, parse_time, stable_id, utc_now

KEYS = {'max_calls_per_day', 'max_cost_usd_per_day'}


class ModelBudgetConfigError(ValueError):
    pass


def validate_limits(value):
    try:
        if not isinstance(value, dict) or set(value) != KEYS:
            raise ValueError()
        calls = value['max_calls_per_day']
        if isinstance(calls, bool) or str(int(calls)) != str(calls) or int(calls) <= 0:
            raise ValueError()
        cost = decimal_value(value['max_cost_usd_per_day'])
        if cost <= 0:
            raise ValueError()
        return {'max_calls_per_day': int(calls), 'max_cost_usd_per_day': str(cost)}
    except (ValueError, TypeError, OverflowError):
        raise ModelBudgetConfigError('Daily budget requires positive calls and cost; new paid calls paused') from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _path(value):
    if not value or not Path(value).is_absolute():
        raise ModelBudgetConfigError('An absolute budget file path is required')
    return Path(value)


def load_limits(config):
    if not config.get('budget_file'):
        return validate_limits({key: config.get(key) for key in KEYS})
    try:
        with _path(config['budget_file']).open('rb') as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ValueError()
        return validate_limits(json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object))
    except (OSError, ValueError, TypeError, RecursionError):
        raise ModelBudgetConfigError('Budget file unavailable or invalid; new paid calls paused') from None


def write_limits(path, value, *, protected_paths=()):
    """Publish both limits atomically; no usage records or model settings change."""
    target, limits = _path(path), validate_limits(value)
    if target.is_symlink() or target.resolve() in {Path(item).resolve() for item in protected_paths if item}:
        raise ModelBudgetConfigError('Budget destination overlaps a protected file')
    if target.suffix.lower() != '.json':
        raise ModelBudgetConfigError('Use a dedicated JSON file for the budget')
    # Never reinterpret an arbitrary existing file as a budget. Corrupt files
    # need an explicit operator repair; this command cannot overwrite them.
    if target.exists():
        load_limits({'budget_file': str(target)})
    owner = target.stat() if target.exists() else None
    fd, temporary = tempfile.mkstemp(prefix='.model-budget-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            os.chmod(temporary, 0o600)
            if owner is not None and hasattr(os, 'fchown'):
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            json.dump(limits, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return limits


def utc_day(timestamp):
    return parse_time(timestamp).astimezone(timezone.utc).date()


def budget_status(store, config, *, now=None):
    day = utc_day(now or utc_now())
    calls = [value for _, value in store.items('model_call') if utc_day(value['timestamp']) == day]
    used = sum((decimal_value(value.get('reserved_cost_usd', '0')) for value in calls), Decimal(0))
    result = {'utc_date': str(day), 'calls_used': len(calls), 'reserved_cost_usd': str(used),
              'hot_reload': bool(config.get('budget_file')), 'actual_provider_cost_known': False}
    try:
        limits = load_limits(config)
        ceiling = decimal_value(config.get('max_cost_usd_per_call'))
        if ceiling <= 0:
            raise ValueError()
        result.update(status='valid', limits=limits, limits_hash=stable_id(limits),
            calls_remaining=max(0, limits['max_calls_per_day'] - len(calls)),
            reservation_remaining_usd=str(max(Decimal(0), decimal_value(limits['max_cost_usd_per_day']) - used)),
            next_paid_call_allowed=len(calls) < limits['max_calls_per_day']
                and used + ceiling <= decimal_value(limits['max_cost_usd_per_day']))
    except (ValueError, TypeError):
        result.update(status='invalid', next_paid_call_allowed=False,
            reason='Budget configuration unavailable or invalid; new paid calls paused')
    return result


def read_budget_status(database, config, *, now=None):
    """Budget administration must never create a new ledger or reset usage."""
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        class Usage:
            def items(self, kind):
                return [(key, json.loads(payload)) for key, payload in db.execute(
                    'SELECT key,payload FROM invest_records WHERE kind=? ORDER BY key', (kind,))]
        return budget_status(Usage(), config, now=now)
    finally:
        db.close()
