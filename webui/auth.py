# -*- coding: utf-8 -*-
"""Kronos 登录 / 用户管理 / 操作审计。

- 用户存 SQLite(默认 webui/users.db, 可用环境变量 KRONOS_AUTH_DB 改路径)
- 密码 werkzeug PBKDF2 哈希, 不存明文
- 角色: admin(全部权限+用户管理) / user(普通使用)
- 操作审计: 所有页面/接口访问写入 op_logs 表 + audit.log 文件
- 首次初始化: 若无任何用户, 自动创建 admin(密码取 KRONOS_ADMIN_PW, 否则随机生成并打印)
"""

import os
import re
import secrets
import sqlite3
import threading
import time

from flask import g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

AUTH_DB = os.environ.get('KRONOS_AUTH_DB') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'users.db')
AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit.log')
_lock = threading.Lock()

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,20}$')

# 无需登录即可访问的路径前缀
PUBLIC_PATHS = ('/login', '/static', '/favicon.ico', '/healthz')


# ---------------- 存储层 ----------------

def _conn():
    c = sqlite3.connect(AUTH_DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock:
        c = _conn()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            enabled INTEGER NOT NULL DEFAULT 1,
            remark TEXT DEFAULT '',
            created_at TEXT, last_login_at TEXT, last_login_ip TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS op_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT, username TEXT, ip TEXT,
            method TEXT, path TEXT, action TEXT,
            detail TEXT, status INTEGER)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_oplogs_at ON op_logs(at DESC)')
        c.commit()
        users = c.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
        c.close()
    if users == 0:
        pw = os.environ.get('KRONOS_ADMIN_PW') or secrets.token_urlsafe(10)
        create_user('admin', pw, role='admin', remark='系统初始管理员')
        if not os.environ.get('KRONOS_ADMIN_PW'):
            # 揚示到日志(仅自动生成时)
            print(f"[auth] 初始管理员已创建: admin / {pw}  (请尽快登录修改密码)")


def create_user(username, password, role='user', remark='', by=''):
    if not USERNAME_RE.match(username or ''):
        return False, '用户名需为 3-20 位字母/数字/下划线'
    if not password or len(password) < 6:
        return False, '密码至少 6 位'
    if role not in ('admin', 'user'):
        return False, '角色只能是 admin 或 user'
    with _lock:
        c = _conn()
        try:
            c.execute('INSERT INTO users(username, password_hash, role, remark, created_at) '
                      'VALUES (?,?,?,?,?)',
                      (username, generate_password_hash(password), role, remark,
                       time.strftime('%Y-%m-%d %H:%M:%S')))
            c.commit()
            ok, msg = True, f'用户 {username} 已创建({role})'
        except sqlite3.IntegrityError:
            ok, msg = False, f'用户名 {username} 已存在'
        finally:
            c.close()
    log_op(by or 'system', '用户管理', f'创建用户 {username}({role})', 200 if ok else 400)
    return ok, msg


def update_user(uid, data, by=''):
    """data 可含 password / role / enabled / remark, 值为 None 表示不改。"""
    with _lock:
        c = _conn()
        row = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not row:
            c.close()
            return False, '用户不存在'
        fields, vals, acts = [], [], []
        if data.get('password'):
            if len(data['password']) < 6:
                return False, '密码至少 6 位'
            fields.append('password_hash=?')
            vals.append(generate_password_hash(data['password']))
            acts.append('重置密码')
        if data.get('role') is not None:
            if row['username'] == 'admin' and data['role'] != 'admin':
                return False, '不能降级内置 admin 的角色'
            fields.append('role=?')
            vals.append(data['role'])
            acts.append(f"角色→{data['role']}")
        if data.get('enabled') is not None:
            if row['username'] == 'admin' and not data['enabled']:
                return False, '不能禁用内置 admin'
            fields.append('enabled=?')
            vals.append(1 if data['enabled'] else 0)
            acts.append('启用' if data['enabled'] else '禁用')
        if data.get('remark') is not None:
            fields.append('remark=?')
            vals.append(data['remark'])
            acts.append('改备注')
        if not fields:
            c.close()
            return False, '没有需要修改的字段'
        vals.append(uid)
        c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)
        c.commit()
        c.close()
    log_op(by, '用户管理', f"{row['username']}: {'/'.join(acts)}", 200)
    return True, '已更新'


def delete_user(uid, by=''):
    with _lock:
        c = _conn()
        row = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not row:
            c.close()
            return False, '用户不存在'
        if row['username'] == 'admin':
            c.close()
            return False, '不能删除内置 admin'
        c.execute('DELETE FROM users WHERE id=?', (uid,))
        c.commit()
        c.close()
    log_op(by, '用户管理', f"删除用户 {row['username']}", 200)
    return True, '已删除'


