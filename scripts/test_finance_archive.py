import copy
import contextlib
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest

from finance_archive import publish, validate, render, render_markdown, rich_text, validate_readability
from finance_collect import parse_feed


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temp_root = pathlib.Path(__file__).resolve().parent.parent / ".cache" / "finance-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.capture = pathlib.Path(self.temp.name)
        self.vid = "abcdefghijk"
        self.directory = self.capture / self.vid
        self.directory.mkdir()
        self.write(self.directory / f"{self.vid}.info.json", {
            "id": self.vid, "title": "META <unsafe>", "duration": 200,
            "availability": "public", "live_status": "not_live",
        })
        (self.directory / "media.f139.m4a").write_bytes(b"captured audio")
        self.transcript = {
            "source": "local_automatic_speech_recognition_not_creator_captions",
            "audio_sha256": hashlib.sha256(b"captured audio").hexdigest(),
            "segments": [{"id": 0, "start": 10, "end": 20, "text": "conditional view"},
                         {"id": 1, "start": 180, "end": 199, "text": "closing"}],
        }
        self.write(self.directory / "transcript.asr.json", self.transcript)
        (self.directory / "frame.jpg").write_bytes(b"frame bytes")
        self.write(self.directory / "frames.json", [{"timestamp_seconds": 15, "path": "frame.jpg",
                   "sha256": hashlib.sha256(b"frame bytes").hexdigest()}])
        self.analysis = {"schema_version": 1, "revision": "1", "edition": "sample",
                         "summary": "summary", "cross_video_observations": "comparison",
                         "videos": [{"video_id": self.vid, "published_shanghai": "2026-10-22",
                                     "overview": "overview", "review_questions": "question",
                                     "claims": [{"headline": "Conditional view", "text": "Only if conditions hold <script>",
                                                 "kind": "creator_view", "timestamp_seconds": 15,
                                                 "evidence_segment_ids": [0], "frame_seconds": 15}]}]}

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_or_partial_transcript_cannot_be_published(self):
        self.transcript["segments"] = self.transcript["segments"][:1]
        self.write(self.directory / "transcript.asr.json", self.transcript)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            validate(self.analysis, self.capture)

    def test_wrong_audio_or_frame_is_rejected(self):
        (self.directory / "media.f139.m4a").write_bytes(b"different recording")
        with self.assertRaisesRegex(ValueError, "audio"):
            validate(self.analysis, self.capture)
        (self.directory / "media.f139.m4a").write_bytes(b"captured audio")
        (self.directory / "frame.jpg").write_bytes(b"changed image")
        with self.assertRaisesRegex(ValueError, "integrity"):
            validate(self.analysis, self.capture)

    def test_unsupported_or_misaligned_claim_is_rejected(self):
        claim = self.analysis["videos"][0]["claims"][0]
        for change in [{"evidence_segment_ids": []}, {"evidence_segment_ids": [50]},
                       {"timestamp_seconds": 140}, {"timestamp_seconds": float("nan")},
                       {"frame_seconds": 16}, {"timestamp_seconds": 201}]:
            candidate = copy.deepcopy(self.analysis)
            candidate["videos"][0]["claims"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(candidate, self.capture)

    def test_frame_path_cannot_escape_capture(self):
        self.write(self.directory / "frames.json", [{"timestamp_seconds": 15, "path": "../outside.jpg",
                   "sha256": "irrelevant"}])
        with self.assertRaisesRegex(ValueError, "inside"):
            validate(self.analysis, self.capture)

    def test_publish_is_idempotent_and_revisions_are_immutable(self):
        analysis_path = self.capture / "analysis.json"
        db = self.capture / "library.sqlite"
        self.write(analysis_path, self.analysis)
        report = publish(analysis_path, db)
        publish(analysis_path, db)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM analyses").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM research_fts").fetchone()[0], 1)
        self.assertIn("&lt;script&gt;", report.read_text(encoding="utf-8"))
        self.analysis["summary"] = "changed"
        self.write(analysis_path, self.analysis)
        with self.assertRaisesRegex(ValueError, "immutable"):
            publish(analysis_path, db)

    def test_feed_uses_published_order_instead_of_updated(self):
        raw = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
        <entry><yt:videoId>aaaaaaaaaaa</yt:videoId><title>old</title><published>2026-10-20</published>
        <updated>2026-10-23</updated><link href="https://www.youtube.com/watch?v=aaaaaaaaaaa"/></entry>
        <entry><yt:videoId>bbbbbbbbbbb</yt:videoId><title>new</title><published>2026-10-22</published>
        <link href="https://www.youtube.com/watch?v=bbbbbbbbbbb"/></entry></feed>'''
        self.assertEqual(parse_feed(raw)[0]["title"], "new")

    def test_emphasis_preserves_conditions_escapes_html_and_does_not_duplicate_summary(self):
        phrase = '**若QQQ跌破698；当前尚未触发**'
        self.analysis['summary'] = phrase
        video = self.analysis['videos'][0]
        video['overview'] = phrase + '\n关注价格与条件。'
        video['claims'][0]['text'] = phrase + '\n<script>保持转义</script>'
        video['claims'][0]['uncertainty'] = '**期权链未展示**'
        video['review_questions'] = '**若收复737–746，则下调偏紧判断**'
        records = validate(self.analysis, self.capture)
        rendered = render(self.analysis, records)
        self.assertIn("<strong class='focus'>若QQQ跌破698；当前尚未触发</strong>", rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn("<strong class='focus'>期权链未展示</strong>", rendered)
        markdown = render_markdown(self.analysis, records)
        self.assertIn('## 本期速读', markdown)
        self.assertNotIn('核心判断：', markdown)
        self.assertEqual(markdown.count(phrase), 2)  # summary and evidence, not a second overview
        validate_readability(video)
        video['claims'][0]['text'] = 'QQQ跌破698'
        with self.assertRaisesRegex(ValueError, 'claim 0'):
            validate_readability(video)

    def test_readability_rejects_long_summary_and_unbalanced_emphasis(self):
        video = self.analysis['videos'][0]
        video['overview'] = '**重点**' + '长' * 220
        with self.assertRaisesRegex(ValueError, '220'):
            validate_readability(video)
        video['overview'] = '**缺失结束\n第二行'
        with self.assertRaisesRegex(ValueError, 'balanced'):
            validate_readability(video)

    def test_multiline_body_and_uncertainty_keep_markdown_breaks(self):
        self.analysis['summary'] = '**重点**\n• 风险\n• 条件'
        claim = self.analysis['videos'][0]['claims'][0]
        claim['text'] = '**关键价位**\n**尚未触发**'
        claim['uncertainty'] = '**未核实**\n不能当成成交'
        markdown = render_markdown(self.analysis, validate(self.analysis, self.capture))
        self.assertIn('**重点**  \n• 风险  \n• 条件', markdown)
        self.assertIn('**关键价位**  \n**尚未触发**', markdown)
        self.assertIn('> 核对说明：**未核实**  \n> 不能当成成交', markdown)

    def test_readability_rejects_unbroken_summary_and_paragraph_highlighting(self):
        video = self.analysis['videos'][0]
        video['overview'] = '**一大段**'
        with self.assertRaisesRegex(ValueError, 'separate takeaway'):
            validate_readability(video)
        video['overview'] = '**' + '大段' * 46 + '**\n第二行'
        with self.assertRaisesRegex(ValueError, 'long paragraph'):
            validate_readability(video)


if __name__ == "__main__":
    unittest.main()
