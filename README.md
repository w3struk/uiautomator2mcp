# uiautomator2-mcp

MCP (Model Context Protocol) server for Android device automation via [uiautomator2](https://github.com/openatx/uiautomator2).

Enables AI agents (Claude, etc.) to control Android devices — tap, swipe, type text, take screenshots, manage apps, and more.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended) or pip
- Android device with USB debugging enabled (or an emulator)
- ADB installed and device visible via `adb devices`

## Quick Start

### Claude Code — one command to add & auto-run:

```bash
claude mcp add uiautomator2 -- uvx uiautomator2-mcp
```

That's it. Claude will auto-launch the server when needed.

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "uiautomator2": {
      "command": "uvx",
      "args": ["uiautomator2-mcp"]
    }
  }
}
```

### Codex CLI

Option 1 — add via CLI:

```bash
codex mcp add uiautomator2 -- uvx uiautomator2-mcp
```

Option 2 — add to `~/.codex/config.toml` (or project `.codex/config.toml`):

```toml
[mcp_servers.uiautomator2]
command = "uvx"
args = ["uiautomator2-mcp"]
```

### opencode

Add `uiautomator2-mcp` as a local MCP server in your opencode configuration (`opencode.json` or `opencode.jsonc`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "uiautomator2": {
      "type": "local",
      "command": ["uvx", "uiautomator2-mcp"],
      "enabled": true
    }
  }
}
```

