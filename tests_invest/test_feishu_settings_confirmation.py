import copy
import unittest

from rhino_invest.config import Config
from rhino_invest.feishu import Feishu
from rhino_invest.store import Store
from tests_invest.test_feishu_delivery import HTTP


class SettingsConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.http = HTTP()
        self.config = Config(feishu_app_id='fixture', feishu_app_secret='fixture', feishu_app_token='fixture')
        self.client = Feishu(self.store, self.config, http=self.http, execute=True)
        self.client.provision()
        self.store.put('plan', 'p', {'id': 'p'})

    def row(self, table, fields):
        row = {'record_id': 'provider-' + table, 'fields': fields}
        self.http.rows[self.client.tables()[table]].append(row)
        return row['fields']

    def plan(self, status='确认设置'):
        return self.row('投资计划', {'系统标识': 'p', '单次投入设置美元': '1000',
                                    '入场批次设置': 'entry', '设置确认状态': status})

    def test_plan_consumes_confirmation_and_preserves_raw_values(self):
        fields = self.plan()
        self.assertEqual(self.client.poll_inputs()['errors'], [])
        first_receipt = fields['设置回执']
        self.assertEqual(fields['设置确认状态'], '已应用')
        fields['单次投入设置美元'] = '2500'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')
        self.assertEqual(fields['单次投入设置美元'], '2500')
        fields['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '2500')
        self.assertEqual(fields['设置确认状态'], '已应用')
        self.assertNotEqual(fields['设置回执'], first_receipt)

    def test_identical_value_reconfirmation_is_consumed_and_new_ack_not_cached(self):
        fields = self.plan()
        self.client.poll_inputs()
        receipt = fields['设置回执']
        fields['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        self.assertEqual(fields['设置确认状态'], '已应用')
        self.assertNotEqual(fields['设置回执'], receipt)
        fields['单次投入设置美元'] = '3000'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')

    def test_account_confirmation_consumed_without_erasing_independent_controls(self):
        fields = self.row('账户与资金', {'系统标识': 'real', '账户': 'real', '回撤硬限制设置': '0.2',
            '持仓核对设置': '已核对（可空仓）', '设置确认状态': '确认设置'})
        self.client.poll_inputs()
        self.assertEqual(fields['设置确认状态'], '已应用')
        fields.update({'回撤硬限制设置': '0.3', '持仓核对设置': '未核对'})
        self.client.poll_inputs()
        self.assertEqual(self.store.get('account_control', 'real')['drawdown_limit'], '0.2')
        self.assertTrue(self.store.get('account_control', 'real')['holdings_complete'])
        fields['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('account_control', 'real')['drawdown_limit'], '0.3')
        self.assertFalse(self.store.get('account_control', 'real')['holdings_complete'])
        self.assertEqual(fields['持仓核对设置'], '未核对')

    def test_edited_staged_payload_requires_new_staging_before_confirmation(self):
        fields = self.plan('待暂存')
        self.client.poll_inputs()
        fields.update({'单次投入设置美元': '2000', '设置确认状态': '确认设置'})
        self.assertTrue(self.client.poll_inputs()['errors'])
        self.assertIsNone(self.store.get('plan_control', 'p'))
        fields['设置确认状态'] = '待暂存'
        self.client.poll_inputs()
        fields['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '2000')

    def test_unknown_ack_blocks_new_values_and_staging_cannot_bypass_outbox(self):
        fields = self.plan()
        original_request, attempts = self.http.request, []
        def fail_ack(method, url, **kwargs):
            if method == 'PUT' and '设置回执' in (kwargs.get('json') or {}).get('fields', {}):
                attempts.append(copy.deepcopy(kwargs['json']))
                raise TimeoutError('unknown fixture')
            return original_request(method, url, **kwargs)
        self.http.request = fail_ack
        self.assertTrue(self.client.poll_inputs()['errors'])
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')
        fields['单次投入设置美元'] = '2500'
        restarted = Feishu(self.store, self.config, http=self.http, execute=True)
        self.assertTrue(restarted.poll_inputs()['errors'])
        fields['设置确认状态'] = '待暂存'
        restarted.poll_inputs()
        fields['设置确认状态'] = '确认设置'
        self.assertTrue(restarted.poll_inputs()['errors'])
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')
        self.assertEqual(len(attempts), 1)

    def test_recovered_old_ack_never_confirms_new_values_from_stale_outer_snapshot(self):
        fields = self.plan()
        original_request = self.http.request
        def fail_ack(method, url, **kwargs):
            if method == 'PUT' and '设置回执' in (kwargs.get('json') or {}).get('fields', {}):
                raise TimeoutError('unknown fixture')
            return original_request(method, url, **kwargs)
        self.http.request = fail_ack
        self.client.poll_inputs()
        consumed = self.store.items('feishu_setting_confirmation')[0][1]
        fields['单次投入设置美元'] = '2500'
        original_records, plan_reads = self.client.records, []
        def concurrent_ack(table):
            if table == '投资计划':
                plan_reads.append(table)
                if len(plan_reads) == 2:
                    fields.update(consumed['body']['fields'])  # Old ack commits after the outer snapshot.
            return original_records(table)
        self.http.request = original_request
        self.client.records = concurrent_ack
        self.client.poll_inputs()
        self.assertEqual(fields['设置确认状态'], '已应用')
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '1000')
        fields['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        self.assertEqual(self.store.get('plan_control', 'p')['amount_usd'], '2500')

    def test_confirmed_trade_remains_immutable_when_input_value_is_edited(self):
        fields = self.row('成交录入', {'系统标识': 'cash1', '账户': 'real', '类型': '期初现金',
            '金额美元': '5000', '币种': 'USD', '成交时间': '2026-10-23T00:00:00Z', '确认状态': '确认'})
        self.client.poll_inputs()
        fields['金额美元'] = '9000'
        self.assertTrue(self.client.poll_inputs()['errors'])
        self.assertEqual(len(self.store.events('ledger')), 1)
        self.assertEqual(self.store.events('ledger')[0]['amount'], '5000')
        self.assertEqual(fields['金额美元'], '9000')

    def test_reusing_cash_input_row_with_new_id_cannot_reuse_old_confirmation(self):
        fields = self.row('账户与资金', {'系统标识': 'real', '账户': 'real', '录入标识': 'baseline',
            '输入类型': '期初现金', '录入金额美元': '5000', '币种': 'USD',
            '录入时间': '2026-10-23T00:00:00Z', '确认状态': '确认'})
        self.client.poll_inputs()
        fields.update({'录入标识': 'deposit-new', '输入类型': '入金', '录入金额美元': '2000'})
        self.assertTrue(self.client.poll_inputs()['errors'])
        self.assertEqual(len(self.store.events('ledger')), 1)
        fields['确认状态'] = '待暂存'
        self.client.poll_inputs()
        fields['确认状态'] = '确认'
        self.client.poll_inputs()
        self.assertEqual(len(self.store.events('ledger')), 2)
        self.assertEqual(self.store.events('ledger')[-1]['id'], 'deposit-new')


if __name__ == '__main__':
    unittest.main()
