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
        self.store=Store(':memory:');self.addCleanup(self.store.close)
        self.endpoint=Endpoint()

    def notify(self,plans,**kwargs):
        return notify_attention(self.store,self.endpoint,plans,now=NOW,when='10月27日 14:30（北京时间）',
            enabled=kwargs.get('enabled',True),execute=kwargs.get('execute',True))

    def test_consolidated_five_plans_distinguish_rejected_and_conditional(self):
        plans=[plan(s) for s in ('CRCL','TSM','GOOG')]+[plan(s,True) for s in ('SOXX','GOOGL')]
        before=copy.deepcopy(plans)
        self.assertEqual(self.notify(plans)['status'],'sent')
        self.assertEqual(len(self.endpoint.calls),1)
        text=self.endpoint.calls[0][1]
        self.assertIn('3 份未通过或待补证据；2 份估值已审核',text)
        self.assertIn('TSM｜已自动审核·未通过',text)
        self.assertIn('SOXX｜估值已审核·入场条件待满足',text)
        self.assertIn('每股盈利',text);self.assertIn('止盈或止损',text)
        self.assertNotIn('THESIS_INCONSISTENT',text)
        self.assertIn('证据更新后继续自动复核',text)
        self.assertEqual(plans,before)
        self.assertEqual(self.store.items('model_call'),[])

    def test_unchanged_is_quiet_across_revision_time_and_restart(self):
        item=plan();self.notify([item])
        for i in range(4):
            item.update(revision=i+2,updated_at='later',created_at='later')
            self.assertEqual(self.notify([item])['status'],'unchanged')
        self.assertEqual(len(self.endpoint.calls),1)

    def test_changed_problem_and_resolution_each_get_one_transition_notice(self):
        item=plan();self.notify([item])
        item['review_issues']=['growth rate is not evidenced']
        self.notify([item]);self.notify([item])
        item['status']='active';self.notify([item]);self.notify([item])
        self.assertEqual(len(self.endpoint.calls),3)
        self.assertEqual(len({key for key,text in self.endpoint.calls}),3)
        self.assertIn('阻塞已解除',self.endpoint.calls[-1][1])
        item['status']='needs_review';self.notify([item]);item['status']='active';self.notify([item])
        self.assertEqual(len(self.endpoint.calls),5)

    def test_no_pending_initial_state_or_disabled_notifications_do_not_send(self):
        self.assertEqual(self.notify([])['status'],'idle')
        self.notify([plan()],enabled=False);self.notify([plan()],execute=False)
        self.assertEqual(self.endpoint.calls,[])
        self.assertEqual(self.store.items('research_attention_notice'),[])
        self.notify([plan()]);self.assertEqual(len(self.endpoint.calls),1)

    def test_unconfigured_route_can_send_once_after_configuration(self):
        self.endpoint.disabled=True
        self.assertEqual(self.notify([plan()])['status'],'disabled')
        self.endpoint.disabled=False
        self.assertEqual(self.notify([plan()])['status'],'sent')
        self.assertEqual(self.endpoint.calls[0][0],self.endpoint.calls[1][0])
        self.assertEqual(self.notify([plan()])['status'],'unchanged')

    def test_unknown_send_cannot_be_bypassed_by_new_signature_or_restart(self):
        self.endpoint.error=UnknownOutcome('unknown')
        with self.assertRaises(UnknownOutcome):self.notify([plan()])
        self.endpoint.error=None
        for plans in ([plan()],[plan('NEW')],[]):
            with self.assertRaises(UnknownOutcome):self.notify(plans)
        self.assertEqual(len(self.endpoint.calls),1)

    def test_provider_receipt_recovers_local_unknown_marker_without_resending(self):
        self.endpoint.error=UnknownOutcome('connection lost')
        with self.assertRaises(UnknownOutcome):self.notify([plan()])
        state=self.store.get('research_attention_state','current')
        self.store.put('feishu_outbox','receipt',{'identity':'notice:'+state['id'],'status':'acknowledged',
            'result':{'status':'provider_ack','verification':'provider_ack_only'}})
        self.endpoint.error=None
        self.assertEqual(self.notify([plan()])['status'],'unchanged')
        self.assertEqual(len(self.endpoint.calls),1)
        self.assertEqual(self.store.get('research_attention_state','current')['receipt']['verification'],'provider_ack_only')

    def test_projection_and_preflight_do_not_claim_unperformed_independent_review(self):
        rejected=plan()
        self.assertEqual(plan_row(rejected)['状态'],'已自动审核·未通过')
        self.assertEqual(plan_row(rejected)['建议动作'],'等待补证据后复核')
        preflight=dict(rejected,research_preflight=[{'detail':'primary evidence missing'}])
        self.assertEqual(attention_items([preflight])[0]['status'],'资料校验未通过·待补证据')
        self.assertEqual(attention_items([plan(conditional=True)])[0]['status'],'估值已审核·入场条件待满足')
        self.assertEqual(attention_items([dict(rejected,status='invalidated')]),[])


if __name__=='__main__':unittest.main()
