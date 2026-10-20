# -*- coding: utf-8 -*-
"""Assembly gates: all nine extension points, per-registration swallowing, and
the real ``PluginLoader`` path (Task 1 carry-ins)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.main import SecretInputPlugin, _write_env_via_host_store  # noqa: E402

# Task 1 carry-in: the old boot hook is gone; the sweep is the observable one.
EXPECTED_CALL = ("secret_input_sweep", 100)
REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingApi:
    """Stub PluginApi recording every registration the plugin makes."""

    def __init__(self, config=None, manifest=None) -> None:
        self.config = config if config is not None else {}
        self.manifest = manifest if manifest is not None else {}
        self.tools: dict[str, dict] = {}
        self.router = None
        self.middleware = None
        self.prompt = None
        self.startup: list[tuple[str, int]] = []
        self.shutdown: list[tuple[str, int]] = []
        self.uninstall: list[tuple[str, int]] = []

    def register_tool(self, tool_name, tool_func, description="", icon="",  # noqa: ANN001
                      enabled=False, tool_type="", target_param=""):
        self.tools[tool_name] = {"func": tool_func, "description": description, "icon": icon,
                                 "enabled": enabled, "tool_type": tool_type,
                                 "target_param": target_param}

    def register_http_router(self, router, *, prefix, tags=None):  # noqa: ANN001
        self.router = {"router": router, "prefix": prefix, "tags": tags}

    def register_middleware(self, middleware_factory, *, priority=100):  # noqa: ANN001
        self.middleware = {"factory": middleware_factory, "priority": priority}

    def register_prompt_section(self, name, after, provider, *, priority=100,  # noqa: ANN001
                                condition=None, agent_id=None):
        self.prompt = {"name": name, "after": after, "provider": provider,
                       "priority": priority, "condition": condition, "agent_id": agent_id}

    def register_startup_hook(self, hook_name, callback, priority=100):  # noqa: ANN001
        self.startup.append((hook_name, priority))

    def register_shutdown_hook(self, hook_name, callback, priority=100):  # noqa: ANN001
        self.shutdown.append((hook_name, priority))

    def register_uninstall_hook(self, hook_name, callback, priority=100):  # noqa: ANN001
        self.uninstall.append((hook_name, priority))


class ExplodingPromptApi(RecordingApi):
    """One extension point fails; the other eight must still land."""

    def register_prompt_section(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("host signature drifted")


class RegistrationTest(unittest.TestCase):
    def test_register_wires_all_nine_extension_points_without_swallowing_anything(self):
        api = RecordingApi(config={"ref_ttl_seconds": 42, "answer_timeout_seconds": 45})
        plugin = SecretInputPlugin()
        with self.assertNoLogs("backend.main", level="ERROR"):
            plugin.register(api)

        self.assertIs(plugin.api, api)
        self.assertEqual(plugin.config.ref_ttl_seconds, 42)
        self.assertEqual(plugin.config.answer_timeout_seconds, 45)

        self.assertEqual(set(api.tools), {"request_secret", "secret_exec", "secret_export"})
        for name, record in api.tools.items():
            with self.subTest(tool=name):
                self.assertFalse(record["enabled"])
                self.assertTrue(record["description"])
        self.assertEqual(api.tools["secret_exec"]["tool_type"], "shell")
        self.assertEqual(api.tools["secret_exec"]["target_param"], "command")
        self.assertEqual(api.tools["request_secret"]["tool_type"], "internal")
        self.assertEqual(api.tools["secret_export"]["tool_type"], "internal")
        # R44: the model-visible description has to explain that 0/omitted is not "instantly".
        self.assertIn("Omit timeout_seconds", api.tools["request_secret"]["description"])
        self.assertIn("operator-configured default",
                      api.tools["request_secret"]["description"])

        self.assertEqual(api.router["prefix"], "/secret-input")
        self.assertEqual(api.router["tags"], ["secret-input"])
        self.assertEqual(api.middleware["priority"], 50)
        self.assertEqual(api.prompt["name"], "secret_input")
        self.assertEqual(api.prompt["after"], "env_context")
        self.assertIn("request_secret", api.prompt["provider"](None))

        self.assertEqual(api.startup, [EXPECTED_CALL])
        self.assertEqual(api.shutdown, [("secret_input_shutdown", 100)])
        self.assertEqual(api.uninstall, [("secret_input_uninstall", 100)])
        self.assertIs(plugin.tools._env_writer, _write_env_via_host_store)

    def test_registration_failure_is_swallowed_but_logged_as_error(self):
        # 刻意保留 except：一处登记失败不能带崩整包加载；但它必须留下 ERROR + traceback
        api = ExplodingPromptApi()
        with self.assertLogs("backend.main", level="ERROR") as logged:
            SecretInputPlugin().register(api)  # 不抛
        output = "\n".join(logged.output)
        self.assertIn("a registration failed", output)
        self.assertIn("RuntimeError: host signature drifted", output)
        self.assertIsNone(api.prompt)
        # 失败的只此一处：其余八路都在
        self.assertEqual(set(api.tools), {"request_secret", "secret_exec", "secret_export"})
        self.assertEqual(api.router["prefix"], "/secret-input")
        self.assertEqual(api.middleware["priority"], 50)
        self.assertEqual(api.startup, [EXPECTED_CALL])

    def test_missing_host_attribute_is_swallowed_but_logged_as_error(self):
        # 最可能的真机漂移：宿主的全部 register_* 都不在
        with self.assertLogs("backend.main", level="ERROR") as logged:
            SecretInputPlugin().register(object())  # 不抛
        self.assertIn("AttributeError", "\n".join(logged.output))
        self.assertEqual(len(logged.records), 9)


class ManifestToolListTest(unittest.TestCase):
    """R55: hot install syncs tools into every agent config only from
    ``meta.tools[]`` (``_sync_plugin_tools_to_agents``); without it the three
    tools stay invisible in ``/api/tools`` until a host restart."""

    def test_meta_tools_names_match_the_registered_tools(self):
        from backend.main import TOOL_DESCRIPTIONS

        manifest = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        names = [t.get("name") for t in manifest["meta"]["tools"] if isinstance(t, dict)]
        self.assertEqual(sorted(names), sorted(TOOL_DESCRIPTIONS))


class HostLogAdoptionTest(unittest.TestCase):
    """R56: the host's file handler lives on the ``qwenpaw`` logger with
    ``propagate = False``, so ``plugin_*`` records never reach qwenpaw.log."""

    def tearDown(self):
        own = logging.getLogger("plugin_testhost")
        for handler in list(own.handlers):
            own.removeHandler(handler)
        own.setLevel(logging.NOTSET)
        own.propagate = True
        host = logging.getLogger("hosttest")
        for handler in list(host.handlers):
            host.removeHandler(handler)
        host.setLevel(logging.NOTSET)

    def test_adopts_host_handlers_once_and_enables_info(self):
        from unittest import mock

        from backend import main as main_module

        host = logging.getLogger("hosttest")
        host_handler = logging.NullHandler()
        host.addHandler(host_handler)
        with mock.patch("qwenpaw.utils.logging.LOG_NAMESPACE", "hosttest"), \
                mock.patch.object(main_module, "PLUGIN_LOGGER_NAME", "plugin_testhost"):
            main_module._adopt_host_log_handlers()
            main_module._adopt_host_log_handlers()

        own = logging.getLogger("plugin_testhost")
        self.assertEqual(own.handlers.count(host_handler), 1)
        self.assertTrue(own.isEnabledFor(logging.INFO))
        self.assertFalse(own.propagate)

    def test_direct_imports_do_not_touch_process_handlers(self):
        from backend import main as main_module

        # Under ``unittest discover`` the module top-level is ``backend``, not
        # ``plugin_secret_input``: the guard must leave logging untouched.
        self.assertFalse(main_module.PLUGIN_LOGGER_NAME.startswith("plugin_"))
        main_module._adopt_host_log_handlers()
        self.assertTrue(logging.getLogger("backend").propagate)


class RealPluginLoaderTest(unittest.TestCase):
    """Task 1 carry-in: the loader must see scheduled hooks, and the config
    snapshot must come from ``manifest.meta`` / ``api.config`` (it used to be a
    dead layer built from ``load({}, None)``)."""

    def test_loader_schedules_hooks_and_the_instance_reads_meta_config(self):
        from qwenpaw.plugins.loader import PluginLoader

        manifest = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
        manifest.setdefault("meta", {})["config"] = {"ref_ttl_seconds": 123}

        with tempfile.TemporaryDirectory() as tmp:
            plugins_dir = Path(tmp) / "plugins"
            dest = plugins_dir / manifest["id"]
            shutil.copytree(
                REPO_ROOT, dest,
                ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc", "tests",
                                              ".github", ".superpowers", "docs", "packaging"))
            (dest / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

            loader = PluginLoader([plugins_dir])
            # The bare loader does not configure the host HTTP app, so the router
            # registration is expected to raise and be swallowed; its ERROR line
            # and the loader's INFO scan lines would only pollute the suite output.
            previous_disable = logging.root.manager.disable
            logging.disable(logging.ERROR)
            try:
                found = [(m, p) for m, p in loader.discover_plugins() if m.id == manifest["id"]]
                self.assertTrue(found, "plugin.json was not discovered")
                found_manifest, source = found[0]
                record = asyncio.run(loader.load_plugin(
                    found_manifest, source,
                    {"answer_timeout_seconds": 45, "ref_ttl_seconds": 222}))
            finally:
                logging.disable(previous_disable)

            self.assertTrue(record.enabled, record.diagnostics)
            self.assertEqual(record.diagnostics, [])
            hooks = loader.registry.get_startup_hooks()
            self.assertGreaterEqual(len(hooks), 1)
            names = [getattr(hook, "hook_name", "") for hook in hooks]
            self.assertIn(EXPECTED_CALL[0], names)
            # meta.config beats api.config, and api.config beats the default.
            self.assertEqual(record.instance.config.ref_ttl_seconds, 123)
            self.assertEqual(record.instance.config.answer_timeout_seconds, 45)


if __name__ == "__main__":
    unittest.main()
