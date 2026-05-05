import os
import re
import time
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


# =============================================================================
# TELEGRAM NOTIF
# =============================================================================

TELEGRAM_NOTIF_ENABLED = False  # Ganti ke True untuk aktifkan notif Telegram
                                                                    #  Ganti ke  False untuk matikan notif Telegram
def kirim_notif(pesan):
    """Kirim notifikasi ke Telegram Bot."""
    if not TELEGRAM_NOTIF_ENABLED:
        return
    try:
        TOKEN   = "8690695346:AAG80VMrIw-s4vQUg5CeYbyG0H1Ecn-CsME"
        CHAT_ID = "8279166856"
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": pesan},
            timeout=3
        )
    except Exception as e:
        logger.warning(f"[NOTIF] Gagal kirim notif Telegram: {e}")

app = Flask(__name__, static_folder='.', static_url_path='')

from flask_cors import CORS
CORS(app)

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
        logger.info(f"[URL] Resolved: {url} -> {r.url}")
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
                logger.info(f"[IMG] TikWM origin_cover: {origin_cover[:80] if origin_cover else 'NONE'}")
                logger.info(f"[IMG] TikWM cover: {cover_plain[:80] if cover_plain else 'NONE'}")
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

    logger.info(f"[DL] Pipe audio ke ffmpeg: {audio_url[:80]}...")

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
        raise RuntimeError(f"ffmpeg pipe->mp3 gagal: {err}")

    size_mb = os.path.getsize(out_mp3) / 1024 / 1024
    logger.info(f"[MP3] Encode selesai: {size_mb:.2f} MB ({bitrate})")


