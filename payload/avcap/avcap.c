/* FW 13.00: capture composited screen AVC through Avcap2 without the gallery.
 * The MCP screenshot backend patches request_slot; unpatched, it runs as a
 * standalone two-second diagnostic capture.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <ps5/kernel.h>
#include <ps5/klog.h>

#define LOG(...) klog_printf("ps5-mcp:avcap: " __VA_ARGS__)
#define RESOURCE 180
#define OUTPUT "/data/ps5mcp-avcap.h264"
#define AGAIN ((int)0x81950002)

/* Patched by the MCP server with a unique request id. Empty means standalone
 * diagnostic mode. volatile prevents the compiler folding the empty slot. */
#define REQUEST_MARKER "PS5MCP-CAPTURE-V1:"
static volatile char request_slot[sizeof(REQUEST_MARKER) + 33] = REQUEST_MARKER;
static char capture_output[160] = OUTPUT;
static int screenshot_mode;

typedef int (*LoadStart)(const char *, size_t, const void *, uint32_t, void *, int *);
typedef int (*HandleCall)(intptr_t);
typedef int (*Register)(int, void *, void *);
typedef int (*Request)(int, const char *, void *, int);
static atomic_int granted;

static int resource_event(int id, int event, const uint8_t *data)
{
    if (id == RESOURCE && event == 1 && data)
        atomic_store(&granted, data[0] == 1);
    if (id == RESOURCE && event == 2)
        atomic_store(&granted, 0);
    return 0;
}

static uint32_t module(LoadStart load, const char *name)
{
    uint32_t handle = 0;
    char path[128];
    int result = -1;
    if (kernel_dynlib_handle(-1, name, &handle) == 0 && handle)
        return handle;
    snprintf(path, sizeof path, "/system/common/lib/%s", name);
    int rc = load(path, 0, NULL, 0, NULL, &result);
    LOG("load %s rc=%#x result=%#x\n", name, rc, result);
    if (kernel_dynlib_handle(-1, name, &handle) != 0)
        return 0;
    return handle;
}

static void u32(uint8_t *p, size_t at, uint32_t value) { memcpy(p + at, &value, 4); }
static void u64(uint8_t *p, size_t at, uint64_t value) { memcpy(p + at, &value, 8); }

static void configure(uint8_t *p, uint32_t output_mode)
{
    /* Size headers copied from the system consumer; necessity of each header
     * has not been isolated. Offsets are relative to the configuration. */
    static const struct { size_t at; uint64_t size; } headers[] = {
        {0, 0x2e0}, {8, 0x28}, {0x30, 0x58}, {0x38, 0x18},
        {0x50, 0x10}, {0x88, 0x30}, {0xb8, 0xe0}, {0xc0, 0x20},
        {0xe0, 0x20}, {0x100, 0x20}, {0x120, 0x18}, {0x1e8, 0xf8},
        {0x1f0, 0x28}, {0x218, 0x88}, {0x2a8, 0x10},
    };
    memset(p, 0, 0x2e0);
    for (size_t i = 0; i < sizeof headers / sizeof headers[0]; i++)
        u64(p, headers[i].at, headers[i].size);
    u32(p, 0x10, 0);          /* scan-in channel */
    u32(p, 0x14, 2);          /* Remoteplay capture purpose */
    u32(p, 0x18, 4);          /* resolution enum: 1920x1080 */
    u32(p, 0x1c, 1);          /* timing enum from the successful probe */
    u32(p, 0x24, 0);          /* codec: AVC=0, HEVC=1, uncompressed=2 */
    u32(p, 0x28, RESOURCE);   /* ResourceArbitrator id, not a user id */
    u32(p, 0x94, 30);
    u32(p, 0xc8, 6000000);
    u32(p, 0x2a0, output_mode);
    u32(p, 0x2a4, 2);         /* SDR composited screen (tap 3); 3 selects app-only tap 4 */
    u32(p, 0x2b0, 1);
    p[0x2b4] = 1;
    /* mode at 0x1a0 remains 0: the service supplies the frame memory. */
}

