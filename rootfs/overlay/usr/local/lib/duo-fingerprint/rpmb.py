#!/usr/bin/env python3
"""UFS RPMB access over SG_IO (the RPMB well-known LUN 0:0:0:49476 as /dev/sgN). Read-only probing helpers plus the
raw frame transfer the TrustZone RPMB listener needs (MinkIPC rpmb_ufs.c, BSD-3-Clause-Clear, as reference)."""
import ctypes, fcntl, glob, os, struct, sys

SG_IO = 0x2285
TO_DEV, FROM_DEV = -2, -3
FRAME = 512

def sg_node():
    g = glob.glob('/sys/class/scsi_device/0:0:0:49476/device/scsi_generic/sg*')
    return '/dev/' + os.path.basename(g[0])

class RPMB:
    def __init__(self):
        p = sg_node()
        if not os.path.exists(p):
            mj, mn = open('/sys/class/scsi_generic/%s/dev' % os.path.basename(p)).read().split(':')
            os.mknod(p, 0o600 | 0o020000, os.makedev(int(mj), int(mn)))
        self.fd = os.open(p, os.O_RDWR)

    def sg(self, cdb, data, direction, length):
        for attempt in range(3):
            try:
                return self._sg(cdb, data, direction, length)
            except OSError as e:
                if 'sense 70000600' not in str(e) or attempt == 2: raise   # unit attention: retry

    def _sg(self, cdb, data, direction, length):
        buf = ctypes.create_string_buffer(data if direction == TO_DEV else b'\0' * length, length)
        cmd = ctypes.create_string_buffer(bytes(cdb), len(cdb)); sense = ctypes.create_string_buffer(32)
        hdr = bytearray(struct.pack('<iiBBHIQQQIIi4xQBBBBHHiII4x', ord('S'), direction, len(cdb), 32, 0, length,
                                    ctypes.addressof(buf), ctypes.addressof(cmd), ctypes.addressof(sense),
                                    30000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(self.fd, SG_IO, hdr)
        f = struct.unpack('<iiBBHIQQQIIi4xQBBBBHHiII4x', hdr)
        status, host, drv = f[13], f[17], f[18]
        if status or host or drv:
            raise OSError('SCSI status %d host %d driver %d sense %s' % (status, host, drv, sense.raw[:18].hex()))
        return buf.raw

    def sec(self, out, payload=b'', length=0):
        n = len(payload) if out else length
        cdb = [0xB5 if out else 0xA2, 0xEC, 0x00, 0x01, 0, 0, (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255, 0, 0]
        return self.sg(cdb, payload, TO_DEV if out else FROM_DEV, n)

    @staticmethod
    def frame(req, addr=0, count=0, nonce=b'\x11' * 16):
        f = bytearray(FRAME)
        f[484:500] = nonce; struct.pack_into('>HHHH', f, 504, addr, count, 0, req)
        return bytes(f)

    def read(self, addr, count=1):
        self.sec(True, self.frame(4, addr, count))
        r = self.sec(False, length=FRAME * count)
        return [struct.unpack_from('>IHHHH', r, i * FRAME + 500) for i in range(count)]   # wcnt, addr, cnt, result, type

    def counter(self):
        self.sec(True, self.frame(2))
        r = self.sec(False, length=FRAME)
        return struct.unpack_from('>IHHHH', r, 500)

if __name__ == '__main__':
    r = RPMB(); print('node', sg_node())
    print('write counter frame (wcnt, addr, cnt, result, type):', r.counter())
    print('read addr 0:', r.read(0)[0])
    lo, hi = 0, 0x10000                       # address in 256-byte units, 16 bits
    while hi - lo > 1:
        mid = (lo + hi) // 2
        res = r.read(mid)[0][3] & 0x7f
        if res == 0: lo = mid
        else: hi = mid
    print('last readable 256-byte block', lo, '-> RPMB size %d KiB, num_sectors(512B) %d' % ((lo + 1) // 4, (lo + 1) // 2))
    for n in (2, 4, 8, 16, 32, 64):
        try:
            res = r.read(0, n); print('read', n, 'frames in one transfer: results', sorted({x[3] for x in res}))
        except OSError as e:
            print('read', n, 'frames: error', e); break
