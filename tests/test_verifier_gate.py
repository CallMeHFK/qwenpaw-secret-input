"""spec §12/§14 判据 1 缺的那半边：冻结 verifier 的负对照（终审 Important 6）。

`packaging/verify_qp_plugin.py` 是 sha256 钉死的**证据脚本**，不是产品代码，不许改。
但在本文件出现之前，"sha256-pinned" 这句话在 CONTRIBUTING / 两份 README / pyproject
四处被宣称、**零处被强制**（仓库里没有任何哈希、测试或 CI 步），而 CONTRIBUTING 自己
写着"不会红的门不算门"。同时 spec §12 要求 `test_packaging.py` 调这个 verifier 并跑
其中 7 条负对照 —— 那个文件实际只测了 `build_zip.sh`（计划 Task 11 悄悄缩了范围）。

这里补齐：8 条负对照各自在**临时副本**上真跑一次 verifier 子进程，断言 rc=1 且 FAIL
文案对得上；再加一条正向对照（未突变的副本必须 rc=0 + PASS），否则"verifier 恒红"
也能让 8 条负对照全绿。哈希本身也在这里钉住 —— 改它一个字节就红。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "packaging" / "verify_qp_plugin.py"
# 钉死的哈希：这个脚本是判据本身，判据不能被它要判的对象悄悄改掉。
VERIFIER_SHA256 = "2182bc4d2150b91ff6d623338cce4fafadaf4c65d122352589cfed212509f18a"
RUNTIME_PARTS = ("plugin.json", "backend", "ui")
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _copy_runtime_tree(dest: Path) -> None:
    for part in RUNTIME_PARTS:
        src = ROOT / part
        target = dest / part
        if src.is_dir():
            shutil.copytree(src, target, ignore=_IGNORE)
        else:
            shutil.copy2(src, target)


def _manifest(dest: Path) -> dict:
    return json.loads((dest / "plugin.json").read_text(encoding="utf-8"))


def _write_manifest(dest: Path, data: dict) -> None:
    (dest / "plugin.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                      encoding="utf-8")


class VerifierGateTest(unittest.TestCase):
    def _run(self, mutate=None) -> tuple[int, str]:
        """在临时副本上跑真 verifier；`mutate(dest)` 负责把副本弄坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "secret-input"
            dest.mkdir()
            _copy_runtime_tree(dest)
            if mutate is not None:
                mutate(dest)
            # 真 PluginLoader 会往 root logger 打 INFO/ERROR，只会污染套件输出。
            previous = logging.root.manager.disable
            logging.disable(logging.ERROR)
            try:
                proc = subprocess.run([sys.executable, str(VERIFIER), str(dest)],
                                      capture_output=True, text=True, timeout=300)
            finally:
                logging.disable(previous)
            return proc.returncode, proc.stdout + proc.stderr

    def _assert_fails(self, mutate, expected: str) -> None:
        rc, out = self._run(mutate)
        self.assertEqual(rc, 1, out[-1500:])
        self.assertIn(expected, out)

    # ---- 判据本身不许被悄悄换掉 -------------------------------------------

    def test_the_verifier_is_byte_identical_to_the_pinned_hash(self):
        digest = hashlib.sha256(VERIFIER.read_bytes()).hexdigest()
        self.assertEqual(
            digest, VERIFIER_SHA256,
            "packaging/verify_qp_plugin.py 被改动了。它是**证据**不是产品代码："
            "改判据来让门变绿等于没有门。要改就同时改这里的哈希并在 commit message 里说明理由。")

    # ---- 8 条负对照（spec §14 判据 1）-------------------------------------

    def test_nc1_missing_manifest_fails(self):
        self._assert_fails(lambda d: (d / "plugin.json").unlink(), "no plugin.json in")

    def test_nc2_unreadable_manifest_fails(self):
        self._assert_fails(
            lambda d: (d / "plugin.json").write_text("{ not json", encoding="utf-8"),
            "plugin.json unreadable")

    def test_nc3_missing_version_fails(self):
        def drop_version(dest):
            m = _manifest(dest)
            m.pop("version")
            _write_manifest(dest, m)

        self._assert_fails(drop_version, "plugin.json has no version")

    def test_nc4_missing_entry_backend_fails(self):
        def drop_entry(dest):
            m = _manifest(dest)
            m["entry"] = {"frontend": "ui/index.js"}
            _write_manifest(dest, m)

        self._assert_fails(drop_entry, "plugin.json declares no entry.backend")

    def test_nc5_entry_backend_points_at_a_missing_file(self):
        def repoint(dest):
            m = _manifest(dest)
            m["entry"]["backend"] = "backend/does_not_exist.py"
            _write_manifest(dest, m)

        self._assert_fails(repoint, "entry.backend file missing")

    def test_nc6_version_window_major_out_of_range_fails(self):
        def bad_major(dest):
            m = _manifest(dest)
            m["qwenpaw_version"]["max"] = "3.0.0"
            _write_manifest(dest, m)

        self._assert_fails(bad_major, "max major must be 1 or 2")

    def test_nc7_self_excluding_max_fails(self):
        # 宿主抓不到这颗地雷（2.2.1 起上限校验停用），verifier 是唯一一道门：
        # max 是**右开**区间，填宿主自己的版本 = 今天能装、 enforcement 回来那天自我排除。
        from importlib.metadata import version as dist_version

        host = dist_version("qwenpaw")

        def self_exclude(dest):
            m = _manifest(dest)
            m["qwenpaw_version"]["max"] = host
            _write_manifest(dest, m)

        self._assert_fails(self_exclude, "self-exclusion")

    def test_nc8_register_raising_is_reported_not_swallowed(self):
        def boom(dest):
            main = dest / "backend" / "main.py"
            main.write_text(main.read_text(encoding="utf-8")
                            + "\nraise RuntimeError('verifier negative control')\n",
                            encoding="utf-8")

        rc, out = self._run(boom)
        self.assertEqual(rc, 1, out[-1500:])
        self.assertIn("FAIL", out)

    # ---- 正向对照：没有这一条，"verifier 恒红"也能让上面 8 条全绿 ----------

    def test_positive_control_unmutated_tree_passes(self):
        rc, out = self._run()
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn("PASS", out)


if __name__ == "__main__":
    unittest.main()
