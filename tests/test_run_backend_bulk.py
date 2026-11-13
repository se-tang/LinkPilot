"""发起测试：后端全选、反选与已选数量提示。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackendBulkSelectionTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.js = (ROOT / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        self.css = (ROOT / 'app' / 'static' / 'app.css').read_text(encoding='utf-8')

    def test_controls_are_rendered(self):
        self.assertIn('id="selectAllBackends"', self.html)
        self.assertIn('id="invertBackends"', self.html)
        self.assertIn('id="backendSelectionSummary"', self.html)
        self.assertIn('id="sSelectAllBackends"', self.html)
        self.assertIn('id="sInvertBackends"', self.html)
        self.assertIn('id="sBackendSelectionSummary"', self.html)

    def test_bulk_actions_update_checkboxes_and_summary(self):
        self.assertIn('function updateBackendSelection()', self.js)
        self.assertIn('function setBackendSelection(invert = false)', self.js)
        self.assertIn('selectAllBackends: () => setBackendSelection(false)', self.js)
        self.assertIn('invertBackends: () => setBackendSelection(true)', self.js)
        self.assertIn('selectAllSchedBackends: () => setSchedBackendSelection(false)', self.js)
        self.assertIn('invertSchedBackends: () => setSchedBackendSelection(true)', self.js)
        self.assertIn('updateBackendSelection();', self.js)
        self.assertIn('updateIpv6Ui();', self.js)

    def test_compact_layout_has_styles(self):
        self.assertIn('.run-backend-head{', self.css)
        self.assertIn('.run-backend-actions{', self.css)


if __name__ == '__main__':
    unittest.main(verbosity=2)
