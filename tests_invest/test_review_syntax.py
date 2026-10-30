import json
import unittest
from rhino_invest.model import ModelGateway,ModelOutputInvalid,recover_review_array_close
from rhino_invest.store import Store


class ReviewSyntaxTests(unittest.TestCase):
    def text(self,accepted=True):
        value={'accepted':accepted,'issues':[] if accepted else ['incorrect facts'],
            'checks':dict.fromkeys(['provenance','currency','unit','adr'],True),
            'position_basis_accepted':None,'no_buy_reviewed':True,
            'findings':{'notes':['literal } and [ remain unchanged','do not buy']},'horizon_months':18}
        raw=json.dumps(value,separators=(',',':'))
        return value,raw.replace('"do not buy"]','"do not buy"')

    def test_only_missing_array_closer_is_inserted_with_all_values_preserved(self):
        for accepted in [True,False]:
            expected,raw=self.text(accepted)
            output,edit=recover_review_array_close(raw);offset=edit['offset']
            self.assertEqual(output,expected)
            self.assertEqual(edit['operation'],'insert')
            self.assertEqual(json.loads(raw[:offset]+']'+raw[offset:]),expected)

    def test_root_notes_wrong_object_closer_preserves_following_root_fields(self):
        expected={'accepted':True,'issues':[],'checks':dict.fromkeys(['provenance','currency','unit','adr'],True),
                  'notes':['do not buy'],'horizon_months':18,'currency':'USD'}
        raw=json.dumps(expected,separators=(',',':')).replace('"do not buy"]','"do not buy"}')
        result,edit=recover_review_array_close(raw)
        self.assertEqual(result,expected);self.assertEqual(edit['operation'],'replace')
        self.assertEqual(edit['before'],'}');self.assertEqual(edit['after'],']')

    def test_extra_object_closer_at_array_boundary_is_deleted_without_changing_decision(self):
        for accepted in (True,False):
            expected={'accepted':accepted,'issues':[] if accepted else ['wrong facts'],
                'checks':dict.fromkeys(['provenance','currency','unit','adr'],True),'notes':['literal } is retained']}
            raw=json.dumps(expected,separators=(',',':')).replace('"literal } is retained"]','"literal } is retained"}]')
            output,edit=recover_review_array_close(raw)
            self.assertEqual(output,expected);self.assertEqual(edit['operation'],'delete')

    def test_duplicates_missing_values_and_incomplete_outputs_stay_blocked(self):
        _,raw=self.text()
        for bad in [raw.replace('"accepted":true','"accepted":true,"accepted":false'),
                    raw[:-2],raw.replace('18','NaN'),raw.replace('18','1e999'),raw.replace('"horizon_months":18','"horizon_months":')]:
            self.assertIsNone(recover_review_array_close(bad))

    def test_completed_review_recovery_replays_original_receipt_without_paid_retry(self):
        expected,raw=self.text();calls=[]
        def transport(body):
            calls.append(body)
            return {'choices':[{'finish_reason':'stop','message':{'content':raw}}]}
        config={'enabled':True,'model':'fixture','base_url':'https://offline.invalid',
            'max_calls_per_day':2,'max_cost_usd_per_call':'0.1','max_cost_usd_per_day':'1',
            'reuse_evidence_cycles':True}
        with Store(':memory:') as store:
            gateway=ModelGateway(store,config,transport)
            first=gateway.run('review_plan',{'id':'same-evidence'})
            self.assertEqual(first,expected)
            self.assertEqual(gateway.run('review_plan',{'id':'same-evidence'}),expected)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(store.items('model_call')),1)
            self.assertEqual(len(store.items('model_receipt')),1)
            self.assertTrue(store.items('model_syntax_recovery')[0][1]['original_review_decision_preserved'])

    def test_truncated_receipt_is_not_recovered_as_an_accepted_review(self):
        _,raw=self.text()
        config={'enabled':True,'model':'fixture','base_url':'https://offline.invalid',
            'max_calls_per_day':2,'max_cost_usd_per_call':'0.1','max_cost_usd_per_day':'1',
            'reuse_evidence_cycles':True}
        with Store(':memory:') as store:
            gateway=ModelGateway(store,config,lambda body:{'choices':[{'finish_reason':'length','message':{'content':raw}}]})
            with self.assertRaises(ModelOutputInvalid):gateway.run('review_plan',{'id':'same-evidence'})
            self.assertEqual(store.items('model_syntax_recovery'),[])


if __name__=='__main__':unittest.main()
