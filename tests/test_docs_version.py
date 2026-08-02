import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / 'docs' / 'sync_version.py'
SPEC = importlib.util.spec_from_file_location('sync_version', MODULE_PATH)
SYNC_VERSION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC_VERSION)


class DocsVersionTest(unittest.TestCase):
    def test_checked_in_site_matches_root_version(self):
        version = SYNC_VERSION.normalize_version((REPO_ROOT / 'VERSION').read_text())
        html = (REPO_ROOT / 'docs' / 'index.html').read_text()

        self.assertEqual(SYNC_VERSION.render_html(html, version), html)

    def test_render_updates_every_marked_version(self):
        marker = SYNC_VERSION.START_MARKER + 'old' + SYNC_VERSION.END_MARKER
        html = f'hero {marker} footer {marker}'

        rendered = SYNC_VERSION.render_html(html, '1.2.3')

        self.assertEqual(rendered.count('1.2.3'), 2)
        self.assertNotIn('old', rendered)

    def test_render_rejects_missing_marker(self):
        marker = SYNC_VERSION.START_MARKER + 'old' + SYNC_VERSION.END_MARKER

        with self.assertRaisesRegex(SYNC_VERSION.VersionSyncError, 'expected 2'):
            SYNC_VERSION.render_html(f'only {marker}', '1.2.3')


if __name__ == '__main__':
    unittest.main()
