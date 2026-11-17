#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import re
import subprocess
from pathlib import Path

from gcommon import (
    download_drive_file,
    extract_drive_id,
    file_view_url,
    folder_url,
    get_services,
    list_folder_files,
    read_task_row,
    resolve_project_folder,
    update_cells,
    upsert_file,
)


def probe_audio_duration(wav_path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path),
        ],
        text=True,
    ).strip()
    return float(out)


def extract_html_duration(html_path: Path) -> float | None:
    txt = html_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r'data-duration="([0-9.]+)"', txt)
    return float(m.group(1)) if m else None


def sanitize_hyperframes_determinism(html_path: Path) -> bool:
    """Ensure composition passes HyperFrames 0.9.37 deterministic lint across parallel workers."""
    src = html_path.read_text(encoding="utf-8", errors="ignore")
    orig = src

    # 1. Strip non-deterministic CSS transitions inside <style> blocks
    src = re.sub(
        r"(<style[^>]*>)(.*?)(</style>)",
        lambda m: m.group(1) + re.sub(r"transition\s*:[^;\}]+;?", "", m.group(2)) + m.group(3),
        src,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2. If HTML used a wall-clock rAF camera loop, upgrade camera pan to pure deterministic f(t) inside update(t)
    if "requestAnimationFrame" in src or "performance.now" in src:
        det_cam = (
            '    if (ai >= 0 && sheets[page]) {\n'
            '      var _ms = Math.max(0, SHEET_H[page] - FRAME_H);\n'
            '      var _curY = Math.max(0, Math.min(_ms, ITEMS[ai].cy - FOCUS_Y));\n'
            '      var _prevY = (ai > 0 && ITEMS[ai - 1].p === page) ? Math.max(0, Math.min(_ms, ITEMS[ai - 1].cy - FOCUS_Y)) : _curY;\n'
            '      var _u = Math.max(0, Math.min(1, (t - ITEMS[ai].s) / 0.42));\n'
            '      var _ez = _u * _u * (3 - 2 * _u);\n'
            '      camCur = _prevY + (_curY - _prevY) * _ez;\n'
            '      camTarget = _curY;\n'
            '      sheets[page].style.transform = "translate3d(0, " + (-camCur).toFixed(2) + "px, 0)";\n'
            '    } else if (sheets[page]) {\n'
            '      sheets[page].style.transform = "translate3d(0, " + (-camTarget).toFixed(2) + "px, 0)";\n'
            '    }\n'
        )
        if 'prog.style.width =' in src and 'var _ez = _u * _u' not in src:
            src = src.replace('    prog.style.width =', det_cam + '    prog.style.width =', 1)

        # Remove wall-clock rAF tick() block
        src = re.sub(
            r"function\s+tick\s*\(\)\s*\{.*?\}\s*requestAnimationFrame\s*\(\s*tick\s*\)\s*;",
            "",
            src,
            flags=re.DOTALL,
        )
        # Replace any wall-clock fallback shim using performance.now/rAF with pure deterministic seek shim
        src = re.sub(
            r"var\s+shim\s*=\s*\{.*?\};\s*window\.__timelines\[\"main\"\]\s*=\s*shim;",
            (
                'var shim = { _t: 0, duration: function () { return TOTAL_S; }, '
                'time: function () { return this._t; }, '
                'seek: function (t) { this._t = Math.max(0, Math.min(TOTAL_S, t)); update(this._t); return this; }, '
                'play: function () { return this; }, pause: function () { return this; }, '
                'eventCallback: function () { return this; } };\n    window.__timelines["main"] = shim;'
            ),
            src,
            flags=re.DOTALL,
        )

    if src != orig:
        html_path.write_text(src, encoding="utf-8")
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--row", type=int, required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--html", required=True)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", default="looks")
    args = ap.parse_args()

    drive, sheets = get_services()
    row_info = read_task_row(sheets, args.row)

    # Idempotent guard: skip if valid video_final.mp4 is already linked
    existing_vid_id = extract_drive_id(row_info["video_url"])
    if existing_vid_id:
        try:
            vmeta = drive.files().get(
                fileId=existing_vid_id, fields="id,size,trashed", supportsAllDrives=True
            ).execute()
            if not vmeta.get("trashed") and int(vmeta.get("size") or 0) > 10_000:
                if row_info["status"] != "Done":
                    update_cells(sheets, {f"B{args.row}": "Done"})
                print(f"Row {args.row}: valid video already exists ({row_info['video_url']}); skipping.")
                return
        except Exception:
            pass

    proj_id, proj_name = resolve_project_folder(drive, sheets, args.row, args.project)
    root_files = list_folder_files(drive, proj_id)

    work_dir = Path("work") / f"row_{args.row}"
    proj_dir = work_dir / "proj"
    pages_dir = proj_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download HTML composition -> work/proj/index.html
    html_fid = extract_drive_id(args.html) or extract_drive_id(row_info["html_url"])
    if not html_fid and "karaoke_16x9.html" in root_files:
        html_fid = root_files["karaoke_16x9.html"]["id"]
    if not html_fid:
        raise RuntimeError(f"Row {args.row}: missing karaoke HTML file ID")
    index_html = proj_dir / "index.html"
    download_drive_file(drive, html_fid, str(index_html))

    # 2. Download optional JSON sidecars if Mushu HTML loads external JSON/JS
    for aux_name in ("manifest.json", "timings.json", "karaoke-data.js"):
        if aux_name in root_files:
            download_drive_file(drive, root_files[aux_name]["id"], str(proj_dir / aux_name))

    # 3. Download all rendered PDF page images -> work/proj/pages/
    if "pages" in root_files and root_files["pages"]["mimeType"] == "application/vnd.google-apps.folder":
        page_files = list_folder_files(drive, root_files["pages"]["id"])
        for pname, pmeta in sorted(page_files.items()):
            if pname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                download_drive_file(drive, pmeta["id"], str(pages_dir / pname))
    else:
        for fname, fmeta in root_files.items():
            if fname.startswith("page_") and fname.lower().endswith(".png"):
                download_drive_file(drive, fmeta["id"], str(pages_dir / fname))

    # 4. Download final voiceover WAV
    voice_fid = extract_drive_id(row_info["voiceover_url"])
    if not voice_fid and "voiceover_final.wav" in root_files:
        voice_fid = root_files["voiceover_final.wav"]["id"]
    if not voice_fid:
        raise RuntimeError(f"Row {args.row}: missing voiceover_final.wav")
    voice_wav = work_dir / "voiceover_final.wav"
    download_drive_file(drive, voice_fid, str(voice_wav))

    # 5. Determine exact render duration
    dur_s = None
    if (proj_dir / "timings.json").is_file():
        t_json = json.loads((proj_dir / "timings.json").read_text(encoding="utf-8"))
        dur_s = float(t_json.get("total_s") or 0) or None
    if not dur_s:
        dur_s = extract_html_duration(index_html) or probe_audio_duration(voice_wav)

    # 6. Ensure deterministic timeline contract, lint composition & render 1920x1080 16:9 silent MP4 via HyperFrames
    if sanitize_hyperframes_determinism(index_html):
        print("Normalized HTML timeline for deterministic multi-worker HyperFrames capture.")
    lint_proc = subprocess.run(
        ["hyperframes", "lint", str(proj_dir), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    print("HyperFrames lint:", (lint_proc.stdout or "").strip()[:800])

    silent_mp4 = work_dir / "silent_16x9.mp4"
    subprocess.run(
        [
            "hyperframes", "render", str(proj_dir),
            "-o", str(silent_mp4),
            "--fps", str(args.fps),
            "--start", "0",
            "--end", f"{dur_s:.3f}",
            "--quality", args.quality,
            "--workers", "auto",
        ],
        check=True,
    )

    # 7. Mux 16:9 video + -14 LUFS Peter voiceover into delivery MP4
    final_mp4 = work_dir / "video_final.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-i", str(silent_mp4),
            "-i", str(voice_wav),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level:v", "4.2",
            "-pix_fmt", "yuv420p",
            "-crf", "20",
            "-preset", "medium",
            "-af", "apad",
            "-t", f"{dur_s:.3f}",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            str(final_mp4),
        ],
        check=True,
    )

    # 8. Upload video_final.mp4 & finalize sheet row -> Video
    vid_fid = upsert_file(drive, proj_id, str(final_mp4), "video_final.mp4", "video/mp4")
    vid_url = file_view_url(vid_fid)
    update_cells(sheets, {
        f"C{args.row}": folder_url(proj_id),
        f"F{args.row}": vid_url,
        f"B{args.row}": "Done",
        f"G{args.row}": f"✅ Hoàn tất Video 16:9 ({dur_s:.1f}s)",
    })
    print(f"Completed 16:9 Karaoke MP4 for row {args.row} ({proj_name}): {vid_url}")


if __name__ == "__main__":
    main()
