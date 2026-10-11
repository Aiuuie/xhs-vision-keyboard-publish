#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小红书图文笔记发布（手工上传图片版）—— 一条命令跑完。

链路（2026-10-07 实测可用，跳过原创声明）：

    上传图文 → 1封面.png → 引用笔记(他人) → 填标题 → 填正文(剪贴板粘贴)
    → 话题(逐话题+空格) → @账号名(点候选) → 发布 → 回笔记管理验证

与 xhs_publish.py 的区别：那条走「文字配图」，这条走「上传图片 + 引用笔记」，
是用户当前实际在用的手工链路，固化成脚本。

用法（解释器必须是装了 PaddleOCR 的 venv）：

    PY=venv/bin/python
    $PY xhs_manual_publish.py --image "封面.png" \
        --title "示例标题" \
        --body-file /tmp/xhs_body_clean.txt \
        --topics "#话题1 #话题2 #话题3" \
        --at "你的账号名" \
        --quote "https://www.xiaohongshu.com/explore/69ca06c3000000002003bfb4?xsec_token=...&xsec_source=app_share"

    # 常用参数
    --no-publish      跑到发布前停手
    --upto N          只跑到第 N 步
    --dry-run         只 OCR 当前页，不点任何东西
    --skip-original   跳过原创声明（当前默认行为，旧版脚本才走原创声明）

纪律：任何一步没验证通过就立刻停，打印现场（OCR 命中 + 截图路径），exit 2。
每步的「提示词」（定位关键字/像素判据）集中在 STEP_HINTS 里。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui_ocr_click as U  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# ---------------------------------------------------------------------------
# 每步的提示词 / 判据（固化在这里，改流程只改这张表）
# ---------------------------------------------------------------------------
STEP_HINTS = {
    "upload_img": "点『上传图片』→ GTK『打开文件』对话框 → Ctrl+L 粘贴图片绝对路径 → Return。通过判据：对话框关、出现『图片编辑』",
    "quote_note": "点『引用笔记』→ 弹窗『选择笔记』→ 点『他人笔记』tab → 粘贴链接 → 出卡片 → 点卡片左图 → 出现『引用@』→ 点『确认引用』。通过判据：底部出现『引用笔记《标题》』",
    "title": "点标题框（占位『填写标题会有更多赞哦』）→ Ctrl+A → 输入标题。通过判据：OCR 读到标题 5 字片段",
    "body": "点正文框（占位『输入正文描述…』）→ Ctrl+A → Ctrl+V 粘贴（剪贴板带换行）。通过判据：逐段 OCR 确认，段间有空行",
    "topics": "光标文末 → Return → 逐『type #话题 + space』×10。通过判据：话题行蓝色像素>0 且深色像素=0",
    "at": "type '@账号名' → 等『头像+名称』候选 → 点带头像那一行。通过判据：候选收起、@账号名 变蓝",
    "publish": "点底部『发布』（exact，避开左栏『发布笔记』）。通过判据：地址栏出现 published=true",
    "verify": "左栏『笔记管理』→ 列表出现新笔记标题",
}

# 卡住时的现场提示（每步给排查方向）
STUCK_HINTS = {
    "upload_img": "对话框没弹？看 wmctrl -l 有没有『打开文件』。图片没上去？OCR 找『图片编辑』。",
    "quote_note": "链接没解析？OCR 看卡片标题。卡片没选中？点卡片左图，看『引用@』提示。确认按钮灰？先选中卡片。",
    "title": "标题没进去？Ctrl+A 后输入，OCR 验证 5 字片段。",
    "body": "正文没粘贴？剪贴板先写入（xclip -selection clipboard < file），Ctrl+V。段间没空行 = 用了 type 没用粘贴。",
    "topics": "话题没成标签？fcitx-remote 查状态，中文态(2)先 fcitx-remote -c。有深色像素 = 有纯文本没空格。",
    "at": "候选是头像+名称一行，点带头像那行，别点文字本身。",
    "publish": "点错了跳上传页？内容会被暂存成草稿（草稿箱计数+1），只能重来。地址栏应出现 published=true。",
    "verify": "笔记管理没看到？可能还在审核中，等几秒刷新。",
}


class Stuck(RuntimeError):
    pass


XHS_CREATOR_URL = "https://creator.xiaohongshu.com/publish/publish?source=toolbar"


def ensure_window(keyword: str = "小红书", timeout: float = 45.0) -> int:
    """找到 Chrome 窗口；找不到就自动打开小红书创作服务平台网页并等窗口出现。

    这是第一步：网页要能打开、登录态要还在，否则后面全白搭。
    """
    try:
        return U.find_window(keyword)
    except RuntimeError:
        pass
    disp = os.environ.get("DISPLAY") or ":0"
    subprocess.Popen(
        ["google-chrome", "--new-window", XHS_CREATOR_URL],
        env={**os.environ, "DISPLAY": disp},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[0] 没找到 Chrome 窗口 → 已自动打开发布页 ({disp})，等窗口出现……", flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        try:
            wid = U.find_window(keyword)
            print(f"[0] 窗口出现了 ({time.time() - t0:.1f}s)，接着往下走。", flush=True)
            return wid
        except RuntimeError:
            continue
    raise RuntimeError(
        f"自动打开后 {timeout:.0f}s 内还没出现「{keyword}」窗口；"
        "多半是登录态失效/首次启动被挡在登录页。请人工确认浏览器里的状态后重跑。")


SPEC_FILE = os.environ.get("XHS_SPEC", str(Path(__file__).resolve().parent / "2说明"))
LLM_BASE = os.environ.get("XHS_LLM_BASE", "https://your-llm-endpoint/v1")
LLM_MODEL = os.environ.get("XHS_LLM_MODEL", "your-model")
# 部分 LLM 会先出 reasoning_content 再给 content，max_tokens 给小了 content 会被吃空
# （实测 max_tokens=30 → content 空；给到 120/2000 就正常）
LLM_KEY_ENV = "XHS_API_KEY"
# 生成物落在磁盘，随时能人工改/复用，也让 step3/step8 能读回真实标题
GEN_TITLE = "/tmp/xhs_gen_title.txt"
GEN_BODY = "/tmp/xhs_gen_body.txt"


def find_dialog(candidates: tuple[str, ...] = (":0", ":99")) -> tuple[str, str] | None:
    """在多个 X display 里找「打开文件/选择文件」对话框，返回 (wid, disp)。

    无头环境 Chrome 的 portal 文件选择器会弹到**物理屏 :0**（portal 连系统
    session dbus），而脚本主体跑在 :99。单 display 的 wmctrl 查不到对端的窗口
    （2026-10-11 实测：脚本在 :99 等对话框 4 次都等不到，其实对话框在 :0）。
    注意窗口 ID 在不同 display 之间可能重复，所以必须按 display 分组返回。
    """
    for disp in candidates:
        env = {**os.environ, "DISPLAY": disp}
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True,
                             env=env, timeout=10).stdout
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) == 4 and ("打开文件" in parts[3] or "选择文件" in parts[3]):
                return parts[0], disp
    return None


def read_spec(path: str = SPEC_FILE) -> dict:
    """解析「2说明」：取 标题要求 / 正文要求 / 话题 / @ / 引用链接。

    文件是自然语言的人肉需求，不是结构化配置，所以按标签前缀切。
    """
    txt = Path(path).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in txt.splitlines():
        for key, tag in (("title", "标题"), ("body", "正文"),
                         ("topics", "话题"), ("at", "结尾需@"),
                         ("quote", "他人笔记引用链接")):
            if line.strip().startswith(tag) or f"{tag}：" in line:
                val = line.split("：", 1)[1].strip() if "：" in line else ""
                if key == "at":
                    val = val.lstrip("@").strip()
                if val and key not in out:
                    out[key] = val
                break
    return out


