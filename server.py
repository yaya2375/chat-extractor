#!/usr/bin/env python3
"""Chat Record Extractor Server - WeChat & QQ local database extraction tool.

Starts a local web server providing:
- WeChat/QQ chat database auto-discovery and decryption
- Chat message browsing with search and filters
- Export to JSON/CSV/TXT
- Responsive UI accessible from both PC and mobile (same WiFi)

Usage: python server.py [--port PORT] [--host HOST]
"""

import os
import sys
import json
import time
import argparse
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, Response

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extractor import utils
from extractor.wechat_key import (
    extract_key_from_memory, test_key_on_db,
    format_key, parse_key_input,
)
from extractor import wechat_db, qq_db

app = Flask(__name__, static_folder=None)

# ── Global state ────────────────────────────────────────
state = {
    'messages': [],        # All loaded messages
    'scan_results': None,  # Last scan results
    'key': None,           # Current WeChat key bytes
    'key_format': '',      # 'hex' or 'raw'
}


# ── Static file serving ─────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(os.path.dirname(__file__), 'index.html')


# ── API: Scan ───────────────────────────────────────────
@app.route('/api/scan', methods=['POST'])
def api_scan():
    """Scan for installed WeChat and QQ instances."""
    result = {
        'wechat': {'found': False, 'exe': None, 'accounts': []},
        'qq': {'found': False, 'exe': None, 'accounts': []},
    }

    # Scan WeChat
    wechat_version, wechat_exe_path = utils.find_wechat_exe()
    if wechat_exe_path:
        result['wechat']['found'] = True
        result['wechat']['exe'] = wechat_exe_path
        result['wechat']['version'] = wechat_version

    wechat_ver, wechat_base, wechat_accounts = utils.find_wechat_data_dirs()
    if wechat_base:
        result['wechat']['base_dir'] = wechat_base
        result['wechat']['version'] = wechat_ver or wechat_version

        for acc_dir in wechat_accounts:
            try:
                wxid = utils.get_wxid_from_path(acc_dir)
                db_info = wechat_db.get_account_info(acc_dir)
                info = {
                    'wxid': wxid,
                    'path': acc_dir,
                    'db_count': db_info.get('db_count', 0),
                    'version': db_info.get('version', wechat_ver or ''),
                    'has_databases': db_info.get('db_count', 0) > 0,
                }
                # Determine if actual chat data exists (migration done)
                if wechat_ver and wechat_ver.startswith('4.x'):
                    if info['has_databases']:
                        info['hint'] = f'已检测到 {info["db_count"]} 个数据库，可以进行提取'
                        info['ready'] = True
                    else:
                        info['hint'] = '未检测到聊天数据库。请先通过手机微信"聊天记录迁移"同步数据。\n操作: 手机微信 → 我 → 设置 → 聊天 → 聊天记录迁移与备份 → 迁移到电脑微信'
                        info['ready'] = False
                else:
                    info['ready'] = info['has_databases']
                result['wechat']['accounts'].append(info)
            except Exception as e:
                result['wechat']['accounts'].append({
                    'wxid': os.path.basename(acc_dir),
                    'path': acc_dir,
                    'ready': False,
                    'error': str(e),
                })

    # Scan QQ
    qq_version, qq_exe_path = utils.find_qq_exe()
    if qq_exe_path:
        result['qq']['found'] = True
        result['qq']['exe'] = qq_exe_path
        result['qq']['version'] = qq_version

    qq_base, qq_accounts = utils.find_qq_data_dirs()
    if qq_base:
        result['qq']['base_dir'] = qq_base
        for acc_dir in qq_accounts:
            try:
                info = qq_db.get_account_info(acc_dir)
                info['uin'] = utils.get_qq_uin_from_path(acc_dir)
                result['qq']['accounts'].append(info)
            except Exception as e:
                result['qq']['accounts'].append({
                    'uin': os.path.basename(acc_dir),
                    'error': str(e),
                })

    state['scan_results'] = result
    return jsonify(result)


