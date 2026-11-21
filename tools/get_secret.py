#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kronos 凭据安全读取工具

凭据库用 openssl AES-256-CBC 加密存放在项目之外：
    ~/.config/kronos/secrets.enc   （密文）
    ~/.config/kronos/.key          （主密钥，权限 600）

用法：
    python3 tools/get_secret.py keys              # 只列键名（不显示值，安全）
    python3 tools/get_secret.py get <key>         # 输出某个键的值（供脚本/管道用）
    python3 tools/get_secret.py env               # 输出 export 格式（供 eval 用，注意可信环境）

支持的键：winrm_password / github_token / github_owner / github_repo /
          cloud_host / cloud_user / cloud_port

设计原则：
  - 明文密码/token 不落项目文件、不进 git、不进 CONTEXT.md
  - 默认（无参数或 keys）绝不显示敏感值
  - 值只在调用 get/env 时解密到 stdout，可管道给部署/推送脚本
"""
import os
import sys
import json
import subprocess

SEC_DIR = os.path.expanduser("~/.config/kronos")
ENC_FILE = os.path.join(SEC_DIR, "secrets.enc")
KEY_FILE = os.path.join(SEC_DIR, ".key")


def load():
    if not os.path.isfile(ENC_FILE) or not os.path.isfile(KEY_FILE):
        sys.exit("凭据库缺失：需要 %s 和 %s" % (ENC_FILE, KEY_FILE))
    p = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-base64", "-a",
         "-in", ENC_FILE, "-pass", "file:" + KEY_FILE],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        sys.exit("解密失败（密钥错误或凭据库损坏）: " + p.stderr.strip())
    return json.loads(p.stdout)


def main():
    data = load()
    args = sys.argv[1:]
    if not args or args[0] == "keys":
        for k in data:
            # 标注哪些是敏感值
            tag = " (敏感)" if k in ("winrm_password", "github_token") else ""
            print("  %-16s%s" % (k, tag))
        return
    cmd = args[0]
    if cmd == "get":
        if len(args) < 2:
            sys.exit("用法: get <key>")
        key = args[1]
        if key not in data:
            sys.exit("未知键: " + key)
        print(data[key], end="")
    elif cmd == "env":
        for k, v in data.items():
            print("export %s='%s'" % (k.upper(), v))
    else:
        sys.exit("未知命令: " + cmd + "（可用: keys / get / env）")


if __name__ == "__main__":
    main()
