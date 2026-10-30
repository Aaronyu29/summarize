import base64,copy,hashlib,json,unittest
from unittest.mock import patch
from rhino_invest.store import Store
from rhino_invest.primary_evidence import _qqq,QQQ_PAGE,QQQ_API,_amazon_operating_basis
from rhino_invest.market import normalize_fundamentals,instrument_contract
from rhino_invest.underwriting import author,material_key,projection,advance,execution_gaps
from rhino_invest.research import _nested_refs,_etf_input_gaps,_price_basis_deadline
from rhino_invest.common import stable_id
from tests_invest.test_primary_evidence_delivery import Response,Transport

NOW='2026-10-30T08:00:00Z'

def qqq_sources():
    maps={'ticker':'QQQ','cusip':'46090E103','totalExpenseRatio':'0.18'}
    page='<html>Invesco QQQ Trust, Series 1. ETF Shares are not individually redeemable. '
    page+='<script>data='+json.dumps({'productListFieldValueMap':maps})+'</script></html>'
    pe={'cusip':'46090E103','currencyCode':'USD','effectiveDate':'2026-09-30','priceToEarningsRatio':29.480945}
    navs={'cusip':'46090E103','currency':'USD','lineChartData':[{'type':'NAV','data':[
        {'date':'09/30/2026','value':739.617545},{'date':'10/29/2026','value':715.178947}]}]}
    prices={'cusip':'46090E103','currency':'USD','effectiveDate':'2026-10-29','nav':715.178947,
        'marketValue':715178947,'sharesOutstanding':1000000}
    holdings={'cusip':'46090E103','effectiveDate':'2026-10-28','holdings':[
        {'ticker':'NVDA','percentageOfTotalNetAssets':100.1,'securityTypeName':'Common Stock'},
        {'ticker':None,'percentageOfTotalNetAssets':-.1,'securityTypeName':'Synthetic Cash'},
        {'ticker':'USDPDV','percentageOfTotalNetAssets':None,'securityTypeName':'Currency'}]}
    # Avoid an impossible individual position above 100% in the fixture.
    holdings['holdings'][0]['percentageOfTotalNetAssets']=50.1
    holdings['holdings'].append({'ticker':'MSFT','percentageOfTotalNetAssets':50,'securityTypeName':'Common Stock'})
    values=[('?expand=nav&idType=cusip&variationType=fundCharacteristics&productType=ETF',pe),
        ('/navs?idType=cusip&productType=ETF',navs),('/prices?idType=cusip&variationType=priceListing&productType=ETF',prices),
        ('/holdings/fund?idType=cusip&productType=ETF',holdings)]
    return {QQQ_PAGE:Response(page),**{QQQ_API+path:Response(json.dumps(value)) for path,value in values}}

