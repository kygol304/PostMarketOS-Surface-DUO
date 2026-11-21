#!/usr/bin/env python3
"""FPC fingerprint on the Surface Duo from Linux: HAL-equivalent open sequence, enroll, identify.
Commands decoded from the Duo's Android HAL (see NOTES.md)."""
import os, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qsee import *
from fptest import FPC, irq_wait, DEV

def u32(b, o): return struct.unpack_from('<I', b, o)[0]
def i32(b, o): return struct.unpack_from('<i', b, o)[0]

class Bio(FPC):
    def small(self, group, cmd, fill=None):
        """command in the HAL's 88-byte buffer; fill(buf) sets arguments; returns (qsee, ta_status, buf copy)"""
        self.sh[:0x58] = b'\0' * 0x58
        self.sh[:8] = struct.pack('<II', group, cmd)
        if fill: fill(self.sh)
        q = self.transfer()
        return q, i32(self.sh, 8), bytes(self.sh[:0x58])

    def big(self, group, cmd, payload, outlen=0):
        """variable-size command in its own ION buffer: {group, cmd, status, len, data...}"""
        size = 16 + max(len(payload), outlen)
        al = (size + 4095) & ~4095
        fd, m, _ = ion_alloc(self.ion, 27, al)
        m[:16] = struct.pack('<IIiI', group, cmd, 0, len(payload) or outlen); m[16:16 + len(payload)] = payload
        q = self.transfer(fd, al)
        r = (q, i32(m, 8), bytes(m[:size]))
        m.close(); os.close(fd)
        return r

def keymaster_key():
    """KEYMASTER_GET_AUTH_TOKEN_KEY (0x205, 2) from the keymaster64 trustlet already loaded in QSEE"""
    q = os.open('/dev/qseecom', os.O_RDWR)
    ion, _ = ion_heaps()
    fd, sb, va = ion_alloc(ion, 27, 1 << 12)
    r = bytearray(struct.pack('i4xQI4x', fd, va, 1 << 12)); fcntl.ioctl(q, IOWR(Q, 8, len(r)), r)
    print('keymaster64 bind:', query(q, 'keymaster64'))
    sb[:64] = struct.pack('<II', 0x205, 2) + b'\0' * 56
    s = bytearray(struct.pack('QI4xQI4x', va, 64, va + 64, 960))
    fcntl.ioctl(q, IOWR(Q, 3, len(s)), s)
    st, off, ln = i32(sb, 64), u32(sb, 68), u32(sb, 72)
    key = bytes(sb[64 + off:64 + off + ln]) if st == 0 and ln < 900 else b''
    print('keymaster: status %d, key %d bytes' % (st, len(key)))
    os.close(q)
    return key

def session_env():
    for pid in os.listdir('/proc'):
        try:
            if pid.isdigit() and open('/proc/%s/comm' % pid).read().strip() == 'phosh':
                for kv in open('/proc/%s/environ' % pid, 'rb').read().split(b'\0'):
                    if kv.startswith(b'DBUS_SESSION_BUS_ADDRESS='):
                        return kv.split(b'=', 1)[1].decode()
        except OSError:
            pass
    return ''

BUS = session_env()

def buzz(event='button-pressed'):
    os.system('DBUS_SESSION_BUS_ADDRESS=%s fbcli -E %s -t 1 >/dev/null 2>&1 &' % (BUS, event))

NID = [0]
def show(text):
    """one on-screen notification, updated in place (notify-send -r)"""
    import subprocess
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=BUS, XDG_RUNTIME_DIR='/run/user/0')
    a = ['notify-send', '-p', '-u', 'critical', '-t', '0', '-a', 'Fingerprint test']
    if NID[0]: a += ['-r', str(NID[0])]
    try:
        out = subprocess.run(a + ['Fingerprint', text], env=env, capture_output=True, text=True, timeout=5).stdout.strip()
        if out.isdigit(): NID[0] = int(out)
    except Exception:
        pass
    print('   [screen] ' + text, flush=True)

def touch(b, timeout=30):
    """HAL capture loop: wait finger up, arm, finger-down IRQ, capture (3 = bad image, try again)"""
    for _ in range(100):
        q, st, _ = b.sensor(1, 1)
        if st > 0: break
        time.sleep(0.02)
    for _ in range(5):
        b.sensor(3, 1)
        if not irq_wait(timeout):
            return None
        q, st, _ = b.sensor(4, 1)
        if st != 3:
            return st
        print('   image not usable, touch again', flush=True)
    return 3

