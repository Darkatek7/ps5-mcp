// Titles, the browser, power and the account id: what payload/titlectl does, served from here so a call needs
// neither a payload send nor klogsrv (whose answers were lost whenever it went silent, see docs/how-it-works.md).
//
// Each answer is "OK " and then the very report titlectl writes to the kernel log, so the server parses both the
// same way:
//   LAUNCH <title id>    launch <id> rc=0x...       the app id, or a negative error
//   CLOSE <title id>     close <id> rc=0x... | close <id> not-running
//   RUNNING              running <id> app_id=0x... | running none
//   URL <url>            url rc=0x...
//   REST                 rest rc=0x...              may not get out before the console sleeps
//   OFF                  off rc=0x...               likewise
//   ACCOUNT <user hex>   account 0x... | account none user=0x...
//
// The calls and their order are titlectl's (payload/titlectl/titlectl.c names their sources).

using System;
using System.Runtime.InteropServices;

namespace RemoteEye;

internal static unsafe partial class SystemCalls
{
    private const string SystemService = "libSceSystemService";
    private const string StateMgr = "libSceSystemStateMgr";
    private const string UserService = "libSceUserService";
    private const string RegMgr = "libSceRegMgr";

    // Registry keys of user slot n (1..16): USER_01_16_user_id and USER_01_16_account_id in linkdev's regmgr.h.
    private static long RegUserId(int n) => (n - 1) * 65536L + 125829376L;
    private static long RegAccountId(int n) => (n - 1) * 65536L + 125830400L;

    [LibraryImport(UserService)] private static partial int sceUserServiceInitialize(void* param);
    [LibraryImport(UserService)] private static partial int sceUserServiceGetForegroundUser(uint* userId);
    [LibraryImport(UserService)] private static partial void sceUserServiceTerminate();
    [LibraryImport(SystemService)] private static partial int sceSystemServiceLaunchApp(byte* titleId, byte** argv, void* context);
    [LibraryImport(SystemService)] private static partial int sceSystemServiceLaunchWebBrowser(byte* url, void* param);
    [LibraryImport(SystemService)] private static partial int sceLncUtilGetAppIdOfRunningBigApp();
    [LibraryImport(SystemService)] private static partial int sceLncUtilGetAppTitleId(uint appId, byte* titleId);
    [LibraryImport(SystemService)] private static partial int sceLncUtilKillApp(uint appId);
    [LibraryImport(SystemService)] private static partial int sceSystemServiceGetAppId(byte* titleId);
    [LibraryImport(SystemService)] private static partial int sceSystemServiceKillApp(int appId, int a, int b, int c);
    [LibraryImport(StateMgr)] private static partial int sceSystemStateMgrEnterStandby();
    [LibraryImport(StateMgr)] private static partial int sceSystemStateMgrTurnOff();
    [LibraryImport(RegMgr)] private static partial int sceRegMgrGetInt(long key, int* value);
    [LibraryImport(RegMgr)] private static partial int sceRegMgrGetBin(long key, void* value, int size);

    public static void Launch(int client, byte* titleId)
    {
        // titlectl's launch_context_t: u32 size, user_id, options; u64 crash_report; u32 flags (32 bytes).
        byte* context = stackalloc byte[32];
        for (int i = 0; i < 32; i++) context[i] = 0;
        *(uint*)context = 32;
        byte** argv = stackalloc byte*[1];
        argv[0] = null;

        int rc = sceUserServiceInitialize(null);
        if (rc == 0)
            rc = sceUserServiceGetForegroundUser((uint*)(context + 4));
        if (rc == 0)
            rc = sceSystemServiceLaunchApp(titleId, argv, context);
        sceUserServiceTerminate();
        Report(client, "launch "u8, titleId, rc);
    }

