import json
import unittest

import numpy as np

from muter.voice import select_reference


SR = 16000


def voiced(seconds=3, amplitude=.08, rate=SR):
    t = np.arange(round(seconds * rate)) / rate
    envelope = .45 + .55 * np.sin(2 * np.pi * 2.4 * t) ** 2
    return (amplitude * envelope * (np.sin(2 * np.pi * 170 * t) + .3 * np.sin(2 * np.pi * 340 * t))).astype(np.float32)


class VoiceReferenceTests(unittest.TestCase):
    def test_empty_short_silent_and_nonfinite_are_rejected(self):
        for x in (np.array([], np.float32), np.ones(SR * 2, np.float32), np.zeros(SR * 8, np.float32),
                  np.full(SR * 3, np.nan, np.float32), np.full(SR * 3, np.inf, np.float32)):
            with self.subTest(length=len(x)), self.assertRaises(ValueError):
                select_reference(x)

    def test_stereo_invalid_sample_rate_and_dc_are_rejected(self):
        with self.assertRaises(ValueError):
            select_reference(np.zeros((SR * 4, 2), np.float32))
        for rate in (True, 0, 1.5, -16000):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                select_reference(voiced(), rate)
        with self.assertRaises(ValueError):
            select_reference(np.full(SR * 4, .2, np.float32))

    def test_long_leading_and_trailing_silence_do_not_win(self):
        source = np.concatenate([np.zeros(SR * 3), voiced(4), np.zeros(SR * 3)]).astype(np.float32)
        selected, report = select_reference(source)
        self.assertEqual(len(selected), 3 * SR)
        self.assertEqual(selected.dtype, np.float32)
        self.assertGreaterEqual(report["selected_start_seconds"], 2.9)
        self.assertLessEqual(report["selected_start_seconds"], 4.1)
        self.assertGreater(report["active_audio_seconds"], 2.8)
        start = round(report["selected_start_seconds"] * SR)
        np.testing.assert_array_equal(selected, source[start:start + SR * 3])

    def test_very_short_burst_is_not_enough(self):
        source = np.zeros(SR * 8, np.float32)
        source[SR:SR + SR // 3] = voiced(1 / 3)
        with self.assertRaises(ValueError):
            select_reference(source)

    def test_quiet_clean_audio_is_retained_with_warning(self):
        selected, report = select_reference(voiced(8, amplitude=.003))
        self.assertEqual(len(selected), SR * 3)
        self.assertTrue(any("较轻" in text for text in report["warnings"]))

    def test_all_clipped_is_rejected(self):
        source = np.tile(np.array([1.0, -1.0], np.float32), SR * 4)
        with self.assertRaises(ValueError):
            select_reference(source)

    def test_unclipped_segment_is_preferred_to_louder_clipped_segment(self):
        clipped = np.tile(np.array([1.0, -1.0], np.float32), SR * 2)
        selected, report = select_reference(np.concatenate([clipped, voiced(4)]))
        self.assertGreaterEqual(report["selected_start_seconds"], 4)
        self.assertEqual(report["selected_clipping_fraction"], 0)
        self.assertLess(np.max(np.abs(selected)), .2)

    def test_noise_pass_does_not_claim_identity_or_speech_verification(self):
        noise = np.random.default_rng(3).normal(0, .05, SR * 8).astype(np.float32)
        _, report = select_reference(noise)
        self.assertFalse(report["identity_verified"])
        self.assertFalse(report["speech_verified"])
        self.assertFalse(report["multiple_speakers_checked"])
        self.assertTrue(any("背景声" in text for text in report["warnings"]))
        self.assertTrue(any("噪声" in text for text in report["limitations"]))

    def test_exact_three_seconds_and_other_sample_rate(self):
        for rate in (16000, 22050, 48000):
            source = voiced(3, rate=rate)
            selected, report = select_reference(source, rate)
            self.assertEqual(len(selected), 3 * rate)
            self.assertEqual(report["selected_start_seconds"], 0)
            np.testing.assert_array_equal(selected, source)
            json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_long_input_is_bounded_and_not_modified(self):
        source = voiced(35)
        original = source.copy()
        selected, report = select_reference(source)
        self.assertEqual(report["analyzed_duration_seconds"], 30)
        self.assertEqual(report["input_duration_seconds"], 35)
        self.assertTrue(any("前 30 秒" in text for text in report["warnings"]))
        np.testing.assert_array_equal(source, original)
        selected[:] = 0
        np.testing.assert_array_equal(source, original)


if __name__ == "__main__":
    unittest.main()
