/* disk_driver.c: runs runtime/disk from commands on standard input, for tests/test_disk_c.py.
 *
 * Usage: disk_driver RAW OVERLAY, OVERLAY "-" for none. It prints "open 0", or "open -1"
 * and the message on the next line and exits with status 1. Then one command per line, each
 * answered by one line (hashes are SHA-256 in hex; names have no spaces):
 *   sectors                  sectors N
 *   header                   header SHA (of the 512 bytes), then: word WORD_AT_0xC
 *   part N                   part RC START COUNT
 *   read LBA COUNT           read RC SHA
 *   write LBA COUNT BYTE     write RC             (every byte of the sectors is BYTE)
 *   stat NAME                stat RC SIZE ATTR TIME DATE
 *   hash NAME                hash RC SIZE SHA     (open mode 0, read to the end in 64 KB
 *                                                  calls, close; RC is open's result)
 *   open NAME MODE           open H
 *   close H                  close RC
 *   rd H N                   rd RC SHA            (SHA of the RC bytes returned, if RC > 0)
 *   wr H N SEED              wr RC                (byte i is (SEED + 7 * i) & 255)
 *   seek H OFF WHENCE        seek RESULT
 *   unlink NAME              unlink RC
 *   rename A B               rename RC
 *   dump N PATH              dump RC              (partition N as now written, to PATH)
 *   flush                    flush
 *   sha TEXT                 sha SHA              (of TEXT; "-" for the empty string)
 *   shaa N                   shaa SHA             (of N bytes "a", fed in uneven pieces)
 *   writes                   writes N             (img_write calls since the image opened)
 *   die N                    die N                (the process exits with status 9, at once,
 *                                                  before the N-th img_write from now: a kill
 *                                                  between two writes)
 *   hosterr                  hosterr              (from now on a host I/O error prints
 *                                                  "hosterr MESSAGE" and exits with status 4,
 *                                                  as the program stops on one; before it, the
 *                                                  call fails with -1)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "disk.h"

static unsigned long writes, die_at;

static void on_write(void)
{
    if (++writes == die_at) {
        fflush(stdout);
        _exit(9);
    }
}

static void on_host_error(const char *message)
{
    printf("hosterr %s\n", message);
    fflush(stdout);
    exit(4);
}

static void print_hash(const char *tag, const void *data, size_t n, const char *prefix)
{
    uint8_t d[32];
    sha256(data, n, d);
    printf("%s%s", tag, prefix);
    for (int i = 0; i < 32; i++)
        printf("%02x", d[i]);
    printf("\n");
}

static void hash_ctx(sha256_ctx *s)
{
    uint8_t d[32];
    sha256_final(s, d);
    for (int i = 0; i < 32; i++)
        printf("%02x", d[i]);
    printf("\n");
}

static uint8_t *buffer(size_t n)
{
    uint8_t *p = malloc(n ? n : 1);
    if (!p) {
        fprintf(stderr, "disk_driver: out of memory\n");
        exit(2);
    }
    return p;
}

static int dump(int n, const char *path)
{
    uint32_t start, count;
    if (img_partition(n, &start, &count) != 0)
        return -1;
    FILE *out = fopen(path, "wb");
    if (!out)
        return -1;
    uint8_t *buf = buffer(256 * IMG_SECTOR);
    int rc = 0;
    for (uint32_t done = 0; done < count && rc == 0;) {
        uint32_t k = count - done < 256 ? count - done : 256;
        if (img_read(start + done, buf, k) != 0 || fwrite(buf, IMG_SECTOR, k, out) != k)
            rc = -1;
        done += k;
    }
    free(buf);
    if (fclose(out) != 0)
        rc = -1;
    return rc;
}

int main(int argc, char **argv)
{
    char err[512];
    if (argc != 3) {
        fprintf(stderr, "usage: disk_driver RAW OVERLAY|-\n");
        return 2;
    }
    int rc = img_open(argv[1], strcmp(argv[2], "-") ? argv[2] : NULL, err, sizeof err);
    img_set_write_hook(on_write);
    printf("open %d\n", rc);
    if (rc != 0) {
        printf("%s\n", err);
        return 1;
    }
    char line[1024], a[256], b[256];
    while (fgets(line, sizeof line, stdin)) {
        char cmd[32];
        unsigned long x, y, z;
        long off;
        int h, m;
        if (sscanf(line, "%31s", cmd) != 1)
            continue;
        if (!strcmp(cmd, "sectors")) {
            printf("sectors %u\n", img_sectors());
        } else if (!strcmp(cmd, "header")) {
            const uint8_t *p = img_header();
            print_hash("header", p, IMG_SECTOR, " ");
            printf("word %u\n", (unsigned)(p[0xC] | p[0xD] << 8 | p[0xE] << 16 |
                                           (uint32_t)p[0xF] << 24));
        } else if (!strcmp(cmd, "part") && sscanf(line, "%*s %d", &h) == 1) {
            uint32_t s = 0, c = 0;
            rc = img_partition(h, &s, &c);
            printf("part %d %u %u\n", rc, s, c);
        } else if (!strcmp(cmd, "read") && sscanf(line, "%*s %lu %lu", &x, &y) == 2) {
            uint8_t *p = buffer(y * IMG_SECTOR);
            rc = img_read((uint32_t)x, p, (uint32_t)y);
            if (rc == 0)
                print_hash("read 0", p, y * IMG_SECTOR, " ");
            else
                printf("read %d -\n", rc);
            free(p);
        } else if (!strcmp(cmd, "write") && sscanf(line, "%*s %lu %lu %lu", &x, &y, &z) == 3) {
            uint8_t *p = buffer(y * IMG_SECTOR);
            memset(p, (int)z, y * IMG_SECTOR);
            printf("write %d\n", img_write((uint32_t)x, p, (uint32_t)y));
            free(p);
        } else if (!strcmp(cmd, "stat") && sscanf(line, "%*s %255s", a) == 1) {
            struct fs_stat st = {0, 0, 0, 0};
            rc = fs_find(a, &st);
            printf("stat %d %u %u %u %u\n", rc, st.size, st.attr, st.time, st.date);
        } else if (!strcmp(cmd, "hash") && sscanf(line, "%*s %255s", a) == 1) {
            h = fs_open(a, 0);
            printf("hash %d ", h);
            sha256_ctx s;
            sha256_init(&s);
            unsigned long size = 0;
            if (h >= 0) {
                uint8_t *p = buffer(65536);
                int got;
                while ((got = fs_read(h, p, 65536)) > 0) {
                    sha256_update(&s, p, (size_t)got);
                    size += (unsigned long)got;
                }
                free(p);
                fs_close(h);
            }
            printf("%lu ", size);
            hash_ctx(&s);
        } else if (!strcmp(cmd, "open") && sscanf(line, "%*s %255s %d", a, &m) == 2) {
            printf("open %d\n", fs_open(a, m));
        } else if (!strcmp(cmd, "close") && sscanf(line, "%*s %d", &h) == 1) {
            printf("close %d\n", fs_close(h));
        } else if (!strcmp(cmd, "rd") && sscanf(line, "%*s %d %lu", &h, &x) == 2) {
            uint8_t *p = buffer(x);
            rc = fs_read(h, p, (uint32_t)x);
            char tag[32];
            snprintf(tag, sizeof tag, "rd %d", rc);
            print_hash(tag, p, rc > 0 ? (size_t)rc : 0, " ");
            free(p);
        } else if (!strcmp(cmd, "wr") && sscanf(line, "%*s %d %lu %lu", &h, &x, &z) == 3) {
            uint8_t *p = buffer(x);
            for (unsigned long i = 0; i < x; i++)
                p[i] = (uint8_t)(z + 7 * i);
            printf("wr %d\n", fs_write(h, p, (uint32_t)x));
            free(p);
        } else if (!strcmp(cmd, "seek") && sscanf(line, "%*s %d %ld %d", &h, &off, &m) == 3) {
            printf("seek %ld\n", fs_seek_words(h, off, m));
        } else if (!strcmp(cmd, "unlink") && sscanf(line, "%*s %255s", a) == 1) {
            printf("unlink %d\n", fs_unlink(a));
        } else if (!strcmp(cmd, "rename") && sscanf(line, "%*s %255s %255s", a, b) == 2) {
            printf("rename %d\n", fs_rename(a, b));
        } else if (!strcmp(cmd, "dump") && sscanf(line, "%*s %d %255s", &h, a) == 2) {
            printf("dump %d\n", dump(h, a));
        } else if (!strcmp(cmd, "flush")) {
            img_flush();
            printf("flush\n");
        } else if (!strcmp(cmd, "writes")) {
            printf("writes %lu\n", writes);
        } else if (!strcmp(cmd, "die") && sscanf(line, "%*s %lu", &x) == 1) {
            die_at = writes + x;
            printf("die %lu\n", x);
        } else if (!strcmp(cmd, "hosterr")) {
            img_set_host_error(on_host_error);
            printf("hosterr\n");
        } else if (!strcmp(cmd, "sha") && sscanf(line, "%*s %255s", a) == 1) {
            print_hash("sha", a, strcmp(a, "-") ? strlen(a) : 0, " ");
        } else if (!strcmp(cmd, "shaa") && sscanf(line, "%*s %lu", &x) == 1) {
            uint8_t piece[97];
            memset(piece, 'a', sizeof piece);
            sha256_ctx s;
            sha256_init(&s);
            for (unsigned long done = 0, k = 1; done < x; done += k, k = k % 97 + 1)
                sha256_update(&s, piece, k < x - done ? k : x - done);
            printf("shaa ");
            hash_ctx(&s);
        } else {
            fprintf(stderr, "disk_driver: bad command: %s", line);
            return 2;
        }
    }
    img_flush();
    return 0;
}
