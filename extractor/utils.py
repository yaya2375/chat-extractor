"""Path discovery utilities for WeChat (3.x, 4.x) and QQ on Windows."""

import os
import re
import glob
import winreg
import subprocess


# ═══════════════════════════════════════════════════════════
#  WeChat discovery
# ═══════════════════════════════════════════════════════════

def find_all_wechat_paths():
    """Deep scan for ALL WeChat data paths.

    Returns a list of dicts, each describing a found account.
    """
    results = []

    # 1) Documents-based paths (3.x + 4.x migrated data)
    for doc_base in _get_document_paths():
        # 4.x: xwechat_files/<wxid>/db_storage/
        v4_path = os.path.join(doc_base, 'xwechat_files')
        if os.path.isdir(v4_path):
            for name in os.listdir(v4_path):
                full = os.path.join(v4_path, name)
                if os.path.isdir(full) and name.startswith('wxid_'):
                    results.append(_build_entry(full, '4.x_migrated', doc_base))

        # 3.x: WeChat Files/<wxid>/Msg/
        for folder_name in ['WeChat Files', 'WeChat']:
            v3_path = os.path.join(doc_base, folder_name)
            if not os.path.isdir(v3_path):
                continue
            for name in os.listdir(v3_path):
                full = os.path.join(v3_path, name)
                if os.path.isdir(full) and (name.startswith('wxid_')
                   or os.path.isdir(os.path.join(full, 'Msg'))):
                    results.append(_build_entry(full, '3.x', doc_base))

    # 2) AppData roam — WeChat 4.x running data (roam databases)
    roam_base = os.path.expanduser(
        r'~\AppData\Roaming\Tencent\xwechat\roam\ilink_im')
    if os.path.isdir(roam_base):
        for app_dir in os.listdir(roam_base):
            app_path = os.path.join(roam_base, app_dir)
            if not os.path.isdir(app_path) or not app_dir.startswith('app_'):
                continue
            # Find wxid from login dir
            wxid = _find_wxid_from_xwechat()
            if not wxid:
                wxid = 'unknown'
            # Find databases under im2_*/database/
            im2_base = os.path.join(app_path)
            for im2_name in os.listdir(im2_base):
                im2_path = os.path.join(im2_base, im2_name)
                db_path = os.path.join(im2_path, 'database')
                if os.path.isdir(db_path):
                    results.append(_build_entry(
                        im2_path, '4.x_roam',
                        os.path.dirname(roam_base),
                        wxid=wxid))

    # 3) AppData config — login dir only (key material)
    xwc_base = os.path.expanduser(r'~\AppData\Roaming\Tencent\xwechat')
    login_dir = os.path.join(xwc_base, 'login')
    if os.path.isdir(login_dir):
        for name in os.listdir(login_dir):
            full = os.path.join(login_dir, name)
            if os.path.isdir(full) and name.startswith('wxid_'):
                if not any(r.get('wxid') == name for r in results):
                    results.append(_build_entry(full, '4.x_app',
                                                xwc_base, wxid=name))

    return results


def _get_document_paths():
    """Get all possible 'Documents' paths including OneDrive redirects."""
    paths = []
    home = os.path.expanduser('~')
    for cand in ['Documents', 'OneDrive\\文档', 'OneDrive\\Documents',
                 'My Documents']:
        p = os.path.join(home, cand)
        if os.path.isdir(p):
            paths.append(p)
    # Also try common roots
    for drive in ['C:', 'D:']:
        p = os.path.join(drive, 'Users', os.environ.get('USERNAME', ''),
                         'Documents')
        if os.path.isdir(p) and p not in paths:
            paths.append(p)
    return paths


def _find_wxid_from_xwechat():
    """Extract wxid from xwechat login directory."""
    login = os.path.expanduser(
        r'~\AppData\Roaming\Tencent\xwechat\login')
    if os.path.isdir(login):
        for name in os.listdir(login):
            if name.startswith('wxid_'):
                return name
    return None


def _build_entry(path, dtype, base_dir, wxid=None):
    """Build a standard account entry dict."""
    if not wxid:
        wxid = os.path.basename(path)
        if not wxid.startswith('wxid_'):
            wxid = os.path.basename(os.path.dirname(path))
    entry = {
        'path': path,
        'wxid': wxid,
        'type': dtype,
        'base_dir': base_dir,
    }

    # Count actual database files
    dbs = _list_wechat_dbs(path, dtype)
    entry['dbs'] = dbs
    entry['db_count'] = len(dbs)
    entry['ready'] = len(dbs) > 0

    return entry


