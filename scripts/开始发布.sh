#!/bin/bash
# 小红书图文笔记「一键发布」启动器
# 2026-10-07  —  双击对应的 .desktop 图标就会跑这个脚本
#
# 做的事：读 2说明 里的需求 → AI 现写标题和正文 → 跑 1→7 全流程 → 发布
#
# 手动跑（想看实时输出）：
#   bash ~/Desktop/工具集/視覺鍵鼠/开始发布.sh
# 只跑到发布前（不真发）：
#   bash ~/Desktop/工具集/視覺鍵鼠/开始发布.sh --no-publish
# 沿用上次的标题正文（不重新生成）：
#   bash ~/Desktop/工具集/視覺鍵鼠/开始发布.sh --keep

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${XHS_PY:-venv/bin/python}"
SCRIPT="${XHS_SCRIPT:-$SCRIPT_DIR/xhs_manual_publish.py}"
DIR="${XHS_DIR:-$SCRIPT_DIR}"
SPEC="$DIR/2说明"
IMAGE="$DIR/1封面.png"
LOG="$DIR/发布日志_$(date +%Y%m%d_%H%M%S).log"
NO_PUBLISH=""
REGEN="--regen"

for a in "$@"; do
  case "$a" in
    --no-publish) NO_PUBLISH="--no-publish" ;;
    --keep)      REGEN="" ;;
  esac
done

say() { printf '%s\n' "$*"; }

# ---- API key（2026-10-07 修）--------------------------------------------
# LLM 的 key 由外部环境注入（如 .env / 会话环境变量），双击图标时继承不到 → step3 崩
# 「环境变量 ... 没设」。所以落一份到 ~/.config/xhs-publish/env（600 权限），
# 这里显式读进来。会话里已有同名变量时不覆盖（方便临时换 key 调试）。
ENVFILE="$HOME/.config/xhs-publish/env"
if [ -z "${XHS_API_KEY:-}" ] && [ -f "$ENVFILE" ]; then
  set -a; . "$ENVFILE"; set +a
fi
if [ -z "${XHS_API_KEY:-}" ]; then
  say "❌ 没找到 LLM API key（$ENVFILE 不存在且环境变量也没有）"
  say "   标题和正文要靠 AI 现写，没有 key 跑不了第 3 步。"
  say "   补一份：printf 'XHS_API_KEY=你的key\n' > $ENVFILE"
  read -r -p "按回车退出…" _
  exit 1
fi

# ---- 前置检查 -------------------------------------------------------------
if [ ! -x "$PY" ]; then
  say "❌ 找不到解释器：$PY"
  say "   PaddleOCR venv 可能被删了/换路径了。"
  exit 1
fi
if [ ! -f "$SCRIPT" ]; then
  say "❌ 找不到发布脚本：$SCRIPT"
  exit 1
fi
if [ ! -f "$SPEC" ]; then
  say "❌ 找不到需求文件：$SPEC"
  say "   里面有：标题要求 / 正文要求 / 话题 / @账号 / 引用链接"
  exit 1
fi
if [ ! -f "$IMAGE" ]; then
  say "❌ 找不到封面图：$IMAGE"
  exit 1
fi
if [ -z "${DISPLAY:-}" ]; then
  export DISPLAY=:0
fi

# ---- Paddle 的 CUDA 依赖（2026-10-07 修）--------------------------------
# venv 里装的是 **CUDA 版 paddlepaddle**（is_compiled_with_cuda=True），import 时就要链
# libcudnn.so.9。而系统没装 cudnn（apt 装的会占 1~2GB），唯一有它的是本地 AI 运行时目录。
# 我在终端里跑没问题是因为继承了 LD_LIBRARY_PATH；双击图标时环境干净 →
# paddle 自动走 GPU 分支 → `ImportError: libcudnn.so.9` → 用户只看到一屏 traceback。
# 所以这里显式补上，并前置检查，让失败信息变成一句人话。
CUDNN_DIR="${CUDNN_DIR:-/usr/local/lib/ai-runtime/cuda-libs}"
if [ -f "$CUDNN_DIR/libcudnn.so.9" ]; then
  export LD_LIBRARY_PATH="$CUDNN_DIR:${LD_LIBRARY_PATH:-}"
