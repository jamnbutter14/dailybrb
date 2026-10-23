"""Daily Brand Reputation Boost - runs every morning:
1) builds today's image  2) posts it to X  3) posts to the Facebook Page
4) sends the finished message to Telegram so you can paste it into the Facebook Group.
"""
import base64
import io
import json
import os
import random
import sys
import traceback
from datetime import datetime, timedelta, timezone

import requests
from requests_oauthlib import OAuth1

from compose import build_image

HERE = os.path.dirname(os.path.abspath(__file__))
VN_TZ = timezone(timedelta(hours=7))
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
DATE_FORMAT = os.environ.get("DATE_FORMAT", "%y%m%d")        # 261023 style. Use "%d/%m/%Y" for 23/10/2026

X_CAPTION = """{date} | DAILY BRAND REPUTATION BOOST FOR JAMES 🦅

🎯: 350 REPLIES & 200 LIKES

• Use pictures, videos or gifs to prevent spam detection.
• Do not use emojis.
• Please use different compliments in English or Korean, and include hashtags.

2️⃣ DAILY SEARCHING BOOST "코르티스 제임스" & "JAMES CORTIS"

🔗 Google: google.com
🔗 Youtube: youtube.com
🔗 Naver: m.naver.com
🔗 Daum: m.daum.net

#JAMES #제임스"""

FB_MESSAGE = """🐈 NHIỆM VỤ HỆ THỐNG VUỐT MÈO {date} 🐈

🍮 BRB (350 replies)

🔗: {x_url}

🔗 Mẫu câu làm BRB: https://v0-ofjvn-trending-party-website.vercel.app/

{fancam_block}"""


def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        raise RuntimeError(f"Missing setting: {name}")
    return v


# ---------------------------------------------------------------- Google
def google_services():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly",
                      "https://www.googleapis.com/auth/spreadsheets.readonly"])
    return (build("drive", "v3", credentials=creds, cache_discovery=False),
            build("sheets", "v4", credentials=creds, cache_discovery=False))


def pick_photo(drive, today, dest_dir):
    """Goes through every photo in the pool once (in a fixed shuffled order) before repeating."""
    local = env("PHOTO_DIR", required=False)
    if local:                                           # testing on your own computer
        files = sorted(f for f in os.listdir(local) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
        names = [(os.path.join(local, f), f) for f in files]
        rnd = random.Random(2026); rnd.shuffle(names)
        path, name = names[today.toordinal() % len(names)]
        return path, name
    folder = env("DRIVE_FOLDER_ID")
    items, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{folder}' in parents and mimeType contains 'image/' and trashed=false",
            fields="nextPageToken, files(id, name)", pageSize=1000, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        items += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    if not items:
        raise RuntimeError("The Photo Pool folder is empty, or it was not shared with the service account email.")
    items.sort(key=lambda f: f["id"])
    random.Random(2026).shuffle(items)
    chosen = items[today.toordinal() % len(items)]
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=chosen["id"], supportsAllDrives=True))
    done = False
    while not done:
        _, done = dl.next_chunk()
    path = os.path.join(dest_dir, "photo_of_the_day")
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path, chosen["name"]


