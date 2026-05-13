"""QQ chat record extraction from local databases."""

import os
import re
import json
import struct
import sqlite3
import tempfile
import shutil
from datetime import datetime


def extract_messages(account_dir, progress_callback=None):
    """Extract all messages from a QQ account's databases.

    QQ account_dir format: Tencent Files/<QQ_number>/
    """
    all_messages = []

    # Find QQ database files
    db_dir = os.path.join(account_dir, "db")
    if not os.path.isdir(db_dir):
        # Try alternate paths for QQNT
        alt_paths = [
            os.path.join(account_dir, "databases"),
        ]
        for p in alt_paths:
            if os.path.isdir(p):
                db_dir = p
                break

    if not os.path.isdir(db_dir):
        raise FileNotFoundError(f"QQ数据目录不存在: {db_dir}")

    uin = os.path.basename(account_dir)

    # Try to read Msg3.0.db (old QQ format)
    msg_db = os.path.join(db_dir, "Msg3.0.db")
    if os.path.exists(msg_db):
        msgs = _parse_msg30_db(msg_db, uin)
        all_messages.extend(msgs)

    # Try QQNT format (nt_msg.db or similar)
    for name in os.listdir(db_dir):
        if name.endswith('.db') and 'msg' in name.lower():
            db_path = os.path.join(db_dir, name)
            if db_path == msg_db:
                continue
            try:
                msgs = _parse_qqnt_db(db_path, uin)
                all_messages.extend(msgs)
            except Exception:
                pass

    # Read from buddy/group name mappings
    contacts = _read_qq_contacts(account_dir)

    # Replace UIN with nicknames where possible
    for msg in all_messages:
        sender = msg['sender']
        if sender in contacts:
            msg['sender'] = contacts[sender]

    all_messages.sort(key=lambda m: m['time'])
    return all_messages


def _parse_msg30_db(db_path, uin):
    """Parse old QQ Msg3.0.db format."""
    messages = []

    # Try to copy to temp (file might be locked)
    tmpdir = tempfile.mkdtemp(prefix="qq_")
    tmp_db = os.path.join(tmpdir, "msg.db")
    try:
        shutil.copy2(db_path, tmp_db)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return messages

    try:
        conn = sqlite3.connect(f'file:{tmp_db}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check for common table names
        tables_to_try = [
            'MsgContent', 'TB_IM_Message', 'message', 'messages',
            'chat_history', 'history',
        ]

        found = False
        for table in tables_to_try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if cur.fetchone():
                found = True
                try:
                    msgs = _parse_qq_generic(cur, table, uin)
                    messages.extend(msgs)
                except Exception:
                    pass

        if not found:
            # List all tables and try each
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            for table in tables:
                try:
                    msgs = _parse_qq_generic(cur, table, uin)
                    messages.extend(msgs)
                except Exception:
                    pass

        conn.close()
    except sqlite3.Error:
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return messages


def _parse_qqnt_db(db_path, uin):
    """Parse QQNT format database."""
    messages = []
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]

        for table in tables:
            try:
                msgs = _parse_qq_generic(cur, table, uin)
                messages.extend(msgs)
            except Exception:
                pass

        conn.close()
    except sqlite3.Error:
        pass

    return messages


