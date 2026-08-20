"""images 阶段：下载条目自带的现成图片，Pillow 重编码消毒后本地化。

安全边界：只信任重编码后的像素数据。Content-Type 必须是 image/*，
大小限制 image_max_bytes，重编码剥离 EXIF/ICC/附加数据段，
文件名取 sha256(url) 前 16 位，页面不引用任何外链图片。
"""

import hashlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from collect import http_open

BEIJING = timezone(timedelta(hours=8))
JPEG_QUALITY = 80


def download_image(url, max_bytes, use_proxy):
    """下载并校验大小/类型，返回原始字节；不合规抛异常。"""
    with http_open(url, timeout=20, use_proxy=use_proxy, accept="image/*") as resp:
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.lower().startswith("image/"):
            raise ValueError(f"Content-Type 不是图片: {content_type}")
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"图片超过 {max_bytes} 字节上限")
    return data


def sanitize_image(data, max_width):
    """Pillow 重编码：只保留像素，缩放并另存为 JPEG，返回 JPEG 字节。"""
    Image.open(io.BytesIO(data)).verify()  # 结构校验；verify 后需重新 open
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, max(1, round(img.height * max_width / img.width))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def fetch_images(cfg, items, today, log):
    """为有 image_url 的条目下载消毒图片，写入 item['image']（站内相对路径）。"""
    limits = cfg["limits"]
    day_dir = Path(cfg["paths"]["image_dir"]) / today
    day_dir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for item in items:
        url = item.get("image_url", "")
        if not url:
            continue
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16] + ".jpg"
        try:
            raw = download_image(url, limits["image_max_bytes"], item.get("image_proxy", False))
            jpeg = sanitize_image(raw, limits["image_max_width"])
        except Exception as exc:
            log.info("图片跳过（%s）: %s", str(exc)[:120], url[:120])
            continue
        (day_dir / name).write_bytes(jpeg)
        item["image"] = f"/news/images/{today}/{name}"
        saved += 1
    log.info("图片处理完成: %d 张保存到 %s", saved, day_dir)
    return saved


def prune_image_dirs(cfg, log):
    """删除 image_dir 下日期目录名早于保留期的整个子目录。"""
    root = Path(cfg["paths"]["image_dir"])
    if not root.exists():
        return
    cutoff = (datetime.now(BEIJING) - timedelta(days=cfg["retention_days"]["images"])).date()
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        try:
            day = datetime.strptime(sub.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            for child in sub.iterdir():
                child.unlink()
            sub.rmdir()
            log.info("清理过期图片目录: %s", sub)
