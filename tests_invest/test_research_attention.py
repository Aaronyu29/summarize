import copy
import unittest

from rhino_invest.feishu import UnknownOutcome
from rhino_invest.research_attention import attention_items, notify_attention
from rhino_invest.reporting import plan_row
from rhino_invest.store import Store


NOW='2026-10-27T06:30:00Z'


def plan(symbol='TSM', conditional=False):
    return {'id':'p-'+symbol,'symbol':symbol,'status':'needs_review','revision':1,
        'review_evidence_id':'review-'+symbol,
        'research_assessment':{'status':'approved_conditional' if conditional else 'needs_evidence'},
        'review_issues':[] if conditional else ['TAKE_PROFIT_BELOW_ENTRY_THESIS_INCONSISTENT', 'EPS anchor needs evidence'],
        'research_gaps':[]}


class Endpoint:
    def __init__(self):self.calls=[];self.error=None;self.disabled=False
    def notify(self,key,text):
        self.calls.append((key,text))
        if self.error:raise self.error
        return {'status':'disabled' if self.disabled else 'provider_ack','verification':'provider_ack_only'}


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:');self.addCleanup(self.store.close);self.endpoint=Endpoint()

    def ready(self,symbol='GOOG'):
        from tests_invest.test_acceptance_core import approved_plan
        p=approved_plan(symbol);p['entry']['basis']='independent_valuation'
        p['research_assessment']={'status':'approved'}
        self.store.put('external_evidence','sealed:1',{'id':'sealed:1','content':'Official fixture source'},immutable=True)
        return self.rebind(p)

    def rebind(self,p):
        from tests_invest.plan_fixtures import seal_fixture_review
        p['review_issues']=[]
        seal_fixture_review(self.store,p,{'accepted':True,'issues':[],'checks':{'currency':True,'unit':True,'provenance':True,'adr':True}})
        return p

    def notify(self,plans,**kwargs):
        return notify_attention(self.store,self.endpoint,plans,now=NOW,when='fixture',
            enabled=kwargs.get('enabled',True),execute=kwargs.get('execute',True))

    def test_pending_conditional_and_no_buy_are_quiet_even_when_reasons_change(self):
        plans=[plan('GOOG'),plan('SOXX',True),dict(plan('CRCL'),research_assessment={'status':'approved_no_buy'})]
        for i in range(8):
            plans[0]['review_issues']=['EPS' if i%2 else 'growth rate']
            self.notify(plans)
        self.assertEqual(self.endpoint.calls,[])
        self.assertEqual(self.store.items('model_call'),[])

    def test_qualified_buy_sends_once_across_revision_and_wording_changes(self):
        p=self.ready();self.assertEqual(self.notify([p])['status'],'sent')
        for i in range(8):
            p.update(revision=i+3,thesis='wording '+str(i));self.rebind(p)
            self.assertEqual(self.notify([p])['status'],'unchanged')
        self.assertEqual(len(self.endpoint.calls),1)

    def test_material_price_change_notifies_only_the_changed_stock(self):
        a=self.ready('GOOG');b=self.ready('SOXX');self.notify([a,b])
        a['entry']['low']='95';a['entry']['execution_conditions'][0]['low']='95'
        a['entry_condition_review']['execution_conditions'][0]['low']='95';self.rebind(a)
        self.notify([a,b]);self.notify([a,b])
        self.assertEqual(len(self.endpoint.calls),2)
        self.assertIn('GOOG',self.endpoint.calls[-1][1]);self.assertNotIn('SOXX',self.endpoint.calls[-1][1])

    def test_equivalent_numeric_format_and_new_source_receipt_do_not_resend(self):
        p=self.ready();self.notify([p]);p['entry']['low']='90.0';self.rebind(p)
        self.assertEqual(self.notify([p])['status'],'unchanged')
        self.assertEqual(len(self.endpoint.calls),1)

    def test_unknown_notice_cannot_be_bypassed_even_by_silence_policy(self):
        p=self.ready();self.endpoint.error=UnknownOutcome('lost')
        with self.assertRaises(UnknownOutcome):self.notify([p])
        self.endpoint.error=None
        for candidate in ([],[plan()], [self.ready('NEW')]):
            with self.assertRaises(UnknownOutcome):self.notify(candidate)
        self.assertEqual(len(self.endpoint.calls),1)

    def test_receipt_recovery_commits_per_stock_state_without_resend(self):
        p=self.ready();self.endpoint.error=UnknownOutcome('lost')
        with self.assertRaises(UnknownOutcome):self.notify([p])
        current=self.store.get('research_attention_state','current')
        self.store.put('feishu_outbox','receipt',{'identity':'notice:'+current['id'],'status':'acknowledged','result':{'status':'provider_ack'}})
        self.endpoint.error=None
        self.assertEqual(self.notify([p])['status'],'unchanged')
        self.assertEqual(len(self.endpoint.calls),1)
        self.assertIsNotNone(self.store.get('research_attention_buy_state','GOOG'))

    def test_disabled_route_reuses_same_identity_after_configuration(self):
        p=self.ready();self.endpoint.disabled=True;self.notify([p]);self.endpoint.disabled=False
        self.notify([p]);self.notify([p]);self.assertEqual(len(self.endpoint.calls),2)
        self.assertEqual(self.endpoint.calls[0][0],self.endpoint.calls[1][0])

    def test_expired_or_unbound_plan_cannot_emit_buy_message(self):
        p=self.ready();p['price_basis_valid_until']='2026-10-26T00:00:00Z';self.rebind(p);self.notify([p])
        p['price_basis_valid_until']='2026-11-05T00:00:00Z';self.notify([p])
        self.assertEqual(self.endpoint.calls,[])

    def test_suppression_does_not_modify_plans(self):
        p=plan();before=copy.deepcopy(p);self.notify([p]);self.assertEqual(p,before)


if __name__=='__main__':unittest.main()
