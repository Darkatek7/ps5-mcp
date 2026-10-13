# ps5-mcp

Give Claude (or any MCP client) eyes and hands on a jailbroken PS5: it can **take screenshots** of whatever
is on screen and **press controller buttons**. That turns homebrew development into a loop the AI can run
by itself — deploy, launch, look, press, look again — without a capture card or Remote Play.

```
Claude ── MCP (stdio) ── server.py ──┬── TCP ── RemoteEye payload ── /dev/hid ── virtual controller
                                     └── FTP ── ftpsrv ── screenshot from the console's gallery
```

- **Input:** the `RemoteEye` payload adds a *virtual controller* through the same kernel device Remote Play
  uses and feeds it button and stick states. Works in the system UI and inside apps and games.
- **Screen:** the virtual controller holds **Create**, the console saves a screenshot to its gallery, and
  the server fetches it over FTP and scales it down (~4 s per screenshot, full composited screen).

> Tested on **firmware 13.00** only. The ioctl numbers and request layouts were read from that firmware's
> `libScePad`; other firmwares may differ. Reports welcome.

## Tools

| Tool | What it does |
|---|---|
| `ps5_screenshot(max_width=1280)` | Screenshot of the current screen, returned as an image |
| `ps5_press(buttons, hold_ms, repeat, gap_ms)` | Press buttons, e.g. `"cross"`, `"down"`, `"l1+r1"` |
| `ps5_stick(left_x, left_y, right_x, right_y, duration_ms, buttons)` | Tilt the sticks (-1..1) for a while |
| `ps5_klog(seconds, filter)` | Read the kernel log from klogsrv (payload output, crashes) |
| `ps5_send_payload(path)` | Send an ELF to the loader on port 9021 |
| `ps5_status()` | Which console services answer, RemoteEye state |

Buttons: `cross circle square triangle up down left right l1 r1 l2 r2 l3 r3 options create ps touchpad`.

## Requirements

**Console** (jailbroken, FW 13.00 tested), running:
- an ELF loader on port **9021** (elfldr)
- **ftpsrv** on port 2121
- optional: **klogsrv** on port 3232 for `ps5_klog`

**Console settings:**
- *Settings → Captures and Broadcasts → Shortcuts for Create Button* must be **Standard** (holding Create
  takes a screenshot). With *Easy Screenshots*, holding opens the Create menu instead.

**PC:**
- Python 3.10+ with `pip install -r server/requirements.txt`
- **Windows** for HDR screenshots: with HDR output the console saves JPEG XR (`.jxr`), which is decoded with
  the built-in Windows Imaging Component. On other systems, turn HDR off or set the screenshot format to
  PNG/JPG on the console.

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
4. First use: a new controller appears on the console and it asks **"Who's using this controller?"**.
   Let Claude answer it (`ps5_press right`, then `ps5_press cross`) or pick your user on the real controller.

### Configuration

| Variable | Default | |
|---|---|---|
| `PS5_IP` | — (required) | Console address |
| `PS5_USER_ID` | first folder under `/user/home` | User id in hex |
| `PS5_FTP_PORT` / `PS5_FTP_USER` / `PS5_FTP_PASS` | `2121` / `anonymous` / `anonymous` | ftpsrv login |
| `PS5_REMOTEEYE_ELF` | `payload/RemoteEye/out/RemoteEye.elf`, then `./RemoteEye.elf` | Payload to start |
| `PS5_DELETE_SHOTS` | off | Delete each screenshot after fetching it (see below) |

**Screenshots pile up in the Media Gallery.** Deleting them over FTP is possible (`PS5_DELETE_SHOTS=1`) but
leaves a blank tile per screenshot, because the gallery's database keeps the entry. So by default they stay;
clear them in the Media Gallery from time to time.

## Building RemoteEye

RemoteEye is written in C# and compiled to a payload ELF with
[SharpProspero](https://github.com/SvenGDK/SharpProspero). Clone it next to this repository (or set
`SHARPPROSPERO_ROOT`), install its prerequisites, then:

```
pwsh ./payload/build.ps1                      # -> payload/RemoteEye/out/RemoteEye.elf
pwsh ./payload/build.ps1 -Send -Ip 192.168.1.50
```

A payload can't be unloaded, and a new instance can't take over the old one's port. RemoteEye therefore
listens on `3320 + BuildTag`; raise `BuildTag` in `Program.cs` when you change it, and the server picks the
newest instance it finds on 3321–3340.

## Limitations

- One virtual controller, user 1 by default. The console may show a controller-assignment prompt after
  RemoteEye starts.
- Screenshots take ~4 s and appear in the gallery. Screens that forbid captures can't be seen.
- Holding Create shows a short "screenshot saved" notification that may appear in the next screenshot.
- RemoteEye serves one client at a time; a second MCP session waits until the first call is done.

## How it works

See [docs/how-it-works.md](docs/how-it-works.md) for how the virtual controller was found and what does
*not* work from a payload (scePadInit, the share/capture services and others hang).

## License

GPL-3.0, like SharpProspero, which RemoteEye is built with and statically links.
