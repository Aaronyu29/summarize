"""Real holdings missing from videos must still receive bounded research."""
import copy
import json
import unittest
from unittest.mock import patch

from rhino_invest.broker_snapshot import record_broker_snapshot
from rhino_invest.common import stable_id, validate_plan
from rhino_invest.decisions import _has_provenance
from rhino_invest.ledger import stage_entry, confirm_entry
from rhino_invest.model import ModelGateway
from rhino_invest.research import ingest_holdings, ingest_archives, review_plan, COMPILE, REVIEW
from rhino_invest.runner import Runner
from rhino_invest.config import Config
from tests_invest import test_research_evidence_contract as evidence_tests
from tests_invest.test_research_evidence_contract import IndependentModel
from tests_invest.test_research import NOW
from tests_invest.test_runner_delivery import Market, Feishu, Calendar


class HoldingResearchTests(unittest.TestCase):
    setUp = evidence_tests.EvidenceContractTests.setUp
    primary = evidence_tests.EvidenceContractTests.primary

    def reported(self, symbol='CRCL', value='128.58', *, now=NOW, identity='holding-screen'):
        return record_broker_snapshot(self.store, {'id': identity, 'account': 'real', 'broker': 'IBKR',
            'currency': 'USD', 'recorded_at': now, 'source': {'type': 'user_screenshot', 'reference': 'offline fixture'},
            'summary': {'cash': '23921.59', 'net_liquidation': '24536.28'},
            'holdings': [{'symbol': symbol, 'market_value': value}], 'supersedes_symbols': []})

    def run_research(self, model, data, *, now=NOW):
        return ingest_holdings(self.store, model=model, fundamentals={'CRCL': data}, now=now)

    def test_reported_unknown_quantity_starts_independent_research_without_video_or_orders(self):
        self.reported()
        data, _ = self.primary('CRCL')
        model = IndependentModel('CRCL')
        result = self.run_research(model, data)
        self.assertEqual(result['errors'], [])
        self.assertEqual(model.calls, ['compile_plan', 'review_plan'])
        plan = result['plans'][0]
        self.assertEqual(plan['status'], 'active', plan['review_issues'])
        self.assertTrue(_has_provenance(self.store, plan))
        self.assertEqual(plan['research_origin'], 'holding_fundamentals')
        self.assertIsNone(plan['source_published_at'])
        source = self.store.get('research_evidence', plan['research_snapshot_id'])
        self.assertEqual(source['origin']['kind'], 'reported_holding')
        self.assertNotIn('video_id', source)
        self.assertNotIn('quantity', source)
        self.assertNotIn('average_cost', source)
        for purpose, payload in model.payloads:
            self.assertEqual(payload['source']['source_type'], 'holding_fundamentals')
            self.assertNotIn('source_url', payload['source'])
            self.assertNotIn('transcript', payload['source'])
            self.assertEqual(payload['levels'], [])
            self.assertFalse(payload['policy']['cash_verified'])
            note = payload['policy']['execution_accounts']['real']['reported_holdings'][0]
            self.assertIsNone(note.get('quantity'))
        self.assertEqual(self.store.events('ledger'), [])
        self.assertEqual(self.store.items('recommendation'), [])
        self.assertEqual(self.store.items('reservation'), [])
        initial = self.store.get('plan_revision', stable_id(plan['id'], 1))
        self.assertEqual(initial['status'], 'needs_review')
        self.assertTrue(validate_plan(initial))

    def test_unchanged_material_facts_quote_clocks_and_report_value_do_not_repay(self):
        self.reported()
        data, _ = self.primary('CRCL')
        model = IndependentModel('CRCL')
        first = self.run_research(model, data)['plans'][0]
        data = dict(data, fetched_at='2026-10-24T15:00:00+00:00',
            primary_refresh_after='2026-10-25T15:00:00+00:00', reference_quote={'price': '999'})
        self.reported(value='130', now='2026-10-24T15:00:00+00:00', identity='new-screen')
        second = self.run_research(model, data, now='2026-10-24T15:00:00+00:00')['plans'][0]
        self.assertEqual(first, second)
        self.assertEqual(len(model.calls), 2)
        changed = copy.deepcopy(data)
        changed['primary_financials']['facts'][0]['value'] = '4.1'
        third = self.run_research(model, changed, now='2026-10-24T15:00:00+00:00')['plans'][0]
        self.assertEqual(third['revision'], second['revision'] + 1)
        self.assertEqual(model.calls, ['compile_plan', 'review_plan'] * 2)
        self.assertEqual(third['research_snapshot_id'], first['research_snapshot_id'])

    def test_missing_evidence_creates_visible_specific_gap_without_paid_model(self):
        self.reported()
        model = IndependentModel('CRCL')
        result = self.run_research(model, {})
        self.assertEqual(result['errors'], [])
        self.assertEqual(model.calls, [])
        plan = result['plans'][0]
        self.assertEqual(plan['status'], 'needs_review')
        codes = {item['code'] for item in plan['research_preflight']}
        self.assertTrue({'HOLDING_FINANCIAL_FACTS_MISSING', 'INSTRUMENT_UNIT_UNVERIFIED'} <= codes)
        self.assertEqual(plan['targets'], [])
        self.store.put('fundamental_cache', 'CRCL', {'data': {}})
        review_plan(self.store, plan, model, event_id='same-missing-facts', evidence={'fundamentals': {'CRCL': {}}}, now=NOW)
        self.assertEqual(model.calls, [])
        data, _ = self.primary('CRCL')
        repaired = self.run_research(model, data)['plans'][0]
        self.assertEqual(repaired['status'], 'active')
        self.assertEqual(model.calls, ['compile_plan', 'review_plan'])

    def test_existing_video_plan_is_not_recompiled_or_replaced_by_holding_bootstrap(self):
        self.reported('NVDA')
        data, _ = self.primary('NVDA')
        model = IndependentModel('NVDA')
        old = ingest_archives(self.store, self.db, model=model, fundamentals={'NVDA': data}, now=NOW)['plans'][0]
        before = len(model.calls)
        result = ingest_holdings(self.store, model=model, fundamentals={'NVDA': data}, now=NOW)
        self.assertEqual(result['skipped_existing'], ['NVDA'])
        self.assertEqual(result['plans'], [])
        self.assertEqual(self.store.get('plan', old['id']), old)
        self.assertEqual(len(model.calls), before)

    def test_only_positive_real_holdings_seed_research_no_cash_or_paper_guesses(self):
        stage_entry(self.store, {'id': 'paper', 'account': 'paper', 'type': 'opening_position',
            'symbol': 'TSM', 'quantity': '2', 'currency': 'USD', 'timestamp': NOW})
        confirm_entry(self.store, 'paper')
        self.reported(value='0')
        self.assertEqual(ingest_holdings(self.store, now=NOW)['symbols'], [])
        stage_entry(self.store, {'id': 'real', 'account': 'real', 'type': 'opening_position',
            'symbol': 'CRCL', 'quantity': '2', 'currency': 'USD', 'timestamp': NOW})
        confirm_entry(self.store, 'real')
        result = ingest_holdings(self.store, now=NOW)
        self.assertEqual(result['symbols'], ['CRCL'])
        plan = result['plans'][0]
        source = self.store.get('research_evidence', plan['research_snapshot_id'])
        self.assertEqual(source['origin']['kind'], 'confirmed_ledger_position')

    def test_unknown_and_budget_fences_leave_placeholder_visible_and_do_not_repeat(self):
        self.reported()
        data, _ = self.primary('CRCL')
        calls = []
        def unknown(body):
            calls.append(body)
            raise TimeoutError()
        config = {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 8, 'max_cost_usd_per_call': '.1', 'max_cost_usd_per_day': '2'}
        model = ModelGateway(self.store, config, unknown)
        first = self.run_research(model, data)
        self.assertEqual(first['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(first['plans'][0]['status'], 'needs_review')
        changed = ModelGateway(self.store, dict(config, configuration_revision=2), unknown)
        second = self.run_research(changed, dict(data, reference_quote={'price': '123'}), now='2026-10-24T15:00:00+00:00')
        self.assertEqual(second['errors'][0]['error'], 'UnknownModelOutcome')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.get('holding_research_state', 'CRCL')['error'], 'UnknownModelOutcome')

    def test_runner_without_archive_database_bootstraps_and_exports_typed_gap(self):
        self.reported()
        model = IndependentModel('CRCL')
        feishu = Feishu()
        runner = Runner(self.store, Config(symbols=(), archive_database=None), market=Market(),
            model=model, feishu=feishu, calendar=Calendar(), execute=True, clock=lambda: NOW)
        result = runner.run_once()
        self.assertEqual(result['holding_research']['symbols'], ['CRCL'])
        self.assertEqual(model.calls, [])
        rows = [fields for table, fields in feishu.calls if table == '投资计划']
        self.assertEqual(len(rows), 1)
        self.assertIn('公司财报事实', rows[0]['理由'])
        self.assertEqual(rows[0]['建议动作'], '等待审核')
        self.assertEqual(self.store.items('model_call'), [])

    def test_budget_deferral_keeps_visible_plan_and_resumes_next_day_from_cached_compile(self):
        self.reported()
        data, _ = self.primary('CRCL')
        calls, clock = [], [NOW]
        fixture = IndependentModel('CRCL')
        def transport(body):
            request = json.loads(body['messages'][1]['content'][0]['text'])
            calls.append(request['purpose'])
            output = fixture.run(request['purpose'], request['evidence'])
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}
        config = {'enabled': True, 'model': 'offline', 'base_url': 'https://offline.invalid',
            'max_calls_per_day': 1, 'max_cost_usd_per_call': '.1', 'max_cost_usd_per_day': '.1'}
        model = ModelGateway(self.store, config, transport, clock=lambda: clock[0])
        first = self.run_research(model, data)
        self.assertEqual(first['errors'][0]['error'], 'ModelBudgetExceeded')
        self.assertEqual(first['plans'][0]['status'], 'needs_review')
        self.assertEqual(calls, ['compile_plan'])
        second = self.run_research(model, data)
        self.assertEqual(second['errors'][0]['error'], 'ModelBudgetExceeded')
        self.assertEqual(calls, ['compile_plan'])
        clock[0] = '2026-10-24T15:00:00+00:00'
        third = self.run_research(model, data, now=clock[0])
        self.assertEqual(third['errors'], [])
        self.assertEqual(third['plans'][0]['status'], 'active')
        self.assertEqual(calls, ['compile_plan', 'review_plan'])

    def test_background_research_prioritizes_missing_real_holding_without_archive(self):
        self.reported()
        data, _ = self.primary('CRCL')
        model = IndependentModel('CRCL')
        runner = Runner(self.store, Config(symbols=(), archive_database=None), market=Market(),
            model=model, feishu=Feishu(), calendar=Calendar(), execute=True, clock=lambda: NOW)
        runner._research_cycle(['CRCL'], {'CRCL': data})
        summary = self.store.get('research_worker', 'latest')
        self.assertEqual(summary['holding_research']['symbols'], ['CRCL'])
        self.assertEqual(summary['holding_research']['plan_count'], 1)
        self.assertEqual(summary['holding_research']['needs_evidence_count'], 0)
        self.assertEqual(model.calls, ['compile_plan', 'review_plan'])
        self.assertEqual(self.store.events('ledger'), [])

    def test_quote_loop_does_not_demote_reviewed_holding_plan_while_model_runs_background(self):
        self.reported()
        data, _ = self.primary('CRCL')
        model = IndependentModel('CRCL')
        old = self.run_research(model, data)['plans'][0]
        result = ingest_holdings(self.store, model=None, fundamentals={'CRCL': dict(data, new_fact='fresh')}, now=NOW)
        self.assertEqual(result['plans'][0], old)
        self.assertEqual(self.store.get('plan', old['id']), old)
        self.assertEqual(model.calls, ['compile_plan', 'review_plan'])

    def test_background_seed_only_does_not_advance_pending_revision_during_paid_review(self):
        self.reported()
        pending = ingest_holdings(self.store, now=NOW)['plans'][0]
        data, _ = self.primary('CRCL')
        result = ingest_holdings(self.store, model=None, fundamentals={'CRCL': data}, seed_only=True, now=NOW)
        self.assertEqual(result['plans'][0], pending)
        self.assertEqual(len(self.store.items('plan_revision')), 1)

    def test_collector_upgrade_refreshes_crcl_immediately_without_refreshing_unrelated_issuer(self):
        self.reported()
        data, _ = self.primary('CRCL')
        for symbol in ('CRCL', 'GOOG'):
            self.store.put('fundamental_cache', symbol, {'period': '2026-10-23',
                'enrichment_revision': 'issuer-evidence-v1', 'data': {'symbol': symbol, 'financial_report': {},
                    'primary_refresh_after': '2026-10-25T15:00:00+00:00'}})
        calls = []
        def enrich(store, symbol, raw, **kwargs):
            calls.append(symbol)
            return copy.deepcopy(data)
        runner = Runner(self.store, Config(symbols=('GOOG',), archive_database=None), market=Market(),
            primary_enricher=enrich, feishu=Feishu(), calendar=Calendar(), execute=True, clock=lambda: NOW)
        runner.run_once()
        runner.run_once()
        self.assertEqual(calls, ['CRCL'])
        self.assertEqual(self.store.get('fundamental_cache', 'CRCL')['enrichment_revision'], 'issuer-evidence-v2')
        self.assertEqual(self.store.get('fundamental_cache', 'GOOG')['enrichment_revision'], 'issuer-evidence-v1')

    def test_prompts_authorize_labelled_scenarios_but_never_claim_future_fy_is_disclosed(self):
        self.assertIn('do not require further', COMPILE)
        self.assertIn('forward-PE', COMPILE)
        self.assertIn('reproducible bridge', REVIEW)
        self.assertIn('unfinished fiscal year', REVIEW)


if __name__ == '__main__':
    unittest.main()
