import os
import random
import requests
import logging
from datetime import datetime

# =============================================================================
# SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('health.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIG — set via environment variable (sama seperti Vinder)
# =============================================================================

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
SAMPLE_URL       = "https://vt.tiktok.com/ZS9GBdy9y/"

# =============================================================================
# PESAN SUKSES ACAK
# =============================================================================

PESAN_SUKSES = [
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

# =============================================================================
# KIRIM NOTIF TELEGRAM
# =============================================================================

def kirim_notif(pesan):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[NOTIF] Token/Chat ID tidak ditemukan di env.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": pesan},
            timeout=10
        )
        logger.info("[NOTIF] Pesan terkirim ke Telegram.")
    except Exception as e:
        logger.warning(f"[NOTIF] Gagal kirim Telegram: {e}")

# =============================================================================
# ANALISIS ERROR VIA GROQ
# =============================================================================

def analisis_error_groq(error_detail):
    if not GROQ_API_KEY:
        logger.warning("[GROQ] GROQ_API_KEY tidak ditemukan.")
        return "Analisis tidak tersedia (API key tidak ada)."
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
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
        logger.warning(f"[GROQ] Analisis gagal: {e}")
        return "Analisis Groq tidak tersedia saat ini."

# =============================================================================
# CORE HEALTH CHECK — logika sama seperti Vinder
# =============================================================================

def run_health_check():
    logger.info(f"[CHECK] Mulai health check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    error_detail = None

    try:
        # Step 1: Hit TikWM API (sama persis dengan get_meta_via_tikwm di Vinder)
        resp = requests.get(
            f"https://www.tikwm.com/api/?url={SAMPLE_URL}",
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()

        # Step 2: Validasi response — bukan cuma status 200
        if data.get("code") != 0:
            error_detail = (
                f"TikWM return code={data.get('code')}, msg={data.get('msg')}.\n"
                f"Raw response: {str(data)[:300]}"
            )
        else:
            v        = data.get("data", {})
            play_url = v.get("play") or v.get("hdplay")
            size     = v.get("size", 0)

            # Step 3: Validasi link download muncul & size > 0
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

    # =============================================================================
    # KIRIM HASIL KE TELEGRAM
    # =============================================================================

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")

    if error_detail:
        logger.error(f"[CHECK] GAGAL — {error_detail}")
        analisis = analisis_error_groq(error_detail)
        pesan = (
            f"❌ Vinder Health Check GAGAL!\n"
            f"🕒 {now_str}\n\n"
            f"📋 Error:\n{error_detail}\n\n"
            f"🤖 Analisis AI:\n{analisis}"
        )
        kirim_notif(pesan)
    else:
        logger.info("[CHECK] PASSED — semua sistem normal.")
        pesan_acak = random.choice(PESAN_SUKSES)
        pesan = f"{pesan_acak}\n🕒 {now_str}"
        kirim_notif(pesan)

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    run_health_check()