static int write_all(int fd, const void *data, size_t length)
{
    const uint8_t *p = data;
    while (length) {
        ssize_t n = write(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        length -= (size_t)n;
    }
    return 0;
}

static int capture(void)
{
    uint32_t fw = kernel_get_fw_version();
    if ((fw & 0xffff0000u) != 0x13000000u) {
        LOG("refusing firmware %#x: ResourceArbitrator offsets are for 13.00\n", fw);
        return 2;
    }
    LOG("firmware=%#x\n", fw);
    const char *kernels[] = {"libkernel_web.sprx", "libkernel_sys.sprx", "libkernel.sprx"};
    LoadStart load = NULL;
    for (size_t i = 0; i < sizeof kernels / sizeof kernels[0] && !load; i++) {
        uint32_t h = 0;
        if (kernel_dynlib_handle(-1, kernels[i], &h) == 0 && h)
            load = (LoadStart)kernel_dynlib_dlsym(-1, h, "sceKernelLoadStartModule");
    }
    if (!load || !module(load, "libSceIpmi.sprx")) return 1;
    uint32_t av = module(load, "libSceAvcap2.sprx");
    uint32_t ra = module(load, "libSceResourceArbitrator.sprx");
    if (!av || !ra) return 1;

    /* These RA exports have only NID names in the inspected module. Relative
     * offsets avoid ASLR assumptions; the firmware gate above is mandatory. */
    intptr_t base = kernel_dynlib_mapbase_addr(-1, ra);
    if (!base) return 1;
    Register reg = (Register)(base + 0xdb0);               /* kJvjeet+f9I */
    int (*unreg)(int) = (void *)(base + 0xee0);            /* LqmYZGB-UEQ */
    Request request = (Request)(base + 0x10b0);           /* CYjfezkWcaE */
    int (*list)(int32_t *) = (void *)(base + 0x11f0);      /* fBX+DBOwNis */
    int32_t active[32];
    if (list(active) != 0) return 1;
    for (size_t i = 0; i < 32; i++) {
        if (active[i] == RESOURCE) {
            LOG("resource 180 is already active; leaving it alone\n");
            return 4;
        }
    }

    int (*init)(void) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2Initialize");
    int (*term)(void) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2Terminate");
    int (*video_mode)(int *) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2GetVideoOutMode");
    int (*open_video)(intptr_t *, const void *, uint8_t) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2OpenVideo");
    HandleCall start = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2Start");
    HandleCall stop = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2Stop");
    HandleCall close_video = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2Close");
    int (*nal_of)(intptr_t, void *) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2GetNonVclNalUnitStream");
    int (*read_video)(intptr_t, void *, size_t, void *, uint32_t) = (void *)kernel_dynlib_dlsym(-1, av, "sceAvcap2ReadVideo");
    if (!init || !term || !video_mode || !open_video || !start || !stop || !close_video || !nal_of || !read_video)
        return 1;

    int rc = reg(RESOURCE, (void *)resource_event, NULL);
    LOG("register rc=%#x\n", rc);
    if (rc != 0) return 1;
    int initialized = 0, started = 0, fd = -1, result = 1;
    intptr_t handle = 0;
    uint8_t *buffer = NULL;
    rc = request(RESOURCE, "RUNNING", NULL, getpid());
    LOG("request RUNNING rc=%#x\n", rc);
    if (rc < 0) goto cleanup;
    for (int i = 0; i < 20 && !atomic_load(&granted); i++) usleep(100000);
    if (!atomic_load(&granted)) { LOG("no resource grant within 2 seconds\n"); goto cleanup; }
    rc = init();
    LOG("Initialize rc=%#x\n", rc);
    if (rc != 0) goto cleanup;
    initialized = 1;
    int output_mode = 0;
    rc = video_mode(&output_mode);
    LOG("GetVideoOutMode rc=%#x flags=%#x\n", rc, output_mode);
    if (rc != 0) goto cleanup;
    if (output_mode & 3) { LOG("capture currently expects SDR\n"); result = 3; goto cleanup; }
    uint8_t config[0x2e0];
    configure(config, output_mode);
    rc = open_video(&handle, config, 0);
    LOG("OpenVideo rc=%#x\n", rc);
    if (rc != 0) goto cleanup;
    const size_t capacity = 4 * 1024 * 1024;
    buffer = malloc(capacity);
    if (!buffer) goto cleanup;
    fd = open(capture_output, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) goto cleanup;
    rc = start(handle);
    LOG("Start rc=%#x\n", rc);
    if (rc != 0) goto cleanup;
    started = 1;

    uint8_t nal[0x1a8] = {0};
    u64(nal, 0, sizeof nal);
    rc = nal_of(handle, nal);
    LOG("GetNonVclNalUnitStream rc=%#x\n", rc);
    if (rc != 0) goto cleanup;
    /* AVC SPS/PPS, and the third optional NAL slot. The API excludes start codes. */
    const uint8_t annex_b[] = {0, 0, 0, 1};
    for (size_t off = 0x10; off <= 0x120; off += 0x88) {
        uint64_t n;
        memcpy(&n, nal + off, sizeof n);
        if (n > 0x80) goto cleanup;
        if (n && (write_all(fd, annex_b, sizeof annex_b) || write_all(fd, nal + off + 8, n)))
            goto cleanup;
    }
    size_t bytes = 0;
    int frames = 0;
    for (int i = 0; i < (screenshot_mode ? 25 : 100) && atomic_load(&granted); i++) {
        uint8_t info[0x70] = {0};
        u64(info, 0, sizeof info);
        rc = read_video(handle, buffer, capacity, info, 1); /* nonblocking */
        if (rc > 0) {
            if ((size_t)rc > capacity || write_all(fd, buffer, rc)) goto cleanup;
            frames++;
            bytes += rc;
        } else if (rc != AGAIN && rc != 0) {
            LOG("ReadVideo rc=%#x\n", rc);
            goto cleanup;
        }
        usleep(20000);
    }
    LOG("captured frames=%d bytes=%lu output=%s\n", frames, bytes, capture_output);
    result = frames && atomic_load(&granted) ? 0 : 1;

cleanup:
    if (started) { rc = stop(handle); LOG("Stop rc=%#x\n", rc); if (rc) result = 1; }
    if (handle) { rc = close_video(handle); LOG("Close rc=%#x\n", rc); if (rc) result = 1; }
    if (fd >= 0 && close(fd)) result = 1;
    free(buffer);
    if (initialized) { rc = term(); LOG("Terminate rc=%#x\n", rc); if (rc) result = 1; }
    rc = request(RESOURCE, "STOP", NULL, getpid());
    LOG("request STOP rc=%#x\n", rc);
    if (rc < 0) result = 1;
    rc = unreg(RESOURCE);
    LOG("unregister rc=%#x\n", rc);
    if (rc) result = 1;
    LOG("done result=%d\n", result);
    return result;
}

int main(void)
{
    char token[33];
    for (size_t i = 0; i < sizeof token; i++) token[i] = request_slot[sizeof(REQUEST_MARKER) + i];
    if (!token[0]) return capture();
    token[32] = 0;
    if (strlen(token) != 32 || strspn(token, "0123456789abcdef") != 32) return 1;
    char output[160], status[160], pending[160];
    snprintf(output, sizeof output, "/data/ps5mcp-avcap-%s.h264", token);
    snprintf(capture_output, sizeof capture_output, "%s.part", output);
    snprintf(status, sizeof status, "/data/ps5mcp-avcap-%s.status", token);
    snprintf(pending, sizeof pending, "%s.part", status);
    screenshot_mode = 1;
    int result = capture();
    if (!result && rename(capture_output, output)) result = 1;
    if (result) unlink(capture_output);
    char report[32];
    int length = snprintf(report, sizeof report, "%d\n", result);
    int fd = open(pending, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return 1;
    int failed = write_all(fd, report, (size_t)length);
    if (close(fd)) failed = 1;
    if (failed || rename(pending, status)) { unlink(pending); return 1; }
    return result;
}
