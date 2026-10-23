import json
import unittest

from rhino_invest.config import Config


WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/fixture-investment-hook'
LEGACY = 'https://open.larksuite.com/open-apis/bot/v2/hook/fixture-legacy-hook'


class NotificationConfigTests(unittest.TestCase):
    def test_legacy_environment_is_not_implicitly_authorized(self):
        config = Config.load(environ={'LARK_WEBHOOK': LEGACY, 'LARK_WEBHOOK_SECRET': 'legacy-signing-secret'})
        self.assertIsNone(config.feishu_webhook_url)
        self.assertIsNone(config.feishu_webhook_secret)
        self.assertEqual(config.diagnostics()['feishu_notification_route'], 'disabled')

    def test_explicit_legacy_reuse_and_investment_url_priority(self):
        reused = Config.load(environ={'INVEST_REUSE_LARK_WEBHOOK': 'true', 'LARK_WEBHOOK': LEGACY,
                                     'LARK_WEBHOOK_SECRET': 'legacy-signing-secret'})
        self.assertEqual(reused.feishu_webhook_url, LEGACY)
        self.assertEqual(reused.feishu_webhook_source, 'legacy_explicit')
        configured = Config.load(environ={'INVEST_REUSE_LARK_WEBHOOK': 'true', 'LARK_WEBHOOK_URL': LEGACY,
            'LARK_WEBHOOK_SECRET': 'wrong-bot-secret', 'INVEST_FEISHU_WEBHOOK_URL': WEBHOOK})
        self.assertEqual(configured.feishu_webhook_url, WEBHOOK)
        self.assertEqual(configured.feishu_webhook_source, 'investment')
        self.assertIsNone(configured.feishu_webhook_secret)  # Never apply another bot's signing secret.

    def test_explicit_signing_secret_and_chat_priority_are_visible_without_secrets(self):
        config = Config.load(environ={'INVEST_FEISHU_WEBHOOK_URL': WEBHOOK,
            'INVEST_FEISHU_WEBHOOK_SECRET': 'private-signing-secret', 'INVEST_FEISHU_CHAT_ID': 'private-chat'})
        self.assertEqual(config.diagnostics()['feishu_notification_route'], 'chat_readback')
        diagnostics = json.dumps(config.diagnostics())
        for hidden in (WEBHOOK, 'private-signing-secret', 'private-chat'):
            self.assertNotIn(hidden, diagnostics)
            self.assertNotIn(hidden, repr(config))

    def test_invalid_url_and_boolean_errors_do_not_echo_values(self):
        for bad in ('http://open.feishu.cn/open-apis/bot/v2/hook/private-token',
                    'https://example.org/open-apis/bot/v2/hook/private-token',
                    WEBHOOK + '?secret=private-token'):
            with self.assertRaises(ValueError) as context:
                Config.load(environ={'INVEST_FEISHU_WEBHOOK_URL': bad})
            self.assertNotIn(bad, str(context.exception))
        with self.assertRaises(ValueError):
            Config.load(environ={'INVEST_REUSE_LARK_WEBHOOK': 'ambiguous'})


if __name__ == '__main__':
    unittest.main()
