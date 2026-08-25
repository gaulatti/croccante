#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>

typedef struct AVIOContext AVIOContext;
typedef struct AVIOInterruptCB AVIOInterruptCB;
typedef struct AVDictionary AVDictionary;

typedef int (*avio_open2_fn)(AVIOContext **, const char *, int,
                            const AVIOInterruptCB *, AVDictionary **);

/*
 * ffmpeg normally places its output URL in argv. Croccante instead gives it a
 * harmless loopback placeholder and substitutes the resolved destination only
 * at libavformat's I/O boundary. The real URL therefore remains ephemeral
 * process state and never appears in /proc/<pid>/cmdline.
 */
int avio_open2(AVIOContext **context, const char *url, int flags,
               const AVIOInterruptCB *interrupt_callback,
               AVDictionary **options) {
    static avio_open2_fn real_avio_open2;
    static const char placeholder_prefix[] =
        "rtmp://127.0.0.1:1/croccante-destination/";
    const char *destination;

    if (real_avio_open2 == NULL) {
        real_avio_open2 = (avio_open2_fn)dlsym(RTLD_NEXT, "avio_open2");
        if (real_avio_open2 == NULL) {
            return -1;
        }
    }

    if (url != NULL &&
        strncmp(url, placeholder_prefix, sizeof(placeholder_prefix) - 1) == 0) {
        destination = getenv("CROCCANTE_DESTINATION_URL");
        if (destination == NULL || destination[0] == '\0') {
            return -1;
        }
        return real_avio_open2(context, destination, flags,
                               interrupt_callback, options);
    }

    return real_avio_open2(context, url, flags, interrupt_callback, options);
}
