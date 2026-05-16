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


# =============================================================================
# GROQ LOG ALERT HANDLER
# Intercept semua log WARNING ke atas -> analisis Groq -> kirim ke Telegram
# =============================================================================

class GroqAlertHandler(logging.Handler):
    """
    Custom logging handler: tangkap WARNING/ERROR/CRITICAL,
    kirim ke Groq untuk analisis, lalu forward hasilnya ke Telegram.
    Cooldown 60 detik per pesan unik supaya tidak spam.
    """

    COOLDOWN_SECONDS = 60

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._lock     = threading.Lock()
        self._last_sent = {}   # key: pesan pendek -> timestamp terakhir dikirim

    def _analisis_groq(self, level, pesan, func_name):
        groq_key = os.environ.get("OPENROUTER_API_KEY")
        if not groq_key:
            return "Analisis tidak tersedia (OPENROUTER_API_KEY tidak ada)."
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type":  "application/json"
                },
                json={
                    "model": "openai/gpt-oss-120b:free",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Kamu adalah analis sistem untuk aplikasi downloader bernama Vinder. "
                                "Tugasmu: analisis log error berikut, jelaskan penyebabnya, "
                                "dan berikan solusi konkret dalam bahasa Indonesia santai. "
                                "Maksimal 4 kalimat. Langsung ke poin, tidak perlu basa-basi."
                            )
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Level: {level}\n"
                                f"Fungsi: {func_name}\n"
                                f"Pesan error:\n{pesan}"
                            )
                        }
                    ],
                    "max_tokens": 300,
                    "temperature": 0.5
                },
                timeout=15
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"Analisis Groq gagal: {e}"

    def emit(self, record):
        # Jangan proses log yang berasal dari handler ini sendiri (hindari loop)
        if getattr(record, '_from_groq_handler', False):
            return
        if not TELEGRAM_NOTIF_ENABLED:
            return

        try:
            level     = record.levelname
            pesan     = self.format(record)
            func_name = record.funcName or "unknown"

            # Cooldown: cek apakah pesan serupa baru saja dikirim
            cooldown_key = pesan[:120]
            now = time.time()
            with self._lock:
                last = self._last_sent.get(cooldown_key, 0)
                if now - last < self.COOLDOWN_SECONDS:
                    return
                self._last_sent[cooldown_key] = now

            # Analisis via Groq
            analisis = self._analisis_groq(level, pesan, func_name)

            emoji = {
                "WARNING":  "⚠️",
                "ERROR":    "❌",
                "CRITICAL": "🔴"
            }.get(level, "📋")

            notif = (
                f"{emoji} [{level}] Log Alert Vinder\n"
                f"🔧 Fungsi: {func_name}\n"
                f"📋 Log:\n{pesan[:400]}\n\n"
                f"🤖 Analisis AI:\n{analisis}"
            )

            # Kirim ke Telegram langsung (tidak lewat kirim_notif untuk hindari rekursi)
            requests.post(
                f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": _TELEGRAM_CHAT_ID, "text": notif},
                timeout=5
            )

        except Exception:
            pass   # Handler tidak boleh raise — diam saja kalau gagal


_groq_alert_handler = GroqAlertHandler()
logger.addHandler(_groq_alert_handler)
logger.info("[ALERT] GroqAlertHandler aktif — semua WARNING/ERROR/CRITICAL akan dianalisis AI.")


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
META_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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
    _is_meta = any(x in url for x in ['facebook.com', 'fb.watch', 'instagram.com'])
    _ua = META_UA if _is_meta else TIKTOK_UA

    ydl_opts = {
        'format':        'bestaudio/best',
        'outtmpl':       out_mp3 + '.%(ext)s',
        'quiet':         True,
        'no_warnings':   True,
        'noplaylist':    True,
        'proxy':         _YTDLP_PROXY if _YTDLP_PROXY else None,
        'user_agent':    _ua,
        'http_headers':  DEFAULT_HEADERS,
        'extractor_args': {'youtube': {'player_client': ['tv', 'ios']}},  # bypass BotGuard YouTube
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '0',    # 0 = ikuti bitrate asli source
        }],
        # Pastikan output final adalah file .mp3
        'keepvideo': False,
    }

    # Inject cookies YouTube kalau tersedia
    if _COOKIES_FILE and os.path.exists(_COOKIES_FILE):
        ydl_opts['cookiefile'] = _COOKIES_FILE
        logger.info("[COOKIES] yt-dlp pakai cookies YouTube.")

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
        'proxy':       _YTDLP_PROXY if _YTDLP_PROXY else None,
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
                # --- PLATFORM LAIN: Instagram (yt-dlp) / Facebook (facebook-scraper) ---
        emit(15, "Mengambil informasi video...")
        final_title = title
        cover_url   = None

        is_fb = any(x in url for x in ['facebook.com', 'fb.watch'])

        if is_fb:
            fb_info     = _fb_get_info(url)
            final_title = fb_info['title']
            cover_url   = fb_info['cover'] or None
            emit(30, "Mengunduh audio Facebook...")
            _fb_download_audio(url, out_mp3)
        else:
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
# YOUTUBE COOKIES SETUP
# Set env var YOUTUBE_COOKIES di Railway dengan isi file cookies.txt (Netscape format)
# Otomatis ditulis ke file temp saat server start, dipakai semua strategi yt-dlp
# =============================================================================

