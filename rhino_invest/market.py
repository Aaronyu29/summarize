"""Business gates around the installed AKShare fork; no SDK/auth duplication."""
from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from .common import decimal_value, parse_time, utc_now, stable_id


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
                  'consensus_live_verified': False,
                  'financial_report_request': {'kind': 'All', 'period': 'Annual'}}
        for method, kwargs in [('company_profile', {}), ('financial_report', {
                'kind': 'All', 'period': 'Annual'}), ('valuations', {}), ('consensus', {})]:
            try:
                result[method] = to_jsonable(self.client.query('fundamental', method, symbol + '.US', **kwargs))
            except Exception as exc:
                result[method] = None
                result['gaps'].append({'field': method, 'error': type(exc).__name__})
        try:
            result['identity'] = to_jsonable(self.client.query('quote', 'static_info', [symbol + '.US']))
        except Exception as exc:
            result['identity'] = None
            result['gaps'].append({'field': 'identity', 'error': type(exc).__name__})
        # A daily research reference is not an execution quote. Keep the exact
        # adapter's session/status/time, including historical or unavailable data.
        try:
            if self.snapshot_fetcher is not None:
                reference = self.snapshots([symbol], result['fetched_at'])[0]
            else:
                from stock_alert.longbridge_quotes import normalize_official_quote
                from stock_alert.market_quotes import fetch_market_snapshot
                rows = self.client.query('quote', 'quote', [symbol + '.US'])
                record = next(row for row in rows if row.symbol == symbol + '.US')
                reference = fetch_market_snapshot(symbol, now=parse_time(result['fetched_at']),
                    source='longbridge-api', include_sessions=True, use_cache=False, max_age=120,
                    _official_batch={symbol: normalize_official_quote(record)})
                permission = self.permissions(result['fetched_at'])
                reference['selected']['permission_verified'] = permission['verified']
                reference['business_gate'] = quote_gate(reference, result['fetched_at'])
            result['reference_quote'] = {key: copy.deepcopy(reference.get(key)) for key in
                ('symbol', 'selected', 'quotes', 'active_session', 'business_gate', 'fetched_at', 'execution_session_open')}
            candidates = [q for q in (reference.get('quotes') or {}).values()
                if q.get('status') in {'current', 'historical', 'stale'} and _numeric(q.get('price')) is not None
                and decimal_value(q['price']) > 0 and q.get('quote_time')]
            if not candidates and isinstance(reference.get('selected'), dict):
                candidates = [reference['selected']] if reference['selected'].get('status') in {'current', 'historical', 'stale'} else []
            result['reference_quote']['comparison_quote'] = copy.deepcopy(max(candidates,
                key=lambda q: parse_time(q['quote_time']))) if candidates else None
            result['reference_quote']['comparison_only'] = True
        except Exception as exc:
            result['reference_quote'] = {'symbol': symbol, 'selected': {'status': 'unavailable', 'price': None},
                'business_gate': {'eligible': False, 'reasons': ['reference_quote_unavailable']},
                'error': type(exc).__name__}
        return normalize_fundamentals(result)


def _numeric(value):
    try:
        return str(decimal_value(value))
    except (ValueError, TypeError):
        return None


def _records(value):
    return value if isinstance(value, (list, tuple)) else []


def _fiscal_period(label, year, code=None, native_period=None):
    """Use explicit source labels, never infer a fiscal year from wall-clock time."""
    label = str(label or '').strip()
    if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2200:
        return None, None
    if re.fullmatch(r'FY\s*' + str(year), label, re.I) and native_period not in {'qf', 'QuarterlyFull'}:
        return 'annual', 'FY'
    quarter = re.fullmatch(r'Q([1-4])\s*(?:FY\s*)?' + str(year), label, re.I)
    if quarter:
        number = quarter.group(1)
        if code is not None and str(code).upper() not in {number, 'Q' + number}:
            return None, None
        return 'quarterly', 'Q' + number
    if label in {'', str(year)} and native_period in {'af', 'Annual', 'annual'} and str(code).upper() in {'FY', 'AF', 'ANNUAL'}:
        return 'annual', 'FY'
    return None, None


