import os
import re
import time
import uuid
import threading
import requests
import logging
import subprocess
import yt_dlp
from flask import Flask, request, jsonify, send_file, Response, stream_with_context


# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def mask_url(url, keep=50):
    """
    Masking URL untuk log — potong sebelum query string (?token=...).
    Hanya tampilkan domain + N karakter pertama path.
    Contoh: https://v19.tiktok.com/video/tos/abc123...[masked]
    """
    if not url:
        return '[empty url]'
    try:
        base = url.split('?')[0]
        if len(base) > keep:
            return base[:keep] + '...[masked]'
        return base
    except Exception:
        return '[url]' 


# =============================================================================
# TELEGRAM NOTIF
# =============================================================================

TELEGRAM_NOTIF_ENABLED = True # Ganti ke True untuk aktifkan notif Telegram                                                                  #  Ganti ke  False untuk matikan notif Telegram


# =============================================================================
# FIX #1: Token Telegram dipindah ke environment variable
# Set di Railway/server: TELEGRAM_TOKEN dan TELEGRAM_CHAT_ID
# JANGAN hardcode token di source code!
# =============================================================================
_TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if TELEGRAM_NOTIF_ENABLED and (not _TELEGRAM_TOKEN or not _TELEGRAM_CHAT_ID):
    logger.warning(
        "[NOTIF] TELEGRAM_TOKEN atau TELEGRAM_CHAT_ID tidak ditemukan di env. "
        "Notif Telegram dinonaktifkan. Set env var untuk mengaktifkan."
    )
    TELEGRAM_NOTIF_ENABLED = False

def kirim_notif(pesan):
    """Kirim notifikasi ke Telegram Bot."""
    if not TELEGRAM_NOTIF_ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": _TELEGRAM_CHAT_ID, "text": pesan},
            timeout=3
        )
    except Exception as e:
        logger.warning(f"[NOTIF] Gagal kirim notif Telegram: {e}")

app = Flask(__name__, static_folder='static', static_url_path='')
# FIX #7: static_folder dipindah dari '.' (root) ke folder 'static' tersendiri
# Sebelumnya semua file di root (termasuk vinder_fixed.py, .env) bisa diakses via URL

# FIX #2: CORS dibatasi ke origin tertentu saja
# Tambahkan domain produksi lo ke list ini, atau set env var CORS_ORIGINS
_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5000,http://127.0.0.1:5000"   # default: hanya local dev
).split(",")

from flask_cors import CORS
CORS(app, origins=_ALLOWED_ORIGINS)

# =============================================================================
# RATE LIMITING
# =============================================================================
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)

def on_rate_limit_exceeded(e):
    ip   = get_remote_address()
    path = request.path
    batas_map = {
        '/api/search':       '10x/menit',
        '/api/download_url': '20x/menit',
        '/api/fast_mp3':     '15x/menit',
    }
    batas = batas_map.get(path, 'batas limit')
    kirim_notif(
        f"⚠️ Rate Limit Terlampaui!\n"
        f"User IP: {ip}\n"
        f"Endpoint: {path}\n"
        f"Melebihi batas {batas}"
    )
    return "Terlalu banyak permintaan. Silakan tunggu sebentar.", 429

app.register_error_handler(429, on_rate_limit_exceeded)

TIKTOK_UA = (
    "com.zhiliaoapp.musically/2022505030 "
    "(Linux; U; Android 12; en_US; Pixel 6; Build/SQ3A.220705.004; Cronet/58.0.2991.0)"
)

DEFAULT_HEADERS = {
    "User-Agent":      TIKTOK_UA,
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection":      "keep-alive",
}

TIKTOK_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer":         "https://www.tiktok.com/",
    "Origin":          "https://www.tiktok.com",
    "Accept-Encoding": "identity",
}

session = requests.Session()

# =============================================================================
# PRE-FETCHING CONNECTION POOL
# Perbesar pool koneksi agar request paralel ke TikWM/CDN tidak ngantre
# Default requests: pool_connections=10, pool_maxsize=10
# =============================================================================
from requests.adapters import HTTPAdapter
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
session.mount('https://', _adapter)
session.mount('http://',  _adapter)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def format_durasi(detik):
    """Format detik ke string 'Xm00s'."""
    if detik is None:
        return "?"
    try:
        m, s = divmod(int(detik), 60)
        return f"{m}m{s:02d}s"
    except Exception:
        return "?"


def parse_filter_durasi(filter_str):
    """
    Parse string filter durasi ke (operator, detik).
    Format: '< 30 s', '> 5 m', '< 2 h'  (spasi bebas, case-insensitive)
    Satuan: s = detik, m = menit, h = jam
    Return: (operator, total_detik) atau (None, None) kalau gagal parse.
    """
    if not filter_str:
        return None, None
    try:
        f = filter_str.strip().lower()
        match = re.match(r'^([<>])\s*(\d+(?:\.\d+)?)\s*([smh])$', f)
        if not match:
            return None, None
        op, angka, satuan = match.group(1), float(match.group(2)), match.group(3)
        multiplier = {'s': 1, 'm': 60, 'h': 3600}[satuan]
        return op, angka * multiplier
    except Exception:
        return None, None


def lolos_filter(durasi_detik, op, batas_detik):
    """Cek apakah durasi video lolos filter. Return True kalau lolos."""
    if op is None or durasi_detik is None:
        return True
    try:
        d = float(durasi_detik)
        if op == '<':
            return d < batas_detik
        if op == '>':
            return d > batas_detik
    except Exception:
        pass
    return True


def resolve_tiktok_url(url):
    """Resolve short URL (vt.tiktok.com / vm.tiktok.com) ke URL panjang."""
    try:
        r = session.head(url, allow_redirects=True, timeout=10)
        logger.info(f"[URL] Resolved: {mask_url(url)} -> {mask_url(r.url)}")
        return r.url
    except Exception as e:
        logger.warning(f"[WARN] Gagal resolve URL: {e}")
        return url


def safe_filename(title, max_len=60):
    """
    Bersihkan judul jadi nama file yang aman.
    - Hapus karakter berbahaya OS: \\ / : * ? " < > |
    - Hapus token yang diawali # (hashtag) atau @ (mention)
    - Pertahankan emoji, unicode, font aneh, simbol umum, spasi
    """
    # Hapus karakter berbahaya untuk nama file (OS-level)
    cleaned = re.sub(r'[\\/:*?"<>|]', '', title)
    # Hapus karakter kontrol
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    # Hapus token hashtag (#kata) dan mention (@kata)
    cleaned = re.sub(r'[#@]\S*', '', cleaned)
    # Bersihkan spasi berlebih
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len] or 'vinder'


def make_content_disposition(filename):
    """
    Buat header Content-Disposition yang aman untuk filename berisi
    emoji / unicode / karakter non-ASCII (RFC 5987).
    Browser modern baca filename* (UTF-8 encoded), browser lama baca
    filename fallback (ASCII-only).
    """
    from urllib.parse import quote
    ascii_fallback = filename.encode('ascii', errors='replace').decode('ascii').replace('?', '_')
    utf8_encoded = quote(filename, safe=" !()\'~")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_encoded}"


def do_cleanup(out_tmpl):
    """Hapus semua file temp yang terkait satu sesi download."""
    suffixes = ['.mp3', '.mp3.raw', '_cover.jpg', '.ready']
    for suffix in suffixes:
        path = out_tmpl + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


# =============================================================================
# GLOBAL ORPHAN CLEANUP
# Background thread: hapus file /tmp/vinder_* yang umurnya > 60 menit
# Jalan otomatis tiap 10 menit, menangani kasus user nutup browser di tengah download
# =============================================================================

def orphan_cleanup_loop():
    """Scan dan hapus file temp vinder yang terbengkalai di /tmp."""
    MAX_AGE_SECONDS = 60 * 60       # 60 menit
    INTERVAL        = 10 * 60       # cek tiap 10 menit
    SUFFIXES        = ['.mp3', '.mp3.raw', '_cover.jpg', '.ready', '.thumb.jpg']

    while True:
        try:
            now = time.time()
            deleted = 0
            for fname in os.listdir('/tmp'):
                if not fname.startswith('vinder_'):
                    continue
                fpath = os.path.join('/tmp', fname)
                try:
                    age = now - os.path.getmtime(fpath)
                    if age > MAX_AGE_SECONDS:
                        os.remove(fpath)
                        deleted += 1
                except Exception:
                    pass
            if deleted:
                logger.info(f"[CLEANUP] Orphan cleanup: {deleted} file temp dihapus dari /tmp")
                kirim_notif(f"🧹 Orphan Cleanup!\n{deleted} file temp berhasil dihapus dari /tmp")
        except Exception as e:
            logger.warning(f"[CLEANUP] Orphan cleanup error: {e}")
        time.sleep(INTERVAL)