_COOKIES_FILE = None
_YTDLP_PROXY  = os.environ.get('YTDLP_PROXY', '')

# Gunakan ini hanya untuk bypass bot saat ambil Metadata
PROXY_OPTS_METADATA = {
    'proxy': _YTDLP_PROXY if _YTDLP_PROXY else None
}

# Paksa routing lewat proxy untuk semua download (anti-leak IP datacenter)
PROXY_OPTS_DOWNLOAD = {
    'proxy': _YTDLP_PROXY if _YTDLP_PROXY else None
}

if _YTDLP_PROXY:
    logger.info("[PROXY] ✅ Proxy Residensial telah Active.")
else:
    logger.info("[PROXY] Tidak ada proxy dikonfigurasi (YTDLP_PROXY kosong).")


def _setup_youtube_cookies():
    """Tulis env var YOUTUBE_COOKIES ke file temp, return path-nya."""
    global _COOKIES_FILE
    cookies_content = os.environ.get('YOUTUBE_COOKIES', '').strip()
    if not cookies_content:
        logger.info("[COOKIES] YOUTUBE_COOKIES tidak ditemukan di env, yt-dlp tanpa cookies.")
        return None
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix='sys_yt_cookies_', suffix='.txt')
        with os.fdopen(fd, 'w') as f:
            f.write(cookies_content)
        _COOKIES_FILE = path
        logger.info("[COOKIES] Cookies YouTube berhasil dimuat dari env var.")
        return path
    except Exception as e:
        logger.warning(f"[COOKIES] Gagal setup cookies: {e}")
        return None

_setup_youtube_cookies()

# =============================================================================
# FACEBOOK COOKIES SETUP
# Set env var FACEBOOK_COOKIES di Railway dengan isi file cookies.txt (Netscape format)
# Export dari browser pakai ekstensi "Get cookies.txt LOCALLY" saat login Facebook
# Otomatis ditulis ke file temp saat server start, dipakai yt-dlp untuk Facebook/Reels
# =============================================================================

_FB_COOKIES_FILE = None

def _setup_facebook_cookies():
    """Tulis env var FACEBOOK_COOKIES ke file temp, return path-nya."""
    global _FB_COOKIES_FILE
    cookies_content = os.environ.get('FACEBOOK_COOKIES', '').strip()
    if not cookies_content:
        logger.info("[FB-COOKIES] FACEBOOK_COOKIES tidak ditemukan di env, FB tanpa cookies.")
        return None
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix='sys_fb_cookies_', suffix='.txt')
        with os.fdopen(fd, 'w') as f:
            f.write(cookies_content)
        _FB_COOKIES_FILE = path
        logger.info("[FB-COOKIES] ✅ Cookies Facebook berhasil dimuat dari env var.")
        return path
    except Exception as e:
        logger.warning(f"[FB-COOKIES] Gagal setup cookies Facebook: {e}")
        return None

_setup_facebook_cookies()

# =============================================================================
# INSTAGRAM COOKIES SETUP
# Set env var INSTAGRAM_COOKIES di Railway dengan isi file cookies.txt (Netscape format)
# Export dari browser pakai ekstensi "Get cookies.txt LOCALLY" saat login Instagram
# Otomatis ditulis ke file temp saat server start, dipakai yt-dlp untuk Instagram
# =============================================================================

_IG_COOKIES_FILE = None

def _setup_instagram_cookies():
    """Tulis env var INSTAGRAM_COOKIES ke file temp, return path-nya."""
    global _IG_COOKIES_FILE
    cookies_content = os.environ.get('INSTAGRAM_COOKIES', '').strip()
    if not cookies_content:
        logger.info("[IG-COOKIES] INSTAGRAM_COOKIES tidak ditemukan di env, IG tanpa cookies.")
        return None
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix='sys_ig_cookies_', suffix='.txt')
        with os.fdopen(fd, 'w') as f:
            f.write(cookies_content)
        _IG_COOKIES_FILE = path
        logger.info("[IG-COOKIES] ✅ Cookies Instagram berhasil dimuat dari env var.")
        return path
    except Exception as e:
        logger.warning(f"[IG-COOKIES] Gagal setup cookies Instagram: {e}")
        return None

_setup_instagram_cookies()

try:
    import yt_dlp as _yt_dlp_ver
    logger.info(f"[YTDLP] Versi yt-dlp aktif: {_yt_dlp_ver.version.__version__}")
except Exception:
    logger.warning("[YTDLP] Gagal deteksi versi yt-dlp")

