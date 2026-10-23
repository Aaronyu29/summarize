import copy
import json
import unittest
import base64
import hashlib
import hmac
from unittest.mock import patch

from rhino_invest.config import Config
from rhino_invest.feishu import Feishu, SCHEMA, UnknownOutcome, FeishuError
from rhino_invest.store import Store
from rhino_invest.ledger import portfolio


class Response:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def json(self):
        return self.data


class HTTP:
    """Stateful server fixture: commits can succeed while a response is lost."""
    def __init__(self):
        self.tables, self.rows, self.fields = [], {}, {}
        self.calls, self.fail_after_commit = [], False

    def request(self, method, url, json=None, params=None, headers=None, timeout=None):
        self.calls.append((method, url, copy.deepcopy(json)))
        if url.endswith('/auth/v3/tenant_access_token/internal'):
            return Response({'code': 0, 'tenant_access_token': 'tenant-secret', 'expire': 3600})
        if url.endswith('/tables'):
            if method == 'GET':
                return Response({'data': {'items': self.tables}})
            table = json['table']
            table_id = 't' + str(len(self.tables))
            self.tables.append({'name': table['name'], 'table_id': table_id})
            self.rows[table_id] = []
            self.fields[table_id] = [{'field_id': str(i), **f} for i, f in enumerate(table['fields'])]
            return Response({'data': {'table_id': table_id}})
        if '/tables/' in url:
            suffix = url.split('/tables/', 1)[1]
            table_id, tail = suffix.split('/', 1)
            if tail == 'fields':
                return Response({'data': {'items': self.fields[table_id]}})
            if tail == 'records' and method == 'GET':
                return Response({'data': {'items': copy.deepcopy(self.rows[table_id])}})
            if tail == 'records' and method == 'POST':
                record = {'record_id': 'r' + str(len(self.rows[table_id])), 'fields': dict(json['fields'])}
                self.rows[table_id].append(record)
                if self.fail_after_commit:
                    self.fail_after_commit = False
                    raise TimeoutError('request URL included a secret and must never be printed')
                return Response({'data': {'record': record}})
            if tail.startswith('records/') and method == 'PUT':
                record_id = tail.split('/')[-1]
                row = next(r for r in self.rows[table_id] if r['record_id'] == record_id)
                row['fields'].update(json['fields'])
                return Response({'data': {'record': row}})
        return Response({'data': {'app': {'app_token': 'app'}}})


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.http = HTTP()
        self.config = Config(feishu_app_id='id', feishu_app_secret='secret', feishu_app_token='app')
        self.client = Feishu(self.store, self.config, http=self.http, execute=True)
    def tearDown(self):
        self.store.close()

    def test_provision_readback_and_rerun_are_idempotent(self):
        result = self.client.provision()
        self.assertEqual(result['status'], 'verified')
        writes = len([c for c in self.http.calls if c[0] == 'POST' and '/tables' in c[1]])
        self.client.provision()
        self.assertEqual(writes, len([c for c in self.http.calls if c[0] == 'POST' and '/tables' in c[1]]))
        self.assertEqual(len(self.http.tables), len(SCHEMA))
        self.assertIn('成交价美元', SCHEMA['成交录入'])

    def test_dry_run_has_no_http_even_with_credentials(self):
        preview = Feishu(self.store, self.config, http=self.http)
        self.assertEqual(preview.provision()['status'], 'dry_run')
        preview.upsert('复盘报告', {'系统标识': 'a', '正文': 'test'})
        self.assertEqual(self.http.calls, [])

    def test_lost_record_response_is_read_back_without_duplicate_post(self):
        self.client.provision()
        self.http.fail_after_commit = True
        fields = {'系统标识': 'report1', '正文': 'a'}
        with self.assertRaises(UnknownOutcome):
            self.client.upsert('复盘报告', fields)
        posts = len([c for c in self.http.calls if c[0] == 'POST' and c[1].endswith('/records')])
        result = self.client.upsert('复盘报告', fields)
        self.assertEqual(result['record']['fields']['正文'], 'a')
        self.assertEqual(posts, len([c for c in self.http.calls if c[0] == 'POST' and c[1].endswith('/records')]))
        self.assertTrue(all(v['status'] == 'delivered' for _, v in self.store.items('feishu_outbox')))

    def test_unknown_write_without_matching_readback_fences_changed_payload(self):
        self.client.provision()
        self.http.fail_after_commit = True
        with self.assertRaises(UnknownOutcome):
            self.client.upsert('复盘报告', {'系统标识': 'report1', '正文': 'a'})
        table = self.client.tables()['复盘报告']
        self.http.rows[table][0]['fields']['正文'] = 'externally changed'
        posts = len([c for c in self.http.calls if c[0] == 'POST' and c[1].endswith('/records')])
        with self.assertRaises(UnknownOutcome):
            self.client.upsert('复盘报告', {'系统标识': 'report1', '正文': 'new payload'})
        self.assertEqual(posts, len([c for c in self.http.calls if c[0] == 'POST' and c[1].endswith('/records')]))

    def test_poll_only_explicit_confirmation_enters_real_ledger(self):
        self.client.provision()
        table = self.client.tables()['成交录入']
        self.http.rows[table].append({'record_id': 'manual', 'fields': {
            '系统标识': 'opening1', '账户': 'real', '类型': '期初现金', '金额美元': '15000',
            '币种': 'USD', '成交时间': '2026-10-23T13:30:00Z', '确认状态': '待暂存'}})
        self.client.poll_inputs()
        self.assertEqual(len(self.store.events('ledger')), 0)
        self.assertIsNone(portfolio(self.store)['cash'])
        self.http.rows[table][0]['fields']['确认状态'] = '确认'
        result = self.client.poll_inputs()
        self.assertEqual(result['confirmed'], ['opening1'])
        self.client.poll_inputs()
        self.assertEqual(len(self.store.events('ledger')), 1)
        self.assertEqual(portfolio(self.store, now='2026-10-23T14:00:00Z')['cash'], '15000')

    def test_reconcile_preview_does_not_resolve_durable_unknown(self):
        self.client.provision()
        self.http.fail_after_commit = True
        with self.assertRaises(UnknownOutcome):
            self.client.upsert('复盘报告', {'系统标识': 'r', '正文': 'x'})
        dry = Feishu(self.store, self.config, http=self.http, execute=False)
        dry.reconcile()
        self.assertTrue(any(v['status'] == 'unknown' for _, v in self.store.items('feishu_outbox')))
        self.assertEqual(len(self.client.reconcile()['resolved']), 1)

    def test_explicit_holdings_confirmation_merges_existing_drawdown_control(self):
        self.client.provision()
        self.store.put('account_control', 'real', {'confirmed': True, 'drawdown_limit': '0.2'})
        table = self.client.tables()['账户与资金']
        self.http.rows[table].append({'record_id': 'account-real', 'fields': {
            '系统标识': 'real', '账户': 'real', '持仓核对设置': '已核对（可空仓）', '设置确认状态': '待暂存'}})
        self.client.poll_inputs()
        self.assertNotIn('holdings_complete', self.store.get('account_control', 'real'))
        self.assertFalse(self.store.get('account_control_staged', 'real')['confirmed'])
        self.http.rows[table][0]['fields']['设置确认状态'] = '确认设置'
        self.client.poll_inputs()
        control = self.store.get('account_control', 'real')
        self.assertTrue(control['holdings_complete'])
        self.assertEqual(control['drawdown_limit'], '0.2')
        self.http.rows[table][0]['fields'].update({'持仓核对设置': '', '回撤硬限制设置': '0.1', '设置确认状态': '确认设置'})
        self.client.poll_inputs()
        self.assertTrue(self.store.get('account_control', 'real')['holdings_complete'])
        self.http.rows[table][0]['fields'].update({'持仓核对设置': '未核对', '回撤硬限制设置': '', '设置确认状态': '确认设置'})
        self.client.poll_inputs()
        self.assertFalse(self.store.get('account_control', 'real')['holdings_complete'])
        self.assertEqual(self.store.get('account_control', 'real')['drawdown_limit'], '0.1')


