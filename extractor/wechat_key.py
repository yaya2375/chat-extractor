"""WeChat database key extraction — v3.x and v4.x.

Scans process memory for SQLCipher PRAGMA key patterns: x'<64 hex chars>'
"""

import os
import re
import hashlib
from collections import Counter

try:
    import pymem
    import pymem.process
    HAS_PYMEM = True
except ImportError:
    HAS_PYMEM = False

try:
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# Process names to try (Weixin.exe = WeChat 4.x main, WeChat.exe = 3.x)
_PROC_NAMES = [
    'Weixin.exe',          # WeChat 4.x main process (contains Weixin.dll 170MB)
    'WeChatAppEx.exe',     # Fallback for some 4.x variants
    'WeChat.exe',          # WeChat 3.x
]

# Priority modules to scan (largest/most likely to contain key)
_PRIORITY_MODULES = [
    'Weixin.dll',
    'WeChatWin.dll',
    'wechat.dll',
]

# DLL modules of interest (v4.x uses Weixin.dll, v3.x uses WeChatWin.dll)
_DLL_NAMES = [
    'Weixin.dll',
    'weixin.dll',
    'WeChatWin.dll',
    'wechatwin.dll',
    'WeChatAppEx.exe',
    'wechat.dll',
    'wmpf_host_export.dll',
    'radium.dll',
]


def extract_key_from_memory():
    """Extract WeChat database encryption key from process memory.

    Scans running WeChat process(es) for x'<64hex>' patterns.
    Returns (key_bytes, method) or (None, error_message).
    """
    if not HAS_PYMEM:
        return None, "pymem not installed"

    pm = _attach_wechat_process()
    if pm is None:
        return None, "No WeChat process found (tried Weixin/WeChatAppEx/WeChat). Is WeChat running?"

    try:
        candidates = Counter()

        # 1. Scan priority modules (Weixin.dll, WeChatWin.dll) — read FULL module
        all_modules = list(pm.list_modules())
        priority = [m for m in all_modules
                    if any(p.lower() in (m.name or '').lower()
                          for p in _PRIORITY_MODULES)]
        other = [m for m in all_modules if m not in priority]

        for module in priority + other:
            size = module.SizeOfImage
            if size < 1024 * 1024 or size > 500 * 1024 * 1024:
                continue
            try:
                # Read full module for priority DLLs, first 50MB for others
                read_size = min(size, size if module in priority else 50 * 1024 * 1024)
                chunk = pm.read_bytes(module.lpBaseOfDll, read_size)
                # Method A: x'<hex>' pattern
                keys = _scan_for_key_patterns(chunk)
                # Method B: raw 32-byte keys near PRAGMA/cipher strings
                if not keys and module in priority:
                    keys = _scan_near_sqlite_strings(chunk)
                if keys:
                    candidates.update(keys)
            except Exception:
                continue

        # 2. If no key, scan from process base in chunks using raw scan
        if not candidates:
            try:
                pb = pm.process_base
                base = pb.lpBaseOfDll
                for offset in range(0, 500 * 1024 * 1024, 50 * 1024 * 1024):
                    try:
                        chunk = pm.read_bytes(base + offset, min(50 * 1024 * 1024, 500 * 1024 * 1024 - offset))
                        keys = _scan_for_key_patterns(chunk)
                        candidates.update(keys)
                    except Exception:
                        break
            except Exception:
                pass

        pm.close_process()

        if not candidates:
            return None, "Memory scan found no key patterns (x'<64hex>'). Try manual key input."

        # Return the most frequent candidate (real key appears multiple times)
        key_hex, freq = candidates.most_common(1)[0]
        key_bytes = bytes.fromhex(key_hex)
        return key_bytes, f"Memory scan (x'...' pattern, freq={freq})"

    except Exception as e:
        try:
            pm.close_process()
        except Exception:
            pass
        return None, f"Memory scan error: {e}"


def _attach_wechat_process():
    """Try to attach to a running WeChat process."""
    for name in _PROC_NAMES:
        try:
            return pymem.Pymem(name)
        except Exception:
            continue
    return None


def _scan_for_key_patterns(data):
    """Scan memory for x'<64 hex chars>' SQLCipher PRAGMA key patterns."""
    candidates = []
    pos = 0
    pattern = re.compile(rb"x'([0-9a-fA-F]{64})'")

    while True:
        m = pattern.search(data, pos)
        if not m:
            break
        hex_key = m.group(1).decode('ascii').lower()
        key_bytes = bytes.fromhex(hex_key)
        if _is_plausible_key(key_bytes):
            candidates.append(hex_key)
        pos = m.end()

    return candidates


