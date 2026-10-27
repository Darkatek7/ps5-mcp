# How it works

Everything here was measured on one console, firmware 13.00, with the payload loaded by elfldr.

## Reaching system libraries from a payload

A payload that lists a system module as `DT_NEEDED` the host process can't supply never starts (declaring
`libSceSysmodule` was enough). So modules are loaded at run time:

- `sceKernelLoadStartModule("/system/common/lib/<name>")` loads them fine.
- libkernel's `sceKernelDlsym` answered `0x80020003` for every symbol, `getpid` included.
- SharpProspero's CRT resolves its own imports with a kernel-side lookup, `__sp_kernel_dynlib_dlsym(pid,
  handle, name)`. Called with `pid = -1` and the handle from `sceKernelLoadStartModule`, it resolves by name.
- The CRT's `dlopen` crashed the payload on a module the host had not loaded.

## What hangs

Called from a payload process, these never return:

- `scePadInit` (it initializes `libSceMbus` first)
- `sceShareInitialize` (so `sceShareCaptureScreenshot` is out of reach)
- loading `libSceSystemService`

They talk to system services over IPC, which apparently never answers a payload process. `libSceAvcap2`
(live composited frames) loads and resolves but was not pursued for the same reason.

That holds for SharpProspero payloads, which load the module themselves. A payload built with the C
[ps5-payload-sdk](https://github.com/ps5-payload-dev/sdk), linked against `libSceSystemService` and
`libSceUserService`, calls `sceSystemServiceLaunchApp` and friends and gets answers (see *Titles* below).

## FW 13.40 and later

SharpProspero's payload runtime finds its own process through the kernel's `allproc` list before anything
else runs. Up to commit `5dfdb4b` it chose that address by major version only, so every 13.x firmware got
13.00's `0x28C5E00`, while 13.40, 13.42 and 13.60 have it at `0x28C9E80`. There the walk found nothing and the
payload stopped before its own code: sent to elfldr, it simply never started. RemoteEye is built with the fix
(SvenGDK/SharpProspero#6).

## Reading the system's code

System modules are mapped execute-only: reading their code from memory kills the process. Instead, ftpsrv's
`SELF` command switches it to decrypting SELF files on download, which gives a plain ELF of e.g.
`/system/common/lib/libScePad.sprx`. Exports are named by NID (`base64(sha1(name + suffix)[0:8])`), so
known names can be matched and disassembled.

## The virtual controller

`libScePad` exports `scePadVirtualDeviceAddDevice`, `scePadVirtualDeviceInsertData` and
`scePadVirtualDeviceDeleteDevice` — the API Remote Play uses to feed its controller into the system. Each
is a thin wrapper around one `ioctl` on the file descriptor `scePadInit` keeps, which is simply
`open("/dev/hid", O_RDONLY)`. Since `scePadInit` hangs, RemoteEye opens `/dev/hid` itself and issues the
same ioctls:

| Call | ioctl | Argument |
|---|---|---|
| add | `0xC018482A` | `{ u32 op = 3; u64 desc*; u64 int* handleOut }` |
| insert | `0x8018482C` | `{ u32 handle; u64 state*; u8 0 }` |
| delete | `0x80104850` | `{ u8 op = 5; u32 handle @8 }` |

- `desc` (0xA8 bytes): device class at 0, user id at 0x1C, subtype at 0x20. Class and subtype come from two
  tables indexed by the public device type 1..12: type 1 is class 3, subtype 1.
- `state` (0xA8 bytes): buttons at 0xC (ScePadButton bits), sticks lx ly rx ry at 0x10, L2/R2 at 0x14,
  a 1 at 0x17, touch count at 0x1C, motion data from 0x78.

RemoteEye's `STATE` command fills buttons, sticks and triggers; `BLOCK` (v4) takes a whole state block in hex,
so fields like the touch points can be tried from the PC without another build. A state stays in effect until
the next one. The system treats the device as a new controller and asks who is
using it; once a user is picked, the shell and apps read it like a real pad.

Button bits used: Create `0x1`, L3 `0x2`, R3 `0x4`, Options `0x8`, Up `0x10`, Right `0x20`, Down `0x40`,
Left `0x80`, L2 `0x100`, R2 `0x200`, L1 `0x400`, R1 `0x800`, Triangle `0x1000`, Circle `0x2000`,
Cross `0x4000`, Square `0x8000`, PS `0x10000`, Touchpad `0x100000`.

## The screen

Rather than capturing frames inside a payload, the tool lets the system do it: holding Create on the virtual
controller makes the console save a screenshot (`/user/av_contents/photo/<title id>/<title id>/<n>/*.jxr`
with HDR output, plus `.meta` and `.dat`). A gallery collects hundreds of `<n>` folders, and listing all of
them on every poll added ~20 s to a screenshot once ~430 had piled up. Instead the server lists the folders
down to `<n>` with their modify times (about a dozen `MLSD` calls) before and after, and only looks inside
folders whose time changed. It waits for the new file's size to settle, downloads it and decodes it.

Finding RemoteEye took another ~20 s on the first call: the console lets connections to closed ports time out
rather than refusing them, so the 20 ports are probed in parallel.

HDR screenshots hold 16-bit PQ (SMPTE ST 2084) values. The Windows Imaging Component turns them into a normal
8-bit picture; elsewhere the server decodes them with `imagecodecs`, converts PQ to nits, takes 250 nits as
white and applies the sRGB curve, which matches the WIC output to about one level on average.

## Titles

`titlectl` is a small C payload with one fixed argument area: a 1040-byte `volatile` array that starts with the
marker `PS5MCP-ARGS-V1:`. Before sending it, the server writes `"<action>\0<argument>\0"` behind the marker
and its NUL, so the one ELF can name any title without a compiler on the PC. elfldr reports nothing back, so
titlectl writes its result to the kernel log as one `ps5-mcp:titlectl: ...` line, which the server reads from
its klogsrv connection. A round trip takes 0.2–0.8 s.

| Action | Calls | Report |
|---|---|---|
| `launch` | `sceUserServiceGetForegroundUser`, `sceSystemServiceLaunchApp(title id, args, {user})` | `launch <id> rc=0x...`: the new app id, negative on error |
| `close` | `sceLncUtilGetAppIdOfRunningBigApp` + `sceLncUtilGetAppTitleId`, then `sceLncUtilKillApp`; for an app not in front `sceSystemServiceGetAppId` + `sceSystemServiceKillApp` | `close <id> rc=0x0`, or `not-running` |
| `running` | `sceLncUtilGetAppIdOfRunningBigApp` + `sceLncUtilGetAppTitleId` | `running <id> app_id=0x...`, or `running none` |
| `url` | `sceSystemServiceLaunchWebBrowser(url)` | `url rc=0x0` |
| `rest` | `sceSystemStateMgrEnterStandby()` | `rest rc=0x0` (may not get out before the console sleeps) |
| `account <user id>` | `sceRegMgrGetInt`/`GetBin` on `USER_01_16_user_id` and `_account_id` (keys from linkdev's `regmgr.h`) | `account 0x...`, or `account none` |

`account` takes the user id from the server: `sceUserServiceGetForegroundUser` answers `0xffffffff` inside a
payload on 13.00, also with a user signed in, which matches every unused registry slot.

`close` only acts on the named title, and it goes through the system's own app lifecycle, as *Close App*
does, rather than signalling the process. Homebrew that renders with the GPU should still rather end itself:
an app that watches a quit file (`/data/<app>/quit`) gets it written by `ps5_close(quit_file=...)`, and the
server waits until the file is gone and the app is no longer in front.

The browser opens as a card over the running app, which keeps running. Its menu (Options, held briefly) has
*Close Card*.

Title names come from `/user/appmeta/<title id>/param.json` (`localizedParameters.<defaultLanguage>.titleName`),
or the app's own `sce_sys/param.json`.

## Power

The power state comes from Remote Play's discovery protocol (DDP, UDP 9302): a `SRCH * HTTP/1.1` message is
answered with `HTTP/1.1 200 Ok` when the console is on and `620 Server Standby` in rest mode, without any key.
`ps5_rest` waits for the 620. The payloads keep running in rest mode; elfldr, ftpsrv and klogsrv even accept
connections right after the console went to rest. Unless *Stay Connected to the Internet* is on, the network
goes down some seconds later and nothing answers, DDP included.

`ps5_wake` sends `WAKEUP * HTTP/1.1` with `user-credential:` set to the registration's RegistKey (hex-decoded
twice, read as one big-endian number, as pyremoteplay does), then waits for 200 and for elfldr to accept a
connection.

`ps5_pair` is Remote Play's registration, ported from pyremoteplay (`register.py`, `crypt.py`) and checked
against chiaki-ng's `regist.c`/`rpcrypt.c` (the request is byte for byte pyremoteplay's for the same nonce):
`SRC3` to UDP 9295, sent **from UDP port 9303**, must be answered with `RES3` (the console only does that while
a PIN is open), then an HTTP POST to `/sie/ps5/rp/sess/rgst` on TCP 9295. Its body is 480 filler
bytes with a key derived from a random nonce hidden at offsets 199 and 401, followed by
`Client-Type: …\r\nNp-AccountId: <base64 of the account id, little-endian>\r\n`, encrypted with AES-CFB-128;
the key is a PS5 constant with the PIN XORed into its last four bytes, the IV the first 16 bytes of
HMAC-SHA256(PS5 key, nonce ‖ counter). The answer is encrypted the same way and holds `PS5-RegistKey`.

Registration errors the console logs for the PIN's owner (from ps5-remoteplay-get-pin): `0x80FC1040` wrong
account id, `0x80FC1041` `REGIST_DEVICE_MISS`, `0x80FC1047` wrong PIN.

## Processes

A payload cannot be unloaded, and one that hangs keeps its port. RemoteEye v6 answers `PS` with the process
table from `sysctl kern.proc.proc` (`struct kinfo_proc`, FreeBSD 11 layout: pid at 72, ppid 76, state 388,
thread name 394, wait channel 411, command 447) and `KILL <pid>` with `kill(2)`. KILL refuses everything not
named `payload.elf` — the name elfldr gives each payload — and RemoteEye's own pid, so no title or system
process can be hit.

## The log

klogsrv sends every kernel log line to every connected client, so the server keeps one connection open in the
background and stamps each line with the time it arrived. `ps5_klog(since_s=...)` can then show what was logged
before the call, and other log readers still get every line.

klogsrv writes to each client in turn and opens `/dev/klog` only while it has clients; it opens it again only
after the last one has left. On 13.00 it sometimes stops sending anything while clients stay connected (twice
after rest mode, once without). Then every titlectl call times out, because its answer never arrives. The
server therefore connects again when the power state changed since it connected (checked every 3 s), when the
log has been quiet for 60 s before a call, and when a titlectl call saw no line at all. That only helps when it
is klogsrv's last client: another ps5-mcp server or log reader that stays connected keeps it silent.

Deleting the files afterwards over FTP leaves the gallery's database entry behind, shown as a blank tile in
the Media Gallery, so that is opt-in.
