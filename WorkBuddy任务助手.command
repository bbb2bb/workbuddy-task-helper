#!/bin/bash
# WorkBuddy 成长计划任务自动执行脚本 —— 全项目唯一入口
#   双击我就行。第一屏是账号列表，按提示输数字或字母。
#   登录 / 挑任务 / 批量跑全部任务 / 批量跑每日任务，都在这里。
cd "$(dirname "$0")" || exit 1

# 找一个 Python 3.10 或更新的版本来跑脚本：先试各安装方式的固定目录（uv / pyenv /
# conda / 官网安装包 / Homebrew），再试 ~/.local/bin 与 PATH 里带版本号的名字。
PY=""
for c in \
  "$HOME/.workbuddy/binaries/python/versions/"*"/bin/python3" \
  "$HOME/.local/share/uv/python/cpython-"*"/bin/python3" \
  "$HOME/.local/bin/python3" \
  "$HOME/.local/bin/python3."* \
  "$HOME/.pyenv/versions/"*"/bin/python3" \
  "$HOME/miniconda3/bin/python3" \
  "$HOME/anaconda3/bin/python3" \
  "$HOME/miniforge3/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3."*"/bin/python3" \
  "/opt/homebrew/bin/python3" \
  "/opt/homebrew/opt/python@3."*"/bin/python3" \
  "/opt/local/bin/python3" \
  "/usr/local/bin/python3" \
  "$(command -v python3.15 2>/dev/null)" \
  "$(command -v python3.14 2>/dev/null)" \
  "$(command -v python3.13 2>/dev/null)" \
  "$(command -v python3.12 2>/dev/null)" \
  "$(command -v python3.11 2>/dev/null)" \
  "$(command -v python3.10 2>/dev/null)" \
  "$(command -v python3 2>/dev/null)"
do
  [ -x "$c" ] || continue
  v=$("$c" -c 'import sys; print("%d.%d"%sys.version_info[:2]) if sys.version_info>=(3,10) else sys.exit(1)' 2>/dev/null) || continue
  case "$v" in
    3.*) PY="$c"; break ;;
  esac
done

if [ -z "$PY" ]; then
  echo "❌ 没有找到 Python 3.10 或更新的版本，脚本跑不起来。"
  echo
  echo "装一个就好，两种方法任选其一："
  echo
  echo "  ① 已经装了 Homebrew 的，在终端里粘贴："
  echo "       brew install python@3.13"
  echo
  echo "  ② 没装 Homebrew 的用 uv（更省事，装自家目录、不用密码）："
  echo "       curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "     装完关掉终端窗口重新打开，再粘贴："
  echo "       uv python install 3.13"
  echo
  echo "装好后重新双击本脚本。"
  echo
  echo "按回车键关闭窗口"
  read -r
  exit 1
fi

"$PY" scripts/任务助手.py

echo
echo "按回车键关闭窗口"
read -r