def _scan_near_sqlite_strings(data):
    """Find raw 32-byte keys near SQLite/PRAGMA/cipher strings.

    When the x'...' wrapper isn't present, the raw key might be stored
    as 32 bytes near SQLCipher-related strings in memory.
    """
    candidates = []
    markers = [b'PRAGMA', b'cipher', b'Cipher', b'WCDB', b'wcdb',
               b'sqlite3_key', b'setCipherKey']

    for marker in markers:
        pos = 0
        while True:
            idx = data.find(marker, pos)
            if idx == -1:
                break

            # Scan 1KB around the marker for raw 32-byte keys
            start = max(0, idx - 1024)
            end = min(len(data), idx + len(marker) + 1024)
            region = data[start:end]

            for i in range(0, len(region) - 32, 4):
                candidate = region[i:i + 32]
                if _is_plausible_key(candidate):
                    hex_key = candidate.hex()
                    candidates.append(hex_key)

            pos = idx + len(marker)

    return candidates


def _is_plausible_key(data):
    """Filter out implausible keys."""
    if len(data) != 32:
        return False

    # Reject all zeros / all FFs
    if data == b'\x00' * 32 or data == b'\xff' * 32:
        return False

    # Count unique bytes
    unique = len(set(data))
    if unique <= 2:
        return False

    # Reject repeating patterns
    if data[:4] * 8 == data or data[:8] * 4 == data:
        return False

    # Reject keys with too many printable ASCII bytes (real keys are mostly non-printable)
    ascii_count = sum(1 for b in data if 0x20 <= b <= 0x7e)
    if ascii_count > 8:
        return False

    # Reject keys with too many zero bytes
    zero_count = sum(1 for b in data if b == 0)
    if zero_count > 8:
        return False

    # Require high entropy: at least 28 unique byte values out of 32
    if unique < 28:
        return False

    return True


# ── Key verification ────────────────────────────────────

def test_key_on_db(key_bytes, db_path, is_v4=True):
    """Test if a key successfully decrypts a database."""
    if not os.path.exists(db_path) or not HAS_CRYPTO:
        return True

    try:
        with open(db_path, 'rb') as f:
            header = f.read(4096)
    except Exception:
        return False

    if len(header) < 16:
        return False

    salt = header[:16]

    # Try SQLCipher 4 (WeChat 4.x): PBKDF2-HMAC-SHA512, 256000 iter
    if is_v4:
        try:
            dk = hashlib.pbkdf2_hmac('sha512', key_bytes, salt, 256000, dklen=64)
            aes_key = dk[:32]
            page_size = 4096
            iv_off = page_size - 16 - 64
            if iv_off >= 16 and len(header) >= page_size:
                iv = header[iv_off:iv_off + 16]
                ct = header[16:iv_off]
                if len(ct) >= 16:
                    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
                    pt = cipher.decrypt(ct)
                    if pt[:5] == b'SQLite':
                        return True
        except Exception:
            pass

    # Try SQLCipher 3/4 (WeChat 3.x): PBKDF2-HMAC-SHA1
    for iterations in [4000, 64000]:
        try:
            dk = PBKDF2(key_bytes, salt, dkLen=48, count=iterations,
                       hmac_hash_module=hashlib.sha1)
            iv = header[16:32] if len(header) >= 32 else b'\x00' * 16
            ct_start = 32 if len(header) >= 32 else 16
            ct = header[ct_start:]
            if len(ct) < 16:
                continue
            cipher = AES.new(dk[:32], AES.MODE_CBC, iv)
            pt = cipher.decrypt(ct[:len(ct) - len(ct) % 16])
            if pt[:5] == b'SQLite':
                return True
        except Exception:
            pass

    return False


# ── Utility ─────────────────────────────────────────────

def format_key(key_bytes):
    if key_bytes is None:
        return ""
    return key_bytes.hex()


def parse_key_input(user_input):
    """Parse user-provided key: hex string, optionally wrapped with x'...'."""
    s = user_input.strip().replace(" ", "")
    if s.startswith("x'") and s.endswith("'"):
        s = s[2:-1]
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    if re.match(r'^[0-9a-fA-F]{64}$', s):
        return bytes.fromhex(s)
    if re.match(r'^[0-9a-fA-F]{32}$', s):
        return bytes.fromhex(s)
    return None
