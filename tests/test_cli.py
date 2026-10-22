"""CLI discovery checks; generated fixtures remain under output/verification."""
from pathlib import Path
import unittest
import uuid

from glb_tools.cli import discover


class DiscoveryTests(unittest.TestCase):
    def test_recursive_png_selection_excludes_thumbnails(self):
        root = Path(__file__).resolve().parent.parent / 'output/verification/unit' / uuid.uuid4().hex
        (root / 'nested').mkdir(parents=True)
        for name in ('b.png', 'a_thumb.PNG', 'not-image.jpg', 'nested/c.PNG'):
            (root / name).write_bytes(b'fixture')
        self.assertEqual(
            [p.relative_to(root).as_posix() for p in discover(root)],
            ['b.png', 'nested/c.PNG'])


if __name__ == '__main__':
    unittest.main()
