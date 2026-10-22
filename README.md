# ps5-mcp

Give Claude (or any MCP client) eyes and hands on a jailbroken PS5: it can **take screenshots** of whatever
is on screen, **press controller buttons**, **move files** to and from the console and **read its log**. That
turns homebrew development into a loop the AI can run by itself — deploy, launch, look, press, look again —
without a capture card or Remote Play.

```
Claude ── MCP (stdio) ── server.py ──┬── TCP ── RemoteEye payload ── /dev/hid ── virtual controller
                                     └── FTP ── ftpsrv ── screenshot from the console's gallery
```

- **Input:** the `RemoteEye` payload adds a *virtual controller* through the same kernel device Remote Play
  uses and feeds it button and stick states. Verified in the system UI and inside homebrew apps.
- **Screen:** the virtual controller holds **Create**, the console saves a screenshot to its gallery, and
  the server fetches it over FTP and scales it down (~4 s per screenshot, full composited screen).
- **Files and log:** ftpsrv for files, klogsrv for the kernel log, which the server reads continuously so it
  can show what happened *before* a call, e.g. right before a crash.

> Tested on **firmware 13.00** only. The ioctl numbers and request layouts were read from that firmware's
> `libScePad`; other firmwares may differ. Reports welcome.

## Tools

| Tool | What it does |
|---|---|
| `ps5_screenshot(max_width=1280, crop)` | Screenshot of the current screen; `crop=(left, top, right, bottom)` in 0..1 cuts a part out of the full-resolution image, for small text |
| `ps5_press(buttons, hold_ms, repeat, gap_ms)` | Press buttons, e.g. `"cross"`, `"down"`, `"l1+r1"` |
| `ps5_stick(left_x, left_y, right_x, right_y, duration_ms, buttons)` | Tilt the sticks (-1..1) for a while |
| `ps5_touch(x, y, to_x, to_y, duration_ms, click)` | Tap or swipe on the touchpad (0..1). Experimental, needs RemoteEye v4 |
| `ps5_sequence(steps)` | Several presses, stick moves, touches, waits and screenshots in one call |
| `ps5_detach()` | Remove the virtual controller (when it is stuck or bound to the wrong user) |
| `ps5_klog(seconds, filter, since_s)` | Kernel log (payload output, crashes): the next `seconds`, plus the last `since_s` before the call |
| `ps5_send_payload(path, log_seconds)` | Send an ELF to the loader on port 9021, optionally return its log output |
| `ps5_ls(path)` | List a folder on the console |
| `ps5_upload(local_path, remote_path)` | Copy a file or folder to the console |
| `ps5_download(remote_path, local_path)` | Copy a file or folder from the console; small text files are returned directly |
| `ps5_delete(remote_path, recursive)` | Delete a file, or a folder with `recursive=true` |
| `ps5_move(from_path, to_path)` | Rename or move on the console |
| `ps5_status()` | Which console services answer, user, RemoteEye version and controller |

`ps5_sequence` saves a round trip per step, e.g. `[{"action": "press", "buttons": "down", "repeat": 3},
{"action": "press", "buttons": "cross"}, {"action": "wait", "duration_ms": 2000}, {"action": "screenshot"}]`.

Buttons: `cross circle square triangle up down left right l1 r1 l2 r2 l3 r3 options create ps touchpad`.

## Requirements

**Console** (jailbroken, FW 13.00 tested), running:
- an ELF loader on port **9021** (elfldr)
- **ftpsrv** on port 2121
- optional: **klogsrv** on port 3232 for `ps5_klog` and `log_seconds`

**Console settings:**
- *Settings → Captures and Broadcasts → Shortcuts for Create Button* must be **Standard** (holding Create
  takes a screenshot). With *Easy Screenshots*, holding opens the Create menu instead.

**PC:**
- Python 3.10+ with `pip install -r server/requirements.txt`
- HDR screenshots: with HDR output the console saves JPEG XR (`.jxr`). On Windows it is decoded with the
  built-in Windows Imaging Component; elsewhere install `imagecodecs` (`pip install imagecodecs`), or turn
  HDR off / set the screenshot format to PNG/JPG on the console.