if __name__ == '__main__':
    step = sys.argv[1] if len(sys.argv) > 1 else 'setup'
    from km import KM
    b = Bio(); print('fpctzrel loaded')
    k = KM(); print('keymaster64', k.bind, 'setup', k.setup()[:4])
    st, key = k.auth_token_key(); print('auth token key', st, len(key), 'bytes')
    PW = b'duo-linux-fingerprint'
    gst, handle, raw = k.gk_enroll(0, PW); print('gatekeeper enroll', gst, len(handle), 'byte handle', raw.hex())
    try:
        print('build info  ', b.big(12, 1, b'', 0x400)[:2])
        print('sensor init ', b.sensor(0), 'sleep', b.sensor(5))
        if key:
            q, st, _ = b.big(3, 5, key); print('set key     ', q, st)
        DB = '/data/vendor_de/0/fpdata/user.db'; HOSTDB = '/var/lib/duo-tz' + DB
        if os.path.exists(HOSTDB):
            q, st, _ = b.big(2, 11, DB.encode() + b'\0'); print('load db     ', q, st, DB)
        else:
            q, st, buf = b.small(11, 9, lambda m: m.__setitem__(slice(0xc, 0x10), struct.pack('<I', 0)))
            print('empty db    ', q, st, 'res', i32(buf, 8))
        q, st, buf = b.small(11, 10); print('db id       ', q, st, hex(struct.unpack_from('<Q', buf, 0x10)[0]))
        q, st, buf = b.small(11, 7, lambda m: m.__setitem__(slice(0xc, 0x10), struct.pack('<I', 5)))
        print('templates   ', q, st, 'count', u32(buf, 0xc), [u32(buf, 0x10 + 4 * i) for i in range(min(5, u32(buf, 0xc)))])
        if step in ('setup', 'enroll', 'both'):
            q, st, buf = b.small(3, 2); ch = struct.unpack_from('<Q', buf, 8)[0]
            print('enroll chal ', q, 'status', i32(buf, 0x10), hex(ch))
            vst, hat, raw = k.gk_verify(0, ch, handle, PW)
            print('gatekeeper verify', vst, len(hat), 'byte token', raw.hex(), hat[:9].hex())
            q, st, _ = b.big(3, 3, hat); print('authorize   ', q, st, '(gatekeeper token, %d bytes)' % len(hat))
            q, st, _ = b.small(11, 0); print('begin enroll', q, st)
        if step == 'setup':
            sys.exit(0)
        def store_db():
            os.makedirs(os.path.dirname(HOSTDB), exist_ok=True)
            open(HOSTDB + '.bak', 'ab').close()
            q, sst, _ = b.big(2, 12, (DB + '.bak').encode() + b'\0')
            if sst == 0 and os.path.exists(HOSTDB + '.bak'):
                os.rename(HOSTDB + '.bak', HOSTDB)
            return sst

        def ids():
            q, st, buf = b.small(11, 7, lambda m: m.__setitem__(slice(0xc, 0x10), struct.pack('<I', 5)))
            return [u32(buf, 0x10 + 4 * i) for i in range(min(5, u32(buf, 0xc)))]

        if step == 'list':
            l = ids()
            print('FINGERS %d' % len(l)); [print('  finger', i) for i in l]
        if step == 'delete':
            want = sys.argv[2] if len(sys.argv) > 2 else ''
            targets = ids() if want == 'all' else [int(want)] if want.isdigit() else []
            for fid in targets:
                q, st, _ = b.small(11, 8, lambda m: m.__setitem__(slice(0xc, 0x10), struct.pack('<I', fid)))
                print('delete finger', fid, '->', st)
            if targets:
                print('saved' if store_db() == 0 else 'SAVE FAILED', '- fingers left:', ids())
            else:
                print('usage: delete <finger id> | delete all')
        if step in ('enroll', 'both'):
            print('ENROLL', flush=True)
            show('ENROLL: touch the sensor with ONE finger, lift, repeat (move it slightly each time)')
            for n in range(40):
                st = touch(b, 180 if n == 0 else 30)
                if st is None: print('   no touch in 30 s, giving up'); break
                q, est, buf = b.small(11, 1)
                rem = u32(buf, 0xc)
                print('   touch %d: capture %d, enroll step status %d, remaining %d' % (n + 1, st, est, rem), flush=True)
                buzz()
                if est == 0: break
                show('ENROLL: got it (%d). Lift and touch again with the SAME finger' % (n + 1))
                if est < 0: break
            q, st, buf = b.small(11, 2); print('end enroll  ', q, st, 'finger id', u32(buf, 0xc), flush=True)
            if st == 0:
                os.makedirs(os.path.dirname(HOSTDB), exist_ok=True)
                open(HOSTDB + '.bak', 'ab').close()          # the TA opens the file read-only first
                q, sst, _ = b.big(2, 12, (DB + '.bak').encode() + b'\0')
                if sst == 0 and os.path.exists(HOSTDB + '.bak'):
                    os.rename(HOSTDB + '.bak', HOSTDB)
                print('store db    ', q, sst, os.path.getsize(HOSTDB) if os.path.exists(HOSTDB) else 'no file', flush=True)
            buzz('phone-incoming-call' if st == 0 else 'message-new-sms')
            show('ENROLL DONE' if st == 0 else 'ENROLL FAILED (%d)' % st)
            q, st, buf = b.small(11, 7, lambda m: m.__setitem__(slice(0xc, 0x10), struct.pack('<I', 5)))
            print('templates   ', st, 'count', u32(buf, 0xc), [u32(buf, 0x10 + 4 * i) for i in range(min(5, u32(buf, 0xc)))], flush=True)
            time.sleep(4)
        if step in ('identify', 'both'):
            for n in range(int(os.environ.get('TRIES', 4))):
                which = 'SAME (enrolled)' if n < 2 else 'a DIFFERENT'
                print('IDENTIFY %d' % (n + 1), flush=True)
                show('TEST %d of 4: touch once with %s finger' % (n + 1, which))
                b.small(3, 1, lambda m: m.__setitem__(slice(8, 16), struct.pack('<Q', 0)))
                st = touch(b, 120 if n == 0 else 60)
                if st is None: print('   no touch'); break
                q, s3, buf3 = b.small(11, 3); q, s4, buf4 = b.small(11, 4)
                print('   capture %d | 11/3 status %d val %d | 11/4 status %d val %d' % (st, s3, u32(buf3, 0xc), s4, u32(buf4, 0xc)), flush=True)
                print('     11/3 buf', buf3[:0x40].hex(), '\n     11/4 buf', buf4[:0x40].hex(), flush=True)
                buzz()
                show('TEST %d: %s. Lift your finger' % (n + 1, ('MATCH, finger id %d' % u32(buf3, 0xc)) if s3 == 0 and u32(buf3, 0xc) else 'no match (status %d)' % s3))
                time.sleep(3)
    finally:
        b.close(); print('closed')
        if step in ('enroll', 'identify', 'both'): show('Finished')
