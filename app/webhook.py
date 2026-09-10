import json
from pathlib import Path
from typing import Dict, Any, Optional

import aiohttp

from app.config import settings, logger


def build_webhook_payload(recording_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Đóng gói thông tin chi tiết thư mục và các file của session recording
    để gửi qua webhook cho service xử lý hậu kì.
    """
    room_name = recording_data.get("room_name", "")
    session_id = recording_data.get("session_id", "")
    recording_id = recording_data.get("recording_id", "")

    if recording_id:
        folder_prefix = f"{room_name}/{session_id}/{recording_id}"
    else:
        folder_prefix = f"{room_name}/{session_id}"

    return {
        "event": "RECORDING_UPLOADED",
        "room_name": room_name,
        "session_id": session_id,
        "recording_id": recording_id,
        "folder": folder_prefix,
        "duration_sec": recording_data.get("duration_sec", 0.0),
        "r2": {
            "bucket_name": settings.R2_BUCKET_NAME,
            "folder_prefix": folder_prefix,
            "endpoint_url": settings.R2_ENDPOINT_URL,
            "public_base_url": settings.R2_PUBLIC_URL,
            "timeline_key": f"{folder_prefix}/timeline.json",
        },
    }


def build_payload_from_session(
    room_name: str, session_id: str, recording_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Đọc thông tin từ session directory hiện có trên local để tạo payload webhook.
    Dùng cho trường hợp trigger webhook thủ công hoặc gửi lại webhook.
    """
    session_dir = settings.RECORDINGS_DIR / room_name / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return None

    target_dir: Optional[Path] = None
    if recording_id:
        cand_dir = session_dir / recording_id
        if cand_dir.exists() and cand_dir.is_dir():
            target_dir = cand_dir
    else:
        # Tìm thư mục rec_* mới nhất trong session_dir nếu có
        rec_subdirs = [
            d for d in session_dir.iterdir()
            if d.is_dir() and d.name.startswith("rec_")
        ]
        if rec_subdirs:
            rec_subdirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            target_dir = rec_subdirs[0]
            recording_id = target_dir.name
        elif (session_dir / "timeline.json").exists():
            # Tương thích ngược với cấu trúc cũ
            target_dir = session_dir
            recording_id = None

    if target_dir is None or not target_dir.is_dir():
        return None

    timeline_path = target_dir / "timeline.json"
    timeline = {}
    if timeline_path.exists():
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Không thể đọc timeline.json tại %s: %s", timeline_path, e)

    if recording_id:
        folder_prefix = f"{room_name}/{session_id}/{recording_id}"
    else:
        folder_prefix = f"{room_name}/{session_id}"

    # Quét tất cả file trong target_dir
    files = []
    for f in target_dir.iterdir():
        if f.is_file():
            obj_key = f"{folder_prefix}/{f.name}"
            public_url = (
                f"{settings.R2_PUBLIC_URL.rstrip('/')}/{obj_key}"
                if settings.R2_PUBLIC_URL
                else None
            )
            files.append(
                {
                    "filename": f.name,
                    "object_key": obj_key,
                    "size_bytes": f.stat().st_size,
                    "public_url": public_url,
                }
            )

    recording_data = {
        "room_name": room_name,
        "session_id": session_id,
        "recording_id": recording_id or "",
        "duration_sec": timeline.get("duration_sec", 0.0),
        "local_output_dir": str(target_dir),
        "timeline": timeline,
        "uploaded_files": files,
    }
    return build_webhook_payload(recording_data)


async def send_post_process_webhook(
    payload_or_data: Dict[str, Any],
    target_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Gửi webhook HTTP POST tới service hậu kì với dữ liệu chi tiết folder.
    """
    url = (target_url or settings.WEBHOOK_URL or "").strip()
    if not url:
        logger.warning("[Webhook] WEBHOOK_URL chưa được cấu hình, bỏ qua bắn webhook.")
        return {
            "success": False,
            "message": "WEBHOOK_URL chưa được cấu hình.",
            "webhook_url": None,
        }

    # Nếu payload chưa được định dạng chi tiết thì chuẩn hoá
    if "event" not in payload_or_data or "folder" not in payload_or_data:
        payload = build_webhook_payload(payload_or_data)
    else:
        payload = payload_or_data

    logger.info(
        "[Webhook] Đang bắn webhook tới %s cho session '%s' (rec='%s', folder='%s')...",
        url,
        payload.get("session_id"),
        payload.get("recording_id", ""),
        payload.get("folder"),
    )

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as response:
                resp_text = await response.text()
                if 200 <= response.status < 300:
                    logger.info(
                        "[Webhook] Bắn webhook thành công! Status: %d | Resp: %s",
                        response.status,
                        resp_text[:120],
                    )
                    return {
                        "success": True,
                        "status_code": response.status,
                        "webhook_url": url,
                        "folder": payload.get("folder"),
                        "response": resp_text[:500],
                    }
                else:
                    logger.warning(
                        "[Webhook] Server nhận webhook trả về status %d: %s",
                        response.status,
                        resp_text[:200],
                    )
                    return {
                        "success": False,
                        "status_code": response.status,
                        "webhook_url": url,
                        "folder": payload.get("folder"),
                        "error": f"HTTP {response.status}: {resp_text[:200]}",
                    }
    except Exception as e:
        logger.exception("[Webhook] Lỗi ngoại lệ khi gửi webhook tới %s: %s", url, e)
        return {
            "success": False,
            "status_code": None,
            "webhook_url": url,
            "folder": payload.get("folder"),
            "error": str(e),
        }