def parse_date(text):
    for fmt in ("%Y-%m-%d", "%y%m%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def get_fancams(sheets, today):
    """Returns (link1, link2) for today from the Google Sheet, or None."""
    if env("FANCAM_1", required=False):
        return env("FANCAM_1"), env("FANCAM_2", required=False)
    sheet_id = env("SHEET_ID", required=False)
    if not sheet_id:
        return None
    rng = env("SHEET_RANGE", required=False) or "A:C"
    rows = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    for row in rows:
        if row and parse_date(str(row[0])) == today:
            f1 = row[1].strip() if len(row) > 1 else ""
            f2 = row[2].strip() if len(row) > 2 else ""
            if f1 or f2:
                return f1, f2
    return None


# ---------------------------------------------------------------- X
def x_auth():
    return OAuth1(env("X_API_KEY"), env("X_API_SECRET"), env("X_ACCESS_TOKEN"), env("X_ACCESS_TOKEN_SECRET"))


def x_upload(image_path, auth):
    data = open(image_path, "rb").read()
    errors = []
    # Current endpoint
    r = requests.post("https://api.x.com/2/media/upload", auth=auth, timeout=60, json={
        "media": base64.b64encode(data).decode(), "media_category": "tweet_image", "media_type": "image/jpeg"})
    if r.ok:
        return str(r.json()["data"]["id"])
    errors.append(f"v2 upload: {r.status_code} {r.text[:300]}")
    # Older endpoint, kept as a fallback
    r = requests.post("https://upload.x.com/1.1/media/upload.json", auth=auth, timeout=60,
                      files={"media": data}, data={"media_category": "tweet_image"})
    if r.ok:
        return r.json()["media_id_string"]
    errors.append(f"v1.1 upload: {r.status_code} {r.text[:300]}")
    raise RuntimeError("X image upload failed. " + " | ".join(errors))


def x_post(text, image_path):
    auth = x_auth()
    media_id = x_upload(image_path, auth)
    r = requests.post("https://api.x.com/2/tweets", auth=auth, timeout=60,
                      json={"text": text, "media": {"media_ids": [media_id]}})
    if not r.ok:
        raise RuntimeError(f"X post failed: {r.status_code} {r.text[:400]}")
    tweet_id = r.json()["data"]["id"]
    user = env("X_USERNAME", required=False).lstrip("@")
    return f"https://x.com/{user}/status/{tweet_id}" if user else f"https://x.com/i/status/{tweet_id}"


# ---------------------------------------------------------------- Facebook / Telegram
def fb_post(message):
    ver = env("FB_GRAPH_VERSION", required=False)
    base = f"https://graph.facebook.com/{ver + '/' if ver else ''}{env('FB_PAGE_ID')}/feed"
    r = requests.post(base, data={"message": message, "access_token": env("FB_PAGE_TOKEN")}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Facebook post failed: {r.status_code} {r.text[:400]}")
    return r.json().get("id")


def telegram(text):
    token, chat = env("TELEGRAM_BOT_TOKEN", required=False), env("TELEGRAM_CHAT_ID", required=False)
    if not token or not chat:
        print("(Telegram not set up - skipping)")
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}, timeout=30)
    if not r.ok:
        print("Telegram error:", r.text[:200])


# ---------------------------------------------------------------- main
def main():
    today = datetime.now(VN_TZ).date()
    date_text = today.strftime(DATE_FORMAT)
    print(f"Date: {date_text}   DRY_RUN={DRY_RUN}")
    warnings = []

    work = os.path.join(HERE, "work")
    os.makedirs(work, exist_ok=True)

    drive = sheets = None
    if not env("PHOTO_DIR", required=False) or env("SHEET_ID", required=False):
        drive, sheets = google_services()

    photo_path, photo_name = pick_photo(drive, today, work)
    print("Photo of the day:", photo_name)

    image_path = os.path.join(work, f"boost_{today:%Y%m%d}.jpg")
    build_image(os.path.join(HERE, "template.png"), photo_path, date_text, image_path)
    print("Image ready:", image_path)

    caption = X_CAPTION.format(date=date_text)
    print(f"X caption length: {len(caption)} characters (non-Premium X accounts are limited to 280)")

    fancams = get_fancams(sheets, today)
    if fancams:
        f1, f2 = fancams
        links = [l for l in (f1, f2) if l]
        block = "🍮 STREAM FANCAM\n\n" + "\n\n".join(f"🔗: {l}" for l in links)
    else:
        block = ""
        warnings.append(f"No fancam links found in the Google Sheet for {date_text} - the fancam section was left out.")

    if DRY_RUN:
        x_url = "https://x.com/i/status/DRY_RUN_EXAMPLE"
        print("\n--- X CAPTION ---\n" + caption)
    else:
        x_url = x_post(caption, image_path)
        print("Posted to X:", x_url)

    fb_text = FB_MESSAGE.format(date=date_text, x_url=x_url, fancam_block=block).rstrip() + "\n"
    print("\n--- FACEBOOK MESSAGE ---\n" + fb_text)

    if DRY_RUN:
        print("Dry run: nothing was posted.")
    else:
        post_id = fb_post(fb_text)
        print("Posted to Facebook Page:", post_id)

    note = "\n\n" + "\n".join("WARNING: " + w for w in warnings) if warnings else ""
    telegram(("[TEST] " if DRY_RUN else "") + "Paste this into the Facebook Group:\n\n" + fb_text + note)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        telegram(f"Daily Boost FAILED: {e}")
        sys.exit(1)
