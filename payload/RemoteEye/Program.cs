// RemoteEye — a virtual controller on the console, driven over the LAN.
//
// It is the input half of the ps5 MCP server (server/server.py), which lets Claude test apps on the
// console. The screen half needs no payload: holding the virtual controller's Create button makes the
// system save a screenshot to the gallery, which the PC fetches over FTP.
//
// The controller is the system's own virtual-pad device, the one Remote Play feeds. scePadInit (like
// every other service that talks to the shell) hangs inside a payload, but the virtual-pad calls in
// libScePad each come down to one ioctl on /dev/hid (docs/how-it-works.md). So this payload opens
// /dev/hid itself and issues those ioctls, with the request blocks laid out as libScePad (FW 13.00)
// builds them:
//
//   add     0xC018482A  { u32 op = 3; u64 desc*; u64 int* handleOut }
//           desc (0xA8 bytes): u8 class @0, u32 userId @0x1C, u8 subtype @0x20
//           (class, subtype) come from two tables indexed by the public device type 1..12 (4 is refused)
//   insert  0x8018482C  { u32 handle; u64 state*; u8 0 }
//           state (0xA8 bytes): u32 buttons @0xC, u8 lx ly rx ry @0x10, u8 l2 r2 @0x14, u8 1 @0x17
//   delete  0x80104850  { u8 op = 5; u32 handle @8 }
//
// A state stays in effect until the next one, so a press is two STATE lines with the hold time between
// them; timing lives on the PC. The controller outlives client connections and is only removed by DETACH.
// A newly added controller is not yet bound to a user: the system asks "Who's using this controller?"
// once, and the controller itself answers it (Right to the user, then Cross).
//
// Requests are ASCII lines terminated by '\n':
//   PING                               port, build tag, and whether a controller is attached
//   ATTACH <userhex> [type]            open /dev/hid and add the controller (type defaults to 1);
//                                      reports the existing one when already attached
//   STATE <btnhex> [lx ly rx ry l2 r2] feed one state (sticks default to centre 128, triggers to 0)
//   DETACH                             remove the controller

using System;
using System.Runtime.InteropServices;
using SharpProspero.Payload;
using SharpProspero.Payload.Net;

namespace RemoteEye;

internal static unsafe class Program
{
    // A payload cannot be unloaded, and binding a port an older instance holds succeeds while the old one
    // keeps receiving, so each build listens on PortBase + BuildTag. Raise BuildTag on each rebuild; the
    // PC scans the range for the newest.
    private const ushort PortBase = 3320;
    private const int BuildTag = 1;
    private const int MaxLine = 256;

    private const int SysIoctl = 54;
    private const ulong IoctlAdd = 0xC018482A;
    private const ulong IoctlInsert = 0x8018482C;
    private const ulong IoctlDelete = 0x80104850;
    private const int BlockSize = 0xA8;

    private static ReadOnlySpan<byte> TypeClass => [3, 2, 2, 0, 2, 2, 2, 2, 2, 3, 3, 3];
    private static ReadOnlySpan<byte> TypeSubtype => [1, 4, 5, 0, 7, 8, 9, 12, 13, 14, 15, 16];

    private static int Port;
    private static int HidFd = -1;
    private static int Handle = -1;

    [UnmanagedCallersOnly(EntryPoint = "__managed__Main")]
    public static int Main(void* args)
    {
        PayloadCrt.Klog("sp:remoteeye:start\n\0"u8);

        Port = PortBase + BuildTag;
        int listener = PayloadNetwork.Listen((ushort)Port, 4);
        if (listener < 0)
            return -1;

        byte* line = stackalloc byte[MaxLine + 1];
        while (true)
        {
            int client = PayloadNetwork.Accept(listener);
            if (client < 0)
                continue;
            ClientTimeout.Apply(client);
            Serve(client, line);
            PayloadTcpServer.Close(client);
        }
    }

    private static void Serve(int client, byte* line)
    {
        while (true)
        {
            int length = ReadLine(client, line, MaxLine);
            if (length <= 0)
                return;
            line[length] = 0;

            if (StartsWith(line, "PING"))
            {
                Send(client, "OK PONG "u8);
                SendInt(client, Port);
                Send(client, " v"u8);
                SendInt(client, BuildTag);
                Send(client, " remoteeye handle="u8);
                SendInt(client, Handle);
                Send(client, "\n"u8);
            }
            else if (StartsWith(line, "ATTACH "))
                ServeAttach(client, line + 7);
            else if (StartsWith(line, "STATE "))
                ServeState(client, line + 6);
            else if (StartsWith(line, "DETACH"))
                ServeDetach(client);
            else
                Send(client, "ERR unknown\n"u8);
        }
    }