def list_users():
    with _lock:
        c = _conn()
        rows = c.execute('SELECT id, username, role, enabled, remark, created_at, '
                         'last_login_at, last_login_ip FROM users ORDER BY id').fetchall()
        c.close()
    return [dict(r) for r in rows]


def verify_login(username, password, ip=''):
    with _lock:
        c = _conn()
        row = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        c.close()
    if not row or not check_password_hash(row['password_hash'] or '', password or ''):
        log_op(username or '?', '登录', '失败(用户名或密码错误)', 401)
        return None
    if not row['enabled']:
        log_op(username, '登录', '失败(账号已禁用)', 403)
        return None
    try:
        with _lock:
            c = _conn()
            c.execute("UPDATE users SET last_login_at=?, last_login_ip=? WHERE id=?",
                      (time.strftime('%Y-%m-%d %H:%M:%S'), ip, row['id']))
            c.commit()
            c.close()
    except Exception:
        pass  # 登录留痕失败不阻断登录
    log_op(username, '登录', '成功', 200)
    return {'id': row['id'], 'username': row['username'], 'role': row['role']}


def change_password(username, old_pw, new_pw):
    with _lock:
        c = _conn()
        row = c.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        c.close()
    if not row or not check_password_hash(row['password_hash'] or '', old_pw or ''):
        return False, '原密码不正确'
    if len(new_pw or '') < 6:
        return False, '新密码至少 6 位'
    with _lock:
        c = _conn()
        c.execute('UPDATE users SET password_hash=? WHERE id=?',
                  (generate_password_hash(new_pw), row['id']))
        c.commit()
        c.close()
    log_op(username, '账号', '修改密码', 200)
    return True, '密码已修改'


# ---------------- 审计日志 ----------------

def log_op(username, action, detail='', status=None):
    at = time.strftime('%Y-%m-%d %H:%M:%S')
    method = path = ip = ''
    try:
        if request:
            method, path, ip = request.method, request.path, (request.remote_addr or '')
    except Exception:
        pass
    with _lock:
        try:
            c = _conn()
            c.execute('INSERT INTO op_logs(at, username, ip, method, path, action, detail, status) '
                      'VALUES (?,?,?,?,?,?,?,?)',
                      (at, username or '-', ip, method, path, action, detail, status))
            c.commit()
            c.close()
        except Exception:
            pass
    try:
        with open(AUDIT_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{at} [{username or '-'}] {action} {detail} "
                    f"({ip} {method} {path})\n")
    except Exception:
        pass


def list_logs(limit=200):
    with _lock:
        c = _conn()
        rows = c.execute('SELECT * FROM op_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        c.close()
    return [dict(r) for r in rows]


# ---------------- Flask 集成 ----------------

def current_user():
    u = session.get('user')
    if u and u.get('id'):
        return u
    return None


def login_required(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith('/api/') or request.path.startswith('/quant/api/'):
                return jsonify({'error': '未登录', 'need_login': True}), 401
            return redirect(url_for('login_page', next=request.path))
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            if request.path.startswith('/api/') or request.path.startswith('/quant/api/'):
                return jsonify({'error': '未登录', 'need_login': True}), 401
            return redirect(url_for('login_page', next=request.path))
        if u.get('role') != 'admin':
            log_op(u['username'], '权限', f'越权访问 {request.path}', 403)
            if request.path.startswith('/api/') or request.path.startswith('/quant/api/'):
                return jsonify({'error': '需要管理员权限'}), 403
            return render_template('login.html', error='需要管理员权限', u=None), 403
        return f(*args, **kwargs)

    return wrapper


# ---- 请求级审计钩子(app 注册) ----

_SENSITIVE = ('/login',)

def before_request_hook():
    """登录检查 + 全量操作审计。"""
    g.user = current_user()
    path = request.path
    if any(path == p or path.startswith(p) for p in PUBLIC_PATHS):
        return None
    if not g.user:
        if path.startswith('/api/') or path.startswith('/quant/api/'):
            return jsonify({'error': '未登录', 'need_login': True}), 401
        return redirect(url_for('login_page', next=path))
    # 审计: 非 GET 全记; GET 记页面(不记 api 静态轮询)
    if request.method != 'GET' or (not path.startswith('/api/') and
                                   not path.startswith('/quant/api/')):
        detail = ''
        if request.method == 'POST' and path not in _SENSITIVE:
            try:
                body = request.get_json(silent=True) or request.form.to_dict()
                detail = str({k: (str(v)[:40]) for k, v in list(body.items())[:6]})[:200]
            except Exception:
                pass
        g._audit = (g.user['username'], '访问', f"{request.method} {path} {detail}".strip()[:240])
    return None


def after_request_hook(resp):
    entry = getattr(g, '_audit', None)
    if entry:
        log_op(entry[0], entry[1], entry[2], resp.status_code)
    return resp
