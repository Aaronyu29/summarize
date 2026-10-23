"""Business gates around the installed AKShare fork; no SDK/auth duplication."""
from __future__ import annotations

import copy
import re
from .common import decimal_value, parse_time, utc_now


def symbol_value(symbol):
    value = str(symbol).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", value) or value.endswith('.US'):
        raise ValueError('Expected a bare US symbol')
    return value


def next_regular_close(now):
    """Use the adapter dependency's actual NYSE calendar, including early closes."""
    from datetime import timedelta
    import exchange_calendars
    moment = parse_time(now)
    calendar = exchange_calendars.get_calendar('XNYS')
    for day in calendar.sessions_in_range(moment.date(), (moment + timedelta(days=10)).date()):
        closing = calendar.session_close(day).to_pydatetime()
        if closing > moment:
            return closing.isoformat()
    raise ValueError('No verified regular close in calendar window')


def latest_regular_session(now):
    from datetime import timedelta
    import exchange_calendars
    moment = parse_time(now)
    calendar = exchange_calendars.get_calendar('XNYS')
    sessions = calendar.sessions_in_range((moment - timedelta(days=14)).date(), moment.date())
    for day in reversed(sessions):
        closing = calendar.session_close(day).to_pydatetime()
        if closing <= moment:
            return {'date': str(day.date()), 'close': closing.isoformat()}
    raise ValueError('No completed regular session available')


def quote_gate(snapshot, now=None, max_age=None):
    quote = snapshot.get('selected', snapshot)
    max_age = max_age if max_age is not None else (30 if quote.get('session') == 'regular' else 120)
    reasons = []
    if quote.get('source') != 'longbridge-openapi':
        reasons.append('official_source_required')
    if quote.get('status') != 'current':
        reasons.append('quote_not_current')
    if quote.get('delayed') not in (None, False, 0):
        reasons.append('delayed_quote')
    if quote.get('permission_verified', snapshot.get('permission_verified')) is not True:
        reasons.append('quote_permission_unverified')
    if quote.get('session') != 'regular':
        reasons.append('regular_session_required')
    if snapshot.get('execution_session_open') is False:
        reasons.append('verified_regular_session_closed')
    if quote.get('currency') != 'USD':
        reasons.append('currency_unverified')
    try:
        if decimal_value(quote.get('price')) <= 0:
            reasons.append('price_invalid')
    except (ValueError, TypeError):
        reasons.append('price_missing')
    try:
        age = (parse_time(now or utc_now()) - parse_time(quote.get('quote_time'))).total_seconds()
        if age < -60 or age > max_age:
            reasons.append('quote_timestamp_not_fresh')
    except (ValueError, TypeError):
        reasons.append('quote_timestamp_missing')
    return {'eligible': not reasons, 'reasons': reasons, 'max_age_seconds': max_age}


