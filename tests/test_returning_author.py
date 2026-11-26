"""An independent delivery leaves a retained author's actual work resumable."""

import os
import unittest
from unittest import mock

import test_independent_publication as fixtures


class ReturningAuthorTest(unittest.TestCase):
    def test_returning_author_commits_retained_staging_without_losing_private_work(self):
        fixture = fixtures.IndependentPublicationTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.open_foreign_lane()
        fixture.dirty_foreign_lane()
        fixture.git('worktree', 'lock', '--reason', 'retain absent author work',
                    str(fixture.foreign_path))
        snapshot = fixture.foreign_snapshot()
        fixture.assert_independent_publication()
        self.assertEqual(fixture.foreign_snapshot(), snapshot)
        remote = fixture.remote_tips()
        with mock.patch.dict(os.environ, {'SYNCWHEEL_LANE_OWNER': fixture.foreign_owner}):
            fixture.git('-C', str(fixture.foreign_path), 'commit', '-qm', 'resume retained staging')
        self.assertEqual(fixture.git('-C', str(fixture.foreign_path), 'rev-parse', 'HEAD^'),
                         snapshot['head'])
        staged = fixture.git('-C', str(fixture.foreign_path), 'show', 'HEAD:alpha.txt')
        self.assertEqual(staged, 'staged foreign bytes\x00')
        self.assertEqual((fixture.foreign_path / 'alpha.txt').read_bytes(),
                         b'unstaged foreign bytes\x00\n')
        for relative in ('draft.bin', 'cache/retained.bin'):
            self.assertEqual((fixture.foreign_path / relative).read_bytes(),
                             snapshot['files'][relative][1])
        self.assertTrue((fixture.foreign_path / 'cache/source-link').is_symlink())
        self.assertEqual(fixture.foreign_record(), snapshot['registry_record'])
        self.assertEqual(fixture.remote_tips(), remote)


if __name__ == '__main__':
    unittest.main()
