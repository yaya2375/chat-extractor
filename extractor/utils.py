"""Path discovery utilities for WeChat (3.x and 4.x) and QQ on Windows."""
import os
import re
import glob
import winreg


# ── WeChat discovery ──────────────────────────────────────

def find_wechat_data_dirs():
    """Find all WeChat account data directories (v3.x + v4.x).

    Returns (version, base_dir, [account_dirs]) or (None, None, []).
    """
    accounts = []

    # 1. WeChat 4.x chat data: Documents/xwechat_files/<wxid>/db_storage/
    v4_data_base = os.path.expanduser(r"~\Documents\xwechat_files")
    if os.path.isdir(v4_data_base):
        for name in os.listdir(v4_data_base):
            full = os.path.join(v4_data_base, name)
            db_storage = os.path.join(full, "db_storage")
            if os.path.isdir(full) and name.startswith("wxid_"):
                accounts.append(('4.x', full))

    # 2. WeChat 4.x program directory (for key extraction, no DB here)
    v4_app_base = os.path.expanduser(r"~\AppData\Roaming\Tencent\xwechat")
    login_dir = os.path.join(v4_app_base, "login")
    if os.path.isdir(login_dir):
        for name in os.listdir(login_dir):
            full = os.path.join(login_dir, name)
            if os.path.isdir(full) and name.startswith("wxid_"):
                # Only add if not already found in xwechat_files
                if not any(a[1] == full for a in accounts):
                    accounts.append(('4.x_app', full))

    # 3. WeChat 3.x: Documents/WeChat Files/<wxid>/Msg/
    for base in [
        os.path.expanduser("~/Documents/WeChat Files"),
        os.path.expanduser("~/Documents/WeChat"),
        "C:/WeChat Files", "D:/WeChat Files",
    ]:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            full = os.path.join(base, name)
            msg_dir = os.path.join(full, "Msg")
            if os.path.isdir(full) and (
                name.startswith("wxid_") or os.path.isdir(msg_dir)
            ):
                accounts.append(('3.x', full))

    if not accounts:
        return None, None, []

    # Determine primary version
    versions = set(a[0] for a in accounts)
    primary = '4.x' if any(v.startswith('4.x') for v in versions) else '3.x'

    # Merge: group by version, return best base
    if primary.startswith('4.x'):
        # Prefer xwechat_files as base (has actual databases)
        v4_entries = [a for a in accounts if a[0] == '4.x']
        v4app_entries = [a for a in accounts if a[0] == '4.x_app']
        all_v4 = v4_entries + v4app_entries
        if all_v4:
            return '4.x', v4_data_base if v4_entries else v4_app_base, [a[1] for a in all_v4]
    else:
        return '3.x', os.path.dirname(accounts[0][1]) if accounts else None, [a[1] for a in accounts]

    return None, None, []


def _find_xwechat_accounts(base_dir):
    """Find WeChat 4.x accounts from xwechat directory."""
    accounts = []
    login_dir = os.path.join(base_dir, "login")
    if not os.path.isdir(login_dir):
        return accounts
    for name in os.listdir(login_dir):
        full = os.path.join(login_dir, name)
        if os.path.isdir(full) and name.startswith("wxid_"):
            accounts.append(full)
    # Also check radium/users for account hashes
    users_dir = os.path.join(base_dir, "radium", "users")
    if os.path.isdir(users_dir):
        for name in os.listdir(users_dir):
            full = os.path.join(users_dir, name)
            if os.path.isdir(full) and len(name) == 32:
                # This is a hash-based user dir
                pass  # Handled via login dir above
    return accounts


def find_wechat_exe():
    """Find WeChat executable (3.x: WeChat.exe, 4.x: WeChatAppEx.exe)."""
    # Check running processes first
    import subprocess
    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', 'IMAGENAME eq WeChatAppEx.exe', '/FO', 'CSV', '/NH'],
            timeout=5, encoding='utf-8', errors='ignore')
        if 'WeChatAppEx.exe' in out:
            # WeChat 4.x is running, find the exe path
            path = _find_exe_from_process('WeChatAppEx.exe')
            if path:
                return '4.x', path
            # Fallback: search for it
            for exe_path in _search_exe('WeChatAppEx.exe'):
                return '4.x', exe_path
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', 'IMAGENAME eq WeChat.exe', '/FO', 'CSV', '/NH'],
            timeout=5, encoding='utf-8', errors='ignore')
        if 'WeChat.exe' in out:
            path = _find_exe_from_process('WeChat.exe')
            if path:
                return '3.x', path
    except Exception:
        pass

    # Search filesystem
    for exe_name in ['WeChatAppEx.exe', 'WeChat.exe']:
        result = _search_exe(exe_name)
        if result:
            ver = '4.x' if 'WeChatAppEx' in exe_name else '3.x'
            return ver, result

    return None, None


