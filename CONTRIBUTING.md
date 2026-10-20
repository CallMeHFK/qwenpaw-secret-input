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

## Development setup

- Python **3.12** (the host interpreter is `/home/leo/miniforge3/bin/python3.12`
  on the reference machine).
- Node.js for `node --check ui/index.js`.
- `ruff` for linting (`pip install ruff`).

## Gates

```bash
/home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v
/home/leo/miniforge3/bin/python3.12 -m compileall -q backend
/home/leo/miniforge3/bin/python3.12 -m ruff check backend tests packaging
/home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py .
node --check ui/index.js
bash packaging/build_zip.sh
```

`packaging/verify_qp_plugin.py` is sha256-pinned evidence; do not edit it. It
loads the plugin with the **real** `PluginLoader` and must print `PASS`.

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
