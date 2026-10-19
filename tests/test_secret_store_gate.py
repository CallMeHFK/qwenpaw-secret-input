# -*- coding: utf-8 -*-
"""Repo-wide gate for the two hard constraints that the source-text guard in
``tests/test_scrub.py`` cannot see.

Why this file exists (Task 4 review, Important 1 / controller ruling R23):
``test_mask_function_is_not_used`` only greps ``backend/scrub.py`` for two
literal strings. ``from qwenpaw.security import secret_store`` and
``import qwenpaw.security`` are invisible to it (the alias can be anything),
and "scrub.py/vault.py are pure stdlib" was guarded by nothing but a grep
claim in a report. This module checks the *import surface* with ``ast``.

Scope is deliberately asymmetric, and the asymmetry is the point:

* the masking-helper ban applies to **every** ``backend/*.py`` — spec section
  6.1 and section 10 phrase it as a project-wide prohibition, not a scrub.py
  quirk;
* the pure-stdlib assertion applies **only** to ``backend/scrub.py`` and
  ``backend/vault.py``, the two modules at the bottom of the dependency graph
  where the claim is actually load-bearing. A blanket stdlib-only allowlist
  across ``backend/`` would be wrong: ``backend/middleware.py`` must import
  ``agentscope``. It must not, however, be read as "``tools.py`` may import the
  host": the host seam of this plugin is **``backend/identity.py`` and nothing
  else** (Task 7 / ruling R30) — ``tools.py`` reaches per-call context only
  through callables injected by the assembler, and the host-coupling gate below
  reads that module too. ``backend/main.py`` (Task 9's assembler) is the only
  place that is *expected* to import ``qwenpaw.*``. If a future task moves the
  purity requirement somewhere else, add that module to ``PURE_MODULES`` by name
  — do not widen it silently.

The ban is checked by resolving each import to its real dotted path and
matching root-by-root, so rebinding it under an alias does not hide it.

Layer (d), added by Task 5 review Important 3 / ruling R32: the R30 rule "the
middleware layer must not import the host package" had no automated guard at
all — the two text bans above only cover ``qwenpaw.security.secret_store``, and
the stdlib-only assertion deliberately excludes ``middleware.py``, so a future
task could add ``from qwenpaw.plugins import api`` to the middleware and nothing
would redden. ``HostCouplingGateTest`` walks the gated modules themselves: no
import root equal to or under ``qwenpaw``, and the only absolute third-party root
allowed is ``agentscope``. Relative sibling imports stay legal
(``.scrub`` always has been a runtime dependency; ``.config`` is stdlib-only),
because R30's intent is "no dependency on the **host** package, so the layer
stays testable and immune to host refactors" — not "no dependency on our own
package".

The scope was widened to ``("middleware.py", "tools.py")`` by Task 7 review
Important 1: ``tools.py`` carries the same R30 promise (identity and cancel
arrive as injected callables; the only module allowed to read host context is
``backend/identity.py``), yet the gate read a single file, so Task 9's assembler
could have re-coupled ``tools.py`` to the host with every gate staying green —
and this docstring used to *assert* that coupling as required, which is how a
pre-existing comment ended up steering the next task into breaking the rule.
``identity.py`` stays out of scope on purpose: it is the seam, its host imports
are inside function bodies behind ``try/except``, and
``tests/test_identity.py`` pins both directions of that.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import ClassVar

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"

# The forbidden module, as a component path (never compared as a substring, so
# "secret_storeish" or a same-named local module cannot trip or slip through).
FORBIDDEN = ("qwenpaw", "security", "secret_store")
# The forbidden callable inside it: it keeps the leading 2-3 and trailing 2-4
# characters of a value, turning a zero-character leak into a several-character
# one (spec section 10 explicit prohibition).
MASKER = "mask_secret_value"

# Only these two bottom modules are held to stdlib-only imports (see docstring).
PURE_MODULES = ("scrub.py", "vault.py")

# The two literals the existing source-text guard bans; kept here so the ban
# covers the whole backend package instead of one file.
BANNED_LITERALS = (MASKER, "qwenpaw.security.secret_store")


def backend_sources() -> list[Path]:
    return sorted(p for p in BACKEND_DIR.glob("*.py") if p.is_file())


def _dotted(parts: str) -> tuple[str, ...]:
    return tuple(p for p in parts.split(".") if p)


def _attr_chain(node: ast.AST) -> list[str] | None:
    """Flatten a Name/Attribute chain to components, root first.

    Returns None when the chain does not start at a plain name (e.g. a call or
    a subscript hangs off the root), which cannot be resolved statically.
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def _import_bindings(tree: ast.AST) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, ...]]]:
    """Map every module-level import binding to its real dotted path.

    ``import a.b.c`` binds ``a`` to ``("a",)`` and ``import a.b.c as m`` binds
    ``m`` to the full path; ``from a.b import c as d`` binds ``d`` to
    ``("a", "b", "c")``. Star imports are returned separately because they
    make a name reachable without any binding we can see.
    """
    bindings: dict[str, tuple[str, ...]] = {}
    stars: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = _dotted(alias.name)
                if alias.asname:
                    bindings[alias.asname] = path
                else:
                    # "import a.b.c" binds the root only.
                    bindings[path[0]] = (path[0],)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative import: cannot be qwenpaw.*
                continue
            base = _dotted(node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    stars.append(base)
                    continue
                full = base + (alias.name,)
                bindings[alias.asname or alias.name] = full
    return bindings, stars


def _resolve(chain: list[str], bindings: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Rewrite a chain's root through the import bindings (alias-proofing)."""
    root = chain[0]
    if root in bindings:
        return bindings[root] + tuple(chain[1:])
    return tuple(chain)


def _hits_forbidden(path: tuple[str, ...]) -> bool:
    return len(path) >= len(FORBIDDEN) and path[:len(FORBIDDEN)] == FORBIDDEN


def find_secret_store_reaches(source: str) -> list[str]:
    """Return human-readable violations for one module's source.

    Two independent layers, both alias-proof:
    1. an import that resolves to the forbidden module (or to a package that
       only becomes the forbidden module once an attribute is hung off it);
    2. any use — attribute access or a bare call on the module object — whose
       alias-resolved path reaches the forbidden module, plus any call whose
       callee name is the masking helper.
    """
    violations: list[str] = []
    tree = ast.parse(source)
    bindings, stars = _import_bindings(tree)

    for name, path in sorted(bindings.items()):
        if _hits_forbidden(path):
            violations.append(f"import binds {name!r} to {'.'.join(path)}")
    for base in stars:
        # "from qwenpaw.security import *" puts the module in reach by bare
        # name; nothing static tells us which name, so it is refused outright.
        if _hits_forbidden(base + ("x",)) or FORBIDDEN[:len(base)] == base:
            violations.append(f"star import from {'.'.join(base) or '<unknown>'}")

    for node in ast.walk(tree):
        target = getattr(node, "func", None) if isinstance(node, ast.Call) else node
        if not isinstance(target, (ast.Attribute, ast.Name)):
            continue
        chain = _attr_chain(target)
        if chain is None:
            continue
        path = _resolve(chain, bindings)
        if _hits_forbidden(path):
            kind = "call" if isinstance(node, ast.Call) else "use"
            violations.append(f"{kind} reaches {'.'.join(path)} (written as {'.'.join(chain)})")
        if isinstance(node, ast.Call) and path[-1] == MASKER:
            violations.append(f"calls {MASKER} via {'.'.join(path)}")
    return violations


def find_non_stdlib_imports(source: str) -> list[str]:
    """Every import root in ``source`` that is not in the stdlib, plus any
    relative import (these two modules must sit at the bottom of the graph and
    must not pull in a sibling ``backend`` module)."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _dotted(alias.name)[0]
                if root not in sys.stdlib_module_names:
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                offenders.append(f"from {'.' * node.level}{node.module or ''} import ...")
                continue
            parts = _dotted(node.module or "")
            if parts and parts[0] not in sys.stdlib_module_names:
                offenders.append(f"from {node.module} import ...")
    return offenders


# R32, widened to tools.py by Task 7 review I-1: the two layers that must not
# depend on the host package. `identity.py` is the seam itself and is left out on
# purpose (see the module docstring); `main.py` is Task 9's assembler, the one
# place expected to import `qwenpaw.*`.
HOST_GATED_MODULES = ("middleware.py", "tools.py")

# R32: the gated layers may reach the framework they hook into, and the standard
# library, and nothing that lives inside the host package.
HOST_PACKAGE_ROOT = "qwenpaw"
MIDDLEWARE_ALLOWLIST = frozenset({"agentscope"})


def find_host_coupled_imports(source: str) -> list[str]:
    """Import roots in ``source`` that break R32, as human-readable violations.

    Red for: any root equal to or under ``qwenpaw`` (``import qwenpaw``,
    ``from qwenpaw.plugins import api``, ``import qwenpaw.x as y``), and any
    other absolute third-party root outside ``MIDDLEWARE_ALLOWLIST``.
    Green for: stdlib roots, ``agentscope``, and relative imports — a sibling
    inside our own package is not a host dependency.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _dotted(alias.name)[0]
                written = f"import {alias.name}"
                if root == HOST_PACKAGE_ROOT or (
                        root not in sys.stdlib_module_names
                        and root not in MIDDLEWARE_ALLOWLIST):
                    offenders.append(f"{written} -> root {root!r}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative: our own package, allowed by R32
                continue
            parts = _dotted(node.module or "")
            root = parts[0] if parts else ""
            written = f"from {node.module} import ..."
            if root == HOST_PACKAGE_ROOT or (
                    root not in sys.stdlib_module_names
                    and root not in MIDDLEWARE_ALLOWLIST):
                offenders.append(f"{written} -> root {root!r}")
    return offenders


class HostCouplingGateTest(unittest.TestCase):
    """R32: ``backend/middleware.py`` and ``backend/tools.py`` must not depend on
    the host package.

    The gate is static (AST over the file text), so it needs no host import and
    keeps R30's rationale true: these layers stay testable and immune to host
    refactors. Note what the pre-existing layers do *not* cover, which is why this
    had to be added: the text bans only name
    ``qwenpaw.security.secret_store``, and ``PURE_MODULES`` excludes these files
    on purpose (middleware imports ``agentscope``; tools imports siblings), so
    ``from qwenpaw.plugins import api`` here used to be invisible to every gate in
    the repo.

    Why ``tools.py`` is in scope now (Task 7 review Important 1): "the tools layer
    never imports the host" is what makes ``tests/test_tools_request.py`` run
    without a booted host, and identity/cancel arriving as injected callables is
    exactly what Task 9's assembler has to wire. The scope used to be one module,
    so the plan could tell the assembler to re-couple ``tools.py`` and no gate
    would have reddened. ``find_host_coupled_imports`` already returned ``[]`` for
    the shipped file: widening the scope was the whole fix, not new logic.
    """

    def test_the_gated_module_is_named_and_present(self):
        # Scope self-check, same shape as test_backend_package_is_not_empty: a
        # gate that silently reads nothing is not a gate.
        names = [p.name for p in backend_sources()]
        self.assertEqual(list(HOST_GATED_MODULES), ["middleware.py", "tools.py"])
        for name in HOST_GATED_MODULES:
            with self.subTest(module=name):
                self.assertIn(name, names)
                self.assertTrue((BACKEND_DIR / name).is_file())
        self.assertEqual(MIDDLEWARE_ALLOWLIST, {"agentscope"})
        self.assertEqual(PURE_MODULES, ("scrub.py", "vault.py"))
        # identity.py must stay OUT of this scope: it is the designated host seam,
        # and it does import `qwenpaw.*` (inside function bodies, behind try/except).
        # Naming that here is what stops the next widening from "fixing" the seam
        # into a violation — or, worse, from quietly folding it into the gate.
        self.assertNotIn("identity.py", HOST_GATED_MODULES)
        self.assertTrue((BACKEND_DIR / "identity.py").is_file())

    def test_gated_modules_do_not_import_the_host_package(self):
        offenders = {}
        for name in HOST_GATED_MODULES:
            path = BACKEND_DIR / name
            self.assertTrue(path.is_file(), path)
            found = find_host_coupled_imports(path.read_text(encoding="utf-8"))
            if found:
                offenders[name] = found
        self.assertEqual(offenders, {}, f"R30/R32 violated: {offenders}")

    def test_both_gated_files_are_actually_read(self):
        # Scope self-check #2: naming two modules only means something if the walk
        # really reads both. The planted control (a host import in tools.py) reddens
        # `test_gated_modules_do_not_import_the_host_package`; this one proves the
        # two files differ in shape (middleware reaches agentscope, tools reaches
        # only stdlib + siblings), so a gate that read the same file twice would not
        # look this plausible.
        mw = find_host_coupled_imports(
            (BACKEND_DIR / "middleware.py").read_text(encoding="utf-8"))
        tools = find_host_coupled_imports(
            (BACKEND_DIR / "tools.py").read_text(encoding="utf-8"))
        self.assertEqual(mw, [])
        self.assertEqual(tools, [])
        self.assertIn("agentscope", (BACKEND_DIR / "middleware.py").read_text(encoding="utf-8"))
        self.assertNotIn("agentscope", (BACKEND_DIR / "tools.py").read_text(encoding="utf-8"))

    def test_host_import_shapes_would_be_caught(self):
        """Positive control: the shapes a future task would actually write."""
        for src in (
            "import qwenpaw\n",
            "import qwenpaw.plugins.api as api\n",
            "from qwenpaw.plugins import api\napi.register_middleware(f)\n",
            "from qwenpaw.agents.middlewares import ToolResultPruningMiddleware\n",
            "import qwenpaw.security\n",
            "import numpy\n",                      # any non-allowlisted root too
        ):
            with self.subTest(src=src.splitlines()[0]):
                self.assertTrue(find_host_coupled_imports(src), src)

    def test_allowed_imports_are_not_flagged(self):
        """Negative control: the gate is targeted, not a blanket import ban."""
        for src in (
            "from __future__ import annotations\nimport logging\nimport traceback\n"
            "from typing import TYPE_CHECKING, Any\n",
            "from agentscope.middleware import MiddlewareBase\n",
            "from .scrub import Scrubber\n",                 # runtime sibling
            "from .config import PluginConfig\n",            # sibling, stdlib-only
            "if TYPE_CHECKING:\n    from .config import PluginConfig\n",
        ):
            with self.subTest(src=src.splitlines()[0]):
                self.assertEqual(find_host_coupled_imports(src), [])


class SecretStoreImportGateTest(unittest.TestCase):
    """Layer (b) of R23: repo-wide AST walk over ``backend/*.py``."""

    def test_backend_package_is_not_empty(self):
        # Guard against the gate silently scanning nothing.
        names = [p.name for p in backend_sources()]
        self.assertIn("scrub.py", names)
        self.assertGreaterEqual(len(names), 3, names)

    def test_no_backend_module_reaches_the_host_masking_module(self):
        for path in backend_sources():
            with self.subTest(module=path.name):
                self.assertEqual(find_secret_store_reaches(path.read_text(encoding="utf-8")), [])

    def test_no_backend_module_names_the_banned_literally(self):
        # The literal ban is kept (R23 layer a) and widened to the whole
        # backend package: no module may even mention the helper's name, so a
        # rationale comment has to describe it without naming it — which is
        # exactly how scrub.py's docstring is written.
        for path in backend_sources():
            src = path.read_text(encoding="utf-8")
            for literal in BANNED_LITERALS:
                with self.subTest(module=path.name, literal=literal):
                    self.assertNotIn(literal, src)


class PureBottomModuleGateTest(unittest.TestCase):
    """Layer (c) of R23: stdlib-only, for the two modules the claim is about."""

    def test_pure_module_list_is_named_not_guessed(self):
        # Naming the scope in a test: if someone widens it, this is where they
        # have to say so. middleware.py (agentscope) and tools.py (sibling imports)
        # must NOT be added here — tools.py's constraint is the host-coupling gate
        # above, and identity.py, the one module that may import the host by design,
        # is guarded by tests/test_identity.py instead.
        self.assertEqual(set(PURE_MODULES), {"scrub.py", "vault.py"})

    def test_scrub_and_vault_import_only_the_stdlib(self):
        for name in PURE_MODULES:
            path = BACKEND_DIR / name
            with self.subTest(module=name):
                self.assertTrue(path.is_file(), path)
                self.assertEqual(find_non_stdlib_imports(path.read_text(encoding="utf-8")), [])

    def test_non_stdlib_sibling_import_is_visible_to_the_purity_check(self):
        # Positive control for the purity walker itself: a sibling import and a
        # third-party import must both be reported.
        src = "import os\nfrom .config import PluginConfig\nimport agentscope\n"
        offenders = find_non_stdlib_imports(src)
        self.assertIn("from .config import ...", offenders)
        self.assertIn("import agentscope", offenders)
        self.assertEqual(offenders, ["from .config import ...", "import agentscope"])


class GateSelfControlTest(unittest.TestCase):
    """The alias-proof walk must fire on the shapes the review named."""

    EVASIONS: ClassVar[list[str]] = [
        "from qwenpaw.security import secret_store as ss\nss.mask_secret_value(v)\n",
        "from qwenpaw.security import secret_store\nsecret_store.mask_secret_value(v)\n",
        "import qwenpaw.security.secret_store as m\nm.mask_secret_value(v)\n",
        "import qwenpaw.security\nqwenpaw.security.secret_store.mask_secret_value(v)\n",
        "from qwenpaw import security\nsecurity.secret_store.mask_secret_value(v)\n",
        "from qwenpaw.security.secret_store import mask_secret_value\nmask_secret_value(v)\n",
        "from qwenpaw.security import *\nsecret_store.mask_secret_value(v)\n",
        "from qwenpaw.security import secret_store as ss\nout = ss.mask_secret_value\n",
    ]

    def test_each_evasion_shape_is_caught(self):
        for src in self.EVASIONS:
            with self.subTest(src=src.splitlines()[0]):
                self.assertTrue(find_secret_store_reaches(src), src)

    def test_benign_imports_are_not_flagged(self):
        # The gate is targeted, not a blanket ban: the host's other surfaces
        # (which Task 5/8 must use) stay legal, and a same-named local module
        # or a sibling of the forbidden module is not mistaken for it.
        for src in (
            "from qwenpaw.plugins import api\napi.register_middleware(f)\n",
            "from qwenpaw.security import secret_box\nsecret_box.put(v)\n",
            "from backend.scrub import Scrubber\ns = Scrubber()\n",
            "import secret_store\nsecret_store.mask(x)\n",
            "from urllib.parse import quote\nquote(v, safe='')\n",
        ):
            with self.subTest(src=src.splitlines()[0]):
                self.assertEqual(find_secret_store_reaches(src), [])


if __name__ == "__main__":
    unittest.main()
