# OMarky

Circle a detail on your desktop and send it, with a note, to the conversation already working on it.

OMarky is a community plugin for the [Omarchy](https://omarchy.org) desktop. Click the mark in the bar to freeze the focused monitor, draw, undo, crop, or copy a marked PNG. **Send is separate from Copy.** Before you can send a desktop image, you pick a conversation and try the real pen on a practice card. Only the card goes to your chat during setup. No account password, API key or separate AI subscription goes into OMarky.

## Install

```sh
omarchy plugin add https://github.com/dumpsteredition/omarky.git --enable
```

Review Omarchy’s prompt: third-party plugins run with your user’s permissions. OMarky does not install packages, switch your default agent, or overwrite your keybindings. The public plugin ID is `io.github.dumpsteredition.omarky`.

**Requirements:** Omarchy’s Quickshell bar and Hyprland desktop, Python 3 with PyGObject and Pycairo, GTK 3, GTK Layer Shell, `hyprctl` and `grim`. Copy uses `wl-copy` from `wl-clipboard`. On Arch the relevant packages are `python-gobject`, `python-cairo`, `gtk3`, `gtk-layer-shell`, `grim`, and `wl-clipboard`. For chat delivery you also need a supported, signed-in agent with image input. Marking and copying do not require an agent.

## Try it

1. Open OMarky from the bar and click **Choose a chat**. If none are connected yet, select **Open Codex picker…**. In Codex’s own terminal, choose the existing conversation you want to keep working in and leave it open. Return to OMarky and click **Rescan**. If the picker button is unavailable, use the [terminal pairing instructions](docs/codex.md).
2. Choose the conversation by project and title. A practice card appears. Circle **any one key** and press **Send**. Your chat gets the marked PNG and editable question. Its answer must identify that key before OMarky saves this connection.
3. Now open OMarky over your work. Draw, add a note if you like, and press **Send**. By default the image is the full focused display, including everything visible on it; choose **Crop** to share only a region. Copy sends nothing to your agent. Escape closes the canvas.

The cog reopens setup. You can pair a second conversation the same way, and then choose between verified conversations while marking. If the chosen chat is gone, OMarky does not switch to a different one. If a send’s outcome is uncertain, inspect the original chat before sending anything again. A newly opened pairing may require the card again.

**Optional shortcut:** right-click the OMarky bar icon to copy a suggested free Hyprland binding. Review and add the line yourself, or just keep using the bar. See [shortcut details](docs/shortcut.md).

## Agent support

**Codex:** direct Send targets a conversation open in the paired Codex terminal. Your agent uses its own account, model, and permissions; a local connection to the terminal does not make a cloud model run offline.

**Claude Code and Grok:** use **Copy**, then paste into the chat you choose. Neither is offered as a direct Send destination in this preview. [Claude Code channels](https://code.claude.com/docs/en/channels) require an opted-in session; [Grok’s documented scripting interface](https://docs.x.ai/build/cli/headless-scripting) does not establish delivery to an already-open terminal. OMarky does not open a substitute chat.

## Update or remove

```sh
omarchy plugin update io.github.dumpsteredition.omarky
omarchy plugin disable io.github.dumpsteredition.omarky
omarchy plugin remove io.github.dumpsteredition.omarky
```

Close any Codex pairing terminal first. If you added a shortcut, remove that one `o.bind(...)` line from your Hyprland bindings. Preferences are separate from the plugin: by default `~/.config/omarky/verified-targets.json` records your verified destination identities and labels. Removing the `omarky` preferences directory under your own `$XDG_CONFIG_HOME` (or `~/.config`) forgets them. Removing OMarky does not delete your agent conversations.

## Development

```sh
omarchy plugin validate .
/usr/bin/python3 -B -m unittest discover -s tests -v
```

The native GTK/Cairo drawing experience was adapted from Rare Tools’ annotator. OMarky is independent of Omarchy, OpenAI, Anthropic and xAI. Released under the [MIT License](LICENSE).
