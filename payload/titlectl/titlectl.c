/* titlectl - starts, closes and reports titles, and opens the web browser, for the ps5 MCP server.
 *
 * The PC sends this payload to elfldr with its arguments written into `slot` below: the server looks for the
 * marker in the ELF and puts "<action>\0<argument>\0" after it, so one prebuilt ELF serves every title and the
 * PC needs no SDK. The result goes to the kernel log as one line starting with "ps5-mcp:titlectl:", which the
 * server reads back through klogsrv.
 *
 *   launch <title id>   start the title for the foreground user
 *   close <title id>    close it, only if that title is the one running (the system's own close, not a signal)
 *   running             the title id of the running app, if any
 *   url <url>           open the web browser at url
 *   rest                put the console in rest mode
 *   account <user id>   the user's account id (signed in to PSN or activated offline), for Remote Play pairing
 *
 * Launch and close follow ps5-homebrew-dev-protocol's controllers (BlackBearReloaded, GPL-3.0-or-later), which
 * come from launch_app() in shsrv and sys_launch_title() in websrv by John Törnblom (GPL-3.0-or-later,
 * https://github.com/ps5-payload-dev). Opening the browser is the call PS5LM's open_browser makes. Rest mode is
 * the call of shsrv's suspend bundle. The account lookup follows linkdev's pairui.c (John Törnblom,
 * GPL-3.0-or-later).
 */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <ps5/klog.h>

typedef struct {
    uint32_t size, user_id, options;
    uint64_t crash_report;
    uint32_t flags;
} launch_context_t;

int sceUserServiceInitialize(void *);
int sceUserServiceGetForegroundUser(uint32_t *);
void sceUserServiceTerminate(void);
int sceSystemServiceLaunchApp(const char *, char **, launch_context_t *);
int sceSystemServiceLaunchWebBrowser(const char *, void *);
int sceLncUtilGetAppIdOfRunningBigApp(void);
int sceLncUtilGetAppTitleId(uint32_t app_id, char *title_id);
int sceLncUtilKillApp(uint32_t app_id);
int sceSystemServiceGetAppId(const char *);
int sceSystemServiceKillApp(int, int, int, int);
int sceSystemStateMgrEnterStandby(void);
int sceRegMgrGetInt(long, int *);
int sceRegMgrGetBin(long, void *, int);

#define MARKER "PS5MCP-ARGS-V1:"
#define SLOT_SIZE 1040

/* Registry keys of user slot n (1..16): USER_01_16_user_id and USER_01_16_account_id in linkdev's regmgr.h. */
#define REG_USER_ID(n) (((n) - 1) * 65536L + 125829376L)
#define REG_ACCOUNT_ID(n) (((n) - 1) * 65536L + 125830400L)

/* Kept writable and volatile so the compiler neither drops it nor folds its contents into the code. */
__attribute__((used)) static volatile char slot[SLOT_SIZE] = MARKER;

static int running_title(char title_id[16])
{
    int app_id = sceLncUtilGetAppIdOfRunningBigApp();
    memset(title_id, 0, 16);
    if (app_id <= 0 || sceLncUtilGetAppTitleId((uint32_t)app_id, title_id) != 0)
        return -1;
    return app_id;
}

static int launch(const char *title_id)
{
    launch_context_t context = {.size = sizeof(launch_context_t)};
    char *args[] = {0};
    int rc = sceUserServiceInitialize(0);

    if (rc == 0)
        rc = sceUserServiceGetForegroundUser(&context.user_id);
    if (rc == 0)
        rc = sceSystemServiceLaunchApp(title_id, args, &context);
    sceUserServiceTerminate();
    /* The app id of the started title, or a negative error code. */
    klog_printf("ps5-mcp:titlectl: launch %s rc=0x%x\n", title_id, rc);
    return rc < 0;
}

static int close_title(const char *title_id)
{
    char running[16];
    int app_id = running_title(running);
    int rc;

    if (app_id > 0 && strcmp(running, title_id) == 0) {
        rc = sceLncUtilKillApp((uint32_t)app_id);
        klog_printf("ps5-mcp:titlectl: close %s rc=0x%x\n", title_id, rc);
        return rc != 0;
    }
    /* Not the big app in front, but it may still run in the background. */
    app_id = sceSystemServiceGetAppId(title_id);
    if (app_id <= 0) {
        klog_printf("ps5-mcp:titlectl: close %s not-running\n", title_id);
        return 1;
    }
    rc = sceSystemServiceKillApp(app_id, -1, 0, 0);
    klog_printf("ps5-mcp:titlectl: close %s rc=0x%x\n", title_id, rc);
    return rc != 0;
}

static int report_running(void)
{
    char title_id[16];
    int app_id = running_title(title_id);

    if (app_id > 0)
        klog_printf("ps5-mcp:titlectl: running %s app_id=0x%x\n", title_id, app_id);
    else
        klog_printf("ps5-mcp:titlectl: running none\n");
    return 0;
}

static int open_url(const char *url)
{
    int rc = sceUserServiceInitialize(0);

    if (rc == 0)
        rc = sceSystemServiceLaunchWebBrowser(url, 0);
    klog_printf("ps5-mcp:titlectl: url rc=0x%x\n", rc);
    return rc != 0;
}

static int rest(void)
{
    int rc = sceSystemStateMgrEnterStandby();

    /* The system goes to rest a moment later, so this line usually still reaches klogsrv. */
    klog_printf("ps5-mcp:titlectl: rest rc=0x%x\n", rc);
    return rc != 0;
}

/* The account id (offline-activated or PSN) of user_id, or 0. */
static uint64_t account_id(uint32_t user_id)
{
    uint64_t id = 0;
    int i, uid;

    for (i = 1; i <= 16; i++) {
        uid = 0;
        if (sceRegMgrGetInt(REG_USER_ID(i), &uid) != 0)
            return 0;
        if ((uint32_t)uid == user_id)
            return sceRegMgrGetBin(REG_ACCOUNT_ID(i), &id, sizeof(id)) == 0 ? id : 0;
    }
    return 0;
}

/* The account id a Remote Play device registers with. user is the user id in hex, passed by the server:
 * sceUserServiceGetForegroundUser gives 0xffffffff in a payload on 13.00, also with a user signed in. */
static int report_account(const char *user)
{
    uint32_t user_id = (uint32_t)strtoul(user, 0, 16);
    uint64_t id = user_id ? account_id(user_id) : 0;

    if (id)
        klog_printf("ps5-mcp:titlectl: account 0x%lx\n", id);
    else
        klog_printf("ps5-mcp:titlectl: account none user=0x%x\n", user_id);
    return id == 0;
}

int main(void)
{
    char args[SLOT_SIZE];
    const char *action, *argument;
    size_t start = sizeof(MARKER);
    size_t i;

    for (i = 0; i < SLOT_SIZE; i++)
        args[i] = slot[i];
    args[SLOT_SIZE - 1] = 0;
    action = args + start;
    argument = action + strlen(action) + 1;

    if (strcmp(action, "launch") == 0 && *argument)
        return launch(argument);
    if (strcmp(action, "close") == 0 && *argument)
        return close_title(argument);
    if (strcmp(action, "running") == 0)
        return report_running();
    if (strcmp(action, "url") == 0 && *argument)
        return open_url(argument);
    if (strcmp(action, "rest") == 0)
        return rest();
    if (strcmp(action, "account") == 0 && *argument)
        return report_account(argument);
    klog_printf("ps5-mcp:titlectl: bad-args '%s'\n", action);
    return 2;
}
