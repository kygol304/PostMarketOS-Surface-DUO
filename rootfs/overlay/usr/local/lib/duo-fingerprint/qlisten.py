#!/usr/bin/env python3
"""qlisten: minimal Linux stand-in for Android's qseecomd on the Surface Duo (legacy /dev/qseecom listeners).

TrustZone apps (keymaster/gatekeeper, FPC fingerprint) reach normal-world services through "listeners" that the
HLOS registers with QSEE. Android's qseecomd provides them; without them gatekeeper bails out (RPMB / SFS errors).
Message formats follow Qualcomm's open-source MinkIPC listeners (BSD-3-Clause-Clear): fs_msg.h, rpmb_service.h,
time_msg.h. Implemented here:
  0xa    file service  -> files under ROOT (TrustZone's absolute paths are prefixed with ROOT)
  0x2000 RPMB          -> UFS RPMB well-known LUN over SG_IO (rpmb.py)
  0xb    time          -> host clock
Every request is logged to LOG.  usage: qlisten.py [start|stop]
"""
import errno, os, signal, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qsee import IOWR, IO, Q, ion_heaps, ion_alloc
import fcntl

ROOT = '/var/lib/duo-tz'
LOG = '/var/log/qlisten.log'
PIDS = '/run/qlisten.pids'
NAME = 256
DATA = 20000


def log(m):
    with open(LOG, 'a') as f:
        f.write(time.strftime('%H:%M:%S ') + m + '\n')


def cstr(b, o, n=NAME):
    s = bytes(b[o:o + n])
    return s.split(b'\0')[0].decode(errors='replace')


def host_path(p):
    p = os.path.normpath('/' + p.lstrip('/'))
    return ROOT + p


class Listener:
    def __init__(self, lid, size):
        self.lid, self.size = lid, size
        self.q = os.open('/dev/qseecom', os.O_RDWR)
        self.ion, _ = ion_heaps()
        self.fd, self.sb, self.va = ion_alloc(self.ion, 27, (size + 4095) & ~4095)
        r = bytearray(struct.pack('<Ii Q I4x', lid, self.fd, self.va, size))
        fcntl.ioctl(self.q, IOWR(Q, 1, len(r)), r)
        log('listener 0x%x registered (%d bytes)' % (lid, size))

    def run(self, handler):
        while True:
            try:
                fcntl.ioctl(self.q, IO(Q, 5))          # RECEIVE_REQ: blocks until TrustZone calls us
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                log('0x%x receive error %s' % (self.lid, e)); time.sleep(1); continue
            try:
                handler(self.sb)
            except Exception as e:                     # never leave TrustZone waiting
                log('0x%x handler crash: %r' % (self.lid, e))
                struct.pack_into('<i', self.sb, 4, -1)
            fcntl.ioctl(self.q, IO(Q, 6))              # SEND_RESP: response is in the shared buffer


