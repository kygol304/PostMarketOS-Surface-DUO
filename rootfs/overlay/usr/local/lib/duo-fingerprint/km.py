#!/usr/bin/env python3
"""keymaster64 trustlet client (QTI keymaster 4 HAL handshake): get_version 0x200, set_version 0x207,
GET_AUTH_TOKEN_KEY 0x205 (as the FPC HAL sends it)."""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qsee import *

SB = 0xa000

class KM:
    def __init__(self, name='keymaster64'):
        self.q = os.open('/dev/qseecom', os.O_RDWR)
        self.ion, _ = ion_heaps()
        self.fd, self.sb, self.va = ion_alloc(self.ion, 27, SB)
        r = bytearray(struct.pack('i4xQI4x', self.fd, self.va, SB)); fcntl.ioctl(self.q, IOWR(Q, 8, len(r)), r)
        self.bind = query(self.q, name)

    def send(self, cmd, resp_off, resp_len):
        self.sb[:SB] = b'\0' * SB
        self.sb[:len(cmd)] = cmd
        s = bytearray(struct.pack('QI4xQI4x', self.va, len(cmd), self.va + resp_off, resp_len))
        fcntl.ioctl(self.q, IOWR(Q, 3, len(s)), s)
        return bytes(self.sb[resp_off:resp_off + resp_len])

    def get_version(self):
        r = self.send(struct.pack('<I', 0x200), 4, SB - 4)
        return struct.unpack_from('<iIIII', r)

    def set_version(self):
        r = self.send(struct.pack('<IIIIII', 0x207, 4, 5, 4, 5, 0), 0x18, SB - 0x18)
        return struct.unpack_from('<i', r)[0]

    def hmac_params(self):
        r = self.send(struct.pack('<I', 0x20e), 4, SB - 4)
        return struct.unpack_from('<i', r)[0], r[4:0x44]

    def compute_shared_hmac(self, params):
        blob = b''.join(params)
        r = self.send(struct.pack('<III', 0x20f, 12, len(params)) + blob, 12 + len(blob), SB - 12 - len(blob))
        return struct.unpack_from('<i', r)[0], r[4:0x24]

    def setup(self):
        """what Android does at boot: version handshake, then HMAC agreement (only this TEE keymaster here)"""
        v = self.get_version(); sv = self.set_version()
        st, p = self.hmac_params()
        cs, check = self.compute_shared_hmac([p]) if st == 0 else (st, b'')
        return v, sv, st, cs, check

    def _gk(self, hdr, parts):
        # legacy gatekeeper: 32-byte header, then the data blobs; header fields hold (offset, length) pairs
        body = b''; offs = []
        for d in parts:
            offs.append((32 + len(body), len(d))); body += d
        cmd = hdr(offs) + body
        r = self.send(cmd, len(cmd), SB - len(cmd))
        st, off, ln = struct.unpack_from('<iII', r)
        return st, (r[off:off + ln] if st == 0 and off + ln <= len(r) else b''), r[:16]

    def gk_enroll(self, uid, password):
        return self._gk(lambda o: struct.pack('<IIIIIIII', 0x1001, uid, 0, 0, 0, 0, *o[0]), [password])

    def gk_verify(self, uid, challenge, handle, password):
        return self._gk(lambda o: struct.pack('<IIQIIII', 0x1002, uid, challenge, *o[0], *o[1]), [handle, password])

    def auth_token_key(self):
        r = self.send(struct.pack('<II', 0x205, 2) + b'\0' * 56, 64, 960)
        st, off, ln = struct.unpack_from('<iII', r)
        return st, (r[off:off + ln] if st == 0 and off + ln <= len(r) else b'')

if __name__ == '__main__':
    k = KM(); print('bind', k.bind)
    v, sv, st, cs, check = k.setup()
    print('version', v, 'set_version', sv, 'hmac params', st, 'compute_shared_hmac', cs, check.hex()[:16])
    st, key = k.auth_token_key()
    print('auth token key status %d (0x%08x), %d bytes' % (st, st & 0xffffffff, len(key)), key[:16].hex())