def _find_exe_from_process(name):
    """Get full path of a running process using PowerShell."""
    import subprocess
    try:
        cmd = f"(Get-Process -Name '{name.replace('.exe','')}' -ErrorAction SilentlyContinue | Select-Object -First 1).MainModule.FileName"
        out = subprocess.check_output(
            ['powershell', '-Command', cmd],
            timeout=10, encoding='utf-8', errors='ignore')
        path = out.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


def _search_exe(name):
    """Search common locations for an executable."""
    dirs_to_search = [
        os.path.expanduser(r"~\AppData\Roaming\Tencent"),
        os.path.expanduser(r"~\AppData\Local\Tencent"),
        r"C:\Program Files\Tencent",
        r"C:\Program Files (x86)\Tencent",
    ]
    for base in dirs_to_search:
        for root, dirnames, filenames in os.walk(base):
            # Limit depth
            depth = root[len(base):].count(os.sep)
            if depth > 5:
                dirnames.clear()
                continue
            if name in filenames:
                return os.path.join(root, name)
    return None


# ── QQ discovery ──────────────────────────────────────────

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
    """Find QQ executable path."""
    import subprocess
    # Check process first
    for proc_name in ['QQ.exe', 'QQNT.exe']:
        try:
            out = subprocess.check_output(
                ['tasklist', '/FI', f'IMAGENAME eq {proc_name}', '/FO', 'CSV', '/NH'],
                timeout=5, encoding='utf-8', errors='ignore')
            if proc_name in out:
                path = _find_exe_from_process(proc_name)
                if path:
                    return 'NT' if 'QQNT' in proc_name else 'classic', path
        except Exception:
            pass

    # Search filesystem
    search_paths = [
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
        r"C:\Program Files\Tencent\QQ\Bin\QQ.exe",
        r"C:\Program Files (x86)\Tencent\QQ\Bin\QQ.exe",
    ]
    for p in search_paths:
        if os.path.exists(p):
            return 'NT' if 'QQNT' in p else 'classic', p

    # Try registry
    try:
        for root in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            for sub in [r"SOFTWARE\Tencent\QQ", r"SOFTWARE\Tencent\QQNT",
                        r"SOFTWARE\WOW6432Node\Tencent\QQ"]:
                try:
                    key = winreg.OpenKey(root, sub)
                    path, _ = winreg.QueryValueEx(key, "InstallPath")
                    winreg.CloseKey(key)
                    for exe_name in ["QQ.exe", "QQNT.exe"]:
                        exe = os.path.join(path, exe_name)
                        if os.path.exists(exe):
                            return 'NT' if 'QQNT' in exe_name else 'classic', exe
                except OSError:
                    pass
    except Exception:
        pass
    return None, None


# ── Helpers ───────────────────────────────────────────────

def get_wxid_from_path(account_dir):
    """Extract wxid from account directory (v3.x or v4.x)."""
    name = os.path.basename(account_dir)
    if name.startswith("wxid_"):
        return name

    # v4.x: login dir IS the wxid
    parent = os.path.basename(os.path.dirname(account_dir))
    if parent == 'login' and name.startswith('wxid_'):
        return name

    # v3.x config file
    config_path = os.path.join(account_dir, "config", "AccInfo.dat")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r'wxid_[a-zA-Z0-9_]+', content)
            if m:
                return m.group(0)
        except Exception:
            pass
    return name


def get_qq_uin_from_path(account_dir):
    """Extract QQ number from account directory."""
    return os.path.basename(account_dir)


def get_wechat_version_info(base_dir, version):
    """Get WeChat version-specific info."""
    info = {'version': version, 'base_dir': base_dir}

    if version == '4.x':
        # Read key_info.dat
        login_dir = os.path.join(base_dir, "login")
        if os.path.isdir(login_dir):
            for item in os.listdir(login_dir):
                item_path = os.path.join(login_dir, item)
                if os.path.isdir(item_path) and item.startswith('wxid_'):
                    key_file = os.path.join(item_path, 'key_info.dat')
                    if os.path.exists(key_file):
                        info['has_key_file'] = True
                    # Check for cloud_account.txt
                    cloud_file = os.path.join(base_dir, 'ilink', 'wechat', 'cloud_account.txt')
                    if os.path.exists(cloud_file):
                        info['cloud_account'] = True
        # Check for local data
        info['data_type'] = 'cloud_primary'
    elif version == '3.x':
        # Old format - check for MSG databases
        msg_base = os.path.join(base_dir, "Msg") if os.path.isdir(
            os.path.join(base_dir, "Msg")) else base_dir
        msg_dir = os.path.join(base_dir, "Msg") if os.path.isdir(
            os.path.join(base_dir)) else None
        info['data_type'] = 'local_encrypted'
        info['msg_dir'] = msg_dir

    return info
