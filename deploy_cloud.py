#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kronos 云端自动部署（从本机通过 WinRM 触发 Windows Server 拉取 GitHub tag）

用法：
    python3 deploy_cloud.py            # 部署 VERSION 文件里对应的 v<版本> tag
    python3 deploy_cloud.py v1.11.0    # 指定 tag
    python3 deploy_cloud.py --status   # 只查看云端当前状态，不部署

凭据约定（值为本机文件，不进对话、不写进本脚本、不打印）：
    ~/.config/dy-workbench/winrm_password   Windows administrator 密码（权限 600）
    可选环境变量：KRONOS_CLOUD_HOST / KRONOS_CLOUD_USER

云端复用数据项目已验证的 WinRM 通道：123.207.186.241:5985 (HTTP, negotiate)。
依赖 pypsrp 及 vendor（数据项目 deploy/vendor），需 Python 3.10+ 运行（3.9 不兼容其 urllib3）。
"""
import os
import sys
import time
import subprocess

HOST = os.environ.get("KRONOS_CLOUD_HOST", "123.207.186.241")
USER = os.environ.get("KRONOS_CLOUD_USER", "administrator")
HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = "/Users/ccb/WorkBuddy/数据/all-cities-leads/deploy/vendor"
PROJECT = r"C:\kronos"

# WinRM 必须绕过本机代理，否则 403
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

STATUS_PS = r'''
$ErrorActionPreference='SilentlyContinue'
$v = Get-Content C:\kronos\VERSION
$dp = Test-Path C:\kronos\webui\daily_pick.py
$deploy = (Get-CimInstance Win32_Process -Filter "name='powershell.exe'" | Where-Object {$_.CommandLine -like '*deploy_cloud*'} | Measure-Object).Count
$app = (Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object {$_.CommandLine -like '*app.py*'} | Measure-Object).Count
"VERSION=$v|daily_pick=$dp|deploy_running=$deploy|app_running=$app"
'''


def load_client():
    if os.path.isdir(os.path.join(VENDOR, "pypsrp")):
        sys.path.insert(0, VENDOR)
    from pypsrp.client import Client
    return Client


def read_password():
    """优先从加密凭据库读取（推荐），回退到旧明文文件（兼容）。"""
    # 1) 加密凭据库：通过 get_secret.py 解密，明文不落本项目文件
    try:
        return subprocess.check_output(
            [sys.executable, os.path.join(HERE, "tools", "get_secret.py"), "get", "winrm_password"],
            text=True,
        ).strip()
    except Exception:
        pass
    # 2) 回退：旧明文文件（权限 600）
    old = os.path.expanduser("~/.config/dy-workbench/winrm_password")
    if os.path.isfile(old):
        return open(old, encoding="utf-8").read().strip()
    sys.exit("缺少凭据：加密凭据库不可用，且无旧凭据文件 " + old)


def main():
    pw = read_password()
    Client = load_client()
    c = Client(HOST, username=USER, password=pw, ssl=False, port=5985,
               auth="negotiate", encryption="auto", operation_timeout=120, read_timeout=120)

    if "--status" in sys.argv:
        out, _, _ = c.execute_ps(STATUS_PS)
        print((out or "").strip())
        return

    tag = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not tag:
        here = os.path.dirname(os.path.abspath(__file__))
        ver = open(os.path.join(here, "VERSION"), encoding="utf-8").read().strip()
        tag = "v" + ver
    print("部署 %s -> %s (WinRM)" % (tag, HOST), flush=True)

    # 后台触发部署，避免 WinRM 长连接被工具超时杀掉：Start-Process 独立跑，日志落云端文件
    cmd = (
        '$log="C:\\kronos\\deploy_auto.log"; '
        '$arg="-ExecutionPolicy Bypass -File C:\\kronos\\deploy_cloud.ps1 -ProjectRoot C:\\kronos '
        '-Tag %s -ExposePort80 -InstallAutostart"; '
        '$p=Start-Process powershell -ArgumentList $arg -RedirectStandardOutput $log '
        '-RedirectStandardError "C:\\kronos\\deploy_auto.err" -PassThru -WindowStyle Hidden; '
        '"STARTED PID=$($p.Id)"' % tag
    )
    out, _, _ = c.execute_ps(cmd)
    print((out or "").strip(), flush=True)

    # 轮询直到完成
    deadline = time.time() + 300
    last = ""
    while time.time() < deadline:
        time.sleep(8)
        out, _, _ = c.execute_ps(STATUS_PS)
        line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if line != last:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)
            last = line
        want = "VERSION=%s" % tag.lstrip("v")
        if want in line and "daily_pick=True" in line and "deploy_running=0" in line:
            print("DEPLOY_DONE", flush=True)
            break
    else:
        print("DEPLOY_TIMEOUT（查看云端 C:\\kronos\\deploy_auto.log）", flush=True)


if __name__ == "__main__":
    main()
