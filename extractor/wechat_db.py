"""WeChat database decryption and message parsing — v3.x + v4.x.

v4.x (WeChat 4.0):
  Data path: Documents/xwechat_files/<wxid>/db_storage/
  Databases:  message/message_0.db, contact/contact.db
  Encryption: SQLCipher 4, PBKDF2-HMAC-SHA512 (256000 iter)
  Messages:   ZSTD-compressed content
  Media:      AES-ECB + XOR encrypted .dat files

v3.x (WeChat 3.x):
  Data path: Documents/WeChat Files/<wxid>/Msg/
  Databases:  MSG0.db, MSG1.db, ...
  Encryption: SQLCipher 3/4 standard PBKDF2-HMAC-SHA1
"""

import os
import re
import hashlib
import sqlite3
import shutil
import tempfile
from datetime import datetime

try:
    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import pyzstd
    HAS_ZSTD = True
except ImportError:
    HAS_ZSTD = False


# ── Public API ──────────────────────────────────────────

def extract_messages(account_dir, key_bytes, progress_callback=None, version='auto'):
    """Auto-detect version and extract messages."""
    if version == '4.x' or _is_v4(account_dir):
        return _extract_v4(account_dir, key_bytes, progress_callback)
    return _extract_v3(account_dir, key_bytes, progress_callback)


def get_account_info(account_dir):
    """Get account info for v3.x or v4.x."""
    info = {
        'wxid': os.path.basename(account_dir),
        'msg_dbs': [], 'db_count': 0, 'version': 'unknown',
    }

    # v4 path
    db_dir = os.path.join(account_dir, "db_storage", "message")
    if os.path.isdir(db_dir):
        info['version'] = '4.x'
        for f in sorted(os.listdir(db_dir)):
            if f.startswith('message_') and f.endswith('.db'):
                fp = os.path.join(db_dir, f)
                info['msg_dbs'].append({
                    'name': f, 'path': fp,
                    'size': os.path.getsize(fp),
                })
        info['db_count'] = len(info['msg_dbs'])
        return info

    # v3 path
    msg_dir = os.path.join(account_dir, "Msg")
    if os.path.isdir(msg_dir):
        info['version'] = '3.x'
        for f in sorted(os.listdir(msg_dir)):
            if re.match(r'^MSG\d*\.db$', f):
                fp = os.path.join(msg_dir, f)
                info['msg_dbs'].append({
                    'name': f, 'path': fp,
                    'size': os.path.getsize(fp),
                })
        info['db_count'] = len(info['msg_dbs'])

    return info


# ── WeChat 4.x extraction ───────────────────────────────

def _is_v4(path):
    return os.path.isdir(os.path.join(path, "db_storage"))


def _extract_v4(account_path, key_bytes, progress_callback=None):
    """Extract from WeChat 4.0 databases."""
    db_storage = os.path.join(account_path, "db_storage")
    msg_dir = os.path.join(db_storage, "message")

    if not os.path.isdir(msg_dir):
        raise FileNotFoundError(
            "Database not found: " + msg_dir + "\n"
            "Please use WeChat phone migration first:\n"
            "Phone WeChat > Settings > Chat > Chat Migration > Migrate to PC"
        )

    msg_dbs = sorted(
        [f for f in os.listdir(msg_dir) if re.match(r'^message_\d+\.db$', f)],
        key=lambda x: int(re.search(r'\d+', x).group())
    )

    if not msg_dbs:
        raise FileNotFoundError(f"未找到 message_*.db: {msg_dir}")

    contacts = _load_contacts_v4(db_storage, key_bytes)
    tmpdir = tempfile.mkdtemp(prefix="wx4_")
    all_msgs = []
    total = len(msg_dbs)

    try:
        for idx, fname in enumerate(msg_dbs):
            src = os.path.join(msg_dir, fname)
            dst = os.path.join(tmpdir, fname)
            if progress_callback:
                progress_callback(idx, total, f"解密 {fname}...")
            _decrypt_v4(src, dst, key_bytes)
            if progress_callback:
                progress_callback(idx, total, f"解析 {fname}...")
            all_msgs.extend(_parse_v4(dst, contacts))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if progress_callback:
        progress_callback(total, total, "完成")
    all_msgs.sort(key=lambda m: m['time'])
    return all_msgs