# Jalankan background thread saat server start
_cleanup_thread = threading.Thread(target=orphan_cleanup_loop, daemon=True)
_cleanup_thread.start()
logger.info("[CLEANUP] Orphan cleanup thread aktif (interval 10 menit, max age 60 menit)")


# =============================================================================
# VIDEO / AUDIO FUNCTIONS
# =============================================================================

def fetch_video_stream(url, fallback_url=None):
    """Stream video langsung dari URL, dengan validasi content-type."""
    headers = DEFAULT_HEADERS.copy()

    if "tiktok.com" in url or "ttwstatic.com" in url:
        headers["Referer"] = "https://www.tiktok.com/"
        headers["Origin"]  = "https://www.tiktok.com"
    else:
        domain = re.search(r'https?://([^/]+)', url)
        if domain:
            headers["Origin"]  = f"https://{domain.group(1)}"
            headers["Referer"] = f"https://{domain.group(1)}/"

    headers.update({"Accept-Encoding": "identity", "Range": "bytes=0-"})

    try:
        r = session.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        content_type   = r.headers.get('Content-Type', '').lower()
        content_length = int(r.headers.get('Content-Length', 0))

        # FIX: blokir HTML/JSON tanpa andal Content-Length
        # CDN publik sering tidak kirim Content-Length, cek content-type saja
        if 'text/html' in content_type or 'application/json' in content_type:
            logger.warning(f"[WARN] Blokir non-video content: {content_type}")
            if fallback_url:
                return session.get(
                    fallback_url, stream=True, timeout=30,
                    headers=headers, allow_redirects=True
                ), True
            return None, False

        return r, False

    except Exception as e:
        logger.error(f"Stream Error: {e}")
        if fallback_url:
            return session.get(
                fallback_url, stream=True, timeout=30,
                headers=headers, allow_redirects=True
            ), True
        raise


def get_meta_via_tikwm(tiktok_url, retries=3, for_audio=False):
    """
    Ambil metadata video dari TikWM API dengan retry otomatis.
    for_audio=True  -> pakai play (SD/360p) - audio track sama, video lebih ringan
    for_audio=False -> pakai hdplay (HD) - untuk download video
    """
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(
                f"https://www.tikwm.com/api/?url={tiktok_url}",
                timeout=15
            )
            data = resp.json()

            if data.get('code') == 0:
                v         = data['data']
                if for_audio:
                    video_url = v.get('wmplay') or v.get('play')
                    logger.info(f"[OK] TikWM OK - pakai SD URL untuk audio (attempt {attempt})")
                else:
                    video_url = v.get('hdplay') or v.get('play')
                    logger.info(f"[OK] TikWM OK - pakai HD URL untuk video (attempt {attempt})")
                # Coba origin_cover dulu, fallback ke cover biasa
                origin_cover = v.get('origin_cover')
                cover_plain  = v.get('cover')
                cover_url    = origin_cover or cover_plain
                title        = v.get('title', 'audio')
                logger.info(f"[IMG] Cover art tersedia: {'Ya' if origin_cover else 'Tidak'}")
                logger.info(f"[IMG] Cover fallback tersedia: {'Ya' if cover_plain else 'Tidak'}")
                return video_url, cover_url, title
            else:
                logger.warning(f"[WARN] TikWM code={data.get('code')} msg={data.get('msg')} (attempt {attempt})")
                logger.warning(f"[WARN] TikWM raw response: {str(data)[:200]}")

        except Exception as e:
            logger.warning(f"[WARN] TikWM gagal attempt {attempt}: {e}")

        if attempt < retries:
            time.sleep(1.5 * attempt)

    return None, None, None


def detect_audio_bitrate(url, headers):
    """
    Detect bitrate audio asli dari URL via ffprobe.
    Return bitrate dalam format string e.g. '128k', '96k'.
    Fallback ke '128k' kalau gagal detect.
    """
    try:
        probe = subprocess.run(
            [
                'ffprobe', '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                '-select_streams', 'a:0',
                url,
            ],
            capture_output=True, timeout=15,
            env={**__import__('os').environ, 'FFPROBE_USER_AGENT': headers.get('User-Agent', '')},
        )
        import json
        data = json.loads(probe.stdout.decode())
        streams = data.get('streams', [])
        if streams:
            br = streams[0].get('bit_rate')
            if br:
                kbps = int(br) // 1000
                # Bulatkan ke nilai standar MP3: 64, 96, 128, 160, 192
                for std in [64, 96, 128, 160, 192]:
                    if kbps <= std:
                        logger.info(f"[PROBE] Bitrate asli: {kbps}k -> pakai {std}k")
                        return f"{std}k"
                return "192k"
    except Exception as e:
        logger.warning(f"[WARN] ffprobe gagal: {e} -> fallback 128k")
    return "128k"


