# Optional Hyprland shortcut

Nothing reads keybindings while the bar is idle. Right-click the OMarky mark only if you want a shortcut suggestion; that explicit action runs `bin/omarky-shortcut --copy`. It asks the running compositor for `hyprctl binds -j`, chooses one free `Super+...` chord, and copies one Lua `o.bind(...)` line with `wl-copy`. It never writes or reloads Hyprland config. If the active binding list is unavailable, malformed, or too ambiguous to prove a chord free, it fails closed and copies nothing.

To inspect the suggested line without copying it, run from the installed plugin directory:

```sh
/usr/bin/python3 bin/omarky-shortcut
```

To copy it explicitly from a terminal:

```sh
/usr/bin/python3 bin/omarky-shortcut --copy
```

Review the line before pasting it into `~/.config/hypr/bindings.lua`. The command points at this plugin's `app/omarky.py` and uses its `launch` action, so a first launch still enters OMarky's guided setup. The bar cog remains the direct way to reopen `setup` to choose or re-pair a conversation.

After pasting, check the key once more and apply the user's own edit with:

```sh
hyprctl reload
hyprctl configerrors
```

The helper deliberately emits `o.bind(...)`, not `o.rebind(...)`: `o.bind` adds a binding, while `o.rebind` unbinds an existing chord first. This follows the installed Omarchy Lua helper (`/usr/share/omarchy/default/hypr/helpers.lua`, `o.bind` calls `hl.bind`) and its keybinding guide (`/usr/share/omarchy/default/agents/skills/omarchy/hyprland.md`). The installed Hyprland Lua stub documents `hl.bind(keys: string, dispatcher, opts?)` in `/usr/share/hypr/stubs/hl.meta.lua`; the installed default bindings use the same key-string form. The helper checks the complete `hyprctl binds -j` result, including bindings reported in other submaps, compares exact modifier-mask/key pairs, and rejects an ambiguous blank-keysym record for a candidate modifier chord rather than guessing its keycode.

The helper needs a running Hyprland session and `wl-copy` only for `--copy`; the no-argument mode prints the one-line suggestion and does not touch the clipboard. No shortcut is configured until the user reviews and pastes the copied line.
