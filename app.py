import os
import uuid
import json
import logging
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

load_dotenv()

FACEBOOK_COOKIES  = os.getenv("FACEBOOK_COOKIES", "")
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WILISKO DropVid")
templates = Jinja2Templates(directory="templates")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_DIR = Path("/tmp/cookies")
COOKIES_DIR.mkdir(exist_ok=True)
FB_COOKIES_FILE = COOKIES_DIR / "facebook.txt"
IG_COOKIES_FILE = COOKIES_DIR / "instagram.txt"

SUPPORTED = {
    "facebook.com":  "Facebook",
    "fb.watch":      "Facebook",
    "fb.com":        "Facebook",
    "twitter.com":   "Twitter/X",
    "x.com":         "Twitter/X",
    "t.co":          "Twitter/X",
    "tiktok.com":    "TikTok",
    "vm.tiktok.com": "TikTok",
    "vt.tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "instagr.am":    "Instagram",
}

def fix_cookies(content: str) -> str:
    return content.replace("\\n", "\n").replace("\\t", "\t")

def setup_cookies():
    if FACEBOOK_COOKIES:
        FB_COOKIES_FILE.write_text(fix_cookies(FACEBOOK_COOKIES), encoding="utf-8")
        logger.info("✅ Cookies Facebook chargés")
    if INSTAGRAM_COOKIES:
        IG_COOKIES_FILE.write_text(fix_cookies(INSTAGRAM_COOKIES), encoding="utf-8")
        logger.info("✅ Cookies Instagram chargés")

setup_cookies()

def detect_platform(url: str):
    for domain, name in SUPPORTED.items():
        if domain in url:
            return name
    return None

def get_cookies_args(platform: str) -> list:
    if platform == "Facebook"  and FB_COOKIES_FILE.exists():
        return ["--cookies", str(FB_COOKIES_FILE)]
    if platform == "Instagram" and IG_COOKIES_FILE.exists():
        return ["--cookies", str(IG_COOKIES_FILE)]
    return []

