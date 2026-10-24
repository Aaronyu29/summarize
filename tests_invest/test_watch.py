import copy
import hashlib
import unittest

from rhino_invest.common import stable_id
from rhino_invest.store import Store
from rhino_invest.watch import evaluate_levels
from rhino_invest.ledger import stage_entry, confirm_entry, correct_entry


NOW = "2026-10-23T15:00:00+00:00"


class VideoWatchTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def level(self, *, date="20261022", video="SOXXvideo01", kind="technical_support", verified=True,
              low="360", high="365", adjustment="NoAdjust", seal=True, extract=True):
        snapshot = {"video_id": video, "revision": "1", "title": "SOXX条件观察",
            "analysis_sha256": "a" * 64, "manifest_sha256": "b" * 64,
            "metadata": {"upload_date": date, "timestamp": None, "duration": 1800},
            "transcript": {"segments": [{"id": 35, "start": 99, "end": 105, "text": "如果回撤企稳，再观察"}]},
            "frames": [{"timestamp_seconds": 100, "path": "frame100.jpg", "sha256": "c" * 64}],
            "files": [{"relative_path": "frame100.jpg", "sha256": "c" * 64}]}
        snapshot["id"] = stable_id("research-evidence-v1", snapshot)
        snapshot["ingested_at"] = NOW
        self.store.put("research_evidence", snapshot["id"], snapshot, immutable=True)
        if seal:
            self.store.put("research_archive_seal", stable_id(video, date), {"snapshot_id": snapshot["id"],
                "analysis_sha256": snapshot["analysis_sha256"], "manifest_sha256": snapshot["manifest_sha256"]}, immutable=True)
        level = {"id": stable_id(snapshot["id"], kind, low, high, adjustment), "symbol": "SOXX", "kind": kind,
                 "low": low, "high": high, "value": None, "currency": "USD", "price_unit": "per_share",
                 "source_date": date, "adjustment": adjustment, "source_horizon": "视频回撤条件，不是长线估值",
                 "timestamp_seconds": 100, "evidence_segment_ids": [35], "frame_seconds": 100,
                 "conditions": "如果回撤企稳，再观察；企稳尚未独立确认", "uncertainty": "仅观察",
                 "evidence_refs": [snapshot["id"]], "verified": verified, "issues": []}
        self.store.put("research_level", level["id"], level, immutable=True)
        if extract:
            self.store.put("research_extraction", stable_id("fixture-extract", level["id"]), {"levels": [level]}, immutable=True)
        return level

    def quote(self, price="362", timestamp=NOW, **changes):
        return {"symbol": "SOXX", "selected": {"symbol": "SOXX", "source": "longbridge-openapi",
            "status": "current", "currency": "USD", "session": "regular", "permission_verified": True,
            "quote_time": timestamp, "price": price, **changes}}

    def test_soxx_needs_review_plan_does_not_disable_independent_zone_watch(self):
        level = self.level()
        self.store.put("plan", "SOXX", {"id": "SOXX", "symbol": "SOXX", "status": "needs_review"})
        events = evaluate_levels(self.store, [self.quote()], NOW)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIn("触达视频观察区间，不构成买入建议", event["body"])
        self.assertIn(level["conditions"], event["body"])
        self.assertIn("01:40", event["body"])
        self.assertEqual(event["price"], "362")
        self.assertEqual(event["adjustment"], "NoAdjust")
        self.assertEqual(event["evidence"], level["evidence_refs"])
        self.assertFalse(event["trade_or_recommendation"])
        self.assertEqual(self.store.events("ledger"), [])
        self.assertEqual(self.store.items("recommendation"), [])
        self.assertEqual(self.store.items("paper_fill"), [])

    def test_repeats_quiet_and_real_leave_reentry_gets_new_immutable_event(self):
        self.level()
        first = evaluate_levels(self.store, [self.quote()], NOW)
        self.assertEqual(evaluate_levels(self.store, [self.quote()], NOW), [])
        self.assertEqual(evaluate_levels(self.store, [self.quote(timestamp="2026-10-23T15:01:00Z")], "2026-10-23T15:01:01Z"), [])
        self.assertEqual(evaluate_levels(self.store, [self.quote("350", "2026-10-23T15:02:00Z")], "2026-10-23T15:02:01Z"), [])
        second = evaluate_levels(self.store, [self.quote("363", "2026-10-23T15:03:00Z")], "2026-10-23T15:03:01Z")
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(second[0]["trigger"], "zone_enter")
        self.assertEqual(len(self.store.events("watch")), 2)

    def test_price_gap_cross_is_not_an_observed_zone_touch(self):
        self.level()
        self.assertEqual(evaluate_levels(self.store, [self.quote("350")], NOW), [])
        events = evaluate_levels(self.store, [self.quote("370", "2026-10-23T15:01:00Z")], "2026-10-23T15:01:01Z")
        self.assertEqual(events[0]["trigger"], "gap_cross")
        self.assertFalse(events[0]["touch_observed"])
        self.assertIn("未观察到实际触达", events[0]["body"])
        self.assertEqual(events[0]["prior_price"], "350")

    def test_stale_delayed_wrong_currency_and_unknown_permission_are_rejected(self):
        self.level()
        for q in (self.quote(timestamp="2026-10-23T14:59:00Z"), self.quote(delayed=True),
                  self.quote(currency="TWD"), self.quote(permission_verified=None),
                  self.quote(source="public-fallback"), self.quote(status="historical")):
            with self.subTest(quote=q):
                self.assertEqual(evaluate_levels(self.store, [q], NOW), [])
        self.assertEqual(self.store.items("watch_state"), [])

    def test_extended_hours_use_120s_and_preserve_session(self):
        self.level()
        quote = self.quote(timestamp="2026-10-23T14:58:30Z", session="premarket")
        events = evaluate_levels(self.store, [quote], NOW)
        self.assertEqual(events[0]["session"], "premarket")
        self.assertIn("盘前", events[0]["body"])

    def test_valuation_historical_and_unsupported_adjusted_levels_never_alert(self):
        self.level(kind="valuation")
        self.level(kind="historical_quote")
        self.level(kind="technical_support", adjustment="ForwardAdjust")
        self.assertEqual(evaluate_levels(self.store, [self.quote()], NOW), [])

    def test_missing_seal_or_missing_independent_extraction_fails_closed(self):
        for seal, extract in ((False, True), (True, False)):
            with self.subTest(seal=seal, extract=extract), Store(":memory:") as isolated:
                original = self.store
                self.store = isolated
                self.level(seal=seal, extract=extract)
                self.assertEqual(evaluate_levels(self.store, [self.quote()], NOW), [])
                self.store = original

    def test_latest_publication_wins_even_when_ingested_older_source_last(self):
        newest = self.level(date="20261022", video="SOXXvideo02", low="400", high="410")
        self.level(date="20261021", video="SOXXvideo01")
        self.assertEqual(evaluate_levels(self.store, [self.quote("362")], NOW), [])
        events = evaluate_levels(self.store, [self.quote("405", "2026-10-23T15:01:00Z")], "2026-10-23T15:01:01Z")
        self.assertEqual(events[0]["level_id"], newest["id"])

    def test_newer_unverified_source_does_not_revive_old_technical_levels(self):
        self.level(date="20261021", video="SOXXvideo01")
        self.level(date="20261022", video="SOXXvideo02", verified=False)
        self.assertEqual(evaluate_levels(self.store, [self.quote()], NOW), [])

    def real_split(self):
        for entry in ({"id": "opening", "type": "opening_position", "symbol": "SOXX", "quantity": "10",
                       "price": "100", "fee": "0", "timestamp": "2026-10-21T12:00:00Z"},
                      {"id": "split", "type": "split", "symbol": "SOXX", "ratio": "2",
                       "timestamp": "2026-10-22T13:00:00Z"}):
            stage_entry(self.store, {"account": "real", "currency": "USD", **entry})
            confirm_entry(self.store, entry["id"])

    def test_known_split_invalidates_older_price_zones_without_inventing_new_levels(self):
        self.level(date="20261022")
        self.real_split()
        self.assertEqual(evaluate_levels(self.store, [self.quote()], NOW), [])
        fresh = self.level(date="20261023", video="SOXXvideo02")
        events = evaluate_levels(self.store, [self.quote()], NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["level_id"], fresh["id"])
        self.assertEqual(events[0]["zone"], {"low": "360", "high": "365"})
        self.assertEqual(len(self.store.events("ledger")), 2)

    def test_void_split_does_not_falsely_suppress_a_verified_observation(self):
        self.level()
        self.real_split()
        staged = correct_entry(self.store, "split", None, id="void-split", timestamp="2026-10-23T14:00:00Z")
        confirm_entry(self.store, staged["id"])
        events = evaluate_levels(self.store, [self.quote()], NOW)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["touch_observed"])

    def issuer_split(self, *, effective='2026-11-05', url='https://www.ishares.com/us/products/239705/ishares-phlx-semiconductor-etf', sha_valid=True, declared=True):
        text = ('On August 21, 2026, iShares filed to issue a forward stock split for SOXX. '
                'Shares of SOXX will begin trading on a split-adjusted basis on November 5, 2026.')
        ref = stable_id('issuer-split', url)
        self.store.put('external_evidence', ref, {'id': ref, 'source_url': url, 'content': text,
            'sha256': hashlib.sha256(text.encode()).hexdigest() if sha_valid else '0' * 64,
            'observed_at': NOW}, immutable=True)
        self.store.put('fundamental_cache', 'SOXX', {'data': {
            'primary_evidence': [{'id': ref}] if declared else [],
            'etf_valuation': {'issuer_verified': True, 'corporate_actions': [{'type': 'split',
                'effective_at': effective, 'status': 'effective', 'new_shares_per_old': '3',
                'source_excerpt': text, 'evidence_refs': [ref]}]}}})

    def test_verified_issuer_split_blocks_old_zone_at_new_york_effective_midnight_without_ledger_write(self):
        self.level()
        self.issuer_split()
        before = '2026-11-05T04:59:59Z'
        self.assertEqual(len(evaluate_levels(self.store, [self.quote(timestamp=before)], before)), 1)
        effective = '2026-11-05T05:00:00Z'
        self.assertEqual(evaluate_levels(self.store, [self.quote(timestamp=effective)], effective), [])
        self.assertEqual(len(self.store.items('watch_block')), 1)
        self.assertEqual(self.store.events('ledger'), [])
        original = self.store.items('research_level')[0][1]
        self.assertEqual((original['low'], original['high']), ('360', '365'))

    def test_announced_split_does_not_hide_current_observations_before_effective_day(self):
        self.level()
        self.issuer_split()
        self.assertEqual(len(evaluate_levels(self.store, [self.quote()], NOW)), 1)
        self.assertEqual(self.store.items('watch_block'), [])

    def test_primary_split_flags_tampered_dates_bad_sha_unapproved_url_and_missing_refs_cannot_suppress(self):
        for fields in ({'effective': '2026-11-04'}, {'sha_valid': False}, {'declared': False},
                       {'url': 'https://www.ishares.com/us/products/999999/other-fund'},
                       {'url': 'https://www.ishares.com.attacker.example/us/products/239705/'}):
            with self.subTest(fields=fields), Store(':memory:') as isolated:
                previous = self.store
                self.store = isolated
                try:
                    self.level()
                    self.issuer_split(**fields)
                    now = '2026-11-05T15:00:00Z'
                    self.assertEqual(len(evaluate_levels(self.store, [self.quote(timestamp=now)], now)), 1)
                    self.assertEqual(self.store.items('watch_block'), [])
                finally:
                    self.store = previous

    def test_new_post_split_video_uses_its_own_prices_instead_of_dividing_old_prices(self):
        self.level()
        self.issuer_split()
        fresh = self.level(date='20261106', video='SOXXvideo02', low='360', high='365')
        now = '2026-11-06T15:00:00Z'
        events = evaluate_levels(self.store, [self.quote(timestamp=now)], now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['level_id'], fresh['id'])
        self.assertEqual(events[0]['zone'], {'low': '360', 'high': '365'})
        self.assertEqual(self.store.events('ledger'), [])


if __name__ == "__main__":
    unittest.main()