# ---------------------------------------------------------------- file service (0xa)
class FS:
    def __init__(self):
        self.err = 0
        self.dirs = {}
        os.makedirs(ROOT, exist_ok=True)

    def ret(self, b, off, fn):
        try:
            v = fn()
            self.err = 0
        except OSError as e:
            self.err, v = e.errno, -1
        if v is None:
            v = 0
        struct.pack_into('<i', b, off, v)
        return v

    def stat_bytes(self, st):
        return struct.pack('<Q4xIIIIIQ4xqIQIIIIIIQ', st.st_dev, st.st_ino & 0xffffffff, st.st_mode, st.st_nlink,
                           st.st_uid, st.st_gid, st.st_rdev, st.st_size, st.st_blksize, st.st_blocks,
                           int(st.st_atime), 0, int(st.st_mtime), 0, int(st.st_ctime), 0, st.st_ino)

    def __call__(self, b):
        cmd = struct.unpack_from('<I', b)[0]
        r = self.ret
        if cmd == 0x202:                                   # open(path, flags)
            p, fl = cstr(b, 4), struct.unpack_from('<i', b, 4 + NAME)[0]
            if fl & os.O_CREAT:
                os.makedirs(os.path.dirname(host_path(p)), exist_ok=True)
            v = r(b, 4, lambda: os.open(host_path(p), fl, 0o600)); log('fs open %s 0x%x -> %d' % (p, fl, v))
        elif cmd == 0x203:                                 # openat(dirfd, path, flags)
            d, p, fl = struct.unpack_from('<i', b, 4)[0], cstr(b, 8), struct.unpack_from('<i', b, 8 + NAME)[0]
            v = r(b, 4, lambda: os.open(host_path(p), fl, 0o600)); log('fs openat %s -> %d' % (p, v))
        elif cmd == 0x204:                                 # unlinkat
            p = cstr(b, 8); v = r(b, 4, lambda: os.unlink(host_path(p))); log('fs unlinkat %s -> %d' % (p, v))
        elif cmd == 0x205:                                 # fcntl(fd, cmd)
            fd, c = struct.unpack_from('<ii', b, 4); v = r(b, 4, lambda: fcntl.fcntl(fd, c)); log('fs fcntl %d %d -> %d' % (fd, c, v))
        elif cmd == 0x206:                                 # creat(path, mode)
            p = cstr(b, 4)
            os.makedirs(os.path.dirname(host_path(p)), exist_ok=True)
            v = r(b, 4, lambda: os.open(host_path(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)); log('fs creat %s -> %d' % (p, v))
        elif cmd == 0x207:                                 # read(fd, count) -> buf[DATA], ret
            fd, n = struct.unpack_from('<iI', b, 4)
            try:
                data = os.read(fd, min(n, DATA)); self.err = 0
                b[4:4 + len(data)] = data; struct.pack_into('<i', b, 4 + DATA, len(data))
            except OSError as e:
                self.err = e.errno; struct.pack_into('<i', b, 4 + DATA, -1); data = b''
            log('fs read fd %d %d -> %d' % (fd, n, len(data)))
        elif cmd == 0x208:                                 # write(fd, buf[DATA], count)
            fd = struct.unpack_from('<i', b, 4)[0]; n = struct.unpack_from('<I', b, 8 + DATA)[0]
            data = bytes(b[8:8 + min(n, DATA)])
            v = r(b, 4, lambda: os.write(fd, data)); log('fs write fd %d %d -> %d' % (fd, n, v))
        elif cmd == 0x209:                                 # close
            fd = struct.unpack_from('<i', b, 4)[0]; v = r(b, 4, lambda: os.close(fd)); log('fs close %d -> %d' % (fd, v))
        elif cmd == 0x20a:                                 # lseek(fd, off, whence)
            fd, off, wh = struct.unpack_from('<iii', b, 4); v = r(b, 4, lambda: os.lseek(fd, off, wh)); log('fs lseek %d %d %d -> %d' % (fd, off, wh, v))
        elif cmd == 0x20b:                                 # link
            a, c = cstr(b, 4), cstr(b, 4 + NAME); v = r(b, 4, lambda: os.link(host_path(a), host_path(c))); log('fs link %s %s -> %d' % (a, c, v))
        elif cmd in (0x20c, 0x213):                        # unlink / remove
            p = cstr(b, 4); v = r(b, 4, lambda: os.unlink(host_path(p))); log('fs remove %s -> %d' % (p, v))
        elif cmd == 0x20d:                                 # rmdir
            p = cstr(b, 4); v = r(b, 4, lambda: os.rmdir(host_path(p))); log('fs rmdir %s -> %d' % (p, v))
        elif cmd in (0x20e, 0x20f):                        # fstat(fd) / lstat(path) -> tz_stat, ret
            arg = struct.unpack_from('<i', b, 4)[0] if cmd == 0x20e else cstr(b, 4)
            try:
                st = os.fstat(arg) if cmd == 0x20e else os.lstat(host_path(arg)); sb = self.stat_bytes(st); v = 0; self.err = 0
            except OSError as e:
                sb, v, self.err = b'\0' * 104, -1, e.errno
            b[4:4 + len(sb)] = sb; struct.pack_into('<i', b, 4 + len(sb), v)
            log('fs %s %s -> %d' % ('fstat' if cmd == 0x20e else 'lstat', arg, v))
        elif cmd == 0x210:                                 # mkdir(path, mode)
            p = cstr(b, 4); v = r(b, 4, lambda: os.makedirs(host_path(p), 0o700, exist_ok=False)); log('fs mkdir %s -> %d' % (p, v))
        elif cmd == 0x211:                                 # testdir(path): 0 if a directory
            p = cstr(b, 4); v = 0 if os.path.isdir(host_path(p)) else -1
            struct.pack_into('<i', b, 4, v); self.err = 0 if v == 0 else errno.ENOENT; log('fs testdir %s -> %d' % (p, v))
        elif cmd == 0x212:                                 # telldir
            struct.pack_into('<i', b, 4, 0); log('fs telldir')
        elif cmd == 0x214:                                 # chown/chmod: nothing to do here
            struct.pack_into('<i', b, 4, 0); log('fs chown_chmod %s' % cstr(b, 4))
        elif cmd == 0x216:                                 # sync(fd)
            fd = struct.unpack_from('<i', b, 4)[0]; v = r(b, 4, lambda: os.fsync(fd)); log('fs sync %d -> %d' % (fd, v))
        elif cmd == 0x217:                                 # rename
            a, c = cstr(b, 4), cstr(b, 4 + NAME); v = r(b, 4, lambda: os.rename(host_path(a), host_path(c))); log('fs rename %s %s -> %d' % (a, c, v))
        elif cmd == 0x218:                                 # partition free size -> u64 size, ret
            st = os.statvfs(ROOT); struct.pack_into('<Qi', b, 4, st.f_bavail * st.f_frsize, 0); log('fs free size %s' % cstr(b, 4))
        elif cmd == 0x219:                                 # opendir -> u64 handle, ret
            p = cstr(b, 4)
            try:
                ents = os.listdir(host_path(p)); h = len(self.dirs) + 1; self.dirs[h] = [0, ents]; v = 0
            except OSError as e:
                h, v, self.err = 0, -1, e.errno
            struct.pack_into('<Qi', b, 4, h, v); log('fs opendir %s -> %d' % (p, v))
        elif cmd == 0x21a:                                 # readdir(handle) -> tz_dirent, ret
            h = struct.unpack_from('<Q', b, 4)[0]; d = self.dirs.get(h)
            ent = struct.pack('<QqHB', 0, 0, 0, 0) + b'\0' * NAME; v = -1
            if d and d[0] < len(d[1]):
                n = d[1][d[0]]; d[0] += 1; v = 0
                ent = struct.pack('<QqHB', d[0], d[0], 19 + len(n), 8) + n.encode()[:NAME - 1].ljust(NAME, b'\0')
            b[4:4 + len(ent)] = ent; struct.pack_into('<i', b, 4 + len(ent), v)
        elif cmd == 0x21b:                                 # closedir
            self.dirs.pop(struct.unpack_from('<Q', b, 4)[0], None); struct.pack_into('<i', b, 4, 0)
        elif cmd == 0x21c:                                 # get errno
            struct.pack_into('<i', b, 4, self.err); log('fs errno -> %d' % self.err)
        else:
            struct.pack_into('<i', b, 4, -1); log('fs UNKNOWN cmd 0x%x' % cmd)


# ---------------------------------------------------------------- RPMB (0x2000)
class RPMBService:
    REL_WR = 32          # frames per transfer reported to TrustZone (device accepts >= 64)
    SECTORS = 32768      # 16 MiB in 512-byte sectors (probed: every 256-byte half-sector address readable)

    def __init__(self):
        from rpmb import RPMB
        self.r = RPMB()
        try:
            self.r.counter()                 # clears the power-on unit attention
        except OSError:
            pass

    def __call__(self, b):
        cmd = struct.unpack_from('<I', b)[0]
        if cmd == 0x101:                     # init {cmd, version} -> {cmd, version, status, sectors, rel_wr[, type...]}
            ver = struct.unpack_from('<I', b, 4)[0]
            if ver >= 2:
                b[:40] = struct.pack('<IIiIIIIIII', cmd, 2, 0, self.SECTORS, self.REL_WR, 2, 0, 0, 0, 0)  # dev_type: UFS
            else:
                b[:20] = struct.pack('<IIiII', cmd, 1, 0, self.SECTORS, self.REL_WR)
            log('rpmb init v%d -> %d sectors, rel_wr %d' % (ver, self.SECTORS, self.REL_WR))
        elif cmd in (0x102, 0x103):          # read/write {cmd, num_sectors, req_len, req_off, version, rel_wr_count}
            n, rlen, roff, ver, rwc = struct.unpack_from('<IIIII', b, 4)
            req = bytes(b[roff:roff + n * 512 if cmd == 0x103 else roff + rlen])
            st, resp = 0, b''
            try:
                if cmd == 0x102:
                    trans = (n + self.REL_WR - 1) // self.REL_WR
                    left = n
                    for i in range(trans):
                        k = min(left, self.REL_WR)
                        self.r.sec(True, req[i * 512:(i + 1) * 512])
                        resp += self.r.sec(False, length=k * 512); left -= k
                else:
                    fpt = rwc or 1
                    for i in range(n // fpt):
                        self.r.sec(True, req[i * fpt * 512:(i + 1) * fpt * 512])
                        self.r.sec(True, self.r.frame(5))             # result read request
                        resp = self.r.sec(False, length=512)
            except OSError as e:
                st = -1; log('rpmb %s error %s' % ('read' if cmd == 0x102 else 'write', e))
            hdr = struct.pack('<IiIII', cmd, st, len(resp), 20, ver)
            b[:20] = hdr; b[20:20 + len(resp)] = resp
            res = struct.unpack_from('>H', resp, 508)[0] if len(resp) >= 512 else -1
            log('rpmb %s %d frames -> status %d, result 0x%x' % ('read' if cmd == 0x102 else 'write', n, st, res))
        elif cmd == 0x104:                   # partition table query: not provisioned
            b[:16] = struct.pack('<IiII', cmd, -1, 0, 16); log('rpmb partition query')
        else:
            struct.pack_into('<i', b, 4, -1); log('rpmb UNKNOWN cmd 0x%x' % cmd)


# ---------------------------------------------------------------- GP file service (0x7000)
GP_MAX = 500 * 1024


LEGACY = {0: 4, 1: 5, 2: 6, 3: 7}  # older absolute-path set: 0 read, 1 write (seen from fpctzrel); 2 remove, 3 rename guessed


def gp_path(cmd, name, legacy=False):
    if legacy:
        return host_path(name)                         # absolute path, same root as the file service
    base = 'persist' if cmd in (8, 9, 10, 11) else 'data'
    return os.path.join(ROOT, 'gp', base, name.lstrip('/'))


def gpfs_service(b):
    cmd = struct.unpack_from('<I', b)[0]
    legacy = cmd in LEGACY
    if legacy:
        log('gpfs legacy cmd %d treated as %d' % (cmd, LEGACY[cmd]))
        orig, cmd = cmd, LEGACY[cmd]
        struct.pack_into('<I', b, 0, cmd)
        gpfs_modern(b, legacy=True)
        struct.pack_into('<I', b, 0, orig)
        return
    gpfs_modern(b)


def gpfs_modern(b, legacy=False):
    cmd = struct.unpack_from('<I', b)[0]
    if cmd in (4, 8):                                  # read {cmd, path, i32 off, u32 count} -> {cmd, err, n, buf}
        name = cstr(b, 4); off, n = struct.unpack_from('<iI', b, 4 + NAME); p = gp_path(cmd, name, legacy)
        try:
            with open(p, 'rb') as f:
                f.seek(off); data = f.read(min(n, GP_MAX))
            err = 0
        except OSError as e:
            data, err = b'', e.errno
        struct.pack_into('<iiI', b, 0, cmd, err, len(data)); b[12:12 + len(data)] = data
        log('gpfs read %s @%d %d -> err %d, %d bytes' % (p[len(ROOT):], off, n, err, len(data)))
    elif cmd in (5, 9):                                # write {cmd, path, i32 off, u32 count, u32 backup, buf}
        name = cstr(b, 4); off, n, bak = struct.unpack_from('<iII', b, 4 + NAME); p = gp_path(cmd, name, legacy)
        data = bytes(b[4 + NAME + 12:4 + NAME + 12 + min(n, GP_MAX)])
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if bak and os.path.exists(p):
                with open(p, 'rb') as s, open(p + '.bak', 'wb') as d:
                    d.write(s.read()); d.flush(); os.fsync(d.fileno())
            fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_SYNC, 0o600)
            os.lseek(fd, off, 0); w = os.write(fd, data); os.fsync(fd); os.close(fd); err = 0
        except OSError as e:
            w, err = 0, e.errno
        struct.pack_into('<iiI', b, 0, cmd, err, w)
        log('gpfs write %s @%d %d (backup %d) -> err %d' % (p[len(ROOT):], off, n, bak, err))
    elif cmd in (6, 10):                               # remove {cmd, path} -> {cmd, err}
        p = gp_path(cmd, cstr(b, 4), legacy)
        try:
            os.unlink(p); err = 0
        except OSError as e:
            err = e.errno
        struct.pack_into('<ii', b, 0, cmd, err); log('gpfs remove %s -> %d' % (p[len(ROOT):], err))
    elif cmd in (7, 11):                               # rename {cmd, from, to} -> {cmd, err}
        a, c = gp_path(cmd, cstr(b, 4), legacy), gp_path(cmd, cstr(b, 4 + NAME), legacy)
        try:
            os.rename(a, c); err = 0
        except OSError as e:
            err = e.errno
        struct.pack_into('<ii', b, 0, cmd, err); log('gpfs rename %s %s -> %d' % (a[len(ROOT):], c[len(ROOT):], err))
    elif cmd == 12:                                    # version -> {cmd, version, err}
        struct.pack_into('<IIi', b, 0, cmd, 2, 0); log('gpfs version')
    else:
        raw = bytes(b[:4 + NAME + 16])
        log('gpfs UNKNOWN cmd 0x%x path %r tail %s' % (cmd, cstr(b, 4), raw[4 + NAME:].hex()))
        struct.pack_into('<ii', b, 0, cmd, -1)


# ---------------------------------------------------------------- time (0xb)
def time_service(b):
    cmd = struct.unpack_from('<I', b)[0]
    t = time.time()
    if cmd == 0x302:                         # utc sec -> timespec, ret
        b[4:16] = struct.pack('<IIi', int(t), int((t % 1) * 1e9), 0)
    elif cmd == 0x303:                       # systime -> tz_time, ret
        g = time.gmtime(t)
        b[4:44] = struct.pack('<10i', g.tm_sec, g.tm_min, g.tm_hour, g.tm_mday, g.tm_mon - 1, g.tm_year - 1900,
                              (g.tm_wday + 1) % 7, g.tm_yday - 1, 0, 0)
    elif cmd == 0x304:                       # time ms -> u64? (tz_time_spec in ms) , ret
        b[4:16] = struct.pack('<Qi', int(t * 1000), 0)
    elif cmd == 0x305:                       # HLOS utc -> timespec, ret
        b[4:16] = struct.pack('<IIi', int(t), int((t % 1) * 1e9), 0)
    else:
        struct.pack_into('<i', b, 4, -1)
    log('time cmd 0x%x' % cmd)


SERVICES = [(0xa, 20 * 1024 + 4096, FS), (0x2000, 20040 + 4096, RPMBService), (0xb, 20 * 1024, lambda: time_service),
            (0x7000, 504 * 1024, lambda: gpfs_service)]


def start():
    pids = []
    for lid, size, make in SERVICES:
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                h = make()
                Listener(lid, size).run(h)
            except Exception as e:
                log('listener 0x%x failed: %r' % (lid, e))
            os._exit(1)
        pids.append(pid)
    open(PIDS, 'w').write(' '.join(map(str, pids)))
    time.sleep(1.5)
    print(open(LOG).read()[-600:])


def stop():
    try:
        for p in open(PIDS).read().split():
            try:
                os.kill(int(p), signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.unlink(PIDS)
    except FileNotFoundError:
        pass


if __name__ == '__main__':
    (stop if sys.argv[1:] == ['stop'] else start)()
