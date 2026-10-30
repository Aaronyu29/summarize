import copy
import unittest
from unittest.mock import patch

from rhino_invest.broker_snapshot import record_broker_snapshot, latest_broker_snapshot, with_broker_snapshot
from rhino_invest.ledger import stage_entry, confirm_entry, correct_entry, portfolio
from rhino_invest.store import Store


BEFORE = '2026-10-24T00:00:00+00:00'
RECEIPT = '2026-10-24T01:00:00+00:00'
AFTER = '2026-10-24T02:00:00+00:00'


def screen():
    return {'id': 'ibkr:user-screenshot:20261024-01', 'account': 'real', 'broker': 'IBKR',
        'currency': 'USD', 'recorded_at': RECEIPT, 'as_of': None,
        'account_identifier': 'UTEST0001',
        'source': {'type': 'user_screenshot', 'reference': 'user-provided IBKR account and holdings screenshots'},
        'summary': {'cash': '23921.59', 'net_liquidation': '24536.28', 'unrealized_pnl': '12.77',
            'realized_pnl': '0', 'maintenance_margin': '272.50', 'excess_liquidity': '24258.03',
            'buying_power': '96974.43', 'dividends': '0', 'account_display_change': '-5.37',
            'all_time_display_change': '24535.76'},
        'holdings': [{'symbol': 'GOOG', 'market_value': '480.55', 'last_price': '318.10',
            'change_percent': '-0.83', 'quantity': None, 'cost_basis': None, 'first_fill_at': None},
            {'symbol': 'CRCL', 'market_value': '128.58', 'last_price': '84.92',
            'change_percent': '-1.96', 'quantity': None, 'cost_basis': None, 'first_fill_at': None}],
        'supersedes_symbols': ['GOOGL'],
        'orders': [{'symbol': 'GOOG', 'quantity': '10.7972', 'limit_price': '274.33',
            'status': 'unknown', 'source': 'IBKR dashboard notice', 'text': '价格调整提示，未确认挂单有效或成交'}]}


def opening(store):
    stage_entry(store, {'id': 'user-opening-20000', 'account': 'real', 'type': 'opening_cash',
        'timestamp': BEFORE, 'currency': 'USD', 'amount': '20000', 'source': 'user_statement'})
    with patch('rhino_invest.ledger.utc_now', return_value=BEFORE):
        confirm_entry(store, 'user-opening-20000')