# =============================================================================
# RAPIDAPI YTSTREAM — fallback YouTube ketika yt-dlp gagal
# Set env var RAPIDAPI_KEY di Railway dengan API key dari rapidapi.com
# Subscribe ke: https://rapidapi.com/ytjar/api/ytstream-download-youtube-videos
# =============================================================================
_RAPIDAPI_KEY = os.environ.get('RAPIDAPI_KEY', '')

def _ytstream_get_info(url):
    """
    Ambil info + download URL video YouTube via RapidAPI YTStream.
    Return dict {title, thumbnail, duration, formats} atau None kalau gagal.
    formats = list of {quality, url, ext}
    """
    if not _RAPIDAPI_KEY:
        logger.warning("[YTSTREAM] RAPIDAPI_KEY tidak ada di env, skip fallback.")
        return None
    try:
        # Ekstrak video ID dari URL YouTube / Shorts
        vid_id = None
        for pattern in [
            r'youtu\.be/([^?&/]+)',
            r'youtube\.com/shorts/([^?&/]+)',
            r'youtube\.com/watch\?v=([^?&/]+)',
            r'youtube\.com/embed/([^?&/]+)',
        ]:
            m = re.search(pattern, url)
            if m:
                vid_id = m.group(1)
                break
        if not vid_id:
            logger.warning(f"[YTSTREAM] Gagal ekstrak video ID dari: {mask_url(url)}")
            return None

        logger.info(f"[YTSTREAM] Fetch info via RapidAPI untuk video ID: {vid_id}")
        resp = requests.get(
            "https://ytstream-download-youtube-videos.p.rapidapi.com/dl",
            params={"id": vid_id},
            headers={
                "x-rapidapi-key":  _RAPIDAPI_KEY,
                "x-rapidapi-host": "ytstream-download-youtube-videos.p.rapidapi.com",
            },
            timeout=20,
        )
        logger.info(f"[YTSTREAM] Response status: {resp.status_code}")
        if resp.status_code != 200:
            logger.warning(f"[YTSTREAM] Status bukan 200: {resp.status_code} — {resp.text[:200]}")
            return None

        data = resp.json()
        logger.info(f"[YTSTREAM] Response keys: {list(data.keys())}")

        title     = data.get('title', 'YouTube Video')
        thumbnail = data.get('thumbnail', '')
        duration  = data.get('lengthSeconds') or data.get('duration') or 0

        # Formats: ada di data['formats'] — list of {qualityLabel, url, mimeType}
        raw_formats = data.get('formats') or data.get('adaptiveFormats') or []
        formats = []
        for f in raw_formats:
            q_label = f.get('qualityLabel') or f.get('quality', '')
            dl_url  = f.get('url', '')
            mime    = f.get('mimeType', '')
            if dl_url and 'video/mp4' in mime:
                formats.append({
                    'quality': q_label,
                    'url':     dl_url,
                    'ext':     'mp4',
                })

        logger.info(f"[YTSTREAM] Berhasil: title={title[:50]} | {len(formats)} format MP4 tersedia")
        return {
            'title':     title,
            'thumbnail': thumbnail,
            'duration':  int(duration),
            'formats':   formats,
        }
    except Exception as e:
        logger.error(f"[YTSTREAM] Error: {e}")
        return None



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
    limit      = max(1, min(int(data.get('limit', 10)), 20))
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
    'instagram.com', 'twitter.com', 'x.com',
    'facebook.com', 'fb.watch',
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
        'format':       'best',
        'quiet':        True,
        'no_warnings':  True,
        'noplaylist':   True,
        'proxy':        _YTDLP_PROXY if _YTDLP_PROXY else None,
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
            # Non-TikTok (Instagram, Facebook): pakai engine masing-masing
            # Instagram: yt-dlp (masih jalan)
            # Facebook: facebook-scraper
            is_fb = any(x in tiktok_url for x in ['facebook.com', 'fb.watch'])

            import tempfile as _tempfile
            _fd2, tmp_base2 = _tempfile.mkstemp(prefix='vinder_fb_' if is_fb else 'vinder_ig_')
            os.close(_fd2)
            os.remove(tmp_base2)
            out_mp3_yt = tmp_base2 + '.mp3'

            if is_fb:
                fb_info     = _fb_get_info(tiktok_url)
                final_title = fb_info['title']
                _fb_download_audio(tiktok_url, out_mp3_yt)
            else:
                _ydl_opts_ig_fast = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'noplaylist': True, 'user_agent': META_UA}
                if _IG_COOKIES_FILE and os.path.exists(_IG_COOKIES_FILE):
                    _ydl_opts_ig_fast['cookiefile'] = _IG_COOKIES_FILE
                    logger.info("[IG] fast_mp3 yt-dlp info pakai cookies Instagram.")
                else:
                    logger.warning("[IG] fast_mp3 tidak ada cookies Instagram — mungkin gagal.")
                with yt_dlp.YoutubeDL(_ydl_opts_ig_fast) as ydl:
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


