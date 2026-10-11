# 小红书图文笔记自动发布 —— 视觉键鼠（支持无显示器）

> [English](README.en.md) | 中文

不用网页后端（不连 CDP、不注入 JS），纯 **OCR 认字 + 真实键鼠事件** 操作小红书创作服务平台，
自动发图文笔记。**无显示器环境（Xvfb 虚拟屏）已验证可跑。**

```
wmctrl 找窗口 → import 截图 → PaddleOCR 认字 → 坐标换算 → xdotool 点击/输入
```

## 功能

- 一键发布图文笔记：上传封面 → 引用他人笔记 → AI 现写标题/正文 → 话题 → @账号 → 发布
- 标题/正文由 LLM 现写（OpenAI 兼容端点，端点/模型/API key 全部走环境变量），落盘缓存可人工改
- 每步确定性通过判据，失败即停（打印现场 OCR + 截图路径），不猜不重试
- 分步调试：`--step 1/2/3/456/7`，一次一步
- 无头模式：Xvfb :99 虚拟屏 + openbox，物理显示器可有可无

## 快速开始

依赖：Linux + X11（`wmctrl` / `xwininfo` / `xdotool` / ImageMagick `import` / `xclip`）+
Chrome + Python venv（paddleocr + numpy + PIL）+ fcitx（无头环境可缺，自动忽略）。

```bash
PY=/path/to/venv/bin/python
S=/path/to/xhs_manual_publish.py

# 环境变量（必配，脚本不存任何 key）
export XHS_API_KEY=你的LLM_key        # 标题/正文由 AI 现写
export XHS_LLM_BASE=https://你的端点/v1
export XHS_LLM_MODEL=你的模型名

# 分步（一次一步，每步有通过判据）
DISPLAY=:0 $PY $S --step 1 --image 封面.png
DISPLAY=:0 $PY $S --step 2 --quote "https://www.xiaohongshu.com/explore/..."
DISPLAY=:0 $PY $S --step 3                  # 不给标题 → AI 现写
DISPLAY=:0 $PY $S --step 456 --topics "#话题1 #话题2 ..." --at "账号名"
DISPLAY=:0 $PY $S --step 7                  # 真发布
# --no-publish 停在发布前 / --regen 重新生成文案 / --spec 换需求文件
```

需求文件（默认脚本同目录 `2说明`，可用 `XHS_SPEC` 指别的路径）：写清楚标题要求、
正文要求、话题、@账号、引用链接，格式见仓库里的说明。

## 无头（无显示器）发布

```bash
Xvfb :99 -screen 0 2560x1440x24 &      # 分辨率与物理屏一致
DISPLAY=:99 openbox &                  # 必须有窗口管理器
# Chrome 用独立 profile（从已登录 profile rsync 复制，排除 Cache/Singleton）
env DISPLAY=:99 google-chrome --user-data-dir=~/.config/google-chrome-xvfb --new-window --no-first-run \
  "https://creator.xiaohongshu.com/publish/publish?source=toolbar"
export DISPLAY=:99
bash scripts/开始发布.sh --no-publish   # 试跑
bash scripts/开始发布.sh                # 真发
```

无头特有的坑（已修进脚本）：
1. **文件对话框弹到物理屏 :0**（Chrome 走 GTK portal，连系统 session bus）→ `find_dialog()`
   跨 display 找「打开文件」窗口，切到对话框所在 display 操作
2. **硬编码扫描区失效**（确认引用/发布按钮区域按桌面窗口尺寸写，小窗口扫不到）→ 放宽 / 动态取底部
3. **登录态别换 dbus session**（私有 bus 无 keyring → cookie 加密对不上）→ 必须用系统 session bus

## 目录

| 路径 | 说明 |
|---|---|
| `xhs_manual_publish.py` | 主编排脚本（8 步链路 + AI 文案生成 + 通过判据） |
| `ui_ocr_click.py` | 底层库：找窗口 / 截图 / OCR / 坐标换算 / 点击 / 对话框关闭 |
| `scripts/开始发布.sh` | 一键启动器（前置检查 → 解析需求 → 跑全流程 → 日志） |

## 配置（全部环境变量，无硬编码）

| 变量 | 作用 | 默认 |
|---|---|---|
| `XHS_API_KEY` | LLM API key（必配） | 无 |
| `XHS_LLM_BASE` | LLM OpenAI 兼容端点 | `https://your-llm-endpoint/v1` |
| `XHS_LLM_MODEL` | 模型名 | `your-model` |
| `XHS_SPEC` | 需求文件路径 | 脚本同目录 `2说明` |
| `XHS_LOG_DIR` | 日志目录 | `./logs` |
| `XHS_PY` / `XHS_SCRIPT` / `XHS_DIR` | 启动器用的解释器/脚本/工作目录 | 相对脚本目录 |

## 关键设计

- 不碰网页后端：CDP 可被页面反向探测（`navigator.webdriver` 等），合成 DOM 事件带
  `isTrusted=false`。这条链路从屏幕像素一侧下手，事件走 X11 输入管线（XTest），和真人同一条通路
- 坐标全部现测不写死（窗口原点每次变）；OCR 会丢首字，校验用「任意 5 字片段命中」
- 小红书按钮 hover/press 反馈基本没用 → 一律 `--no-verify`，真实验证靠「点完页面有没有变」
- 发布成功判据：地址栏出现 `published=true`（不是 `publish/success`）

## 安全说明

- 仓库不含任何 API key / token / 密码 / 账号信息 / 业务内容：LLM key 只从环境变量读
- 不含 OCR 模型文件（PaddleOCR 模型由 venv 首次运行时自动下载到本地）
- 不含用户数据（发布日志、Chrome profile、缓存）

## 已知限制

- 只跑 X11 / Xvfb；Wayland 需换 `grim` + `ydotool`
- 绑本机 Chrome 登录态；换机器/版式需重调判据