class BrokerSnapshotTests(unittest.TestCase):
    def test_reported_values_overlay_without_inventing_positions_trades_or_pnl(self):
        with Store(':memory:') as store:
            opening(store)
            raw = screen()
            before = copy.deepcopy(raw)
            saved = record_broker_snapshot(store, raw)
            self.assertEqual(raw, before)
            record_broker_snapshot(store, raw)
            self.assertEqual(len(store.items('broker_snapshot')), 1)
            ledger = portfolio(store, now=AFTER)
            result = with_broker_snapshot(store, ledger)
            self.assertEqual(result['cash'], '23921.59')
            self.assertEqual(result['remaining_cash'], '23921.59')
            self.assertEqual(result['ledger_cash'], '20000')
            self.assertEqual(result['total_equity'], '24536.28')
            self.assertEqual(result['total_equity_source'], 'broker_reported_net_liquidation')
            self.assertIsNone(result['broker_snapshot_as_of'])
            self.assertEqual(result['broker_snapshot_recorded_at'], RECEIPT)
            self.assertEqual(result['target_total_usd'], '20000')
            self.assertEqual(result['broker_reported_buying_power'], '96974.43')
            self.assertFalse(result['broker_buying_power_is_cash'])
            self.assertEqual(result['broker_reported_unrealized_pnl'], '12.77')
            self.assertEqual(result['broker_reported_realized_pnl'], '0')
            self.assertEqual(result['realized_pnl_net'], ledger['realized_pnl_net'])
            self.assertEqual(result['positions'], [])
            self.assertFalse(result['holdings_complete'])
            self.assertFalse(result['cash_complete'])
            self.assertFalse(result['cost_complete'])
            self.assertFalse(result['fees_complete'])
            self.assertFalse(result['valuation_complete'])
            self.assertFalse(result['broker_reconciled'])
            self.assertIsNone(result['available_cash_after_reservations'])
            self.assertEqual(result['broker_reported_holdings_market_value_sum'], '609.13')
            self.assertEqual(result['broker_reported_non_cash_value'], '614.69')
            self.assertEqual(result['broker_reported_unexplained_non_cash_value'], '5.56')
            self.assertEqual(result['broker_supersedes_symbols'], ['GOOGL'])
            self.assertEqual([h['symbol'] for h in result['broker_reported_holdings']], ['GOOG', 'CRCL'])
            self.assertTrue(all(h['quantity'] is None and h['quantity_inferred'] is False for h in result['broker_reported_holdings']))
            self.assertEqual(len(store.events('ledger')), 1)
            self.assertEqual(store.get('ledger_staged', 'user-opening-20000')['amount'], '20000')
            self.assertEqual(saved['summary']['all_time_display_change'], '24535.76')
            self.assertEqual(ledger['cash'], '20000')
            self.assertEqual(with_broker_snapshot(store, result), result)

    def test_order_hint_never_becomes_fill_or_position(self):
        with Store(':memory:') as store:
            record_broker_snapshot(store, screen())
            result = with_broker_snapshot(store, portfolio(store, now=AFTER))
            order = result['broker_order_hints'][0]
            self.assertEqual(order['quantity'], '10.7972')
            self.assertEqual(order['limit_price'], '274.33')
            self.assertEqual(order['execution_status'], 'unknown')
            self.assertFalse(order['is_fill_evidence'])
            self.assertFalse(order['execution_evidence'])
            self.assertEqual(result['positions'], [])
            self.assertEqual(store.events('ledger'), [])
            self.assertEqual(store.items('ledger_staged'), [])

    def test_immutable_same_receipt_conflicts_and_hash_identity_is_stable(self):
        with Store(':memory:') as store:
            raw = screen()
            record_broker_snapshot(store, raw)
            raw['summary']['cash'] = '50000'
            with self.assertRaises(ValueError):
                record_broker_snapshot(store, raw)
            raw = screen()
            raw.pop('id')
            first = record_broker_snapshot(store, raw)
            second = record_broker_snapshot(store, raw)
            self.assertEqual(first['id'], second['id'])

    def test_information_cutoff_uses_recorded_at_not_unknown_screenshot_clock(self):
        with Store(':memory:') as store:
            record_broker_snapshot(store, screen())
            self.assertIsNone(latest_broker_snapshot(store, now=BEFORE))
            record = latest_broker_snapshot(store, now=RECEIPT)
            self.assertEqual(record['id'], screen()['id'])
            newer = screen()
            newer.update(id='later-screen', recorded_at=AFTER)
            newer['summary']['cash'] = '24000'
            record_broker_snapshot(store, newer)
            self.assertEqual(latest_broker_snapshot(store, now=RECEIPT)['id'], screen()['id'])
            self.assertEqual(latest_broker_snapshot(store, now=AFTER)['id'], 'later-screen')
            self.assertNotIn('broker_snapshot_id', with_broker_snapshot(store, portfolio(store, now=BEFORE)))

    def test_paper_account_is_never_overlaid_with_real_ibkr_cash(self):
        with Store(':memory:') as store:
            record_broker_snapshot(store, screen())
            paper = {'account': 'paper', 'as_of': AFTER, 'cash': '20000', 'total_equity': '20000'}
            self.assertEqual(with_broker_snapshot(store, paper), paper)
            self.assertIsNone(latest_broker_snapshot(store, 'paper', now=AFTER))

    def test_staged_cash_change_does_not_invalidate_but_confirmation_does(self):
        with Store(':memory:') as store:
            opening(store)
            record_broker_snapshot(store, screen())
            stage_entry(store, {'id': 'new-deposit', 'type': 'deposit', 'account': 'real',
                'timestamp': AFTER, 'currency': 'USD', 'amount': '100'})
            self.assertEqual(with_broker_snapshot(store, portfolio(store, now=AFTER))['cash'], '23921.59')
            with patch('rhino_invest.ledger.utc_now', return_value=AFTER):
                confirm_entry(store, 'new-deposit')
            result = with_broker_snapshot(store, portfolio(store, now=AFTER))
            self.assertIsNone(result['cash'])
            self.assertIsNone(result['total_equity'])
            self.assertEqual(result['broker_reported_cash'], '23921.59')
            self.assertEqual(result['ledger_cash'], '20100')
            self.assertEqual(result['broker_new_cash_event_ids'], ['new-deposit'])
            self.assertEqual(result['cash_status'], 'new_ledger_cash_event_requires_broker_refresh')
            refreshed = screen()
            refreshed.update(id='refreshed', recorded_at='2026-10-24T03:00:00+00:00')
            refreshed['summary']['cash'] = '24021.59'
            record_broker_snapshot(store, refreshed)
            self.assertEqual(with_broker_snapshot(store, portfolio(store, now=refreshed['recorded_at']))['cash'], '24021.59')

    def test_backdated_late_confirmed_cash_event_and_correction_need_new_report(self):
        with Store(':memory:') as store:
            opening(store)
            stage_entry(store, {'id': 'old-deposit', 'type': 'deposit', 'account': 'real',
                'timestamp': BEFORE, 'currency': 'USD', 'amount': '100'})
            record_broker_snapshot(store, screen())
            with patch('rhino_invest.ledger.utc_now', return_value=AFTER):
                confirm_entry(store, 'old-deposit')
            self.assertIsNone(with_broker_snapshot(store, portfolio(store, now=AFTER))['cash'])
        with Store(':memory:') as store:
            opening(store)
            record_broker_snapshot(store, screen())
            correct_entry(store, 'user-opening-20000', dict(store.get('ledger_staged', 'user-opening-20000'), amount='21000'),
                id='cash-correction', timestamp=AFTER, reason='new statement correction')
            with patch('rhino_invest.ledger.utc_now', return_value=AFTER):
                confirm_entry(store, 'cash-correction')
            result = with_broker_snapshot(store, portfolio(store, now=AFTER))
            self.assertIsNone(result['cash'])
            self.assertEqual(result['broker_new_cash_event_ids'], ['cash-correction'])

    def test_future_confirmation_does_not_leak_into_earlier_report_cutoff(self):
        with Store(':memory:') as store:
            opening(store)
            record_broker_snapshot(store, screen())
            stage_entry(store, {'id': 'late-old-deposit', 'type': 'deposit', 'account': 'real',
                'timestamp': BEFORE, 'currency': 'USD', 'amount': '100'})
            with patch('rhino_invest.ledger.utc_now', return_value=AFTER):
                confirm_entry(store, 'late-old-deposit')
            result = with_broker_snapshot(store, portfolio(store, now=RECEIPT))
            self.assertEqual(result['cash'], '23921.59')
            self.assertEqual(result['broker_new_cash_event_ids'], [])

    def test_new_position_baseline_and_paper_cash_are_not_real_cash_events(self):
        with Store(':memory:') as store:
            opening(store)
            record_broker_snapshot(store, screen())
            for entry in ({'id': 'known-position', 'type': 'opening_position', 'account': 'real',
                'symbol': 'GOOG', 'timestamp': AFTER, 'currency': 'USD', 'quantity': '1', 'price': None, 'fee': None},
                {'id': 'paper-baseline', 'type': 'opening_cash', 'account': 'paper',
                 'timestamp': AFTER, 'currency': 'USD', 'amount': '20000'}):
                stage_entry(store, entry)
                with patch('rhino_invest.ledger.utc_now', return_value=AFTER):
                    confirm_entry(store, entry['id'])
            result = with_broker_snapshot(store, portfolio(store, now=AFTER))
            self.assertEqual(result['cash'], '23921.59')
            self.assertEqual(result['broker_new_cash_event_ids'], [])
            self.assertIsNone(result['positions'][0]['cost_basis'])

    def test_unknown_values_remain_unknown_and_input_errors_are_rejected(self):
        with Store(':memory:') as store:
            raw = screen()
            raw['summary']['cash'] = None
            raw['holdings'][0]['market_value'] = None
            record_broker_snapshot(store, raw)
            result = with_broker_snapshot(store, portfolio(store, now=AFTER))
            self.assertIsNone(result['cash'])
            self.assertIsNone(result['broker_reported_non_cash_value'])
            self.assertIsNone(result['broker_reported_holdings_market_value_sum'])
            self.assertIsNone(result['broker_reported_unexplained_non_cash_value'])
            for edit in (lambda p: p.update(account='paper'), lambda p: p.update(recorded_at='2026-10-24'),
                lambda p: p.update(as_of='2026-10-25T00:00:00+00:00'),
                lambda p: p['summary'].update(cash='NaN'), lambda p: p.update(supersedes_symbols=['GOOG']),
                lambda p: p['holdings'][0].update(quantity=True)):
                invalid = screen()
                edit(invalid)
                with self.assertRaises(ValueError):
                    record_broker_snapshot(store, invalid)


if __name__ == '__main__':
    unittest.main()
