import unittest

import numpy as np

from tools.evaluate_scenarios import ReferencePrimedStream, active_mask, fidelity_scenarios, gate_variant_summary, leakage_metrics, probability_summary, score_scene, target_metrics


class MetricTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(71)
        self.target = rng.normal(0, .1, 16000).astype(np.float32)

    def test_silent_output_never_looks_like_target_preservation(self):
        m = target_metrics(np.zeros_like(self.target), self.target)
        self.assertIsNone(m["si_sdr_db"])
        self.assertFalse(m["output_present"])
        self.assertLess(m["target_gain_db"], -100)
        self.assertAlmostEqual(m["scale_sensitive_snr_db"], 0, places=6)
        self.assertTrue(m["target_attenuated_over_9db"])

    def test_si_sdr_alone_cannot_hide_large_gain_loss(self):
        m = target_metrics(self.target * .01, self.target)
        self.assertGreater(m["si_sdr_db"], 50)
        self.assertAlmostEqual(m["target_gain_db"], -40, places=4)
        self.assertTrue(m["target_attenuated_over_9db"])
        self.assertLess(m["scale_sensitive_snr_db"], 1)

    def test_clean_output_scores_well(self):
        m = target_metrics(self.target, self.target)
        self.assertGreater(m["si_sdr_db"], 80)
        self.assertGreater(m["scale_sensitive_snr_db"], 80)
        self.assertAlmostEqual(m["target_gain_db"], 0, places=6)
        self.assertTrue(m["output_present"])

    def test_silent_reference_is_not_a_target_metric(self):
        with self.assertRaises(ValueError):
            target_metrics(self.target, np.zeros_like(self.target))

    def test_noise_removal_and_target_preservation_are_separate(self):
        mask = np.arange(len(self.target)) < 8000
        target, other = self.target.copy(), self.target[::-1].copy()
        target[~mask] = 0
        other[mask] = 0
        mix = target + other
        report = score_scene(np.zeros_like(mix), mix, target, other, np.zeros_like(mix))
        self.assertGreater(report["target_absent_leakage"]["attenuation_db"], 70)
        self.assertFalse(report["target"]["output_present"])
        self.assertTrue(report["target"]["target_attenuated_over_9db"])

    def test_low_volume_target_remains_in_activity_mask(self):
        quiet = self.target * .001
        self.assertTrue(active_mask(quiet).all())
        self.assertFalse(active_mask(np.zeros(16000)).any())

    def test_leakage_db_has_expected_direction(self):
        m = leakage_metrics(self.target * .1, self.target)
        self.assertAlmostEqual(m["attenuation_db"], 20, places=5)

    def test_nonfinite_metrics_rejected(self):
        bad = self.target.copy()
        bad[0] = np.nan
        with self.assertRaises(ValueError):
            target_metrics(bad, self.target)

    def test_fidelity_scenarios_do_not_include_a_competing_speaker(self):
        source = [{"target": self.target}, {"noise": np.full_like(self.target, .01)}]
        scenes = fidelity_scenarios(source)
        self.assertEqual([s["name"] for s in scenes], ["clean_speech", "light_noise_speech", "quiet_speech_light_noise"])
        for i, scene in enumerate(scenes):
            self.assertTrue(np.all(scene["other"] == 0))
            np.testing.assert_array_equal(scene["target"], self.target if i < 2 else self.target * .25)
        np.testing.assert_array_equal(scenes[0]["mixture"], self.target)
        self.assertGreater(rms := float(np.sqrt(np.mean(scenes[1]["noise"] ** 2))), 0)
        self.assertLess(rms, .01)


class PrimingBoundaryTests(unittest.TestCase):
    class DelayedIdentity:
        def __init__(self):
            self.n_in = self.n_out = 0
            self.pending = np.zeros(0, np.float32)

        def push(self, x):
            self.n_in += len(x)
            self.pending = np.concatenate([self.pending, x])
            n = max(0, len(self.pending) - 3)
            y, self.pending = self.pending[:n], self.pending[n:]
            self.n_out += len(y)
            return y

        def flush(self):
            y, self.pending = self.pending, np.zeros(0, np.float32)
            self.n_out += len(y)
            return y

    def test_primer_tail_never_enters_actual_output(self):
        wrapped = ReferencePrimedStream(self.DelayedIdentity(), np.full(10, 99, np.float32))
        self.assertEqual(wrapped.held_primer_samples, 3)
        chunks = [np.array([1], np.float32), np.array([2, 3], np.float32), np.array([4, 5], np.float32)]
        out = [wrapped.push(chunk) for chunk in chunks] + [wrapped.flush()]
        np.testing.assert_array_equal(np.concatenate(out), [1, 2, 3, 4, 5])

    def test_call_shorter_than_primer_tail_keeps_exact_length(self):
        wrapped = ReferencePrimedStream(self.DelayedIdentity(), np.full(10, 99, np.float32))
        out = np.concatenate([wrapped.push(np.array([1], np.float32)), wrapped.flush()])
        np.testing.assert_array_equal(out, [1])


class DetectorSummaryTests(unittest.TestCase):
    def test_target_rejection_and_other_acceptance_are_separate(self):
        rows = [
            {"start_seconds": 0.0, "end_seconds": .16, "probability": .2, "processing_ms": 10,
             "target_present": True, "other_present": False, "target_active_fraction": 1., "other_active_fraction": 0.},
            {"start_seconds": 1.12, "end_seconds": 1.28, "probability": .6, "processing_ms": 20,
             "target_present": False, "other_present": True, "target_active_fraction": 0., "other_active_fraction": .5},
        ]
        result = probability_summary(rows)
        low = result["all"]["thresholds"]["0.5"]
        high = result["all"]["thresholds"]["0.65"]
        self.assertEqual(low["target_rejected_chunks"], 1)
        self.assertEqual(low["other_accepted_chunks"], 1)
        self.assertEqual(high["target_rejected_chunks"], 1)
        self.assertEqual(high["other_accepted_chunks"], 0)
        self.assertAlmostEqual(low["other_accepted_active_seconds"], .08)
        self.assertEqual(result["initial_1s"]["chunks"], 1)
        self.assertEqual(result["stable_after_1s"]["chunks"], 1)

    def test_dilation_trades_rejection_for_more_open_frames(self):
        rows = [{"start_seconds": i * .16, "end_seconds": (i + 1) * .16, "probability": p,
                 "processing_ms": 1, "target_present": i < 2, "other_present": i >= 2,
                 "target_active_fraction": float(i < 2), "other_active_fraction": float(i >= 2)}
                for i, p in enumerate((.1, .9, .1, .1))]
        report = gate_variant_summary(rows)["0.65"]
        self.assertEqual(report["base"]["all"]["target_rejected_chunks"], 1)
        self.assertEqual(report["one_frame_dilation"]["all"]["target_rejected_chunks"], 0)
        self.assertEqual(report["base"]["all"]["other_accepted_chunks"], 0)
        self.assertEqual(report["one_frame_dilation"]["all"]["other_accepted_chunks"], 1)


if __name__ == "__main__":
    unittest.main()
