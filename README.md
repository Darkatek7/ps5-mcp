# ps5-mcp

Give Claude (or any MCP client) eyes and hands on a jailbroken PS5: it can **take screenshots** of whatever
is on screen, **press controller buttons**, **start and close titles**, **move files** to and from the console
and **read its log**. That turns homebrew development into a loop the AI can run by itself — deploy, launch,
look, press, look again, close — without a capture card or Remote Play.

```
Claude ── MCP (stdio) ── server.py ──┬── TCP ── RemoteEye payload ──┬── /dev/hid ── virtual controller
                                     │                              └── titles, browser, rest mode, power off
                                     ├── FTP ── ftpsrv ── screenshot from the console's gallery, files
                                     ├── TCP ── klogsrv ── kernel log
                                     └── UDP ── Remote Play discovery ── power state, wake from rest mode
```

- **Input:** the `RemoteEye` payload adds a *virtual controller* through the same kernel device Remote Play
  uses and feeds it button and stick states. Verified in the system UI and inside homebrew apps.
- **Screen:** the virtual controller holds **Create**, the console saves a screenshot to its gallery, and
  the server fetches it over FTP and scales it down (~4 s per screenshot, full composited screen).
- **Titles and power:** RemoteEye (build 7 and newer) asks the system to start or close a title, names the
  app in front, opens the web browser, puts the console in rest mode or turns it off. With an older RemoteEye
  the small `titlectl` payload does it instead: the server writes the action into the ELF, sends it, and reads
  the answer back from the kernel log.
- **Files and log:** ftpsrv for files, klogsrv for the kernel log, which the server reads in the background
  once a tool needs it, so it can show what happened *before* a call, e.g. right before a crash.
- **Power:** Remote Play's discovery protocol tells whether the console is on or resting (it also finds the
  console, so `PS5_IP` is optional), and its wake message brings it back from rest mode.

