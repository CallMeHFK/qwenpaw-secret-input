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

from backend.main import (  # noqa: E402
    TOOL_DESCRIPTIONS,
    SecretInputPlugin,
    _write_env_via_host_store,
)

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


class HostToolBridgeContractTest(unittest.TestCase):
    """R58: hot install rolled all three tools back.

    ``plugins/api.py:_bridge_to_runtime`` ends with
    ``tool_func._tool_descriptor = desc``. A **bound method** has no
    ``__dict__``, so that line raised ``AttributeError: 'method' object has no
    attribute '_tool_descriptor'``; the host's except-branch then released
    ownership and governance, leaving the tools listed by ``meta.tools`` (which
    is what ``/api/tools`` reads) but never callable — measured on the live
    2.2.2 host at 2026-10-20 10:13:53 and again at 10:21:09.

    ``enabled`` is not the trigger: it only reaches ``ToolDescriptor`` *after*
    the failing line, so ``enabled=True`` cannot fix the rollback — and it would
    break the spec's default-disabled contract.
    """

    EXPECTED_REQUIRED = {
        "request_secret": ["label", "reason"],
        "secret_exec": ["ref", "command"],
        "secret_export": ["ref", "env_key"],
    }

    def _registered(self) -> dict:
        api = RecordingApi()
        with self.assertNoLogs("backend.main", level="ERROR"):
            SecretInputPlugin().register(api)
        return api.tools

    def _bridge(self, name: str, func):  # noqa: ANN001
        from qwenpaw.plugins.api import _bridge_to_runtime

        # registry=None: the host still attaches the descriptor first, which is
        # exactly the line that killed the bound method. Its "Attached
        # ToolDescriptor" INFO lines would only pollute the suite output.
        previous_disable = logging.root.manager.disable
        logging.disable(logging.INFO)
        try:
            _bridge_to_runtime(name, func, False, TOOL_DESCRIPTIONS[name], None)
        finally:
            logging.disable(previous_disable)
        return func._tool_descriptor

    def test_negative_control_a_raw_bound_method_fails_the_bridge(self):
        # 负对照：不裹一层就必须炸。没有这一条，上面两个用例即使桥变了形状也照样绿。
        plugin = SecretInputPlugin()
        plugin.register(RecordingApi())
        with self.assertRaises(AttributeError) as ctx:
            self._bridge("request_secret", plugin.tools.request_secret)
        self.assertIn("_tool_descriptor", str(ctx.exception))

    def test_the_callables_handed_to_the_host_survive_the_bridge(self):
        for name, record in self._registered().items():
            with self.subTest(tool=name):
                desc = self._bridge(name, record["func"])
                self.assertEqual(desc.name, name)
                self.assertTrue(desc.async_execution)
                self.assertEqual(desc.description, TOOL_DESCRIPTIONS[name])
                # spec：三个工具默认关闭，由人在 agent 的工具设置里打开
                self.assertFalse(desc.enabled_by_default)
                self.assertFalse(record["enabled"])

    def test_the_model_visible_schema_keeps_name_description_and_params(self):
        # ``list_tools`` 只把 ``desc.func`` 交给 agentscope 的 ``FunctionTool``
        # （``ToolDescriptor.description`` 从不参与 schema），所以名字与说明必须
        # 长在 callable 上。裸 partial 两处都丢：没有 ``__name__`` 直接
        # AttributeError，没有 ``__doc__`` 则回落成 "partial(func, *args, …)"
        # 并把每个参数的说明抹掉。
        from agentscope.tool import FunctionTool

        for name, record in self._registered().items():
            with self.subTest(tool=name):
                tool = FunctionTool(record["func"])
                self.assertEqual(tool.name, name)
                self.assertEqual(tool.description, TOOL_DESCRIPTIONS[name])
                self.assertNotIn("partial(func", tool.description)
                schema = tool.input_schema
                self.assertEqual(sorted(schema["required"]), sorted(self.EXPECTED_REQUIRED[name]))
                for param in schema["required"]:
                    self.assertIn(param, schema["properties"])


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


class VaultCeilingTest(unittest.TestCase):
    """终审 Minor 7：spec §9 说 vault ≤ 64，实现曾是 `scrub_max_entries * 4`。

    耦合到一个不相关的键，意味着把 `scrub_max_entries` 调到 1000 就悄悄允许 vault 同时
    驻留 4000 份明文。改成常量后用**行为**断言（灌 80 个 ref，活着的不得超过 64），
    而不是去读 `vault._max` —— 摸私有字段正是 Minor 10 要修的那件事。
    """

    def test_the_ceiling_is_64_and_does_not_follow_the_scrub_key(self):
        from backend.main import _VAULT_MAX_ENTRIES

        self.assertEqual(_VAULT_MAX_ENTRIES, 64)            # spec §9
        api = RecordingApi(config={"scrub_max_entries": 1000})
        plugin = SecretInputPlugin()
        plugin.register(api)
        self.assertEqual(plugin.config.scrub_max_entries, 1000)
        for i in range(80):
            plugin.vault.issue(f"DUMMY-SK-{i:04d}")
        self.assertLessEqual(len(plugin.vault), _VAULT_MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
