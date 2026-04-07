"""
TikTok uploader via Playwright (sync API for Celery compatibility).

Features:
- Sync Playwright (works inside Celery workers)
- Stealth: randomized user-agent, viewport, human-like delays
- Screenshot on failure for debugging
- Rate limiting (configurable max per day, delay between uploads)
- Session health check before upload
"""
from playwright.sync_api import sync_playwright, Page
import json
import time
import random
import logging
from pathlib import Path
from datetime import datetime, date

from config import settings

logger = logging.getLogger(__name__)

# Track daily upload count (reset on new day)
_upload_tracker = {"date": None, "count": 0}

# Rotating user agents for stealth
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

# TikTok selectors — configurable so they can be updated without code changes
SELECTORS = {
    "file_input": "input[type='file']",
    "caption_editor": "[data-text='true']",
    "post_button": "button:has-text('Post'), button:has-text('Posting')",
    "upload_progress": ".upload-progress, [class*='progress'], [class*='uploading']",
    "success_indicator": "**/creator#/content",
}


def save_session():
    """Run once manually to login and save TikTok cookies.
    
    Usage: python -c "from uploader import save_session; save_session()"
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # Visible for manual login
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://www.tiktok.com/login")
        print("Login manually in the browser, then press Enter...")
        input()
        cookies = ctx.cookies()
        settings.COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(settings.COOKIES_PATH, "w") as f:
            json.dump(cookies, f)
        browser.close()
        print(f"Session saved to {settings.COOKIES_PATH}")


def check_session_valid() -> bool:
    """Quick check if saved cookies are still valid."""
    if not settings.COOKIES_PATH.exists():
        logger.warning("No TikTok session file found")
        return False

    with open(settings.COOKIES_PATH) as f:
        cookies = json.load(f)

    # Check if session cookies exist and haven't expired
    now = time.time()
    session_cookies = [c for c in cookies if "tiktok" in c.get("domain", "")]
    if not session_cookies:
        return False

    # Check expiry of critical cookies
    for cookie in session_cookies:
        if cookie.get("name") in ("sessionid", "sid_tt", "sessionid_ss"):
            expires = cookie.get("expires", 0)
            if expires > 0 and expires < now:
                logger.warning(f"TikTok cookie '{cookie['name']}' expired")
                return False

    return True


def check_rate_limit() -> bool:
    """Check if we've hit the daily upload limit."""
    today = date.today()
    if _upload_tracker["date"] != today:
        _upload_tracker["date"] = today
        _upload_tracker["count"] = 0

    return _upload_tracker["count"] < settings.TIKTOK_MAX_UPLOADS_PER_DAY


def _take_screenshot(page: Page, name: str):
    """Save a screenshot for debugging."""
    from storage import get_screenshots_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = get_screenshots_dir() / f"{name}_{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info(f"Screenshot saved: {path}")
    except Exception as e:
        logger.warning(f"Failed to save screenshot: {e}")


def _human_delay(min_s: float = 0.5, max_s: float = 2.0):
    """Random delay to simulate human behavior."""
    time.sleep(random.uniform(min_s, max_s))


def upload_clip(video_path: str, caption: str, retries: int = 3) -> bool:
    """Upload one clip to TikTok via sync Playwright.
    
    ✅ FIXED: Uses sync_playwright instead of async_playwright for Celery compatibility.
    """
    # Pre-flight checks
    if not check_rate_limit():
        raise RuntimeError(
            f"Daily upload limit reached ({settings.TIKTOK_MAX_UPLOADS_PER_DAY}/day). "
            "Will retry tomorrow."
        )

    if not check_session_valid():
        raise RuntimeError(
            "TikTok session expired or missing. "
            "Run: python -c \"from uploader import save_session; save_session()\""
        )

    if not Path(video_path).exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",  # Stealth
            ],
        )

        # Randomize viewport and user agent for stealth
        ua = random.choice(USER_AGENTS)
        ctx = browser.new_context(
            viewport={
                "width": random.randint(1280, 1440),
                "height": random.randint(800, 900),
            },
            user_agent=ua,
        )

        # Load saved session cookies
        with open(settings.COOKIES_PATH) as f:
            ctx.add_cookies(json.load(f))

        page = ctx.new_page()

        for attempt in range(retries):
            try:
                logger.info(f"Upload attempt {attempt + 1}/{retries}: {Path(video_path).name}")

                # Navigate to TikTok upload page
                page.goto(settings.TIKTOK_UPLOAD_URL, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=20000)
                _human_delay(1, 3)

                # Upload file via hidden input
                upload_input = page.locator(SELECTORS["file_input"])
                upload_input.set_input_files(video_path)

                # Wait for TikTok to process the video
                page.wait_for_selector(
                    SELECTORS["upload_progress"],
                    state="hidden",
                    timeout=120000,
                )
                _human_delay(2, 4)

                # Fill caption with human-like typing
                caption_box = page.locator(SELECTORS["caption_editor"]).first
                caption_box.click()
                _human_delay(0.5, 1)
                caption_box.fill("")
                _human_delay(0.3, 0.8)

                # Type caption character by character (human simulation)
                for char in caption:
                    page.keyboard.type(char, delay=random.randint(30, 80))
                    if random.random() < 0.05:  # 5% chance of small pause
                        _human_delay(0.3, 0.8)

                # Wait before clicking post (human behavior)
                _human_delay(2, 5)

                # Click Post button
                post_btn = page.locator(SELECTORS["post_button"]).first
                post_btn.click()

                # Wait for success redirect
                page.wait_for_url(SELECTORS["success_indicator"], timeout=30000)

                # Update daily counter
                _upload_tracker["count"] += 1
                logger.info(f"Upload success! Daily count: {_upload_tracker['count']}")

                browser.close()
                return True

            except Exception as e:
                logger.warning(f"Upload attempt {attempt + 1} failed: {e}")
                _take_screenshot(page, f"upload_fail_attempt{attempt + 1}")

                if attempt == retries - 1:
                    browser.close()
                    raise RuntimeError(
                        f"Upload failed after {retries} attempts: {e}"
                    )

                # Exponential backoff
                backoff = 10 * (attempt + 1) + random.randint(1, 10)
                logger.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

        browser.close()
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Celery task wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from tasks import app as celery_app


@celery_app.task(bind=True, max_retries=2, default_retry_delay=600)
def upload_to_tiktok(self, clip_id: str, video_path: str, caption: str):
    """Celery task: upload a single clip to TikTok."""
    from database import SessionLocal
    from models import Clip, ClipStatus

    db = SessionLocal()
    clip = db.query(Clip).filter_by(id=clip_id).first()

    if not clip:
        db.close()
        logger.error(f"Clip {clip_id} not found")
        return

    try:
        clip.status = ClipStatus.uploading
        db.commit()

        # Add delay between uploads (rate limiting)
        delay = random.randint(
            settings.TIKTOK_DELAY_BETWEEN_UPLOADS_MIN,
            settings.TIKTOK_DELAY_BETWEEN_UPLOADS_MAX,
        )
        logger.info(f"Waiting {delay}s before upload (rate limiting)...")
        time.sleep(delay)

        upload_clip(video_path, caption)

        clip.status = ClipStatus.done
        clip.uploaded_at = datetime.utcnow()
        db.commit()

        logger.info(f"Clip {clip_id[:8]} uploaded successfully")

    except Exception as e:
        clip.status = ClipStatus.failed
        clip.error = str(e)[:2000]
        db.commit()
        logger.error(f"Clip {clip_id[:8]} upload failed: {e}")
        raise self.retry(exc=e)
    finally:
        db.close()


if __name__ == "__main__":
    save_session()