def _fb_resolve_url(url):
    """
    Resolve Facebook share/redirect URL ke URL asli video.
    Format /share/v/ dan /share/ butuh follow redirect ke URL final.
    Return URL yang sudah di-resolve (atau URL asli kalau bukan redirect).
    """
    share_patterns = ['/share/v/', '/share/p/', '/share/']
    is_share = any(p in url for p in share_patterns)
    if not is_share:
        return url
    try:
        logger.info(f"[FB] Resolve share URL: {mask_url(url)}")
        r = session.head(url, allow_redirects=True, timeout=10)
        resolved = r.url
        logger.info(f"[FB] Resolved -> {mask_url(resolved)}")
        return resolved
    except Exception as e:
        logger.warning(f"[FB] Gagal resolve URL, pakai URL asli: {e}")
        return url


def _fb_get_info(url):
    """
    Ambil metadata video Facebook via yt-dlp + FB cookies.
    Support: /reel/, /watch?v=, /videos/, /share/v/, fb.watch
    """
    url = _fb_resolve_url(url)
    logger.info(f"[FB] Ambil info via yt-dlp: {mask_url(url)}")

    try:
        ydl_opts = {
            'quiet':       True,
            'no_warnings': True,
            'noplaylist':  True,
            'proxy':       _YTDLP_PROXY if _YTDLP_PROXY else None,
            'user_agent':  META_UA,
        }
        if _FB_COOKIES_FILE and os.path.exists(_FB_COOKIES_FILE):
            ydl_opts['cookiefile'] = _FB_COOKIES_FILE
            logger.info("[FB] yt-dlp pakai cookies Facebook.")
        else:
            logger.warning("[FB] Tidak ada cookies Facebook — Reels mungkin gagal.")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title   = (info.get('title') or 'Facebook Video').replace('\n', ' ')[:80]
        cover   = info.get('thumbnail') or ''
        author  = info.get('uploader') or info.get('channel') or 'Facebook'
        dur_sec = int(info.get('duration') or 0)

        # Pilih format sweet spot: ada height (= ada video), bukan audio-only, resolusi <= 1080p
        video_url = None
        formats = info.get('formats') or []
        video_formats = [
            f for f in formats
            if f.get('url')
            and (f.get('height') or 0) > 0
            and f.get('acodec') not in (None, 'none')
            and (f.get('height') or 0) <= 1080
        ]
        if video_formats:
            best = max(video_formats, key=lambda f: f.get('height') or 0)
            video_url = best.get('url')
            logger.info(f"[FB] Sweet spot format: {best.get('height')}p | vcodec={best.get('vcodec')} | acodec={best.get('acodec')}")
        else:
            # Fallback: url default dari info (biasanya merged best)
            video_url = info.get('url') or (
                (info.get('formats') or [{}])[-1].get('url')
            )
            logger.info(f"[FB] Tidak ada format video+audio <=1080p, fallback ke url default")

        if not video_url:
            raise RuntimeError("Gagal mengekstrak video URL dari Meta. IP terblokir atau postingan private.")

        logger.info(f"[FB] yt-dlp OK: title={title[:40]} | video_url={'Ada' if video_url else 'Kosong'}")
        return {
            'title':        title,
            'cover':        cover,
            'author':       author,
            'duration_sec': dur_sec,
            'video_url':    video_url,
        }

    except Exception as e:
        logger.error(f"[FB] _fb_get_info gagal: {type(e).__name__}: {e}")
        raise