def get_useragent(platform: str) -> list:
    agents = {
        "TikTok":    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Instagram": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Twitter/X": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Facebook":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }
    ua = agents.get(platform)
    return ["--add-header", f"User-Agent:{ua}"] if ua else []

def run_ytdlp(cmd: list, timeout=300) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True
        logger.error(f"yt-dlp: {result.stderr[:500]}")
    except Exception as e:
        logger.error(f"yt-dlp exception: {e}")
    return False

def remux_to_mp4(input_path: Path, output_path: Path) -> Path:
    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-c", "copy", "-movflags", "+faststart", str(output_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and output_path.exists():
            return output_path
    except Exception as e:
        logger.error(f"Remux: {e}")
    return input_path

# ── Analyse du format réel de la vidéo ──────────────────────────────────────

def get_video_info(url: str, platform: str) -> dict:
    """Récupère les formats disponibles sans télécharger."""
    cookies = get_cookies_args(platform)
    ua      = get_useragent(platform)

    extra = []
    if platform == "TikTok":
        extra = ["--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com"]

    cmd = (["yt-dlp", "--no-playlist", "--no-warnings",
             "--dump-json", "--no-download"]
            + cookies + ua + extra + [url])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            formats = data.get("formats", [])

            # Collecter toutes les résolutions vidéo disponibles
            resolutions = []
            seen = set()
            for f in formats:
                h = f.get("height")
                w = f.get("width")
                fps = f.get("fps")
                vcodec = f.get("vcodec", "none")
                ext = f.get("ext", "")
                tbr = f.get("tbr")  # bitrate total kb/s
                if h and vcodec != "none" and h not in seen:
                    seen.add(h)
                    label = f"{h}p"
                    if h >= 2160: label = f"4K ({h}p)"
                    elif h >= 1440: label = f"2K ({h}p)"
                    elif h >= 1080: label = f"Full HD ({h}p)"
                    elif h >= 720:  label = f"HD ({h}p)"
                    else:           label = f"SD ({h}p)"
                    resolutions.append({
                        "height": h,
                        "width": w,
                        "label": label,
                        "fps": round(fps) if fps else None,
                        "ext": ext,
                        "bitrate_kbps": round(tbr) if tbr else None,
                    })

            resolutions.sort(key=lambda x: x["height"], reverse=True)

            # Info générale
            best = resolutions[0] if resolutions else None
            return {
                "title": data.get("title", ""),
                "duration": data.get("duration"),
                "thumbnail": data.get("thumbnail", ""),
                "original_format": best,
                "all_formats": resolutions,
                "platform": platform,
                "uploader": data.get("uploader", data.get("channel", "")),
            }
    except Exception as e:
        logger.error(f"get_video_info: {e}")
    return {}

# ── Téléchargement ───────────────────────────────────────────────────────────

def dl_video(url: str, output_dir: Path, platform: str, force_hd: bool = False) -> Path:
    uid     = str(uuid.uuid4())[:8]
    final   = output_dir / f"video_{uid}.mp4"
    cookies = get_cookies_args(platform)
    ua      = get_useragent(platform)

    # Sélecteur de format selon force_hd
    if force_hd:
        # Force 1080p minimum, prend la meilleure dispo au-dessus
        fmt_selector = "bestvideo[height>=1080]+bestaudio/bestvideo[height>=720]+bestaudio/bestvideo+bestaudio/best"
    else:
        # Qualité originale (résolution postée exacte)
        fmt_selector = "bestvideo+bestaudio/best"

    # ── TikTok ───────────────────────────────────────────────────────────────
    if platform == "TikTok":
        extra = ["--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com"]
        attempts = [
            ["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", "download_addr-2/play_addr_h264-0/play_addr-0/" + fmt_selector,
             "--merge-output-format", "mp4"] + extra + ["-o", str(final), url],
            ["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", fmt_selector, "--merge-output-format", "mp4"] + ua + ["-o", str(final), url],
            ["yt-dlp", "--no-playlist", "--no-warnings",
             "--merge-output-format", "mp4", "-o", str(final), url],
        ]
        for i, cmd in enumerate(attempts, 1):
            if run_ytdlp(cmd) and final.exists():
                logger.info(f"✅ TikTok (méthode {i})")
                return final
        return None

    # ── Instagram ─────────────────────────────────────────────────────────────
    if platform == "Instagram":
        for cmd in [
            ["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", fmt_selector, "--merge-output-format", "mp4"] + cookies + ua + ["-o", str(final), url],
            ["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", fmt_selector, "--merge-output-format", "mp4"] + ua + ["-o", str(final), url],
        ]:
            if run_ytdlp(cmd) and final.exists():
                return final
        return None

    # ── Facebook & Twitter/X ─────────────────────────────────────────────────
    cmd_best = (
        ["yt-dlp", "--no-playlist", "--no-warnings",
         "-f", fmt_selector,
         "--merge-output-format", "mp4",
         "--postprocessor-args", "ffmpeg:-c copy -movflags +faststart",
         "-o", str(final)]
        + cookies + ua + [url]
    )
    if run_ytdlp(cmd_best) and final.exists():
        logger.info(f"✅ {platform} : {final.stat().st_size/1024/1024:.1f} Mo")
        return final

    cmd_fallback = (
        ["yt-dlp", "--no-playlist", "--no-warnings",
         "-f", fmt_selector, "--merge-output-format", "mp4", "-o", str(final)]
        + cookies + ua + [url]
    )
    if run_ytdlp(cmd_fallback) and final.exists():
        return final

    for f in output_dir.iterdir():
        if f.suffix in (".webm", ".mkv", ".mov") and f != final:
            return remux_to_mp4(f, final)
    return None

def dl_audio(url: str, output_dir: Path, platform: str) -> Path:
    uid     = str(uuid.uuid4())[:8]
    mp3     = output_dir / f"audio_{uid}.mp3"
    m4a     = output_dir / f"audio_{uid}.m4a"
    cookies = get_cookies_args(platform)
    ua      = get_useragent(platform)
    extra   = ["--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com"] if platform == "TikTok" else []

    cmd = (["yt-dlp", "--no-playlist", "--no-warnings",
             "-f", "bestaudio", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "0",
             "-o", str(mp3)] + cookies + ua + extra + [url])
    if run_ytdlp(cmd) and mp3.exists():
        return mp3

    cmd2 = (["yt-dlp", "--no-playlist", "--no-warnings",
              "-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(m4a)] + cookies + ua + extra + [url])
    if run_ytdlp(cmd2) and m4a.exists():
        conv = ["ffmpeg", "-y", "-i", str(m4a), "-codec:a", "libmp3lame", "-qscale:a", "0", str(mp3)]
        try:
            r = subprocess.run(conv, capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and mp3.exists():
                return mp3
        except Exception as e:
            logger.error(f"m4a→mp3: {e}")
        return m4a
    return None

# ── Routes ────────────────────────────────────────────────────────────────────

class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    mode: str
    force_hd: bool = False

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/info")
async def info(req: InfoRequest):
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Plateforme non supportée.")
    data = get_video_info(url, platform)
    if not data:
        raise HTTPException(500, "Impossible de récupérer les infos de la vidéo.")
    return data

@app.post("/download")
async def download(req: DownloadRequest):
    url      = req.url.strip()
    mode     = req.mode
    force_hd = req.force_hd

    if not url.startswith("http"):
        raise HTTPException(400, "URL invalide")
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(400, "Plateforme non supportée. Utilise Facebook, TikTok, Instagram ou Twitter/X.")

    output_dir = DOWNLOAD_DIR / str(uuid.uuid4())
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {"platform": platform, "files": []}

    if mode in ("video", "both"):
        video = dl_video(url, output_dir, platform, force_hd=force_hd)
        if video and video.exists():
            result["files"].append({
                "type": "video", "name": video.name,
                "path": str(video),
                "size": f"{video.stat().st_size/1024/1024:.1f} Mo"
            })
        elif mode == "video":
            raise HTTPException(500, "Échec du téléchargement vidéo")

    if mode in ("audio", "both"):
        audio = dl_audio(url, output_dir, platform)
        if audio and audio.exists():
            result["files"].append({
                "type": "audio", "name": audio.name,
                "path": str(audio),
                "size": f"{audio.stat().st_size/1024/1024:.1f} Mo"
            })
        elif mode == "audio":
            raise HTTPException(500, "Échec du téléchargement audio")

    if not result["files"]:
        raise HTTPException(500, "Échec du téléchargement")
    return result

@app.get("/file")
async def serve_file(path: str, filename: str):
    fp = Path(path)
    if not fp.exists():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(path=str(fp), filename=filename, media_type="application/octet-stream")
