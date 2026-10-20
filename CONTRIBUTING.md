# Contributing

## Ground rules

This plugin's product is a security property: the plaintext must not reach the
model's side of the wire. A change that weakens an invariant is not acceptable
even if it makes the feature nicer.

Read the design spec first: [`docs/superpowers/specs/2026-10-19-secret-input-plugin-design.md`](docs/superpowers/specs/2026-10-19-secret-input-plugin-design.md).
The non-negotiable invariants:

- Values travel only in `POST /api/secret-input/card/{id}/answer` bodies.
- `ref` is opaque, one-time, TTL'd; replays say "ask again with a new
  `client_request_id`".
- The redaction marker is exactly `***`; the host's `mask_secret_value` is
  forbidden (it preserves leading/trailing characters).
- `tools.py` and `middleware.py` must not import `qwenpaw`; only `main.py` may.
- Tools register `enabled=False` by default.
- `register_tool` must be handed a plain function or `functools.partial`, never a
  bound method: the host attaches `_tool_descriptor` to the callable, a bound
  method rejects the assignment, and the host then rolls the whole registration
  back — the tools stay listed in `/api/tools` yet are never callable. Use
  `main.py::_host_callable` (it also carries `__name__`/`__doc__`, which is where
  the model-visible name and description come from).

## Development setup

- Python **3.12** — and it must be the **host's** interpreter, i.e. the one that
  can `import qwenpaw`. The commands below write `python3.12`; if that is not the
  host's interpreter on your `PATH`, substitute its absolute path.
- Node.js for `node --check ui/index.js`.
- `ruff` for linting (`pip install ruff`).
- `zip` / `unzip` for `packaging/build_zip.sh`.

## Gates

```bash
python3.12 -m unittest discover -s tests -t . -v
python3.12 -m compileall -q backend
python3.12 -m ruff check backend tests packaging
python3.12 packaging/verify_qp_plugin.py .
node --check ui/index.js
bash packaging/build_zip.sh
```

`packaging/verify_qp_plugin.py` is **frozen evidence**, not product code: do not
edit it. `tests/test_verifier_gate.py` pins its sha256
(`2182bc4d2150b91ff6d623338cce4fafadaf4c65d122352589cfed212509f18a`) and runs its
8 negative controls plus a positive control, so editing it — or shipping a package
it should reject — turns the suite red. It loads the plugin with the **real**
`PluginLoader` and must print `PASS`.

CI runs the static subset only (ruff / compileall / node --check) because the
full suite imports the host package. Run the full suite locally.

## Negative controls

Every new gate must come with a control that proves it can fail: remove or
weaken the defence, run the test, watch it go red, restore. State the control in
the pull request. A gate that cannot fail is not a gate.

Never use a real credential in tests, fixtures, or reports. Use
`DUMMY-SK-abcdef123456`.

## Pull requests

- Keep the security story in the description: which invariant is affected, and
  how you proved it still holds.
- Include the commands you ran and their actual output (red readings included).
- Update the spec or plan when behaviour drifts; do not leave the docs describing
  an older design.
- One logical change per pull request.

## Code style

- `ruff check backend tests packaging` must pass; the config lives in
  `pyproject.toml`.
- Comments explain *why* (a non-obvious constraint, a past bug, a host
  workaround), not *what*.
- Tool results are JSON strings and never raise into the host.
- Log lines never carry the value, and (since R51a) never carry model-written
  `label`/`reason` either.

## Commit messages

`<type>: <what changed and why>` — e.g.
`fix: 消费点评审修复轮 1（合池序列化预算、cwd fail-closed）`.
