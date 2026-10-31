#!/usr/bin/env bash
# ===================================================================
#  MyEnglishVault 一键启动（macOS / Linux）
#  运行本脚本即可：首次自动安装依赖，随后打开桌面窗口。
#  用法：  bash start.sh
# ===================================================================
set -e
echo "[1/2] 安装/校验依赖（首次稍慢，之后很快）..."
python3 -m pip install -r requirements.txt
echo "[2/2] 启动 MyEnglishVault 桌面窗口..."
python3 run.py
