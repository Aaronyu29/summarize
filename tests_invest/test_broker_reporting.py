import unittest
from unittest.mock import patch
from rhino_invest.store import Store
from rhino_invest.broker_snapshot import record_broker_snapshot
from rhino_invest.reporting import (record_reported_holding, reported_holdings,
    with_reported_holdings, portfolio_rows, broker_account_fields)
from rhino_invest.ledger import stage_entry, confirm_entry, portfolio


class BrokerReportingTests(unittest.TestCase):
    def test_new_screenshot_corrects_symbol_and_keeps_reported_balances_out_of_ledger(self):
        with Store(':memory:') as store:
            stamp = '2026-10-24T02:01:58+00:00'
            stage_entry(store, {'id': 'initial', 'account': 'real', 'type': 'opening_cash',
                'currency': 'USD', 'amount': '20000', 'timestamp': '2026-10-23T12:00:00+00:00'})
            with patch('rhino_invest.ledger.utc_now', return_value='2026-10-23T12:00:00+00:00'):
                confirm_entry(store, 'initial')
            record_reported_holding(store, 'GOOGL', '750', reported_at='2026-10-23T12:00:00+00:00')
            record_broker_snapshot(store, {'id': 'screen', 'account': 'real', 'broker': 'IBKR',
                'currency': 'USD', 'recorded_at': stamp, 'as_of': None,
                'source': {'type': 'user_screenshot', 'reference': 'fixture'},
                'summary': {'cash': '23921.59', 'net_liquidation': '24536.28', 'buying_power': '96974.43'},
                'holdings': [{'symbol': 'GOOG', 'market_value': '480.55', 'last_price': '318.10'},
                             {'symbol': 'CRCL', 'market_value': '128.58', 'last_price': '84.92'}],
                'supersedes_symbols': ['GOOGL']})
            state = with_reported_holdings(store, portfolio(store, now=stamp))
            self.assertEqual((state['cash'], state['total_equity']), ('23921.59', '24536.28'))
            self.assertEqual(portfolio(store, now=stamp)['cash'], '20000')
            self.assertEqual({p['symbol'] for p in state['reported_holdings']}, {'GOOG', 'CRCL'})
            self.assertEqual([p['symbol'] for p in reported_holdings(store, now='2026-10-23T13:00:00+00:00')], ['GOOGL'])
            fields = broker_account_fields(state)
            self.assertEqual(fields['购买力美元'], '96974.43')
            self.assertEqual(fields['对账差额美元'], '5.56')
            self.assertIn('截图', fields['净值来源'])
            rows = {r['股票']: r for r in portfolio_rows(state)}
            self.assertEqual(rows['GOOG']['市价'], '318.10')
            self.assertIn('未显示', rows['GOOG']['数据状态'])
            self.assertFalse(state['holdings_complete'])
            self.assertEqual(len(store.events('ledger')), 1)


if __name__ == '__main__':
    unittest.main()
