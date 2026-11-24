# Codex pairing and routing

OMarky connects to the Codex CLI app-server protocol and sends the marked PNG plus note as one `turn/start` input to an explicitly selected, already-loaded conversation. It does not create a new chat, resume a detached thread, fork history, edit Codex configuration, copy authentication, or invoke an alternate store writer. When waiting for a reply, it may re-subscribe to that same thread only after confirming the ID is still loaded in the paired TUI.

## Pair an actual Codex TUI

The normal Codex CLI may use a shared/default app-server that OMarky cannot safely claim as a client-owned session. Start the opt-in pairing launcher instead:

```sh
bin/omarky-codex
```

This starts Codex's app-server on an owner-private Unix socket and runs the real Codex TUI's existing-conversation resume picker against that socket. Choose the existing conversation in the picker and leave the TUI open while OMarky is connected. OMarky lists only threads the paired app-server currently reports as loaded and idle/active; it never picks the newest chat or infers the foreground target. A thread that is no longer loaded is not eligible until the user reopens it in the paired TUI.

To open a fresh conversation on the same private pair instead, run:

```sh
bin/omarky-codex --new
```

The launcher keeps the server, socket, and private lease alive only while its TUI runs; normal TUI exit removes the lease and stops the server. A forced `SIGKILL` can prevent cleanup, but stale leases are rejected because OMarky verifies the owner process identity and socket. No credentials or history are copied. Codex itself reads its normal `CODEX_HOME` and authentication in place.

If Codex is not available on `PATH`, set `OMARKY_CODEX_BIN` to the absolute path of the installed Codex executable for both the OMarky process and launcher, for example:

```sh
OMARKY_CODEX_BIN=/absolute/path/to/codex bin/omarky-codex
```

OMarky does not invoke mise shims that could auto-update Codex; when available it uses a direct installed Codex executable. `bin/omarky-codex --help` displays the launcher options.

## Send and response semantics

- The adapter uses only Python's standard library and `codex app-server proxy --sock PATH`. The Codex Unix transport is WebSocket over the proxy's raw stdio tunnel; each connection performs `initialize`/`initialized` before app-server calls.
- Discovery calls `thread/loaded/list`, then performs a metadata-only `thread/read` with `includeTurns: false` for each exact ID. It emits only `idle` or `active` threads from a live OMarky-owned TUI lease; it does not page the stored thread catalog or load/resume a thread. Titles and project names are plain text labels only.
- Before a send, OMarky re-reads the selected thread with `includeTurns: false`. `active` means `busy`; `notLoaded`/missing targets mean `gone`; unrecognized state or app-server failure fails closed.
- When `wait_reply=True`, OMarky first confirms the exact thread ID is still in `thread/loaded/list`, then calls documented `thread/resume` with `{threadId, excludeTurns: true}` on that already-loaded thread. It never uses resume to resurrect a detached or archived conversation.
- A send uses `turn/start` with the exact selected `threadId`, the provided `request_id` as `clientUserMessageId`, and the note plus marked PNG data URL. The adapter does not send an unmarked/original image, retry a request automatically, or start a second turn to check the first.
- With `wait_reply=False`, `ok` means Codex returned an accepted `turn/start` response, not that a model completed. With `wait_reply=True`, OMarky consumes live `item/completed` and `turn/completed` notifications and reports `ok` only after an `agentMessage` item for the exact thread and returned `turn.id` is complete and that turn's status is `completed`. Partial deltas, tool items, other threads/turns, and incomplete statuses cannot produce success. A failed turn is `error`; interruption, timeout, transport loss after acceptance, or missing final text is `uncertain`. Check the paired conversation before any manual retry.
- OMarky rejects app-server initiated approval/user-input requests; it is an image sender, not a general Codex approval client.

The dispatcher in `app/routes.py` exposes only Codex in this public v1 and imports the adapter lazily. Missing setup is reported as guidance, never represented as a conversation.

## Verification

Focused tests are standard-library `unittest` tests:

```sh
python3 -m unittest discover -s tests -p 'test_codex*.py' -v
```

The 0.1.0 preview was exercised with Omarchy 4.5.0-3, Codex CLI 0.172.0, and two disposable conversations open in the real Codex terminal. In setup, the selected conversation read `MP9` from a circled practice key. OMarky saved that pairing; the other conversation received nothing. In a separate delivery check, the same terminal answered from both the attached picture and a fact from its own earlier turn. The desktop interactions were tested on an isolated Wayland display, not on anyone’s personal desktop.

Official protocol reference: <https://developers.openai.com/codex/app-server>
