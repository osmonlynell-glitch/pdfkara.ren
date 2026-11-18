#!/usr/bin/env python3
import argparse
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from google.oauth2 import credentials as oauth_credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

DEFAULT_SHEET_ID = os.environ.get(
    "KARAOKE_SHEET_ID", "1Cqv_iazZcYCkswIAEnuQL4ohaMTB13o7_iL23OtKdfc"
)
DEFAULT_TAB = os.environ.get("KARAOKE_TAB", "TASKS")
DEFAULT_TASKS_FOLDER_ID = os.environ.get(
    "KARAOKE_TASKS_FOLDER_ID", "1lV234BAYULLGPqyHgZUjB6F-epU0r4GW"
)


def get_credentials():
    raw = os.environ.get("GOOGLE_SA_KEY", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_SA_KEY environment variable")
    info = json.loads(raw)
    if info.get("type") == "authorized_user" or "refresh_token" in info:
        return oauth_credentials.Credentials.from_authorized_user_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def get_services():
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets


def extract_drive_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if not s:
        return ""
    for pat in (
        r"/folders/([A-Za-z0-9_-]{20,})",
        r"/file/d/([A-Za-z0-9_-]{20,})",
        r"/d/([A-Za-z0-9_-]{20,})",
        r"[?&]id=([A-Za-z0-9_-]{20,})",
    ):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,80}", s):
        return s
    return ""


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bridge_post(payload: dict) -> dict:
    bridge_url = os.environ.get("BRIDGE_UPLOAD_URL", "").strip()
    bridge_secret = os.environ.get("BRIDGE_UPLOAD_SECRET", "").strip()
    if not bridge_url or not bridge_secret:
        raise RuntimeError("BRIDGE_UPLOAD_URL / BRIDGE_UPLOAD_SECRET not configured")
    body = dict(payload)
    body["secret"] = bridge_secret
    data = json.dumps(body).encode("utf-8")

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        bridge_url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8", "User-Agent": "pdfkara-ren/2.4"},
    )
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location") or e.headers.get("location")
            if not loc:
                raise RuntimeError(f"Bridge redirect HTTP {e.code} missing Location")
            g_req = urllib.request.Request(loc, method="GET", headers={"User-Agent": "pdfkara-ren/2.4"})
            with urllib.request.urlopen(g_req, timeout=60) as g_resp:
                raw = g_resp.read().decode("utf-8")
        else:
            raise RuntimeError(f"Bridge HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
    out = json.loads(raw)
    if not out.get("ok"):
        raise RuntimeError(f"Bridge error: {out.get('error')}")
    return out


def _resumable_put_file(upload_url: str, local_path: Path, mime_type: str) -> str:
    total = local_path.stat().st_size
    if total == 0:
        req = urllib.request.Request(
            upload_url,
            data=b"",
            method="PUT",
            headers={"Content-Type": mime_type, "Content-Length": "0"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))["id"]

    chunk_size = 16 * 1024 * 1024
    opener = urllib.request.build_opener(_NoRedirect)
    with open(local_path, "rb") as fh:
        offset = 0
        while offset < total:
            buf = fh.read(chunk_size)
            end = offset + len(buf) - 1
            headers = {
                "Content-Type": mime_type,
                "Content-Length": str(len(buf)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            }
            req = urllib.request.Request(upload_url, data=buf, method="PUT", headers=headers)
            try:
                with opener.open(req, timeout=300) as resp:
                    if resp.status in (200, 201):
                        res_json = json.loads(resp.read().decode("utf-8"))
                        return res_json["id"]
            except urllib.error.HTTPError as e:
                if e.code == 308:
                    pass
                else:
                    detail = e.read().decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"Resumable PUT failed HTTP {e.code}: {detail}")
            offset = end + 1
    raise RuntimeError("Resumable upload completed bytes without final 200/201 JSON")


def read_task_row(sheets, row: int, sheet_id: str = DEFAULT_SHEET_ID, tab: str = DEFAULT_TAB):
    rng = f"{tab}!A{row}:F{row}"
    res = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    vals = (res.get("values") or [[]])[0]
    while len(vals) < 6:
        vals.append("")
    return {
        "row": row,
        "pdf_link": vals[0].strip(),
        "status": vals[1].strip(),
        "project_url": vals[2].strip(),
        "voiceover_url": vals[3].strip(),
        "html_url": vals[4].strip(),
        "video_url": vals[5].strip(),
    }


def list_all_task_rows(sheets, sheet_id: str = DEFAULT_SHEET_ID, tab: str = DEFAULT_TAB):
    rng = f"{tab}!A2:F300"
    res = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    rows = []
    for idx, vals in enumerate(res.get("values") or [], start=2):
        padded = list(vals) + [""] * max(0, 6 - len(vals))
        if not padded[0].strip() and not padded[1].strip():
            continue
        rows.append({
            "row": idx,
            "pdf_link": padded[0].strip(),
            "status": padded[1].strip(),
            "project_url": padded[2].strip(),
            "voiceover_url": padded[3].strip(),
            "html_url": padded[4].strip(),
            "video_url": padded[5].strip(),
        })
    return rows


def update_cells(sheets, cells_map: dict, sheet_id: str = DEFAULT_SHEET_ID, tab: str = DEFAULT_TAB):
    data = [
        {"range": f"{tab}!{col_row}", "values": [[val]]}
        for col_row, val in cells_map.items()
    ]
    for attempt in range(4):
        try:
            svc = sheets if attempt == 0 else get_services()[1]
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def resolve_project_folder(drive, sheets, row: int, project_arg: str = "") -> tuple[str, str]:
    info = read_task_row(sheets, row)
    existing_id = extract_drive_id(info["project_url"]) or extract_drive_id(project_arg)
    if existing_id:
        meta = drive.files().get(fileId=existing_id, fields="id,name,mimeType,trashed", supportsAllDrives=True).execute()
        if meta.get("mimeType") == "application/vnd.google-apps.folder" and not meta.get("trashed"):
            return meta["id"], meta["name"]

    proj_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", (project_arg or f"PDF_ROW_{row}")).strip("_")
    q = (
        f"'{DEFAULT_TASKS_FOLDER_ID}' in parents and name = '{proj_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    res = drive.files().list(
        q=q, fields="files(id,name)", pageSize=5, supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = res.get("files") or []
    if files:
        return files[0]["id"], files[0]["name"]
    raise RuntimeError(f"Project folder not found for row {row} ({project_arg})")


def list_folder_files(drive, folder_id: str) -> dict:
    for attempt in range(4):
        try:
            svc = drive if attempt == 0 else get_services()[0]
            out = {}
            page_token = None
            while True:
                res = svc.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id,name,size,mimeType,modifiedTime)",
                    pageSize=500,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                for f in res.get("files") or []:
                    out[f["name"]] = f
                page_token = res.get("nextPageToken")
                if not page_token:
                    break
            return out
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return {}


def download_drive_file(drive, file_id: str, out_path: str):
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            svc = drive if attempt == 0 else get_services()[0]
            req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
            with io.FileIO(str(dest), "wb") as fh:
                downloader = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def upsert_file(drive, folder_id: str, local_path: str, remote_name: str = "", mime_type: str = "application/octet-stream") -> str:
    p = Path(local_path)
    name = remote_name or p.name
    use_bridge = bool(
        os.environ.get("BRIDGE_UPLOAD_URL", "").strip()
        and os.environ.get("BRIDGE_UPLOAD_SECRET", "").strip()
    )

    if use_bridge:
        for attempt in range(4):
            try:
                session = _bridge_post({
                    "action": "init_upload",
                    "folder_id": folder_id,
                    "filename": name,
                    "mime_type": mime_type,
                })
                return _resumable_put_file(session["upload_url"], p, mime_type)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    escaped = name.replace("'", "\\'")
    q = f"'{folder_id}' in parents and name = '{escaped}' and trashed = false"
    res = drive.files().list(
        q=q, fields="files(id)", pageSize=5, supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = res.get("files") or []
    media = MediaFileUpload(str(p), mimetype=mime_type, resumable=p.stat().st_size > 2 * 1024 * 1024)

    for attempt in range(4):
        try:
            if files:
                fid = files[0]["id"]
                drive.files().update(fileId=fid, media_body=media, supportsAllDrives=True).execute()
                return fid
            created = drive.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            return created["id"]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def dispatch_github_workflow(repo_full_name: str, workflow_file: str, inputs: dict, ref: str = "main"):
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing GH_TOKEN for workflow_dispatch")
    url = f"https://api.github.com/repos/{repo_full_name}/actions/workflows/{workflow_file}/dispatches"
    body = json.dumps({"ref": ref, "inputs": {k: str(v) for k, v in inputs.items()}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"workflow_dispatch unexpected HTTP {resp.status}")


def list_active_render_titles(repo_full_name: str) -> list[str]:
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token or not repo_full_name:
        return []
    titles = []
    for st in ("in_progress", "queued"):
        url = (
            f"https://api.github.com/repos/{repo_full_name}/actions/workflows/render.yml/runs"
            f"?status={st}&event=workflow_dispatch&per_page=30"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for run in data.get("workflow_runs") or []:
                titles.append(str(run.get("display_title") or run.get("name") or ""))
        except Exception as exc:
            print(f"Warning checking active render runs ({st}): {exc}", flush=True)
    return titles


def cli_poll_html(args):
    _, sheets = get_services()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    rows = list_all_task_rows(sheets)
    active_titles = list_active_render_titles(repo)
    dispatched = 0
    for r in rows:
        if dispatched >= args.max_dispatch:
            break
        if r["status"] in ("Html", "Video") and r["project_url"] and r["html_url"] and not r["video_url"] and repo:
            tag = f"R{r['row']}"
            if any(t == f"Render {tag}" or f"{tag} " in t or t.endswith(tag) for t in active_titles):
                print(f"Skipping row {r['row']}: render.yml already active")
                continue
            dispatch_github_workflow(
                repo,
                "render.yml",
                {
                    "row": str(r["row"]),
                    "project_name": r["project_url"],
                    "html_url": r["html_url"],
                },
            )
            dispatched += 1
            print(f"Backup poll dispatched render for row {r['row']}")
    print(f"Render poll finished, dispatched={dispatched}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_poll = sub.add_parser("poll-html-rows")
    p_poll.add_argument("--max-dispatch", type=int, default=5)
    args = ap.parse_args()
    if args.cmd == "poll-html-rows":
        cli_poll_html(args)


if __name__ == "__main__":
    main()