    private static void ServeAttach(int client, byte* args)
    {
        uint userId = (uint)ParseHex(ref args);
        int type = ParseInt(args);
        if (type < 0) type = 1;
        if (userId == 0 || type < 1 || type > 12 || type == 4)
        {
            Send(client, "ERR usage: ATTACH <userhex> [type 1..12, not 4]\n"u8);
            return;
        }

        if (Handle >= 0)
        {
            Send(client, "OK handle="u8);
            SendInt(client, Handle);
            Send(client, " existing\n"u8);
            return;
        }

        if (HidFd < 0)
        {
            fixed (byte* path = "/dev/hid\0"u8)
                HidFd = (int)PayloadCrt.Syscall(PayloadCrt.SYS_open, (long)(nint)path, 0);
            if (HidFd < 0)
            {
                Send(client, "ERR open /dev/hid rc="u8);
                SendInt(client, HidFd);
                Send(client, "\n"u8);
                HidFd = -1;
                return;
            }
        }

        byte* desc = stackalloc byte[BlockSize];
        Clear(desc, BlockSize);
        desc[0] = TypeClass[type - 1];
        *(uint*)(desc + 0x1C) = userId;
        desc[0x20] = TypeSubtype[type - 1];

        int handle = -1;
        ulong* request = stackalloc ulong[3];
        request[0] = 3;
        request[1] = (ulong)desc;
        request[2] = (ulong)&handle;
        long rc = Ioctl(IoctlAdd, request);
        if (rc != 0 || handle < 0)
        {
            Send(client, "ERR add rc="u8);
            SendInt(client, (int)rc);
            Send(client, "\n"u8);
            return;
        }

        Handle = handle;
        Send(client, "OK handle="u8);
        SendInt(client, Handle);
        Send(client, " new\n"u8);
    }

    private static void ServeState(int client, byte* args)
    {
        if (Handle < 0) { Send(client, "ERR not attached\n"u8); return; }

        uint buttons = (uint)ParseHex(ref args);

        // lx ly rx ry l2 r2; sticks rest at the centre.
        byte* analog = stackalloc byte[6];
        analog[0] = analog[1] = analog[2] = analog[3] = 128;
        analog[4] = analog[5] = 0;
        for (int i = 0; i < 6; i++)
        {
            while (*args == (byte)' ') args++;
            int value = ParseInt(args);
            if (value < 0) break;
            analog[i] = (byte)(value > 255 ? 255 : value);
            while (*args >= (byte)'0' && *args <= (byte)'9') args++;
        }

        byte* state = stackalloc byte[BlockSize];
        Clear(state, BlockSize);
        *(uint*)(state + 0xC) = buttons;
        for (int i = 0; i < 6; i++) state[0x10 + i] = analog[i];
        state[0x17] = 1;

        ulong* request = stackalloc ulong[3];
        request[0] = (uint)Handle;
        request[1] = (ulong)state;
        request[2] = 0;
        long rc = Ioctl(IoctlInsert, request);
        if (rc != 0)
        {
            Send(client, "ERR insert rc="u8);
            SendInt(client, (int)rc);
            Send(client, "\n"u8);
            return;
        }
        Send(client, "OK\n"u8);
    }

    private static void ServeDetach(int client)
    {
        if (Handle < 0) { Send(client, "OK none\n"u8); return; }
        ulong* request = stackalloc ulong[2];
        request[0] = 5;
        request[1] = (uint)Handle;
        long rc = Ioctl(IoctlDelete, request);
        Handle = -1;
        Send(client, "OK rc="u8);
        SendInt(client, (int)rc);
        Send(client, "\n"u8);
    }

    private static long Ioctl(ulong request, void* arg)
        => PayloadCrt.Syscall(SysIoctl, HidFd, (long)request, (long)(nint)arg);

    private static void Clear(byte* buffer, int length)
    {
        for (int i = 0; i < length; i++) buffer[i] = 0;
    }

    // ---- io ----

    private static int ReadLine(int client, byte* buffer, int max)
    {
        int count = 0;
        while (count < max)
        {
            byte one;
            long got = PayloadNetwork.Receive(client, new Span<byte>(&one, 1));
            if (got <= 0) return count;
            if (one == (byte)'\n') return count;
            if (one == (byte)'\r') continue;
            buffer[count++] = one;
        }
        return count;
    }

    private static void Send(int client, ReadOnlySpan<byte> data) => PayloadTcpServer.SendAll(client, data);

    private static void SendInt(int client, int value)
    {
        byte* digits = stackalloc byte[12];
        int count = 0;
        long v = value;
        if (v < 0) { Send(client, "-"u8); v = -v; }
        if (v == 0) digits[count++] = (byte)'0';
        else while (v > 0) { digits[count++] = (byte)('0' + (int)(v % 10)); v /= 10; }
        for (int i = count - 1; i >= 0; i--)
            PayloadTcpServer.SendAll(client, digits + i, 1);
    }

    // ---- text helpers ----

    private static bool StartsWith(byte* line, string prefix)
    {
        for (int i = 0; i < prefix.Length; i++)
        {
            if (line[i] != (byte)prefix[i]) return false;
        }
        return true;
    }

    /// <summary>Parses a hex number (optional 0x), skipping spaces around it; advances past it.</summary>
    private static ulong ParseHex(ref byte* text)
    {
        while (*text == (byte)' ') text++;
        if (text[0] == (byte)'0' && (text[1] == (byte)'x' || text[1] == (byte)'X')) text += 2;
        ulong value = 0;
        while (true)
        {
            byte c = *text;
            int d = c >= (byte)'0' && c <= (byte)'9' ? c - '0'
                  : c >= (byte)'a' && c <= (byte)'f' ? c - 'a' + 10
                  : c >= (byte)'A' && c <= (byte)'F' ? c - 'A' + 10 : -1;
            if (d < 0) break;
            value = (value << 4) | (uint)d;
            text++;
        }
        while (*text == (byte)' ') text++;
        return value;
    }

    private static int ParseInt(byte* text)
    {
        while (*text == (byte)' ') text++;
        int value = 0;
        bool any = false;
        while (*text >= (byte)'0' && *text <= (byte)'9')
        {
            value = value * 10 + (*text - (byte)'0');
            text++;
            any = true;
        }
        return any ? value : -1;
    }
}
