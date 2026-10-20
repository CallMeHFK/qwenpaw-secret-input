# secret-input

A QwenPaw backend plugin for asking the human for a secret **in the middle of a run**,
without putting the secret into the model's context.

English | [中文版](README.zh-CN.md)

## What it does

The model calls `request_secret`; a private card appears in the session (and at a
self-contained answer page); the human types the value; the model receives a
one-time `ref`, never the value.

**The value never enters the model's context; the model only ever receives a one-time ref.**

The plugin keeps the plaintext in memory only (never on disk), substitutes it
in-process for `secret_exec`, and scrubs any occurrence of it out of tool output
as `***`. `secret_export` is the single, deliberate exception: it persists the
value into the host's encrypted env store so later shell tools can use
`$ENV_KEY`.

- Three tools: `request_secret`, `secret_exec`, `secret_export`
- Four HTTP endpoints under `/api/secret-input` (loopback or authenticated caller)
- One block-time overlay card + one standalone answer page
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

Download `secret-input-0.1.0.zip` from this repository's Releases page (built by
`packaging/build_zip.sh`; the archive has a single top-level `secret-input/`
directory) and install it as above.

## Enable the tools

All three tools register disabled (`enabled=False`). Turn them on in the agent's tool settings before use.

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

model → secret_exec(ref="sr_…", command='curl -H "Authorization: Bearer {{secret}}" …')
tool  ← {"status": "ok", "exit_code": 0, "stdout": "…", "stderr": "", "ref": "consumed"}
```

`request_secret` arguments: `label`, `reason`, `placeholder`, `timeout_seconds`,
`client_request_id`.

- `timeout_seconds` omitted or `0` means "use the operator's
  `answer_timeout_seconds` (default 600)". It is **not** "time out immediately".
  An explicit value must be in `30..3600` and wins over the config.
- Reuse the same `client_request_id` when retrying a call whose result was lost
  (idempotent replay). Use a **new** one after `timed_out` / `ref_unavailable` /
  `ref_expired`.

Outcomes you must handle:

| status | meaning | what to do |
| --- | --- | --- |
| `saved` | human answered; `ref` is valid once | spend it with `secret_exec`/`secret_export` |
| `declined` | human refused | do not ask again for the same thing |
| `timed_out` | nobody answered before the deadline | ask again with `request_secret` and a **new** `client_request_id` |
| `cancelled` | the run was stopped while the card was open | stop; do not retry blindly |
| `ref_unavailable` | ref already spent or unknown | ask again with a **new** `client_request_id` |
| `ref_expired` | ref outlived `ref_ttl_seconds` | ask again with a **new** `client_request_id` |

`secret_exec`: write `{{secret}}` where the value belongs. The plugin substitutes
it in-process; the value is also exported to `$<env_name>` (default `SECRET`) for
the child only. `stdout`/`stderr` pass through the scrubber (`***`) and share one
`max_output_bytes` budget (default 32768, range 1024..32768). The marker is
reserved inside the budget, so the serialized result stays under the host's
50 000 B disk-offload threshold.

`secret_export`: `env_key` must match `^[A-Z_][A-Z0-9_]{0,63}$` and must not be
a host-critical name (`PATH`, `HOME`, `LD_PRELOAD`, `NODE_OPTIONS`, …). The value
goes into the host's encrypted env store and the process environment, for later
`$ENV_KEY` use; the ref is consumed only if the write succeeds. **Prefer
`secret_exec` for one-shot use** — export is a persistent exposure by design.

## Where the answer can be given

1. **Overlay card** in the Console session (appears while the tool is blocked;
   polls the plugin every 2 s for this session's pending requests).
2. **Standalone answer page** at `/api/secret-input/card/<request_id>` — no CDN,
   no build, works in a plain browser tab.
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

Scrubbing replaces every known variant (raw/base64/base64url/hex/percent) with
exactly `***`. The host's `mask_secret_value` is **not** used: it preserves
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
| `public_base_url` | empty | prefix for the answer URL in the INFO log; empty = `http://127.0.0.1:<listen port>` |
| `max_pending` | 32 | concurrent open cards |

The loopback check matches the literal TCP peers `127.0.0.1`, `::1`, `localhost`.
If your client connects from another loopback address (`127.0.0.2`, …), it is
rejected (fail-closed) — use `127.0.0.1`.

## Development

```bash
# tests + packaging verifier are run with the host interpreter
/home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v
/home/leo/miniforge3/bin/python3.12 -m compileall -q backend
/home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py .
/home/leo/miniforge3/bin/python3.12 -m ruff check backend tests packaging
node --check ui/index.js          # or the nodejs binary on Debian-style hosts
bash packaging/build_zip.sh
```

- `packaging/verify_qp_plugin.py` runs the **real** `PluginLoader` offline and is
  sha256-pinned; do not edit it.
- Every gate is expected to have a **negative control** (delete the defence, watch
  the test go red). A gate that cannot fail is not a gate.
- Tests use obvious dummy values (`DUMMY-SK-abcdef123456`); never a real secret.
- The full `unittest` suite needs the host package (it imports `qwenpaw`), so CI
  runs only the static gates; run the full suite locally with the host
  interpreter.
- Building the release zip needs `python3`, `zip`, and `unzip`.

## License

MIT — see [LICENSE](LICENSE).