    public static void Close(int client, byte* titleId)
    {
        byte* running = stackalloc byte[16];
        int appId = RunningTitle(running);
        int rc;
        if (appId > 0 && Same(running, titleId))
        {
            rc = sceLncUtilKillApp((uint)appId);
            Report(client, "close "u8, titleId, rc);
            return;
        }
        // Not the big app in front, but it may still run in the background.
        appId = sceSystemServiceGetAppId(titleId);
        if (appId <= 0)
        {
            Program.Send(client, "OK close "u8);
            SendText(client, titleId);
            Program.Send(client, " not-running\n"u8);
            return;
        }
        rc = sceSystemServiceKillApp(appId, -1, 0, 0);
        Report(client, "close "u8, titleId, rc);
    }

    public static void Running(int client)
    {
        byte* titleId = stackalloc byte[16];
        int appId = RunningTitle(titleId);
        if (appId <= 0)
        {
            Program.Send(client, "OK running none\n"u8);
            return;
        }
        Program.Send(client, "OK running "u8);
        SendText(client, titleId);
        Program.Send(client, " app_id=0x"u8);
        SendHex(client, (ulong)(uint)appId);
        Program.Send(client, "\n"u8);
    }

    public static void Url(int client, byte* url)
    {
        int rc = sceUserServiceInitialize(null);
        if (rc == 0)
            rc = sceSystemServiceLaunchWebBrowser(url, null);
        Report(client, "url"u8, null, rc);
    }

    public static void Rest(int client) => Report(client, "rest"u8, null, sceSystemStateMgrEnterStandby());

    public static void Off(int client) => Report(client, "off"u8, null, sceSystemStateMgrTurnOff());

    public static void Account(int client, uint userId)
    {
        ulong id = 0;
        for (int i = 1; i <= 16 && userId != 0; i++)
        {
            int uid = 0;
            if (sceRegMgrGetInt(RegUserId(i), &uid) != 0)
                break;
            if ((uint)uid == userId)
            {
                if (sceRegMgrGetBin(RegAccountId(i), &id, sizeof(ulong)) != 0)
                    id = 0;
                break;
            }
        }
        if (id == 0)
        {
            Program.Send(client, "OK account none user=0x"u8);
            SendHex(client, userId);
            Program.Send(client, "\n"u8);
            return;
        }
        Program.Send(client, "OK account 0x"u8);
        SendHex(client, id);
        Program.Send(client, "\n"u8);
    }

    /// <summary>The app id of the app in front and its title id, or a value &lt;= 0.</summary>
    private static int RunningTitle(byte* titleId)
    {
        for (int i = 0; i < 16; i++) titleId[i] = 0;
        int appId = sceLncUtilGetAppIdOfRunningBigApp();
        if (appId <= 0 || sceLncUtilGetAppTitleId((uint)appId, titleId) != 0)
            return -1;
        return appId;
    }

    /// <summary>"OK &lt;what&gt;[ &lt;title id&gt;] rc=0x&lt;rc&gt;", as titlectl logs it.</summary>
    private static void Report(int client, ReadOnlySpan<byte> what, byte* titleId, int rc)
    {
        Program.Send(client, "OK "u8);
        Program.Send(client, what);
        if (titleId != null)
            SendText(client, titleId);
        Program.Send(client, " rc=0x"u8);
        SendHex(client, (uint)rc);
        Program.Send(client, "\n"u8);
    }

    private static bool Same(byte* a, byte* b)
    {
        for (int i = 0; i < 16; i++)
        {
            if (a[i] != b[i]) return false;
            if (a[i] == 0) return true;
        }
        return true;
    }

    private static void SendText(int client, byte* text)
    {
        int n = 0;
        while (n < 256 && text[n] != 0) n++;
        Program.Send(client, new ReadOnlySpan<byte>(text, n));
    }

    private static void SendHex(int client, ulong value)
    {
        byte* digits = stackalloc byte[16];
        int count = 0;
        do
        {
            int d = (int)(value & 0xF);
            digits[count++] = (byte)(d < 10 ? '0' + d : 'a' + d - 10);
            value >>= 4;
        } while (value != 0);
        for (int i = count - 1; i >= 0; i--)
            Program.Send(client, new ReadOnlySpan<byte>(digits + i, 1));
    }
}
