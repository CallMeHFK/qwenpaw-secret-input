# -*- coding: utf-8 -*-
"""System-prompt section. Mirrors T3 Code's rule set for request_secret."""

PROMPT_SECTION = """\
Secrets (tokens, API keys, signing secrets, passwords):
- Ask with the `request_secret` tool only. It shows a private card to the user.
- Never ask for a secret in chat, never invent one, and never repeat one back.
- The value is never returned to you. You get a one-time `ref` instead.
- Spend a `ref` with `secret_exec` ({{secret}} placeholder, plugin-side substitution)
  or `secret_export` (persist into the host env store as $ENV_KEY).
- `ref_unavailable` / `ref_expired` means: call `request_secret` again.
- Never put a secret into a tool argument, a file you write, or a command echo.\
"""
