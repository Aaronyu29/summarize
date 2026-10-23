import pathlib
import tempfile
import unittest
from finance_jobs import Jobs
from finance_cloud import unseen_entries, batches, canonicalize_evidence, publication_time, delivery_title


class JobsTests(unittest.TestCase):
    def test_publication_uses_matching_video_and_beijing_calendar_date(self):
        entries = [{'video_id': 'other', 'published_utc': '2026-10-20T02:00:00Z'},
                   {'video_id': 'target', 'published_utc': '2026-10-22T18:10:36Z'}]
        self.assertEqual(publication_time({'id': 'target'}, entries), '2026-10-23 02:10:36')
        with self.assertRaises(ValueError):
            publication_time({'id': 'missing'}, entries)
        with self.assertRaises(ValueError):
            publication_time({'id': 'target'}, [{'video_id': 'target', 'published_utc': '2026-10-23T10:00:00'}])

    def test_notice_uses_archived_publication_not_report_generation_date(self):
        info = {'id': 'target', 'title': '半导体观察'}
        analysis = {'videos': [{'video_id': 'target', 'published_shanghai': '2026-10-23 10:10:36'}]}
        self.assertIn('视频发布：2026-10-23 10:10:36（北京时间）', delivery_title(info, analysis))

    def test_symbolic_evidence_maps_to_real_ids_without_mutating_candidate(self):
        candidate = {'claims': [{'evidence_segment_ids': ['S000041']}]}
        result = canonicalize_evidence(candidate, [{'id':41}])
        self.assertEqual(result['claims'][0]['evidence_segment_ids'], [41])
        self.assertEqual(candidate['claims'][0]['evidence_segment_ids'], ['S000041'])

    def test_seconds_cannot_be_accepted_as_evidence_ids(self):
        for ref in [41, 2011, '2011', 'S002011', 'S41']:
            with self.assertRaises(ValueError):
                canonicalize_evidence({'claims':[{'evidence_segment_ids':[ref]}]}, [{'id':41}])

    def test_visual_batches_cover_all_frames_without_loading_one_large_batch(self):
        frames = list(range(38))
        result = list(batches(frames))
        self.assertEqual([len(b) for b in result], [8,8,8,8,6])
        self.assertEqual([f for b in result for f in b], frames)

    def setUp(self):
        root = pathlib.Path(__file__).resolve().parent.parent / '.cache' / 'finance-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / 'jobs.sqlite'
        self.jobs = Jobs(self.path)
        self.addCleanup(self.jobs.db.close)
        self.entry = {'video_id': 'DQ0Yn27pdgk', 'title': 'original', 'url': 'https://youtu.be/DQ0Yn27pdgk'}

    def test_duplicate_id_across_dates_and_changed_titles_is_never_requeued(self):
        self.assertTrue(self.jobs.add(self.entry))
        self.jobs.save(self.entry['video_id'], status='complete', analysis_complete=True)
        self.assertFalse(self.jobs.add({**self.entry, 'title': 'renamed', 'published_utc': 'later'}))
        self.assertEqual(self.jobs.pending(), [])
        self.assertEqual(self.jobs.get(self.entry['video_id'])['source']['title'], 'original')

    def test_delivery_retry_reuses_analysis_checkpoint_after_restart(self):
        vid = self.entry['video_id']
        self.jobs.add(self.entry)
        self.jobs.start(vid)
        self.jobs.save(vid, status='retry', analysis_complete=True, report='/data/brief.md')
        other = Jobs(self.path)
        self.addCleanup(other.db.close)
        self.assertEqual(other.pending(), [vid])
        self.assertTrue(other.get(vid)['checkpoint']['analysis_complete'])
        self.assertEqual(other.get(vid)['checkpoint']['report'], '/data/brief.md')

    def test_unknown_remote_results_and_historic_baseline_never_auto_run(self):
        for index, status in enumerate(['needs_review', 'baseline', 'complete']):
            self.jobs.add({**self.entry, 'video_id': f'video{index:06d}'}, status=status)
        self.assertEqual(self.jobs.pending(), [])

    def test_restart_keeps_unfinished_job_and_attempt_limit(self):
        vid = self.entry['video_id']
        self.jobs.add(self.entry)
        self.jobs.start(vid)
        self.assertEqual(self.jobs.pending(), [vid])
        for _ in range(4):
            self.jobs.start(vid)
        self.assertEqual(self.jobs.pending(), [])

    def test_playlist_frontier_avoids_rechecking_old_members_only_videos(self):
        self.jobs.add(self.entry, status='complete')
        new = {**self.entry, 'video_id': 'NEWvideo123'}
        old = {**self.entry, 'video_id': 'OLDvideo123'}
        entries = [{**e, 'discovery_method': 'channel_playlist'} for e in [new, self.entry, old]]
        self.assertEqual([e['video_id'] for e in unseen_entries(entries, self.jobs)], ['NEWvideo123'])

    def test_rss_still_checks_unknown_entries_after_a_known_id(self):
        self.jobs.add(self.entry, status='complete')
        entries = [self.entry, {**self.entry, 'video_id': 'NEWvideo123'}]
        self.assertEqual([e['video_id'] for e in unseen_entries(entries, self.jobs)], ['NEWvideo123'])

    def test_explicit_backfill_only_promotes_historic_video_once(self):
        vid = self.entry['video_id']
        self.jobs.add(self.entry, status='baseline')
        self.assertTrue(self.jobs.request_historic(vid))
        self.assertEqual(self.jobs.pending(), [vid])
        self.assertTrue(self.jobs.get(vid)['checkpoint']['explicitly_requested_backfill'])
        self.jobs.save(vid, status='complete', analysis_complete=True)
        self.assertFalse(self.jobs.request_historic(vid))
        self.assertEqual(self.jobs.pending(), [])

    def test_backfill_does_not_reset_held_or_exhausted_jobs(self):
        vid = self.entry['video_id']
        self.jobs.add(self.entry, status='needs_review')
        with self.assertRaises(ValueError):
            self.jobs.request_historic(vid)
        self.jobs.save(vid, status='retry')
        for _ in range(5):
            self.jobs.start(vid)
        with self.assertRaises(ValueError):
            self.jobs.request_historic(vid)


if __name__ == '__main__':
    unittest.main()
