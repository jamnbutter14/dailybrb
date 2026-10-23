"""Builds the daily image: puts a photo into the template's green frame and the date into the pink box."""
import os
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(HERE, "fonts", "InstrumentSerif-Regular.ttf")
DATE_FONT_SIZE = 66        # tuned so it looks like Canva's "53" on the 1200px-wide export
DATE_COLOR = None          # None = copy the dark colour used by the "#JAMES" text


def _marker_mask(arr, kind):
    r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
    if kind == "green":
        m = (g > 150) & (g - r > 90) & (g - b > 90)
    else:  # magenta
        m = (r > 150) & (b > 150) & (r - g > 90) & (b - g > 90)
    return m.astype(np.uint8) * 255


def _largest_rect(mask):
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    return cv2.minAreaRect(cv2.convexHull(c))


def _face_center(img_rgb):
    """Return (cx, cy) of the largest face in pixels, or None."""
    try:
        gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
        scale = 800 / max(gray.shape)
        small = cv2.resize(gray, None, fx=scale, fy=scale)
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = casc.detectMultiScale(small, 1.1, 5, minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return (x + w / 2) / scale, (y + h / 2) / scale
    except Exception:
        return None


def _cover_crop(photo, target_w, target_h):
    """Scale + crop photo to exactly target size, keeping the face in view."""
    photo = ImageOps.exif_transpose(photo).convert("RGB")
    arr = np.array(photo)
    pw, ph = photo.size
    scale = max(target_w / pw, target_h / ph)
    nw, nh = int(round(pw * scale)), int(round(ph * scale))
    resized = photo.resize((nw, nh), Image.LANCZOS)
    face = _face_center(arr)
    if face:
        cx, cy = face[0] * scale, face[1] * scale
    else:
        cx, cy = nw / 2, nh * 0.35      # no face found: favour the upper part
    left = int(min(max(cx - target_w / 2, 0), nw - target_w))
    top = int(min(max(cy - target_h / 2, 0), nh - target_h))
    return resized.crop((left, top, left + target_w, top + target_h))


def build_image(template_path, photo_path, date_text, out_path):
    tpl = Image.open(template_path).convert("RGBA")
    arr = np.array(tpl)

    green = _marker_mask(arr, "green")
    pink = _marker_mask(arr, "magenta")

    # ---- photo ----
    (cx, cy), (rw, rh), angle = _largest_rect(green)
    rw, rh = int(round(rw)) + 6, int(round(rh)) + 6        # small overscan
    crop = _cover_crop(Image.open(photo_path), rw, rh)
    crop_arr = np.array(crop)
    # tilt the crop to match the frame
    src = np.float32([[0, 0], [rw, 0], [rw, rh], [0, rh]])
    box = cv2.boxPoints(((cx, cy), (rw, rh), angle))       # 4 corners, any order
    # order box corners: tl, tr, br, bl
    s = box.sum(axis=1); d = np.diff(box, axis=1).ravel()
    tl, br = box[np.argmin(s)], box[np.argmax(s)]
    tr, bl = box[np.argmin(d)], box[np.argmax(d)]
    dst = np.float32([tl, tr, br, bl])
    H = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(crop_arr, H, (tpl.width, tpl.height), flags=cv2.INTER_LANCZOS4)

    mask = cv2.dilate(green, np.ones((3, 3), np.uint8), iterations=1)   # swallow green fringe
    mask = cv2.GaussianBlur(mask, (3, 3), 0).astype(float)[..., None] / 255.0
    out = arr[..., :3].astype(float) * (1 - mask) + warped.astype(float) * mask

    # ---- date ----
    pink_d = cv2.dilate(pink, np.ones((3, 3), np.uint8), iterations=1)
    px, py, pw_, ph_ = cv2.boundingRect(pink_d)
    # fill the pink box with the surrounding paper texture
    pink_big = cv2.dilate(pink, np.ones((5, 5), np.uint8), iterations=1)
    filled = cv2.inpaint(out.clip(0, 255).astype(np.uint8), pink_big, 7, cv2.INPAINT_TELEA)
    out = filled.astype(float)

    result = Image.fromarray(out.clip(0, 255).astype(np.uint8)).convert("RGBA")

    color = DATE_COLOR
    if color is None:
        # darkest pixels in the "#JAMES" word area
        zone = arr[400:470, 560:810, :3].reshape(-1, 3)
        color = tuple(int(v) for v in zone[np.argmin(zone.sum(axis=1))])
    font = ImageFont.truetype(FONT_PATH, DATE_FONT_SIZE)
    draw = ImageDraw.Draw(result)
    l, t, r, b = draw.textbbox((0, 0), date_text, font=font)
    tx = px + (pw_ - (r - l)) / 2 - l
    ty = py + (ph_ - (b - t)) / 2 - t
    draw.text((tx, ty), date_text, font=font, fill=color + (255,))

    result.convert("RGB").save(out_path, quality=95)
    return out_path


if __name__ == "__main__":
    import sys
    build_image("template.png", sys.argv[1], sys.argv[2], sys.argv[3])
