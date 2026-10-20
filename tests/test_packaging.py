import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_build(cwd):
    return subprocess.run(["bash", "packaging/build_zip.sh"], cwd=cwd,
                          capture_output=True, text=True)


class PackagingTest(unittest.TestCase):
    def setUp(self):
        proc = run_build(ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        version = json.loads((ROOT / "plugin.json").read_text())["version"]
        self.zip_path = ROOT / "dist" / f"secret-input-{version}.zip"
        self.assertTrue(self.zip_path.is_file(), str(self.zip_path))

    def test_single_top_level_dir_named_after_the_plugin_id(self):
        names = zipfile.ZipFile(self.zip_path).namelist()
        tops = {n.split("/")[0] for n in names if n}
        self.assertEqual(tops, {"secret-input"})

    def test_plugin_json_is_found_by_both_install_paths(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            self.assertIn("secret-input/plugin.json", zf.namelist())

    def test_ships_only_runtime_files(self):
        names = zipfile.ZipFile(self.zip_path).namelist()
        for forbidden in ("tests/", "packaging/", ".github/", "docs/", "__pycache__"):
            self.assertFalse(any(forbidden in n for n in names), forbidden)
        self.assertTrue(any(n.endswith("backend/main.py") for n in names))
        self.assertTrue(any(n.endswith("ui/index.js") for n in names))

    def test_no_symlink_members(self):
        # zipfile 不重建 symlink；成员落成"内容为目标路径的普通文件"是静默坏
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                self.assertNotEqual(mode, 0o120000, info.filename)

    def test_no_pyc_or_stale_build(self):
        for n in zipfile.ZipFile(self.zip_path).namelist():
            self.assertFalse(n.endswith((".pyc", ".pyo")), n)


class SymlinkGateTest(unittest.TestCase):
    """symlink 门的红/绿对照：在运行时树的临时副本上跑，不碰共享工作树。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="secret-input-pkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(ROOT / "packaging", self.tmp / "packaging")
        shutil.copytree(ROOT / "backend", self.tmp / "backend")
        shutil.copytree(ROOT / "ui", self.tmp / "ui")
        shutil.copy2(ROOT / "plugin.json", self.tmp / "plugin.json")
        self.link = self.tmp / "backend" / "evil.py"

    def test_refuses_to_build_when_a_symlink_exists(self):
        for runtime_dir in ("backend", "ui"):
            with self.subTest(runtime_dir=runtime_dir):
                link = self.tmp / runtime_dir / "evil.py"
                link.symlink_to("/etc/passwd")
                proc = run_build(self.tmp)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("FATAL: symlink found in runtime tree; refuse to build",
                              proc.stderr)
                link.unlink()

    def test_builds_again_once_the_symlink_is_gone(self):
        self.link.symlink_to("/etc/passwd")
        self.assertNotEqual(run_build(self.tmp).returncode, 0)
        self.link.unlink()
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        version = json.loads((self.tmp / "plugin.json").read_text())["version"]
        zip_path = self.tmp / "dist" / f"secret-input-{version}.zip"
        self.assertTrue(zip_path.is_file(), str(zip_path))
        with zipfile.ZipFile(zip_path) as zf:
            self.assertNotIn("secret-input/backend/evil.py", zf.namelist())


if __name__ == "__main__":
    unittest.main()