# ── API: Extract WeChat ─────────────────────────────────
@app.route('/api/extract/wechat', methods=['POST'])
def api_extract_wechat():
    """Extract WeChat messages."""
    data = request.get_json() or {}
    account_dir = data.get('account_dir', '')
    key_input = data.get('key', '')  # Optional manual key
    auto_key = data.get('auto_key', True)

    if not account_dir or not os.path.isdir(account_dir):
        return jsonify({'error': '无效的微信数据目录'}), 400

    # ── Get key ──────────────────────────────────────────
    key_bytes = None
    key_method = ''

    # Detect WeChat version from the account path
    is_v4 = 'xwechat_files' in account_dir or 'db_storage' in account_dir
    version_label = '4.x' if is_v4 else '3.x'

    if key_input:
        key_bytes = parse_key_input(key_input)
        if key_bytes is None:
            return jsonify({'error': '密钥格式无效，请输入64位十六进制字符串 (或 x\'...\' 格式)'}), 400
        key_method = '手动输入'

        # Test the key on an available DB
        test_db = _find_test_db(account_dir, is_v4)
        if test_db and not test_key_on_db(key_bytes, test_db, is_v4=is_v4):
            return jsonify({'error': '密钥验证失败，请确认密钥是否正确'}), 400
    elif auto_key:
        # Try memory extraction
        key_bytes, msg = extract_key_from_memory()
        if key_bytes:
            key_method = msg
        else:
            return jsonify({
                'error': f'自动提取密钥失败: {msg}',
                'need_manual_key': True,
                'hint': '请确保微信已运行，或手动输入数据库密钥（64位十六进制）',
            }), 400

    if key_bytes is None:
        return jsonify({'error': '未能获取密钥'}), 400

    state['key'] = key_bytes

    def generate():
        try:
            def progress(step, total, msg):
                yield f"data: {json.dumps({'type': 'progress', 'step': step, 'total': total, 'message': msg})}\n\n"

            messages = wechat_db.extract_messages(
                account_dir, key_bytes,
                progress_callback=progress, version=version_label
            )

            existing = state['messages']
            existing = [m for m in existing if m['platform'] != '微信']
            state['messages'] = existing + messages

            yield f"data: {json.dumps({'type': 'done', 'count': len(messages), 'method': key_method})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


# ── API: Extract QQ ─────────────────────────────────────
@app.route('/api/extract/qq', methods=['POST'])
def api_extract_qq():
    """Extract QQ messages."""
    data = request.get_json() or {}
    account_dir = data.get('account_dir', '')

    if not account_dir or not os.path.isdir(account_dir):
        return jsonify({'error': '无效的QQ数据目录'}), 400

    def generate():
        try:
            def progress(step, total, msg):
                yield f"data: {json.dumps({'type': 'progress', 'step': step, 'total': total, 'message': msg})}\n\n"

            messages = qq_db.extract_messages(account_dir, progress_callback=progress)

            # Update state
            existing = state['messages']
            existing = [m for m in existing if m['platform'] != 'QQ']
            state['messages'] = existing + messages

            yield f"data: {json.dumps({'type': 'done', 'count': len(messages)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(generate(), mimetype='text/event-stream')


# ── API: Messages ───────────────────────────────────────
@app.route('/api/messages', methods=['GET'])
def api_messages():
    """Get all loaded messages with optional filters."""
    msgs = state['messages']

    # Filters
    search = request.args.get('search', '').strip()
    sender = request.args.get('sender', '').strip()
    date_start = request.args.get('date_start', '')
    date_end = request.args.get('date_end', '')
    msg_type = request.args.get('type', '')
    platform = request.args.get('platform', '')

    filtered = []
    for m in msgs:
        if search:
            q = search.lower()
            if q not in m['sender'].lower() and q not in m['content'].lower():
                continue
        if sender and m['sender'] != sender:
            continue
        if date_start:
            try:
                start = datetime.fromisoformat(date_start)
                if m['time'] < start:
                    continue
            except ValueError:
                pass
        if date_end:
            try:
                end = datetime.fromisoformat(date_end + 'T23:59:59')
                if m['time'] > end:
                    continue
            except ValueError:
                pass
        if msg_type and m['type'] != msg_type:
            continue
        if platform and m['platform'] != platform:
            continue
        filtered.append(m)

    # Pagination
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 500))
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page

    page_data = filtered[start_idx:end_idx]

    return jsonify({
        'total': len(filtered),
        'page': page,
        'per_page': per_page,
        'messages': [_serialize_msg(m) for m in page_data],
    })


