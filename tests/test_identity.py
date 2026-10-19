import asyncio
import sys
import types
import unittest

from backend import identity

# identity.py 是宿主 ContextVar 的唯一出口（裁定 R30/R32 的落地处）。这里用 sys.modules
# 塞假模块，两个方向都要钉：
#   1) 名字写对了 —— 拼错一个 getter 不会报错，只会让每条请求都拿到 session_id=""，
#      会话内卡片永远列不出它（R41 那条 WARNING 会变成常态），而单测全绿。
#   2) 没有宿主时照样回空 —— 单测进程不许依赖启动过的宿主。
_HOST_NAMES = ("qwenpaw", "qwenpaw.app", "qwenpaw.app.agent_context",
               "qwenpaw.tool_calls", "qwenpaw.tool_calls._ctxvars",
               "qwenpaw.config", "qwenpaw.config.context")


def _package(name):
    module = types.ModuleType(name)
    module.__path__ = []          # 有 __path__ 才是"包"，子模块才找得到
    return module


class FakeHost:
    """把宿主那四个 getter 与 `get_call_context()` 摆成宿主 2.2.2 的形状。"""

    def __init__(self, values=None, call_context=None, project_dir=None):
        self.values = values or {}
        self.call_context = call_context
        self.project_dir = project_dir

    def __enter__(self):
        saved = {name: sys.modules.get(name) for name in _HOST_NAMES}
        ac = _package("qwenpaw.app.agent_context")

        def _getter(value):
            # 每个 getter 关掉一个固定值（宿主那边是 ContextVar 读出来的），
            # 用工厂而不是带默认值的 lambda，免得假模块自己变成 bug 温床。
            def getter():
                return value
            return getter

        for key, name in (("session_id", "get_current_session_id"),
                          ("agent_id", "get_current_agent_id"),
                          ("user_id", "get_current_user_id"),
                          ("channel", "get_current_channel")):
            setattr(ac, name, _getter(self.values.get(key)))
        ctxvars = _package("qwenpaw.tool_calls._ctxvars")
        ctxvars.get_call_context = lambda: self.call_context
        config_context = _package("qwenpaw.config.context")
        config_context.get_current_project_dir = lambda: self.project_dir

        app = _package("qwenpaw.app")
        app.agent_context = ac
        tool_calls = _package("qwenpaw.tool_calls")
        tool_calls._ctxvars = ctxvars
        config = _package("qwenpaw.config")
        config.context = config_context
        root = _package("qwenpaw")
        root.app, root.tool_calls, root.config = app, tool_calls, config

        sys.modules.update({
            "qwenpaw": root, "qwenpaw.app": app, "qwenpaw.app.agent_context": ac,
            "qwenpaw.tool_calls": tool_calls, "qwenpaw.tool_calls._ctxvars": ctxvars,
            "qwenpaw.config": config, "qwenpaw.config.context": config_context,
        })
        self._saved = saved
        return self

    def __exit__(self, *_exc):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        return False


class _CallContext:
    def __init__(self, cancel_event):
        self.cancel_event = cancel_event


class IdentityReadTest(unittest.TestCase):
    def test_identity_reads_the_four_host_getters_by_name(self):
        with FakeHost(values={"session_id": "s-9", "agent_id": "a-9",
                              "user_id": "u-9", "channel": "console"}):
            self.assertEqual(identity.identity(), {
                "session_id": "s-9", "agent_id": "a-9", "user_id": "u-9", "channel": "console"})

    def test_missing_context_normalises_to_empty_strings(self):
        # 宿主未设上下文时 getter 回 None（app/agent_context.py:351/378/388），
        # 而 registry 的幂等键与 R41 的判据都只认字符串 ⇒ None 必须变成 ""。
        with FakeHost(values={"session_id": None, "agent_id": None,
                              "user_id": None, "channel": None}):
            self.assertEqual(identity.identity(),
                             {"session_id": "", "agent_id": "", "user_id": "", "channel": ""})

    def test_cancel_event_is_the_host_per_call_one(self):
        # spec 15 A6：Stop 要真的落到我们手里，靠的就是这一个对象。
        evt = asyncio.Event()
        with FakeHost(call_context=_CallContext(evt)):
            self.assertIs(identity.cancel_event(), evt)

    def test_no_call_context_gives_no_cancel_event(self):
        with FakeHost(call_context=None):
            self.assertIsNone(identity.cancel_event())

    def test_project_dir(self):
        with FakeHost(project_dir="/tmp/proj"):
            self.assertEqual(identity.project_dir(), "/tmp/proj")
        with FakeHost(project_dir=None):
            self.assertEqual(identity.project_dir(), "")


class NoHostFallbackTest(unittest.TestCase):
    def test_without_the_host_package_everything_degrades_to_empty(self):
        # 单测进程里 qwenpaw 是可 import 的，所以要显式把它从 sys.modules 里"弄没"：
        # 置 None 会让 import 机制抛 ImportError，正好走 identity.py 里那三个 try/except。
        # 这一条是"单测不需要启动过的宿主"（裁定 R30）的门。
        saved = sys.modules.get("qwenpaw")
        sys.modules["qwenpaw"] = None
        try:
            self.assertEqual(identity.identity(),
                             {"session_id": "", "agent_id": "", "user_id": "", "channel": ""})
            self.assertIsNone(identity.cancel_event())
            self.assertEqual(identity.project_dir(), "")
        finally:
            if saved is None:
                del sys.modules["qwenpaw"]
            else:
                sys.modules["qwenpaw"] = saved


if __name__ == "__main__":
    unittest.main()