def _list_wechat_dbs(path, dtype):
    """List decryptable database files in a WeChat data directory."""
    dbs = []

    def _add(filepath, label):
        if os.path.isfile(filepath):
            dbs.append({
                'path': filepath,
                'name': os.path.basename(filepath),
                'label': label,
                'size': os.path.getsize(filepath),
            })

    if 'migrated' in dtype:
        # xwechat_files/<wxid>/db_storage/
        msg_dir = os.path.join(path, 'db_storage', 'message')
        if os.path.isdir(msg_dir):
            for f in sorted(os.listdir(msg_dir)):
                if re.match(r'^message_\d+\.db$', f):
                    _add(os.path.join(msg_dir, f), 'message')
        contact = os.path.join(path, 'db_storage', 'contact', 'contact.db')
        _add(contact, 'contact')

    elif 'roam' in dtype:
        # xwechat/roam/.../im2_xxx/database/
        db_dir = os.path.join(path, 'database')
        if os.path.isdir(db_dir):
            for f in os.listdir(db_dir):
                if f.endswith('.db'):
                    _add(os.path.join(db_dir, f), 'roam')
            # kb.bin — key bundle
            kb = os.path.join(db_dir, 'kb.bin')
            if os.path.isfile(kb):
                dbs.append({
                    'path': kb,
                    'name': 'kb.bin',
                    'label': 'key_bundle',
                    'size': os.path.getsize(kb),
                })

    else:
        # 3.x: WeChat Files/<wxid>/Msg/
        msg_dir = os.path.join(path, 'Msg')
        # Main Msg/*.db
        if os.path.isdir(msg_dir):
            for f in sorted(os.listdir(msg_dir)):
                fp = os.path.join(msg_dir, f)
                if os.path.isfile(fp) and f.endswith('.db'):
                    _add(fp, 'db')
            # Msg/Multi/MSG*.db
            multi = os.path.join(msg_dir, 'Multi')
            if os.path.isdir(multi):
                for f in sorted(os.listdir(multi)):
                    fp = os.path.join(multi, f)
                    if os.path.isfile(fp) and f.endswith('.db'):
                        _add(fp, 'msg_db')

    return dbs


def find_wechat_data_dirs():
    """Backward-compatible wrapper, returns (version, base_dir, [paths])."""
    all_found = find_all_wechat_paths()
    if not all_found:
        return None, None, []

    # Determine primary version
    types = {e['type'] for e in all_found}
    primary = '4.x' if any('4.x' in t for t in types) else '3.x'
    base = all_found[0]['base_dir']
    paths = [e['path'] for e in all_found]

    return primary, base, paths


# ═══════════════════════════════════════════════════════════
#  WeChat executable
# ═══════════════════════════════════════════════════════════

_PROC_NAMES = ['Weixin.exe', 'WeChatAppEx.exe', 'WeChat.exe']


def find_wechat_exe():
    """Find WeChat executable. Returns (version_label, path) or (None, None)."""
    for name in _PROC_NAMES:
        path = _find_exe_from_process(name)
        if path:
            ver = '4.x' if name in ('Weixin.exe', 'WeChatAppEx.exe') else '3.x'
            return ver, path

    # Fallback: filesystem search
    for name in _PROC_NAMES:
        for base in [
            os.path.expanduser(r'~\AppData\Roaming\Tencent'),
            os.path.expanduser(r'~\AppData\Local\Tencent'),
            r'C:\Program Files\Tencent',
            r'C:\Program Files (x86)\Tencent',
        ]:
            for root, dirs, files in os.walk(base):
                if root[len(base):].count(os.sep) > 5:
                    dirs.clear()
                    continue
                if name in files:
                    ver = '4.x' if name in ('Weixin.exe', 'WeChatAppEx.exe') else '3.x'
                    return ver, os.path.join(root, name)

    return None, None


def _find_exe_from_process(name):
    """Get full path of a running process."""
    try:
        base = name.replace('.exe', '')
        cmd = (
            f"(Get-Process -Name '{base}' -ErrorAction SilentlyContinue "
            f"| Select-Object -First 1).MainModule.FileName"
        )
        out = subprocess.check_output(
            ['powershell', '-Command', cmd],
            timeout=10, encoding='utf-8', errors='ignore')
        path = out.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
#  QQ discovery
# ═══════════════════════════════════════════════════════════

def find_qq_data_dirs():
    """Find all QQ account data directories."""
    candidates = [
        os.path.expanduser("~/Documents/Tencent Files"),
        "C:/Tencent Files", "D:/Tencent Files",
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        accounts = []
        for name in os.listdir(base):
            full = os.path.join(base, name)
            if os.path.isdir(full) and re.match(r'^\d{5,15}$', name):
                accounts.append(full)
        if accounts:
            return base, accounts
    return None, []


def find_qq_exe():
    """Find QQ executable. Returns (version_label, path) or (None, None)."""
    for proc_name in ['QQ.exe', 'QQNT.exe']:
        path = _find_exe_from_process(proc_name)
        if path:
            return 'NT' if 'QQNT' in path else 'classic', path

    search_paths = [
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
        r"C:\Program Files\Tencent\QQ\Bin\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQ\Bin\QQ.exe",
    ]
    for p in search_paths:
        if os.path.exists(p):
            return 'NT' if 'QQNT' in p else 'classic', p

    return None, None


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def get_wxid_from_path(account_dir):
    """Extract wxid from any account directory."""
    name = os.path.basename(account_dir)
    if name.startswith("wxid_"):
        return name
    parent = os.path.basename(os.path.dirname(account_dir))
    if parent.startswith("wxid_"):
        return parent
    # Try to find in login dir
    return _find_wxid_from_xwechat() or name


def get_qq_uin_from_path(account_dir):
    """Extract QQ number from account directory."""
    return os.path.basename(account_dir)


def get_wechat_version_info(base_dir, version):
    """Get WeChat version-specific info."""
    info = {'version': version, 'base_dir': base_dir}
    if version and '4.x' in version:
        info['data_type'] = 'cloud_primary'
    else:
        info['data_type'] = 'local_encrypted'
    return info
