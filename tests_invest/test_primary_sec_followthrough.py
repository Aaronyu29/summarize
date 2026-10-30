import copy
import json
import unittest
from unittest.mock import patch
from rhino_invest.primary_evidence import enrich_primary_fundamentals, SEC_ISSUERS, QQQ_PAGE, INTEL_RESULTS
from rhino_invest.market import normalize_fundamentals
from rhino_invest.store import Store
from rhino_invest.common import stable_id, validate_plan, classify_plan_validation
from rhino_invest.remediation import update_research_followups
from tests_invest.test_primary_evidence_delivery import Transport, Response

NOW='2026-10-30T05:00:00Z'

def sources(symbol='MSFT'):
    cik=SEC_ISSUERS[symbol]
    rows=[dict(start='2026-07-01',end='2026-09-30',val=4.2,accn='latest',filed='2026-10-29',form='10-Q'),
          dict(start='2026-01-01',end='2026-09-30',val=12.0,accn='latest',filed='2026-10-29',form='10-Q'),
          dict(start='2026-07-01',end='2026-09-30',val=99,accn='future',filed='2026-10-30',form='10-Q')]
    listing=dict(cik=cik,name=symbol,tickers=[symbol],exchanges=['Nasdaq'],filings={'recent':{
        'accessionNumber':['latest','future'], 'acceptanceDateTime':['2026-10-29T20:00:00Z','2026-10-30T06:00:00Z']}})
    facts=dict(cik=int(cik),entityName=symbol,facts={'us-gaap':{'EarningsPerShareDiluted':{
        'label':'Earnings Per Share, Diluted','units':{'USD/shares':rows}}}})
    return {f'https://data.sec.gov/submissions/CIK{cik}.json':Response(json.dumps(listing)),
            f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json':Response(json.dumps(facts))}

class PrimaryFollowthroughTests(unittest.TestCase):
    def test_exact_sec_period_unit_cutoff_and_cache_without_model_calls(self):
        with Store(':memory:') as s:
            transport=Transport(sources())
            raw={'symbol':'MSFT','identity':[{'symbol':'MSFT.US','currency':'USD'}],
                'company_profile':{'company_name':'Microsoft'}}
            first=enrich_primary_fundamentals(s,'MSFT',raw,now=NOW,transport=transport)
            result=normalize_fundamentals(first,store=s)
            facts=result['primary_financials']['facts'];self.assertEqual(len(facts),1)
            self.assertEqual((facts[0]['value'],facts[0]['fiscal_year'],facts[0]['fiscal_period']),('4.2',2027,'Q1'))
            self.assertEqual(facts[0]['period_start'],'2026-07-01');self.assertTrue(facts[0]['source_verified'])
            self.assertEqual(result['instrument_contract']['quote_price_unit'],'per_share')
            second=enrich_primary_fundamentals(s,'MSFT',raw,now=NOW,transport=transport)
            self.assertEqual(first['primary_evidence_hash'],second['primary_evidence_hash'])
            self.assertEqual(len(transport.calls),2);self.assertEqual(s.items('model_call'),[])
            ev=s.get('external_evidence',facts[0]['evidence_refs'][0]);ev['content']+='tampered'
            original=s.get
            with patch.object(s,'get',side_effect=lambda kind,key: ev if kind=='external_evidence' and key==ev['id'] else original(kind,key)):
                self.assertFalse(normalize_fundamentals(first,store=s)['primary_financials']['facts'][0]['source_verified'])

    def test_issuer_mismatch_does_not_certify_units_or_emit_facts(self):
        values=sources();key=f'https://data.sec.gov/submissions/CIK{SEC_ISSUERS["MSFT"]}.json'
        listing=json.loads(values[key].content);listing['tickers']=['FAKE'];values[key]=Response(json.dumps(listing))
        with Store(':memory:') as s:
            result=enrich_primary_fundamentals(s,'MSFT',{},now=NOW,transport=Transport(values))
            self.assertEqual(result['primary_financials']['facts'],[])
            self.assertEqual(result['primary_gaps'][0]['code'],'SEC_INSTRUMENT_IDENTITY_MISMATCH')

    def test_twelve_month_observation_is_not_mislabelled_as_fiscal_full_year(self):
        values=sources('AMZN');url=f'https://data.sec.gov/api/xbrl/companyfacts/CIK{SEC_ISSUERS["AMZN"]}.json'
        data=json.loads(values[url].content)
        data['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'].append(
            dict(start='2025-10-01',end='2026-09-30',val=12,accn='latest',filed='2026-10-29',form='10-Q'))
        values[url]=Response(json.dumps(data))
        with Store(':memory:') as s:
            out=enrich_primary_fundamentals(s,'AMZN',{},now=NOW,transport=Transport(values))
            ttm=next(f for f in out['primary_financials']['facts'] if f['period_type']=='trailing_twelve_months')
            self.assertEqual(ttm['fiscal_period'],'TTM');self.assertIsNone(ttm['fiscal_year'])
            self.assertFalse(any(f['fiscal_period']=='FY' for f in out['primary_financials']['facts']))

    def test_qqq_official_identity_is_not_a_fabricated_etf_valuation(self):
        page='Invesco QQQ Trust, Series 1\nETF Shares are not individually redeemable. Details on the official issuer website.'
        with Store(':memory:') as s:
            result=enrich_primary_fundamentals(s,'QQQ',{'symbol':'QQQ'},now=NOW,
                transport=Transport({QQQ_PAGE:Response(page)}))
            result=normalize_fundamentals(result,store=s)
            self.assertEqual(result['instrument_contract']['asset_type'],'etf')
            self.assertIsNone(result['instrument_contract']['quote_price_unit'])
            self.assertFalse(result['instrument_contract']['stock_eps_valuation_applicable'])
            self.assertTrue(result['primary_gaps']);self.assertNotIn('etf_valuation',result)

    def test_newer_intel_issuer_fallback_must_match_sec_earnings_date(self):
        for date,ok in [('October 29, 2026',True),('July 23, 2026',False)]:
            values=sources('INTC');url=f'https://data.sec.gov/submissions/CIK{SEC_ISSUERS["INTC"]}.json'
            listing=json.loads(values[url].content)
            listing['filings']['recent'].update(form=['10-Q','10-Q','8-K'],items=['','','2.02'],
                accessionNumber=['latest','future','0000050863-26-000188'],
                acceptanceDateTime=['2026-10-29T20:00:00Z','2026-10-30T06:00:00Z','2026-10-30T01:00:00Z'],
                primaryDocument=['q.htm','future.htm','intc-20261029.htm'],filingDate=['2026-10-29','2026-10-30','2026-10-29'])
            values[url]=Response(json.dumps(listing))
            release='https://www.intc.com/news-events/press-releases/detail/1786/intel-reports-third-quarter-2026-financial-results'
            values[INTEL_RESULTS]=Response('<html><p>Intel official quarterly financial results and reports for shareholders and public investors</p><a href="'+release+'">Earnings Release</a></html>',kind='text/html')
            values[release]=Response('Intel Reports Third-Quarter 2026 Financial Results\n'+date+'\nActual GAAP results and separately labelled fourth quarter guidance.')
            with Store(':memory:') as s:
                result=enrich_primary_fundamentals(s,'INTC',{},now=NOW,transport=Transport(values))
                self.assertEqual('latest_earnings_release' in result['primary_financials'],ok)
                self.assertEqual(bool(result['primary_gaps']),not ok)

    def test_every_failed_plan_gets_durable_system_followup_without_paid_retries(self):
        with Store(':memory:') as s:
            plan=dict(id=stable_id('investment-plan','MSFT'),symbol='MSFT',revision=1,status='needs_review',
                research_snapshot_id='video',fundamental_evidence_id='facts',
                research_assessment={'status':'needs_evidence'},research_gaps=[{'code':'EPS','blocking':True}])
            s.put('plan',plan['id'],plan)
            s.put('fundamental_cache','MSFT',{'data':{'primary_refresh_after':'2026-10-31T05:00:00Z'}})
            result=update_research_followups(s,NOW);self.assertEqual(result['awaiting_evidence'],1)
            first=s.get('research_followup','MSFT');self.assertTrue(first['system_owned'])
            self.assertFalse(first['repeat_same_evidence_paid_calls_allowed'])
            update_research_followups(s,'2026-10-30T05:01:00Z')
            self.assertEqual(first,s.get('research_followup','MSFT'));self.assertEqual(s.items('model_call'),[])

    def test_price_free_abstention_never_relaxes_execution_or_hides_valued_target_errors(self):
        from tests_invest.test_acceptance_core import approved_plan
        p=approved_plan('QQQ');p.update(status='needs_review',research_intent='no_buy',targets=[],take_profit=[],
            price_unit=None,valuation_basis={'kind':'insufficient_valuation'},stop={'price':None,'confirmation':'daily_close'})
        p['entry'].update(low=None,high=None,conditions=[],execution_conditions=[])
        self.assertIn('no-buy research cannot authorize execution',validate_plan(p))
        self.assertNotIn('price_unit must be per_share/per_adr',classify_plan_validation(p)['research_errors'])
        self.assertIn('price_unit must be per_share/per_adr',validate_plan(p))
        p['entry']['amount_usd']='0';p['horizon_months']=None
        p['verification']['unit']=False
        self.assertEqual(classify_plan_validation(p)['research_errors'],[])
        self.assertTrue(validate_plan(p))
        from rhino_invest.research import _assess_plan
        p['entry']['basis']=None
        review={'accepted':True,'no_buy_reviewed':True,'issues':[],
            'checks':dict.fromkeys(['provenance','currency','unit','adr'],True)}
        issues=_assess_plan(p,review,p['evidence_refs'],{'instrument_contract':{'asset_type':'etf'}})
        self.assertNotIn('Explicit entry basis required: source_level or independent_valuation',issues)
        changed=copy.deepcopy(p);changed['research_intent']=None
        self.assertIn('price_unit must be per_share/per_adr',classify_plan_validation(changed)['research_errors'])

    def test_closed_malformed_symbol_does_not_block_other_symbols_but_unknown_billing_does(self):
        from rhino_invest.research import _compile_archive_symbols
        from rhino_invest.model import ModelOutputInvalid,UnknownModelOutcome
        with Store(':memory:') as s:
            s.put('model_receipt','closed',{'response':{'choices':[]}},immutable=True)
            s.put('plan',stable_id('investment-plan','AAA'),{'id':stable_id('investment-plan','AAA'),
                'symbol':'AAA','status':'active','review_issues':[]})
            calls=[]
            def compile_one(store,snapshot,levels,*args,**kwargs):
                symbol=levels[0]['symbol'];calls.append(symbol)
                if symbol=='AAA':raise ModelOutputInvalid('bad',call_id='closed')
                return [{'symbol':symbol,'status':'needs_review'}]
            result={'held':[]};snapshot={'id':'video','video_id':'source','revision':'1'}
            with patch('rhino_invest.research.compile_plan',side_effect=compile_one):
                output=_compile_archive_symbols(s,snapshot,[{'symbol':'AAA'},{'symbol':'BBB'}],object(),{},NOW,result)
            self.assertEqual(calls,['AAA','BBB']);self.assertEqual(len(result['held']),1)
            self.assertEqual(output[-1]['symbol'],'BBB')
            self.assertEqual(s.get('plan',stable_id('investment-plan','AAA'))['status'],'needs_review')
            with patch('rhino_invest.research.compile_plan',side_effect=UnknownModelOutcome('billing unknown')):
                with self.assertRaises(UnknownModelOutcome):
                    _compile_archive_symbols(s,snapshot,[{'symbol':'AAA'},{'symbol':'BBB'}],object(),{},NOW,{'held':[]})

    def test_first_bad_symbol_stays_visible_for_future_recovery_without_approved_revision(self):
        from rhino_invest.research import _compile_archive_symbols
        from rhino_invest.model import ModelOutputInvalid
        with Store(':memory:') as s:
            s.put('model_receipt','closed',{'response':{}},immutable=True)
            snapshot={'id':'video','video_id':'source','revision':'1','metadata':{'upload_date':'20261030'}}
            with patch('rhino_invest.research.compile_plan',side_effect=ModelOutputInvalid('bad',call_id='closed')):
                plans=_compile_archive_symbols(s,snapshot,[{'symbol':'AAA'}],object(),{},NOW,{'held':[]})
            self.assertEqual(plans[0]['revision'],0)
            self.assertEqual(s.get('plan',plans[0]['id'])['status'],'needs_review')
            self.assertFalse(s.items('plan_revision'))

if __name__=='__main__':unittest.main()