After saving the configuration, start opencode and ask it to use the `uiautomator2` MCP tools. For more details, see the [opencode MCP servers documentation](https://opencode.ai/docs/ru/mcp-servers).

### Alternative methods

```bash
# Using pipx
pipx run uiautomator2-mcp

# Using pip (global install)
pip install uiautomator2-mcp
uiautomator2-mcp

# Using python -m
pip install uiautomator2-mcp
python -m uiautomator2_mcp

# From source
git clone https://github.com/stayer147/uiautomator2mcp.git
cd uiautomator2mcp
pip install -e .
uiautomator2-mcp
```

## Available Tools (45)

### Connection
| Tool | Description |
|------|-------------|
| `list_devices` | List devices visible to ADB and whether they are connected in the MCP session |
| `list_avds` | List configured Android Virtual Devices |
| `start_emulator` | Start an Android emulator in the background |
| `connect` | Connect to a device by serial/IP or auto-detect |
| `disconnect` | Disconnect one connected device from the MCP session |
| `device_info` | Get device model, screen size, Android version |

### UI Interaction
| Tool | Description |
|------|-------------|
| `tap` | Tap at screen coordinates |
| `multi_tap` | Tap the same coordinates multiple times |
| `double_tap` | Double-tap at coordinates |
| `long_tap` | Long-press at coordinates |
| `swipe` | Swipe between two points |
| `drag` | Drag between two points |
| `input_text` | Type text into the focused field |
| `press_key` | Press a device key (home, back, enter, etc.) |

### Compound Actions
| Tool | Description |
|------|-------------|
| `tap_and_wait` | Tap an element, wait for the next UI state, and return a fresh hierarchy snapshot |
| `tap_sequence` | Execute a validated sequence of taps, waits, typing, key presses, and swipes |

### Element Operations
| Tool | Description |
|------|-------------|
| `find_element` | Find element by text, resource ID, class, description, or XPath |
| `tap_element` | Find and tap an element |
| `double_tap_element` | Find and double-tap an element by selector or XPath |
| `set_element_text` | Set text in an input field |
| `element_exists` | Check if an element exists |
| `wait_element` | Wait for an element to appear |

### Screenshots & UI Hierarchy
| Tool | Description |
|------|-------------|
| `screenshot` | Take a screenshot as a saved file or inline JSON/base64 image payload |
| `dump_hierarchy` | Get XML UI hierarchy or a compact text summary |
| `get_ui_tree` | Get a richer JSON UI tree with element state for agent analysis |

#### Agent-friendly analysis outputs

- `screenshot(inline=True)` returns a JSON object with base64-encoded image data, MIME type, dimensions, and byte size so MCP clients can render the image without opening a filesystem path.
- `screenshot(save_path="/tmp/screen.jpg", quality=70, max_width=1280)` keeps the original save-to-disk workflow while allowing resize/compression controls.
- `get_ui_tree()` returns a structured JSON array of UI elements with `text`, `resource_id`, `class_name`, `content_desc`, `bounds`, `clickable`, `enabled`, `focused`, `selected`, `checked`, `scrollable`, and `index`.
- `dump_hierarchy(compact=True)` remains available as the lightweight one-line-per-element snapshot, while `dump_hierarchy(compact=False)` still returns raw XML.

### App Management
| Tool | Description |
|------|-------------|
| `app_start` | Launch an app |
| `app_stop` | Force-stop an app |
| `app_install` | Install an APK |
| `app_uninstall` | Uninstall an app |
| `app_clear` | Clear app data |
| `app_info` | Get app information |
| `app_list_running` | List running apps |
| `current_app` | Get current foreground app |

### Device Control
| Tool | Description |
|------|-------------|
| `screen_on` | Wake up the device |
| `screen_off` | Turn off the screen |
| `unlock` | Unlock the device |
| `open_notification` | Open notification panel |
| `open_quick_settings` | Open quick settings |
| `get_clipboard` | Get clipboard content |
| `set_clipboard` | Set clipboard content |

### Diagnostics
| Tool | Description |
|------|-------------|
| `clear_logs` | Clear logcat buffers on the device |
| `get_logs` | Read filtered logcat output by package, level, timestamp, and line count |

### Shell & Files
| Tool | Description |
|------|-------------|
| `shell` | Run a shell command on the device |
| `push_file` | Push a file to the device |
| `pull_file` | Pull a file from the device |

## Example Workflow

When only one device is connected in the MCP session, tools can omit `device_id`. If you connect multiple devices, pass `device_id` explicitly. Multi-device targeting is now supported consistently across the interactive UI, app-management, device-control, watcher, clipboard, and file-transfer tools, in addition to diagnostics tools like `clear_logs` and `get_logs`.

1. **List available devices**: `list_devices()`
2. **Connect** to one device: `connect("emulator-5554")`
3. **Optionally connect a second device**: `connect("emulator-5556")`
4. **Inspect a specific device**: `device_info(device_id="emulator-5554")`
5. **Take a screenshot** to see the current screen: `screenshot(device_id="emulator-5554")` or request inline JSON/base64 output with `screenshot(inline=True, max_width=1080, device_id="emulator-5554")`
6. **Dump hierarchy** to understand the lightweight UI structure: `dump_hierarchy(device_id="emulator-5554")`
7. **Get a richer UI tree** when state matters: `get_ui_tree(device_id="emulator-5554")`
8. **Tap elements** by text or resource ID
9. **Double-tap directly via XPath** when needed, e.g. `double_tap_element(xpath="//android.widget.TextView[@text='Gallery']")`
10. **Type text** into input fields
11. **Take another screenshot** to verify results

## Example bug-report workflow

1. **Clear old logs**: `clear_logs()`
2. **Reproduce the bug** using the UI interaction tools
3. **Capture error logs**: `get_logs(level="E", package="com.example.app")`
4. **Take a screenshot** of the failing state, optionally inline for vision analysis
5. **Dump hierarchy** for a lightweight snapshot or `get_ui_tree()` for richer element state

## Example login in one tool call

```python
tap_sequence(
  steps=[
    {"action": "tap_element", "resource_id": "com.example:id/email"},
    {"action": "input_text", "text": "user@example.com"},
    {"action": "tap_element", "resource_id": "com.example:id/password"},
    {"action": "input_text", "text": "correct horse battery staple"},
    {"action": "tap_element", "resource_id": "com.example:id/sign_in"},
    {"action": "wait", "resource_id": "com.example:id/home_feed", "timeout": 15}
  ]
)
```

## Publishing to PyPI

```bash
uv build
uv publish
```

After publishing, `uvx uiautomator2-mcp` will work out of the box.

## License

MIT
