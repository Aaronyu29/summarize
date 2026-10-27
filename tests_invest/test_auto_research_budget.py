import json,tempfile,unittest
from pathlib import Path
from decimal import Decimal
from rhino_invest.store import Store
from rhino_invest.budget import authorized_auto_limits,load_limits,write_limits,ModelBudgetConfigError

class AutoBudgetTests(unittest.TestCase):
    def test_only_open_goog_followup_expands_with_hard_cap_and_no_usage_reset(self):
        with tempfile.TemporaryDirectory(dir='research-data') as tmp,Store(':memory:') as store:
            budget=(Path(tmp)/'budget.json').resolve();policy=(Path(tmp)/'policy.json').resolve()
            write_limits(str(budget),{'max_calls_per_day':90,'max_cost_usd_per_day':'9'})
            value={'enabled':True,'symbols':['GOOG'],'step_calls':30,'step_cost_usd':'3','max_calls':180,'max_cost_usd':'18',
                   'valid_until':'2026-11-03T00:00:00Z','baseline':{'max_calls_per_day':60,'max_cost_usd_per_day':'6'}}
            policy.write_text(json.dumps(value));config={'budget_file':str(budget),'auto_budget_policy_file':str(policy)}
            def call(symbol='GOOG',n=90,day='2026-10-27T12:00:00Z'):
                return authorized_auto_limits(store,config,load_limits(config),now=day,calls_used=n,
                    reserved_used=Decimal(n)/10,ceiling=Decimal('.1'),purpose='compile_plan',symbol=symbol)
            self.assertEqual(call()['max_calls_per_day'],90)
            store.put('research_followup','GOOG',{'status':'researching'})
            self.assertEqual(call('TSM')['max_calls_per_day'],90)
            for used,wanted in ((90,120),(120,150),(150,180),(180,180)):
                self.assertEqual(call(n=used)['max_calls_per_day'],wanted)
            self.assertEqual(store.items('model_call'),[])
            self.assertTrue(all(v['usage_reset'] is False for _,v in store.items('model_budget_adjustment')))
            self.assertEqual(call(n=0,day='2026-10-28T00:00:01Z')['max_calls_per_day'],60)
            value['max_calls']=100000;policy.write_text(json.dumps(value))
            with self.assertRaises(ModelBudgetConfigError):call()

if __name__=='__main__':unittest.main()
