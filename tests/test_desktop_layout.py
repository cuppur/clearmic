import unittest

from muter.desktop_ui import CLOSING_MARKS, wrap_text
from muter.window_dpi import fit_window


class CharacterFont:
    measure = staticmethod(len)


class DesktopLayoutTests(unittest.TestCase):
    def test_unconstrained_sizes_are_logical_pixels(self):
        for dpi in (96, 120, 144, 192, 240):
            with self.subTest(dpi=dpi):
                scale, minimum, initial = fit_window(dpi, (0, 0, 3840, 2070))
                self.assertEqual(scale, dpi / 96)
                self.assertEqual(minimum, (round(680 * scale), round(540 * scale)))
                self.assertEqual(initial, (round(780 * scale), round(620 * scale)))

    def test_small_work_area_fits_startup_and_minimum(self):
        scale, minimum, initial = fit_window(240, (-1920, 0, 0, 980))
        self.assertLess(scale, 2.5)
        self.assertLessEqual(minimum[0], initial[0])
        self.assertLessEqual(minimum[1], initial[1])
        self.assertLessEqual(initial[0], 1920 - 50)
        self.assertLessEqual(initial[1], 980 - 50)

    def test_chinese_punctuation_stays_with_the_preceding_clause(self):
        text = "用本人声音样本区分说话人，适合日常会议。"
        for width in (6, 12, 18):
            with self.subTest(width=width):
                lines = wrap_text(text, CharacterFont(), width)
                self.assertEqual("".join(lines), text)
                self.assertTrue(all(len(line) <= width for line in lines))
                self.assertTrue(all(line[0] not in CLOSING_MARKS for line in lines))
                self.assertGreaterEqual(len(lines[-1]), 5)
