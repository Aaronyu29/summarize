import base64
import hashlib
import unittest

from rhino_invest.primary_evidence import (enrich_primary_fundamentals, TSM_PAGE,
    GOOGLE_PDF, SOXX_PAGE, SOXX_HOLDINGS, SOXX_SPLIT_PDF, _fetch, _evidence, EvidenceError)
from rhino_invest.store import Store


NOW = '2026-10-24T01:00:00Z'
DOWNLOAD = 'https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?component=fundDownload&portfolioId=239705'
SPLIT_SUPPLEMENT = '''iShares Trust
Supplement dated October 23, 2026
for the iShares Semiconductor ETF (SOXX)
As previously announced, the Board approved a three-for-one forward stock split.
The record date for the split will be November 3, 2026, the distribution date will be
November 4, 2026, and the forward split will be recognized at the commencement of trading on The
Nasdaq Stock Market LLC on November 5, 2026 (the ex-dividend date).
PLEASE RETAIN THIS SUPPLEMENT
iShares Trust
Supplement dated August 21, 2026
for the iShares Semiconductor ETF (SOXX)
Fund Name Ticker Forward Split Ratio
iShares Semiconductor ETF SOXX 3:1
The record date for the forward stock split will be November 3, 2026, and the stock split will be
effectuated after the close of trading on November 4, 2026. Shares of the Fund will begin trading on a
split-adjusted basis on November 5, 2026.
PLEASE RETAIN THIS SUPPLEMENT
'''


def pdf(text):
    """A real tiny PDF, so the test exercises production PDF extraction too."""
    commands = ['BT /F1 10 Tf 14 TL 10 780 Td']
    for i, line in enumerate(text.splitlines()):
        escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        commands.append(('T* ' if i else '') + '(' + escaped + ') Tj')
    commands.append('ET')
    stream = '\n'.join(commands).encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1600 900] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    result, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(result)
    result += b'xref\n0 6\n0000000000 65535 f \n'
    result += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return result + f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode()


class Response:
    def __init__(self, content=b'', status=200, kind='text/plain', headers=None):
        self.content = content.encode() if isinstance(content, str) else content
        self.status_code = status
        self.headers = {'Content-Type': kind, **(headers or {})}


class Transport:
    def __init__(self, sources):
        self.sources, self.calls = sources, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        value = self.sources.get(url, Response(status=404))
        if isinstance(value, Exception):
            raise value
        return value


def google_sources():
    q2 = '''Alphabet Announces Second Quarter 2026 Results
MOUNTAIN VIEW, Calif. - July 22, 2026 - Alphabet Inc. (NASDAQ: GOOG, GOOGL)
financial results for the quarter ended June 30, 2026.
in millions, except for per share information and percentages).
Revenues $ 96,428 $ 119,796
Operating income $ 31,271 $ 40,770
Other income (expense), net $ 2,662 $ 97,983
Diluted net income per common share $ 2.31 $ 9.11
Other income reflected a net gain of $98.0 billion, primarily the result of net unrealized gains
on our equity securities.
'''
    fy = '''Alphabet Announces Fourth Quarter and Fiscal Year 2025 Results
MOUNTAIN VIEW, Calif. - February 4, 2026 - Alphabet Inc. (NASDAQ: GOOG, GOOGL)
financial results for the quarter ended December 31, 2025.
in millions, except for per share information and percentages).
Revenues $ 96,469 $ 113,828 $ 350,018 $ 402,836
Operating income $ 30,972 $ 35,934 $ 112,390 $ 129,039
Other income (expense), net $ 1,271 $ 3,183 $ 7,425 $ 29,787
Diluted net income per share $ 2.15 $ 2.82 $ 8.04 $ 10.81
'''
    return {GOOGLE_PDF.format(year=2026, quarter=2): Response(pdf(q2), kind='application/pdf'),
            GOOGLE_PDF.format(year=2025, quarter=4): Response(pdf(fy), kind='application/pdf')}


