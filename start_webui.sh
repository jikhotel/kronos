#!/bin/bash
# Kronos WebUI 本机启动脚本
# 用法: ./start_webui.sh
# 说明: 沙盒内长驻进程会被回收, 持久化运行请在真实 macOS 终端执行本脚本

set -e

KRONOS_DIR="/Users/ccb/dev/Kronos"
VENV="/Users/ccb/.workbuddy/binaries/python/envs/kronos"

# HuggingFace 主站在当前网络下不可达, 走国内镜像
export HF_ENDPOINT="https://hf-mirror.com"
# 必须禁用 xet 传输后端: 启用时文件下载会被重定向到 us.aws.cdn.hf.co 并超时
export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0
# 模型权重已预缓存到 ~/.cache/huggingface, 离线加载可避免每次请求 HF 主站导致长超时
# 若日后需要在线拉取新模型, 把下面的 1 改成 0
export HF_HUB_OFFLINE=1

cd "$KRONOS_DIR/webui" || exit 1

echo "======================================"
echo "  Kronos WebUI 启动中"
echo "======================================"
echo "Python : $VENV/bin/python"
"$VENV/bin/python" --version
echo "设备   : CPU (Intel Mac, 无 MPS/CUDA)"
echo "数据   : $KRONOS_DIR/data"
echo "镜像   : $HF_ENDPOINT"
echo "--------------------------------------"
echo "访问地址: http://localhost:7070"
echo "停止服务: Ctrl+C"
echo "======================================"
echo

exec "$VENV/bin/python" app.py