def download_audio_direct(audio_url, out_mp3):
    """
    Pipe audio/video URL langsung ke ffmpeg tanpa buffer ke disk.
    Bitrate MP3 output mengikuti bitrate audio asli dari source.
    """
    headers = TIKTOK_HEADERS.copy()
    headers["Range"] = "bytes=0-"

    logger.info(f"[DL] Pipe audio ke ffmpeg: {mask_url(audio_url)}")

    # Detect bitrate asli dulu sebelum download
    bitrate = detect_audio_bitrate(audio_url, headers)

    r = session.get(audio_url, stream=True, timeout=60, headers=headers, allow_redirects=True)
    r.raise_for_status()

    content_type = r.headers.get('Content-Type', '').lower()
    logger.info(f"[PKG] Content-Type: {content_type} | Target bitrate: {bitrate}")

    # Pipe stream langsung ke ffmpeg via stdin - tanpa temp file
    cmd = [
        'ffmpeg', '-y',
        '-i', 'pipe:0',          # baca dari stdin
        '-vn',                   # buang video track
        '-acodec', 'libmp3lame',
        '-ab', bitrate,          # ikuti bitrate asli source
        '-ar', '44100',
        out_mp3,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk:
                proc.stdin.write(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass

    proc.wait(timeout=120)

    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors='ignore')[-300:]
        raise RuntimeError("Gagal memproses audio, silakan coba lagi.")

    size_mb = os.path.getsize(out_mp3) / 1024 / 1024
    logger.info(f"[MP3] Encode selesai: {size_mb:.2f} MB ({bitrate})")


def download_audio_ytdlp(url, out_mp3):
    """
    Download audio asli video via yt-dlp dengan format bestaudio.
    Dipakai untuk YouTube, Instagram, Twitter/X, Facebook.
    Tidak download video sama sekali - langsung ambil audio stream.
    """
    ydl_opts = {
        'format':        'bestaudio/best',
        'outtmpl':       out_mp3 + '.%(ext)s',
        'quiet':         True,
        'no_warnings':   True,
        'noplaylist':    True,
        'user_agent':    TIKTOK_UA,
        'http_headers':  DEFAULT_HEADERS,
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',    # 0 = ikuti bitrate asli source
        }],
        # Pastikan output final adalah file .mp3
        'keepvideo': False,
    }

    logger.info(f"[DL] Proses audio bestaudio: {mask_url(url)}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # yt-dlp output: out_mp3.mp3 (karena postprocessor rename)
    expected = out_mp3 + '.mp3'
    if os.path.exists(expected):
        os.replace(expected, out_mp3)
        logger.info(f"[OK] yt-dlp audio selesai: {out_mp3}")
    elif os.path.exists(out_mp3):
        logger.info(f"[OK] yt-dlp audio selesai (langsung): {out_mp3}")
    else:
        # fallback scan file hasil yt-dlp
        import glob
        candidates = glob.glob(out_mp3 + '.*')
        if candidates:
            os.replace(candidates[0], out_mp3)
            logger.info(f"[OK] yt-dlp audio (fallback rename): {out_mp3}")
        else:
            raise RuntimeError("Gagal memproses audio, silakan coba lagi.")


def download_cover(cover_url, cover_path):
    """Download thumbnail dari TikWM sebagai cover art."""
    try:
        cr = session.get(cover_url, timeout=15)
        cr.raise_for_status()
        if len(cr.content) > 1000:
            with open(cover_path, 'wb') as f:
                f.write(cr.content)
            logger.info("[IMG] Cover berhasil didownload dari TikWM")
            return True
    except Exception as e:
        logger.warning(f"[WARN] Gagal download cover: {e}")
    return False


def embed_cover(mp3_path, cover_path):
    """
    Embed cover art ke file MP3 via mutagen (ID3 APIC tag langsung).
    - Resize cover ke 500x500 JPEG via ffmpeg
    - Embed sebagai ID3 APIC frame (pure JPEG still, bukan video stream)
    - Output tetap MP3 container beneran, bukan MP4 nyamar
    """
    thumb_path = cover_path + '.thumb.jpg'
    try:
        # Step 1: resize cover ke 500x500 JPEG via ffmpeg
        subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', cover_path,
                '-vf', 'scale=500:500:force_original_aspect_ratio=decrease,pad=500:500:(ow-iw)/2:(oh-ih)/2',
                '-q:v', '6',
                thumb_path,
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
              # Step 2: embed via mutagen ID3 APIC tag langsung ke MP3
        # Mutagen tulis ID3 tag native - tidak ada container MP4, tidak ada video stream
        from mutagen.id3 import ID3, APIC, error as ID3Error

        with open(thumb_path, 'rb') as img_f:
            img_data = img_f.read()

        try:
            tags = ID3(mp3_path)
        except ID3Error:
            tags = ID3()

        tags.add(APIC(
            encoding=3,          # UTF-8
            mime='image/jpeg',
            type=3,              # Cover (front)
            desc='Cover',
            data=img_data,
        ))
        tags.save(mp3_path, v2_version=3)
        logger.info(f"[IMG] Cover art di-embed via ID3 APIC ({len(img_data)//1024}KB)")

    except Exception as e:
        logger.warning(f"[WARN] Cover embed gagal (tidak fatal): {e}")
    finally:
        if os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass


def get_tiktok_audio_url(tiktok_url):
    """
    Ambil URL audio stream asli video TikTok via yt-dlp (bestaudio).
    Ini adalah audio yang benar-benar tertanam di video - bukan field 'music'
    yang merupakan lagu background TikWM terpisah.

    Return: (audio_direct_url, cover_url, title) atau (None, cover, title)
    """
    ydl_opts = {
        'format':      'bestaudio/best',
        'quiet':       True,
        'no_warnings': True,
        'noplaylist':  True,
        'user_agent':  TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tiktok_url, download=False)
            audio_url = None

            # Cari format audio saja (acodec ada, vcodec none/null)
            for fmt in (info.get('formats') or []):
                if fmt.get('acodec') not in (None, 'none') and fmt.get('vcodec') in (None, 'none'):
                    audio_url = fmt.get('url')
                    logger.info(f"[MP3] Audio stream ditemukan: {fmt.get('format_id')} | {fmt.get('ext')}")
                    break

            # Fallback: pakai URL terbaik (meski campur video, tetap bisa extract audio)
            if not audio_url:
                audio_url = info.get('url')
                logger.info("[WARN] Tidak ada pure audio stream, fallback ke URL terbaik")

            cover_url = info.get('thumbnail')
            title     = info.get('title', 'audio')
            return audio_url, cover_url, title
    except Exception as e:
        logger.warning(f"[WARN] yt-dlp gagal ambil audio URL TikTok: {e}")
        return None, None, None


def process_mp3_pipeline(url, title, out_tmpl, progress_cb=None):
    """
    Pipeline MP3 LANGSUNG AUDIO - tidak download video, langsung ambil audio stream.

    - TikTok  : yt-dlp extract audio stream URL -> download raw audio -> encode MP3
    - Lainnya : yt-dlp bestaudio + FFmpegExtractAudio postprocessor

    Return: (path_mp3, final_title)
    """
    def emit(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        logger.info(f"[{pct}%] {msg}")

    out_mp3 = out_tmpl + '.mp3'
    is_tiktok = any(x in url for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com'])

    if is_tiktok:
        # --- TIKTOK: extract audio stream URL via yt-dlp, lalu download langsung ---
        emit(15, "Mengambil informasi video...")

            # Coba yt-dlp dulu untuk audio stream asli
        audio_url, cover_url, api_title = get_tiktok_audio_url(url)
        final_title = api_title or title

        # Fallback ke TikWM untuk cover art kalau yt-dlp berhasil
        if not cover_url:
            _, cover_url_tikwm, tikwm_title = get_meta_via_tikwm(url)
            cover_url  = cover_url_tikwm
            if not final_title or final_title == 'audio':
                final_title = tikwm_title or title

        if audio_url:
            emit(30, "Mengunduh audio...")
            download_audio_direct(audio_url, out_mp3)
        else:
            # Terakhir: fallback ke TikWM video URL + extract audio
            # for_audio=True -> ambil play/SD bukan hdplay, audio track identik tapi stream lebih ringan
            emit(20, "Memproses video...")
            video_url, cover_url2, tikwm_title = get_meta_via_tikwm(url, for_audio=True)
            if not cover_url:
                cover_url = cover_url2
            if not final_title or final_title == 'audio':
                final_title = tikwm_title or title
            if not video_url:
                raise RuntimeError("Gagal mengambil video, silakan coba lagi.")
            emit(35, "Mengunduh audio...")
            download_audio_direct(video_url, out_mp3)

    else:
                # --- PLATFORM LAIN: yt-dlp bestaudio + FFmpegExtractAudio ---
        emit(15, "Mengambil informasi video...")
        final_title = title

        try:
            # Ambil info dulu untuk title & cover
         with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                final_title = info.get('title', title)
                cover_url   = info.get('thumbnail')
        except Exception:
            cover_url = None

        emit(30, "Mengunduh audio...")
        download_audio_ytdlp(url, out_mp3)

    # Embed cover art kalau ada
    if cover_url:
        cover_path = out_tmpl + '_cover.jpg'
        emit(88, "Menyiapkan file...")
        if download_cover(cover_url, cover_path):
            embed_cover(out_mp3, cover_path)

    return out_mp3, final_title


# =============================================================================
# SPOTIFY ENGINE
# Tiru logika Spotify.py:
#   get_metadata()   -> spotify_get_metadata()
#   download_audio() -> spotify_download_mp3()
# =============================================================================

def spotify_get_metadata(url):
    """
    Scrape judul lagu dari halaman Spotify.
    Tiru get_metadata() di Spotify.py persis — og:title dulu, fallback <title>.
    Return: title string atau None kalau gagal.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=15)

        match = re.search(r'<meta property="og:title" content="([^"]+)"', res.text)
        if match:
            return match.group(1)

        match = re.search(r'<title>([^<]+)</title>', res.text)
        if match:
            title = match.group(1).replace('| Spotify', '').strip()
            return title
    except Exception as e:
        logger.warning(f"[SPOTIFY] Gagal scrape metadata: {e}")
    return None


def spotify_get_cover(url):
    """
    Scrape cover art (og:image) dari halaman Spotify.
    Return: cover_url string atau None.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=15)
        match = re.search(r'<meta property="og:image" content="([^"]+)"', res.text)
        if match:
            return match.group(1)
    except Exception as e:
        logger.warning(f"[SPOTIFY] Gagal scrape cover: {e}")
    return None


def _spotify_finalize_output(out_mp3):
    """
    Cek dan rename output yt-dlp ke out_mp3.
    Return True kalau file berhasil ditemukan, False kalau tidak.
    """
    import glob
    expected = out_mp3 + '.mp3'
    if os.path.exists(expected):
        os.replace(expected, out_mp3)
        logger.info(f"[SPOTIFY] Download selesai: {out_mp3}")
        return True
    if os.path.exists(out_mp3):
        logger.info(f"[SPOTIFY] Download selesai (langsung): {out_mp3}")
        return True
    candidates = glob.glob(out_mp3 + '.*')
    if candidates:
        os.replace(candidates[0], out_mp3)
        logger.info(f"[SPOTIFY] Download selesai (fallback rename): {out_mp3}")
        return True
    return False


def _build_ytdlp_opts_base(out_mp3):
    """Base yt-dlp opts yang dipakai semua strategi Spotify."""
    return {
        'format': 'bestaudio/best',
        'outtmpl': out_mp3 + '.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        # Retry otomatis kalau fragment gagal
        'retries': 5,
        'fragment_retries': 5,
        'skip_unavailable_fragments': True,
    }


def spotify_download_mp3(query, out_mp3):
    """
    Download audio dari YouTube via ytsearch dengan multi-strategy fallback.

    Strategi (dijalankan urutan):
    1. youtube_music ytsearch via web client (paling ringan, bypass 403)
    2. YouTube reguler ytsearch via tv_embedded client (sering lolos 403)
    3. YouTube reguler ytsearch via mweb client (fallback)
    4. SoundCloud sebagai last-resort

    HTTP 403 dari YouTube biasanya karena:
    - player client default (android/ios) kena bot-detection
    - Tidak ada cookies / PO token
    Solusi: pakai player_client alternatif yang lebih longgar rate-limit-nya.
    """
    import glob

    base_opts = _build_ytdlp_opts_base(out_mp3)

    # =========================================================
    # Strategi 1: YouTube Music via web client
    # =========================================================
    logger.info(f"[SPOTIFY] Strategi 1 — YouTube Music web client: {query}")
    try:
        opts1 = {
            **base_opts,
            'default_search': 'ytsearch',
            'extractor_args': {
                'youtube': {
                    'player_client': ['web'],
                    'player_skip': ['webpage'],
                }
            },
        }
        with yt_dlp.YoutubeDL(opts1) as ydl:
            ydl.download([f"https://music.youtube.com/search?q={requests.utils.quote(query)}"])
        if _spotify_finalize_output(out_mp3):
            return
    except Exception as e:
        logger.warning(f"[SPOTIFY] Strategi 1 gagal: {e}")

    # Bersihkan file gagal sebelum retry
    for f in glob.glob(out_mp3 + '.*'):
        try: os.remove(f)
        except Exception: pass

    # =========================================================
    # Strategi 2: YouTube biasa — tv_embedded client (bypass 403)
    # tv_embedded tidak kena bot check yang sama dengan android/web biasa
    # =========================================================
    logger.info(f"[SPOTIFY] Strategi 2 — YouTube tv_embedded client: {query}")
    try:
        opts2 = {
            **base_opts,
            'extractor_args': {
                'youtube': {
                    'player_client': ['tv_embedded', 'web'],
                }
            },
        }
        with yt_dlp.YoutubeDL(opts2) as ydl:
            ydl.download([f"ytsearch1:{query}"])
        if _spotify_finalize_output(out_mp3):
            return
    except Exception as e:
        logger.warning(f"[SPOTIFY] Strategi 2 gagal: {e}")

    for f in glob.glob(out_mp3 + '.*'):
        try: os.remove(f)
        except Exception: pass

    # =========================================================
    # Strategi 3: YouTube biasa — mweb client
    # =========================================================
    logger.info(f"[SPOTIFY] Strategi 3 — YouTube mweb client: {query}")
    try:
        opts3 = {
            **base_opts,
            'extractor_args': {
                'youtube': {
                    'player_client': ['mweb'],
                }
            },
        }
        with yt_dlp.YoutubeDL(opts3) as ydl:
            ydl.download([f"ytsearch1:{query}"])
        if _spotify_finalize_output(out_mp3):
            return
    except Exception as e:
        logger.warning(f"[SPOTIFY] Strategi 3 gagal: {e}")

    for f in glob.glob(out_mp3 + '.*'):
        try: os.remove(f)
        except Exception: pass

    # =========================================================
    # Strategi 4: SoundCloud sebagai last-resort
    # SoundCloud tidak butuh auth, tapi kualitas/ketersediaan lagu beda
    # =========================================================
    logger.info(f"[SPOTIFY] Strategi 4 — SoundCloud last-resort: {query}")
    try:
        opts4 = {**base_opts}
        with yt_dlp.YoutubeDL(opts4) as ydl:
            ydl.download([f"scsearch1:{query}"])
        if _spotify_finalize_output(out_mp3):
            return
    except Exception as e:
        logger.warning(f"[SPOTIFY] Strategi 4 (SoundCloud) gagal: {e}")

    raise RuntimeError("Gagal mendownload audio. YouTube mungkin sedang blokir request. Coba lagi nanti.")


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/api/spotify_info', methods=['POST'])
@limiter.limit('20 per minute')
def spotify_info_api():
    """Preview info lagu Spotify untuk ditampilin di frontend."""
    data        = request.get_json(force=True) or {}
    spotify_url = data.get('url', '').strip()

    if not spotify_url:
        return jsonify({"status": "error", "msg": "URL kosong."}), 400

    if not is_safe_external_url(spotify_url) or not is_supported_url(spotify_url):
        return jsonify({"status": "error", "msg": "URL tidak valid atau bukan link Spotify."}), 400

    title = spotify_get_metadata(spotify_url)
    if not title:
        return jsonify({"status": "error", "msg": "Gagal membaca metadata lagu dari Spotify."}), 500

    cover = spotify_get_cover(spotify_url)

    return jsonify({
        "status":   "success",
        "title":    title,
        "cover":    cover or "",
        "author":   "Spotify",
        "platform": "spotify",
    })


@app.route('/api/spotify_mp3', methods=['POST'])
@limiter.limit('10 per minute')
def spotify_mp3_api():
    """
    Download MP3 dari URL Spotify.
    Flow: scrape metadata -> ytsearch yt-dlp -> encode MP3 192k -> stream ke browser.
    """
    import tempfile

    data        = request.get_json(force=True) or {}
    spotify_url = data.get('url', '').strip()
    title       = data.get('title', 'audio')

    if not spotify_url:
        return "URL kosong.", 400

    if not is_safe_external_url(spotify_url) or not is_supported_url(spotify_url):
        return "URL tidak valid atau bukan link Spotify.", 400

    logger.info(f"[SPOTIFY] Request MP3: {mask_url(spotify_url)}")

    try:
        query = spotify_get_metadata(spotify_url)
        if not query:
            return "Gagal membaca metadata lagu dari Spotify. Coba lagi.", 500

        final_title = query
        logger.info(f"[SPOTIFY] Query YouTube: {query}")

        _fd, tmp_base = tempfile.mkstemp(prefix='vinder_spotify_')
        os.close(_fd)
        os.remove(tmp_base)

        out_mp3 = tmp_base + '.mp3'
        spotify_download_mp3(query, out_mp3)

        if not os.path.exists(out_mp3):
            return "Gagal memproses audio, silakan coba lagi.", 500

        filename  = f"[Vinder].{safe_filename(final_title)}.mp3"
        file_size = os.path.getsize(out_mp3)
        logger.info(f"[SPOTIFY] Siap stream: {filename} ({file_size // 1024} KB)")

        def generate_and_cleanup():
            try:
                with open(out_mp3, 'rb') as f:
                    while True:
                        chunk = f.read(512 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.remove(out_mp3)
                except Exception:
                    pass

        return Response(
            stream_with_context(generate_and_cleanup()),
            headers={
                'Content-Type':        'audio/mpeg',
                'Content-Disposition': make_content_disposition(filename),
                'Cache-Control':       'no-cache',
                'Content-Length':      str(file_size),
            }
        )

    except Exception as e:
        logger.error(f"[SPOTIFY] spotify_mp3 error: {e}")
        return "Terjadi kesalahan saat memproses audio Spotify. Silakan coba lagi.", 500

@app.route('/')
def index():
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'Unknown').split(',')[0].strip()
    # kirim_notif(f"🌐 Visitor masuk!\nIP: {ip}")
    return send_file('vinder.html')


@app.route('/api/ping')
def ping():
    """Keep-alive endpoint — dipanggil frontend tiap 4 menit biar Railway tidak sleep."""
    try:
        session.head('https://www.tikwm.com', timeout=5)
    except Exception:
        pass
    return '', 204


@app.route('/api/search', methods=['POST'])
@limiter.limit('10 per minute')
def search_videos_api():
    data       = request.json
    keyword    = data.get('keyword')
    limit      = data.get('limit', 10)
    filter_str = data.get('filter', '').strip()
    # kirim_notif(f"User nyari keyword: {keyword}")
    logger.info(f"[SEARCH] Searching for: {keyword} | filter: '{filter_str}'")

    filter_op, filter_detik = parse_filter_durasi(filter_str)
    if filter_str and filter_op is None:
        logger.warning(f"[WARN] Format filter tidak dikenali: '{filter_str}'")

    try:
        resp = session.post(
            "https://www.tikwm.com/api/feed/search",
            data={"keywords": keyword, "count": limit, "HD": 1},
            timeout=30,
        )
        resp.raise_for_status()
        json_data = resp.json()

        if json_data.get('code') != 0:
            msg = json_data.get('msg', 'API TikWM return non-zero code')
            logger.error(f"[ERR] TikWM API Error: {msg}")
            return jsonify({"status": "error", "msg": f"TikWM API: {msg}"})

        videos  = json_data.get('data', {}).get('videos', [])
        results = []

        for v in videos:
            durasi_detik = v.get('duration')

            if not lolos_filter(durasi_detik, filter_op, filter_detik):
                continue

            cover_url  = v.get('origin_cover') or v.get('cover') or ''
            size_bytes = v.get('size', 0)
            size_mb    = round(size_bytes / (1024 * 1024), 2) if size_bytes else "?"
            author     = v.get('author', {})

            results.append({
                'title':     v.get('title', 'Video TikTok'),
                'duration':  format_durasi(durasi_detik),
                'play':      v.get('play', ''),
                'hdplay':    v.get('hdplay', '') or v.get('play', ''),
                'cover':     cover_url,
                'size':      f"{size_mb} MB",
                'video_id':  v.get('id', ''),
                'author_id': author.get('id', '') if isinstance(author, dict) else '',
            })

        logger.info(f"[OK] Found {len(results)} videos (after filter)")
        return jsonify({"status": "success", "data": results})

    except Exception as e:
        logger.error(f"Search Error: {str(e)}")
        return jsonify({"status": "error", "msg": str(e)})





# Platform yang didukung - FIX agar URL asing tidak nyasar ke static files
SUPPORTED_PLATFORMS = [
    'tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com',
    'youtube.com', 'youtu.be',
    'instagram.com', 'twitter.com', 'x.com',
    'facebook.com', 'fb.watch',
    'open.spotify.com',
]

def is_supported_url(url):
    if not url:
        return False
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.split(":")[0]
        return any(netloc == p or netloc.endswith("." + p) for p in SUPPORTED_PLATFORMS)
    except Exception:
        return False

# FIX #3 & #7: Validasi URL untuk mencegah SSRF dan skema berbahaya
# Blokir: file://, ftp://, http://localhost, http://127.x, http://169.254.x (AWS metadata)
import ipaddress
from urllib.parse import urlparse

def is_safe_external_url(url):
    """
    Cek apakah URL aman untuk di-fetch oleh server.
    Return False jika URL mengarah ke resource internal/private.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
        # Hanya izinkan http dan https
        if parsed.scheme not in ('http', 'https'):
            logger.warning(f"[SSRF] Blokir skema berbahaya: {parsed.scheme}")
            return False
        hostname = parsed.hostname or ''
        # Blokir localhost dan variasi
        if hostname in ('localhost', ''):
            logger.warning(f"[SSRF] Blokir hostname: {hostname}")
            return False
        # Blokir IP private/loopback/link-local
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                logger.warning(f"[SSRF] Blokir IP internal: {hostname}")
                return False
        except ValueError:
            pass  # bukan IP, hostname biasa - lanjut
        return True
    except Exception as e:
        logger.warning(f"[SSRF] Gagal parse URL: {e}")
        return False


@app.route('/api/download_url', methods=['POST'])
@limiter.limit('20 per minute')
def download_url_api():
    data      = request.json
    url_input = data.get('url', '').strip()
    logger.info(f"[URL] Processing: {mask_url(url_input)}")

    # FIX: tolak URL platform yang tidak didukung (Pinterest, dll)
    # Sebelumnya Pinterest URL lolos ke yt_dlp dan sering menyebabkan
    # Flask fallback serve vinder.html sebagai file download
    if not is_supported_url(url_input):
        logger.warning(f"[WARN] Platform tidak didukung: {mask_url(url_input)}")
        return jsonify({
            "status": "error",
            "msg":    "Platform tidak didukung. Vinder mendukung: TikTok, YouTube, Instagram, Twitter/X, Facebook."
        })

    ydl_opts = {
        'format':       'bestvideo+bestaudio/best',
        'quiet':        True,
        'no_warnings':  True,
        'noplaylist':   True,
        'user_agent':   TIKTOK_UA,
        'http_headers': DEFAULT_HEADERS,
    }

    try:
        if any(x in url_input for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com']):
            resp = session.get(f"https://www.tikwm.com/api/?url={url_input}", timeout=15).json()
            if resp.get('code') == 0:
                v = resp['data']

                # Deteksi slideshow: ada field 'images' (array foto) dan tidak ada video stream
                images     = v.get('images') or []
                play_url   = v.get('play')
                is_slideshow = bool(images)

                if is_slideshow:
                    logger.info(f"[SLIDESHOW] Konten foto terdeteksi ({len(images)} gambar): {url_input[-40:]}")
                    # kirim_notif(f"📸 Slideshow terdeteksi!\nURL: {url_input[-60:]}\nJumlah foto: {len(images)}")
                    return jsonify({
                        "status":       "slideshow",
                        "title":        v.get('title', 'TikTok Slideshow'),
                        "cover":        v.get('origin_cover') or v.get('cover'),
                        "author":       v.get('author', {}).get('nickname', 'User'),
                        "duration":     f"{v.get('duration', 0)}s",
                        "size":         f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                        "image_count":  len(images),
                    })

                result = {
                    "status":   "success",
                    "title":    v.get('title', 'TikTok Video'),
                    "cover":    v.get('origin_cover') or v.get('cover'),
                    "author":   v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size":     f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                    "play":     play_url,
                    "hdplay":   v.get('hdplay'),
                }

                # Pre-fetch audio URL di background — siap sebelum user klik MP3
                logger.info(f"[URL] Preview response OK untuk: {url_input[-40:]}")

                return jsonify(result)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_input, download=False)
            return jsonify({
                "status":   "success",
                "title":    info.get('title', 'Video'),
                "cover":    info.get('thumbnail'),
                "author":   info.get('uploader', 'Unknown'),
                "duration": f"{info.get('duration', 0)}s",
                "size":     "N/A",
                "play":     info.get('url'),
                "hdplay":   info.get('url'),
            })

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})


@app.route('/api/get_video')
def get_video_api():
    video_url    = request.args.get('url')
    fallback_url = request.args.get('fallback')
    title        = request.args.get('title', 'video')
    # kirim_notif(f"User download MP4: {title}")

    if not video_url:
        return "URL Kosong", 400

    # FIX #3: Cek SSRF - tolak URL internal/berbahaya
    if not is_safe_external_url(video_url):
        return "URL tidak valid atau tidak diizinkan.", 400
    if fallback_url and not is_safe_external_url(fallback_url):
        fallback_url = None

    try:
        r, _ = fetch_video_stream(video_url, fallback_url)

        if r is None or r.status_code >= 400:
            return "Video tidak ditemukan atau link sudah kadaluarsa.", 403

        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return "Video tidak dapat diakses, silakan coba lagi.", 403

        fname = f'[Vinder].{safe_filename(title)}.mp4'
        return Response(
            stream_with_context(r.iter_content(chunk_size=1024 * 1024)),
            headers={
                'Content-Type':        content_type,
                'Content-Disposition': make_content_disposition(fname),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        # FIX #6: Jangan kembalikan detail error ke user (mencegah info disclosure)
        logger.error(f"get_video error: {str(e)}")
        return "Terjadi kesalahan saat memproses video. Silakan coba lagi.", 500


@app.route('/api/mp3_progress')
def mp3_progress_api():
    """
    SSE endpoint - push progress real-time ke frontend tiap tahap selesai.
    Format pesan : "data: {pct}|{msg}\\n\\n"
    Pesan selesai: "data: 100|[OK] DONE|{uid}|{filename}\\n\\n"
    Pesan error  : "data: -1|[ERR] {msg}\\n\\n"
    """
    tiktok_url = request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    # FIX #3: Cek SSRF dan platform whitelist
    if not is_safe_external_url(tiktok_url) or not is_supported_url(tiktok_url):
        return "URL tidak valid atau platform tidak didukung.", 400

    def generate():
        def send(pct, msg):
            return f"data: {pct}|{msg}\n\n"

        uid      = str(uuid.uuid4())
        out_tmpl = f'/tmp/vinder_{uid}'

        # FIX: gunakan queue + thread agar SSE bisa yield progress real-time
        # Sebelumnya events dikumpul di list, baru di-yield setelah pipeline selesai
        # - menyebabkan bubble lompat langsung 0% -> 100% tanpa animasi bertahap
        import queue, threading

        q = queue.Queue()

        def emit_sse(pct, msg):
            q.put(send(pct, msg))

        def run_pipeline():
            try:
                emit_sse(5, "Memeriksa link video...")
                url = tiktok_url
                if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                    url = resolve_tiktok_url(url)

                out_mp3, final_title = process_mp3_pipeline(url, title, out_tmpl, progress_cb=emit_sse)


                if not os.path.exists(out_mp3):
                    q.put(send(-1, "Gagal memproses audio, silakan coba lagi."))
                    do_cleanup(out_tmpl)
                    q.put(None)
                    return

                fname = f"[Vinder].{safe_filename(final_title)}.mp3"
                emit_sse(95, "Menyiapkan file untuk diunduh...")
                with open(out_tmpl + '.ready', 'w') as f:
                    f.write(fname)

                q.put(send(100, f"[OK] DONE|{uid}|{fname}"))
            except Exception as e:
                # FIX #6: Log detail error di server, kirim pesan generik ke client
                logger.error(f"SSE MP3 Error: {e}")
                do_cleanup(out_tmpl)
                q.put(send(-1, "Gagal memproses audio, silakan coba lagi."))
            finally:
                q.put(None)  # sentinel = selesai

        t = threading.Thread(target=run_pipeline, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=120)
            except queue.Empty:
                yield send(-1, "Proses terlalu lama, silakan coba lagi.")
                break
            if item is None:
                break
            yield item

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':     'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/get_mp3_file')
def get_mp3_file_api():
    """Ambil file MP3 yang sudah selesai diproses via SSE."""
    uid = request.args.get('uid', '')
    # FIX #5: Validasi uid format UUID (setelah migrasi dari timestamp ke uuid4)
    # Cegah path traversal seperti uid='../etc/passwd'
    if not uid or not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', uid):
        return "UID tidak valid", 400

    out_tmpl  = f'/tmp/vinder_{uid}'
    out_mp3   = out_tmpl + '.mp3'
    done_flag = out_tmpl + '.ready'

    if not os.path.exists(out_mp3) or not os.path.exists(done_flag):
        return "File tidak ditemukan atau belum selesai", 404

    with open(done_flag) as f:
        filename = f.read().strip()

    # Kirim file dengan Content-Disposition RFC 5987 (aman untuk emoji/unicode)
    def generate_mp3_file():
        with open(out_mp3, 'rb') as audio_f:
            while True:
                chunk = audio_f.read(512 * 1024)
                if not chunk:
                    break
                yield chunk
        do_cleanup(out_tmpl)

    return Response(
        stream_with_context(generate_mp3_file()),
        headers={
            'Content-Type':        'audio/mpeg',
            'Content-Disposition': make_content_disposition(filename),
            'Cache-Control':       'no-cache',
        }
    )


@app.route('/api/get_mp3')
def get_mp3_api():
    """Endpoint fallback MP3 tanpa SSE (satu request langsung)."""
    tiktok_url = request.args.get('tiktok_url') or request.args.get('url')
    title      = request.args.get('title', 'audio')

    if not tiktok_url:
        return "URL Kosong", 400

    # FIX #3: Cek SSRF sebelum fetch
    if not is_safe_external_url(tiktok_url) or not is_supported_url(tiktok_url):
        return "URL tidak valid atau platform tidak didukung.", 400

    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    uid      = str(uuid.uuid4())
    out_tmpl = f'/tmp/vinder_{uid}'

    try:
        logger.info(f"[MP3] MP3 request: {mask_url(tiktok_url)}")
        out_mp3, final_title = process_mp3_pipeline(tiktok_url, title, out_tmpl)

        if not os.path.exists(out_mp3):
            do_cleanup(out_tmpl)
            return "Gagal memproses audio, silakan coba lagi.", 500

        filename = f"[Vinder].{safe_filename(final_title)}.mp3"
        logger.info(f"[OK] Siap dikirim: {filename}")

        # Kirim file dengan Content-Disposition RFC 5987 (aman untuk emoji/unicode)
        def generate_mp3():
            with open(out_mp3, 'rb') as audio_f:
                while True:
                    chunk = audio_f.read(512 * 1024)
                    if not chunk:
                        break
                    yield chunk
            do_cleanup(out_tmpl)

        return Response(
            stream_with_context(generate_mp3()),
            headers={
                'Content-Type':        'audio/mpeg',
                'Content-Disposition': make_content_disposition(filename),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        # FIX #6: Sembunyikan detail error dari user
        logger.error(f"MP3 Error: {str(e)}")
        do_cleanup(out_tmpl)
        return "Terjadi kesalahan saat memproses audio. Silakan coba lagi.", 500




@app.route('/api/fast_mp3', methods=['GET', 'POST'])
@limiter.limit('15 per minute')
def fast_mp3_api():
    """
    FAST MP3 - El Kedips Edition (Maximum Speed)
    Optimasi:
    1. TikTok: skip yt-dlp, langsung TikWM (hemat 1-2 detik)
    2. Resolve URL + TikWM call paralel (hemat 0.5-1 detik)
    3. Cover: 1 ffmpeg command tanpa ffprobe (hemat 0.5 detik)
       - Seek ke 50% durasi via -sseof trick
    4. Cover + audio encode paralel (threading)
    """
    import tempfile, threading

    if request.method == 'POST':
        data       = request.get_json(force=True) or {}
        tiktok_url = data.get('url', '').strip()
        title      = data.get('title', 'audio')
    else:
        tiktok_url = request.args.get('url', '').strip()
        title      = request.args.get('title', 'audio')

    # kirim_notif(f"User download MP3: {tiktok_url}")

    if not tiktok_url:
        return "URL Kosong", 400

    # FIX #3: Cek SSRF dan platform whitelist sebelum fetch apapun
    if not is_safe_external_url(tiktok_url) or not is_supported_url(tiktok_url):
        return "URL tidak valid atau platform tidak didukung.", 400

    is_short = 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url
    is_tiktok = is_short or 'tiktok.com' in tiktok_url

    try:
        audio_url   = None
        video_url   = None
        final_title = title

        if is_tiktok:
            # Resolve short URL dulu
            if is_short:
                tiktok_url = resolve_tiktok_url(tiktok_url)

            # Selalu fetch langsung ke TikWM - tanpa cache
            logger.info(f"[FETCH] Ambil metadata video: {mask_url(tiktok_url)}")
            vid_url, _, tikwm_title = get_meta_via_tikwm(tiktok_url, for_audio=True)
            video_url   = vid_url
            audio_url   = vid_url
            final_title = tikwm_title or title

            if not audio_url:
                return "Gagal mengambil URL audio dari TikTok.", 500

            _fd, tmp_base = tempfile.mkstemp(prefix='vinder_fast_')
            os.close(_fd)
            os.remove(tmp_base)
            out_mp3 = tmp_base + '.mp3'

            download_audio_direct(audio_url, out_mp3)

            if not os.path.exists(out_mp3):
                return "Gagal memproses audio, silakan coba lagi.", 500

            filename  = f"[Vinder].{safe_filename(final_title)}.mp3"
            file_size = os.path.getsize(out_mp3)

            def generate_tiktok_mp3():
                try:
                    with open(out_mp3, 'rb') as f:
                        while True:
                            chunk = f.read(512 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.remove(out_mp3)
                    except Exception:
                        pass

            return Response(
                stream_with_context(generate_tiktok_mp3()),
                headers={
                    'Content-Type':        'audio/mpeg',
                    'Content-Disposition': make_content_disposition(filename),
                    'Cache-Control':       'no-cache',
                    'Content-Length':      str(file_size),
                }
            )

        else:
            # Non-TikTok (YouTube, Instagram, Facebook): pakai download_audio_ytdlp
            import tempfile as _tempfile
            _fd2, tmp_base2 = _tempfile.mkstemp(prefix='vinder_yt_')
            os.close(_fd2)
            os.remove(tmp_base2)
            out_mp3_yt = tmp_base2 + '.mp3'

            with yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
                info_yt     = ydl.extract_info(tiktok_url, download=False)
                final_title = (info_yt or {}).get('title', title)

            download_audio_ytdlp(tiktok_url, out_mp3_yt)

            if not os.path.exists(out_mp3_yt):
                return "Gagal memproses audio, silakan coba lagi.", 500

            filename  = f"[Vinder].{safe_filename(final_title)}.mp3"
            file_size = os.path.getsize(out_mp3_yt)

            def generate_yt_mp3():
                try:
                    with open(out_mp3_yt, 'rb') as f:
                        while True:
                            chunk = f.read(512 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.remove(out_mp3_yt)
                    except Exception:
                        pass

            return Response(
                stream_with_context(generate_yt_mp3()),
                headers={
                    'Content-Type':        'audio/mpeg',
                    'Content-Disposition': make_content_disposition(filename),
                    'Cache-Control':       'no-cache',
                    'Content-Length':      str(file_size),
                }
            )

    except Exception as e:
        # FIX #6: Sembunyikan detail error dari user
        logger.error(f"fast_mp3 error: {e}")
        return "Terjadi kesalahan saat memproses audio. Silakan coba lagi.", 500

# =============================================================================
# INSTAGRAM / YOUTUBE / FACEBOOK — MP4 INFO & DOWNLOAD
# Mekanisme igG.py: instaloader untuk Instagram (post/reel/igtv)
# yt-dlp untuk YouTube & Facebook
# =============================================================================

def _ig_parse_shortcode(url):
    """
    Ekstrak shortcode dari URL Instagram post/reel/igtv.
    Tiru parse_url() di igG.py — strip query string & trailing slash dulu.
    Return shortcode string atau None.
    """
    url = url.strip().split('?')[0].rstrip('/')
    for pattern in [
        r'instagram\.com/p/([\w\-]+)',
        r'instagram\.com/reel/([\w\-]+)',
        r'instagram\.com/tv/([\w\-]+)',
    ]:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def _ig_get_info_instaloader(url):
    """
    Ambil metadata video Instagram via instaloader (tanpa download file).
    Tiru logika dl_post() di igG.py tapi hanya ambil info, tidak simpan file.
    Return dict info atau raise Exception.
    """
    import instaloader
    shortcode = _ig_parse_shortcode(url)
    if not shortcode:
        raise ValueError("Shortcode Instagram tidak ditemukan di URL.")

    loader = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return {
        'title':        (post.caption or '').replace('\n', ' ')[:80] or f'Instagram {post.shortcode}',
        'cover':        post.url,
        'author':       post.owner_username,
        'duration_sec': int(post.video_duration or 0),
        'is_video':     post.is_video,
        'shortcode':    shortcode,
    }


def _ig_download_video_instaloader(url, out_mp4):
    """
    Download video Instagram ke out_mp4 via instaloader.
    Tiru dl_post() di igG.py: download ke tmp dir, lalu move file mp4.
    """
    import instaloader, shutil, glob as _glob
    shortcode = _ig_parse_shortcode(url)
    if not shortcode:
        raise ValueError("Shortcode Instagram tidak ditemukan di URL.")

    loader = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return {
        'title':        (post.caption or '').replace('\n', ' ')[:80] or f'Instagram {post.shortcode}',
        'cover':        post.url,
        'author':       post.owner_username,
        'duration_sec': int(post.video_duration or 0),
        'is_video':     post.is_video,
        'shortcode':    shortcode,
    }


def _ig_download_video_instaloader(url, out_mp4):
    """
    Download video Instagram ke out_mp4 via instaloader.
    Tiru dl_post() di igG.py: download ke tmp dir, lalu move file mp4.
    """
    import instaloader, shutil, glob as _glob
    shortcode = _ig_parse_shortcode(url)
    if not shortcode:
        raise ValueError("Shortcode Instagram tidak ditemukan di URL.")

    tmp_dir = out_mp4 + '_ig_tmp'
    os.makedirs(tmp_dir, exist_ok=True)

    loader = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern='',
        filename_pattern='{shortcode}',
        quiet=True,
    )

    old_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        loader.download_post(post, target=tmp_dir)
    finally:
        os.chdir(old_cwd)

    # Cari file .mp4 hasil download (tiru move_media() di igG.py)
    mp4_files = _glob.glob(os.path.join(tmp_dir, '**', '*.mp4'), recursive=True)
    if not mp4_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("File MP4 tidak ditemukan setelah download Instagram.")

    shutil.move(mp4_files[0], out_mp4)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info(f"[IG] Download selesai: {out_mp4}")


@app.route('/api/thumb')
def thumb_proxy_api():
    """
    Proxy thumbnail Instagram — hindari CORS block di browser.
    Frontend kirim: /api/thumb?url=<encoded_image_url>
    """
    img_url = request.args.get('url', '').strip()
    if not img_url or not is_safe_external_url(img_url):
        return '', 400
    try:
        r = session.get(img_url, timeout=10, stream=True)
        content_type = r.headers.get('Content-Type', 'image/jpeg')
        return Response(r.content, headers={'Content-Type': content_type, 'Cache-Control': 'public, max-age=3600'})
    except Exception as e:
        logger.warning(f"[THUMB] Gagal proxy thumbnail: {e}")
        return '', 502



@app.route('/api/mp4_info', methods=['POST'])
@limiter.limit('20 per minute')
def mp4_info_api():
    """
    Preview info video untuk YouTube / Instagram / Facebook.
    Instagram: pakai instaloader (mekanisme igG.py).
    YouTube / Facebook: pakai yt-dlp extract_info.
    """
    data = request.get_json(force=True) or {}
    url  = data.get('url', '').strip()

    if not url:
        return jsonify({"status": "error", "msg": "URL kosong."}), 400

    if not is_safe_external_url(url) or not is_supported_url(url):
        return jsonify({"status": "error", "msg": "URL tidak valid atau platform tidak didukung."}), 400

    logger.info(f"[MP4INFO] Request: {mask_url(url)}")

    try:
        is_ig = 'instagram.com' in url

        if is_ig:
            # ── INSTAGRAM INFO: pakai yt-dlp extract_info ──
            # Lebih reliable untuk thumbnail & filesize dibanding instaloader
            ydl_opts_ig = {
                'format':      'bestvideo+bestaudio/best',
                'quiet':       True,
                'no_warnings': True,
                'noplaylist':  True,
            }
            with yt_dlp.YoutubeDL(ydl_opts_ig) as ydl:
                info_ig = ydl.extract_info(url, download=False)
                dur_sec  = int(info_ig.get('duration') or 0)
                size_raw = info_ig.get('filesize') or info_ig.get('filesize_approx') or 0
                size_str = f"{size_raw / 1024 / 1024:.2f}MB" if size_raw else "N/A"
                return jsonify({
                    "status":   "success",
                    "title":    info_ig.get('title', 'Instagram Video'),
                    "cover":    info_ig.get('thumbnail', ''),
                    "author":   info_ig.get('uploader') or info_ig.get('channel') or 'Instagram',
                    "duration": format_durasi(dur_sec),
                    "size":     size_str,
                    "play":     url,
                    "hdplay":   url,
                })
        else:
            ydl_opts = {
                'format':      'bestvideo+bestaudio/best',
                'quiet':       True,
                'no_warnings': True,
                'noplaylist':  True,
                # FIX: pastikan YouTube Shorts (?si=...) tidak error karena dianggap playlist
                'extract_flat': False,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # FIX: pakai format_durasi (sama seperti Instagram) bukan raw string
                dur_sec  = int(info.get('duration') or 0)
                size_raw = info.get('filesize') or info.get('filesize_approx') or 0
                size_str = f"{size_raw / 1024 / 1024:.2f}MB" if size_raw else "N/A"
                return jsonify({
                    "status":   "success",
                    "title":    info.get('title', 'Video'),
                    "cover":    info.get('thumbnail'),
                    "author":   info.get('uploader') or info.get('channel', 'Unknown'),
                    "duration": format_durasi(dur_sec),
                    "size":     size_str,
                    "play":     url,
                    "hdplay":   url,
                })

    except Exception as e:
        logger.error(f"[MP4INFO] Error: {e}")
        return jsonify({"status": "error", "msg": "Gagal membaca info video. Coba lagi."}), 500


@app.route('/api/download_mp4', methods=['POST'])
@limiter.limit('10 per minute')
def download_mp4_api():
    """
    Download MP4 untuk YouTube / Instagram / Facebook.
    Instagram: pakai instaloader (mekanisme igG.py), stream file ke browser.
    YouTube / Facebook: pakai yt-dlp, stream file ke browser.
    """
    import tempfile
    data    = request.get_json(force=True) or {}
    url     = data.get('url', '').strip()
    quality = data.get('quality', 'best')
    title   = data.get('title', 'video')

    if not url:
        return jsonify({"status": "error", "msg": "URL kosong."}), 400

    if not is_safe_external_url(url) or not is_supported_url(url):
        return jsonify({"status": "error", "msg": "URL tidak valid atau platform tidak didukung."}), 400

    logger.info(f"[MP4DL] Request: {mask_url(url)} | quality={quality}")

    is_ig = 'instagram.com' in url

    try:
        if is_ig:
            # ── INSTAGRAM: download via instaloader (igG.py mechanism) ──
            _fd, tmp_base = tempfile.mkstemp(prefix='vinder_ig_')
            os.close(_fd)
            os.remove(tmp_base)
            out_mp4 = tmp_base + '.mp4'

            _ig_download_video_instaloader(url, out_mp4)

            if not os.path.exists(out_mp4):
                return jsonify({"status": "error", "msg": "Gagal download video Instagram."}), 500

            filename  = f"[Vinder].{safe_filename(title)}.mp4"
            file_size = os.path.getsize(out_mp4)
            logger.info(f"[IG] Siap stream: {filename} ({file_size // 1024} KB)")

            def generate_ig():
                try:
                    with open(out_mp4, 'rb') as f:
                        while True:
                            chunk = f.read(512 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.remove(out_mp4)
                    except Exception:
                        pass

            return Response(
                stream_with_context(generate_ig()),
                headers={
                    'Content-Type':        'video/mp4',
                    'Content-Disposition': make_content_disposition(filename),
                    'Cache-Control':       'no-cache',
                    'Content-Length':      str(file_size),
                }
            )

        else:
            # ── YOUTUBE / FACEBOOK: download via yt-dlp ──
            fmt = 'bestvideo+bestaudio/best' if quality == 'best' else 'bestvideo[height<=480]+bestaudio/best[height<=480]/best[height<=480]/best'
            _fd, tmp_base = tempfile.mkstemp(prefix='vinder_mp4_')
            os.close(_fd)
            os.remove(tmp_base)
            out_mp4 = tmp_base + '.mp4'

            ydl_opts = {
                'format':    fmt,
                'outtmpl':   out_mp4,
                'quiet':     True,
                'no_warnings': True,
                'noplaylist':  True,
                'merge_output_format': 'mp4',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if not os.path.exists(out_mp4):
                return jsonify({"status": "error", "msg": "Gagal download video."}), 500

            filename  = f"[Vinder].{safe_filename(title)}.mp4"
            file_size = os.path.getsize(out_mp4)
            logger.info(f"[MP4DL] Siap stream: {filename} ({file_size // 1024} KB)")

            def generate_mp4():
                try:
                    with open(out_mp4, 'rb') as f:
                        while True:
                            chunk = f.read(512 * 1024)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.remove(out_mp4)
                    except Exception:
                        pass

            return Response(
                stream_with_context(generate_mp4()),
                headers={
                    'Content-Type':        'video/mp4',
                    'Content-Disposition': make_content_disposition(filename),
                    'Cache-Control':       'no-cache',
                    'Content-Length':      str(file_size),
                }
            )

    except Exception as e:
        logger.error(f"[MP4DL] Error: {e}")
        return jsonify({"status": "error", "msg": "Terjadi kesalahan saat download video. Silakan coba lagi."}), 500


# =============================================================================
# DAILY HEALTH + AI MESSAGE
# =============================================================================

_GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
_HEALTH_SAMPLE_URL = "https://vt.tiktok.com/ZS9GBdy9y/"

_PESAN_SUKSES_DAILY = [
    "🟢 Vinder masih hidup bro, aman!",
    "✅ Cek harian kelar — downloader jalan normal, santuy~",
    "💪 Semua sistem OK, TikWM nurut hari ini.",
    "🎯 Health check passed! Vinder sehat walafiat.",
    "🚀 Server masih ngebut, GK ada masalah hari ini.",
    "😎 Dicek udah, aman. Vinder lagi on fire!",
    "🟢 TikWM kooperatif, link download keluar normal.",
    "✅ Vinder hidup & sehat — laporan harian beres.",
    "🔥 Semua OK boss, sistem berjalan mulus.",
    "💡 Cek harian: passed! Ga ada yang perlu dikhawatirin.",
]


def _analisis_groq_daily(error_detail):
    """Panggil Groq untuk analisis error health check harian."""
    if not _GROQ_API_KEY:
        logger.warning("[DAILY] GROQ_API_KEY tidak ditemukan, analisis skip.")
        return "Analisis tidak tersedia (API key tidak ada)."
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Kamu adalah analis sistem untuk website downloader TikTok bernama Vinder. "
                            "Tugasmu: analisis error health check dengan singkat, jelas, dan dalam bahasa Indonesia santai. "
                            "Maksimal 3 kalimat. Langsung ke poin, tidak perlu basa-basi."
                        )
                    },
                    {
                        "role": "user",
                        "content": f"Health check Vinder gagal. Detail error:\n{error_detail}"
                    }
                ],
                "max_tokens": 200,
                "temperature": 0.7
            },
            timeout=15
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[DAILY] Groq analisis gagal: {e}")
        return "Analisis Groq tidak tersedia saat ini."


def _groq_startup_ping():
    """Panggil Groq sekali saat server ON, kirim ke Telegram sebagai test ping AI."""
    if not _GROQ_API_KEY:
        logger.warning("[DAILY] GROQ_API_KEY tidak ditemukan, startup ping skip.")
        return
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Kamu adalah asisten bot Vinder, website downloader TikTok. "
                            "Kamu baru saja aktif. Kirim sapaan singkat, santai, bahasa Indonesia. "
                            "Maksimal 2 kalimat. Langsung sapaan, tidak perlu basa-basi."
                        )
                    },
                    {
                        "role": "user",
                        "content": "Hello apakah kamu bisa mendengarkan ku?"
                    }
                ],
                "max_tokens": 100,
                "temperature": 0.9
            },
            timeout=15
        )
        data = resp.json()
        pesan_ai = data["choices"][0]["message"]["content"].strip()
        kirim_notif(f"🤖 Vinder AI Online!\n{pesan_ai}")
        logger.info("[DAILY] Startup AI ping berhasil dikirim ke Telegram.")
    except Exception as e:
        logger.warning(f"[DAILY] Startup AI ping gagal: {e}")


def _run_daily_health_check():
    """Jalankan health check harian: test TikWM, kirim hasil ke Telegram."""
    import random as _random
    from datetime import datetime as _datetime

    logger.info(f"[DAILY] Mulai health check harian — {_datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    error_detail = None
    try:
        resp = requests.get(
            f"https://www.tikwm.com/api/?url={_HEALTH_SAMPLE_URL}",
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            error_detail = (
                f"TikWM return code={data.get('code')}, msg={data.get('msg')}.\n"
                f"Raw response: {str(data)[:300]}"
            )
        else:
            v        = data.get("data", {})
            play_url = v.get("play") or v.get("hdplay")
            size     = v.get("size", 0)

            if not play_url:
                error_detail = "TikWM response OK tapi link download tidak muncul (play/hdplay kosong)."
            elif size == 0:
                error_detail = "TikWM response OK, link ada, tapi size video = 0 bytes."

    except requests.exceptions.Timeout:
        error_detail = "Request ke TikWM timeout (>15 detik). Server mungkin lambat atau down."
    except requests.exceptions.ConnectionError:
        error_detail = "Gagal konek ke TikWM. Cek koneksi server atau TikWM sedang down."
    except Exception as e:
        error_detail = f"Error tidak terduga: {type(e).__name__}: {str(e)}"

    now_str = _datetime.now().strftime("%d/%m/%Y %H:%M")

    if error_detail:
        logger.error(f"[DAILY] Health check GAGAL — {error_detail}")
        analisis = _analisis_groq_daily(error_detail)
        kirim_notif(
            f"❌ Vinder Health Check GAGAL!\n"
            f"🕒 {now_str}\n\n"
            f"📋 Error:\n{error_detail}\n\n"
            f"🤖 Analisis AI:\n{analisis}"
        )
    else:
        logger.info("[DAILY] Health check PASSED — semua sistem normal.")
        pesan_acak = _random.choice(_PESAN_SUKSES_DAILY)
        kirim_notif(f"{pesan_acak}\n🕒 {now_str}")


def _daily_health_loop():
    """Background thread: startup AI ping sekali, lalu health check tiap jam 15:00."""
    import time as _time
    from datetime import datetime as _datetime, timedelta as _timedelta

    _time.sleep(5)  # tunggu server ready dulu
    _groq_startup_ping()  # test AI langsung saat server ON

    while True:
        now    = _datetime.now()
        target = now.replace(hour=15, minute=0, second=0, microsecond=0)
        if now >= target:
            target += _timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"[DAILY] Health check dijadwalkan dalam {int(wait_seconds//3600)}j {int((wait_seconds%3600)//60)}m")
        _time.sleep(wait_seconds)
        _run_daily_health_check()


# =============================================================================
# MAIN
# =============================================================================

def _self_ping_loop():
    """Self-ping ke server sendiri tiap 4 menit supaya Railway tidak sleep."""
    import time as _time
    _time.sleep(60)  # tunggu server ready dulu
    base = os.environ.get('RAILWAY_PUBLIC_DOMAIN') or os.environ.get('PUBLIC_URL')
    if not base:
        logger.info("[PING] RAILWAY_PUBLIC_DOMAIN tidak ditemukan, self-ping nonaktif.")
        return
    url = f"https://{base.rstrip('/')}/api/ping"
    logger.info(f"[PING] Self-ping aktif → {url} setiap 4 menit")
    first_ping = True
    while True:
        try:
            requests.get(url, timeout=10)
            logger.info("[PING] Self-ping OK")
            if first_ping:
                kirim_notif("📡 Self ping Active")
                first_ping = False
        except Exception as e:
            logger.warning(f"[PING] Self-ping gagal: {e}")
        _time.sleep(4 * 60)

if __name__ == "__main__":
    threading.Thread(target=_self_ping_loop, daemon=True).start()
    threading.Thread(target=_daily_health_loop, daemon=True).start()
    kirim_notif("Sistem Vinder Berhasil ON di Railway!")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)