def tsm_sources():
    sources = {}
    for year, q in ((2026, 3), (2025, 4)):
        base = f'https://investor.tsmc.com/english/test/{year}q{q}/'
        sources[TSM_PAGE.format(year=year, quarter=q)] = Response(
            f'<html><h1>TSMC quarterly results {year} q{q}</h1><a href="{base}management.pdf">Management Report</a>'
            f'<a href="{base}FS.pdf">Financial Statements</a></html>', kind='text/html')
        if q == 3:
            text = '''TSMC 3Q26 Quarterly Management Report October 15, 2026
3Q26 2Q26 3Q25 QoQ YoY
EPS (NT$ per common share) 29.43 27.25 17.44 8.0% 68.7%
(US$ per ADR unit) 4.60 4.31 2.92
Net Revenue (US$ billions) 46.72 40.20 33.10 16.2% 41.2%
'''
            fs = '''TAIWAN SEMICONDUCTOR MANUFACTURING COMPANY LIMITED
Earnings per ADR - Diluted (2) $ 4.60 $ 147.13 $ 136.23 $ 87.22
(2) 1 ADR equals 5 ordinary shares.
'''
        else:
            text = '''TSMC 4Q25 Quarterly Management Report January 15, 2026
4Q25 3Q25 4Q24 QoQ YoY 2025 2024
EPS (NT$ per common share) 19.50 17.44 14.45 11.8% 35.0% 66.25 45.25
(US$ per ADR unit) 3.14 2.92 2.24 10.65 7.04
Net Revenue (US$ billions) 33.73 33.10 26.88 1.9% 25.5% 122.42 90.08
'''
            fs = '''TAIWAN SEMICONDUCTOR MANUFACTURING COMPANY LIMITED
Earnings per ADR - Diluted (2) $ 3.14 $ 97.52 $ 87.22 $ 72.25
(2) 1 ADR equals 5 ordinary shares.
For the Years Ended December 31 2025 2024
Earnings per ADR - Diluted (2) $ 10.65 $ 331.25 $ 226.24
'''
        sources[base + 'management.pdf'] = Response(pdf(text), kind='application/pdf')
        sources[base + 'FS.pdf'] = Response(pdf(fs), kind='application/pdf')
    return sources


def soxx_sources():
    page = f'''<html><h1>iShares Semiconductor ETF</h1>
<div>NAV as of Oct 23, 2026 $478.50</div>
<div>P/E Ratio 44.96 as of Oct 22, 2026</div>
<div>Expense Ratio: Fees as stated in the prospectus 0.33%</div>
<p>Important Notice Regarding a Corporate Action - On August 21, 2026, iShares filed to issue a forward stock split for the iShares Semiconductor ETF (NASDAQ: SOXX). The record date is November 3, 2026. Shares of SOXX will begin trading on a split-adjusted basis on November 5, 2026.</p>
<a href="{DOWNLOAD.replace('&', '&amp;')}">Data Download</a></html>'''
    holdings = '''iShares Semiconductor ETF
Fund Holdings as of,"Oct 22, 2026"
Ticker,Name,Asset Class,Weight (%)
AMD,ADVANCED MICRO DEVICES,Equity,60.00
NVDA,NVIDIA,Equity,40.00
USD,USD CASH,Cash,0.00
'''
    xml = '''<?xml version="1.0"?><ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><ss:Worksheet ss:Name="Historical"><ss:Table>
<ss:Row><ss:Cell><ss:Data>As Of</ss:Data></ss:Cell><ss:Cell><ss:Data>NAV per Share</ss:Data></ss:Cell></ss:Row>
<ss:Row><ss:Cell><ss:Data>Oct 23, 2026</ss:Data></ss:Cell><ss:Cell><ss:Data>478.504739</ss:Data></ss:Cell></ss:Row>
<ss:Row><ss:Cell><ss:Data>Oct 22, 2026</ss:Data></ss:Cell><ss:Cell><ss:Data>475.335589</ss:Data></ss:Cell></ss:Row>
<ss:Row><ss:Cell><ss:Data>Oct 20, 2026</ss:Data></ss:Cell><ss:Cell><ss:Data>500.282491</ss:Data></ss:Cell></ss:Row>
</ss:Table></ss:Worksheet></ss:Workbook>'''
    return {SOXX_PAGE: Response(page, kind='text/html'), SOXX_HOLDINGS: Response(holdings, kind='text/csv'),
            SOXX_SPLIT_PDF: Response(pdf(SPLIT_SUPPLEMENT), kind='application/pdf'),
            DOWNLOAD: Response(xml, kind='application/vnd.ms-excel')}


class PrimaryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)

    def enrich(self, symbol, sources, now=NOW, fundamentals=None):
        transport = Transport(sources)
        data = enrich_primary_fundamentals(self.store, symbol, fundamentals or {}, now=now, transport=transport)
        return data, transport

    def test_tsm_real_pdf_explicit_annual_and_quarterly_adr_eps_without_provider_multiplication(self):
        original = {'financial_report': {'eps': '10.5478', 'currency': 'USD'}, 'consensus': {'eps': '5.6737'}}
        data, transport = self.enrich('TSM', tsm_sources(), fundamentals=original)
        facts = data['primary_financials']['facts']
        self.assertIn(('annual', 'per_adr', '10.65'), {(f['period_type'], f['price_unit'], f['value']) for f in facts})
        self.assertIn(('quarterly', 'per_adr', '4.60'), {(f['period_type'], f['price_unit'], f['value']) for f in facts})
        self.assertNotIn('52.7390', str(facts))
        self.assertEqual(data['financial_report'], original['financial_report'])
        self.assertEqual(data['consensus'], original['consensus'])
        self.assertNotIn('primary_financials', original)
        self.assertEqual(data['primary_gaps'], [])
        self.assertTrue(data['adr_conversion_verified'])
        self.assertEqual(data['reviewed_identity']['metadata']['ordinary_shares_per_adr'], '5')
        self.assertEqual(len(transport.calls), 6)
        for _, evidence in self.store.items('external_evidence'):
            self.assertEqual(evidence['sha256'], hashlib.sha256(evidence['content'].encode()).hexdigest())
            source = self.store.get('primary_source', evidence['source_id'])
            self.assertEqual(source['raw_sha256'], hashlib.sha256(base64.b64decode(source['raw_base64'])).hexdigest())

    def test_tsm_ratio_or_pdf_unit_conflict_does_not_create_verified_binding(self):
        sources = tsm_sources()
        for url in list(sources):
            if url.endswith('FS.pdf'):
                sources[url] = Response(pdf('TSMC consolidated financial report but no verified ADR ratio or per ADR EPS denominator.'), kind='application/pdf')
        data, _ = self.enrich('TSM', sources)
        self.assertFalse(data.get('adr_conversion_verified', False))
        self.assertIsNone(self.store.get('instrument_identity', 'TSM'))
        self.assertTrue(data['primary_gaps'])

    def test_google_eps_is_reported_common_share_and_investment_gains_remain_explicit(self):
        data, transport = self.enrich('GOOGL', google_sources())
        facts = data['primary_financials']['facts']
        annual = next(f for f in facts if f['metric'] == 'eps_diluted' and f['period_type'] == 'annual')
        self.assertEqual((annual['value'], annual['currency'], annual['price_unit']), ('10.81', 'USD', 'per_share'))
        self.assertEqual(annual['eps_scope'], 'consolidated_all_classes')
        self.assertEqual(data['primary_financials']['class_eps_reconciliation']['status'], 'not_verified')
        self.assertEqual(annual['published_at'], '2026-02-04T23:59:59+00:00')
        quarter = next(f for f in facts if f['metric'] == 'eps_diluted' and f['period_end'] == '2026-06-30')
        self.assertEqual(quarter['value'], '9.11')
        self.assertIn('not normalized or recurring', data['primary_financials']['non_operating_items'][0]['warning'])
        self.assertNotIn('forecast', data['primary_financials'])
        self.assertTrue(any(g['blocking'] and g.get('field')=='GOOGL_class_filing' for g in data['primary_gaps']))
        self.assertEqual(len(transport.calls), 5)

    def test_goog_uses_shared_group_filing_but_preserves_native_ticker_quote_and_class_scope(self):
        native = {'symbol': 'GOOG', 'identity': [{'symbol': 'GOOG.US', 'currency': 'USD'}],
            'reference_quote': {'selected': {'symbol': 'GOOG', 'price': '319.71'}},
            'financial_report': {'native_scope': 'GOOG provider'}}
        data, _ = self.enrich('GOOG', google_sources(), fundamentals=native)
        self.assertEqual(data['symbol'], 'GOOG')
        self.assertEqual(data['identity'], native['identity'])
        self.assertEqual(data['reference_quote'], native['reference_quote'])
        self.assertEqual(data['primary_financials']['symbol'], 'GOOG')
        self.assertEqual(data['primary_financials']['class_eps_reconciliation']['share_class'], 'Class C')
        eps = [f for f in data['primary_financials']['facts'] if f['metric'] == 'eps_diluted']
        self.assertTrue(eps)
        self.assertTrue(all(f['eps_scope'] == 'consolidated_all_classes' for f in eps))
        self.assertFalse(any(f.get('eps_scope') == 'Class C' for f in eps))
        self.assertEqual(self.store.items('plan'), [])

    def test_soxx_aligns_historical_nav_with_pe_and_holdings_without_future_split_adjustment(self):
        data, _ = self.enrich('SOXX', soxx_sources())
        etf = data['etf_valuation']
        self.assertTrue(etf['issuer_verified'])
        self.assertEqual(etf['nav']['value'], '475.335589')
        self.assertEqual({etf[k]['as_of'] for k in ('nav', 'pe', 'holdings')}, {'2026-10-22'})
        self.assertEqual(etf['pe']['value'], '44.96')
        self.assertEqual(etf['holdings']['coverage'], '1.00')
        self.assertEqual(etf['holdings']['rows'][0]['weight'], '0.60')
        self.assertEqual(etf['corporate_actions'][0]['status'], 'announced_not_effective')
        self.assertEqual(etf['corporate_actions'][0]['effective_at'], '2026-11-05')
        self.assertEqual(etf['corporate_actions'][0]['new_shares_per_old'], '3')
        self.assertFalse(etf['corporate_actions'][0]['automatic_price_adjustment'])
        self.assertFalse(data['primary_financials']['instrument']['stock_eps_valuation_applicable'])
        self.assertEqual(data['primary_refresh_after'], '2026-10-25T01:00:00+00:00')

    def test_latest_holdings_and_pe_cannot_be_backdated_to_video_day(self):
        data, _ = self.enrich('SOXX', soxx_sources(), now='2026-10-20T23:59:59Z')
        etf = data['etf_valuation']
        self.assertFalse(etf['issuer_verified'])
        self.assertIsNone(etf['pe'])
        self.assertIsNone(etf['holdings'])
        self.assertEqual(etf['nav']['value'], '500.282491')
        self.assertEqual(etf['nav']['as_of'], '2026-10-20')
        self.assertTrue(any(g['code'] == 'SOURCE_NOT_AVAILABLE_AS_OF' for g in data['primary_gaps']))

    def test_malformed_or_partial_holdings_cannot_claim_verified_etf_valuation(self):
        sources = soxx_sources()
        sources[SOXX_HOLDINGS] = Response('iShares Semiconductor ETF\nFund Holdings as of,"Oct 22, 2026"\nTicker,Name,Asset Class,Weight (%)\nAMD,AMD,Equity,60.00\n', kind='text/csv')
        data, _ = self.enrich('SOXX', sources)
        self.assertFalse(data['etf_valuation']['issuer_verified'])
        self.assertIsNone(data['etf_valuation']['holdings'])
        self.assertTrue(any(g['code'] == 'SOURCE_HOLDINGS_COVERAGE_INCOMPLETE' for g in data['primary_gaps']))

    def test_timestamp_only_retry_does_not_change_evidence_hash_or_identity(self):
        sources = tsm_sources()
        data, first = self.enrich('TSM', sources)
        again, second = self.enrich('TSM', sources, now='2026-10-24T02:00:00Z')
        self.assertEqual(second.calls, [])
        self.assertEqual(data['primary_evidence_hash'], again['primary_evidence_hash'])
        self.assertEqual(data['reviewed_identity'], again['reviewed_identity'])
        self.assertEqual(len(self.store.items('instrument_identity_revision')), 1)
        self.assertTrue(first.calls)

    def test_refetched_pdf_metadata_does_not_rearm_evidence_or_identity(self):
        sources = tsm_sources()
        data, _ = self.enrich('TSM', sources)
        changed = {url: Response(response.content + b'\n% modified container metadata\n', kind=response.headers['Content-Type'])
                   if url.endswith('.pdf') else response for url, response in sources.items()}
        again, transport = self.enrich('TSM', changed, now='2026-10-24T08:00:00Z')
        self.assertEqual(len(transport.calls), 6)
        self.assertEqual(data['primary_evidence_hash'], again['primary_evidence_hash'])
        self.assertEqual(data['reviewed_identity'], again['reviewed_identity'])
        self.assertEqual(len(self.store.items('external_evidence')), 4)

    def test_source_period_or_future_publication_is_not_relabelled_as_current_quarter(self):
        sources = tsm_sources()
        management = next(url for url in sources if '2026q3' in url and url.endswith('management.pdf'))
        sources[management] = Response(pdf('TSMC 3Q26 Management Report October 25, 2026\nEPS (NT$ per common share) 29.43 27.25 17.44\n(US$ per ADR unit) 4.60 4.31 2.92\nNet Revenue (US$ billions) 46.72 40.20 33.10\n'), kind='application/pdf')
        data, _ = self.enrich('TSM', sources)
        self.assertTrue(any(g['code'] == 'SOURCE_NOT_AVAILABLE_AS_OF' for g in data['primary_gaps']))
        self.assertFalse(any(f['period_end'] == '2026-09-30' for f in data['primary_financials']['facts']))

    def test_transport_failure_is_typed_and_retryable_without_raw_exception_or_fake_verification(self):
        data, _ = self.enrich('SOXX', {SOXX_PAGE: TimeoutError('secret token=private')})
        self.assertEqual(data['primary_gaps'][0]['code'], 'PRIMARY_SOURCE_UNAVAILABLE')
        self.assertNotIn('private', str(data))
        self.assertNotIn('etf_valuation', data)
        self.assertEqual(data['primary_refresh_after'], '2026-10-24T07:00:00+00:00')

    def test_redirect_to_unapproved_host_is_never_followed(self):
        transport = Transport({SOXX_PAGE: Response(status=302, headers={'Location': 'http://127.0.0.1/private'})})
        data = enrich_primary_fundamentals(self.store, 'SOXX', {}, now=NOW, transport=transport)
        self.assertEqual(transport.calls, [SOXX_PAGE])
        self.assertEqual(data['primary_gaps'][0]['code'], 'SOURCE_REDIRECT_NOT_ALLOWED')

    def test_pdf_url_returning_html_login_is_rejected(self):
        sources = google_sources()
        sources[GOOGLE_PDF.format(year=2026, quarter=2)] = Response('<html><body>Authentication challenge login required to read this report, not a financial statement.</body></html>', kind='text/html')
        data, _ = self.enrich('GOOGL', sources)
        self.assertTrue(any(g['code'] == 'SOURCE_CONTENT_INVALID' for g in data['primary_gaps']))
        self.assertFalse(any(f['period_end'] == '2026-06-30' for f in data['primary_financials']['facts']))

    def test_external_text_hash_id_ignores_changed_pdf_container_metadata(self):
        source = {'id': 'container-a', 'source_url': SOXX_HOLDINGS, 'content': 'public issuer text',
            'sha256': hashlib.sha256(b'public issuer text').hexdigest(), 'raw_sha256': 'a',
            'observed_at': NOW, 'parser': 'test'}
        first = _evidence(self.store, source, period_end='2026-10-22')
        second = _evidence(self.store, dict(source, id='container-b', raw_sha256='b', observed_at='2026-10-25T01:00:00Z'), period_end='2026-10-22')
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.items('external_evidence')), 1)

    def test_unknown_symbol_does_not_fetch_or_rewrite_native_values(self):
        original = {'financial_report': {'EPS': None}}
        data, transport = self.enrich('ZZZZ', {}, fundamentals=original)
        self.assertEqual(transport.calls, [])
        self.assertEqual(data, dict(original, symbol='ZZZZ'))


if __name__ == '__main__':
    unittest.main()
