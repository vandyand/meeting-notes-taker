"""Focused tests for live audio preparation and local Moonshine inference."""
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class LiveAudioSegmentTests(unittest.TestCase):
    def test_pause_segments_keep_source_relative_boundaries_and_trim_silence(self):
        from live_audio_segments import segment_audio

        rng = np.random.default_rng(2)
        audio = np.concatenate([
            np.zeros(16000),
            rng.normal(0, .2, 5 * 16000),
            np.zeros(int(.7 * 16000)),
            rng.normal(0, .2, 2 * 16000),
            np.zeros(16000),
        ]).astype(np.float32)
        parts = segment_audio(audio, 16000, final=True)
        self.assertEqual(len(parts), 2)
        self.assertLess(parts[0].start_sample, 16000 * 1.3)
        self.assertGreater(parts[0].end_sample, 5 * 16000)
        self.assertLess(parts[0].end_sample, 6.5 * 16000)
        self.assertLess(len(parts[0].audio_16k), 6 * 16000)
        self.assertGreater(parts[1].start_sample, parts[0].end_sample)

    def test_continuous_speech_is_split_below_eight_seconds(self):
        from live_audio_segments import segment_audio

        audio = np.random.default_rng(4).normal(0, .1, 20 * 16000).astype(np.float32)
        parts = segment_audio(audio, 16000)
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(len(p.audio_16k) <= 8 * 16000 for p in parts))

    def test_48khz_input_is_resampled_but_boundaries_stay_source_samples(self):
        from live_audio_segments import segment_audio

        audio = np.random.default_rng(3).normal(0, .1, 9 * 48000).astype(np.float32)
        parts = segment_audio(audio, 48000, final=True)
        self.assertTrue(parts)
        self.assertTrue(all(p.source_sample_rate == 48000 for p in parts))
        self.assertTrue(all(len(p.audio_16k) <= 8 * 16000 for p in parts))
        self.assertTrue(all(p.start_sample % 3 == 0 and p.end_sample % 3 == 0 for p in parts))

    def test_two_tracks_segment_independently(self):
        from live_audio_segments import segment_audio

        a = np.random.default_rng(1).normal(0, .1, 5 * 16000).astype(np.float32)
        b = np.concatenate([np.zeros(16000), a])
        first, second = segment_audio(a, 16000, final=True), segment_audio(b, 16000, final=True)
        self.assertEqual(first[0].start_sample, 0)
        self.assertGreater(second[0].start_sample, 0)

    def test_growing_audio_does_not_emit_unfinished_tail(self):
        from live_audio_segments import segment_audio

        audio = np.random.default_rng(5).normal(0, .1, int(8.2 * 16000)).astype(np.float32)
        self.assertEqual(segment_audio(audio[:int(7.9 * 16000)], 16000), [])
        completed = segment_audio(audio, 16000)
        self.assertEqual(len(completed), 1)
        self.assertLessEqual(len(completed[0].audio_16k), 8 * 16000)
        self.assertEqual(len(segment_audio(audio, 16000, final=True)), 2)

    def test_short_meaningful_utterance_closes_at_natural_pause(self):
        from live_audio_segments import segment_audio

        rng = np.random.default_rng(6)
        audio = np.concatenate([
            rng.normal(0, .1, int(1.5 * 16000)),
            np.zeros(int(.6 * 16000)),
            rng.normal(0, .1, int(.5 * 16000)),
        ]).astype(np.float32)
        closed = segment_audio(audio, 16000)
        self.assertEqual(len(closed), 1)
        self.assertLess(len(closed[0].audio_16k), 2 * 16000)


class MoonshineLiveTests(unittest.TestCase):
    def test_fake_model_is_loaded_once_and_inference_is_serialized(self):
        from moonshine_live import MoonshineLive

        loads, active, peak = [], 0, 0
        guard = threading.Lock()

        class FakeModel:
            pass

        def infer(audio, model):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(.01)
            with guard:
                active -= 1
            return ["local result"]

        engine = MoonshineLive(model_factory=lambda name: loads.append(name) or FakeModel(),
                               transcribe_fn=infer)
        outputs = []
        workers = [threading.Thread(target=lambda: outputs.append(engine.transcribe(np.ones(3200, np.float32))))
                   for _ in range(4)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(len(loads), 1)
        self.assertEqual(peak, 1)
        self.assertEqual(outputs, ["local result"] * 4)

    def test_missing_dependency_and_model_cache_are_actionable_errors(self):
        from moonshine_live import MoonshineLive, MoonshineUnavailableError

        with self.assertRaisesRegex(MoonshineUnavailableError, "moonshine-onnx"):
            MoonshineLive._load_local_model("moonshine/base", importer=lambda _name: None)

        class FakeHub:
            @staticmethod
            def hf_hub_download(**kwargs):
                if not kwargs.get("local_files_only"):
                    raise AssertionError("must not access network")
                raise FileNotFoundError("not cached")

        with self.assertRaisesRegex(MoonshineUnavailableError, "cache"):
            MoonshineLive._model_directory("moonshine/base", hub=FakeHub)


if __name__ == "__main__":
    unittest.main()