def _fb_download_video(url, out_mp4):
    """
    Download video Facebook ke out_mp4 via yt-dlp dengan merge video+audio.
    Facebook pakai DASH (video & audio stream terpisah), yt-dlp handle merge otomatis.
    Target: bestvideo[height<=720]+bestaudio — sweet spot ~15-30MB.
    """
    url = _fb_resolve_url(url)
    ydl_opts = {
        'format':      'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best/b',
        'outtmpl':     out_mp4 + '.%(ext)s',
        'quiet':       True,
        'no_warnings': True,
        'noplaylist':  True,
        'proxy':       _YTDLP_PROXY if _YTDLP_PROXY else None,
        'merge_output_format': 'mp4',
        'user_agent':  META_UA,
    }
    if _FB_COOKIES_FILE and os.path.exists(_FB_COOKIES_FILE):
        ydl_opts['cookiefile'] = _FB_COOKIES_FILE
        logger.info("[FB] yt-dlp download pakai cookies Facebook.")
    else:
        logger.warning("[FB] Tidak ada cookies Facebook — download mungkin gagal.")

    logger.info(f"[FB] Download video via yt-dlp (merge DASH): {mask_url(url)}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # yt-dlp output: out_mp4.mp4 (karena merge_output_format=mp4)
    expected = out_mp4 + '.mp4'
    if os.path.exists(expected):
        os.replace(expected, out_mp4)
    elif not os.path.exists(out_mp4):
        import glob as _glob
        candidates = _glob.glob(out_mp4 + '.*')
        if candidates:
            os.replace(candidates[0], out_mp4)
        else:
            raise RuntimeError("File MP4 Facebook tidak ditemukan setelah download.")

    size_mb = os.path.getsize(out_mp4) / 1024 / 1024
    logger.info(f"[FB] Download selesai: {size_mb:.2f} MB -> {out_mp4}")


def _fb_download_audio(url, out_mp3):
    """
    Download audio Facebook via yt-dlp + FB cookies (primary).
    Fallback ke facebook-scraper + ffmpeg pipe kalau yt-dlp gagal.
    """
    # --- PRIMARY: yt-dlp bestaudio + FB cookies ---
    try:
        ydl_opts = {
            'format':        'bestaudio/best',
            'outtmpl':       out_mp3 + '.%(ext)s',
            'quiet':         True,
            'no_warnings':   True,
            'noplaylist':    True,
            'proxy':         _YTDLP_PROXY if _YTDLP_PROXY else None,
            'postprocessors': [{
                'key':              'FFmpegExtractAudio',
                'preferredcodec':   'mp3',
                'preferredquality': '0',
            }],
            'keepvideo': False,
            'user_agent':  META_UA,
        }
        if _FB_COOKIES_FILE and os.path.exists(_FB_COOKIES_FILE):
            ydl_opts['cookiefile'] = _FB_COOKIES_FILE
            logger.info("[FB] yt-dlp audio pakai cookies Facebook.")
        else:
            logger.warning("[FB] Tidak ada cookies Facebook — download audio mungkin gagal.")

        logger.info(f"[FB] Download audio via yt-dlp: {mask_url(url)}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # yt-dlp output: out_mp3.mp3 (postprocessor rename)
        expected = out_mp3 + '.mp3'
        if os.path.exists(expected):
            os.replace(expected, out_mp3)
            size_mb = os.path.getsize(out_mp3) / 1024 / 1024
            logger.info(f"[FB] yt-dlp audio selesai: {size_mb:.2f} MB")
            return
        elif os.path.exists(out_mp3):
            logger.info("[FB] yt-dlp audio selesai (langsung).")
            return
        else:
            import glob as _glob
            candidates = _glob.glob(out_mp3 + '.*')
            if candidates:
                os.replace(candidates[0], out_mp3)
                logger.info(f"[FB] yt-dlp audio (fallback rename): {out_mp3}")
                return
            raise RuntimeError("File audio tidak ditemukan setelah yt-dlp selesai.")

    except Exception as e:
        logger.warning(f"[FB] yt-dlp audio gagal ({type(e).__name__}: {e}), coba fallback scraper+ffmpeg...")

    # --- FALLBACK: facebook-scraper + ffmpeg pipe ---
    info = _fb_get_info(url)
    video_url = info.get('video_url')
    if not video_url:
        raise RuntimeError("URL video Facebook tidak ditemukan (semua strategi gagal).")

    logger.info(f"[FB] Pipe audio ke ffmpeg dari: {mask_url(video_url)}")
    headers = TIKTOK_HEADERS.copy()
    headers["Range"] = "bytes=0-"

    bitrate = detect_audio_bitrate(video_url, headers)

    r = session.get(video_url, stream=True, timeout=60, headers=headers, allow_redirects=True)
    r.raise_for_status()

    cmd = [
        'ffmpeg', '-y',
        '-i', 'pipe:0',
        '-vn',
        '-acodec', 'libmp3lame',
        '-ab', bitrate,
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
        logger.error(f"[FB] ffmpeg error: {err}")
        raise RuntimeError("Gagal memproses audio Facebook, silakan coba lagi.")

    size_mb = os.path.getsize(out_mp3) / 1024 / 1024
    logger.info(f"[FB] Audio encode selesai: {size_mb:.2f} MB ({bitrate})")


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
                'user_agent':  META_UA,
            }
            if _IG_COOKIES_FILE and os.path.exists(_IG_COOKIES_FILE):
                ydl_opts_ig['cookiefile'] = _IG_COOKIES_FILE
                logger.info("[IG] yt-dlp info pakai cookies Instagram.")
            else:
                logger.warning("[IG] Tidak ada cookies Instagram — info mungkin gagal.")
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
            # ── FACEBOOK: pakai facebook-scraper ──
            is_fb = any(x in url for x in ['facebook.com', 'fb.watch'])
            if not is_fb:
                return jsonify({"status": "error", "msg": "Platform tidak didukung."}), 400

            fb_info = _fb_get_info(url)
            return jsonify({
                "status":   "success",
                "title":    fb_info['title'],
                "cover":    fb_info['cover'],
                "author":   fb_info['author'],
                "duration": format_durasi(fb_info['duration_sec']),
                "size":     "N/A",
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
            # ── INSTAGRAM: download via yt-dlp (sama seperti Facebook) ──
            _fd, tmp_base = tempfile.mkstemp(prefix='vinder_ig_')
            os.close(_fd)
            os.remove(tmp_base)
            out_mp4 = tmp_base + '.mp4'

            ydl_opts_ig_dl = {
                'format':              'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best/b',
                'outtmpl':             out_mp4 + '.%(ext)s',
                'quiet':               True,
                'no_warnings':         True,
                'noplaylist':          True,
                'proxy':               _YTDLP_PROXY if _YTDLP_PROXY else None,
                'merge_output_format': 'mp4',
                'user_agent':          META_UA,
            }
            if _IG_COOKIES_FILE and os.path.exists(_IG_COOKIES_FILE):
                ydl_opts_ig_dl['cookiefile'] = _IG_COOKIES_FILE
                logger.info("[IG] yt-dlp download pakai cookies Instagram.")
            else:
                logger.warning("[IG] Tidak ada cookies Instagram — download mungkin gagal.")

            try:
                with yt_dlp.YoutubeDL(ydl_opts_ig_dl) as ydl:
                    ydl.download([url])
            except Exception as e:
                logger.error(f"[IG] yt-dlp download gagal: {e}")
                return jsonify({"status": "error", "msg": "Gagal memproses file, format tidak tersedia atau terblokir."}), 500

            # Resolve nama file output yt-dlp
            expected = out_mp4 + '.mp4'
            if os.path.exists(expected):
                os.replace(expected, out_mp4)
            elif not os.path.exists(out_mp4):
                import glob as _glob
                candidates = _glob.glob(out_mp4 + '.*')
                if candidates:
                    os.replace(candidates[0], out_mp4)
                else:
                    return jsonify({"status": "error", "msg": "Gagal memproses file, format tidak tersedia atau terblokir."}), 500

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
            # ── FACEBOOK: download via yt-dlp ──
            is_fb = any(x in url for x in ['facebook.com', 'fb.watch'])
            if not is_fb:
                return jsonify({"status": "error", "msg": "Platform tidak didukung."}), 400

            _fd, tmp_base = tempfile.mkstemp(prefix='vinder_fb_')
            os.close(_fd)
            os.remove(tmp_base)
            out_mp4 = tmp_base + '.mp4'

            try:
                _fb_download_video(url, out_mp4)
            except Exception as e:
                logger.error(f"[FB] yt-dlp download gagal: {e}")
                return jsonify({"status": "error", "msg": "Gagal memproses file, format tidak tersedia atau terblokir."}), 500

            if not os.path.exists(out_mp4):
                return jsonify({"status": "error", "msg": "Gagal download video Facebook."}), 500

            filename  = f"[Vinder].{safe_filename(title)}.mp4"
            file_size = os.path.getsize(out_mp4)
            logger.info(f"[MP4DL] FB siap stream: {filename} ({file_size // 1024} KB)")

            def generate_fb_mp4():
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
                stream_with_context(generate_fb_mp4()),
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

_GROQ_API_KEY = os.environ.get("OPENROUTER_API_KEY")
_HEALTH_SAMPLE_URL = "https://vt.tiktok.com/ZSxLdGQbS/"

HEALTH_SAMPLES = {
    "TikTok":    "https://vt.tiktok.com/ZSxLdGQbS/",
    "Instagram": "https://www.instagram.com/p/C4tunnElWEz/",
    "Facebook":  "https://www.facebook.com/watch/?v=1392506781438996",
}

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
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openai/gpt-oss-120b:free",
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
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "openai/gpt-oss-120b:free",
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
    """Jalankan health check harian: test TikTok, Instagram, Facebook, kirim hasil ke Telegram."""
    import random as _random
    from datetime import datetime as _datetime

    logger.info(f"[DAILY] Mulai health check harian — {_datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    platform_results = []
    any_error = False

    # --- TikTok: test via TikWM API ---
    tiktok_error = None
    try:
        resp = requests.get(
            f"https://www.tikwm.com/api/?url={HEALTH_SAMPLES['TikTok']}",
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            tiktok_error = (
                f"TikWM return code={data.get('code')}, msg={data.get('msg')}.\n"
                f"Raw response: {str(data)[:300]}"
            )
        else:
            v        = data.get("data", {})
            play_url = v.get("play") or v.get("hdplay")
            size     = v.get("size", 0)
            if not play_url:
                tiktok_error = "TikWM response OK tapi link download tidak muncul (play/hdplay kosong)."
            elif size == 0:
                tiktok_error = "TikWM response OK, link ada, tapi size video = 0 bytes."

    except requests.exceptions.Timeout:
        tiktok_error = "Request ke TikWM timeout (>15 detik). Server mungkin lambat atau down."
    except requests.exceptions.ConnectionError:
        tiktok_error = "Gagal konek ke TikWM. Cek koneksi server atau TikWM sedang down."
    except Exception as e:
        tiktok_error = f"Error tidak terduga: {type(e).__name__}: {str(e)}"

    if tiktok_error:
        platform_results.append(f"[TikTok] ❌ GAGAL: {tiktok_error}")
        any_error = True
        logger.error(f"[DAILY] TikTok health check GAGAL — {tiktok_error}")
    else:
        platform_results.append("[TikTok] ✅ OK — TikWM response normal, link download tersedia.")
        logger.info("[DAILY] TikTok health check PASSED.")

    # --- Instagram: test via yt-dlp + IG cookies ---
    ig_error = None
    try:
        _ydl_opts_ig_hc = {
            'quiet':       True,
            'no_warnings': True,
            'noplaylist':  True,
        }
        if _IG_COOKIES_FILE and os.path.exists(_IG_COOKIES_FILE):
            _ydl_opts_ig_hc['cookiefile'] = _IG_COOKIES_FILE
        with yt_dlp.YoutubeDL(_ydl_opts_ig_hc) as ydl:
            ig_info = ydl.extract_info(HEALTH_SAMPLES['Instagram'], download=False)
        if not ig_info or not ig_info.get('url'):
            ig_error = f"yt-dlp berhasil konek tapi URL video kosong. Data: {str(ig_info)[:200]}"
        else:
            logger.info(f"[DAILY] Instagram health check PASSED — title={ig_info.get('title','')[:40]}")
    except Exception as e:
        ig_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[DAILY] Instagram health check GAGAL — {ig_error}")

    if ig_error:
        platform_results.append(f"[Instagram] ❌ GAGAL: {ig_error}")
        any_error = True
    else:
        platform_results.append("[Instagram] ✅ OK — yt-dlp berhasil ambil metadata video.")

    # --- Facebook: test via _fb_get_info ---
    fb_error = None
    try:
        fb_info = _fb_get_info(HEALTH_SAMPLES['Facebook'])
        if not fb_info or not fb_info.get('video_url'):
            fb_error = f"_fb_get_info berhasil tapi video_url kosong. Data: {str(fb_info)[:200]}"
        else:
            logger.info(f"[DAILY] Facebook health check PASSED — title={fb_info.get('title','')[:40]}")
    except Exception as e:
        fb_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[DAILY] Facebook health check GAGAL — {fb_error}")

    if fb_error:
        platform_results.append(f"[Facebook] ❌ GAGAL: {fb_error}")
        any_error = True
    else:
        platform_results.append("[Facebook] ✅ OK — yt-dlp berhasil ambil metadata video.")

    # --- Kirim hasil ke Telegram + Groq ---
    now_str     = _datetime.now().strftime("%d/%m/%Y %H:%M")
    full_report = "\n".join(platform_results)

    if any_error:
        logger.error(f"[DAILY] Health check selesai — ada platform yang GAGAL.\n{full_report}")
        analisis = _analisis_groq_daily(full_report)
        kirim_notif(
            f"❌ Vinder Health Check GAGAL!\n"
            f"🕒 {now_str}\n\n"
            f"📋 Hasil per Platform:\n{full_report}\n\n"
            f"🤖 Analisis AI:\n{analisis}"
        )
    else:
        logger.info(f"[DAILY] Health check PASSED — semua platform normal.\n{full_report}")
        pesan_acak = _random.choice(_PESAN_SUKSES_DAILY)
        kirim_notif(
            f"{pesan_acak}\n"
            f"🕒 {now_str}\n\n"
            f"📋 Detail:\n{full_report}"
        )


def _daily_health_loop():
    """Background thread: startup AI ping sekali, lalu health check tiap jam 15:00.
    Pakai polling interval 60 detik agar tahan banting saat server restart Railway.
    """
    import time as _time
    from datetime import datetime as _datetime

    _time.sleep(5)  # tunggu server ready dulu
    _groq_startup_ping()  # test AI langsung saat server ON

    _last_health_check_date = None  # track tanggal terakhir health check dijalankan

    while True:
        _time.sleep(60)  # polling tiap 60 detik, bukan sleep berjam-jam
        try:
            now   = _datetime.now()
            today = now.date()
            if now.hour == 15 and now.minute < 60 and _last_health_check_date != today:
                logger.info(f"[DAILY] Jam 15:00 terdeteksi, menjalankan health check — {now.strftime('%Y-%m-%d %H:%M:%S')}")
                _last_health_check_date = today
                _run_daily_health_check()
        except Exception as e:
            logger.warning(f"[DAILY] Error di loop health check: {e}")


# =============================================================================
# MAIN
# =============================================================================

def _self_ping_loop():
    """Self-ping ke server sendiri tiap 4 menit supaya Railway tidak sleep."""
    import time as _time
    _time.sleep(60)  # tunggu server ready dulu
    port = int(os.environ.get('PORT', 5000))
    url  = f"http://127.0.0.1:{port}/api/ping"
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

# =============================================================================
# HEARTBEAT COOKIES — Pemanasan Session
# Request ringan ke tiap platform setiap 8 jam supaya cookies tidak "dingin"
# dan session dianggap masih aktif di sisi server platform.
# =============================================================================

def _heartbeat_cookies_loop():
    """
    Kirim request ringan ke TikTok, Instagram, Facebook setiap 8 jam
    menggunakan cookies yang ada di env var, supaya session tetap 'Live'.
    Tidak download apapun — hanya extract_info (metadata saja).
    """
    import time as _time
    from datetime import datetime as _datetime
    import random

    # Sampel URL publik yang stabil untuk test — bukan konten sensitif
    _HB_SAMPLES = {
        'TikTok':    'https://vt.tiktok.com/ZSxLdGQbS/',
        'Instagram': 'https://www.instagram.com/p/C4tunnElWEz/',
        'Facebook':  'https://www.facebook.com/watch/?v=1392506781438996',
    }

    _INTERVAL_SECONDS = 8 * 60 * 60  # 8 jam

    _time.sleep(90)  # tunggu server ready + cookies sudah di-setup
    logger.info("[HEARTBEAT] Heartbeat cookies loop aktif — interval 8 jam.")

    while True:
        now_str = _datetime.now().strftime("%d/%m/%Y %H:%M")
        hasil   = []

        # --- TikTok ---
        try:
            resp = requests.get(
                f"https://www.tikwm.com/api/?url={_HB_SAMPLES['TikTok']}",
                timeout=15
            )
            data = resp.json()
            if data.get('code') == 0 and data.get('data', {}).get('play'):
                logger.info("[HEARTBEAT] TikTok — TikWM OK.")
                hasil.append("🎵 TikTok ✅ TikWM aktif")
            else:
                raise ValueError(f"TikWM code={data.get('code')} msg={data.get('msg')}")
        except Exception as e:
            logger.warning(f"[HEARTBEAT] TikTok heartbeat gagal: {e}")
            hasil.append(f"🎵 TikTok ❌ Gagal: {e}")

        # --- Instagram ---
        if _IG_COOKIES_FILE and os.path.exists(_IG_COOKIES_FILE):
            try:
                _ydl_opts_ig_hb = {
                    'quiet':       True,
                    'no_warnings': True,
                    'noplaylist':  True,
                    'cookiefile':  _IG_COOKIES_FILE,
                    'proxy':       _YTDLP_PROXY if _YTDLP_PROXY else None,
                    'user_agent':  META_UA,
                }
                with yt_dlp.YoutubeDL(_ydl_opts_ig_hb) as ydl:
                    ydl.extract_info(_HB_SAMPLES['Instagram'], download=False)
                logger.info("[HEARTBEAT] Instagram cookies — session OK.")
                hasil.append("📸 Instagram ✅ Session aktif")
            except Exception as e:
                logger.warning(f"[HEARTBEAT] Instagram cookies heartbeat gagal: {e}")
                hasil.append(f"📸 Instagram ❌ Gagal: {e}")
        else:
            logger.info("[HEARTBEAT] Instagram cookies tidak ada, skip.")

        # --- Facebook ---
        if _FB_COOKIES_FILE and os.path.exists(_FB_COOKIES_FILE):
            try:
                _ydl_opts_fb_hb = {
                    'quiet':       True,
                    'no_warnings': True,
                    'noplaylist':  True,
                    'cookiefile':  _FB_COOKIES_FILE,
                    'proxy':       _YTDLP_PROXY if _YTDLP_PROXY else None,
                    'user_agent':  META_UA,
                }
                with yt_dlp.YoutubeDL(_ydl_opts_fb_hb) as ydl:
                    ydl.extract_info(_HB_SAMPLES['Facebook'], download=False)
                logger.info("[HEARTBEAT] Facebook cookies — session OK.")
                hasil.append("📘 Facebook ✅ Session aktif")
            except Exception as e:
                logger.warning(f"[HEARTBEAT] Facebook cookies heartbeat gagal: {e}")
                hasil.append(f"📘 Facebook ❌ Gagal: {e}")
        else:
            logger.info("[HEARTBEAT] Facebook cookies tidak ada, skip.")

        # Kirim ringkasan ke Telegram hanya kalau ada cookies yang diproses
        if hasil:
            ada_gagal = any("❌" in h for h in hasil)
            icon      = "⚠️" if ada_gagal else "💓"
            kirim_notif(
                f"{icon} Vinder Heartbeat Cookies\n"
                f"🕒 {now_str}\n\n"
                + "\n".join(hasil)
            )

        _jitter = random.randint(60, 2700)
        _time.sleep(_INTERVAL_SECONDS + _jitter)


if __name__ == "__main__":
    threading.Thread(target=_self_ping_loop, daemon=True).start()
    threading.Thread(target=_daily_health_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_cookies_loop, daemon=True).start()
    kirim_notif("Sistem Vinder Berhasil ON di Railway!")
    if _YTDLP_PROXY:
        kirim_notif("🌐 Proxy Residensial telah Active.")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)