else
  say "⚠️  没找到 $CUDNN_DIR/libcudnn.so.9"
  say "   venv 里的 paddlepaddle 是 CUDA 版，import 时需要它。两条出路："
  say "   ① 装 cudnn：sudo apt install libcudnn9-cuda-12 （约 1~2GB）"
  say "   ② 换成 CPU 版 paddle：见 README"
  say "   找得到的话，LD_LIBRARY_PATH 里指到含 libcudnn.so.9 的目录即可。"
  read -r -p "按回车退出…" _
  exit 1
fi

# ---- 从 2说明 解析参数 ----------------------------------------------------
# 引用链接
QUOTE=$(grep -oP '他人笔记引用链接：\K\S+' "$SPEC" | head -1)
# @账号
AT=$(grep -oP '结尾需@\K\S+' "$SPEC" | head -1)
# 话题：用 python 解析（shell 的 tr/sed 在「#读书推荐 书单 读书分享」这种
# 缺前导#、且词间空格易错位的行上会拆坏，实测拆出 '#单 #书分享'）
TOPICS=$(python3 - "$SPEC" <<'PYEOF'
import re, sys
line = next((l for l in open(sys.argv[1], encoding='utf-8').read().splitlines()
             if l.strip().startswith('话题')), '')
body = line.split('：', 1)[1] if '：' in line else ''
body = re.split(r',?\s*结尾需@', body)[0]
words = [w for w in body.split() if w]
print(' '.join(w if w.startswith('#') else '#' + w for w in words))
PYEOF
)

say "==================================================="
say "  小红书图文笔记 · 一键发布"
say "==================================================="
say "封面图  : $IMAGE"
say "引用链接: ${QUOTE:0:60}…"
say "@账号   : $AT"
say "话题    : $TOPICS"
say "日志    : $LOG"
say "---------------------------------------------------"

if [ -z "$QUOTE" ] || [ -z "$TOPICS" ] || [ -z "$AT" ]; then
  say "❌ 2说明 里缺引用链接 / 话题 / @账号，解析失败。"
  say "   当前解析结果 → QUOTE='$QUOTE' TOPICS='$TOPICS' AT='$AT'"
  exit 1
fi

# ---- 前置：有没有正在跑的发布任务 ----------------------------------------
if pgrep -f "xhs_manual_publish.py" > /dev/null 2>&1; then
  say "⚠️  已经有一个发布任务在跑（PID $(pgrep -f xhs_manual_publish.py | head -1)）"
  say "   等它跑完再点，否则两个脚本会抢同一个 Chrome 窗口。"
  read -r -p "按回车退出…" _
  exit 1
fi

# ---- 跑 -------------------------------------------------------------------
# 注意：不要加 | tee / | grep —— 管道会让进程挂着不退出（见 skill 铁律二）
"$PY" "$SCRIPT" \
  --image "$IMAGE" \
  --quote "$QUOTE" \
  --topics "$TOPICS" \
  --at "$AT" \
  $REGEN $NO_PUBLISH \
  --upto 7 > "$LOG" 2>&1
RC=$?

say "---------------------------------------------------"
if [ $RC -eq 0 ]; then
  say "✅ 发布流程跑完（第 1 → 7 步）"
  if [ -n "$NO_PUBLISH" ]; then
    say "   ⚠️ --no-publish：停在发布前，笔记还没发出去。"
  fi
  TITLE=$(cat /tmp/xhs_gen_title.txt 2>/dev/null | head -1)
  BODY_N=$(python3 -c "
import pathlib
p=pathlib.Path('/tmp/xhs_gen_body.txt')
print(len(p.read_text(encoding='utf-8').replace(chr(10),'')) if p.exists() else 0)" 2>/dev/null)
  say "   标题: ${TITLE:-（未生成）}"
  say "   正文: ${BODY_N:-0} 字"
  say ""
  say "📋 日志全文：$LOG"
  say ""
  read -r -p "按回车关闭本窗口…" _
else
  say "❌ 卡住了（退出码 $RC）。日志里最后一段有现场 OCR 和截图路径："
  say ""
  tail -32 "$LOG"
  say ""
  say "📋 完整日志：$LOG"
  say "🖼️  截图目录：/tmp/xhs_manual_stuck_step*.png"
  say ""
  read -r -p "按回车关闭本窗口…" _
fi
exit $RC