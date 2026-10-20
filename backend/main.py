"""secret-input plugin entry point.

This is the only **assembly** module allowed a top-level ``qwenpaw.*`` import,
and it owns the env write seam (ruling R46). It is not exclusive: ``identity.py``
is the designated per-call seam, and its host imports live inside functions
behind ``try/except``. ``tools.py`` is held to "never import the host" by
``tests/test_secret_store_gate.py``, so the host env write is injected into it
from here.

Every extension point is registered behind its own try/except: one host
signature drift must degrade that one surface, not take the whole plugin down
(the loader raises on a failing ``register``).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from .config import PluginConfig
from .identity import cancel_event, identity, project_dir
from .middleware import make_scrub_middleware_factory
from .prompt import PROMPT_SECTION
from .records import RequestRegistry
from .router import build_router
from .scrub import Scrubber
from .tools import SecretTools
from .vault import Vault

logger = logging.getLogger(__name__)

PLUGIN_LOGGER_NAME = __name__.split(".", 1)[0]


def _adopt_host_log_handlers() -> None:
    """Write plugin records through the host's own log handlers.

    The host attaches its file handler to the ``qwenpaw`` logger and sets
    ``propagate = False`` there, so a ``plugin_*`` namespace never reaches
    ``~/.qwenpaw/qwenpaw.log`` by itself (measured on the live 2.2.2 host:
    the router's 403 warning was absent from the file). The answer-URL and
    answer-audit lines are part of the contract, so adopt the same handlers.
    Idempotent: force-reinstall runs ``register`` again in the same process.
    """
    # Only the loader's module name (``plugin_<id>``) means "running in the
    # host"; direct imports in tests must not capture the process's handlers.
    if not PLUGIN_LOGGER_NAME.startswith("plugin_"):
        return
    from qwenpaw.utils.logging import LOG_NAMESPACE

    host = logging.getLogger(LOG_NAMESPACE)
    own = logging.getLogger(PLUGIN_LOGGER_NAME)
    if own.level == logging.NOTSET or own.level > logging.INFO:
        own.setLevel(logging.INFO)
    adopted = False
    for handler in host.handlers:
        if handler not in own.handlers:
            own.addHandler(handler)
        adopted = True
    if adopted:
        # Root carries no handler here; keep records from being reflected twice.
        own.propagate = False


TOOL_DESCRIPTIONS = {
    "request_secret": (
        "Ask the user for a secret (token, API key, signing secret, password) through a "
        "private card, and wait. The value is kept by the plugin and NEVER returned to you "
        "or shown in the transcript. On save you get a one-time `ref`. Never ask for a "
        "secret in chat, never invent one, never repeat one. Omit timeout_seconds (or pass "
        "0) to use the operator-configured default. Reuse client_request_id when retrying "
        "a call that lost its result; use a new one after timed_out."
    ),
    "secret_exec": (
        "Run a shell command with the secret the user just provided. Write {{secret}} where "
        "the value belongs; the plugin substitutes it in-process, the value never passes "
        "through you. It is also exported to $<env_name> (default SECRET). stdout/stderr are "
        "scrubbed: any occurrence of the value renders as ***. The ref works once."
    ),
    "secret_export": (
        "Persist the secret behind a ref into QwenPaw's encrypted env store under env_key "
        "(^[A-Z_][A-Z0-9_]{0,63}$). Later shell tools use $ENV_KEY without you ever seeing "
        "the value. The ref is consumed only if the write succeeds."
    ),
}


def _host_callable(name: str, method: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a bound tool method so the host's bridge will take it (R58).

    ``plugins/api.py:_bridge_to_runtime`` ends with
    ``tool_func._tool_descriptor = desc``. A bound method has no ``__dict__``, so
    that line raised ``AttributeError`` and the host rolled the registration
    back: the tools stayed listed by ``meta.tools`` (which is all ``/api/tools``
    reads) yet were never callable. ``functools.partial`` carries a ``__dict__``
    and keeps ``inspect.signature`` plus coroutine detection.

    ``__name__`` / ``__doc__`` are not cosmetics. ``list_tools`` hands only
    ``desc.func`` to agentscope's ``FunctionTool``, which reads the tool name
    from ``__name__`` (a bare partial raises there) and the model-visible
    description from the docstring — ``ToolDescriptor.description`` never reaches
    the schema, so without this the model would see an empty description.
    """
    bound = functools.partial(method)
    bound.__name__ = name
    bound.__doc__ = TOOL_DESCRIPTIONS[name]
    return bound


def _write_env_via_host_store(key: str, value: str) -> None:
    """The one legal host-env write (R46).

    ``tools.py`` must stay host-import-free, so the write is injected from this
    module. The import sits inside the function: importing the plugin for static
    checks does not need the host's env store, and the error surfaces only when
    the tool is actually used.
    """
    from qwenpaw.envs.store import set_env_var  # 唯一合法落点：main.py

    set_env_var(key, value)


def _manifest_meta(api) -> dict | None:  # noqa: ANN001 - PluginApi 在宿主侧
    """The ``manifest.meta`` layer of the config chain, from either manifest shape.

    The real loader passes ``api.manifest`` as a **dict** (``PluginApi`` stores
    ``manifest or {}``), while tests and older hosts may hand an object with a
    ``meta`` attribute. Reading only the attribute would silently kill the
    ``meta.config`` precedence layer (Task 1 carry-in).
    """
    manifest = getattr(api, "manifest", None)
    meta = manifest.get("meta") if isinstance(manifest, dict) else getattr(manifest, "meta", None)
    return meta if isinstance(meta, dict) else None


class SecretInputPlugin:
    def __init__(self) -> None:
        self.config = PluginConfig.load({}, None)

    def register(self, api) -> None:  # noqa: ANN001 - PluginApi 在宿主侧
        self.api = api
        try:
            _adopt_host_log_handlers()
        except Exception:  # noqa: BLE001 - 日志接管失败只降级可观测性
            logger.exception("secret-input: could not adopt the host log handlers")
        # meta.config > api.config > env > defaults. Both layers used to be dead
        # here (Task 1 snapshot); tests/test_main.py pins the loaded instance.
        self.config = PluginConfig.load(getattr(api, "config", None) or {}, _manifest_meta(api))
        self.vault = Vault(ttl_seconds=self.config.ref_ttl_seconds,
                           max_entries=self.config.scrub_max_entries * 4)
        self.registry = RequestRegistry(self.vault,
                                        timeout_seconds=self.config.answer_timeout_seconds,
                                        max_pending=self.config.max_pending)
        self.scrubber = Scrubber(ttl_seconds=self.config.scrub_ttl_seconds,
                                 max_entries=self.config.scrub_max_entries)
        self.tools = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=identity, cancel_event=cancel_event,
                                 project_dir=project_dir,
                                 env_writer=_write_env_via_host_store)
        for hook in (
            lambda: api.register_tool("request_secret",
                                      _host_callable("request_secret", self.tools.request_secret),
                                      description=TOOL_DESCRIPTIONS["request_secret"],
                                      icon="🔑", enabled=False, tool_type="internal"),
            lambda: api.register_tool("secret_exec",
                                      _host_callable("secret_exec", self.tools.secret_exec),
                                      description=TOOL_DESCRIPTIONS["secret_exec"],
                                      icon="🛡️", enabled=False, tool_type="shell",
                                      target_param="command"),
            lambda: api.register_tool("secret_export",
                                      _host_callable("secret_export", self.tools.secret_export),
                                      description=TOOL_DESCRIPTIONS["secret_export"],
                                      icon="📦", enabled=False, tool_type="internal"),
            lambda: api.register_http_router(
                build_router(registry=self.registry, config=self.config, vault=self.vault),
                prefix="/secret-input", tags=["secret-input"]),
            lambda: api.register_middleware(
                make_scrub_middleware_factory(self.scrubber, self.config), priority=50),
            lambda: api.register_prompt_section(
                "secret_input", after="env_context", provider=lambda _agent: PROMPT_SECTION),
            lambda: api.register_startup_hook("secret_input_sweep", self._sweep, priority=100),
            lambda: api.register_shutdown_hook("secret_input_shutdown", self._shutdown,
                                               priority=100),
            lambda: api.register_uninstall_hook("secret_input_uninstall", self._uninstall,
                                                priority=100),
        ):
            try:
                hook()
            except Exception:  # noqa: BLE001 - 一处失败不能带崩整包加载
                logger.exception("secret-input: a registration failed; the plugin is degraded")

    def _sweep(self) -> None:
        self.registry.sweep()
        self.vault.sweep()
        self.scrubber.sweep()
        logger.info("secret-input: swept (pending=%d vault=%d scrub=%d)",
                    len(self.registry.list_pending()), len(self.vault), len(self.scrubber))

    async def _shutdown(self) -> None:
        for rec in self.registry.list_pending():
            await self.registry.cancel(rec.request_id, "shutdown")

    def _uninstall(self, **_kwargs: Any) -> None:
        # Refs die with the process; nothing of the user's secret is persisted.
        self._sweep()

    def status(self) -> dict:
        return {"pending": [r.request_id for r in self.registry.list_pending()],
                "vault_size": len(self.vault), "scrub_entries": len(self.scrubber),
                "scrub_enabled": self.config.scrub_enabled}


plugin = SecretInputPlugin()
