import unittest
from rhino_invest.primary_evidence import _google_class_filing,GOOGLE_10Q_2026Q2,EvidenceError
from rhino_invest.market import normalize_fundamentals
from rhino_invest.store import Store
from tests_invest.test_primary_evidence_delivery import Response,Transport

TEXT='''Alphabet Inc. (Form: 10-Q, Received: 07/23/2026 06:04:16)
0001652044 FORM 10-Q
For the quarterly period ended June 30, 2026
Note 11. Stockholders Equity
May 15, 2029 2.2740 2.8420
Note 12. Net Income Per Common Share
Three Months Ended June 30,
2025 2026
Class AClass BClass CConsolidatedClass AClass BClass CConsolidated
Diluted net income per common share:
Net income$15,429 $1,970 $12,767 $28,196 $60,893 $7,612 $51,300 112,193
Number of shares used in per share computation6,671 852 5,527 12,198 6,679 835 5,630 12,309
Diluted net income per common share$2.31 $2.31 $2.31 $2.31 $9.12 $9.12 $9.11 $9.11
Preferred dividends are added back and preferred shares are assumed to have converted to common pursuant to the if-converted method.
Six Months Ended June 30,
Note 13. Other
'''

class GoogleClassTests(unittest.TestCase):
    def test_class_c_shares_eps_and_preferred_addback_are_sourced_not_group_guesses(self):
        with Store(':memory:') as store:
            body='<html>'+''.join('<p>'+line+'</p>' for line in TEXT.splitlines())+'</html>'
            transport=Transport({GOOGLE_10Q_2026Q2:Response(body)})
            out={'symbol':'GOOG','primary_financials':{'symbol':'GOOG','facts':[]},'primary_evidence':[]}
            _google_class_filing(store,out,'2026-10-27T12:00:00Z',transport)
            p=out['primary_financials'];self.assertEqual(p['class_eps_reconciliation']['diluted_eps'],'9.11')
            self.assertEqual(p['class_eps_reconciliation']['diluted_shares'],'5630000000')
            self.assertEqual(p['class_eps_reconciliation']['group_diluted_shares'],'12309000000')
            normalized=normalize_fundamentals(out,store=store)
            self.assertTrue(all(f['source_verified'] for f in normalized['primary_financials']['facts']))
            self.assertIn('never deduct dividends a second time',p['financing_reconciliation']['preferred_method'])
            self.assertEqual(store.items('model_call'),[])

    def test_future_filing_or_reordered_columns_cannot_fill_missing_evidence(self):
        for text in (TEXT.replace('07/23/2026','10/28/2026'),TEXT.replace('Class AClass BClass C','Class AClass CClass B')):
            with Store(':memory:') as store:
                body='<html>'+''.join('<p>'+line+'</p>' for line in text.splitlines())+'</html>'
                out={'symbol':'GOOG','primary_financials':{'facts':[]},'primary_evidence':[]}
                with self.assertRaises((EvidenceError,ValueError)):
                    _google_class_filing(store,out,'2026-10-27T12:00:00Z',Transport({GOOGLE_10Q_2026Q2:Response(body)}))
                self.assertEqual(out['primary_financials']['facts'],[])

if __name__=='__main__':unittest.main()
