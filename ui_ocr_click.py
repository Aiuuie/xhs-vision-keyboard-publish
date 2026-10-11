"""OCR 定位窗口内的文字 → 换算屏幕绝对坐标 → 发真实鼠标事件点击。

不碰网页后端（不连 CDP、不注入 JS）：整条链路是
    wmctrl 找窗口 → import 截图 → PaddleOCR 认字 → 坐标换算 → xdotool 点击

为什么不用网页后端：CDP 连接本身可以被页面反向探到；截图 + OCR + X11 输入事件
是从"屏幕像素"这一侧下手的，页面看不到自己被怎么操作的。

坐标口径（关键，踩过坑）
    `import -window WID` 抓的是窗口**内容区**，不含标题栏、不含窗口边框；
    `xwininfo -id WID` 的 "Absolute upper-left X/Y" 给的也正是内容区左上角。
    两者同源，所以  屏幕绝对坐标 = 内容区原点 + 截图内坐标。
    若 Border width > 0，截图会含边框，需要把边框宽加回去（模块里已处理）。

用法见文件末尾 CLI，例如：
    python ui_ocr_click.py --list
    python ui_ocr_click.py --window 小红书 --ocr
    python ui_ocr_click.py --window 小红书 --click "发布图文笔记"
    python ui_ocr_click.py --window 打开文件 --dismiss
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

_OCR = None


def _ocr_engine():
    """PaddleOCR 加载一次几秒，进程内复用。"""
    global _OCR
    if _OCR is None:
        from paddleocr import PaddleOCR
        _OCR = PaddleOCR(use_doc_orientation_classify=False,
                         use_doc_unwarping=False,
                         use_textline_orientation=False,
                         lang="ch")
    return _OCR


# ---------------------------------------------------------------- 窗口

@dataclass(frozen=True)
class Window:
    wid: str
    title: str
    origin: tuple[int, int]     # 内容区左上角（屏幕绝对）
    size: tuple[int, int]
    border: int = 0


def list_windows() -> list[tuple[str, str]]:
    out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append((parts[0], parts[3]))
    return rows


def find_window(keyword: str, index: int = 0) -> str:
    """按标题子串找窗口，返回 wid。index 用于同名多窗口时挑第几个。

    注意：子串匹配会把「小红书发布脚本-8步说明.md (~/Desktop)」这类
    文档窗口一起命中（2026-10-07 踩过：在 md 里点到「上传图片」，Chrome 根本没开）。
    所以先排掉编辑器/文件管理器窗口 —— 排完还剩 0 个就**报错**而不是回退，
    这样上层 ensure_window() 会去把 Chrome 打开，而不是对着 md 继续点。
    """
    hits = [(wid, t) for wid, t in list_windows() if keyword in t]
    if not hits:
        raise RuntimeError(f"没找到标题含 {keyword!r} 的窗口；当前有：" +
                           "; ".join(t for _, t in list_windows()))
    # 假窗口：文档编辑器 / 文件管理器 / 终端（标题带这些后缀或编辑器名）
    FAKE_EXT = (".md", ".txt", ".py", ".doc", ".docx", ".pdf", ".xlsx",
                ".csv", ".json", ".jpg", ".jpeg", ".png", "(~/", "(~")
    FAKE_APP = ("Visual Studio Code", "文本文档", "gedit", "Kate",
                "文件管理器", "Nautilus", "Terminal", "终端")
    real = [(wid, t) for wid, t in hits
            if not any(f in t for f in FAKE_EXT) and not any(a in t for a in FAKE_APP)]
    if not real:
        raise RuntimeError(
            f"标题含 {keyword!r} 的窗口只有编辑器/文件管理器，没有真浏览器窗口："
            + "; ".join(t for _, t in hits))
    return real[index][0]


def window_info(wid: str) -> Window:
    raw = subprocess.run(["xwininfo", "-id", wid], capture_output=True, text=True, check=True).stdout
    g: dict[str, int] = {}
    title = ""
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            if k in {"Absolute upper-left X", "Absolute upper-left Y", "Width", "Height",
                     "Border width"}:
                try:
                    g[k] = int(v.strip())
                except ValueError:
                    pass
    for line in raw.splitlines():
        if "xwininfo: Window id" in line and '"' in line:
            title = line.split('"')[1]
            break
    return Window(wid=wid, title=title,
                  origin=(g["Absolute upper-left X"], g["Absolute upper-left Y"]),
                  size=(g["Width"], g["Height"]), border=g.get("Border width", 0))


def activate(wid: str) -> None:
    """点之前一定要激活，否则某些窗口管理器会把手势吃掉用于聚焦。"""
    subprocess.run(["wmctrl", "-ia", wid], check=False, timeout=10)
    time.sleep(0.5)


# ---------------------------------------------------------------- 截图 / OCR

@dataclass(frozen=True)
class TextBlock:
    text: str
    score: float
    img_box: tuple[int, int, int, int]   # 截图内 x1,y1,x2,y2
    abs_center: tuple[int, int]          # 屏幕绝对坐标（文字块中心）

    @property
    def row_key(self) -> tuple[int, int]:
        return (self.img_box[1], self.img_box[0])


def screenshot(wid: str, path: str | None = None) -> Image.Image:
    path = path or f"/tmp/ocrclick-{wid.replace('0x', 'w')}.png"
    subprocess.run(["import", "-window", wid, path], check=True, timeout=30)
    img = Image.open(path).convert("RGB")
    return img


def _to_abs(w: Window, x: int, y: int) -> tuple[int, int]:
    # 截图含边框时，内容区在截图里的起点是 border；屏幕原点再加 border
    return (w.origin[0] + w.border + x, w.origin[1] + w.border + y)


def ocr_blocks(wid: str, image: str | None = None,
               region: tuple[int, int, int, int] | None = None,
               min_score: float = 0.5) -> tuple[list[TextBlock], Image.Image]:
    """OCR 窗口（或其中一块 region，能显著提速），返回文字块 + 原图。

    region 用截图内坐标 (left, top, right, bottom)。
    """
    w = window_info(wid)
    img = screenshot(wid, image)

    if region:
        ox, oy = region[0], region[1]
        work = img.crop(region)
        tmp = "/tmp/ocrclick-region.png"
        work.save(tmp)
    else:
        ox, oy = 0, 0
        tmp = image or f"/tmp/ocrclick-{wid.replace('0x', 'w')}.png"

    r = _ocr_engine().predict(tmp)[0]
    blocks: list[TextBlock] = []
    for t, s, b in zip(r["rec_texts"], r["rec_scores"], r["rec_boxes"]):
        if not t or float(s) < min_score:
            continue
        x1, y1, x2, y2 = (int(v) for v in b)
        cx, cy = ox + (x1 + x2) // 2, oy + (y1 + y2) // 2
        blocks.append(TextBlock(text=str(t).strip(), score=round(float(s), 3),
                                img_box=(ox + x1, oy + y1, ox + x2, oy + y2),
                                abs_center=_to_abs(w, cx, cy)))
    blocks.sort(key=lambda b: b.row_key)
    return blocks, img


def find_blocks(blocks: list[TextBlock], keyword: str,
                exact: bool = False, min_score: float = 0.0) -> list[TextBlock]:
    out = []
    for b in blocks:
        if b.score < min_score:
            continue
        if (b.text == keyword) if exact else (keyword in b.text):
            out.append(b)
    return out


def pick(blocks: list[TextBlock], keyword: str, exact: bool = False,
         min_score: float = 0.6, which: int = 0) -> TextBlock:
    hits = find_blocks(blocks, keyword, exact=exact, min_score=min_score)
    if not hits:
        raise RuntimeError(f"OCR 没找到 {keyword!r}")
    return hits[which]


# ---------------------------------------------------------------- 点击

def _region_of(img: Image.Image, center: tuple[int, int], pad: int = 45):
    w, h = img.size
    cx = center[0] - 0  # 传进来的 center 是截图内坐标
    cy = center[1] - 0
    return (max(0, cx - pad), max(0, cy - pad), min(w, cx + pad), min(h, cy + pad))


def _pixels(img: Image.Image, box) -> np.ndarray:
    return np.asarray(img.crop(box), dtype=np.int16)


def hover_verify(wid: str, abs_xy: tuple[int, int], img_box: tuple[int, int, int, int],
                 pad: int = 45, threshold_pct: float = 0.05) -> float:
    """把鼠标移过去，比较目标区域 hover 前后的像素差异。

    返回变化像素占比（%）。实测两套 UI 差异很大：
      · Chromium 自绘（网页、Chrome 界面）：hover 高亮明显，命中时 5%~10%
      · GTK 原生对话框：hover 反馈几乎为零（实测 max 像素差 1~5），
        只有**按下态**才有明确反馈（max 80）
    所以阈值不能定 1%，否则 GTK 按钮会被全部判死。这里默认 0.05%，
    配合 press_probe() 一起判断；两者都没反应才认定不可交互。

    注意基线噪声：鼠标静止连拍两张的像素差为 0，所以低阈值是安全的。
    """
    w = window_info(wid)
    cx = (img_box[0] + img_box[2]) // 2
    cy = (img_box[1] + img_box[3]) // 2
    region = _region_of(Image.new("RGB", w.size), (cx, cy), pad)

    activate(wid)
    before = _pixels(screenshot(wid, "/tmp/ocrclick-hover-before.png"), region)

    subprocess.run(["xdotool", "mousemove", str(abs_xy[0]), str(abs_xy[1])],
                   check=True, timeout=10)
    time.sleep(random.uniform(0.5, 0.8))
    after = _pixels(screenshot(wid, "/tmp/ocrclick-hover-after.png"), region)

    diff = np.abs(after - before)
    return float((diff.max(axis=2) > 12).mean()) * 100


def press_probe(wid: str, abs_xy: tuple[int, int], img_box: tuple[int, int, int, int],
                pad: int = 45) -> float:
    """非破坏性按下探测：按住看按下态，再把光标移开才松手。

    针对 GTK 这类没有 hover 反馈的 UI。释放点故意偏离目标，GTK 按钮要求
    press/release 落在同一 widget，所以不会误触发点击。返回按下态像素变化率（%）。
    """
    w = window_info(wid)
    cx = (img_box[0] + img_box[2]) // 2
    cy = (img_box[1] + img_box[3]) // 2
    region = _region_of(Image.new("RGB", w.size), (cx, cy), pad)

    activate(wid)
    subprocess.run(["xdotool", "mousemove", str(abs_xy[0]), str(abs_xy[1])],
                   check=True, timeout=10)
    time.sleep(random.uniform(0.15, 0.3))
    before = _pixels(screenshot(wid, "/tmp/ocrclick-press-before.png"), region)
    subprocess.run(["xdotool", "mousedown", "1"], check=True, timeout=10)
    time.sleep(0.35)
    pressed = _pixels(screenshot(wid, "/tmp/ocrclick-press-during.png"), region)
    subprocess.run(["xdotool", "mousemove", str(w.origin[0] + 4), str(w.origin[1] + 4)],
                   check=True, timeout=10)   # 先移开
    time.sleep(0.15)
    subprocess.run(["xdotool", "mouseup", "1"], check=False, timeout=10)
    time.sleep(0.2)

    diff = np.abs(pressed - before)
    return float((diff.max(axis=2) > 12).mean()) * 100


def click_at(x: int, y: int, button: int = 1, human: bool = True) -> None:
    """落到目标点再点。human=True 时移动与按下之间插一段随机停顿，
    且落点带 ±1~2px 抖动 —— 瞬移+零延迟是最典型的脚本特征。"""
    if human:
        x += random.randint(-2, 2)
        y += random.randint(-2, 2)
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], check=True, timeout=10)
    time.sleep(random.uniform(0.09, 0.35) if human else 0.0)
    subprocess.run(["xdotool", "click", str(button)], check=True, timeout=10)


def click_text(wid: str, keyword: str, exact: bool = False, min_score: float = 0.6,
               which: int = 0, verify: bool = True, human: bool = True,
               region: tuple[int, int, int, int] | None = None,
               settle: float = 2.5) -> dict:
    """截图 → OCR 找文字 → 点它的中心。verify=True 时先 hover 验像素再点。"""
    blocks, _ = ocr_blocks(wid, region=region, min_score=min(min_score, 0.5))
    target = pick(blocks, keyword, exact=exact, min_score=min_score, which=which)

    result = {"window": wid, "keyword": keyword, "text": target.text,
              "score": target.score, "img_box": target.img_box,
              "abs_center": target.abs_center,
              "hover_pct": None, "press_pct": None, "clicked": False}

    if verify:
        pct = hover_verify(wid, target.abs_center, target.img_box)
        result["hover_pct"] = round(pct, 2)
        if pct < 0.05:
            # hover 没反应不一定是死点：GTK 按钮就没有 hover 反馈。
            # 补一次非破坏性的按下探测再判。
            press = press_probe(wid, target.abs_center, target.img_box)
            result["press_pct"] = round(press, 2)
            if press < 0.05:
                result["skipped"] = "hover 和按下都没有视觉反馈，疑似不是可交互元素"
                return result

    click_at(target.abs_center[0], target.abs_center[1], human=human)
    result["clicked"] = True
    time.sleep(settle)
    return result


# ---------------------------------------------------------------- 关对话框

_CANCEL_WORDS = ("Cancel", "取消", "关闭", "Close", "Don't Save", "不保存", "放弃")


def dismiss(wid: str, words: tuple[str, ...] = _CANCEL_WORDS, human: bool = True) -> dict:
    """先 Esc；窗口还在就 OCR 找取消类按钮点掉。

    为什么不能只靠 Esc：焦点/模态关系会让 xdotool key 送到错误的窗口，
    GTK 文件对话框实测 Esc 无效，必须点按钮。
    """
    info = window_info(wid)
    activate(wid)
    subprocess.run(["xdotool", "key", "Escape"], check=False, timeout=10)
    time.sleep(0.8)

    if not any(w == info.wid for w, _ in list_windows()):
        return {"window": wid, "method": "escape", "closed": True}

    blocks, _ = ocr_blocks(wid, min_score=0.5)
    for word in words:
        hits = find_blocks(blocks, word, min_score=0.6)
        if hits:
            target = hits[0]
            pct = hover_verify(wid, target.abs_center, target.img_box)
            press = None
            if pct < 0.05:
                press = press_probe(wid, target.abs_center, target.img_box)
                if press < 0.05:
                    continue
            click_at(*target.abs_center, human=human)
            time.sleep(1.2)
            still = any(w == wid for w, _ in list_windows())
            return {"window": wid, "method": f"click:{target.text}",
                    "abs_center": target.abs_center, "hover_pct": round(pct, 2),
                    "press_pct": None if press is None else round(press, 2),
                    "closed": not still}
    return {"window": wid, "method": "none", "closed": False,
            "seen": [b.text for b in blocks]}


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="OCR 定位窗口文字并真实点击")
    ap.add_argument("--list", action="store_true", help="列出所有窗口")
    ap.add_argument("--window", help="窗口标题关键字")
    ap.add_argument("--index", type=int, default=0, help="同名窗口取第几个")
    ap.add_argument("--ocr", action="store_true", help="打印窗口内所有文字块")
    ap.add_argument("--click", help="要点击的文字")
    ap.add_argument("--exact", action="store_true", help="文字完全匹配")
    ap.add_argument("--which", type=int, default=0, help="多个匹配时取第几个")
    ap.add_argument("--min-score", type=float, default=0.6)
    ap.add_argument("--no-verify", action="store_true", help="跳过 hover 验证直接点")
    ap.add_argument("--dismiss", action="store_true", help="关掉这个对话框")
    ap.add_argument("--region", help="只 OCR 这一块，格式 l,t,r,b（截图内坐标）")
    a = ap.parse_args()

    if a.list:
        for wid, title in list_windows():
            print(f"{wid}  {title}")
        return 0

    if not a.window:
        ap.print_help()
        return 1

    wid = find_window(a.window, a.index)
    info = window_info(wid)
    print(f"窗口 {wid} {info.title!r} 原点={info.origin} 尺寸={info.size} 边框={info.border}")

    if a.ocr:
        blocks, _ = ocr_blocks(wid, min_score=a.min_score)
        for b in blocks:
            print(f"  abs={list(b.abs_center)} img={list(b.img_box)} {b.score} {b.text!r}")
        return 0

    if a.dismiss:
        print(json.dumps(dismiss(wid), ensure_ascii=False, indent=1))
        return 0

    if a.click:
        region = tuple(int(v) for v in a.region.split(",")) if a.region else None
        r = click_text(wid, a.click, exact=a.exact, min_score=a.min_score,
                       which=a.which, verify=not a.no_verify, region=region)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["clicked"] else 2

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
