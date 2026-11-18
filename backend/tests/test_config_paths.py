import os
from pathlib import Path
import subprocess
import sys
import unittest


class ConfigPathTests(unittest.TestCase):
    def test_paths_are_identical_from_root_and_backend(self):
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ, PYTHONPATH=str(root / 'backend'),
                   DATABASE_PATH='./backend/db/college.db',
                   CHROMA_PERSIST_DIR='./backend/chroma_store')
        script = "from config import Config; print(Config.DATABASE_PATH); print(Config.CHROMA_PERSIST_DIR)"
        results = [subprocess.check_output([sys.executable, '-c', script], cwd=cwd, env=env, text=True).splitlines()
                   for cwd in [root, root / 'backend']]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], [str(root / 'backend' / 'db' / 'college.db'), str(root / 'backend' / 'chroma_store')])

    def test_absolute_path_is_preserved(self):
        root = Path(__file__).resolve().parents[2]
        target = str(root / 'custom.db')
        env = dict(os.environ, PYTHONPATH=str(root / 'backend'), DATABASE_PATH=target)
        result = subprocess.check_output([sys.executable, '-c', 'from config import Config; print(Config.DATABASE_PATH)'], env=env, text=True).strip()
        self.assertEqual(result, target)
