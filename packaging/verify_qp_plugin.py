#!/usr/bin/env python3
"""Load a QwenPaw plugin under the real PluginLoader without touching the host.

Usage: python3 verify_qp_plugin.py <plugin-source-dir> [--config '{"k":"v"}'] [--keep]

Exit 0 on PASS, 1 on FAIL.
"""

import argparse
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
from pathlib import Path


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="plugin source directory (contains plugin.json)")
    ap.add_argument("--config", default="{}", help="JSON plugin config passed to PluginApi")
    ap.add_argument("--keep", action="store_true", help="keep the temp dir for inspection")
    args = ap.parse_args()

    src = Path(args.source).resolve()
    manifest_path = src / "plugin.json"
    if not manifest_path.is_file():
        fail(f"no plugin.json in {src}")
    try:
        manifest_json = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"plugin.json unreadable: {exc}")
    plugin_id = manifest_json.get("id")
    if not manifest_json.get("version"):
        fail("plugin.json has no version")

    # entry.backend must name a file that exists: the loader raises a bare
    # FileNotFoundError for it, which reads like a code bug, not a packaging bug.
    entry_backend = (manifest_json.get("entry") or {}).get("backend")
    if not entry_backend:
        fail("plugin.json declares no entry.backend")
    if not (src / entry_backend).is_file():
        fail(f"entry.backend file missing: {entry_backend}")

    # qwenpaw_version.max is EXCLUSIVE (>=min, <max) and upper-bound enforcement
    # is DISABLED in 2.2.1: a manifest whose max equals the host version loads
    # fine today (verified: enabled=True, diagnostics=[]), then gets rejected the
    # day enforcement returns. The host cannot warn you -- this gate is the only
    # thing that catches the landmine.
    qv = manifest_json.get("qwenpaw_version") or {}
    qmax = str(qv.get("max", ""))
    if qmax:
        if qmax.split(".")[0] not in ("1", "2"):
            fail(f"qwenpaw_version.max major must be 1 or 2, got {qmax}")
        from importlib.metadata import version as dist_version
        host_version = dist_version("qwenpaw")  # qwenpaw.__version__ does NOT exist
        if host_version.split(".")[:2] == qmax.split(".")[:2]:
            fail(f"qwenpaw_version.max {qmax} equals host {host_version} -> self-exclusion")
    print(f"manifest: id={plugin_id} version={manifest_json['version']} "
          f"window >={qv.get('min', '?')} <{qmax or '(open)'}")

    try:
        config = json.loads(args.config)
    except Exception as exc:
        fail(f"--config is not valid JSON: {exc}")

    import logging
    logging.disable(logging.ERROR)  # 宿主注册日志几十行，会淹掉判据行
    try:
        from qwenpaw.plugins.loader import PluginLoader
    except ImportError as exc:
        fail(f"cannot import qwenpaw ({exc}); run with the QwenPaw host python")

    tmp = Path(tempfile.mkdtemp(prefix="qpw-verify-"))
    plugins_dir = tmp / "plugins"
    plugins_dir.mkdir()
    dest = plugins_dir / plugin_id
    try:
        shutil.copytree(src, dest, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc",
                                                      "tests", ".github"))
    except Exception as exc:
        fail(f"copytree into temp plugins dir failed: {exc}")

    try:
        loader = PluginLoader([plugins_dir])
        discovered = loader.discover_plugins()
        # discover_plugins returns (manifest, path) tuples, not manifests.
        match = [(m, p) for m, p in discovered if m.id == plugin_id]
        if not match:
            fail(f"{plugin_id} not discovered in {plugins_dir} "
                 f"(found: {[m.id for m, _ in discovered]})")
        manifest, found_path = match[0]
        print(f"discovered: {manifest.id} {manifest.version} at {found_path}")

        if inspect.iscoroutinefunction(loader.load_plugin):
            record = asyncio.run(loader.load_plugin(manifest, found_path, config))
        else:
            record = loader.load_plugin(manifest, found_path, config)
        # A bare coroutine object here would be a silent false PASS.
        if inspect.iscoroutine(record):
            record.close()
            fail("load_plugin returned a coroutine; host API drifted from expected shape")

        if not getattr(record, "enabled", False):
            fail(f"record.enabled is False; diagnostics={record.diagnostics}")
        if record.instance is None:
            fail("record.instance is None; entry module exported no plugin object")
        if record.diagnostics:
            fail(f"loader reported diagnostics: {record.diagnostics}")

        # register_tool defers to a startup hook and the bare loader never runs
        # hooks, so the tool table is empty on record. The only observable proof
        # that tools were scheduled is the registry's deferred hook names.
        registry = getattr(loader, "registry", None)
        hooks = (registry.get_startup_hooks() or []) if registry else []
        print(f"loaded: id={record.manifest.id} version={record.manifest.version} "
              f"enabled={record.enabled} instance={type(record.instance).__name__}")
        # hook_name@priority only: the HookRegistration repr is 1000+ chars.
        print(f"scheduled startup hooks ({len(hooks)}): " + ", ".join(
            f"{getattr(h, 'hook_name', h)}@{getattr(h, 'priority', '?')}" for h in hooks))

        skills_src = dest / "skills"
        skill_names = sorted(p.name for p in skills_src.iterdir() if p.is_dir()) \
            if skills_src.is_dir() else []
        if skill_names:
            # A skill dir without SKILL.md makes the host log a warning, install
            # the plugin green, and land ZERO skills in every workspace.
            for sk in skill_names:
                if not (skills_src / sk / "SKILL.md").is_file():
                    fail(f"skills/{sk} has no SKILL.md (host only warns; skill silently lands as zero)")
            print(f"skill dirs in package ({len(skill_names)}): {skill_names}")
        print("PASS: plugin loads under the real PluginLoader (register(api) ran, "
              "startup hooks did not)")
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
    finally:
        if args.keep:
            print(f"kept temp dir: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