class WebhookHTTP:
    def __init__(self, *, response=None, failure=None, status=200):
        self.calls = []
        self.response = {'code': 0} if response is None else response
        self.failure, self.status = failure, status
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        response = Response(self.response)
        response.status_code = self.status
        return response


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.config = Config(feishu_webhook_url='https://open.feishu.cn/open-apis/bot/v2/hook/private-fixture-token')
        self.addCleanup(self.store.close)

    def test_preview_needs_no_app_credentials_and_never_exposes_target(self):
        http = WebhookHTTP()
        client = Feishu(self.store, self.config, http=http)
        result = client.notify('first', '投资预览')
        self.assertEqual(result['status'], 'dry_run')
        self.assertFalse(result['readback_verified'])
        self.assertEqual(http.calls, [])
        self.assertNotIn('private-fixture-token', json.dumps(result))

    def test_provider_ack_is_not_readback_and_is_idempotent_across_restart(self):
        http = WebhookHTTP(response={'StatusCode': 0, 'StatusMessage': 'success'})
        client = Feishu(self.store, self.config, http=http, execute=True)
        receipt = client.notify('first', '投资状态')
        self.assertEqual(receipt['status'], 'provider_ack')
        self.assertEqual(receipt['verification'], 'provider_ack_only')
        self.assertFalse(receipt['readback_verified'])
        self.assertFalse(http.calls[0][2]['allow_redirects'])
        restarted = Feishu(self.store, self.config, http=http, execute=True)
        self.assertEqual(restarted.notify('first', '修改正文也不能重复发送同一回执'), receipt)
        self.assertEqual(len(http.calls), 1)
        operation = self.store.items('feishu_outbox')[0][1]
        self.assertEqual(operation['status'], 'acknowledged')
        self.assertNotIn('private-fixture-token', json.dumps(operation))

    def test_optional_signature_is_correct_but_secret_and_signature_are_not_stored(self):
        self.config.feishu_webhook_secret = 'fixture-signing-secret'
        http = WebhookHTTP()
        with patch('rhino_invest.feishu.time.time', return_value=1700000000):
            Feishu(self.store, self.config, http=http, execute=True).notify('signed', '投资签名')
        payload = http.calls[0][2]['json']
        expected = base64.b64encode(hmac.new(b'1700000000\nfixture-signing-secret', digestmod=hashlib.sha256).digest()).decode()
        self.assertEqual(payload['sign'], expected)
        stored = json.dumps(self.store.items('feishu_outbox'))
        self.assertNotIn('fixture-signing-secret', stored)
        self.assertNotIn(expected, stored)

    def test_timeout_fence_survives_restart_changed_body_and_route_switch(self):
        http = WebhookHTTP(failure=TimeoutError('secret-bearing URL must be omitted'))
        client = Feishu(self.store, self.config, http=http, execute=True)
        with self.assertRaises(UnknownOutcome) as caught:
            client.notify('uncertain', '投资状态')
        self.assertNotIn('secret-bearing', str(caught.exception))
        self.config.feishu_chat_id = 'new-chat'
        restarted = Feishu(self.store, self.config, http=http, execute=True)
        with self.assertRaises(UnknownOutcome):
            restarted.notify('uncertain', 'different content')
        reconciled = restarted.reconcile()
        self.assertEqual(len(reconciled['unknown']), 1)
        self.assertEqual(len(http.calls), 1)

    def test_unknown_chat_cannot_be_resent_via_new_webhook(self):
        self.store.put('feishu_outbox', 'old', {'id': 'old', 'identity': 'notice:uncertain', 'status': 'unknown'})
        http = WebhookHTTP()
        with self.assertRaises(UnknownOutcome):
            Feishu(self.store, self.config, http=http, execute=True).notify('uncertain', '投资状态')
        self.assertEqual(http.calls, [])

    def test_chat_route_failure_does_not_automatically_send_webhook(self):
        self.config.feishu_chat_id = 'configured-chat'
        http = WebhookHTTP()
        with self.assertRaises(FeishuError):
            Feishu(self.store, self.config, http=http, execute=True).notify('chat-first', '投资状态')
        self.assertEqual(http.calls, [])  # Missing app credentials do not authorize route fallback.

    def test_malformed_and_server_response_are_unknown_and_rejection_is_explicit(self):
        for response, status in (({}, 200), ({'code': 0}, 503), ({'code': 0}, 302)):
            with self.subTest(status=status), Store(':memory:') as store:
                http = WebhookHTTP(response=response, status=status)
                with self.assertRaises(UnknownOutcome):
                    Feishu(store, self.config, http=http, execute=True).notify('failed', '投资状态')
                self.assertEqual(store.items('feishu_outbox')[0][1]['status'], 'unknown')
        with Store(':memory:') as store:
            with self.assertRaises(FeishuError):
                Feishu(store, self.config, http=WebhookHTTP(response={'code': 19024}), execute=True).notify('rejected', '投资状态')
            self.assertEqual(store.items('feishu_outbox')[0][1]['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
