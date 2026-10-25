// The process table, for finding and ending payloads that hang.
//
// A payload cannot be unloaded, and one that hangs keeps its port, so a fixed build cannot take that port
// over until the console restarts. PS shows every process with its state and what it waits on (a payload
// spinning in a loop shows state 2 and no wait channel); KILL ends one, but only a process named
// payload.elf (the name elfldr gives every payload) and never RemoteEye itself, so no title or system
// process can be hit by mistake. Titles are closed through the system instead (payload/titlectl).
//
// struct kinfo_proc offsets (FreeBSD 11, amd64) as measured on FW 13.00.

using System;
using SharpProspero.Payload;
using SharpProspero.Payload.Net;
using SharpProspero.Payload.Posix;

namespace RemoteEye;

internal static unsafe class Processes
{
    private const int KiPid = 72;
    private const int KiPpid = 76;
    private const int KiStat = 388;     // 1 idle, 2 run, 3 sleep, 4 stop, 5 zombie, 6 wait, 7 lock
    private const int KiTdName = 394;   // char[17]
    private const int KiWmesg = 411;    // char[9]
    private const int KiComm = 447;     // char[20]

    private const int SysGetpid = 20;
    private const int SysKill = 37;

    private static ReadOnlySpan<byte> PayloadName => "payload.elf"u8;

    /// <summary>The whole process table (kern.proc.proc), or null; size is the part that was filled.</summary>
    private static byte[]? ReadTable(out int size)
    {
        size = 0;
        int* mib = stackalloc int[3];
        mib[0] = PayloadSysctl.CtlKern;
        mib[1] = PayloadSysctl.KernProc;
        mib[2] = PayloadSysctl.KernProcProc;
        nuint length = 0;
        if (PayloadSysctl.sysctl(mib, 3, null, &length, null, 0) != 0 || length == 0)
            return null;
        length += length / 4;   // processes started between the two calls
        byte[] table = new byte[(int)length];
        fixed (byte* buffer = table)
        {
            if (PayloadSysctl.sysctl(mib, 3, buffer, &length, null, 0) != 0)
                return null;
        }
        size = (int)length;
        return table;
    }

    /// <summary>"OK", then "P &lt;pid&gt; &lt;ppid&gt; &lt;stat&gt; &lt;comm&gt; &lt;wmesg or -&gt; &lt;thread name&gt;" per process, then "END".</summary>
    public static void ServeList(int client)
    {
        byte[]? table = ReadTable(out int size);
        if (table == null)
        {
            Program.Send(client, "ERR sysctl\n"u8);
            return;
        }
        fixed (byte* buffer = table)
        {
            Program.Send(client, "OK\n"u8);
            byte* entry = buffer;
            byte* end = buffer + size;
            while (entry + KiComm + 20 <= end)
            {
                int structSize = *(int*)entry;
                if (structSize <= 0) break;
                Program.Send(client, "P "u8);
                Program.SendInt(client, *(int*)(entry + KiPid));
                Program.Send(client, " "u8);
                Program.SendInt(client, *(int*)(entry + KiPpid));
                Program.Send(client, " "u8);
                Program.SendInt(client, entry[KiStat]);
                Program.Send(client, " "u8);
                PayloadTcpServer.SendAll(client, entry + KiComm, Length(entry + KiComm, 20));
                Program.Send(client, " "u8);
                int wait = Length(entry + KiWmesg, 9);
                if (wait == 0) Program.Send(client, "-"u8);
                else PayloadTcpServer.SendAll(client, entry + KiWmesg, wait);
                Program.Send(client, " "u8);
                PayloadTcpServer.SendAll(client, entry + KiTdName, Length(entry + KiTdName, 17));
                Program.Send(client, "\n"u8);
                entry += structSize;
            }
            Program.Send(client, "END\n"u8);
        }
    }

    /// <summary>Ends a payload: refuses anything not named payload.elf, and RemoteEye itself.</summary>
    public static void ServeKill(int client, int pid, int signal)
    {
        if (pid <= 1 || signal < 1 || signal > 31)
        {
            Program.Send(client, "ERR usage: KILL <pid> [signal]\n"u8);
            return;
        }
        if (pid == (int)PayloadCrt.Syscall(SysGetpid, 0))
        {
            Program.Send(client, "ERR that is RemoteEye itself\n"u8);
            return;
        }

        // Looked up in the whole table: kern.proc.pid into a buffer of FreeBSD's kinfo_proc size answered
        // nothing for any process on FW 13.00.
        byte[]? table = ReadTable(out int size);
        if (table == null)
        {
            Program.Send(client, "ERR sysctl\n"u8);
            return;
        }
        fixed (byte* buffer = table)
        {
            byte* entry = buffer;
            byte* end = buffer + size;
            while (entry + KiComm + 20 <= end && *(int*)entry > 0 && *(int*)(entry + KiPid) != pid)
                entry += *(int*)entry;
            if (entry + KiComm + 20 > end || *(int*)entry <= 0)
            {
                Program.Send(client, "ERR no such process\n"u8);
                return;
            }
            int length = Length(entry + KiComm, 20);
            if (!new ReadOnlySpan<byte>(entry + KiComm, length).SequenceEqual(PayloadName))
            {
                Program.Send(client, "ERR not a payload: "u8);
                PayloadTcpServer.SendAll(client, entry + KiComm, length);
                Program.Send(client, "\n"u8);
                return;
            }
        }

        long rc = PayloadCrt.Syscall(SysKill, pid, signal);
        Program.Send(client, "OK rc="u8);
        Program.SendInt(client, (int)rc);
        Program.Send(client, "\n"u8);
    }

    /// <summary>Length of a NUL-terminated string in a fixed-size field.</summary>
    private static int Length(byte* text, int max)
    {
        int n = 0;
        while (n < max && text[n] != 0) n++;
        return n;
    }
}
