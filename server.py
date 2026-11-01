"""AI 讲道短视频助手 V1.0 网页工作台服务器（标准库，无第三方依赖）。

启动：python3 server.py [端口，默认 8901]
Windows：双击 start.bat；macOS/Linux：sh start.sh。
本机浏览器打开 http://localhost:8901 即可使用。

路由：
  GET  /                        工作台页面
  GET  /static/<file>           前端文件
  GET  /media/<task>/<path>     任务文件（含视频 Range 断点续传）
  GET  /api/tasks               任务列表
  POST /api/tasks/upload        上传本地 MP4（multipart，字段 file）
  POST /api/tasks/drive         从已授权 Drive 取视频 {file_id}
  GET  /api/tasks/<id>          任务详情（含片段状态与任务内作业）
  POST /api/tasks/<id>/rough/<idx>    生成无字幕粗剪（后台）
  POST /api/tasks/<id>/fine/<idx>     生成无字幕精剪（后台）
  POST /api/tasks/<id>/clips/<idx>/approve|discard|unconfirm
  POST /api/tasks/<id>/clips/<idx>/modify  {new_start?, new_end?, delete_positions?}
  POST /api/tasks/<id>/package  门禁检查后开始最终制作（后台）
  GET  /api/tasks/<id>/download ZIP 下载
  GET  /api/jobs/<job_id>       后台作业状态
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_wb as P

# 启动时单线程预导入 AutoClip 模块，避免工作线程并发导入竞态。
sys.path.insert(0, str(Path(__file__).resolve().parent / "autoclip-backend"))  # demo: engine via pip pin, see Dockerfile
from autoclip.pipeline import captions as _ac_captions  # noqa: F401
from autoclip.pipeline import export as _ac_export  # noqa: F401
from autoclip.pipeline import reframe as _ac_reframe  # noqa: F401
from autoclip.pipeline import transcript as _ac_transcript  # noqa: F401

WB = Path(__file__).resolve().parent
STATIC = WB / "static"
LOCK = threading.RLock()
JOBS = {}


def new_job(job_type, task_id, clip_idx=None):
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {"id": job_id, "type": job_type, "task": task_id,
                        "clip": clip_idx, "status": "queued", "detail": "",
                        "error": None, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    return job_id


def set_job(job_id, **kw):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def run_bg(fn, job_id, *args):
    def wrapper():
        set_job(job_id, status="running")
        try:
            fn(job_id, *args)
            set_job(job_id, status="done", detail="完成")
        except Exception as exc:
            set_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
            # 修改作业失败时把片段状态从“修改中”恢复为“待审核”，避免卡死
            with LOCK:
                job = JOBS.get(job_id, {})
            if job.get("type") == "modify":
                try:
                    with LOCK:
                        task = P.load_task(job["task"])
                        clip = P.get_clip(task, job["clip"])
                        if clip["state"] == "modifying":
                            clip["state"] = "pending_review"
                            clip["history"].append(
                                {"action": "modify_failed",
                                 "note": f"修改失败，已恢复待审核：{exc}"[:200],
                                 "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                            P.save_task(task)
                except Exception:
                    pass
    threading.Thread(target=wrapper, daemon=True).start()


def task_jobs(task_id):
    with LOCK:
        return [j for j in JOBS.values() if j["task"] == task_id]


# ---------------- 后台作业 ----------------

def do_rough(job_id, task_id, idx):
    with LOCK:
        task = P.load_task(task_id)
        clip = P.get_clip(task, idx)
    set_job(job_id, detail="AutoClip 粗剪渲染中（无字幕）……")
    out = P.export_rough_autoclip(task, clip)
    with LOCK:
        task = P.load_task(task_id)
        clip = P.get_clip(task, idx)
        clip["rough_file"] = f"clips/c{idx:02d}/rough.mp4"
        clip["qc"]["rough"] = P.qc_check(out)
        clip["history"].append({"action": "rough", "note": "无字幕粗剪已生成",
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        P.save_task(task)


def do_fine(job_id, task_id, idx):
    with LOCK:
        task = P.load_task(task_id)
        clip = P.get_clip(task, idx)
    if not clip["rough_file"]:
        raise RuntimeError("请先生成粗剪")
    set_job(job_id, detail="精剪分析与导出中（无字幕）……")
    edl = P.analyze_fine(clip["words"], clip["start"], clip["end"])
    clip["edl"] = edl
    clip["fine_analysis"] = {"deletes": edl["deletes"], "note": edl["note"]}
    out, is_copy = P.export_fine_segments(task, clip)
    with LOCK:
        task = P.load_task(task_id)
        clip = P.get_clip(task, idx)
        clip["edl"] = edl
        clip["fine_analysis"] = {"deletes": edl["deletes"], "note": edl["note"]}
        clip["words"] = edl["new_words"]
        clip["fine_file"] = f"clips/c{idx:02d}/fine.mp4"
        clip["qc"]["fine"] = P.qc_check(out)
        clip["history"].append({"action": "fine",
                                "note": f"精剪完成：{edl['note']}（无字幕）"
                                        + ("，复用粗剪文件" if is_copy else ""),
                                "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        P.save_task(task)


def do_modify(job_id, task_id, idx, new_start, new_end, delete_positions):
    with LOCK:
        task = P.load_task(task_id)
        clip = P.get_clip(task, idx)
    notes = []
    if new_start is not None or new_end is not None:
        ns = float(new_start) if new_start is not None else clip["start"]
        ne = float(new_end) if new_end is not None else clip["end"]
        dur = ne - ns
        if dur < P.MIN_CLIP_S:
            raise RuntimeError(f"修改后时长 {dur:.1f} 秒过短（需 ≥{P.MIN_CLIP_S} 秒）")
        if dur > P.MAX_CLIP_S:
            raise RuntimeError(f"修改后时长 {dur:.1f} 秒过长（需 ≤{P.MAX_CLIP_S} 秒）")
        if not (0 <= ns < ne <= task["source"]["duration_s"]):
            raise RuntimeError("时间码超出原视频范围")
        clip["start"], clip["end"] = ns, ne
        clip["duration"] = round(ne - ns, 2)
        clip["words"] = P.words_in_range(task["words"], ns, ne)
        notes.append(f"边界调整为 {ns}-{ne} 秒")
        set_job(job_id, detail="边界调整：重新生成粗剪与精剪（无字幕）……")
        P.export_rough_autoclip(task, clip)
        clip["rough_file"] = f"clips/c{idx:02d}/rough.mp4"
    extra = []
    if delete_positions:
        for pos in delete_positions:
            w = clip["words"][int(pos)]
            extra.append({"src_start": float(w["src_start"]),
                          "src_end": float(w["src_end"]),
                          "reason": f"用户删除词句“{w['word']}”"})
        notes.append(f"删除 {len(extra)} 处用户选定词句")
    if new_start is not None or new_end is not None or extra:
        set_job(job_id, detail="精剪重算与导出中（无字幕）……")
        edl = P.analyze_fine(clip["words"], clip["start"], clip["end"],
                             extra_deletes=extra)
        clip["edl"] = edl
        clip["fine_analysis"] = {"deletes": edl["deletes"], "note": edl["note"]}
        P.export_fine_segments(task, clip)
        clip["fine_file"] = f"clips/c{idx:02d}/fine.mp4"
        clip["words"] = edl["new_words"]
    with LOCK:
        task = P.load_task(task_id)
        old = P.get_clip(task, idx)
        old.update({k: v for k, v in clip.items() if k not in ("history",)})
        old["version"] = old.get("version", 1) + 1
        old["state"] = "pending_rereview"
        old["history"].append({"action": "modify", "note": "；".join(notes),
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        P.save_task(task)


def task_report(task):
    ok, unconfirmed, gate_msg = P.packaging_gate(task)
    L = ["AI 讲道短视频助手 V1.0 任务报告"]
    L.append(f"任务：{task['id']}，源视频：{task['source']['name']}")
    L.append(f"源文件指纹：{task['source']['sha256']}")
    L.append(f"源视频：{task['source']['duration_s']} 秒，"
             f"{task['source']['width']}x{task['source']['height']}，"
             f"{task['source']['size_bytes']} 字节")
    L.append("")
    L.append("四阶段状态：")
    L.append("  1. 高光识别：见各片段来源（种子任务复用已有结果，未重调 Gemini；"
             "新上传任务见任务状态）。")
    L.append("  2. 边界修复：见各片段 repairs 与 history。")
    L.append(f"  3. 视频成功导出：任务状态 {task['status']}。")
    L.append(f"  4. 人工审核通过：门禁检查——{gate_msg}。")
    L.append("")
    for c in task["clips"]:
        L.append(f"短视频{c['index']:02d}《{c['title']}》[{c['state']}] "
                 f"{c['start']}-{c['end']} 秒（{c['duration']} 秒）评分 {c['score']}")
        L.append(f"  精剪：{(c.get('fine_analysis') or {}).get('note', '未生成')}")
        if c.get("final_file"):
            L.append(f"  成片：{c['final_file']}")
    return "\n".join(L) + "\n"


def do_package(job_id, task_id):
    with LOCK:
        task = P.load_task(task_id)
    ok, unconfirmed, msg = P.packaging_gate(task)
    if not ok:
        raise RuntimeError(f"门禁未通过：{msg}，未确认片段：{unconfirmed}")
    task["status"] = "packaging"
    with LOCK:
        P.save_task(task)
    final_dir = Path(task["dir"]) / "final"
    final_dir.mkdir(exist_ok=True)
    for clip in task["clips"]:
        if clip["state"] != "confirmed":
            continue
        set_job(job_id, detail=f"最终制作：短视频{clip['index']:02d}（字幕只烧录一次）……")
        cdir = P.clip_dir(task["dir"], clip["index"])
        events = P.break_lines(clip["edl"]["new_words"])
        srt_path = cdir / "字幕.srt"
        ass_path = cdir / "final.ass"
        P.write_srt(events, srt_path)
        P.write_ass(events, ass_path)
        fine = cdir / "fine.mp4"
        final = cdir / "final.mp4"
        P.run(["ffmpeg", "-y", "-v", "error", "-i", str(fine),
               "-vf", f"ass={ass_path}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-c:a", "copy", str(final)])
        cover = cdir / "封面.png"
        P.make_cover(fine, clip["title"], cover)
        copy_data = P.build_copy(clip)
        copy_path = cdir / "四平台文案.txt"
        copy_path.write_text(P.copy_to_text(copy_data), encoding="utf-8")
        with LOCK:
            task = P.load_task(task_id)
            c = P.get_clip(task, clip["index"])
            c["final_file"] = f"clips/c{clip['index']:02d}/final.mp4"
            c["qc"]["final"] = P.qc_check(final)
            c["history"].append({"action": "package", "note": "最终制作完成（字幕烧录一次）",
                                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
            P.save_task(task)
    report = task_report(P.load_task(task_id))
    (Path(task["dir"]) / "任务报告.txt").write_text(report, encoding="utf-8")
    zip_path = Path(task["dir"]) / "final_package.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for clip in task["clips"]:
            if clip["state"] != "confirmed":
                continue
            cdir = P.clip_dir(task["dir"], clip["index"])
            for name in ["final.mp4", "字幕.srt", "封面.png", "四平台文案.txt"]:
                p = cdir / name
                if p.exists():
                    zf.write(p, f"短视频{clip['index']:02d}_《{clip['title']}》/{name}")
        zf.write(Path(task["dir"]) / "任务报告.txt", "任务报告.txt")
    with LOCK:
        task = P.load_task(task_id)
        task["status"] = "done"
        P.save_task(task)


# ---------------- 新上传任务： intake / 转录 / 高光 ----------------

def do_transcribe(job_id, task_id):
    with LOCK:
        task = P.load_task(task_id)
    src = task["source"]["path"]
    out_words = Path(task["dir"]) / "corrected_words.json"
    set_job(job_id, detail="本地转录中（small 模型）……")
    script = WB / "transcribe_generic.py"
    P.run([P.AUDIT_VENV_PY, str(script), src, str(Path(task["dir"]) / "transcript.json")],
          timeout=7200)
    set_job(job_id, detail="中文校对中（繁体＋等长误听校对）……")
    P.run([sys.executable, "-c",
           "import sys; sys.path.insert(0, '.'); "
           "from correct_generic import main; main()",
           str(Path(task["dir"]) / "transcript.json"), str(out_words)],
          timeout=1800)
    with LOCK:
        task = P.load_task(task_id)
        words = P.load_json(out_words)
        task["words"] = [{"word": w["word"], "start": float(w["start"]),
                          "end": float(w["end"])} for w in words]
        task["status"] = "transcribed"
        P.save_task(task)


def do_highlights(job_id, task_id):
    """高光分析：调用与 run_highlights_real 同逻辑的通用驱动，经由
    custom.gemini 连接器调用 gemini-3.8-flash。每次调用需用户在对话中
    批准保管库调用；本作业会如实报告等待/失败状态，绝不绕过审批。
    （本功能已接入但未在本次交付测试中实跑，见测试报告。）"""
    with LOCK:
        task = P.load_task(task_id)
    if task["status"] != "transcribed":
        raise RuntimeError("请先完成转录")
    set_job(job_id, detail="高光分析中（需批准 Gemini 调用，等待中）……")
    P.run([sys.executable, str(WB / "run_highlights_wb.py"), task_id], timeout=7200)
    with LOCK:
        task = P.load_task(task_id)
        data = P.load_json(Path(task["dir"]) / "highlights.json")
        clips = []
        for i, c in enumerate(data["clips"], 1):
            start, end = float(c["start"]), float(c["end"])
            clips.append({
                "index": i, "title": c["title"], "hook": c.get("hook", ""),
                "reason": c.get("reason", ""), "score": c["score"],
                "orig_start": start, "orig_end": end, "start": start, "end": end,
                "duration": round(end - start, 2),
                "words": P.words_in_range(task["words"], start, end),
                "repairs": [], "state": "pending_review", "version": 1,
                "rough_file": None, "fine_file": None, "final_file": None,
                "edl": None, "fine_analysis": None, "qc": {},
                "history": [{"action": "highlights", "note": "Gemini 高光分析完成",
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}],
            })
        task["clips"] = clips
        task["status"] = "review"
        P.save_task(task)


def create_task_from_file(src_path, name):
    task_id = "task_" + uuid.uuid4().hex[:10]
    task_dir = P.TASKS / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "clips").mkdir(exist_ok=True)
    info = P.ffprobe_info(src_path)
    task = {
        "id": task_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"name": name, "path": str(src_path),
                   "duration_s": info["duration_s"], "size_bytes": info["size_bytes"],
                   "width": info["width"], "height": info["height"],
                   "sha256": P.sha256_file(src_path)},
        "status": "intaked", "clips": [], "words": [],
        "note": "新上传任务：已登记，待转录与高光。",
    }
    task["dir"] = str(task_dir)
    P.atomic_write_json(task_dir / "task.json",
                        {k: v for k, v in task.items() if k != "dir"})
    return task


def parse_multipart(body, boundary):
    """最小 multipart 解析：返回 {字段名: (文件名, bytes)} 与文本字段。"""
    parts = body.split(b"--" + boundary)
    files, fields = {}, {}
    for part in parts:
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        m = re.search(rb'name="([^"]+)"(?:; filename="([^"]+)")?', head)
        if not m:
            continue
        name = m.group(1).decode()
        if m.group(2):
            files[name] = (m.group(2).decode(), data)
        else:
            fields[name] = data.decode(errors="replace")
    return files, fields


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        u = urlparse(unquote(self.path))
        path = u.path
        try:
            if path == "/":
                self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            elif path.startswith("/static/"):
                name = path[len("/static/"):]
                if ".." in name or "/" in name:
                    return self.send_json({"error": "非法路径"}, 400)
                self.serve_file(STATIC / name)
            elif path.startswith("/media/"):
                self.serve_media(path[len("/media/"):])
            elif path == "/api/tasks":
                items = []
                for d in sorted(P.TASKS.iterdir()) if P.TASKS.exists() else []:
                    if (d / "task.json").exists():
                        t = P.load_json(d / "task.json")
                        items.append({"id": t["id"], "name": t["source"]["name"],
                                      "status": t["status"],
                                      "clips": len(t["clips"]),
                                      "confirmed": sum(1 for c in t["clips"]
                                                       if c["state"] == "confirmed"),
                                      "discarded": sum(1 for c in t["clips"]
                                                       if c["state"] == "discarded")})
                self.send_json({"tasks": items})
            elif path.startswith("/api/tasks/") and path.endswith("/download"):
                task_id = path.split("/")[3]
                task = P.load_task(task_id)
                zp = Path(task["dir"]) / "final_package.zip"
                if not zp.exists():
                    return self.send_json({"error": "尚未生成最终打包文件"}, 404)
                self.serve_file(zp, "application/zip", download=f"{task_id}_final.zip")
            elif path.startswith("/api/jobs/"):
                job_id = path.split("/")[3]
                with LOCK:
                    job = JOBS.get(job_id)
                if not job:
                    return self.send_json({"error": "无此作业"}, 404)
                self.send_json(job)
            elif path.startswith("/api/tasks/"):
                task_id = path.split("/")[3]
                task = P.load_task(task_id)
                out = P.task_public(task)
                out["jobs"] = task_jobs(task_id)
                ok, unconfirmed, msg = P.packaging_gate(task)
                out["gate"] = {"ok": ok, "unconfirmed": unconfirmed, "message": msg}
                self.send_json(out)
            else:
                self.send_json({"error": "未知路径"}, 404)
        except FileNotFoundError:
            self.send_json({"error": "任务或文件不存在"}, 404)
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def serve_file(self, path, ctype=None, download=None):
        path = Path(path)
        if not path.exists():
            return self.send_json({"error": "文件不存在"}, 404)
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)

    def serve_media(self, rel):
        parts = rel.split("/")
        if len(parts) < 2 or ".." in parts:
            return self.send_json({"error": "非法路径"}, 400)
        task_id = parts[0]
        task = P.load_task(task_id)
        fpath = Path(task["dir"]).joinpath(*parts[1:])
        if not fpath.exists() or not str(fpath.resolve()).startswith(str(Path(task["dir"]).resolve())):
            return self.send_json({"error": "文件不存在"}, 404)
        size = fpath.stat().st_size
        ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        range_hdr = self.headers.get("Range")
        with open(fpath, "rb") as f:
            if range_hdr:
                m = re.match(r"bytes=(\d*)-(\d*)", range_hdr)
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                f.seek(start)
                data = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
            else:
                data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)

    def do_POST(self):
        u = urlparse(unquote(self.path))
        path = u.path
        try:
            if path == "/api/tasks/upload":
                ctype = self.headers.get("Content-Type", "")
                m = re.search(r"boundary=(.+)", ctype)
                if not m:
                    return self.send_json({"error": "需要 multipart 上传"}, 400)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                files, _ = parse_multipart(body, m.group(1).encode())
                if "file" not in files:
                    return self.send_json({"error": "未收到文件字段 file"}, 400)
                fname, data = files["file"]
                task_id = "task_" + uuid.uuid4().hex[:10]
                task_dir = P.TASKS / task_id
                (task_dir / "clips").mkdir(parents=True, exist_ok=True)
                src = task_dir / "source.mp4"
                src.write_bytes(data)
                task = create_task_from_file(src, fname)
                job_id = new_job("transcribe", task_id)
                run_bg(do_transcribe, job_id, task_id)
                return self.send_json({"task_id": task_id, "job": job_id})
            if path == "/api/tasks/drive":
                body = self.read_json()
                file_id = body.get("file_id", "")
                if "drive.google.com" in file_id:
                    mm = re.search(r"/d/([^/]+)", file_id)
                    file_id = mm.group(1) if mm else file_id
                if not file_id:
                    return self.send_json({"error": "需要 file_id 或 Drive 链接"}, 400)
                task_id = "task_" + uuid.uuid4().hex[:10]
                task_dir = P.TASKS / task_id
                (task_dir / "clips").mkdir(parents=True, exist_ok=True)
                src = task_dir / "source.mp4"
                r = subprocess.run(
                    ["/opt/hatch/bin/hatch_gws_cli", "drive", "files", "get",
                     "--params", json.dumps({"fileId": file_id, "alt": "media"}),
                     "--output", str(src)],
                    capture_output=True, text=True, timeout=3600)
                if not src.exists() or src.stat().st_size == 0:
                    return self.send_json({"error": "Drive 下载失败", "detail": r.stdout[-500:]}, 500)
                meta = subprocess.run(
                    ["/opt/hatch/bin/hatch_gws_cli", "drive", "files", "get",
                     "--params", json.dumps({"fileId": file_id,
                                             "fields": "name,mimeType,size"})],
                    capture_output=True, text=True, timeout=120)
                name = "drive_video.mp4"
                try:
                    name = json.loads(meta.stdout).get("name", name)
                except Exception:
                    pass
                task = create_task_from_file(src, name)
                job_id = new_job("transcribe", task_id)
                run_bg(do_transcribe, job_id, task_id)
                return self.send_json({"task_id": task_id, "job": job_id})
            m = re.match(r"^/api/tasks/([^/]+)/rough/(\d+)$", path)
            if m:
                task_id, idx = m.group(1), int(m.group(2))
                job_id = new_job("rough", task_id, idx)
                run_bg(do_rough, job_id, task_id, idx)
                return self.send_json({"job": job_id})
            m = re.match(r"^/api/tasks/([^/]+)/fine/(\d+)$", path)
            if m:
                task_id, idx = m.group(1), int(m.group(2))
                job_id = new_job("fine", task_id, idx)
                run_bg(do_fine, job_id, task_id, idx)
                return self.send_json({"job": job_id})
            m = re.match(r"^/api/tasks/([^/]+)/clips/(\d+)/(approve|discard|unconfirm)$", path)
            if m:
                task_id, idx, action = m.group(1), int(m.group(2)), m.group(3)
                with LOCK:
                    task = P.load_task(task_id)
                    clip = P.get_clip(task, idx)
                    if action == "approve":
                        if clip["state"] not in ("pending_review", "pending_rereview"):
                            return self.send_json({"error": f"当前状态 {clip['state']} 不能确认"}, 400)
                        if not clip["fine_file"]:
                            return self.send_json({"error": "请先生成精剪"}, 400)
                        clip["state"] = "confirmed"
                        fine_path = P.clip_dir(task["dir"], idx) / "fine.mp4"
                        clip["locked_fine_sha256"] = P.sha256_file(fine_path)
                        note = "确认通过（精剪版本已锁定）"
                    elif action == "discard":
                        if clip["state"] == "confirmed":
                            return self.send_json({"error": "已确认的片段请先撤销确认再废弃"}, 400)
                        clip["state"] = "discarded"
                        note = "已废弃，不进入最终包装"
                    else:
                        if clip["state"] != "confirmed":
                            return self.send_json({"error": "只有已确认的片段可以撤销确认"}, 400)
                        clip["state"] = "pending_review"
                        clip.pop("locked_fine_sha256", None)
                        note = "已撤销确认，回到待审核"
                    clip["history"].append({"action": action, "note": note,
                                            "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                    P.save_task(task)
                return self.send_json({"ok": True, "state": clip["state"]})
            m = re.match(r"^/api/tasks/([^/]+)/clips/(\d+)/modify$", path)
            if m:
                task_id, idx = m.group(1), int(m.group(2))
                body = self.read_json()
                with LOCK:
                    task = P.load_task(task_id)
                    clip = P.get_clip(task, idx)
                    if clip["state"] == "confirmed":
                        return self.send_json({"error": "已确认的片段请先撤销确认再修改"}, 400)
                    if clip["state"] == "discarded":
                        return self.send_json({"error": "已废弃的片段不能修改"}, 400)
                with LOCK:
                    task = P.load_task(task_id)
                    clip = P.get_clip(task, idx)
                    clip["state"] = "modifying"
                    clip["history"].append(
                        {"action": "modify_start", "note": "修改中",
                         "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
                    P.save_task(task)
                job_id = new_job("modify", task_id, idx)
                run_bg(do_modify, job_id, task_id, idx,
                       body.get("new_start"), body.get("new_end"),
                       body.get("delete_positions") or [])
                return self.send_json({"job": job_id})
            m = re.match(r"^/api/tasks/([^/]+)/highlights$", path)
            if m:
                task_id = m.group(1)
                job_id = new_job("highlights", task_id)
                run_bg(do_highlights, job_id, task_id)
                return self.send_json({"job": job_id})
            m = re.match(r"^/api/tasks/([^/]+)/package$", path)
            if m:
                task_id = m.group(1)
                with LOCK:
                    task = P.load_task(task_id)
                ok, unconfirmed, msg = P.packaging_gate(task)
                if not ok:
                    return self.send_json({"error": f"门禁未通过：{msg}",
                                           "unconfirmed": unconfirmed}, 400)
                job_id = new_job("package", task_id)
                run_bg(do_package, job_id, task_id)
                return self.send_json({"job": job_id})
            return self.send_json({"error": "未知路径"}, 404)
        except FileNotFoundError:
            self.send_json({"error": "任务或文件不存在"}, 404)
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    P.TASKS.mkdir(parents=True, exist_ok=True)
    STATIC.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"AI 讲道短视频助手 V1.0 工作台运行中：http://localhost:{port}")
    print("按 Ctrl+C 停止。任务与审核状态保存在 workbench/tasks/，重启后可恢复。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止。")


if __name__ == "__main__":
    main()