# ── API: Stats ──────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
def api_stats():
    """Get message statistics."""
    msgs = state['messages']
    if not msgs:
        return jsonify({'total': 0, 'senders': [], 'platforms': [], 'date_range': None})

    # Sender counts
    sender_counts = {}
    for m in msgs:
        sender_counts[m['sender']] = sender_counts.get(m['sender'], 0) + 1

    senders = sorted(sender_counts.items(), key=lambda x: -x[1])

    # Platform counts
    platform_counts = {}
    for m in msgs:
        platform_counts[m['platform']] = platform_counts.get(m['platform'], 0) + 1

    # Date range
    dates = [m['time'] for m in msgs]
    min_date = min(dates)
    max_date = max(dates)
    days = max(1, (max_date - min_date).days)

    return jsonify({
        'total': len(msgs),
        'senders': [{'name': n, 'count': c} for n, c in senders],
        'platforms': platform_counts,
        'date_range': {
            'start': min_date.isoformat(),
            'end': max_date.isoformat(),
            'days': days,
        },
    })


# ── API: Export ─────────────────────────────────────────
@app.route('/api/export/<format>', methods=['GET'])
def api_export(format):
    """Export messages in specified format."""
    msgs = state['messages']
    if not msgs:
        return jsonify({'error': '没有可导出的消息'}), 400

    now = datetime.now().strftime('%Y%m%d_%H%M%S')

    if format == 'json':
        data = json.dumps(
            [_serialize_msg(m) for m in msgs],
            ensure_ascii=False, indent=2, default=str
        )
        return Response(
            data, mimetype='application/json',
            headers={'Content-Disposition': f'attachment; filename=chat_{now}.json'}
        )

    elif format == 'csv':
        lines = ['发送者,时间,平台,类型,内容']
        for m in msgs:
            content = m['content'].replace('"', '""').replace('\n', ' ')
            lines.append(f'{csv_escape(m["sender"])},{m["time"].isoformat()},{m["platform"]},{m["type"]},{csv_escape(content)}')
        data = '﻿' + '\n'.join(lines)
        return Response(
            data, mimetype='text/csv;charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename=chat_{now}.csv'}
        )

    elif format == 'txt':
        lines = []
        for m in msgs:
            ts = m['time'].strftime('%Y-%m-%d %H:%M:%S')
            lines.append(f'{ts}  {m["sender"]}\n{m["content"]}\n')
        data = '﻿' + '\n'.join(lines)
        return Response(
            data, mimetype='text/plain;charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename=chat_{now}.txt'}
        )

    return jsonify({'error': f'不支持的导出格式: {format}'}), 400


# ── API: Clear ──────────────────────────────────────────
@app.route('/api/clear', methods=['POST'])
def api_clear():
    """Clear all loaded messages."""
    state['messages'] = []
    state['key'] = None
    return jsonify({'status': 'ok'})


# ── Helpers ─────────────────────────────────────────────
def _find_test_db(account_dir, is_v4):
    """Find a database file to test key against."""
    if is_v4:
        msg_dir = os.path.join(account_dir, "db_storage", "message")
        if os.path.isdir(msg_dir):
            for f in sorted(os.listdir(msg_dir)):
                if f.startswith('message_') and f.endswith('.db'):
                    return os.path.join(msg_dir, f)
    else:
        msg_dir = os.path.join(account_dir, "Msg")
        if os.path.isdir(msg_dir):
            for f in sorted(os.listdir(msg_dir)):
                if f.startswith('MSG') and f.endswith('.db'):
                    return os.path.join(msg_dir, f)
    return None


def _serialize_msg(m):
    return {
        'sender': m['sender'],
        'content': m['content'],
        'time': m['time'].isoformat(),
        'type': m['type'],
        'platform': m['platform'],
        'chat_name': m.get('chat_name', ''),
    }


def csv_escape(s):
    return '"' + s.replace('"', '""') + '"'


def get_local_ip():
    """Get the local network IP address."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ── Main ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Chat Record Extractor Server')
    parser.add_argument('--port', type=int, default=5000, help='Server port (default: 5000)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Bind host (default: 0.0.0.0)')
    parser.add_argument('--no-browser', action='store_true', help='Do not auto-open browser')
    args = parser.parse_args()

    local_ip = get_local_ip()
    port = args.port

    print("=" * 60)
    print("  聊天记录提取工具  |  WeChat & QQ Chat Extractor")
    print("=" * 60)
    print()
    print(f"  本地访问:  http://localhost:{port}")
    print(f"  手机访问:  http://{local_ip}:{port}")
    print()
    print("  支持功能:")
    print("    * 微信聊天记录提取 (PC端加密数据库)")
    print("    * QQ聊天记录提取 (本地数据库)")
    print("    * 关键词搜索、按发送者/日期筛选")
    print("    * 导出 JSON / CSV / TXT")
    print("    * 手机同WiFi下远程查看")
    print()
    print("=" * 60)

    if not args.no_browser:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{port}')).start()

    app.run(host=args.host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
