import os
import uuid
import logging
import tempfile
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

load_dotenv()

YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "")
FACEBOOK_COOKIES = os.getenv("FACEBOOK_COOKIES", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Video Downloader")
templates = Jinja2Templates(directory="templates")

# Dossier temporaire pour les fichiers téléchargés
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_DIR = Path("/tmp/cookies")
COOKIES_DIR.mkdir(exist_ok=True)
YT_COOKIES_FILE = COOKIES_DIR / "youtube.txt"
FB_COOKIES_FILE = COOKIES_DIR / "facebook.txt"

SUPPORTED = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "fb.com": "Facebook",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
}

def fix_cookies(content: str) -> str:
    content = content.replace("\\n", "\n")
    content = content.replace("\\t", "\t")
    return content

def setup_cookies():
    if YOUTUBE_COOKIES:
        YT_COOKIES_FILE.write_text(fix_cookies(YOUTUBE_COOKIES), encoding="utf-8")
        logger.info("✅ Cookies YouTube chargés")
    if FACEBOOK_COOKIES:
        FB_COOKIES_FILE.write_text(fix_cookies(FACEBOOK_COOKIES), encoding="utf-8")
        logger.info("✅ Cookies Facebook chargés")

setup_cookies()

def detect_platform(url: str):
    for domain, name in SUPPORTED.items():
        if domain in url:
            return name
    return None

def get_cookies_args(platform: str) -> list:
    if platform == "YouTube" and YT_COOKIES_FILE.exists():
        return ["--cookies", str(YT_COOKIES_FILE)]
    elif platform == "Facebook" and FB_COOKIES_FILE.exists():
        return ["--cookies", str(FB_COOKIES_FILE)]
    return []

def run_ytdlp(cmd: list, timeout=300) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True
        logger.error(f"yt-dlp: {result.stderr[:400]}")
    except Exception as e:
        logger.error(f"yt-dlp exception: {e}")
    return False

def convert_to_mp4(input_path: Path, output_path: Path) -> Path:
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c:v", "copy", "-c:a", "copy",
        "-movflags", "+faststart", str(output_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and output_path.exists():
            return output_path
        cmd2 = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "+faststart", str(output_path)
        ]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
        if result2.returncode == 0 and output_path.exists():
            return output_path
    except Exception as e:
        logger.error(f"Conversion: {e}")
    return input_path

def dl_video(url: str, output_dir: Path, platform: str) -> Path:
    uid = str(uuid.uuid4())[:8]
    output_file = output_dir / f"video_{uid}.mp4"
    cookies = get_cookies_args(platform)

    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "bestvideo[height>=720][ext=mp4]/bestvideo[height>=720]/bestvideo[ext=mp4]/bestvideo",
        "--merge-output-format", "mp4",
        "-o", str(output_file),
    ] + cookies + [url]

    if run_ytdlp(cmd) and output_file.exists():
        return output_file

    # Chercher fichier converti
    for f in output_dir.iterdir():
        if f.suffix in (".webm", ".mkv", ".mov"):
            return convert_to_mp4(f, output_file)
    return None

def dl_audio(url: str, output_dir: Path, platform: str) -> Path:
    uid = str(uuid.uuid4())[:8]
    output_mp3 = output_dir / f"audio_{uid}.mp3"
    cookies = get_cookies_args(platform)

    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", str(output_mp3),
    ] + cookies + [url]

    if run_ytdlp(cmd) and output_mp3.exists():
        return output_mp3

    output_m4a = output_dir / f"audio_{uid}.m4a"
    cmd2 = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "-o", str(output_m4a),
    ] + cookies + [url]

    if run_ytdlp(cmd2) and output_m4a.exists():
        cmd_conv = [
            "ffmpeg", "-y", "-i", str(output_m4a),
            "-codec:a", "libmp3lame", "-qscale:a", "0", str(output_mp3)
        ]
        try:
            result = subprocess.run(cmd_conv, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and output_mp3.exists():
                return output_mp3
        except Exception as e:
            logger.error(f"Conversion m4a→mp3: {e}")
        return output_m4a
    return None

# ── Routes ────────────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str
    mode: str  # "video", "audio", "both"

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/download")
async def download(req: DownloadRequest):
    url = req.url.strip()
    mode = req.mode

    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")

    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Plateforme non supportée. Utilise YouTube, Facebook ou Twitter/X.")

    output_dir = DOWNLOAD_DIR / str(uuid.uuid4())
    output_dir.mkdir(parents=True, exist_ok=True)

    result = {"platform": platform, "files": []}

    if mode in ("video", "both"):
        video = dl_video(url, output_dir, platform)
        if video and video.exists():
            result["files"].append({
                "type": "video",
                "name": video.name,
                "path": str(video),
                "size": f"{video.stat().st_size / 1024 / 1024:.1f} Mo"
            })
        elif mode == "video":
            raise HTTPException(500, "Échec du téléchargement vidéo")

    if mode in ("audio", "both"):
        audio = dl_audio(url, output_dir, platform)
        if audio and audio.exists():
            result["files"].append({
                "type": "audio",
                "name": audio.name,
                "path": str(audio),
                "size": f"{audio.stat().st_size / 1024 / 1024:.1f} Mo"
            })
        elif mode == "audio":
            raise HTTPException(500, "Échec du téléchargement audio")

    if not result["files"]:
        raise HTTPException(500, "Échec du téléchargement")

    return result

@app.get("/file")
async def serve_file(path: str, filename: str):
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream"
    )