class UnderwritingTests(unittest.TestCase):
    def test_review_only_pipeline_commits_and_reuses_one_independent_review(self):
        from tests_invest.test_research import FixtureModel
        with Store(':memory:') as s:
            raw=self.collect(s);model=FixtureModel();model.config={'primary_underwriting_symbols':('QQQ',)}
            source={'id':'v','video_id':'53SJvdzJneM','title':'sealed report','revision':'1','metadata':{'upload_date':'20261030'},
                'source':'unused','analysis':{},'transcript':{'segments':[]},'frames':[],'files':[],
                'visual_caches':{},'analysis_sha256':'sealed','manifest_sha256':'sealed'}
            s.put('research_evidence','v',source)
            s.put('research_level','l',{'symbol':'QQQ','verified':False,'evidence_refs':['v']})
            pid=stable_id('investment-plan','QQQ')
            s.put('plan',pid,{'id':pid,'symbol':'QQQ','revision':1,'status':'needs_review','research_snapshot_id':'v'})
            result=advance(s,model,{'QQQ':raw},NOW)
            self.assertEqual(result['errors'],[],s.items('primary_underwriting'));self.assertEqual(result['processed'],1)
            self.assertEqual(model.calls,['review_plan'])
            p=s.get('plan',pid);self.assertEqual(p['status'],'active',p.get('review_issues'))
            self.assertEqual(advance(s,model,{'QQQ':raw},NOW)['processed'],0)
            self.assertEqual(model.calls,['review_plan'])

    def collect(self,s,mapping=None):
        out={'symbol':'QQQ','identity':[{'symbol':'QQQ.US','currency':'USD'}],
            'primary_financials':{'symbol':'QQQ','facts':[]},'primary_evidence':[],'primary_gaps':[]}
        _qqq(s,out,NOW,Transport(mapping or qqq_sources()))
        out=normalize_fundamentals(out,store=s);out['instrument_contract']=instrument_contract(out,store=s)
        return out

    def test_month_end_alignment_complete_scenarios_and_risk_policy(self):
        with Store(':memory:') as s:
            raw=self.collect(s)
            self.assertEqual(raw['instrument_contract']['quote_price_unit'],'per_share')
            self.assertEqual(_etf_input_gaps(raw,NOW,set(_nested_refs(raw)),s),[])
            p=author('QQQ',raw,s,NOW)
            self.assertEqual([t['price'] for t in p['targets']],['452.84','731.57','944.11'])
            self.assertEqual(p['entry']['low'],'548.68');self.assertEqual(p['entry']['high'],'585.26')
            self.assertEqual(p['stop']['price'],'493.81')
            self.assertEqual(_price_basis_deadline(raw,NOW,set(_nested_refs(raw)),s),'2026-11-15T00:00:00+00:00')
            raw['etf_valuation']['nav']['as_of']='2026-10-29'
            self.assertTrue(_etf_input_gaps(raw,NOW,set(_nested_refs(raw)),s))

    def test_currency_share_reconciliation_and_raw_tamper_fail_closed(self):
        with Store(':memory:') as s:
            raw=self.collect(s);ref=raw['etf_valuation']['latest_nav']['evidence_refs'][0]
            ev=s.get('external_evidence',ref);ev['content']+='tampered';original=s.get
            with patch.object(s,'get',side_effect=lambda kind,key:ev if kind=='external_evidence' and key==ref else original(kind,key)):
                with self.assertRaises(ValueError):author('QQQ',raw,s,NOW)
        mapping=qqq_sources();key=next(k for k in mapping if '/prices?' in k)
        body=json.loads(mapping[key].content);body['sharesOutstanding']=1
        mapping[key]=Response(json.dumps(body))
        with Store(':memory:') as s:
            with self.assertRaises(ValueError):self.collect(s,mapping)

    def test_daily_nav_churn_does_not_reset_paid_valuation_fence(self):
        with Store(':memory:') as s:
            raw=self.collect(s);key=material_key('QQQ',raw)
            raw['etf_valuation']['latest_nav']['value']='716'
            raw['etf_valuation']['holdings']['as_of']='2026-10-29'
            self.assertEqual(material_key('QQQ',raw),key)
            raw['etf_valuation']['pe']['value']='28'
            self.assertNotEqual(material_key('QQQ',raw),key)

    def test_new_material_facts_block_old_entry_and_native_forecasts_excluded(self):
        with Store(':memory:') as s:
            raw=self.collect(s);raw['financial_report']={'unverified_forecast':'do not send'}
            self.assertNotIn('financial_report',projection(raw))
            p=author('QQQ',raw,s,NOW);p.update(id='p',revision=1)
            s.put('fundamental_cache','QQQ',{'data':raw})
            self.assertIn('primary_valuation_changed_requires_review',execution_gaps(s,p,{'price':'550'},NOW))

    def test_amazon_ttm_columns_use_worldwide_not_segment_or_oneoff(self):
        text='''Supplemental Financial Information and Business Metrics
(in millions, except per share data)
Q2 2025Q3 2025Q4 2025Q1 2026Q2 2026Q3 2026Y/Y %
Operating cash flow -- trailing twelve months (TTM)$121,137 $130,691 $139,514 $148,531 $161,403 $168,300 29 %
Free cash flow -- TTM (1)$18,184 $14,788 $11,194 $1,232 $(7,604)$(42,357)(386)%
Operating income -- TTM$76,190 $76,201 $79,975 $85,422 $93,712 $104,366 37 %
Supplemental Financial Information and Business Metrics
Operating income -- TTM$1 $2 $3 $4 $5 $999,999
Weighted-average shares used in computation of earnings per share:
Basic10,674 10,797 10,638 10,770
Diluted10,845 10,929 10,815 10,903'''
        with Store(':memory:') as s:
            s.put('external_evidence','e',{'content':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),
                'source_url':'https://www.sec.gov/Archives/edgar/data/1018724/test.htm','published_at':NOW})
            out={'symbol':'AMZN','primary_evidence':[],'primary_gaps':[],
                'primary_financials':{'latest_earnings_release':{'evidence_refs':['e']}}}
            _amazon_operating_basis(s,out,{},NOW,Transport({}))
            basis=out['primary_financials']['operating_basis']
            self.assertEqual(basis['operating_income_ttm']['value'],'104.366')
            self.assertEqual(basis['free_cash_flow_ttm']['value'],'-42.357')
            self.assertEqual(basis['diluted_shares_quarter']['value'],'10.929')
            self.assertEqual(out['primary_gaps'],[])

if __name__=='__main__':unittest.main()
