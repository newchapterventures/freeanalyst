#!/bin/bash
# FreeAnalyst 启动器（macOS）
#
# 给不懂命令行的同事用：**双击这个文件**就行 —— 它会起本地向导并打开浏览器。
#
# 三条刻意的设计：
#   1. **不替安装任何东西。** 缺 Python 就告诉你缺，给官方下载页，不偷偷装（不出网、
#      不改系统是这工具的基本承诺，安装脚本自己先破坏它就荒唐了）。
#   2. **出错不闪退。** 双击运行的程序跑完就关窗口，错误信息一闪而过没人看得见 ——
#      所以任何失败路径都停下来等你按键。
#   3. **关掉这个窗口 = 停止服务。** 材料始终只在本机处理，窗口一关就没了。

set -uo pipefail                                    # 不用 -e：出错要自己处理并暂停

cd "$(dirname "$0")" || { echo "进不了脚本所在目录"; read -r -n 1 -p "按任意键关闭…"; exit 1; }

echo "──────────────────────────────────────────────"
echo " FreeAnalyst · 本地估值向导"
echo " 目录：$(pwd)"
echo "──────────────────────────────────────────────"
echo

# ── 找 Python（3.10 起） ──
PY=""
for cand in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" - <<'EOF' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
    then PY="$cand"; break; fi
  fi
done

if [ -z "$PY" ]; then
  echo "没有找到 Python 3.10 或更新的版本 —— 这个工具需要它。"
  echo
  echo "怎么办："
  echo "  1) 打开 https://www.python.org/downloads/ 下载 macOS 安装包"
  echo "  2) 双击安装（一路继续即可）"
  echo "  3) 再双击本文件"
  echo
  # 不自动打开浏览器：先让人读完这几行
  read -r -n 1 -p "按任意键关闭…"
  exit 1
fi
echo "Python：$("$PY" -V 2>&1)"

# ── 起向导（材料在第 1 步里选，这里不用给） ──
echo
echo "正在启动……浏览器会自动打开 http://127.0.0.1:8765/"
echo "**关掉这个窗口就是停止服务。**"
echo
"$PY" freeanalyst.py ui
code=$?

echo
if [ $code -ne 0 ]; then
  echo "启动失败（退出码 ${code}）。把上面这段信息发给做这个工具的人。"
else
  echo "已停止。"
fi
# 末尾这句 read 只是为了留住窗口；它的返回值不能变成脚本的退出码
read -r -n 1 -p "按任意键关闭…" || true
echo
exit ${code}
