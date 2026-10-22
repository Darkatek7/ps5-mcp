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

## The log

klogsrv sends every kernel log line to every connected client, so the server keeps one connection open in the
background and stamps each line with the time it arrived. `ps5_klog(since_s=...)` can then show what was logged
before the call, and other log readers still get every line.

Deleting the files afterwards over FTP leaves the gallery's database entry behind, shown as a blank tile in
the Media Gallery, so that is opt-in.
