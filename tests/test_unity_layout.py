"""Synthetic metric and hierarchy checks without fonts or Unity runtime."""
import importlib.util
from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'profiles/sdk-unity-2022.3/tools'
spec = importlib.util.spec_from_file_location('synthetic_layout', TOOLS / 'unity_layout.py')
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.metrics = m.FontMetrics(1000, 1200, {ord(ch): 500 for ch in 'SOURCE_TARG <b>/\t'}, 1000)

    def test_nested_anchor_hierarchy(self):
        rectangles = {'canvas': m.Rect(), 'panel': m.Rect('canvas', (0, 0), (.5, .5), (-10, -20)),
                      'label': m.Rect('panel', (0, 0), (1, 1), (-10, 0))}
        self.assertEqual(m.resolve_rect_size(rectangles, 'label', (200, 200)), (80, 80))

    def test_root_is_canvas_reference(self):
        self.assertEqual(m.resolve_rect_size({'root': m.Rect(size_delta=(1, 1))}, 'root', (200, 100)), (200, 100))

    def test_missing_cycle_and_invalid_dimensions(self):
        for rectangles, node, code in [({'a': m.Rect('b')}, 'a', 'RECT_BINDING_MISSING'),
                ({'a': m.Rect('b'), 'b': m.Rect('a')}, 'a', 'RECT_HIERARCHY_CYCLE'),
                ({'root': m.Rect(), 'a': m.Rect('root', size_delta=(-999, 0))}, 'a', 'LAYOUT_DIMENSIONS_INVALID'),
                ({'root': m.Rect(anchor_min=(float('nan'), 0))}, 'root', 'RECT_VALUES_INVALID')]:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                m.resolve_rect_size(rectangles, node, (200, 100))

    def test_explicit_newlines_and_wrapping(self):
        result = m.screen_text('SOURCE\nTARGET', (15, 100), self.metrics, m.TextLayout(10))
        self.assertEqual(result['measurement']['line_count'], 4)
        self.assertEqual(result['measurement']['max_line_width'], 15)
        self.assertFalse(result['conservative_overflow'])
        self.assertFalse(result['runtime_verified'])

    def test_unwrapped_width_and_vertical_overflow(self):
        result = m.screen_text('SOURCE', (15, 10), self.metrics, m.TextLayout(10, wrap=False))
        self.assertTrue(result['conservative_overflow'])
        self.assertEqual(result['measurement']['max_line_width'], 30)
        result = m.screen_text('SOURCE\nTARGET', (100, 10), self.metrics,
                               m.TextLayout(10, truncate_vertical=False))
        self.assertFalse(result['conservative_overflow'])

    def test_best_fit_selects_largest_fitting_size(self):
        result = m.screen_text('SOURCE', (24, 100), self.metrics,
                               m.TextLayout(10, wrap=False, best_fit=True, best_fit_min=4, best_fit_max=10))
        self.assertEqual(result['font_size_tested'], 8)
        self.assertFalse(result['conservative_overflow'])

    def test_best_fit_exhaustion_reports_minimum_overflow(self):
        result = m.screen_text('SOURCE', (1, 1), self.metrics,
                               m.TextLayout(10, best_fit=True, best_fit_min=4, best_fit_max=10))
        self.assertEqual(result['font_size_tested'], 4)
        self.assertTrue(result['conservative_overflow'])

    def test_missing_glyphs_and_optional_markup_stripping(self):
        result = m.screen_text('<b>SOURCE_Z</b>', (100, 100), self.metrics,
                               m.TextLayout(10), strip_rich_text=True)
        self.assertEqual(result['missing_codepoints'], [ord('Z')])
        self.assertTrue(result['rich_markup_present'])
        self.assertEqual(result['measurement']['max_line_width'], 45)

    def test_invalid_font_metrics_sizes_and_canvas_rejected(self):
        for metrics, layout, dimensions, code in [
                (m.FontMetrics(0, 1, {}, 1), m.TextLayout(10), (1, 1), 'FONT_METRICS_INVALID'),
                (m.FontMetrics(1, 1, {65: -1}, 1), m.TextLayout(10), (1, 1), 'FONT_METRICS_INVALID'),
                (self.metrics, m.TextLayout(0), (1, 1), 'FONT_SIZE_INVALID'),
                (self.metrics, m.TextLayout(10, best_fit=True, best_fit_min=8, best_fit_max=4), (1, 1), 'FONT_SIZE_INVALID'),
                (self.metrics, m.TextLayout(10), (float('nan'), 1), 'LAYOUT_DIMENSIONS_INVALID')]:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                m.screen_text('SOURCE', dimensions, metrics, layout)

    def test_blank_paragraph_and_crlf_preserved(self):
        result = m.screen_text('SOURCE\r\n\r\nTARGET', (100, 100), self.metrics, m.TextLayout(10))
        self.assertEqual(result['measurement']['line_count'], 3)


if __name__ == '__main__':
    unittest.main()
