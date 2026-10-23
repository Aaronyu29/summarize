import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rhino_invest.__main__ import main
from rhino_invest.config import Config
from rhino_invest.store import Store


class CLITests(unittest.TestCase):
    def test_csv_preview_stage_and_confirm_are_separate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db, csv = str(Path(folder) / 'invest.sqlite'), Path(folder) / 'input.csv'
            csv.write_text('id,type,timestamp,currency,amount\nopen1,opening_cash,2026-10-23T00:00:00Z,USD,9000\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--database', db, 'import-csv', str(csv)]), 0)
            with Store(db) as store:
                self.assertEqual(store.items('ledger_staged'), [])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--database', db, 'import-csv', str(csv), '--execute']), 0)
            with Store(db) as store:
                self.assertEqual(len(store.items('ledger_staged')), 1)
                self.assertEqual(store.events('ledger'), [])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--database', db, 'confirm', 'open1', '--execute']), 0)
            with Store(db) as store:
                self.assertEqual(len(store.events('ledger')), 1)

    def test_diagnostics_does_not_print_credentials(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder, patch.dict(os.environ, {'INVEST_FEISHU_APP_SECRET': 'DO_NOT_PRINT'}, clear=False):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(['--database', str(Path(folder)/'a.sqlite'), 'diagnostics']), 0)
            self.assertNotIn('DO_NOT_PRINT', output.getvalue())
            self.assertIn('manual_only', output.getvalue())

    def test_model_remains_explicit_and_polling_cannot_be_accidentally_too_fast(self):
        self.assertFalse(Config.load(environ={}).model['enabled'])
        with self.assertRaises(ValueError):
            Config.load(environ={'INVEST_INTERVAL_SECONDS': '1'})

    def test_reviewed_evidence_and_adr_binding_require_explicit_execute(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db = str(Path(folder) / 'invest.sqlite')
            evidence = Path(folder) / 'reviewed-source.txt'
            evidence.write_text('Fixture primary source: reviewed ADR ratio and currency facts.', encoding='utf-8')
            metadata = Path(folder) / 'instrument.json'
            metadata.write_text(json.dumps({'is_adr': True, 'ordinary_shares_per_adr': '5',
                'underlying_currency': 'TWD', 'currency': 'USD', 'price_unit': 'per_adr'}), encoding='utf-8')
            arguments = ['--database', db, 'import-evidence', str(evidence), '--source-url',
                'https://issuer.example/filing', '--as-of', '2026-10-22T00:00:00Z', '--reviewed-by', 'fixture-reviewer']
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(arguments), 0)
            with Store(db) as store:
                self.assertEqual(store.items('external_evidence'), [])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(arguments + ['--execute']), 0)
            evidence_id = json.loads(output.getvalue())['evidence_id']
            binding = ['--database', db, 'bind-instrument', 'TSM', str(metadata), '--evidence-id', evidence_id,
                       '--reviewed-by', 'fixture-reviewer', '--reviewed-at', '2026-10-23T00:00:00Z']
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(binding), 0)
            with Store(db) as store:
                self.assertIsNone(store.get('instrument_identity', 'TSM'))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(binding + ['--execute']), 0)
                self.assertEqual(main(binding + ['--execute']), 0)
            with Store(db) as store:
                identity = store.get('instrument_identity', 'TSM')
                self.assertEqual(identity['metadata']['ordinary_shares_per_adr'], '5')
                self.assertEqual(len(store.items('review_trigger')), 1)
                self.assertEqual(store.items('model_call'), [])
                self.assertEqual(store.events('ledger'), [])


if __name__ == '__main__':
    unittest.main()
