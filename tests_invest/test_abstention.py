import copy,hashlib,unittest
from rhino_invest.store import Store
from rhino_invest.common import stable_id,validate_plan
from rhino_invest.research import plan_fingerprint
from rhino_invest.abstention import review_abstentions,current_decision
from rhino_invest.reporting import plan_row
from rhino_invest.model import UnknownModelOutcome,ModelBudgetExceeded
from tests_invest.test_acceptance_core import approved_plan

NOW='2026-10-30T05:00:00Z'
class Reviewer:
    def __init__(self,accept=True,error=None,after=None):self.calls=[];self.accept=accept;self.error=error;self.after=after
    def run(self,purpose,payload,**kwargs):
        self.calls.append((purpose,payload,kwargs))
        if self.error:raise self.error('deferred')
        if self.after:self.after()
        return dict(accepted=self.accept,no_buy_reviewed=self.accept,issues=[] if self.accept else ['bad proposal'],
            checks=dict.fromkeys(['provenance','currency','unit','adr'],True))

class AbstentionTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:');self.addCleanup(self.s.close)
        p=approved_plan('AMZN');p.update(status='needs_review',research_intent='no_buy',
            source_published_at='2026-10-30T02:31:13Z',research_snapshot_id='video',fundamental_evidence_id='facts',
            research_assessment={'status':'needs_evidence'},review_issues=['valuation not established'])
        self.p=p;self.s.put('plan',p['id'],p)
        self.s.put('research_evidence','video',{'id':'video','video_id':'source','manifest_sha256':'sealed'})
        text='Official issuer common stock identity. No future price or investment return is stated.'
        self.s.put('external_evidence','issuer',{'content':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),
            'source_url':'https://www.sec.gov/test'},immutable=True)
        data={'identity':[{'symbol':'AMZN.US','currency':'USD','name_en':'Amazon','unrelated_shares':'999999'}],
            'financial_report':{'unreviewed_financial_value':'must remain archived'},
            'primary_financials':{'instrument':{'source_excerpt':text,'evidence_refs':['issuer']}}}
        self.s.put('fundamental_evidence','facts',{'data':data},immutable=True)

    def run_review(self,model):return review_abstentions(self.s,model,NOW,research_since='2026-10-30T00:00:00Z')

    def test_one_small_review_does_not_approve_or_change_rejected_valuation(self):
        before=copy.deepcopy(self.p);model=Reviewer();out=self.run_review(model)
        self.assertEqual(out['approved'],1);p=self.s.get('plan',self.p['id'])
        self.assertEqual(p['research_assessment'],before['research_assessment'])
        self.assertEqual(p['review_issues'],before['review_issues'])
        self.assertEqual(plan_fingerprint(p),plan_fingerprint(before));self.assertTrue(validate_plan(p))
        self.assertIsNotNone(current_decision(p))
        row=plan_row(p);self.assertEqual(row['单次买入美元'],'0');self.assertIn('估值待补齐',row['状态'])
        self.assertEqual(row['建议动作'],'不新增买入')
        for _ in range(3):self.run_review(model)
        self.assertEqual(len(model.calls),1)
        payload=model.calls[0][1];self.assertNotIn('financial_report',str(payload))
        self.assertNotIn('999999',str(payload));self.assertFalse(payload['proposal']['valuation_approved'])
        self.assertEqual(model.calls[0][2]['frames'],[])

    def test_rejection_or_stale_input_never_gets_a_display_approval(self):
        model=Reviewer(accept=False);self.run_review(model);self.run_review(model)
        self.assertEqual(len(model.calls),1);self.assertIsNone(current_decision(self.s.get('plan',self.p['id'])))

    def test_unknown_or_budget_defer_keeps_the_same_frozen_identity(self):
        for error in (UnknownModelOutcome,ModelBudgetExceeded):
            model=Reviewer(error=error);out=self.run_review(model)
            self.assertTrue(out['deferred']);self.assertEqual(self.s.items('research_abstention'),[])
        self.assertEqual(len(self.s.items('research_abstention_input')),1)
        self.assertEqual(self.run_review(Reviewer())['approved'],1)

    def test_concurrent_new_plan_stays_unapproved(self):
        def changed():self.s.put('plan',self.p['id'],dict(self.p,revision=self.p['revision']+1))
        self.run_review(Reviewer(after=changed))
        self.assertIsNone(current_decision(self.s.get('plan',self.p['id'])))

    def test_unverified_or_absent_identity_never_starts_review(self):
        for identity in (None,[],1):
            key='bad'+str(identity)
            self.s.put('fundamental_evidence',key,{'data':{'identity':identity}},immutable=True)
            self.s.put('plan',self.p['id'],dict(self.p,fundamental_evidence_id=key))
            model=Reviewer();self.run_review(model);self.assertEqual(model.calls,[])

    def test_graceful_stop_does_not_consume_or_seal_a_review_attempt(self):
        class Stopped(Reviewer):
            def run(self,*args,**kwargs):raise RuntimeError('Investment worker is stopping; no new paid call started')
        result=self.run_review(Stopped());self.assertTrue(result['deferred'])
        self.assertEqual(self.s.items('research_abstention'),[])
        self.assertEqual(self.run_review(Reviewer())['approved'],1)

if __name__=='__main__':unittest.main()