def _parse_qq_generic(cur, table, uin):
    """Parse messages from a QQ database table by auto-detecting columns."""
    messages = []

    # Get column info
    cur.execute(f"PRAGMA table_info([{table}])")
    columns = {r[1].lower(): r[1] for r in cur.fetchall()}

    # Find time column
    time_col = None
    for candidate in ['time', 'msgtime', 'sendtime', 'createtime', 'timestamp', 'msg_time', 'm_uiMsgTime']:
        if candidate in columns:
            time_col = columns[candidate]
            break

    # Find content column
    content_col = None
    for candidate in ['msg', 'content', 'msgdata', 'message', 'text', 'msg_content', 'msgbody']:
        if candidate in columns:
            content_col = columns[candidate]
            break

    # Find sender column
    sender_col = None
    for candidate in ['senderuin', 'sender_uin', 'sender', 'sendername', 'from_uin', 'author', 'uin']:
        if candidate in columns:
            sender_col = columns[candidate]
            break

    if content_col is None:
        return messages

    # Build select
    select_cols = []
    if time_col:
        select_cols.append(time_col)
    if content_col:
        select_cols.append(content_col)
    if sender_col:
        select_cols.append(sender_col)

    if not select_cols:
        return messages

    select_sql = f"SELECT {', '.join(set(select_cols))} FROM [{table}]"
    try:
        cur.execute(select_sql)
    except sqlite3.Error:
        return messages

    for row in cur.fetchall():
        row_dict = dict(row)

        # Parse time
        time_val = None
        if time_col:
            for key in [time_col.lower()] if time_col.lower() in row_dict else []:
                pass
            raw = row_dict.get(time_col, row_dict.get(time_col.lower()))
            if raw:
                time_val = _parse_qq_time(raw)

        if time_val is None:
            time_val = datetime(2000, 1, 1)

        # Parse content
        content = ''
        raw_content = row_dict.get(content_col, row_dict.get(content_col.lower(), ''))
        if isinstance(raw_content, bytes):
            try:
                content = raw_content.decode('utf-8', errors='ignore')
            except Exception:
                content = str(raw_content)
        else:
            content = str(raw_content) if raw_content else ''

        if not content or len(content) < 1:
            continue

        # Parse sender
        sender = str(uin)
        if sender_col:
            raw_sender = row_dict.get(sender_col, row_dict.get(sender_col.lower()))
            if raw_sender:
                sender = str(raw_sender)

        # Detect type
        msg_type = 'text'
        if content.startswith('[图片]') or content.startswith('[Image]'):
            msg_type = 'image'
        elif content.startswith('[语音]') or content.startswith('[Audio]'):
            msg_type = 'voice'
        elif content.startswith('[视频]') or content.startswith('[Video]'):
            msg_type = 'video'
        elif content.startswith('[文件]') or content.startswith('[File]'):
            msg_type = 'file'

        messages.append({
            'sender': sender,
            'content': content,
            'time': time_val,
            'type': msg_type,
            'platform': 'QQ',
            'chat_name': '',
        })

    return messages


def _parse_qq_time(raw):
    """Parse QQ timestamp formats."""
    if raw is None:
        return None

    if isinstance(raw, (int, float)):
        if raw > 1e12:  # milliseconds
            return datetime.fromtimestamp(raw / 1000)
        elif raw > 1e9:  # seconds
            return datetime.fromtimestamp(raw)
        else:
            return datetime(2000, 1, 1)

    if isinstance(raw, str):
        # Try ISO format
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except Exception:
            pass
        # Try numeric string
        try:
            ts = int(raw)
            return _parse_qq_time(ts)
        except ValueError:
            pass

    return datetime(2000, 1, 1)


def _read_qq_contacts(account_dir):
    """Read QQ contact nicknames."""
    contacts = {}
    uin = os.path.basename(account_dir)

    # Try to read from buddy database
    buddy_paths = [
        os.path.join(account_dir, "db", "buddy.db"),
        os.path.join(account_dir, "db", "Buddy.db"),
    ]

    for bp in buddy_paths:
        if not os.path.exists(bp):
            continue
        try:
            conn = sqlite3.connect(f'file:{bp}?mode=ro', uri=True)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]

            for table in tables:
                try:
                    cur.execute(f"SELECT * FROM [{table}] LIMIT 1")
                    cols = [d[0] for d in cur.description]

                    uin_col = next((c for c in cols if 'uin' in c.lower()), None)
                    name_col = next((c for c in cols if 'name' in c.lower() or 'nick' in c.lower() or 'remark' in c.lower()), None)

                    if uin_col and name_col:
                        cur.execute(f"SELECT [{uin_col}], [{name_col}] FROM [{table}]")
                        for row in cur:
                            contacts[str(row[0])] = str(row[1])
                except Exception:
                    pass

            conn.close()
        except Exception:
            pass

    # Also check for remark/name files
    remark_paths = [
        os.path.join(account_dir, "remark"),
        os.path.join(account_dir, "Remark"),
    ]
    for rp in remark_paths:
        if os.path.isdir(rp):
            for f in os.listdir(rp):
                try:
                    fpath = os.path.join(rp, f)
                    with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                        name = fh.read().strip()
                    if name:
                        contacts[f] = name
                except Exception:
                    pass

    return contacts


def get_account_info(account_dir):
    """Get basic QQ account info."""
    info = {
        'uin': os.path.basename(account_dir),
        'nickname': os.path.basename(account_dir),
        'db_files': [],
    }

    db_dir = os.path.join(account_dir, "db")
    if os.path.isdir(db_dir):
        for f in sorted(os.listdir(db_dir)):
            if f.endswith('.db'):
                path = os.path.join(db_dir, f)
                info['db_files'].append({
                    'name': f,
                    'path': path,
                    'size': os.path.getsize(path),
                })

    return info
