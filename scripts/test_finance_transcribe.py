import unittest
from finance_transcribe import windows, globalize


class ChunkedTranscriptionTests(unittest.TestCase):
    def test_entire_audio_is_covered_with_bounded_windows(self):
        chunks = list(windows(1698))
        self.assertEqual(len(chunks), 6)
        self.assertEqual(chunks[0], (0, 300, 0, 305))
        self.assertEqual(chunks[-1], (1500, 1698, 1495, 1698))
        self.assertTrue(all(b-a <= 310 for _, _, a, b in chunks))
        self.assertTrue(all(a[1] == b[0] for a,b in zip(chunks, chunks[1:])))

    def test_boundary_segment_is_owned_once_and_keeps_absolute_time(self):
        left = [{'start': 298, 'end': 302, 'text': 'cross-boundary'}]
        right = [{'start': 3, 'end': 7, 'text': 'cross-boundary'}]
        self.assertEqual(globalize(left, 0, 300, 0), [])
        kept = globalize(right, 300, 600, 295)
        self.assertEqual([(s['start'],s['end']) for s in kept], [(298,302)])

    def test_short_final_window_and_unchanged_source_items(self):
        original = {'start': 10, 'end': 12, 'text': 'x'}
        self.assertEqual(globalize([original], 300, 600, 295)[0]['start'],305)
        self.assertEqual(original['start'],10)
        self.assertEqual(list(windows(20)), [(0,20,0,20)])


if __name__ == '__main__':
    unittest.main()
