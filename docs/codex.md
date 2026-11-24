# Codex pairing and routing

OMarky connects to the Codex CLI app-server protocol and sends the marked PNG plus note as one `turn/start` input to an explicitly selected, already-loaded conversation. It does not create a new chat, resume a detached thread, fork history, edit Codex configuration, copy authentication, or invoke an alternate store writer.

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
- Discovery calls `thread/list` and emits only loaded `idle` or `active` threads from a live OMarky-owned TUI lease. It does not load or resume a thread. Titles and project names are plain text labels only.
- Before a send, OMarky re-reads the selected thread with `includeTurns: false`. `active` means `busy`; `notLoaded`/missing targets mean `gone`; unrecognized state or app-server failure fails closed.
- A send uses `turn/start` with the exact selected `threadId`, the provided `request_id` as `clientUserMessageId`, and the note plus marked PNG data URL. The adapter does not send an unmarked/original image, retry a request automatically, or start a second turn to check the first.
- With `wait_reply=False`, `ok` means Codex returned an accepted `turn/start` response, not that a model completed. With `wait_reply=True`, OMarky polls `thread/turns/list` for that returned `turn.id` and reports `ok` only after the same turn is `completed` and its final `agentMessage` text is available. A failed turn is `error`; interruption, timeout, transport loss after acceptance, or missing final text is `uncertain`. Check the paired conversation before any manual retry.
- OMarky rejects app-server initiated approval/user-input requests; it is an image sender, not a general Codex approval client.

The dispatcher in `app/routes.py` exposes only Codex in this public v1 and imports the adapter lazily. Missing setup is reported as guidance, never represented as a conversation.

## Verification

Focused tests are standard-library `unittest` tests:

```sh
python3 -m unittest discover -s tests -p 'test_codex*.py' -v
```

The transport protocol can be exercised without a real model call by using a disposable `CODEX_HOME`, starting `codex app-server --listen unix://...`, and connecting through `codex app-server proxy --sock ...`. The lane's transport check creates and reads only a synthetic empty thread; it does not start a model turn or access normal user conversations.

Official protocol reference: <https://developers.openai.com/codex/app-server>