def download_audio_ytdlp(url, out_mp3):
    """
    Download audio asli video via yt-dlp dengan format bestaudio.
    Dipakai untuk YouTube, Instagram, Facebook.
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

    logger.info(f"[DL] yt-dlp bestaudio: {url[:80]}...")
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
            raise RuntimeError("yt-dlp tidak menghasilkan file audio")


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
        from mutagen.id3 import ID3, APIC, ID3NoHeaderError

        with open(thumb_path, 'rb') as img_f:
            img_data = img_f.read()

        try:
            tags = ID3(mp3_path)
        except ID3NoHeaderError:
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
        emit(15, "[API] Ambil metadata & audio stream URL...")

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
            emit(30, "[MP3] Download audio stream langsung...")
            download_audio_direct(audio_url, out_mp3)
        else:
            # Terakhir: fallback ke TikWM video URL + extract audio
            # for_audio=True -> ambil play/SD bukan hdplay, audio track identik tapi stream lebih ringan
            emit(20, "[API] Fallback: ambil URL dari TikWM...")
            video_url, cover_url2, tikwm_title = get_meta_via_tikwm(url, for_audio=True)
            if not cover_url:
                cover_url = cover_url2
            if not final_title or final_title == 'audio':
                final_title = tikwm_title or title
            if not video_url:
                raise RuntimeError("Gagal ambil audio maupun video dari TikTok")
            emit(35, "[MP3] Download & extract audio dari video...")
            download_audio_direct(video_url, out_mp3)

    else:
        # --- PLATFORM LAIN: yt-dlp bestaudio + FFmpegExtractAudio ---
        emit(15, "[API] Ambil audio stream via yt-dlp...")
        final_title = title

        try:
            # Ambil info dulu untuk title & cover
         with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'noplaylist': True}) as ydl:
                info = ydl.extract_info(url, download=False)
                final_title = info.get('title', title)
                cover_url   = info.get('thumbnail')
        except Exception:
            cover_url = None

        emit(30, "[MP3] Download audio langsung (bestaudio)...")
        download_audio_ytdlp(url, out_mp3)

    # Embed cover art kalau ada
    if cover_url:
        cover_path = out_tmpl + '_cover.jpg'
        emit(88, "[IMG] Embed cover art...")
        if download_cover(cover_url, cover_path):
            embed_cover(out_mp3, cover_path)

    return out_mp3, final_title


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    kirim_notif("Visitor masuk ke Web Vinder")
    return send_file('vinder.html')


@app.route('/api/search', methods=['POST'])
def search_videos_api():
    data       = request.json
    keyword    = data.get('keyword')
    limit      = data.get('limit', 10)
    filter_str = data.get('filter', '').strip()
    kirim_notif(f"User nyari keyword: {keyword}")
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
    'instagram.com',
    'facebook.com', 'fb.watch',
]

def is_supported_url(url):
    if not url:
        return False
    return any(p in url for p in SUPPORTED_PLATFORMS)


@app.route('/api/download_url', methods=['POST'])
def download_url_api():
    data      = request.json
    url_input = data.get('url', '').strip()
    logger.info(f"[URL] Processing: {url_input}")

    # FIX: tolak URL platform yang tidak didukung (Pinterest, dll)
    # Sebelumnya Pinterest URL lolos ke yt_dlp dan sering menyebabkan
    # Flask fallback serve vinder.html sebagai file download
    if not is_supported_url(url_input):
        logger.warning(f"[WARN] Platform tidak didukung: {url_input}")
        return jsonify({
            "status": "error",
            "msg":    "Platform tidak didukung. Vinder mendukung: TikTok, YouTube, Instagram, Facebook."
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
                result = {
                    "status":   "success",
                    "title":    v.get('title', 'TikTok Video'),
                    "cover":    v.get('origin_cover') or v.get('cover'),
                    "author":   v.get('author', {}).get('nickname', 'User'),
                    "duration": f"{v.get('duration', 0)}s",
                    "size":     f"{v.get('size', 0) / 1024 / 1024:.2f}MB",
                    "play":     v.get('play'),
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
    kirim_notif(f"User download MP4: {title}")

    if not video_url:
        return "URL Kosong", 400

    try:
        r, _ = fetch_video_stream(video_url, fallback_url)

        if r is None or r.status_code >= 400:
            return "Gagal: Video tidak ditemukan atau link kadaluarsa.", 403

        content_type = r.headers.get('Content-Type', '').lower()
        if 'text/html' in content_type:
            return "Gagal: Server mengirimkan file korup (HTML).", 403

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
        return f"Error: {str(e)}", 500


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

    def generate():
        def send(pct, msg):
            return f"data: {pct}|{msg}\n\n"

        uid      = str(int(time.time() * 1000))
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
                emit_sse(5, "[URL] Resolve URL...")
                url = tiktok_url
                if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                    url = resolve_tiktok_url(url)

                out_mp3, final_title = process_mp3_pipeline(url, title, out_tmpl, progress_cb=emit_sse)


                if not os.path.exists(out_mp3):
                    q.put(send(-1, "[ERR] File MP3 tidak berhasil dibuat"))
                    do_cleanup(out_tmpl)
                    q.put(None)
                    return

                fname = f"[Vinder].{safe_filename(final_title)}.mp3"
                emit_sse(95, "[PKG] Siapkan file...")
                with open(out_tmpl + '.ready', 'w') as f:
                    f.write(fname)

                q.put(send(100, f"[OK] DONE|{uid}|{fname}"))
            except Exception as e:
                logger.error(f"SSE MP3 Error: {e}")
                do_cleanup(out_tmpl)
                q.put(send(-1, f"[ERR] Error: {str(e)[:100]}"))
            finally:
                q.put(None)  # sentinel = selesai

        t = threading.Thread(target=run_pipeline, daemon=True)
        t.start()

        while True:
            try:
                item = q.get(timeout=120)
            except queue.Empty:
                yield send(-1, "[ERR] Timeout: proses terlalu lama")
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
    uid = request.args.get('uid')
    if not uid:
        return "UID kosong", 400

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

    if 'vt.tiktok.com' in tiktok_url or 'vm.tiktok.com' in tiktok_url:
        tiktok_url = resolve_tiktok_url(tiktok_url)

    uid      = str(int(time.time() * 1000))
    out_tmpl = f'/tmp/vinder_{uid}'

    try:
        logger.info(f"[MP3] MP3 request: {tiktok_url}")
        out_mp3, final_title = process_mp3_pipeline(tiktok_url, title, out_tmpl)

        if not os.path.exists(out_mp3):
            do_cleanup(out_tmpl)
            return "Gagal: File MP3 tidak berhasil dibuat.", 500

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
        logger.error(f"MP3 Error: {str(e)}")
        do_cleanup(out_tmpl)
        return f"Error: {str(e)}", 500




@app.route('/api/fast_mp3', methods=['GET', 'POST'])
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

    kirim_notif(f"User download MP3: {tiktok_url}")

    if not tiktok_url:
        return "URL Kosong", 400

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
            logger.info(f"[FETCH] Fresh fetch TikWM untuk: {tiktok_url[-40:]}")
            vid_url, _, tikwm_title = get_meta_via_tikwm(tiktok_url, for_audio=True)
            video_url   = vid_url
            audio_url   = vid_url
            final_title = tikwm_title or title

        else:
            # Non-TikTok: tetap pakai yt-dlp
            ydl_opts = {
                'format':      'bestaudio/best',
                'quiet':       True,
                'no_warnings': True,
                'noplaylist':  True,
                'user_agent':  TIKTOK_UA,
                'http_headers': DEFAULT_HEADERS,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info        = ydl.extract_info(tiktok_url, download=False)
                audio_url   = info.get('url')
                video_url   = info.get('url')
                final_title = info.get('title', title)

        if not audio_url:
            return "Gagal: tidak bisa ambil URL audio", 500

        # OPTIMASI 3: Cover dari frame tengah - 1 ffmpeg command, no ffprobe
        # -sseof -0.5 = seek ke 50% dari akhir (efektif = tengah untuk video pendek)
        # Lebih akurat: pakai -ss 50% tapi ffmpeg support ini via metadata
        # Trick: seek ke posisi relatif dengan -ss dan total duration dari header
        cover_raw  = [None]
        cover_done = threading.Event()

        def extract_cover_fast():
            """Extract frame tengah video - 1 subprocess, no ffprobe."""
            src = video_url or audio_url
            try:
                # Trick: ffmpeg baca sedikit header dulu untuk durasi
                # lalu seek ke tengah - semua dalam 1 command
                # -sseof -N seek dari akhir N detik (kita pakai durasi/2 = seek dari akhir durasi/2)
                # Karena kita ga tau durasi, pakai pendekatan: seek ke 5 detik dulu,
                # jika gagal fallback ke detik 1
                frame_proc = subprocess.run(
                    [
                        'ffmpeg', '-y',
                        '-ss', '00:00:05',       # seek ke detik 5 (tengah video ~10 detik)
                        '-i', src,
                        '-vframes', '1',
                        '-vf', 'crop=min(iw\\,ih):min(iw\\,ih),scale=500:500',
                        '-f', 'image2',
                        '-vcodec', 'mjpeg',
                        'pipe:1',
                    ],
                    capture_output=True, timeout=12,
                )
                if frame_proc.returncode == 0 and len(frame_proc.stdout) > 500:
                    cover_raw[0] = frame_proc.stdout
                    logger.info(f"[IMG] Frame cover OK ({len(cover_raw[0])//1024}KB)")
                else:
                    # Fallback: detik 1 (video sangat pendek < 5 detik)
                    frame_proc2 = subprocess.run(
                        [
                            'ffmpeg', '-y',
                            '-ss', '00:00:01',
                            '-i', src,
                            '-vframes', '1',
                            '-vf', 'crop=min(iw\\,ih):min(iw\\,ih),scale=500:500',
                            '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1',
                        ],
                        capture_output=True, timeout=10,
                    )
                    if frame_proc2.returncode == 0 and len(frame_proc2.stdout) > 500:
                        cover_raw[0] = frame_proc2.stdout
                        logger.info(f"[IMG] Frame fallback OK ({len(cover_raw[0])//1024}KB)")
                    else:
                        logger.warning("[WARN] Frame extract gagal semua")
            except Exception as e:
                logger.warning(f"[WARN] Cover error: {e}")
            finally:
                cover_done.set()

        # OPTIMASI 4: cover + audio paralel
        cover_thread = threading.Thread(target=extract_cover_fast, daemon=True)
        cover_thread.start()

        # ── Encode audio via ffmpeg pipe ──
        audio_headers = TIKTOK_HEADERS.copy()
        audio_headers['Range'] = 'bytes=0-'

        r = session.get(audio_url, stream=True, timeout=30, headers=audio_headers, allow_redirects=True)
        if r.status_code >= 400:
            return f"Gagal: CDN return {r.status_code}", 502

        tmp_mp3 = tempfile.mktemp(suffix='.mp3')

        proc = subprocess.Popen(
            ['ffmpeg', '-y', '-i', 'pipe:0', '-vn',
             '-acodec', 'libmp3lame', '-ab', '128k', '-ar', '44100',
             '-f', 'mp3', tmp_mp3],
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
            raise RuntimeError(f"ffmpeg encode gagal: {err}")

        # Tunggu cover (max 3 detik — audio encode biasanya lebih lama)
        cover_done.wait(timeout=3)

        # ── Embed cover via mutagen ──
        if cover_raw[0]:
            try:
                from mutagen.id3 import ID3, APIC
                from mutagen.id3 import ID3NoHeaderError
                try:
                    tags = ID3(tmp_mp3)
                except ID3NoHeaderError:
                    tags = ID3()
                tags.add(APIC(
                    encoding=3, mime='image/jpeg',
                    type=3, desc='Cover',
                    data=cover_raw[0],
                ))
                tags.save(tmp_mp3, v2_version=3)
                logger.info(f"[IMG] Cover embed OK ({len(cover_raw[0])//1024}KB)")
            except Exception as e:
                logger.warning(f"[WARN] Cover embed gagal: {e}")

        # ── Stream MP3 ke browser ──
        filename  = f"[Vinder].{safe_filename(final_title)}.mp3"
        file_size = os.path.getsize(tmp_mp3)

        def generate_and_cleanup():
            try:
                with open(tmp_mp3, 'rb') as f:
                    while True:
                        chunk = f.read(512 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                try:
                    os.remove(tmp_mp3)
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
        logger.error(f"fast_mp3 error: {e}")
        return f"Error: {str(e)}", 500

# =============================================================================
# MULTI-PLATFORM MP4 DOWNLOAD
# =============================================================================

# Platform list yang didukung untuk download MP4
MP4_SUPPORTED_PLATFORMS = [
    # TikTok
    'tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com',
    # YouTube
    'youtube.com', 'youtu.be', 'www.youtube.com',
    # Instagram
    'instagram.com', 'www.instagram.com',
    # Facebook
    'facebook.com', 'fb.watch', 'www.facebook.com', 'fb.com',
]

def detect_platform(url):
    """Detect platform dari URL. Return string platform name."""
    url_lower = url.lower()
    if any(x in url_lower for x in ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com']):
        return 'tiktok'
    elif any(x in url_lower for x in ['youtube.com', 'youtu.be']):
        return 'youtube'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif any(x in url_lower for x in ['facebook.com', 'fb.watch', 'fb.com']):
        return 'facebook'
    return 'unknown'


def is_mp4_supported_url(url):
    """Cek apakah URL dari platform yang didukung."""
    if not url:
        return False
    return any(p in url for p in MP4_SUPPORTED_PLATFORMS)


def download_video_ytdlp(url, out_mp4, quality='best', progress_cb=None):
    """
    Download video MP4 via yt-dlp.
    Mendukung YouTube, Instagram, Facebook, TikTok.

    quality: 'best' | 'hd' | 'sd'
    - best : bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best
    - hd   : bestvideo[height<=1080][ext=mp4]+bestaudio/best[height<=1080]
    - sd   : bestvideo[height<=480][ext=mp4]+bestaudio/best[height<=480]
    """
    def emit(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)
        logger.info(f"[{pct}%] {msg}")

    format_map = {
        'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
        'hd':   'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'sd':   'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]',
    }
    fmt = format_map.get(quality, format_map['best'])

    # Cookies untuk platform yang butuh auth (Instagram, Facebook)
    platform = detect_platform(url)

    ydl_opts = {
        'format':          fmt,
        'outtmpl':         out_mp4,
        'quiet':           True,
        'no_warnings':     True,
        'noplaylist':      True,
        'merge_output_format': 'mp4',
        'user_agent':      TIKTOK_UA,
        'http_headers':    DEFAULT_HEADERS,
        'retries':         3,
        'fragment_retries': 3,
        'socket_timeout':  30,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }

    # Tambahan opsi platform-spesifik
    if platform == 'youtube':
        ydl_opts['http_headers'] = {
            **DEFAULT_HEADERS,
            'Referer': 'https://www.youtube.com/',
        }
    elif platform == 'instagram':
        ydl_opts['http_headers'] = {
            **DEFAULT_HEADERS,
            'Referer': 'https://www.instagram.com/',
        }
    elif platform in ('facebook', 'fb'):
        ydl_opts['http_headers'] = {
            **DEFAULT_HEADERS,
            'Referer': 'https://www.facebook.com/',
        }

    emit(20, f"[DL] Download {platform.upper()} via yt-dlp ({quality})...")
    logger.info(f"[DL] yt-dlp format: {fmt} | URL: {url[:80]}...")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_title = info.get('title', 'video') if info else 'video'

    emit(85, "[OK] Download selesai, siapkan file...")
    return final_title


@app.route('/api/download_mp4', methods=['GET', 'POST'])
def download_mp4_api():
    """
    Download MP4 dari berbagai platform: YouTube, TikTok, Instagram, Facebook.

    Parameter:
      - url     : URL video
      - quality : 'best' | 'hd' | 'sd' (default: 'best')
      - title   : judul override (opsional)

    Flow:
      TikTok  -> coba TikWM (HD) dulu, fallback ke yt-dlp
      Lainnya -> yt-dlp langsung, merge ke MP4
    """
    import tempfile

    if request.method == 'POST':
        data    = request.get_json(force=True) or {}
        url     = data.get('url', '').strip()
        quality = data.get('quality', 'best').strip()
        title   = data.get('title', '').strip()
    else:
        url     = request.args.get('url', '').strip()
        quality = request.args.get('quality', 'best').strip()
        title   = request.args.get('title', '').strip()

    kirim_notif(f"User download MP4: {url}")
    logger.info(f"[MP4] Request: {url[:80]} | quality={quality}")

    if not url:
        return jsonify({"status": "error", "msg": "URL kosong"}), 400

    if not is_mp4_supported_url(url):
        return jsonify({
            "status": "error",
            "msg": "Platform tidak didukung. Didukung: TikTok, YouTube, Instagram, Facebook."
        }), 400

    platform = detect_platform(url)
    uid      = str(int(time.time() * 1000))
    out_mp4  = f'/tmp/vinder_mp4_{uid}.mp4'

    try:
        final_title = title or 'video'

        # ── TikTok: coba TikWM dulu (lebih cepat, HD tanpa watermark) ──
        if platform == 'tiktok':
            resolved = url
            if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                resolved = resolve_tiktok_url(url)

            logger.info(f"[MP4] TikTok: coba TikWM HD...")
            video_url, cover_url, tikwm_title = get_meta_via_tikwm(resolved, for_audio=False)

            if video_url:
                final_title = tikwm_title or final_title
                logger.info(f"[MP4] TikWM HD OK, stream langsung...")
                r, _ = fetch_video_stream(video_url)
                if r and r.status_code < 400:
                    fname        = f"[Vinder].{safe_filename(final_title)}.mp4"
                    content_type = r.headers.get('Content-Type', 'video/mp4')

                    def stream_tiktok():
                        try:
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    yield chunk
                        finally:
                            pass

                    return Response(
                        stream_with_context(stream_tiktok()),
                        headers={
                            'Content-Type':        content_type,
                            'Content-Disposition': make_content_disposition(fname),
                            'Cache-Control':       'no-cache',
                        }
                    )
                logger.warning("[MP4] TikWM stream gagal, fallback yt-dlp...")
            else:
                logger.warning("[MP4] TikWM tidak dapat URL, fallback yt-dlp...")

        # ── Semua platform (termasuk TikTok fallback): yt-dlp download ──
        logger.info(f"[MP4] yt-dlp download: {platform.upper()} | quality={quality}")
        final_title = download_video_ytdlp(url, out_mp4, quality=quality)
        final_title = title or final_title

        if not os.path.exists(out_mp4):
            # Cari file hasil yt-dlp (mungkin ada ekstensi berbeda)
            import glob
            candidates = sorted(glob.glob(f'/tmp/vinder_mp4_{uid}*'), key=os.path.getsize, reverse=True)
            if candidates:
                os.replace(candidates[0], out_mp4)
            else:
                return jsonify({"status": "error", "msg": "File video tidak berhasil dibuat"}), 500

        file_size = os.path.getsize(out_mp4)
        fname     = f"[Vinder].{safe_filename(final_title)}.mp4"
        logger.info(f"[OK] MP4 siap: {fname} ({file_size // 1024 // 1024}MB)")

        def generate_mp4():
            try:
                with open(out_mp4, 'rb') as f:
                    while True:
                        chunk = f.read(1024 * 1024)
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
                'Content-Disposition': make_content_disposition(fname),
                'Content-Length':      str(file_size),
                'Cache-Control':       'no-cache',
            }
        )

    except Exception as e:
        logger.error(f"[ERR] download_mp4 error: {e}")
        # Cleanup kalau ada file temp
        try:
            if os.path.exists(out_mp4):
                os.remove(out_mp4)
        except Exception:
            pass
        return jsonify({"status": "error", "msg": str(e)}), 500


@app.route('/api/mp4_info', methods=['GET', 'POST'])
def mp4_info_api():
    """
    Ambil info/metadata video sebelum download MP4.
    Return: title, thumbnail, duration, platform, available_qualities.

    Beda dengan /api/download_url - ini spesifik untuk preview sebelum MP4 download,
    dan juga return list kualitas yang tersedia.
    """
    if request.method == 'POST':
        data = request.get_json(force=True) or {}
        url  = data.get('url', '').strip()
    else:
        url = request.args.get('url', '').strip()

    if not url:
        return jsonify({"status": "error", "msg": "URL kosong"}), 400

    if not is_mp4_supported_url(url):
        return jsonify({
            "status": "error",
            "msg": "Platform tidak didukung. Didukung: TikTok, YouTube, Instagram, Facebook."
        }), 400

    platform = detect_platform(url)
    logger.info(f"[INFO] mp4_info request: {platform.upper()} | {url[:80]}")

    try:
        # TikTok: pakai TikWM untuk info cepat
        if platform == 'tiktok':
            resolved = url
            if 'vt.tiktok.com' in url or 'vm.tiktok.com' in url:
                resolved = resolve_tiktok_url(url)
            video_url, cover_url, tikwm_title = get_meta_via_tikwm(resolved, for_audio=False)
            if video_url:
                return jsonify({
                    "status":    "success",
                    "platform":  "tiktok",
                    "title":     tikwm_title or "TikTok Video",
                    "cover":     cover_url,
                    "qualities": ["best"],
                    "note":      "TikTok: HD tanpa watermark via TikWM"
                })

        # Platform lain: pakai yt-dlp extract_info (no download)
        ydl_opts = {
            'quiet':       True,
            'no_warnings': True,
            'noplaylist':  True,
            'user_agent':  TIKTOK_UA,
            'http_headers': DEFAULT_HEADERS,
        }
        # Facebook share/redirect URL — follow redirect ke URL asli dulu
        if platform == 'facebook' and any(x in url for x in ['share/r/', 'share/v/', 'fb.watch', 'v.facebook.com']):
            try:
                import urllib.request as _req
                _r = _req.urlopen(
                    _req.Request(url, headers={'User-Agent': TIKTOK_UA}),
                    timeout=8
                )
                resolved_fb_url = _r.url
                if resolved_fb_url and 'facebook.com' in resolved_fb_url and resolved_fb_url != url:
                    logger.info(f"[INFO] Facebook redirect resolved: {url[:60]} → {resolved_fb_url[:60]}")
                    url = resolved_fb_url
            except Exception as _e:
                logger.warning(f"[WARN] Facebook redirect resolve gagal: {_e}, lanjut dengan URL asli")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        # Kumpulkan kualitas unik yang tersedia
        formats   = info.get('formats') or []
        heights   = sorted(set(
            f.get('height') for f in formats
            if f.get('height') and f.get('vcodec') not in (None, 'none')
        ), reverse=True)
        qualities = []
        if any(h >= 1080 for h in heights):
            qualities.append('hd')
        if any(h <= 480 for h in heights):
            qualities.append('sd')
        if not qualities:
            qualities = ['best']
        else:
            qualities = ['best'] + qualities

        # ── Durasi ──
        # Facebook sering return duration=None di info root, cari di formats
        duration_s = int(info.get('duration') or 0)
        if not duration_s and formats:
            for fmt in formats:
                d = fmt.get('duration')
                if d and int(d) > 0:
                    duration_s = int(d)
                    break
        # Fallback: parse dari duration_string jika ada (format "H:MM:SS" atau "M:SS")
        if not duration_s:
            dur_str_raw = info.get('duration_string') or ''
            if dur_str_raw:
                try:
                    parts = dur_str_raw.strip().split(':')
                    if len(parts) == 2:
                        duration_s = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        duration_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except Exception:
                    pass
        # Fallback: cek dari entries (playlist/single entry)
        if not duration_s:
            entries = info.get('entries') or []
            if entries:
                duration_s = int(entries[0].get('duration') or 0)
        if duration_s > 0:
            m, s = divmod(duration_s, 60)
            duration_str = f"{m}m{s:02d}s"
        else:
            duration_str = '-'

        # ── Ukuran file ──
        size_bytes = 0
        try:
            # Lapis 1: filesize/filesize_approx dari format video
            for fmt in reversed(formats):
                fs = fmt.get('filesize') or fmt.get('filesize_approx') or 0
                if fs and fmt.get('vcodec') not in (None, 'none'):
                    size_bytes = fs
                    break
            # Lapis 2: video + audio terpisah
            if not size_bytes:
                v_size = next((
                    f.get('filesize') or f.get('filesize_approx') or 0
                    for f in reversed(formats)
                    if f.get('vcodec') not in (None, 'none')
                ), 0)
                a_size = next((
                    f.get('filesize') or f.get('filesize_approx') or 0
                    for f in reversed(formats)
                    if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')
                ), 0)
                size_bytes = (v_size or 0) + (a_size or 0)
            # Lapis 3: info root
            if not size_bytes:
                size_bytes = info.get('filesize_approx') or info.get('filesize') or 0
            # Lapis 4 (Facebook fallback): estimasi dari tbr × duration
            # tbr = total bitrate dalam kbps
            if not size_bytes and duration_s > 0:
                tbr = info.get('tbr') or 0
                if not tbr and formats:
                    tbr = next((f.get('tbr') or 0 for f in reversed(formats) if f.get('tbr')), 0)
                # Fallback: vbr + abr kalau tbr tetap 0
                if not tbr and formats:
                    vbr = next((f.get('vbr') or 0 for f in reversed(formats) if f.get('vbr') and f.get('vcodec') not in (None, 'none')), 0)
                    abr = next((f.get('abr') or 0 for f in reversed(formats) if f.get('abr') and f.get('acodec') not in (None, 'none')), 0)
                    tbr = (vbr or 0) + (abr or 0)
                if tbr:
                    size_bytes = int(tbr * 1000 / 8 * duration_s)
        except Exception:
            size_bytes = 0

        if size_bytes >= 1024 * 1024:
            size_str = f"{size_bytes / 1024 / 1024:.1f}MB"
        elif size_bytes > 0:
            size_str = f"{size_bytes // 1024}KB"
        else:
            size_str = 'N/A'

        # ── Author — hindari hostname sebagai nama ──
        import re as _re
        raw_author = (
            info.get('uploader') or
            info.get('channel') or
            info.get('creator') or
            info.get('uploader_url') and None or  # skip uploader_url, itu URL bukan nama
            info.get('page_name') or              # Facebook page name (kadang ada)
            info.get('uploader_id') or
            'Unknown'
        )
        # Hanya replace kalau string DIMULAI dengan http/www,
        # atau seluruhnya berupa angka (uploader_id numerik FB)
        is_url_like = _re.match(r'^(https?://|www\.)', raw_author or '')
        is_numeric  = _re.match(r'^\d+$', raw_author or '')
        if is_url_like or is_numeric:
            platform_names = {
                'youtube': 'YouTube', 'instagram': 'Instagram',
                'facebook': 'Facebook', 'tiktok': 'TikTok'
            }
            raw_author = platform_names.get(platform, platform.title())

        # ── Thumbnail — pilih yang paling besar / berkualitas ──
        thumbnails = info.get('thumbnails') or []
        best_thumb = info.get('thumbnail')
        if thumbnails:
            # Pilih thumbnail dengan resolusi tertinggi
            def thumb_score(t):
                return (t.get('width') or 0) * (t.get('height') or 0)
            best = max(thumbnails, key=thumb_score, default=None)
            if best and best.get('url'):
                best_thumb = best['url']

        return jsonify({
            "status":      "success",
            "platform":    platform,
            "title":       info.get('title', 'Video'),
            "cover":       best_thumb,
            "author":      raw_author,
            "duration":    duration_str,
            "size":        size_str,
            "qualities":   qualities,
            "resolutions": [f"{h}p" for h in heights[:5]],
        })

    except Exception as e:
        logger.error(f"[ERR] mp4_info error: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    kirim_notif("Sistem Vinder Berhasil ON di Railway!")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)