def _decrypt_v4(src, dst, key_bytes):
    """Decrypt SQLCipher 4 database (WeChat 4.0).

    Page layout: [encrypted data][IV:16][HMAC-SHA512:64]
    Page 1 has 16-byte salt prefix before encrypted data.
    PBKDF2-HMAC-SHA512, 256000 iterations.
    """
    with open(src, 'rb') as f:
        data = f.read()

    page_size = 4096
    salt = data[:16]
    dk = hashlib.pbkdf2_hmac('sha512', key_bytes, salt, 256000, dklen=64)
    aes_key = dk[:32]

    num_pages = len(data) // page_size
    plain = bytearray()

    for pg in range(num_pages):
        start = pg * page_size
        page = data[start:start + page_size]
        if len(page) < page_size:
            page += b'\x00' * (page_size - len(page))

        if pg == 0:
            continue  # Reserved page

        iv_off = page_size - 16 - 64
        iv = page[iv_off:iv_off + 16]
        ct_start = 16 if pg == 1 else 0
        ct = page[ct_start:iv_off]

        if len(ct) < 16:
            continue

        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        pt = cipher.decrypt(ct)
        pad = pt[-1]
        if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
            pt = pt[:-pad]
        plain.extend(pt)

    if len(plain) < 100:
        raise ValueError("解密数据太小，密钥不正确")

    with open(dst, 'wb') as f:
        f.write(plain)


def _parse_v4(db_path, contacts):
    """Parse WeChat 4.0 message database."""
    msgs = []
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]

        if 'msg_content' in tables:
            try:
                cur.execute(
                    "SELECT create_time, talker_id, message_content, is_sender "
                    "FROM msg_content ORDER BY create_time"
                )
                for row in cur.fetchall():
                    ts = _ts(int(row[0]) if row[0] else 0)
                    talker = str(row[1] or '')
                    raw = row[2]
                    content = _zstd_decompress(raw) if isinstance(raw, bytes) else str(raw or '')
                    if not content:
                        continue
                    is_self = int(row[3] or 0)
                    sender = contacts.get(talker, talker) if talker else '未知'
                    msgs.append(_make_msg(
                        '我' if is_self else sender, content, ts,
                        _msg_type(content), '微信', talker
                    ))
            except sqlite3.Error:
                pass
        else:
            for table in tables:
                try:
                    msgs.extend(_parse_generic(cur, table, contacts))
                    break
                except Exception:
                    continue

        conn.close()
    except sqlite3.Error:
        pass
    return msgs


