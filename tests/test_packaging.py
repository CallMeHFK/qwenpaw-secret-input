import json
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FATAL_SYMLINK = "FATAL: symlink found in runtime tree; refuse to build"


def run_build(cwd):
    return subprocess.run(["bash", "packaging/build_zip.sh"], cwd=cwd,
                          capture_output=True, text=True)


def load_manifest(path=ROOT / "plugin.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def copy_runtime_tree(src, dst):
    """复制脚本输入（packaging/、backend/、ui/、plugin.json），不碰共享工作树。"""
    shutil.copytree(src / "packaging", dst / "packaging")
    shutil.copytree(src / "backend", dst / "backend")
    shutil.copytree(src / "ui", dst / "ui")
    shutil.copy2(src / "plugin.json", dst / "plugin.json")


class PackagingTest(unittest.TestCase):
    def setUp(self):
        # 在运行时树的**临时副本**上构建（终审 Minor 13）：文档化的门禁不该往开发者的
        # checkout 里删/写 `dist/`。本文件另两个类早就是这么做的，只有这个类是例外。
        # 顺带把"布局断言不能靠上一版遗留的 zip 通过"变得更强 —— 副本里根本不存在旧产物。
        self.tmp = Path(tempfile.mkdtemp(prefix="secret-input-pkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        copy_runtime_tree(ROOT, self.tmp)
        started_ns = time.time_ns()
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.manifest = load_manifest()
        self.version = self.manifest["version"]
        self.plugin_id = self.manifest["id"]
        self.zip_path = self.tmp / "dist" / f"{self.plugin_id}-{self.version}.zip"
        self.assertTrue(self.zip_path.is_file(), str(self.zip_path))
        # 产物必须是本次构建写出的，不是某台机器上遗留的同名 zip。
        self.assertGreaterEqual(self.zip_path.stat().st_mtime_ns, started_ns)

    def test_single_top_level_dir_named_after_the_plugin_id(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            names = zf.namelist()
        tops = {n.split("/", 1)[0] for n in names if n}
        self.assertEqual(tops, {self.plugin_id})

    def test_plugin_json_is_found_by_both_install_paths(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            self.assertIn(f"{self.plugin_id}/plugin.json", zf.namelist())

    def test_ships_only_runtime_files(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            names = zf.namelist()
        for forbidden in ("tests/", "packaging/", ".github/", "docs/", "__pycache__"):
            self.assertFalse(any(forbidden in n for n in names), forbidden)
        self.assertTrue(any(n.endswith("backend/main.py") for n in names))
        self.assertTrue(any(n.endswith("ui/index.js") for n in names))

    def test_exact_member_set_is_manifest_plus_backend_py_plus_ui_js(self):
        # 反“只查黑名单”：任何额外成员（白名单外文件/目录内容）都必须失败。
        expected_files = {f"{self.plugin_id}/plugin.json",
                          f"{self.plugin_id}/ui/index.js"} | {
            f"{self.plugin_id}/{(p.relative_to(ROOT)).as_posix()}"
            for p in (ROOT / "backend").glob("*.py")
        }
        with zipfile.ZipFile(self.zip_path) as zf:
            members = {info.filename for info in zf.infolist()
                       if not info.is_dir()}
        self.assertEqual(members, expected_files)

    def test_no_symlink_members(self):
        # zipfile 不重建 symlink；成员落成"内容为目标路径的普通文件"是静默坏
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                self.assertNotEqual(mode, 0o120000, info.filename)

    def test_no_bytecode_members(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            for n in zf.namelist():
                self.assertFalse(n.endswith((".pyc", ".pyo")), n)


class SymlinkGateTest(unittest.TestCase):
    """symlink 门的红/绿对照：在运行时树的临时副本上跑，不碰共享工作树。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="secret-input-pkg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        copy_runtime_tree(ROOT, self.tmp)

    def assert_refused(self, proc):
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(FATAL_SYMLINK, proc.stderr)
        self.assertFalse((self.tmp / "dist").exists(),
                         "gate must refuse before any artifact is produced")

    def test_refuses_symlinks_in_runtime_dirs(self):
        cases = (("backend", "evil.py"), ("ui", "evil.js"),
                 ("backend/nested", "evil.py"))
        for runtime_dir, name in cases:
            with self.subTest(runtime_dir=runtime_dir):
                dest = self.tmp / runtime_dir
                dest.mkdir(parents=True, exist_ok=True)
                link = dest / name
                link.symlink_to("/etc/passwd")
                try:
                    proc = run_build(self.tmp)
                finally:
                    link.unlink()
                self.assert_refused(proc)

    def test_refuses_symlinked_plugin_json(self):
        manifest = self.tmp / "plugin.json"
        target = self.tmp / "plugin.json.real"
        manifest.rename(target)
        manifest.symlink_to(target)
        proc = run_build(self.tmp)
        self.assert_refused(proc)
        self.assertIn("plugin.json", proc.stderr)

    def test_builds_again_once_the_symlink_is_gone(self):
        link = self.tmp / "backend" / "evil.py"
        link.symlink_to("/etc/passwd")
        try:
            self.assert_refused(run_build(self.tmp))
        finally:
            link.unlink()
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = load_manifest(self.tmp / "plugin.json")
        zip_path = self.tmp / "dist" / f"{manifest['id']}-{manifest['version']}.zip"
        self.assertTrue(zip_path.is_file(), str(zip_path))
        with zipfile.ZipFile(zip_path) as zf:
            self.assertNotIn(f"{manifest['id']}/backend/evil.py", zf.namelist())


class VersionParseTest(unittest.TestCase):
    """版本必须取 JSON 顶层 version；嵌套同名键不得劫持产物名。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="secret-input-ver-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        copy_runtime_tree(ROOT, self.tmp)
        self.manifest_path = self.tmp / "plugin.json"
        self.manifest = load_manifest(self.manifest_path)

    def write_manifest(self, manifest):
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def test_nested_version_key_cannot_hijack_artifact_name(self):
        manifest = dict(self.manifest)
        manifest["meta"] = dict(manifest.get("meta") or {}, version="9.9.9")
        # 顶层 version 挪到嵌套键之后：sed 首个匹配会选中 meta.version。
        reordered = {k: v for k, v in manifest.items() if k != "version"}
        reordered["version"] = manifest["version"]
        self.write_manifest(reordered)
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = self.tmp / "dist" / \
            f"{self.manifest['id']}-{self.manifest['version']}.zip"
        self.assertEqual([p.name for p in (self.tmp / "dist").iterdir()],
                         [expected.name])

    def test_missing_version_fails(self):
        manifest = dict(self.manifest)
        manifest.pop("version")
        self.write_manifest(manifest)
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FATAL", proc.stderr)
        self.assertFalse((self.tmp / "dist").exists())

    def test_empty_version_fails(self):
        manifest = dict(self.manifest)
        manifest["version"] = "  "
        self.write_manifest(manifest)
        proc = run_build(self.tmp)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FATAL", proc.stderr)
        self.assertFalse((self.tmp / "dist").exists())


if __name__ == "__main__":
    unittest.main()