def _period_end(value):
    try:
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        if isinstance(value, bool) or value is None:
            return None
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _primary_verified(store, record):
    """An assertion cannot certify a unit: recheck persisted official raw text."""
    refs, excerpt = record.get('evidence_refs'), record.get('source_excerpt')
    if store is None or not isinstance(refs, list) or not refs or not isinstance(excerpt, str) or not excerpt.strip():
        return False
    excerpts = [excerpt]
    if isinstance(record.get('unit_source_excerpt'), str) and record['unit_source_excerpt'].strip():
        excerpts.append(record['unit_source_excerpt'])
    matched = set()
    for ref in refs:
        if not isinstance(ref, str):
            return False
        source = store.get('external_evidence', ref)
        if not isinstance(source, dict) or not isinstance(source.get('content'), str):
            return False
        if not isinstance(source.get('source_url'), str):
            return False
        parsed = urlparse(source['source_url'])
        host = parsed.hostname or ''
        domains = ('tsmc.com', 'abc.xyz', 'sec.gov', 'ishares.com', 'blackrock.com')
        official = any(host == d or host.endswith('.' + d) for d in domains)
        official = official or (host == 's206.q4cdn.com' and parsed.path.startswith('/479360582/files/doc_financials/'))
        if parsed.scheme != 'https' or not official:
            return False
        if hashlib.sha256(source['content'].encode('utf-8')).hexdigest() != source.get('sha256'):
            return False
        found = {text for text in excerpts if text in source['content']}
        if not found:
            return False
        matched.update(found)
    return len(matched) == len(set(excerpts))