def _load_contacts_v4(db_storage, key_bytes):
    """Load contacts from WeChat 4.0 contact.db."""
    contacts = {}
    contact_db = os.path.join(db_storage, "contact", "contact.db")
    if not os.path.exists(contact_db):
        return contacts

    tmpdir = tempfile.mkdtemp(prefix="wxc_")
    tmp = os.path.join(tmpdir, "c.db")
    try:
        _decrypt_v4(contact_db, tmp, key_bytes)
        conn = sqlite3.connect(f'file:{tmp}?mode=ro', uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for table in [r[0] for r in cur.fetchall()]:
            try:
                cur.execute(f"SELECT * FROM [{table}] LIMIT 1")
                cols = [d[0] for d in cur.description]
                idc = next((c for c in cols if 'id' in c.lower() or 'username' in c.lower() or 'user' in c.lower()), None)
                nc = next((c for c in cols if 'name' in c.lower() or 'nick' in c.lower() or 'remark' in c.lower()), None)
                if idc and nc:
                    cur.execute(f"SELECT [{idc}], [{nc}] FROM [{table}]")
                    for r in cur:
                        contacts[str(r[0])] = str(r[1])
                    break
            except Exception:
                continue
        conn.close()
    except Exception:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return contacts


def _zstd_decompress(data):
    if not data:
        return ''
    if HAS_ZSTD:
        try:
            return pyzstd.decompress(data).decode('utf-8', errors='replace')
        except Exception:
            pass
    try:
        return data.decode('utf-8', errors='replace')
    except Exception:
        return str(data)


# ── WeChat 3.x extraction ───────────────────────────────

def _extract_v3(account_dir, key_bytes, progress_callback=None):
    """Extract from WeChat 3.x databases."""
    msg_dir = os.path.join(account_dir, "Msg")
    if not os.path.isdir(msg_dir):
        raise FileNotFoundError(f"消息目录不存在: {msg_dir}")

    db_files = sorted(
        [f for f in os.listdir(msg_dir) if re.match(r'^MSG\d*\.db$', f)],
        key=lambda x: int(re.search(r'\d*', x).group() or 0)
    )
    if not db_files:
        raise FileNotFoundError(f"未找到 MSG*.db: {msg_dir}")

    tmpdir = tempfile.mkdtemp(prefix="wx3_")
    all_msgs = []
    total = len(db_files)

    try:
        for idx, fname in enumerate(db_files):
            src = os.path.join(msg_dir, fname)
            dst = os.path.join(tmpdir, fname)
            if progress_callback:
                progress_callback(idx, total, f"解密 {fname}...")
            _decrypt_v3(src, dst, key_bytes)
            if progress_callback:
                progress_callback(idx, total, f"解析 {fname}...")
            all_msgs.extend(_parse_v3(dst))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if progress_callback:
        progress_callback(total, total, "完成")
    all_msgs.sort(key=lambda m: m['time'])
    return all_msgs


def _decrypt_v3(src, dst, key_bytes):
    """Decrypt SQLCipher 3/4 database (WeChat 3.x)."""
    with open(src, 'rb') as f:
        data = f.read()

    salt = data[:16]
    page_size = 4096

    for iterations in [4000, 64000]:
        dk = PBKDF2(key_bytes, salt, dkLen=48, count=iterations,
                    hmac_hash_module=hashlib.sha1)
        aes_key = dk[:32]
        plain = bytearray()
        offset = 16

        while offset + 16 <= len(data):
            iv = data[offset:offset + 16]
            ct_end = min(offset + 16 + page_size - 16, len(data))
            ct = data[offset + 16:ct_end]
            if len(ct) < 16:
                break
            cipher = AES.new(aes_key, AES.MODE_CBC, iv)
            pt = cipher.decrypt(ct)
            pad = pt[-1]
            if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
                pt = pt[:-pad]
            plain.extend(pt)
            offset = ct_end

        if len(plain) >= 100 and plain[:5] == b'SQLite':
            break

    if len(plain) < 100 or plain[:5] != b'SQLite':
        raise ValueError("解密失败，密钥不正确")

    with open(dst, 'wb') as f:
        f.write(plain)


def _parse_v3(db_path):
    """Parse WeChat 3.x MSG table."""
    msgs = []
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        cur = conn.cursor()
        cur.execute("SELECT CreateTime, StrTalker, StrContent, Type, IsSender FROM MSG ORDER BY CreateTime")
        tmap = {1: 'text', 3: 'image', 34: 'voice', 43: 'video',
                47: 'emoji', 49: 'link', 10000: 'system'}
        for row in cur.fetchall():
            ts = _ts(int(row[0]) if row[0] else 0)
            talker = str(row[1] or '')
            content = str(row[2] or '')
            if not content:
                continue
            tp = tmap.get(int(row[3] or 0), 'text')
            is_self = int(row[4] or 0) if len(row) > 4 else 0
            msgs.append(_make_msg(
                '我' if is_self else talker, content, ts, tp, '微信', talker
            ))
        conn.close()
    except sqlite3.Error:
        pass
    return msgs


def _parse_generic(cur, table, contacts):
    """Fallback parser for unknown table schemas."""
    msgs = []
    try:
        cur.execute(f"SELECT * FROM [{table}]")
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            ts = None
            for k in ['CreateTime', 'create_time', 'time', 'sort_seq']:
                if k in d and d[k]:
                    ts = _ts(int(d[k]))
                    break
            if ts is None:
                ts = datetime(2000, 1, 1)
            content = ''
            for k in ['StrContent', 'content', 'message_content']:
                if k in d and d[k]:
                    v = d[k]
                    content = _zstd_decompress(v) if isinstance(v, bytes) else str(v)
                    break
            if not content:
                continue
            sender = '未知'
            for k in ['StrTalker', 'talker_id', 'talker']:
                if k in d and d[k]:
                    sender = str(d[k])
                    break
            msgs.append(_make_msg(sender, content, ts, 'text', '微信', ''))
    except sqlite3.Error:
        pass
    return msgs


# ── Helpers ──────────────────────────────────────────────

def _make_msg(sender, content, ts, msg_type, platform, chat_name):
    return {
        'sender': sender,
        'content': content,
        'time': ts,
        'type': msg_type,
        'platform': platform,
        'chat_name': chat_name,
    }


def _ts(val):
    if val > 1e12:
        return datetime.fromtimestamp(val / 1000)
    if val > 1e8:
        return datetime.fromtimestamp(val)
    return datetime(2000, 1, 1)


def _msg_type(content):
    for tag, tp in [('[图片]', 'image'), ('[语音]', 'voice'),
                     ('[视频]', 'video'), ('[文件]', 'file'),
                     ('[表情]', 'emoji'), ('[链接]', 'link'),
                     ('[红包]', 'redpacket'), ('<sysmsg', 'system')]:
        if tag in content:
            return tp
    return 'text'