## Setup

1. Get `RemoteEye.elf` from the [releases](../../releases) and put it in the repository root
   (or build it, see below). You don't have to send it yourself — the server starts it on first use.
2. Install the Python dependencies:
   ```
   pip install -r server/requirements.txt
   ```
3. Register the server with your MCP client. For Claude Code:
   ```
   claude mcp add ps5 --env PS5_IP=192.168.1.50 -- python /path/to/ps5-mcp/server/server.py
   ```
   or copy `.mcp.json.example` to `.mcp.json` in your project and set the paths and IP.
4. First use: a new controller appears and the console asks **"Who's using this controller?"** on its
   first input. Pick your user there — with your real controller, or let Claude do it (`ps5_press` to move,
   `cross` to select). Until then screenshots fail, since the Create button of an unassigned controller does
   nothing.

### Configuration

| Variable | Default | |
|---|---|---|
| `PS5_IP` | — (required) | Console address |
| `PS5_USER_ID` | first folder under `/user/home` | User id in hex |
| `PS5_FTP_PORT` / `PS5_FTP_USER` / `PS5_FTP_PASS` | `2121` / `anonymous` / `anonymous` | ftpsrv login |
| `PS5_REMOTEEYE_ELF` | `payload/RemoteEye/out/RemoteEye.elf`, then `./RemoteEye.elf` | Payload to start |
| `PS5_DELETE_SHOTS` | off | Delete each screenshot after fetching it (see below) |
| `PS5_SAVE_DIR` | — | Keep the original full-resolution file of every screenshot in this folder |
| `PS5_ELFLDR_PORT` / `PS5_KLOG_PORT` / `PS5_REMOTEEYE_PORTS` | `9021` / `3232` / `3321-3340` | Other ports |

**Screenshots pile up in the Media Gallery.** Deleting them over FTP is possible (`PS5_DELETE_SHOTS=1`) but
leaves a blank tile per screenshot, because the gallery's database keeps the entry. So by default they stay;
clear them in the Media Gallery from time to time.

## Building RemoteEye

RemoteEye is written in C# and compiled to a payload ELF with
[SharpProspero](https://github.com/SvenGDK/SharpProspero). Release builds are made from SharpProspero
commit `5dfdb4b0b166e83dc9ae275001afb695f12c7174`, unmodified. Clone it next to this repository (or set
`SHARPPROSPERO_ROOT`), install its prerequisites, then:

```
pwsh ./payload/build.ps1                      # -> payload/RemoteEye/out/RemoteEye.elf
pwsh ./payload/build.ps1 -Send -Ip 192.168.1.50
```

A payload can't be unloaded, and a new instance can't take over the old one's port. RemoteEye therefore
listens on `3320 + BuildTag`; raise `BuildTag` in `Program.cs` when you change it, and the server picks the
newest instance it finds on 3321–3340 (and removes the controller of any older one, so only one is left).
The server only starts RemoteEye itself when none is running; to switch to a new build, send it
(`-Send`, or `ps5_send_payload`). The console may then ask once more who is using the controller.

## Tests

```
python -m unittest server/test_server.py
```

They run without a console, against a fake RemoteEye, klogsrv and FTP server.

## Limitations

- One virtual controller, user 1 by default. The console may show a controller-assignment prompt after
  RemoteEye starts.
- Touch input (`ps5_touch`) is unverified: where the touch points go in the virtual pad's state is assumed
  from `ScePadTouchData`, not read from the firmware.
- No starting or closing apps: the system services for that hang when called from a payload.
- Screenshots take ~4 s and appear in the gallery. Screens that forbid captures can't be seen.
- Holding Create shows a short "screenshot saved" notification that may appear in the next screenshot.
- RemoteEye serves one client at a time; a second MCP session waits until the first call is done.

## How it works

See [docs/how-it-works.md](docs/how-it-works.md) for how the virtual controller was found and what does
*not* work from a payload (scePadInit, the share/capture services and others hang).

## License

GPL-3.0, like SharpProspero, which RemoteEye is built with and statically links.