def normalize_fundamentals(fundamentals, *, store=None):
    """Project native evidence without discarding raw data or certifying unknowns.

    This can be run again after primary-source enrichment. Source JSON pointers
    are relative to the returned fundamentals object, with canonical raw hashes.
    Annual coverage verification is separate from EPS currency/unit validation.
    """
    result = copy.deepcopy(fundamentals)
    report = result.get('financial_report')
    actuals = []
    groups = report.get('list', {}) if isinstance(report, dict) else {}
    for kind, group in groups.items() if isinstance(groups, dict) else ():
        if not isinstance(group, dict):
            continue
        for i, indicator in enumerate(_records(group.get('indicators'))):
            if not isinstance(indicator, dict):
                continue
            for j, account in enumerate(_records(indicator.get('accounts'))):
                if not isinstance(account, dict):
                    continue
                for k, value in enumerate(_records(account.get('values'))):
                    if not isinstance(value, dict):
                        continue
                    period_type, fiscal_period = _fiscal_period(value.get('period'), value.get('year'))
                    actuals.append({'metric': account.get('field'), 'value': _numeric(value.get('value')),
                        'currency': indicator.get('currency') or None, 'price_unit': None,
                        'unit_label': account.get('name'), 'description': account.get('tip'),
                        'percent': account.get('percent'), 'period_type': period_type,
                        'fiscal_year': value.get('year'), 'fiscal_period': fiscal_period,
                        'period_label': value.get('period'), 'period_start': None,
                        'period_end': _period_end(value.get('fp_end')), 'fp_end_raw': value.get('fp_end'),
                        'published_at': None, 'as_of': None, 'value_kind': 'reported',
                        'source': result.get('source'), 'statement': kind,
                        'raw_path': f'/financial_report/list/{kind}/indicators/{i}/accounts/{j}/values/{k}',
                        'raw_hash': stable_id(value), 'account_hash': stable_id(account),
                        'source_hash': stable_id(report)})
    result['normalized_financials'] = {'actuals': actuals,
        'source_period_requested': (result.get('financial_report_request') or {}).get('period'),
        'source_hash': stable_id(report), 'publication_time_provided': False,
        'scope': 'native provider report; fiscal label and reporting currency retained; share denominator unverified'}
    consensus = result.get('consensus')
    if isinstance(consensus, dict):
        normalized = {'currency': consensus.get('currency') or None,
            'period_type': consensus.get('current_period'), 'current_index': consensus.get('current_index'),
            'opt_periods': copy.deepcopy(consensus.get('opt_periods')), 'provider_updated_at': None,
            'observed_at': result.get('fetched_at'), 'source_hash': stable_id(consensus),
            'forecasts': [], 'annual_series': [],
            'verification_scope': 'period coverage/numeric aggregation only; not EPS unit or publication-time certification'}
        quarters = {}
        for i, row in enumerate(_records(consensus.get('list'))):
            if not isinstance(row, dict):
                continue
            period_type, fiscal_period = _fiscal_period(row.get('period_text'), row.get('fiscal_year'),
                row.get('fiscal_period'), consensus.get('current_period'))
            projected = {key: copy.deepcopy(row.get(key)) for key in ('fiscal_year', 'fiscal_period', 'period_text')}
            projected.update(period_type=period_type, period_start=None, period_end=None,
                raw_path=f'/consensus/list/{i}', raw_hash=stable_id(row), metrics=[])
            for j, metric in enumerate(_records(row.get('details'))):
                if not isinstance(metric, dict):
                    continue
                item = copy.deepcopy(metric)
                item.update(currency=normalized['currency'], price_unit=None,
                    raw_path=f'/consensus/list/{i}/details/{j}', raw_hash=stable_id(metric))
                projected['metrics'].append(item)
                if metric.get('key') not in {'eps', 'normalized_eps'}:
                    continue
                actual, estimate = _numeric(metric.get('actual')), _numeric(metric.get('estimate'))
                selected = actual if metric.get('is_released') is True else estimate if metric.get('is_released') is False else None
                basis = 'reported' if metric.get('is_released') is True else 'estimate' if metric.get('is_released') is False else 'unknown'
                if period_type == 'annual' and selected is not None:
                    normalized['annual_series'].append({'fiscal_year': row['fiscal_year'], 'metric': metric['key'],
                        'value': selected, 'currency': normalized['currency'], 'price_unit': None,
                        'value_kind': basis, 'method': 'native_annual', 'raw_paths': [item['raw_path']]})
                elif period_type == 'quarterly':
                    key = (row['fiscal_year'], metric['key'])
                    quarters.setdefault(key, {}).setdefault(fiscal_period, []).append((selected, basis, item['raw_path']))
            normalized['forecasts'].append(projected)
        for (year, metric), quarter_map in sorted(quarters.items()):
            if set(quarter_map) != {'Q1', 'Q2', 'Q3', 'Q4'} or any(len(v) != 1 or v[0][0] is None for v in quarter_map.values()):
                continue
            values = [quarter_map[q][0] for q in ('Q1', 'Q2', 'Q3', 'Q4')]
            kinds = {v[1] for v in values}
            normalized['annual_series'].append({'fiscal_year': year, 'metric': metric,
                'value': str(sum((decimal_value(v[0]) for v in values), decimal_value('0'))),
                'currency': normalized['currency'], 'price_unit': None,
                'value_kind': next(iter(kinds)) if len(kinds) == 1 else 'hybrid_reported_estimate',
                'method': 'sum_four_fiscal_quarters', 'raw_paths': [v[2] for v in values],
                'limitation': 'Derived sum; quarterly diluted share weights may differ from reported full-year EPS.'})
        # Duplicate native fiscal periods are ambiguous, even if values happen
        # to agree. Keep the raw rows, but do not certify their annual coverage.
        annual = normalized['annual_series']
        counts = {}
        for item in annual:
            key = (item['fiscal_year'], item['metric'])
            counts[key] = counts.get(key, 0) + 1
        for item in annual:
            item['coverage_verified'] = counts[(item['fiscal_year'], item['metric'])] == 1
        normalized['annual_forecast_verified'] = bool(result.get('source') == 'longbridge-openapi'
            and normalized['currency'] and any(v['coverage_verified'] and
            v['metric'] == 'eps' and v['value_kind'] == 'estimate' for v in annual))
        normalized['coverage'] = {'available_periods': len(normalized['forecasts']),
            'future_eps_quarters': sum(row['period_type'] == 'quarterly' and any(m.get('key') == 'eps'
                and m.get('is_released') is False and _numeric(m.get('estimate')) is not None for m in row['metrics'])
                for row in normalized['forecasts']),
            'annual_eps_available': bool(normalized['currency'] and any(v['coverage_verified'] and v['metric'] == 'eps' for v in annual)),
            'basis': 'explicit fiscal annual period or four distinct complete fiscal quarters; hybrid is not annual forecast'}
        normalized['source_response_verified'] = bool(result.get('source') == 'longbridge-openapi'
            and normalized['currency'] and normalized['forecasts'])
        result.update(consensus_normalized=normalized, consensus_response_received=True,
            consensus_live_verified=False)  # Native SDK supplies no provider update timestamp.
    primary = result.get('primary_financials')
    primary = primary if isinstance(primary, dict) else {}
    if primary.get('symbol') == result.get('symbol'):
        for fact in _records(primary.get('facts')):
            if isinstance(fact, dict):
                fact['source_verified'] = _primary_verified(store, fact)
        instrument = primary.get('instrument')
        instrument = instrument if isinstance(instrument, dict) else {}
        instrument['source_verified'] = _primary_verified(store, instrument)
    result['instrument_contract'] = instrument_contract(result, store=store)
    return result