class MarketData:
    def __init__(self, client=None, snapshot_fetcher=None, *, max_age=30):
        self.client, self.snapshot_fetcher, self.max_age = client, snapshot_fetcher, max_age
        self._close_cache = {}
        self._permission_cache = None

    def snapshots(self, symbols, now=None):
        moment = parse_time(now or utc_now())
        values = list(dict.fromkeys(symbol_value(s) for s in symbols))
        fetcher = self.snapshot_fetcher
        if fetcher is None:
            from stock_alert.market_quotes import fetch_market_snapshots
            fetcher = fetch_market_snapshots
        raw = fetcher(values, source='longbridge-api', include_sessions=True,
                      now=moment, max_age=max(120, self.max_age))
        indexed = {s.get('symbol'): s for s in raw}
        result = []
        for symbol in values:
            snapshot = copy.deepcopy(indexed.get(symbol, {'symbol': symbol, 'selected': {
                'symbol': symbol, 'price': None, 'status': 'unavailable'}}))
            if self.client is not None or self.snapshot_fetcher is None:
                permission = self.permissions(moment.isoformat())
                snapshot['permission_evidence'] = permission
                snapshot['permission_verified'] = permission['verified']
                snapshot.setdefault('selected', {})['permission_verified'] = permission['verified']
                depth = self.depth(symbol, moment.isoformat())
                snapshot['depth_evidence'] = depth
                if depth.get('valid'):
                    snapshot['selected'].update(bid=depth['bid'], ask=depth['ask'],
                        bid_size=None, ask_size=None, raw_bid_volume=depth['bid_size'], raw_ask_volume=depth['ask_size'], depth_evidence=depth,
                        depth_timestamp=depth['fetched_at'], depth_source='longbridge-openapi',
                        depth_verified=True, depth_time_kind='observed_at', depth_timestamp_basis='client_receipt',
                        depth_size_unit=None, liquidity_verified=False)
            age_limit = self.max_age if snapshot.get('selected', {}).get('session') == 'regular' else 120
            snapshot['business_gate'] = quote_gate(snapshot, moment.isoformat(), age_limit)
            try:
                snapshot['next_regular_close'] = next_regular_close(moment)
                latest = latest_regular_session(moment)
                snapshot['latest_completed_regular_session'] = latest
                snapshot['regular_close'] = (self.regular_close(symbol, latest)
                    if self.client is not None or self.snapshot_fetcher is None else None)
            except (ImportError, ValueError, KeyError):
                snapshot['next_regular_close'] = None
            result.append(snapshot)
        return result

    def depth(self, symbol, now=None):
        """Depth lacks exchange timestamp: retain fetch-time as fetch-time, never quote-time."""
        result = {'valid': False, 'source': 'longbridge-openapi', 'source_timestamp': None,
                  'fetched_at': now or utc_now(), 'scope': 'provider book; no NBBO claim'}
        try:
            try:
                from stock_alert.longbridge_sdk import to_jsonable
            except ImportError:
                to_jsonable = lambda value: value
            raw = to_jsonable(self.client.query('quote', 'depth', symbol + '.US'))
            result['fetched_at'] = utc_now()
            bids = [v for v in raw['bids'] if decimal_value(v['price']) > 0 and decimal_value(v['volume']) > 0]
            asks = [v for v in raw['asks'] if decimal_value(v['price']) > 0 and decimal_value(v['volume']) > 0]
            bid = max(bids, key=lambda v: decimal_value(v['price']))
            ask = min(asks, key=lambda v: decimal_value(v['price']))
            if decimal_value(bid['price']) > decimal_value(ask['price']):
                raise ValueError('Crossed book')
            from .common import stable_id
            result.update(valid=True, bid=str(bid['price']), ask=str(ask['price']),
                bid_size=str(bid['volume']), ask_size=str(ask['volume']), raw_hash=stable_id(raw))
        except Exception as exc:
            result['gap'] = type(exc).__name__
        return result

    def permissions(self, now=None):
        moment = parse_time(now or utc_now())
        cached = self._permission_cache
        if cached and cached.get('valid_until') and parse_time(cached['valid_until']) > moment:
            return copy.deepcopy(cached)
        result = {'verified': False, 'source': 'longbridge-openapi', 'scope': None, 'valid_until': None}
        try:
            if self.client is None:
                from stock_alert.longbridge_sdk import LongbridgeClient
                self.client = LongbridgeClient(timeout=10)
            try:
                from stock_alert.longbridge_sdk import to_jsonable
            except ImportError:
                to_jsonable = lambda value: value
            packages = to_jsonable(self.client.query('quote', 'quote_package_details'))
            for package in packages:
                if (package.get('key') == 'US_QBBO_OpenAPI'
                        and parse_time(package['start_at']) <= moment < parse_time(package['end_at'])):
                    from .common import stable_id
                    result.update(verified=True, package_key='US_QBBO_OpenAPI',
                        scope='Nasdaq QBBO venue quotes; not consolidated NBBO',
                        valid_until=package['end_at'], evidence_hash=stable_id(package))
                    break
        except Exception:
            pass
        self._permission_cache = result
        return copy.deepcopy(result)

    def regular_close(self, symbol, session):
        """Only a completed exact exchange session may confirm a daily price stop."""
        key = (symbol, session['date'])
        if key in self._close_cache:
            return copy.deepcopy(self._close_cache[key])
        try:
            if self.client is None:
                from stock_alert.longbridge_sdk import LongbridgeClient
                self.client = LongbridgeClient(timeout=10)
            try:
                from stock_alert.longbridge_sdk import to_jsonable
            except ImportError:
                to_jsonable = lambda value: value
            bars = to_jsonable(self.client.query('quote', 'candlesticks', symbol + '.US',
                period='Day', count=10, adjust_type='NoAdjust'))
            from zoneinfo import ZoneInfo
            for bar in bars:
                stamp = parse_time(bar['timestamp'])
                # SDK daily candle timestamps identify the New York trading day.
                if stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat() == session['date']:
                    close = {'symbol': symbol, 'session_date': session['date'],
                        'quote_time': session['close'], 'bar_timestamp': stamp.isoformat(),
                        'price': str(decimal_value(bar['close'])), 'currency': 'USD',
                        'adjustment': 'NoAdjust', 'source': 'longbridge-openapi', 'confirmed': True}
                    self._close_cache[key] = close
                    return copy.deepcopy(close)
        except Exception:
            pass
        return None

    def fundamentals(self, symbol, now=None):
        symbol = symbol_value(symbol)
        if self.client is None:
            from stock_alert.longbridge_sdk import LongbridgeClient
            self.client = LongbridgeClient(timeout=10)
        try:
            from stock_alert.longbridge_sdk import to_jsonable
        except ImportError:
            # Injection supports native JSON fixtures without installing the external SDK.
            to_jsonable = lambda value: value
        result = {'symbol': symbol, 'source': 'longbridge-openapi',
                  'fetched_at': now or utc_now(), 'gaps': [], 'adr_conversion_verified': False,
                  'consensus_live_verified': False}
        for method, kwargs in [('company_profile', {}), ('financial_report', {
                'kind': 'All', 'period': 'Annual'}), ('valuations', {}), ('consensus', {})]:
            try:
                result[method] = to_jsonable(self.client.query('fundamental', method, symbol + '.US', **kwargs))
            except Exception as exc:
                result[method] = None
                result['gaps'].append({'field': method, 'error': type(exc).__name__})
        consensus = result.get('consensus')
        try:
            result['identity'] = to_jsonable(self.client.query('quote', 'static_info', [symbol + '.US']))
        except Exception as exc:
            result['identity'] = None
            result['gaps'].append({'field': 'identity', 'error': type(exc).__name__})
        if isinstance(consensus, dict):
            result['consensus_normalized'] = {'currency': consensus.get('currency'),
                'period_type': consensus.get('current_period'), 'annual_forecast_verified': False,
                'forecasts': [{'fiscal_year': row.get('fiscal_year'), 'period_text': row.get('period_text'),
                    'fiscal_period': row.get('fiscal_period'), 'metrics': [
                        {'key': metric.get('key'), 'actual': metric.get('actual'),
                         'estimate': metric.get('estimate'), 'is_released': metric.get('is_released')}
                        for metric in row.get('details', []) if metric.get('key') in {'eps', 'normalized_eps', 'revenue'}]}
                    for row in consensus.get('list', [])]}
            result['consensus_live_verified'] = True
        return result
