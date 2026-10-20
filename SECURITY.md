# Security policy

## Supported versions

| version | supported |
| --- | --- |
| 0.1.x | yes |

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

## Known, deliberate exposures

- `secret_export` persists the value into the host's encrypted env store and the
  process environment. Later tools (and a same-uid agent) can read it with
  `printenv`. Use `secret_exec` for one-shot use.
- A same-host agent can answer the card before the human does and make the plugin
  store a fabricated value. The plugin records and displays the answering peer and
  timestamp so the human can notice; it cannot prevent this at the HTTP layer.
- The card's `label`/`reason` are written by the model and can carry
  prompt-injection bait aimed at the human. They are rendered as untrusted text.

## Scope of the access gate

The four `/api/secret-input` endpoints accept a loopback TCP peer or an
authenticated caller (`request.state.user`). `allow_remote=true` disables that
gate entirely — including the loopback-only rule on the bare `/pending` list (no
`session_id`). Do not enable it on a host exposed to an untrusted network.
