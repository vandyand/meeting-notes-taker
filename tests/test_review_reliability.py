import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import windows_ocr
import screenshot_analysis
import process_latest


class NearDuplicateTests(unittest.TestCase):
    def test_small_changes_are_near_duplicates_but_strict_keeps_them(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / f'{n}.png' for n in range(3)]
            for n, path in enumerate(paths):
                image = Image.new('RGB', (1000, 1000), 'white')
                if n:
                    ImageDraw.Draw(image).rectangle((100, 100, 109, 109), fill='black')
                if n == 2:
                    ImageDraw.Draw(image).rectangle((200, 200, 220, 220), fill='black')
                image.save(path)
            _, strict = windows_ocr.select_screenshots(paths)
            self.assertTrue(strict[1]['automatic_selected'])
            _, near = windows_ocr.select_screenshots(paths, near_duplicate_fraction=.0002)
            self.assertFalse(near[1]['automatic_selected'])
            self.assertEqual(near[1]['reason'], 'near-duplicate')
            self.assertTrue(near[2]['automatic_selected'])

    def test_near_mode_compares_each_capture_to_immediate_predecessor(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for n in range(4):
                path = Path(temp) / f'{n}.png'
                image = Image.new('RGB', (1000, 1000), 'white')
                for box in range(n):
                    ImageDraw.Draw(image).rectangle((box*20, 100, box*20+9, 109), fill='black')
                image.save(path)
                paths.append(path)
            _, frames = windows_ocr.select_screenshots(paths, near_duplicate_fraction=.0002)
            self.assertEqual([f['automatic_selected'] for f in frames], [True, False, False, False])
            self.assertEqual(frames[3]['duplicate_of'], str(paths[2]))

    def test_aggressive_mode_culls_similar_adjacent_frames_even_after_a_cull(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            for n in range(3):
                path = Path(temp) / f'{n}.png'
                image = Image.new('RGB', (320, 200), 'white')
                ImageDraw.Draw(image).rectangle((40 + n * 4, 40, 100 + n * 4, 100), fill='black')
                image.save(path)
                paths.append(path)
            _, frames = windows_ocr.select_screenshots(paths, perceptual_distance=8)
            self.assertEqual([f['automatic_selected'] for f in frames], [True, False, False])
            self.assertEqual(frames[2]['duplicate_of'], str(paths[1]))
            self.assertEqual(frames[2]['reason'], 'perceptual-duplicate')

    def test_aggressive_mode_keeps_large_brightness_change(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / 'white.png', Path(temp) / 'black.png'
            Image.new('RGB', (320, 200), 'white').save(first)
            Image.new('RGB', (320, 200), 'black').save(second)
            _, frames = windows_ocr.select_screenshots([first, second], perceptual_distance=8)
            self.assertTrue(frames[1]['automatic_selected'])

    def test_session_default_and_strict_mode_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            self.assertEqual(screenshot_analysis.comparison_policy(session)['mode'], 'Aggressive')
            self.assertEqual(screenshot_analysis.comparison_options(session, {}).get('perceptual_distance'), 8)
            screenshot_analysis.save_duplicate_mode(session, 'Strict')
            self.assertEqual(screenshot_analysis.comparison_options(session, {}).get('near_duplicate_fraction'), 0)
            screenshot_analysis.save_duplicate_mode(session, 'Near duplicates')
            self.assertEqual(screenshot_analysis.comparison_options(session, {}).get('near_duplicate_fraction'), .0002)


class StateWriteTests(unittest.TestCase):
    def test_transient_replace_failure_recovers_with_complete_json(self):
        original = Path.replace
        failures = [True, True, False]
        def replace(source, destination):
            if failures.pop(0):
                raise PermissionError(13, 'Sharing conflict')
            return original(source, destination)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, 'replace', replace):
            session = Path(temp)
            process_latest.write_process_state(session, True)
            self.assertEqual(json.loads((session/'transcripts/process_state.json').read_text())['status'], 'complete')
            self.assertFalse(list((session/'transcripts').glob('*.tmp')))

    def test_status_write_failure_does_not_abort_processing_or_hide_live_progress(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(Path, 'replace', side_effect=PermissionError('Locked')), \
                mock.patch.object(process_latest, 'ACTIVE_SESSION', Path(temp)), \
                mock.patch.dict('os.environ', {'MNT_PROGRESS': '1'}), contextlib.redirect_stdout(io.StringIO()) as output:
            process_latest.progress('Still transcribing', stage='transcript')
            event = json.loads(output.getvalue())
            self.assertTrue(event['progress'].startswith('Still transcribing'))
            self.assertIn('Status file', event['progress'])
            self.assertFalse(list((Path(temp)/'transcripts').glob('*.tmp')))
