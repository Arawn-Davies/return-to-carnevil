/* dev_pic.c: the PIC microcontroller, `pic:`.
 *
 * docs/analysis/loader-services.md section 2 ("pic": only init and ioctl; the init, which
 * read the PIC's serial block, ran before the hand-off) and section 3 (`pic:`): the three
 * commands the game issues return words the init stored, read from the dump. Open, close,
 * read and write are the shared return-0 routine.
 */
#include "loader.h"

/* The words at 0x800B8650, 0x800B8398 and 0x800B867C at the hand-off (sections 3 and 7);
 * their meaning (serial number, game number, date code are plausible) is not decoded. */
static const uint32_t PIC_WORDS[3] = {0x0001E240u, 0x000001D5u, 0x00001B88u};

/* ioctl: section 3, 0x7000 to 0x7002 store a word through arg */
static int pic_ioctl(int fd, uint32_t cmd, uint32_t arg)
{
    if (cmd < 0x7000 || cmd > 0x7002)
        ld_bad_ioctl(fd, cmd, arg); /* 0x7003 to 0x7006, the clock commands: not issued */
    ld_put32(arg, PIC_WORDS[cmd - 0x7000]);
    return 0;
}

const struct ldev ld_pic = {"pic:", ld_ret0_open, ld_ret0_close, ld_ret0_rw, ld_ret0_rw,
                            pic_ioctl};