def instrument_contract(fundamentals, *, store=None):
    """Describe native evidence scope without guessing ADR conversion or ETF EPS."""
    symbol = fundamentals.get('symbol')
    identity = fundamentals.get('identity')
    rows = [row for row in identity if isinstance(row, dict)
            and row.get('symbol') == symbol + '.US'] if isinstance(identity, list) and symbol else []
    profile = fundamentals.get('company_profile') or {}
    profile = profile if isinstance(profile, dict) else {}
    names = [row.get('name_en', '') for row in rows] + [profile.get('name', '')]
    is_etf = any('ETF' in str(name).upper().split() for name in names)
    asset_type = 'etf' if is_etf else 'equity' if profile.get('company_name') else 'unknown'
    currencies = {row.get('currency') for row in rows if row.get('currency')}
    from .common import stable_id
    contract = {'asset_type': asset_type, 'classification_basis': 'official identity/profile name and legal company name',
        'identity_sha256': stable_id(identity), 'source': fundamentals.get('source'),
        'quote_currency': next(iter(currencies)) if len(currencies) == 1 else None,
        'quote_price_unit': None, 'adr_direction': None,
        'stock_eps_valuation_applicable': False if is_etf else None,
        'financial_report_scope': 'fund accounting; not operating-company EPS' if is_etf else
            'native provider report; period, currency and per-share denominator require verification',
        'valuation_gap': 'ETF NAV, holdings and constituent/index earnings research unavailable' if is_etf else None}
    primary = fundamentals.get('primary_financials')
    primary = primary if isinstance(primary, dict) else {}
    instrument = primary.get('instrument')
    instrument = instrument if isinstance(instrument, dict) else {}
    if primary.get('symbol') == symbol and _primary_verified(store, instrument):
        unit = instrument.get('quote_price_unit')
        currency = instrument.get('quote_currency')
        excerpt = instrument.get('source_excerpt', '')
        unit_text = (re.search(r'\b(?:ADR|ADS)\b', excerpt, re.I) if unit == 'per_adr'
            else re.search(r'\b(?:(?:common|ordinary)\s+(?:stock|shares?)|per\s+(?:common\s+)?shares?)\b', excerpt, re.I))
        if (unit in {'per_share', 'per_adr'} and currency == contract['quote_currency']
                and currency is not None and unit_text
                and instrument.get('is_adr') is (unit == 'per_adr')):
            contract.update(quote_price_unit=unit,
                quote_unit_evidence_refs=list(instrument['evidence_refs']),
                quote_unit_source_excerpt=excerpt)
            if asset_type == 'equity' and instrument.get('stock_eps_valuation_applicable') is True:
                contract['stock_eps_valuation_applicable'] = True
            if asset_type == 'etf':
                etf = fundamentals.get('etf_valuation') or {}
                components = [etf.get(name) or {} for name in ('nav', 'pe', 'holdings')]
                nav, pe, holdings = components
                try:
                    dates = {datetime.strptime(item['as_of'], '%Y-%m-%d').date() for item in components}
                    valid = (etf.get('issuer_verified') is True and len(dates) == 1
                        and all(_primary_verified(store, item) for item in components)
                        and nav.get('currency') == 'USD' and nav.get('price_unit') == 'per_share'
                        and decimal_value(nav.get('value')) > 0 and decimal_value(pe.get('value')) > 0
                        and decimal_value('0.95') <= decimal_value(holdings.get('coverage')) <= decimal_value('1.01')
                        and bool(holdings.get('rows')) and bool(holdings.get('concentration')))
                    if fundamentals.get('fetched_at'):
                        valid = valid and next(iter(dates)) <= parse_time(fundamentals['fetched_at']).date()
                except (KeyError, ValueError, TypeError):
                    valid = False
                if valid:
                    contract.update(valuation_gap=None, etf_valuation_as_of=nav['as_of'],
                        financial_report_scope='issuer ETF NAV, portfolio PE and holdings supplied; use fund scenario methodology, never company EPS')
    return contract
