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
  ``agentscope``, and ``backend/tools.py`` / ``backend/main.py`` must import
  ``qwenpaw.*``. If a future task moves the purity requirement somewhere else,
  add that module to ``PURE_MODULES`` by name — do not widen it silently.

The ban is checked by resolving each import to its real dotted path and
matching root-by-root, so rebinding it under an alias does not hide it.
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
        # have to say so. middleware.py (agentscope) and tools.py/main.py
        # (qwenpaw.*) must NOT be added here.
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
