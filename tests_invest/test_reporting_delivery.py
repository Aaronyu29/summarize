import unittest
from rhino_invest.reporting import make_report, report_row, portfolio_rows


class ReportingTests(unittest.TestCase):
    def test_unknown_balance_is_never_presented_as_target_cash(self):
        snapshot = {'account': 'real', 'cash': None, 'market_value': None, 'total_equity': None,
                    'positions': [], 'cash_complete': False, 'cost_complete': False, 'fees_complete': False}
        report = make_report('每日复盘', '2026-10-23', snapshot)
        self.assertIn('实际现金：未知', report['body'])
        self.assertEqual(report_row(report)['现金美元'], '未知／未配置')
        self.assertIn('最大回撤硬限制：未配置', report['body'])
        self.assertEqual(make_report('每日复盘', '2026-10-23', snapshot)['id'], report['id'])
        self.assertNotEqual(make_report('每日复盘', '2026-10-23', dict(snapshot, account='paper'))['id'], report['id'])

    def test_readable_positions_use_net_cost_and_do_not_zero_gaps(self):
        rows = portfolio_rows({'account': 'real', 'positions': [{'symbol': 'TSM', 'quantity': '3',
            'cost_basis_net': '1000', 'market_value': None, 'unrealized_pnl_net': None}]})
        self.assertEqual(rows[0]['成本'], '1000')
        self.assertEqual(rows[0]['市值'], '未知／未配置')


if __name__ == '__main__':
    unittest.main()