> Tested on **firmware 13.00** only. The ioctl numbers and request layouts were read from that firmware's
> `libScePad`; other firmwares may differ. Release builds include the SDK fix that lets payloads start on
> 13.40 and later (see [Building RemoteEye](#building-remoteeye)). Reports welcome.

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
| `ps5_titles(contains)` | Installed titles with their names |
| `ps5_running()` | The app in front (title id and name), or the home screen |
| `ps5_launch(title_id)` | Start a title, e.g. `"PPSA01234"` |
| `ps5_close(title_id, quit_file, timeout_s)` | Close a title the way the system's *Close App* does; or, with `quit_file`, write the file an app of your own watches and wait until it has ended itself |
| `ps5_open_url(url)` | Open the console's web browser at a URL (as a card over the running app) |
| `ps5_rest()` | Put the console in rest mode; the payloads keep running |
| `ps5_power_off(confirm)` | Turn the console off; **ends the jailbreak**, and `ps5_wake` cannot turn it on again. Only with `confirm=true` |
| `ps5_wake(timeout_s)` | Wake it from rest mode over the network; needs a Remote Play registration (see [Rest mode and waking](#rest-mode-and-waking)) |
| `ps5_pair(pin)` | Register this PC as a Remote Play device once, with the PIN from *Link Device*; saves the key for `ps5_wake` |
| `ps5_rescan()` | Ask ShadowMountPlus to register newly uploaded homebrew (`/data/homebrew/<title id>`) |
| `ps5_processes(contains)` | The console's process list (pid, state, name, wait channel); needs RemoteEye v6 |
| `ps5_kill_payload(pid)` | End a hanging payload (`payload.elf` only, never a title or system process); needs RemoteEye v6 |
| `ps5_ls(path)` | List a folder on the console |
| `ps5_upload(local_path, remote_path)` | Copy a file or folder to the console |
| `ps5_download(remote_path, local_path)` | Copy a file or folder from the console; small text files are returned directly |
| `ps5_delete(remote_path, recursive)` | Delete a file, or a folder with `recursive=true` |
| `ps5_move(from_path, to_path)` | Rename or move on the console |
| `ps5_status()` | Power state, which console services answer, user, RemoteEye version and controller |
| `ps5_selftest(screenshot)` | Checks every part the tools rely on (console and firmware, loader, FTP, user, RemoteEye, title calls, kernel log, pairing) without changing anything; good on a new console or firmware, and for bug reports |

A typical homebrew loop: `ps5_upload` the app to `/data/homebrew/`, `ps5_rescan`, `ps5_launch`,
`ps5_screenshot` and `ps5_press` to try it, `ps5_klog(since_s=30)` if it crashed, `ps5_close` when done.

`ps5_sequence` saves a round trip per step, e.g. `[{"action": "press", "buttons": "down", "repeat": 3},
{"action": "press", "buttons": "cross"}, {"action": "wait", "duration_ms": 2000}, {"action": "screenshot"}]`.

Buttons: `cross circle square triangle up down left right l1 r1 l2 r2 l3 r3 options create ps touchpad`.

## Requirements

**Console** (jailbroken, FW 13.00 tested), running:
- an ELF loader on port **9021** (elfldr)
- **ftpsrv** on port 2121
- **klogsrv** on port 3232 for `ps5_klog` and `log_seconds`, and for the title and power tools when RemoteEye is
  older than build 7 (they then read titlectl's answer from the log)
- optional: **ShadowMountPlus** with its API on port 10101 for `ps5_rescan`

**Console settings:**
- *Settings → Captures and Broadcasts → Shortcuts for Create Button* must be **Standard** (holding Create
  takes a screenshot). With *Easy Screenshots*, holding opens the Create menu instead.

**PC:**
- Python 3.10+ with `pip install -r server/requirements.txt`
- HDR screenshots: with HDR output the console saves JPEG XR (`.jxr`). On Windows it is decoded with the
  built-in Windows Imaging Component; elsewhere install `imagecodecs` (`pip install imagecodecs`), or turn
  HDR off / set the screenshot format to PNG/JPG on the console.

## Setup

1. Get `RemoteEye.elf` and `titlectl.elf` from the [releases](../../releases) and put them in the repository
   root (or build them, see below). You don't have to send them yourself — the server does when needed.
2. Install the Python dependencies:
   ```
   pip install -r server/requirements.txt
   ```
3. Register the server with your MCP client. For Claude Code:
   ```
   claude mcp add ps5 -- python /path/to/ps5-mcp/server/server.py
   ```
   or copy `.mcp.json.example` to `.mcp.json` in your project and set the path. The server finds the console
   on the local network by itself; with several consoles (or none answering) set `PS5_IP`, e.g.
   `--env PS5_IP=192.168.1.50`.
4. First use: a new controller appears and the console asks **"Who's using this controller?"** on its
   first input. Pick your user there — with your real controller, or let Claude do it (`ps5_press` to move,
   `cross` to select). Until then screenshots fail, since the Create button of an unassigned controller does
   nothing.
5. `ps5_selftest` shows whether everything answers.

### Configuration

| Variable | Default | |
|---|---|---|
| `PS5_IP` | the one PS5 that answers a discovery search | Console address |
| `PS5_USER_ID` | first folder under `/user/home` | User id in hex |
| `PS5_FTP_PORT` / `PS5_FTP_USER` / `PS5_FTP_PASS` | `2121` / `anonymous` / `anonymous` | ftpsrv login |
| `PS5_REMOTEEYE_ELF` | `payload/RemoteEye/out/RemoteEye.elf`, then `./RemoteEye.elf` | Payload to start |
| `PS5_TITLECTL_ELF` | `payload/titlectl/out/titlectl.elf`, then `./titlectl.elf` | Payload for the title tools |
| `PS5_SMP_PORT` | `10101` | ShadowMountPlus API port |
| `PS5_DELETE_SHOTS` | off | Delete each screenshot after fetching it (see below) |
| `PS5_SAVE_DIR` | — | Keep the original full-resolution file of every screenshot in this folder |
| `PS5_REGIST_KEY` | the one `ps5_pair` saved | RegistKey of a Remote Play registration, for `ps5_wake` |
| `PS5_REGIST_FILE` | `~/.ps5-mcp/regist.json` | Where `ps5_pair` saves the registration |
| `PS5_ELFLDR_PORT` / `PS5_KLOG_PORT` / `PS5_REMOTEEYE_PORTS` | `9021` / `3232` / `3321-3340` | Other ports |
| `PS5_DDP_PORT` | `9302` | Remote Play discovery port (power state, waking) |

**Screenshots pile up in the Media Gallery.** Deleting them over FTP is possible (`PS5_DELETE_SHOTS=1`) but
leaves a blank tile per screenshot, because the gallery's database keeps the entry. So by default they stay;
clear them in the Media Gallery from time to time.

### Rest mode and waking

`ps5_rest` puts the console in rest mode the way its power menu does (`sceSystemStateMgrEnterStandby`, from
RemoteEye or titlectl). The payloads keep running and answer again once it wakes, so the jailbreak
stays. `ps5_power_off(confirm=true)` turns the console off (`sceSystemStateMgrTurnOff`); that ends the jailbreak,
and only the power button or a controller turns it on again, so the tool refuses without `confirm=true`.

`ps5_wake` sends Remote Play's wake message, which the console only accepts with the key of a Remote Play
registration of this PC. Register once with `ps5_pair`: open *Settings → System → Remote Play → Link Device*
on the console and pass the 8-digit PIN it shows. The server reads the user's account id (signed in to PSN or
activated offline) from the registry, registers, and saves the key to `PS5_REGIST_FILE`; a key from
[pyremoteplay](https://github.com/ktnrg45/pyremoteplay) or chiaki works as `PS5_REGIST_KEY` too.

The console listens for the wake message in rest mode only with both **Stay Connected to the Internet** and
**Enable Turning On PS5 from Network** in *Settings → System → Power Saving → Features Available in Rest Mode*.
In rest mode it may stop answering on the network (`ps5_status` then says `power: no answer`); with those
settings the wake message still gets through. Mind that staying connected
also lets the console download system updates in rest mode, which a jailbroken console should not: block
Sony's update hosts with a DNS filter (e.g. [nanoDNS](https://github.com/drakmor/nanoDNS) with the console's DNS
set to 127.0.0.1) and switch off automatic update downloads. Without those settings, wake the console with a
controller or the TV (HDMI-CEC).

> Tested on 13.00: `ps5_pair` with a *Link Device* PIN, then `ps5_rest` and `ps5_wake` twice; the console was on
> again 11–13 s after the wake message, with the jailbreak's payloads still running. Two things that did not work there:
> a registration whose `SRC3` probe came from another port than 9303 (the console still answers `RES3`, but
> then refuses the registration: `CE-110033-8` on screen, HTTP 403), and a PIN generated by a payload
> (`sceRemoteplayGeneratePinCode`), refused with `REGIST_DEVICE_MISS` because a payload sees no foreground user.

## Building RemoteEye

RemoteEye is written in C# and compiled to a payload ELF with
[SharpProspero](https://github.com/SvenGDK/SharpProspero). Release builds are made from SharpProspero commit
`e78aaa107cfba578139e9baf6d76a34d2785befc` (branch `sharpprospero-crt-allproc-fw13` of
[Darkatek7/SharpProspero](https://github.com/Darkatek7/SharpProspero), proposed upstream as
[SvenGDK/SharpProspero#6](https://github.com/SvenGDK/SharpProspero/pull/6)). It is the earlier pin
`5dfdb4b` plus one fix: that version's payload runtime looked for the kernel's process list at the 13.00
address on every 13.x firmware, so on 13.40 and later a payload stopped before its own code ran. Once the fix
is merged upstream, any later SharpProspero works too.

Build with the **.NET SDK 10.0.203** (NativeAOT runtime pack 10.0.7). A build with runtime pack 10.0.14
crashed on FW 13.00 before RemoteEye's own code ran (SIGILL in the payload's start-up), so the SDK is pinned.
The release builds come from CI (`.github/workflows/payloads.yml`); the same build in Docker, from the
repository root with SharpProspero next to it:

```
docker run --rm -v "$PWD:/repo" -v "$PWD/../SharpProspero:/sp" mcr.microsoft.com/dotnet/sdk:10.0.203 bash -c \
  "apt-get update -qq && apt-get install -y -qq clang lld zlib1g-dev && pwsh /repo/payload/build.ps1 -SdkRoot /sp"
```

Or natively: clone SharpProspero next to this repository as `SharpProspero` (or set `SHARPPROSPERO_ROOT`),
install its prerequisites with that SDK, then:

```
pwsh ./payload/build.ps1                      # -> payload/RemoteEye/out/RemoteEye.elf
pwsh ./payload/build.ps1 -Send -Ip 192.168.1.50
```

Rebuilding the same commit gives the same ELF except for a 16-byte build id.

A payload can't be unloaded, and a new instance can't take over the old one's port. RemoteEye therefore
listens on `3320 + BuildTag`; raise `BuildTag` in `Program.cs` when you change it, and the server picks the
newest instance it finds on 3321–3340 (and removes the controller of any older one, so only one is left).
The server only starts RemoteEye itself when none is running; to switch to a new build, send it
(`-Send`, or `ps5_send_payload`). The console may then ask once more who is using the controller.

## Building titlectl

titlectl is C, built with the [PS5 payload SDK](https://github.com/ps5-payload-dev/sdk) v0.45 (Linux, WSL or
Docker as in the CI workflow); the build is byte-for-byte reproducible:

```
PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk bash payload/titlectl/build.sh   # -> payload/titlectl/out/titlectl.elf
```

It has no command line: the server finds the marker `PS5MCP-ARGS-V1:` in the ELF and writes the action and
title id after it before each send, so the one build serves every title.

## Tests

```
python -m unittest server/test_server.py
```

They run without a console, against a fake RemoteEye, klogsrv, elfldr, FTP server, Remote Play discovery and
registration. CI runs them on every push (`.github/workflows/tests.yml`).

## Limitations

- One virtual controller, user 1 by default. The console may show a controller-assignment prompt after
  RemoteEye starts (also after every console restart). While that prompt is open, the real controller can't do
  anything either; `ps5_detach` closes it.
- Input is sent like a real controller sends it: a held button is reported again every 20 ms (`PS5_REPORT_MS`),
  the release three times, and a controller that was just added gets a second before the first input. With one
  report per press, presses were lost while a real DualSense was in use as well.
- Touch input (`ps5_touch`) is unverified: where the touch points go in the virtual pad's state is assumed
  from `ScePadTouchData`, not read from the firmware.
- `ps5_close` ends a title through the system, like *Close App*. Apps that render with the GPU are better
  ended on their own terms: give them a quit file to watch and use `quit_file`. Titles are never killed.
- `ps5_launch` starts a title for the user in front; homebrew must be registered first (`ps5_rescan`).
- Screenshots take ~4 s and appear in the gallery. Screens that forbid captures can't be seen.
- Holding Create shows a short "screenshot saved" notification that may appear in the next screenshot.
- RemoteEye serves one client at a time; a second MCP session waits up to 15 s for the first call to finish.
- klogsrv goes silent for everyone while one client is stuck (seen after rest mode). The server reconnects
  when it notices, and its log reader disconnects after 10 minutes without a log tool, but another program
  that keeps a stuck connection open keeps klogsrv silent until it lets go.

## How it works

See [docs/how-it-works.md](docs/how-it-works.md) for how the virtual controller was found and what does
*not* work from a payload (scePadInit, the share/capture services and others hang).

## License

GPL-3.0, like SharpProspero, which RemoteEye is built with and statically links. titlectl's launch and close
calls follow [ps5-homebrew-dev-protocol](https://github.com/manoma080/ps5-homebrew-dev-protocol)
(BlackBearReloaded), which takes them from John Törnblom's shsrv and websrv
([ps5-payload-dev](https://github.com/ps5-payload-dev)); both are GPL-3.0.
