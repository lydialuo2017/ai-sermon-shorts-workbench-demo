"""AI 讲道短视频助手 V1.0 网页工作台：任务与剪辑核心逻辑。

流程（用户 2026-10-31 正式批准的新顺序）：
  上传 → AI 粗剪（无字幕）→ AI 精剪（无字幕）→ 网页人工审核
  → 全部确认后才统一字幕、封面、四平台文案、ZIP。
审核阶段的视频一律无字幕；字幕只在最终包装烧录一次。

引擎：AutoClip 为唯一剪辑引擎（粗剪单段导出走 AutoClip 原生 export_clip）。
精剪多段拼接走 FFmpeg 段拼接（与 V2 已验证的 cut_clip_with_edits 同一模式，
源视频已是 9:16，故不做裁切、全幅保留）。
中文层：复用 ai-sermon-shorts/chinese_layer（繁体＋等长误听校对）。
转录：faster-whisper small（audit venv，与真实视频验证同一环境）。
高光：gemini-3.8-flash＋讲道提示词（仅种子任务复用已有结果，不重调；
新上传任务的高光需经助手对话中的保管库批准流程，见 server.py 说明）。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

WB = Path(__file__).resolve().parent
REAL = WB.parent / "real"
TASKS = WB / "tasks"
AUDIT_VENV_PY = sys.executable  # demo: container python
sys.path.insert(0, str(Path(__file__).resolve().parent / "autoclip-backend"))  # demo: engine via pip pin

FILLER_WORDS = {"呃", "嗯", "啊", "额"}
SILENCE_GAP_S = 1.5      # 词间停顿超过此值，视为无意义停顿
SILENCE_KEEP_S = 0.3     # 停顿两端各保留的呼吸长度
MIN_CLIP_S = 20.0
MAX_CLIP_S = 180.0

# 字幕预设：现行实测方案（见 SUBTITLE_PRESET.md；不是用户旧规范）。
SUBTITLE_STYLE_LINE = (
    "Style: AutoClip,Noto Sans CJK TC,77,&H00FFFFFF,&H00FFFFFF,&H00000000,"
    "&H28000000,-1,0,0,0,100,100,0,0,1,2,1,2,58,58,192,1"
)
SUBTITLE_HEADER = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
    "WrapStyle: 0\nScaledBorderAndShadow: yes\n"
    "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
    "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
    "MarginV, Encoding\n"
    + SUBTITLE_STYLE_LINE + "\n"
    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
    "Effect, Text\n"
)


def atomic_write_json(path, data):
    path = Path(path)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ffprobe_info(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-show_entries", "format=duration,size",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    d = json.loads(out.stdout)
    stream = d["streams"][0]
    return {
        "width": int(stream["width"]), "height": int(stream["height"]),
        "duration_s": round(float(d["format"]["duration"]), 2),
        "size_bytes": int(d["format"]["size"]),
    }


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clip_dir(task_dir, idx):
    return Path(task_dir) / "clips" / f"c{idx:02d}"


def words_in_range(words, start, end):
    """带源时间戳的词表：src_* 为原视频绝对时间，start/end 初始等于源时间，
    精剪后 start/end 为新时间线时间，src_* 保持不变（供再次修改与溯源）。"""
    return [{"word": w["word"],
             "src_start": float(w["start"]), "src_end": float(w["end"]),
             "start": float(w["start"]), "end": float(w["end"])}
            for w in words
            if float(w["end"]) > start and float(w["start"]) < end]


def analyze_fine(clip_words, start, end, extra_deletes=None):
    """真实精剪分析：只删（a）超长词间停顿的中间部分（两端各留呼吸），
    （b）独立填充词，（c）用户选定的词句（extra_deletes）。
    返回 deletes、kept 段与新时间线词表。
    无可删内容时明确记录“无需内部删减”，不强行跳切。"""
    deletes = []
    for a, b in zip(clip_words, clip_words[1:]):
        gap = float(b["src_start"]) - float(a["src_end"])
        if gap > SILENCE_GAP_S:
            deletes.append({
                "src_start": round(float(a["src_end"]) + SILENCE_KEEP_S, 2),
                "src_end": round(float(b["src_start"]) - SILENCE_KEEP_S, 2),
                "reason": f"无意义停顿 {gap:.1f} 秒（两端各保留 {SILENCE_KEEP_S} 秒呼吸）",
            })
    for w in clip_words:
        if w["word"] in FILLER_WORDS:
            deletes.append({
                "src_start": float(w["src_start"]), "src_end": float(w["src_end"]),
                "reason": f"口水词“{w['word']}”",
            })
    for d in (extra_deletes or []):
        deletes.append(d)
    deletes.sort(key=lambda d: d["src_start"])
    kept = []
    cursor = start
    for d in deletes:
        if d["src_start"] > cursor:
            kept.append((cursor, d["src_start"]))
        cursor = max(cursor, d["src_end"])
    if cursor < end:
        kept.append((cursor, end))

    def shift(t):
        removed = sum(max(0.0, min(t, d["src_end"]) - d["src_start"])
                      for d in deletes if d["src_start"] < t)
        return round(t - removed, 2)

    new_words = []
    for w in clip_words:
        if any(d["src_start"] <= float(w["src_start"]) < d["src_end"] for d in deletes):
            continue
        new_words.append({"word": w["word"],
                          "src_start": float(w["src_start"]),
                          "src_end": float(w["src_end"]),
                          "start": shift(float(w["src_start"])),
                          "end": shift(float(w["src_end"]))})
    note = "无需内部删减" if not deletes else f"删减 {len(deletes)} 处"
    return {"deletes": deletes, "kept": kept, "new_words": new_words, "note": note}


def run(cmd, timeout=1800):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"命令失败（{r.returncode}）：{' '.join(map(str, cmd))}\n{r.stderr[-800:]}")
    return r


def export_rough_autoclip(task, clip):
    """粗剪：AutoClip 原生单段导出，无字幕烧录。源视频已是 9:16，全幅保留。"""
    from autoclip.pipeline import captions as captions_module
    from autoclip.pipeline import export as export_module
    from autoclip.pipeline.reframe import centre_crop
    from autoclip.pipeline.transcript import Word
    src = Path(task["source"]["path"])
    info = task["source"]
    cdir = clip_dir(task["dir"], clip["index"])
    cdir.mkdir(parents=True, exist_ok=True)
    dest = cdir / "rough.mp4"
    words = [Word(text=w["word"], start=float(w["src_start"]), end=float(w["src_end"]))
             for w in clip["words"]]
    style = captions_module.CaptionStyle(
        key="sermon_tc", label="Sermon TC",
        description="Traditional Chinese sermon captions.",
        font="Noto Sans CJK TC", font_file="NotoSansCJK-Bold.ttc",
        size_ratio=0.04, primary="#FFFFFF", accent=None, outline="#000000",
        outline_width=2.0, shadow=1.0, bold=True, all_caps=False,
        margin_v_ratio=0.10, animation="none", max_words=8)
    duration = clip["end"] - clip["start"]
    req = export_module.ExportRequest(
        source=src, destination=dest,
        start_s=float(clip["start"]), end_s=float(clip["end"]),
        crop_path=centre_crop(info["width"], info["height"], duration),
        words=words, style=style, ratio="9:16", burn_captions=False)
    out = export_module.export_clip(req, work_dir=Path(task["dir"]) / "_work")
    return out


def export_fine_segments(task, clip):
    """精剪：按 EDL kept 段逐段重编码后拼接（无字幕）。无删减时直接复用粗剪。"""
    cdir = clip_dir(task["dir"], clip["index"])
    fine = cdir / "fine.mp4"
    edl = clip["edl"]
    if not edl["deletes"]:
        shutil.copyfile(cdir / "rough.mp4", fine)
        return fine, True
    src = task["source"]["path"]
    tmpdir = cdir / "parts"
    shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)
    list_path = cdir / "parts.txt"
    with open(list_path, "w", encoding="utf-8") as lf:
        for i, (s, e) in enumerate(edl["kept"]):
            part = tmpdir / f"part_{i}.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-i", src,
                 "-ss", str(s), "-to", str(e),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-c:a", "aac", "-b:a", "128k", str(part)])
            lf.write(f"file '{part}'\n")
    merged = cdir / "fine_merged.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_path), "-c", "copy", str(merged)])
    shutil.move(str(merged), str(fine))
    shutil.rmtree(tmpdir, ignore_errors=True)
    return fine, False


def qc_check(mp4_path):
    info = ffprobe_info(mp4_path)
    r = subprocess.run(
        ["ffmpeg", "-i", str(mp4_path), "-vn", "-af", "volumedetect",
         "-f", "null", "/dev/null"],
        capture_output=True, text=True, timeout=600)
    vol = None
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            vol = line.split("mean_volume:")[-1].strip()
    black = subprocess.run(
        ["ffmpeg", "-i", str(mp4_path), "-vf",
         "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "/dev/null"],
        capture_output=True, text=True, timeout=600)
    black_hits = black.stderr.count("black_start")
    return {"duration_s": info["duration_s"], "width": info["width"],
            "height": info["height"], "size_bytes": info["size_bytes"],
            "mean_volume": vol, "black_segments": black_hits}


def load_task(task_id):
    task_dir = TASKS / task_id
    task = load_json(task_dir / "task.json")
    task["dir"] = str(task_dir)
    return task


def save_task(task):
    atomic_write_json(Path(task["dir"]) / "task.json",
                      {k: v for k, v in task.items() if k != "dir"})


def get_clip(task, idx):
    for c in task["clips"]:
        if c["index"] == idx:
            return c
    raise KeyError(f"无此片段：{idx}")


def task_public(task):
    """返回给前端的任务视图（去掉绝对路径与全量词表）。"""
    out = {k: v for k, v in task.items() if k not in ("dir", "words")}
    out["source"] = {k: v for k, v in task["source"].items() if k != "path"}
    out["source"]["sha256_short"] = task["source"]["sha256"][:16]
    for c in out["clips"]:
        c["word_count"] = len(c["words"])
    return out


# ---------------- 最终包装 ----------------

def break_lines(new_words, max_chars=12, max_lines=2, gap_break=0.8):
    """语义断句分行：按字拼接（无空格），每行≤12 字，每屏≤2 行，
    优先在词间停顿处断句。返回事件列表 [{start, end, text}]。"""
    lines, cur, cur_chars = [], [], 0
    prev_end = None
    for w in new_words:
        gap = (float(w["start"]) - prev_end) if prev_end is not None else 0.0
        if cur and (cur_chars + len(w["word"]) > max_chars or gap > gap_break):
            lines.append(cur)
            cur, cur_chars = [], 0
        cur.append(w)
        cur_chars += len(w["word"])
        prev_end = float(w["end"])
    if cur:
        lines.append(cur)
    events = []
    for i in range(0, len(lines), max_lines):
        group = lines[i:i + max_lines]
        flat = [w for ln in group for w in ln]
        events.append({
            "start": float(flat[0]["start"]), "end": float(flat[-1]["end"]),
            "text": "\\N".join("".join(w["word"] for w in ln) for ln in group),
        })
    return events


def fmt_ass_time(t):
    h, m = int(t // 3600), int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def fmt_srt_time(t):
    h, m = int(t // 3600), int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def write_srt(events, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, ev in enumerate(events, 1):
            f.write(f"{i}\n{fmt_srt_time(ev['start'])} --> {fmt_srt_time(ev['end'])}\n"
                    f"{ev['text'].replace(chr(92) + 'N', chr(10))}\n\n")


def write_ass(events, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(SUBTITLE_HEADER)
        for ev in events:
            f.write(f"Dialogue: 0,{fmt_ass_time(ev['start'])},{fmt_ass_time(ev['end'])},"
                    f"AutoClip,,0,0,0,,{ev['text']}\n")


def make_cover(fine_mp4, title, out_path):
    """封面：取精剪中点帧，底部深色带＋繁体标题（Noto Sans CJK TC Bold）。"""
    from PIL import Image, ImageDraw, ImageFont
    frame = Path(out_path).parent / "_cover_frame.jpg"
    run(["ffmpeg", "-y", "-v", "error", "-ss", "3", "-i", str(fine_mp4),
         "-frames:v", "1", str(frame)])
    img = Image.open(frame).convert("RGB")
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    band_h = int(h * 0.30)
    od.rectangle([0, h - band_h, w, h], fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
    size = 84
    font = ImageFont.truetype(font_path, size)
    lines, cur = [], ""
    for ch in title:
        if len(cur) >= 12:
            lines.append(cur)
            cur = ""
        cur += ch
    if cur:
        lines.append(cur)
    y = h - band_h + 40
    for ln in lines[:3]:
        bbox = draw.textbbox((0, 0), ln, font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((w - tw) // 2, y), ln, font=font, fill=(255, 255, 255))
        y += size + 16
    img.save(out_path)
    frame.unlink(missing_ok=True)
    return out_path


def build_copy(clip):
    """四平台文案：全部取自本条真实逐字稿、标题与 hook，不编造经文与故事。
    四平台结构、篇幅、CTA 各不相同；本版为基于真实内容的模板生成，
    未经 Gemini 润色（见报告说明）。"""
    text = "".join(w["word"] for w in clip["words"])
    excerpt = text[:180]
    title = clip["title"]
    hook = clip.get("hook", "")
    base_tags = "#講道 #基督信仰 #聖經真理 #主日信息 #信仰生活"
    yt_titles = [
        title,
        f"{hook}" if hook else title,
        f"基督徒一定要聽：{title}",
        f"{title}｜主日講道精華",
        f"為什麼{title}",
        f"{title}，你聽過嗎？",
        f"主日短講｜{title}",
    ]
    yt_titles = [t for t in dict.fromkeys(yt_titles) if t][:7]
    cover_texts = [title, hook or title, f"{title}（精華）",
                   "主日講道精華", title[:12]]
    cover_texts = [t for t in dict.fromkeys(cover_texts) if t][:5]
    seo = [title, hook or title, "主日講道", "基督徒生活", "聖經教導"]
    yt_desc = (f"{excerpt}……\n\n本片段取自主日講道，完整內容請留意教會完整講道。"
               f"\n\n{base_tags}")
    fb = (f"【主日短講】{title}\n\n{excerpt}……\n\n"
        f"你也有過這樣的經歷嗎？歡迎在留言中分享你的看見，也請把這條短片"
        f"分享給需要的朋友。\n\n{base_tags} #FacebookReels")
    ig = (f"{title}\n\n{excerpt[:100]}……\n\n完整講道在教會頻道，歡迎追蹤收看。\n\n"
        f"{base_tags} #Reels")
    tt = (f"{title}\n\n{excerpt[:80]}…… #講道 #信仰 #主日 #基督徒 #短講")
    return {
        "youtube": {"cover_texts": cover_texts, "titles": yt_titles,
                    "description": yt_desc, "hashtags": base_tags, "seo_tags": seo},
        "facebook": {"copy": fb, "hashtags": base_tags, "tags": ["主日講道", "信仰分享"]},
        "instagram": {"copy": ig, "hashtags": base_tags, "cta": "追蹤收看完整講道"},
        "tiktok": {"title": title, "description": tt, "tags": base_tags},
        "note": "模板生成：全部取自本条真实逐字稿、标题与 hook，未编造经文与故事；"
                "未经 Gemini 润色。",
    }


def copy_to_text(copy_data):
    L = ["【YouTube Shorts】"]
    L.append("封面文字（5 组）：")
    for t in copy_data["youtube"]["cover_texts"]:
        L.append(f"  - {t}")
    L.append("标题（7 个）：")
    for t in copy_data["youtube"]["titles"]:
        L.append(f"  - {t}")
    L.append(f"描述：\n{copy_data['youtube']['description']}")
    L.append(f"Hashtags：{copy_data['youtube']['hashtags']}")
    L.append(f"SEO Tags：{'、'.join(copy_data['youtube']['seo_tags'])}")
    L.append("\n【Facebook Reels】")
    L.append(copy_data["facebook"]["copy"])
    L.append(f"后台标签：{'、'.join(copy_data['facebook']['tags'])}")
    L.append("\n【Instagram Reels】")
    L.append(copy_data["instagram"]["copy"])
    L.append(f"CTA：{copy_data['instagram']['cta']}")
    L.append("\n【TikTok】")
    L.append(copy_data["tiktok"]["description"])
    L.append(f"\n说明：{copy_data['note']}")
    return "\n".join(L) + "\n"


def packaging_gate(task):
    unconfirmed = [c["index"] for c in task["clips"]
                   if c["state"] not in ("confirmed", "discarded")]
    confirmed = [c for c in task["clips"] if c["state"] == "confirmed"]
    if not confirmed:
        return False, unconfirmed, "尚无已确认的片段"
    if unconfirmed:
        return False, unconfirmed, f"仍有 {len(unconfirmed)} 条未确认"
    return True, [], "全部保留片段已确认，可以开始最终制作"