def llm(system: str, user: str, max_tokens: int = 1200,
        temperature: float = 0.9) -> str:
    """调 LLM 生成文本。走 OpenAI 兼容 /v1/chat/completions。"""
    import json
    import urllib.request
    key = os.environ.get(LLM_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"环境变量 {LLM_KEY_ENV} 没设，取不到 API key")
    payload = json.dumps({
        "model": LLM_MODEL, "temperature": temperature, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        f"{LLM_BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()


MAX_TITLE_CHARS = 20         # 标题硬规则：20 字以内（用户 2026-10-07 指定）


def gen_title(spec: dict) -> str:
    """按「标题要求」生成一条标题。AI 直出，只要标题本身。

    硬规则：≤MAX_TITLE_CHARS 字。超了先重试一次（让 AI 自己收短），
    仍超就报错停下 —— 不用字符串硬截断，那会把标题砍成半句。
    """
    sys_p = (f"你是小红书标题编辑。严格只输出标题本身，不要引号、不要解释、不要多个备选、"
             f"不要任何前后缀。遵守用户给的所有要求（风格、关键词、禁违规）。"
             f"**标题总字数必须 ≤{MAX_TITLE_CHARS} 字**（emoji 也算 1 字），写紧凑。")
    user = spec.get("title", "") + f"\n\n只输出一条标题，总字数不超过 {MAX_TITLE_CHARS} 字。"

    def clean(s: str) -> str:
        return s.strip().strip('"「」『』').split("\n")[0].strip().lstrip("#＃").strip()

    # ⚠ 实测过：部分 LLM 偶发返回**空 content** —— 它先出 reasoning_content，
    #   思考太长把 max_tokens 吃光，content 就空了（实测标题生成出空字符串，
    #   缓存文件 0 字节，笔记标题框留白）。所以空结果必须重试，不能直接用。
    t = ""
    for attempt, mt in enumerate((400, 800, 1200)):
        t = clean(llm(sys_p, user, max_tokens=mt, temperature=1.0))
        if t:
            break
        print(f"[标题] 第 {attempt + 1} 次返回空（max_tokens={mt}），加大额度重试")
        time.sleep(1.0)
    if not t:
        raise RuntimeError(
            "AI 连续 3 次返回空标题（LLM 的 reasoning 吃光了 max_tokens）。"
            "请稍后重试，或把 gen_title 里的 max_tokens 上限再调大。")

    if len(t) <= MAX_TITLE_CHARS:
        return t
    # 第一次超了：把当前标题回给它，让它自己收短
    sys2 = sys_p + " 这次太长了，请重新写一条更短的。"
    t2 = clean(llm(sys2, f"你上一条标题是：{t}\n请重新写一条 ≤{MAX_TITLE_CHARS} 字的标题，只输出标题。",
                    max_tokens=1200, temperature=0.7))
    if not t2:
        raise RuntimeError(f"AI 收短标题时返回空（原标题 {len(t)} 字『{t}』）。请稍后重试。")
    if len(t2) <= MAX_TITLE_CHARS:
        return t2
    raise RuntimeError(
        f"AI 连两次都没把标题控制在 {MAX_TITLE_CHARS} 字内：{len(t)} 字『{t}』 / "
        f"{len(t2)} 字『{t2}』。请在 2说明 里收紧字数要求，或调大 MAX_TITLE_CHARS 后重试。")


def _clean_paras(text: str) -> str:
    """把 LLM 输出规整成「段落之间恰好一个空行」的纯文本。

    去掉 markdown 标题行（# 开头，但话题行如「#话题」不在正文里出现，
    仍按 markdown 处理掉）、压缩连续空行、去掉首尾空白。
    """
    paras: list[str] = []
    for p in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in p.splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        s = "\n".join(lines).strip()
        if s:
            paras.append(s)
    return "\n\n".join(paras)


def gen_body(spec: dict) -> str:
    """按「正文要求」生成正文。段落之间用空行分隔（粘贴后才有段间距）。"""
    sys_p = ("你是小红书笔记写手。直接输出正文纯文本，段落之间用**空行**分隔。"
             "不要输出标题、不要 markdown 语法、不要解释、不要『以下是』之类的前缀。"
             f"**正文总字数必须控制在 {MAX_BODY_CHARS} 字以内**（不含换行），"
             "写紧凑，不要铺垫过长，段落少而有力。"
             "严格遵守用户给的所有要求（人设、语气、是否带引流段等）。")
    user = spec.get("body", "") + f"\n\n直接输出正文纯文本，总字数不超过 {MAX_BODY_CHARS} 字。"
    out = _clean_paras(llm(sys_p, user, max_tokens=3000, temperature=1.0))
    return _enforce_length(out, MAX_BODY_CHARS)


MAX_BODY_CHARS = 300          # 平台正文上限；脚本硬规则：超过就停


def _enforce_length(body: str, limit: int = MAX_BODY_CHARS) -> str:
    """正文必须 ≤limit 字（不含换行）。超了先按段落丢，仍超就报错 —— 不静默截断。

    为什么不用字符串截断：会把最后一段砍成半句，正文被腰斩比超字更难看。
    为什么不给 AI 重写：正文在 step4 已经粘贴进页面了，改来改去容易和页面内容不一致；
    所以生成阶段就压好，粘贴阶段不再动长度。
    """
    n = len(body.replace("\n", ""))
    if n <= limit:
        return body
    paras = body.split("\n\n")
    # 从后往前丢段落：保留开头完整（开头承接标题，是最重要的钩子）
    while len(paras) > 1:
        paras.pop()
        cand = "\n\n".join(paras)
        if len(cand.replace("\n", "")) <= limit:
            print(f"[字数] 正文 {n} 字超限，按段落精简到 "
                  f"{len(cand.replace(chr(10), ''))} 字（丢弃末尾 {n - len(cand.replace(chr(10), ''))} 字）")
            return cand
    raise RuntimeError(
        f"AI 生成的正文 {n} 字，超出上限 {limit}；压到只剩 1 段仍超。"
        f"请在 2说明 里收紧长度要求，或调大 MAX_BODY_CHARS 后重试。")


class Runner:
    def __init__(self, keyword: str = "小红书", min_score: float = 0.5,
                 spec: str | None = None, regen: bool = False):
        self.min_score = min_score
        self.spec_path = spec or SPEC_FILE
        self.regen = regen
        self.wid = self.pick_window(keyword)
        self.w = U.window_info(self.wid)
        self.log: list[dict] = []
        self.t0 = time.time()
        self._at_key: str | None = None    # step6 用：账号名比对键（去掉易错的末字）
        self.last_blocks: list[U.TextBlock] = []
        self.last_img: Image.Image | None = None
        self.editor_bottom: int | None = None   # step5 记正文区最下沿，给 step6 定候选带
        self.topic_row: int | None = None       # step5 记话题行 y，给 step6 定位候选
        self.body_anchor_xy: tuple[int, int] | None = None  # step4 记正文框位置

    # 「已经进入编辑页」的特征字：任一命中即认为这个窗口才是要操作的那个
    EDITOR_MARKS = ("引用笔记", "填写标题会有更多赞哦", "输入正文描述", "图片编辑")
    # 左栏编辑区的右边界：x≥1065 是右边手机预览，它把同一篇正文又渲染一遍，
    # OCR 会读到两套一样的块。所有找正文/话题的扫描都必须卡在这个 x 以内。
    EDIT_X_MAX = 1050

    @classmethod
    def pick_window(cls, keyword: str) -> str:
        """多个小红书窗口时挑『已经进到编辑页』的那个。

        2026-10-07 踩过：step1 新开窗口后有两个同名窗口，find_window 按顺序抓到
        那个空白的，step2 就会在错误的窗口里点。每个 --step N 是新进程，
        所以每一步都得自己重新认窗口。
        """
        cands = [(w, t) for w, t in U.list_windows()
                 if keyword in t and "Chrome" in t
                 and not any(f in t for f in (".md", ".txt", ".py", "(~/"))]
        if not cands:
            return ensure_window(keyword)
        if len(cands) > 1:
            for wid, _t in cands:
                try:
                    blocks, _ = U.ocr_blocks(wid, region=(250, 150, 1150, 1200),
                                             min_score=0.5)
                except Exception:
                    continue
                joined = "".join(b.text for b in blocks)
                if any(m in joined for m in cls.EDITOR_MARKS):
                    print(f"[窗口] 有 {len(cands)} 个小红书窗口，认出发布编辑页那个：{wid}")
                    return wid
            print(f"[窗口] {len(cands)} 个小红书窗口都没有编辑页特征，取第一个：{cands[0][0]}")
        return cands[0][0]

    def say(self, msg: str) -> None:
        print(f"[{time.time() - self.t0:6.1f}s] {msg}", flush=True)

    def record(self, step: int, name: str, ok: bool, **kw) -> None:
        self.log.append({"step": step, "name": name, "ok": ok,
                         "t": round(time.time() - self.t0, 2), **kw})

    def scan(self, region=None) -> list[U.TextBlock]:
        blocks, img = U.ocr_blocks(self.wid, region=region, min_score=self.min_score)
        self.last_blocks, self.last_img = blocks, img
        return blocks

    def seen(self, limit: int = 40) -> list[str]:
        return [b.text for b in self.last_blocks][:limit]

    def stuck(self, step: int, name: str, why: str) -> None:
        p = f"/tmp/xhs_manual_stuck_step{step}.png"
        try:
            if self.last_img is not None:
                self.last_img.save(p)
            else:
                U.screenshot(self.wid, p)
        except Exception:
            pass
        self.record(step, name, False, why=why)
        print("\n" + "=" * 60)
        print(f"卡在第 {step} 步「{name}」：{why}")
        print(f"提示词（这步该找什么）：{STEP_HINTS.get(name, '')}")
        print(f"排查方向：{STUCK_HINTS.get(name, '')}")
        print(f"现场 OCR：{self.seen()}")
        print(f"现场截图：{p}")
        print("已停下，不做重试也不换方案 —— 交人判断。")
        print("=" * 60)
        self.dump_log()
        raise Stuck(why)

    def dump_log(self, path: str | None = None) -> str:
        path = path or os.environ.get("XHS_LOG_DIR", "logs") + f"/xhs_manual_{int(time.time())}.json"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(
            {"window": {"wid": self.wid, "origin": self.w.origin, "size": self.w.size},
             "total_s": round(time.time() - self.t0, 2), "steps": self.log},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def click(self, keyword: str, exact=False, why: str = "") -> dict:
        self.say(f"  点『{keyword}』{why}")
        # 小红书按钮 hover/press 基本无反馈（已实测），一律 --no-verify；
        # human=False 鼠标瞬移到目标，不画弧线（用户在看屏幕，飘动会干扰）
        r = U.click_text(self.wid, keyword, exact=exact, verify=False,
                         human=False, min_score=self.min_score, settle=1.0)
        if not r.get("clicked"):
            raise Stuck(f"『{keyword}』点击被跳过：{r.get('skipped')}")
        return r

    def click_xy(self, xy, why: str = "") -> None:
        self.say(f"  点像素 {xy}{why}")
        U.click_at(xy[0], xy[1], human=False)
        time.sleep(0.8)

    def abs_of(self, ix: int, iy: int) -> tuple[int, int]:
        return (self.w.origin[0] + self.w.border + ix,
                self.w.origin[1] + self.w.border + iy)

    def wait_texts(self, want: list[str], regions: list[tuple], timeout: float = 25.0):
        deadline = time.time() + timeout
        while True:
            for r in list(regions) + [None]:
                blocks = self.scan(region=r)
                for kw in want:
                    hit = U.find_blocks(blocks, kw, min_score=self.min_score)
                    if hit:
                        return kw, blocks
            if time.time() > deadline:
                return None, self.last_blocks
            time.sleep(2.0)

    def text_present(self, probe: str, regions, timeout: float = 12.0) -> bool:
        """任意 5 字连续片段命中（OCR 会丢首字）。"""
        p = probe.replace(" ", "")
        n = min(5, len(p))
        cands = [p[i:i + n] for i in range(0, max(len(p) - n + 1, 1))]
        end = time.time() + timeout
        while True:
            for reg in regions:
                blocks = self.scan(region=reg) if reg else self.scan()
                joined = "".join(b.text for b in blocks).replace(" ", "")
                for c in cands:
                    if c in joined:
                        return True
            if time.time() >= end:
                return False
            time.sleep(1.5)

    def pixel_topics_ok(self, band: tuple[int, int, int, int]) -> tuple[bool, int, int]:
        """话题行判据：蓝色像素>0 且深色像素=0 = 全转成标签。"""
        a = np.asarray(self.last_img, dtype=np.int16)[band[1]:band[3], band[0]:band[2]]
        blue = ((a[:, :, 2] > a[:, :, 0] + 40) & (a[:, :, 2] > a[:, :, 1] + 20)).sum()
        dark = ((a[:, :, 0] < 100) & (a[:, :, 1] < 100) & (a[:, :, 2] < 100)).sum()
        return (blue > 0 and dark == 0), int(blue), int(dark)

    def fcitx_english(self) -> None:
        """把输入法切到英文直通态（中文态 2 时空格会被吞）。"""
        try:
            st = subprocess.run(["fcitx-remote"], capture_output=True, text=True, timeout=5).stdout.strip()
            if st == "2":
                subprocess.run(["fcitx-remote", "-c"], timeout=5)
                time.sleep(0.5)
                self.say("  fcitx 在中文态 → 已切英文直通")
        except Exception:
            pass  # 没有 fcitx 就忽略，空格直通

    def page_is_logged_in(self, wid: str) -> bool:
        """这个窗口当前是否已登录（不在登录页）。

        判据：登录页会出现「短信登录 / 手机号 / 验证码」这些字；已登录的发布页不会。
        """
        try:
            blocks, _ = U.ocr_blocks(wid, min_score=0.5)
        except Exception:
            return False
        joined = "".join(b.text for b in blocks)
        return not any(k in joined for k in ("短信登录", "收不到验证码", "验证码", "登录即同意"))

    def find_logged_in_window(self, keyword: str = "小红书") -> str | None:
        """找一个**已登录**的小红书窗口（2026-10-07 用户要求「改成复用」）。

        为什么：每次新开窗口都可能停在登录页（`redirectReason=401`），要人重新登。
        复用已登录的那个窗口就不用反复登录 —— 登录态在小红书的服务端 cookie 里，
        只要不是新开的隐私窗口就一直在。
        """
        for wid, t in U.list_windows():
            if keyword not in t or "Chrome" not in t:
                continue
            if any(f in t for f in (".md", ".txt", ".py", "(~/")):
                continue
            if self.page_is_logged_in(wid):
                return wid
        return None

    def reopen_chrome(self, timeout: float = 40.0) -> None:
        """第 1 步的头：**优先复用已登录的窗口**，没有才新开一个。

        2026-10-07 用户要求「改成复用」：原来每次都 `--new-window`，而新窗口
        常常停在登录页，要人反复短信登录。现在：
        1. 先找一个已登录的小红书窗口 → 直接用它（不再新开，窗口也不会越开越多）；
        2. 找不到 → 才新开一个（这时提示可能要登录）。

        不杀已有窗口 —— 用户可能有别的标签页在用。
        """
        reuse = self.find_logged_in_window()
        if reuse:
            info = U.window_info(reuse)
            self.wid, self.w = reuse, info
            self.say(f"  复用已登录的小红书窗口 {reuse}：{info.title}")
            U.activate(reuse)
            time.sleep(1.2)
            return

        before = {w for w, t in U.list_windows() if "Chrome" in t}
        self.say(f"  没找到已登录窗口（已有 Chrome {len(before)} 个）→ 新开一个发布页")
        env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY") or ":0"}
        subprocess.Popen(["google-chrome", "--new-window", XHS_CREATOR_URL],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(1.5)
            now = {w for w, t in U.list_windows() if "Chrome" in t}
            fresh = now - before                     # 只认这次新冒出来的窗口
            for wid in sorted(fresh):
                info = U.window_info(wid)
                if "小红书" in info.title and info.size[0] > 400:
                    self.wid, self.w = wid, info
                    U.activate(wid)
                    time.sleep(2.5)
                    if not self.page_is_logged_in(wid):
                        raise RuntimeError(
                            f"新开的窗口 {wid} 停在登录页。\n"
                            f"请在这个 Chrome 窗口里完成短信登录，登录后重跑即可 ——\n"
                            f"下次脚本会直接复用这个已登录的窗口，不会再新开。")
                    self.say(f"  新窗口就绪且已登录 ({time.time() - t0:.1f}s)：{info.title}")
                    return
        raise RuntimeError(
            f"新开 Chrome 窗口后 {timeout:.0f}s 内没找到新的小红书窗口"
            f"（新出现的 Chrome 窗口：{sorted(now - before)}）")

    # ---------------------------------------------------------------- 步骤
    def step1_upload_image(self, image_path: str) -> None:
        self.say("① 重开 Chrome → 上传图片")
        self.reopen_chrome()
        # 入口有几种，最终都收敛到「文件对话框出现 → 粘贴路径」：
        #  A. 发布页已有『上传图片』            → 点它弹框
        #  B. 主页顶栏『上传图文』              → 点它进发布页 → 点『上传图片』弹框
        #  C. 主页快捷卡『发布图文笔记』         → 点它直接弹框（openFilePicker=true）
        #  D. 其他页（笔记管理等）             → 点左栏『首页』回主页再走 B/C
        for attempt in range(4):
            blocks = self.scan()
            if U.find_blocks(blocks, "上传图片", min_score=self.min_score):
                self.say("  A: 已在发布页，点『上传图片』")
                self.click("上传图片", why="(--no-verify)")
                time.sleep(2.0)
            elif U.find_blocks(blocks, "上传图文", min_score=self.min_score):
                self.say("  B: 点『上传图文』进发布页")
                self.click("上传图文", why="(主页入口)")
                time.sleep(3.0)
                blocks = self.scan()
                if not U.find_blocks(blocks, "上传图片", min_score=self.min_score):
                    self.stuck(1, "upload_img", "点『上传图文』后没看到『上传图片』（可能没进发布页）")
                self.click("上传图片", why="(--no-verify)")
                time.sleep(2.0)
            elif U.find_blocks(blocks, "发布图文笔记", min_score=self.min_score):
                self.say("  C: 点首页快捷卡『发布图文笔记』（会直接弹文件框）")
                self.click("发布图文笔记", why="(首页快捷卡)")
                time.sleep(3.0)
            elif U.find_blocks(blocks, "首页", min_score=self.min_score):
                self.say("  D: 在其他页 → 点左栏『首页』回主页")
                self.click("首页", why="(左栏)")
                time.sleep(3.0)
                continue
            else:
                self.stuck(1, "upload_img", "当前页找不到任何可用入口（上传图片/上传图文/发布图文笔记/首页）")
            # 等文件对话框出现（无头环境弹在 :0，必须跨 display 查）
            found = find_dialog()
            if found:
                dlg_wid, ddisp = found
                break
            time.sleep(1.5)
        else:
            self.stuck(1, "upload_img", "点了上传入口但 4 次都没等到文件对话框（wmctrl -l 确认）")
        # GTK 文件对话框：粘贴路径（对话框弹在 ddisp，用它的 display 操作）
        if ddisp != os.environ.get("DISPLAY", ":0"):
            self.say(f"  文件对话框在 {ddisp}（不是本 DISPLAY），切换过去操作")
        denv = {**os.environ, "DISPLAY": ddisp}
        subprocess.run(["wmctrl", "-ia", dlg_wid], env=denv, timeout=10)
        time.sleep(0.5)
        subprocess.run(["xclip", "-selection", "clipboard"], input=image_path.encode(), env=denv, timeout=5)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+l"], env=denv, timeout=5)
        time.sleep(0.5)
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], env=denv, timeout=5)
        time.sleep(0.6)
        subprocess.run(["xdotool", "key", "Return"], env=denv, timeout=5)
        time.sleep(3.0)
        # 验证：对话框关 + 出现『图片编辑』（对话框可能在 :0，两个 display 都查）
        dlg_still = False
        for disp in (os.environ.get("DISPLAY", ":0"), ddisp):
            env = {**os.environ, "DISPLAY": disp}
            out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True,
                                 env=env, timeout=10).stdout
            if any(t in out for t in ("打开文件", "选择文件")):
                dlg_still = True
                break
        if dlg_still:
            self.stuck(1, "upload_img", "回车后文件对话框还在（路径可能没粘贴进去）")
        kw, _ = self.wait_texts(["图片编辑"], [(250, 150, 1150, 300), None], 15)
        if not kw:
            self.stuck(1, "upload_img", "图片上传后没出现『图片编辑』页")
        self.record(1, "upload_img", True, path=image_path)

    def step2_quote_note(self, quote_url: str) -> None:
        self.say("② 引用笔记")
        blocks = self.scan()
        if not U.find_blocks(blocks, "引用笔记", min_score=self.min_score):
            self.stuck(2, "quote_note", "发布页底部找不到『引用笔记』")
        self.click("引用笔记", why="(发布页底部)")
        time.sleep(2.0)
        # 弹窗：切『他人笔记』tab
        self.click("他人笔记", why="(tab)")
        time.sleep(2.0)
        # 粘贴链接
        subprocess.run(["xclip", "-selection", "clipboard"], input=quote_url.encode(), timeout=5)
        blocks = self.scan(region=(250, 250, 1150, 400))
        box = U.find_blocks(blocks, "请粘贴", min_score=self.min_score)
        if not box:
            box = U.find_blocks(blocks, "粘贴", min_score=self.min_score)
        if not box:
            self.stuck(2, "quote_note", "『他人笔记』tab 里找不到粘贴链接输入框")
        b = box[0]
        xy = (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
              self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)
        self.click_xy(xy, "(粘贴框)")
        subprocess.run(["xdotool", "key", "ctrl+v"], timeout=5)
        time.sleep(3.0)                      # 用户指定：贴完先等 3s 让卡片渲染
        # ── OCR 全屏认卡片文字，卡片上有字（第一轮卡住时现场 OCR 读到『淘宝五星好店』，
        #    说明卡片不是纯图，是我没用它）。点那块文字即可选中卡片。
        card_box, hit = self.find_card_by_text()
        if card_box is None:
            self.stuck(2, "quote_note",
                        "粘贴后等 3s，全屏 OCR 没读到卡片文字"
                        f"（锚点词：{self.CARD_MARKS}）。现场：{self.seen(30)}")
        self.say(f"  OCR 全屏认出卡片文字 {hit.text!r} @ img={hit.img_box}")
        card_img = self.card_thumb_xy(hit)
        self.click_xy(card_img, f"(卡片文字 {hit.text[:12]})")
        time.sleep(1.5)
        # ── 再 OCR：等『确认引用』变成大红色（选中后才激活）才点
        btn = self.wait_confirm_enabled(timeout=15.0)
        if btn is None:
            self.stuck(2, "quote_note",
                        f"点卡片({card_img})后『确认引用』一直没变红（卡片没选中）。"
                        f"现场：{self.seen(30)}")
        self.say(f"  OCR 认出『{btn.text}』已变红 @ img={btn.img_box}")
        self.click_xy(btn.abs_center, "(确认引用·已激活)")
        time.sleep(2.5)
        kw3, _ = self.wait_texts(["引用笔记《"], [(250, 1000, 1150, 1200), None], 15)
        if not kw3:
            self.stuck(2, "quote_note", "点『确认引用』后底部没出现『引用笔记《…》』条目")
        self.record(2, "quote_note", True, url=quote_url,
                    card_box=card_box, card_click=card_img,
                    confirm_img=btn.img_box)

    # ── 第 2 步的定位辅助 ──────────────────────────────────────
    # 卡片上**有文字**（2026-10-07 实测 OCR 全屏读到『淘宝五星好店』），
    # 所以直接 OCR 认文字块的位置来点卡片 —— 不要用像素，也不要写死坐标。
    CARD_MARKS = ("淘宝五星好店", "星星好店", "好店")

    def find_card_by_text(self):
        """全屏 OCR，找卡片上的文字，返回 (img_box, 命中的块)。找不到返回 (None, None)。"""
        blocks = self.scan()
        for mark in self.CARD_MARKS:
            hit = U.find_blocks(blocks, mark, min_score=0.4)
            if hit:
                b = hit[0]
                return b.img_box, b
        return None, None

    def card_thumb_xy(self, card) -> tuple[int, int]:
        """点卡片文字块的中心绝对坐标（点在卡片文字上即可选中整张卡片）。"""
        return (self.w.origin[0] + self.w.border + (card.img_box[0] + card.img_box[2]) // 2,
                self.w.origin[1] + self.w.border + (card.img_box[1] + card.img_box[3]) // 2)

    # 『确认引用』未选中时浅粉 RGB≈(246,218,220)，选中后**大红色** RGB≈(241,60,70)。
    # 判据用像素，不能只看 OCR —— OCR 认不出颜色。
    CONFIRM_BOX = (1000, 730, 1090, 765)

    def confirm_is_red(self) -> tuple[bool, tuple]:
        """OCR 定位『确认引用』按钮 → 取它那块像素判颜色是否变红。

        region 曾硬编码 (950,720,1150,780)，适配旧桌面布局（按钮 x≈1042）。
        无头 :99 窗口更宽，按钮右移到 x≈1442 —— 扫描区扫不到 → hit 空 →
        误报 RGB=(0,0,0) 卡死。放宽到整个弹窗底部 (600,700,1600,800)。
        """
        import numpy as np
        blocks = self.scan(region=(600, 700, 1600, 800))
        hit = [b for b in blocks if "确认引用" in b.text]
        if not hit:
            return False, (0, 0, 0)
        b = hit[0]
        a = np.asarray(self.last_img, dtype=np.int16)
        x1, y1, x2, y2 = b.img_box
        patch = a[max(y1, 0):y2, max(x1, 0):x2].reshape(-1, 3)
        if patch.size == 0:
            return False, (0, 0, 0)
        r, g, bl = patch[:, 0].mean(), patch[:, 1].mean(), patch[:, 2].mean()
        red = (r > 200 and g < 110 and bl < 120)     # 大红 vs 浅粉(g≈218)
        return red, (int(r), int(g), int(bl))

    def wait_confirm_enabled(self, timeout: float = 15.0):
        """轮询到『确认引用』变成大红色为止，返回那个按钮的文字块。"""
        end = time.time() + timeout
        last_rgb = (0, 0, 0)
        while True:
            red, rgb = self.confirm_is_red()
            last_rgb = rgb
            if red:
                blocks = self.scan(region=(600, 700, 1600, 800))
                hit = [b for b in blocks if "确认引用" in b.text]
                return hit[0] if hit else None
            if time.time() > end:
                self.say(f"  『确认引用』超时未变红，最后取样 RGB={last_rgb}")
                return None
            time.sleep(1.5)


    def ensure_title(self, given: str | None) -> str:
        """标题：优先用 --title 传的；没有就读 2说明 让 AI 现写，落盘缓存。

        落盘的意义：--step 3 单独重试时不会每次都重新生成（换一批文案），
        而且 step8 回查能读到同一个标题去核对。
        """
        if given:
            return given
        cached = Path(GEN_TITLE)
        if not self.regen and cached.exists() and cached.read_text(encoding="utf-8").strip():
            t = cached.read_text(encoding="utf-8").strip()
            self.say(f"  用已生成的标题（{cached}）：{t}")
            return t
        self.say("  读 2说明 → 让 AI 现写标题 …")
        t = gen_title(read_spec(self.spec_path))
        cached.write_text(t, encoding="utf-8")
        self.say(f"  AI 生成标题：{t}")
        return t

    def step3_title(self, title: str | None) -> None:
        title = self.ensure_title(title)
        self.say(f"③ 填标题：{title}")
        blocks = self.scan(region=(250, 300, 1150, 470))
        box = U.find_blocks(blocks, "填写标题", min_score=self.min_score)
        if box:
            b = box[0]
            xy = (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
                  self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)
        else:
            # 同 step4：标题已填过 → 占位符『填写标题会有更多赞哦』消失。
            # 点标题区里已有的文字（下一步是 Ctrl+A 全选重打，位置对就行）。
            filled = [b for b in blocks if 300 < b.img_box[1] < 470 and len(b.text) >= 4]
            if not filled:
                self.stuck(3, "title",
                            "找不到标题框占位符，标题区也没有已有文字可点"
                            f"（找的是 {title[:12]!r}）")
            b = filled[0]
            xy = (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
                  self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)
            self.say(f"  标题框已有内容（占位符已消失），点已有标题 {b.text[:14]!r} 重填")
        self.click_xy(xy, "(标题框)")
        subprocess.run(["xdotool", "key", "ctrl+a"], timeout=5)
        subprocess.run(["xdotool", "type", "--delay", "40", title], timeout=60)
        time.sleep(1.0)
        if not self.text_present(title, [(250, 350, 1150, 450), None], 10):
            self.stuck(3, "title", f"输入后没在标题框读到『{title[:12]}』")
        self.record(3, "title", True, title=title)

    def ensure_body_file(self, given: str | None) -> str:
        """正文：优先用 --body-file；没有就读 2说明 让 AI 现写，落盘成 txt。"""
        if given:
            return given
        cached = Path(GEN_BODY)
        if not self.regen and cached.exists() and cached.read_text(encoding="utf-8").strip():
            self.say(f"  用已生成的正文（{cached}，{len(cached.read_text(encoding='utf-8'))} 字）")
            return str(cached)
        self.say("  读 2说明 → 让 AI 现写正文 …")
        body = gen_body(read_spec(self.spec_path))
        cached.write_text(body, encoding="utf-8")
        self.say(f"  AI 生成正文 {len(body)} 字，已写 {cached}")
        return str(cached)

    def step4_body(self, body_path: str | None) -> None:
        body_path = self.ensure_body_file(body_path)
        self.say(f"④ 填正文（剪贴板粘贴）：{body_path}")
        blocks = self.scan(region=(250, 350, 1150, 700))
        box = U.find_blocks(blocks, "输入正文", min_score=self.min_score)
        if box:
            b = box[0]
            xy = (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
                  self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)
        else:
            # 没有占位符 = 框里已经有内容（重跑 step4 时必然如此，2026-10-07 踩过：
            # 上一轮粘贴成功后『输入正文描述…』消失，脚本判成「找不到正文框」而停下）。
            # 改成点编辑区里已有的正文块 —— 反正下一步就是 Ctrl+A 全选重贴。
            filled = [b for b in blocks if b.img_box[1] > 400 and len(b.text) >= 6]
            if not filled:
                self.stuck(4, "body", "找不到正文框占位符，编辑区里也没有已有正文可点")
            b = min(filled, key=lambda x: x.img_box[1])
            xy = (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
                  self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)
            self.say(f"  正文框已有内容（占位符已消失），点已有正文 {b.text[:14]!r} 重贴")
        self.body_anchor_xy = xy   # step5 靠它把光标送回正文区
        self.click_xy(xy, "(正文框)")
        subprocess.run(["xdotool", "key", "ctrl+a"], timeout=5)
        subprocess.run(["xdotool", "key", "Delete"], timeout=5)
        # 剪贴板粘贴（保持换行）
        subprocess.run(["xclip", "-selection", "clipboard"], input=Path(body_path).read_bytes(), timeout=5)
        subprocess.run(["xdotool", "key", "ctrl+v"], timeout=5)
        time.sleep(2.0)
        # 验证：拿文件里**多段**去比对，任一段命中即算粘贴成功。
        # 为什么不只看开头（2026-10-07 踩过）：正文超一屏时开头那行可能被
        # 折叠/换行/滚动出视野，OCR 读不到 → 把成功误判成失败。实测正文
        # 明明粘贴成功（计数器 277/1000），却因为开头 12 字读不到而卡住。
        raw = Path(body_path).read_text(encoding="utf-8")
        paras = [p.strip().replace("\n", " ") for p in raw.split("\n\n") if p.strip()]
        probes = [p for p in paras if len(p.replace(" ", "")) >= 8][:3]
        regions = [(250, 400, 1150, 900), None]
        hit = next((p for p in probes if self.text_present(p, regions, 6)), None)
        if not hit:
            self.stuck(4, "body",
                        f"粘贴后正文框里找不到任何一段（试了 {len(probes)} 段，各 {probes}）。"
                        f"字数计数器读到的现场：{self.seen(25)}")
        self.say(f"  命中正文段落：{hit[:20]}…")
        self.record(4, "body", True, chars=len(raw.replace("\n", "")),
                    paras=len(paras), matched=hit[:20])

    def body_anchor(self):
        """点正文**最后一段**（2026-10-07 用户指定：正文最后一个字点两次）。

        两个必须避开的坑（都是这轮实测踩的）：
        1. 正文下面有小红书的灰色推荐候选（#话题A/#话题B…），它们更靠下；
           点那里等于点空气，话题全打不进去。
        2. **右边 x≥1050 是手机预览区**，它把同一篇正文又渲染了一遍，
           OCR 会读到两套几乎一样的块。不排除它就会取到右栏的坐标，
           点在两栏中间的空白上 → 触发页面滚动 → 话题全丢（实测 0 个进）。
        所以：只在 x<1050 的左栏编辑区里找，取 y 最大的那段。
        """
        if self.body_anchor_xy:
            return self.body_anchor_xy
        blocks = self.scan(region=(250, 300, self.EDIT_X_MAX, 800))
        body = [b for b in blocks
                if b.img_box[1] > 300 and b.img_box[2] <= self.EDIT_X_MAX
                and len(b.text) >= 4
                and not b.text.lstrip().startswith("#")]
        if not body:
            return None
        b = max(body, key=lambda x: x.img_box[3])       # 正文最靠下那段 = 最后一段
        return (self.w.origin[0] + self.w.border + (b.img_box[0] + b.img_box[2]) // 2,
                self.w.origin[1] + self.w.border + (b.img_box[1] + b.img_box[3]) // 2)

    def topic_rows(self):
        """扫出左栏编辑区里所有「话题行」文字块（按 y 排序）。

        话题 = 以 # 开头、在正文下方、且不是小红书的灰色推荐候选。
        判据用 x 上限过滤掉右栏手机预览（它会把同样的内容再渲染一遍）。
        """
        blocks = self.scan(region=(250, 150, self.EDIT_X_MAX, 900))
        rows = [b for b in blocks
                if b.text.lstrip().startswith("#") and b.img_box[2] <= self.EDIT_X_MAX]
        # 灰色推荐候选在「#话题」「@用户」按钮那一带（y 更靠下且紧邻按钮）
        rows = [b for b in rows if b.img_box[1] > 150]
        rows.sort(key=lambda b: b.img_box[1])
        return rows

    def step456_body_topics_at(self, body_path: str | None,
                                topics: str | None, at_name: str | None) -> None:
        """第 4、5、6 步合并：正文 → 话题 → @账号（2026-10-07 用户要求合并）。

        为什么合：三步都在同一块编辑区、同一根光标上连续做 —— 粘完正文光标在文末，
        回车输话题，再接着输 @，中间不需要人确认。分开跑只是多几次状态交接，
        而每次交接都可能因为「重跑某一步」把页面搞乱（本轮已吃过苦头）。

        注意：这里**不做任何删除**。盲删 Backspace 是这轮最大的破坏源。
        要重来就重新粘正文覆盖，不要靠删。
        """
        # ── 正文
        self.step4_body(body_path)
        # ── 话题
        if topics:
            self.step5_topics(topics)
        # ── @账号
        if at_name:
            self.step6_at(at_name)

    def step5_topics(self, topics: str) -> None:
        self.say("⑤ 逐话题输入 + 空格确认")
        self.fcitx_english()  # 关键：中文态空格会被吞
        # 定位到正文最后一个字（2026-10-07 用户指定：点两次）
        xy = self.body_anchor()
        if not xy:
            self.stuck(5, "topics", "正文区找不到可点的文字块（无法定位光标）")
        self.click_xy(xy, "(正文最后一个字)")
        time.sleep(0.3)
        self.click_xy(xy, "(再点一次)")
        time.sleep(0.3)
        subprocess.run(["xdotool", "key", "ctrl+End"], timeout=5)
        time.sleep(0.3)
        # 回车两次另起一行（用户指定）
        subprocess.run(["xdotool", "key", "Return"], timeout=5)
        time.sleep(0.4)
        subprocess.run(["xdotool", "key", "Return"], timeout=5)
        time.sleep(0.5)
        for t in [x.strip() for x in topics.split("#") if x.strip()]:
            topic = "#" + t
            subprocess.run(["xdotool", "type", "--delay", "50", topic], timeout=30)
            time.sleep(0.6)
            subprocess.run(["xdotool", "key", "space"], timeout=5)
            time.sleep(0.6)
        time.sleep(1.5)
        # 验证：找话题行（topic_rows 内部已过滤右栏预览 + 按 y 排序）
        rows = self.topic_rows()
        if not rows:
            self.stuck(5, "topics",
                        f"话题输完没扫到任何以 # 开头的行。现场：{self.seen(30)}")
        b = rows[0]
        self.say(f"  认出 {len(rows)} 行话题，首行 {b.text[:36]!r}")
        band = (max(b.img_box[0] - 5, 0), max(b.img_box[1] - 5, 0),
                b.img_box[2] + 5, b.img_box[3] + 5)
        ok, blue, dark = self.pixel_topics_ok(band)
        self.topic_row = b.img_box[1]
        self.editor_bottom = b.img_box[3]
        if not ok:
            self.stuck(5, "topics",
                        f"话题行像素判据失败：蓝色{blue} 深色{dark}（有话题没空格成标签）。"
                        f"读到的话题行：{b.text[:40]!r}")
        self.record(5, "topics", True, blue=blue, dark=dark, row_text=b.text[:40])

    def _blue_dark(self, box) -> tuple[int, int]:
        """取一块区域里的蓝色像素数、深色像素数。

        用来判「是不是可点击的关联标签」：话题标签、@关联 都是蓝色；
        纯文本是深色/黑色。
        """
        a = np.asarray(self.last_img, dtype=np.int16)
        x1, y1, x2, y2 = box
        patch = a[max(y1, 0):max(y2, 0), max(x1, 0):max(x2, 0)]
        if patch.size == 0:
            return 0, 0
        blue = int(((patch[:, :, 2] > patch[:, :, 0] + 40)
                    & (patch[:, :, 2] > patch[:, :, 1] + 20)).sum())
        dark = int(((patch[:, :, 0] < 100) & (patch[:, :, 1] < 100)
                    & (patch[:, :, 2] < 100)).sum())
        return blue, dark

    def body_bottom_y(self) -> int:
        """左栏编辑区里**正文+话题标签**的最下沿 y（不含灰色推荐候选、按钮、页面其它区块）。

        ⚠ 连踩两次的坑，别再犯：
        1. 取全页最靠下的块 → 会把「内容设置/加入合集」这些页面下半部分区块算进来
           （算到 y=1083，而正文在 900 之前），基准离谱到匹配不到任何东西。
        2. 只排除「内容设置」还不够 → 剩下的最靠下块是**灰色推荐候选**
           （#漫画书/#旧书/#电子书…，本轮 y=851-874），它们不是正文也不是话题。
           真话题行在 809 附近，@ 就在它末尾（809-831）。
        所以判据：正文/话题 = 不含「#话题」按钮、不含推荐候选特征、
        且要在推荐候选行**上方**。推荐候选的识别：同一 y 带上有 ≥4 个以 # 开头的
        短词 → 那一带就是推荐候选，不是真话题行。
        """
        blocks = self.scan(region=(250, 150, self.EDIT_X_MAX, 900))
        HARD_SKIP = ("# 话题", "@用户", "表情", "更多", "内容设置", "收起",
                     "加入合集", "创建合集", "汇集系列", "原创声明")
        rows = [b for b in blocks
                if b.img_box[2] <= self.EDIT_X_MAX and b.img_box[1] > 150
                and not any(s in b.text for s in HARD_SKIP)]
        if not rows:
            return 150
        # 找出「推荐候选带」：某个 y 起 30px 内有 ≥4 个以 # 开头的块
        hashy = [b for b in rows if b.text.lstrip().startswith("#")]
        rec_top = None
        if len(hashy) >= 4:
            hashy.sort(key=lambda b: b.img_box[1])
            band = hashy[0].img_box[1]
            same = [b for b in hashy if b.img_box[1] < band + 30]
            if len(same) >= 4:
                rec_top = band
        if rec_top is None:
            return max(b.img_box[3] for b in rows)
        above = [b for b in rows if b.img_box[3] <= rec_top]
        return max((b.img_box[3] for b in above), default=rec_top)

    def editor_area_bottom(self) -> int:
        """编辑器可视区的下沿（页面里正文+话题+推荐候选+按钮那一整块的范围）。"""
        blocks = self.scan(region=(250, 150, self.EDIT_X_MAX, 1100))
        rows = [b for b in blocks if b.img_box[2] <= self.EDIT_X_MAX and b.img_box[1] > 150]
        return max((b.img_box[3] for b in rows), default=1100)

    # 「@用户」候选弹窗（2026-10-07）
    # 曾用像素阈值找它，连踩 5 次（阈值一换结论就变，详见 at_popup_rows 注释）。
    # 现在**只用 OCR 全屏幕读文字**定位，坐标来自 OCR 的 img_box/abs_center，不做任何像素估算。
    def at_popup_rows(self):
        """OCR 全屏幕，找出「@用户」候选弹窗里每一条候选的精确 img_box。

        ⚠ 2026-10-07 连踩 5 次的教训：不要再用像素阈值（深色像素计数）去找候选 ——
        阈值一换结论就变（同一个弹窗先后被认成 1 条、2 条、y 830-843 十几像素高的细条），
        每次点空或点错变体。**改成 OCR 全屏幕读文字** —— 候选里的账号名是文字，
        OCR 能读出来（读不出的是正文里的蓝色小标签，那不是这里要找的东西）。
        """
        blocks = self.scan()          # 全屏，不裁剪区域
        key = self._at_key
        hits = []
        for b in blocks:
            if key and key in b.text and b.img_box[1] > 150:
                hits.append(b)
        hits.sort(key=lambda b: b.img_box[1])
        # ⚠ 正文里刚输入的「@账号名」纯文本也会命中（img x≈375），
        #   候选弹窗在 x≈1040+。用它把两者分开，只保留候选。
        popup = [b for b in hits if b.img_box[0] > 900]
        if popup:
            return popup
        # 兜底：没识别到弹窗位置就全给（交给上层打印现场人工判断，不猜）
        return hits


    def step6_at(self, at_name: str) -> None:
        self.say("⑥ @账号名（点候选）")
        self.fcitx_english()
        # 光标已在话题行末尾，直接输入 @
        # 单独 --step 6 跑时（没有上一轮状态）先从页面反推话题行 y
        if self.topic_row is None:
            for b in self.scan(region=(250, 150, self.EDIT_X_MAX, 600)):
                if (b.text.lstrip().startswith("#") and b.img_box[1] > 150
                        and b.img_box[2] <= self.EDIT_X_MAX):
                    self.topic_row = b.img_box[1]
                    break
        subprocess.run(["xdotool", "type", "--delay", "50", "@" + at_name], timeout=30)
        time.sleep(2.5)
        key = at_name[:-1] if len(at_name) > 2 else at_name
        self._at_key = key
        # OCR 全屏幕等候选弹出（不裁区域、不用像素阈值 —— 见 at_popup_rows 注释）
        cands = None
        end_t = time.time() + 15.0
        while time.time() < end_t and not cands:
            cands = self.at_popup_rows()
            if not cands:
                time.sleep(1.5)
        if not cands:
            self.stuck(6, "at",
                        f"输入 @{at_name} 后 15s 内 OCR 全屏没读到候选弹窗里的『{key}』。"
                        f"现场：{self.seen(30)}")
        # ── 先把所有候选的精确位置打出来（只读，不动鼠标键盘），确认再点
        self.say(f"  OCR 全屏读到 {len(cands)} 处『{key}』：")
        for i, b in enumerate(cands):
            tag = "  ← 取这条" if i == 0 else ""
            self.say(f"    [{i}] {b.text!r} img={b.img_box} abs={b.abs_center}{tag}")
        b0 = cands[0]
        # 点第一条候选的文字块中心（弹窗里同账号可能有多条变体：
        # 「头像+名字」和「头像+脚印+名字」，取最上面那条）
        ax, ay = b0.abs_center
        self.click_xy((ax, ay), f"(候选 {b0.text[:14]})")
        time.sleep(2.0)
        # 验证①：候选必须消失（OCR 全屏再扫一次，读不到候选了才算点上）
        # ⚠ 2026-10-07 连踩两次的判据教训：
        #   · 用 x>900 过滤 → 误报：关联成功后 @ 会并进**正文话题行**
        #     （实测 img=(377,786,997,803)，右端 997>900），明明成功却报没点上。
        #   · 改用 y>800 → 又误报：正文里独立的 @ 在 **y=809**，同样 >800。
        # 唯一稳定的区分是 **x**：弹窗候选在 x≈1040+，正文/话题行里的 @ 在 x≈375。
        left = [b for b in self.scan()
                if self._at_key and self._at_key in b.text
                and b.img_box[0] > 900 and b.img_box[1] > 800]
        if left:
            self.stuck(6, "at",
                        f"点候选({ax},{ay})后弹窗区(x>900,y>800)还能读到 {len(left)} 处，没点上。"
                        f"位置：{[(x.text[:12], x.img_box) for x in left]}")
        self.say("  候选已消失 ✓")
        # 验证②：正文里的 @ 是蓝色关联（纯文本是深色）
        blocks = self.scan()
        atblk = [b for b in blocks if key and key in b.text]
        if not atblk:
            self.stuck(6, "at", f"点候选后找不到 @{key}。现场：{self.seen(30)}")
        b = atblk[0]
        blue, dark = self._blue_dark(b.img_box)
        self.say(f"  正文 @{key} @ img={b.img_box} 蓝={blue} 深色={dark}")
        if blue <= 0:
            self.stuck(6, "at",
                        f"正文 @{key} 无蓝色像素（蓝={blue}）= 纯文本没关联。现场：{self.seen(30)}")
        self.record(6, "at", True, at_name=at_name, key=key, abs_click=(ax, ay),
                    cand_text=b0.text[:24], blue=blue, dark=dark, at_img=b.img_box)

    def step7_publish(self) -> None:
        self.say("⑦ 发布")
        # 发布按钮在页面底部，OCR 定位。
        # ⚠ region 曾硬编码 (250,1250,1150,1420)，适配桌面大窗口（高≥1400）。
        # 无头 :99 窗口高 1233 —— 扫描区整个在窗口外，永远找不到『发布』。
        # 改为按窗口高度动态取底部 ~250px 横带。
        w = self.w.size or (1920, 1080)
        x1, y1, x2, y2 = 250, max(120, w[1] - 250), min(1150, w[0]), w[1]
        blocks = self.scan(region=(x1, y1, x2, y2))
        if not U.find_blocks(blocks, "发布", exact=True, min_score=self.min_score):
            self.stuck(7, "publish", "页面底部找不到『发布』按钮")
        r = self.click("发布", exact=True, why="(--exact --no-verify)")
        # ⚠ 2026-10-07 实测：小红书发布成功跳的是
        #   publish/publish?source=toolbar&**published=true**
        # 不是 publish/success。原先只等 publish/success，判据永远匹配不上，
        # 「通过」是蒙对的（wait_texts 超时后返回 None，但代码只判 kw 为假→报卡住…
        # 实测那次却在 0.3s 就过了，说明这里另有分支）。两种都收下。
        kw, _ = self.wait_texts(["published=true", "publish/success"],
                                [(150, 60, 1010, 150)], 25)
        if not kw:
            self.stuck(7, "publish",
                        "点『发布』后地址栏既没有 published=true 也没有 publish/success"
                        "（可能点错跳上传页，内容变草稿；查草稿箱计数）")
        self.record(7, "publish", True, abs_center=r["abs_center"])

    def step8_verify(self, title: str | None) -> None:
        self.say("⑧ 回笔记管理验证")
        if not title:                      # 没传就读第 3 步生成/缓存的那个标题
            p = Path(GEN_TITLE)
            title = p.read_text(encoding="utf-8").strip() if p.exists() else ""
        if not title:
            self.stuck(8, "verify", f"没有标题可比对（{GEN_TITLE} 不存在且没传 --title）")
        probe = title.replace(" ", "")[:8]
        try:
            self.click("笔记管理", why="(左栏)")
            time.sleep(3.0)
            self.click("已发布", why="(tab)")
            time.sleep(3.0)
        except Stuck:
            pass
        if not self.text_present(probe, [(250, 180, 1450, 900)], 20):
            self.stuck(8, "verify", f"笔记管理列表里没找到刚发的『{probe}』")
        self.record(8, "verify", True, probe=probe)

    # ---------------------------------------------------------------- 主流程
    def run(self, image: str, title: str | None, body_path: str | None,
            topics: str, at: str, quote: str, no_publish: bool, upto: int) -> None:
        self.say(f"窗口 {self.wid} '{self.w.title}' 原点={self.w.origin} 尺寸={self.w.size}")
        U.activate(self.wid)
        order = [
            (1, lambda: self.step1_upload_image(image)),
            (2, lambda: self.step2_quote_note(quote)),
            (3, lambda: self.step3_title(title)),
            # ④⑤⑥ 合并成一步（2026-10-07 用户要求）
            (6, lambda: self.step456_body_topics_at(body_path, topics, at)),
            (7, self.step7_publish if not no_publish
             else lambda: self.say("⑦ 发布 —— --no-publish，停在发布前")),
            (8, (lambda: self.step8_verify(title)) if not no_publish
             else lambda: self.say("⑧ 回查 —— --no-publish，跳过")),
        ]
        for n, fn in order:
            if upto and n > upto:
                self.say(f"已到 --upto {upto}，停。")
                break
            fn()
        self.say("全部完成 ✓")
        print(f"日志：{self.dump_log()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="小红书图文笔记发布（上传图片版，分步执行）")
    ap.add_argument("--image", help="封面图片绝对路径（step1 用）")
    ap.add_argument("--title", help="笔记标题（step3/8 用）；不给就读 2说明 让 AI 现写")
    ap.add_argument("--body-file", help="正文 txt（UTF-8，带换行=段落）（step4 用）；"
                                        "不给就读 2说明 让 AI 现写")
    ap.add_argument("--spec", default=SPEC_FILE,
                    help="需求文件（默认 2说明），step3/4 从这里读要求交给 AI")
    ap.add_argument("--regen", action="store_true",
                    help="忽略 /tmp 里的缓存，重新生成标题和正文")
    ap.add_argument("--topics", help="话题，空格分隔，如 '#a #b #c'（step5 用）")
    ap.add_argument("--at", default="", help="@ 的账号名（step6 用）")
    ap.add_argument("--quote", help="他人笔记引用链接（step2 用）")
    ap.add_argument("--step", type=int, default=0, help="只跑第 N 步（1-8）；0=全流程")
    ap.add_argument("--upto", type=int, default=0, help="全流程模式下只跑到第 N 步就停")
    ap.add_argument("--no-publish", action="store_true",
                    help="第 7、8 步都跳过，停在发布前")
    ap.add_argument("--skip-original", action="store_true",
                    help="兼容参数：本链路本来就跳过原创声明，留着不报错")
    ap.add_argument("--dry-run", action="store_true", help="只 OCR 当前页，不点任何东西")
    args = ap.parse_args()

    if args.dry_run:
        wid = U.find_window("小红书")
        blocks, img = U.ocr_blocks(wid, min_score=0.5)
        for b in blocks:
            print(f"  abs={b.abs_center} img={b.img_box} {b.text}")
        return 0

    STEPS = {
        1: ("上传图片", lambda r: r.step1_upload_image(args.image)),
        2: ("引用笔记", lambda r: r.step2_quote_note(args.quote)),
        3: ("填标题", lambda r: r.step3_title(args.title)),
        456: ("正文+话题+@账号（合并）",
              lambda r: r.step456_body_topics_at(args.body_file, args.topics, args.at)),
        7: ("发布", lambda r: (r.say("⑦ 发布 —— --no-publish，停在发布前")
                              if args.no_publish else r.step7_publish())),
        8: ("回查", lambda r: r.step8_verify(args.title)),
    }
    if args.step:
        if args.step not in STEPS:
            ap.error(f"--step 只能是 1,2,3,456,7,8（{args.step}）")
        name, fn = STEPS[args.step]
        # title / body-file 现在是**可选**的：不给就读 2说明 让 AI 现写（落盘缓存）。
        # 仍然必填的是需要外部素材的步骤。
        needed = {1: "image", 2: "quote", 456: "topics"}.get(args.step)
        if needed:
            v = getattr(args, needed.replace("-", "_"))
            if not v:
                ap.error(f"--step {args.step} 需要参数 --{needed}")
        r = Runner(spec=args.spec, regen=args.regen)
        try:
            print(f"=== 第 {args.step} 步：{name} ===")
            fn(r)
            print(f"=== 第 {args.step} 步完成 ✓ ===")
            r.dump_log()
            return 0
        except Stuck as e:
            print(f"\n卡住：{e}")
            return 2

    # 全流程模式。--title / --body-file **可选**：不给就读 2说明 让 AI 现写
    # （ensure_title / ensure_body_file 内部处理 + 落盘缓存）。
    missing = [n for n, v in [("--image", args.image), ("--topics", args.topics),
                              ("--quote", args.quote)] if not v]
    if missing:
        ap.error("以下参数必填: " + ", ".join(missing))
    r = Runner(spec=args.spec, regen=args.regen)
    try:
        r.run(args.image, args.title, args.body_file, args.topics,
              args.at, args.quote, args.no_publish, args.upto)
        return 0
    except Stuck as e:
        print(f"\n卡住：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
