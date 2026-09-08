from contextlib import asynccontextmanager
from typing import Dict, Any

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings, logger
from app.recorder import recorder_manager
from app.r2_storage import r2_storage
from app.schemas import (
    StartRecordingRequest,
    StartRecordingResponse,
    StopRecordingRequest,
    StopRecordingResponse,
    ActiveRecordingsResponse,
    RecordingStatusItem,
    R2ConfigStatusResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Quản lý vòng đời ứng dụng FastAPI."""
    logger.info("=== LiveKit Recorder Bot Service đang khởi động ===")
    logger.info("LiveKit URL: %s", settings.LIVEKIT_URL)
    logger.info(
        "R2 Storage status: %s (Bucket: %s)",
        "ĐÃ CẤU HÌNH" if settings.is_r2_configured else "CHƯA CẤU HÌNH",
        settings.R2_BUCKET_NAME if settings.is_r2_configured else "N/A",
    )
    yield
    # Cleanup khi tắt server: Dừng toàn bộ bot đang ghi
    active_rooms = list(recorder_manager._active_bots.keys())
    if active_rooms:
        logger.info("Đang dừng %d bot ghi hình trước khi tắt server...", len(active_rooms))
        for room_name in active_rooms:
            try:
                await recorder_manager.stop_recording(room_name, auto_upload_r2=True)
            except Exception as e:
                logger.error("Lỗi dừng bot phòng %s khi shutdown: %s", room_name, e)
    logger.info("=== LiveKit Recorder Bot Service đã dừng ===")


app = FastAPI(
    title="LiveKit Recorder Bot API",
    description="API điều khiển bot ghi âm / quay màn hình phòng LiveKit và tự động upload file thô lên Cloudflare R2",
    version="1.0.0",
    lifespan=lifespan,
)

# Cho phép CORS cho frontend kết nối
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["General"])
async def root():
    return {
        "service": "LiveKit Recorder Bot API",
        "status": "running",
        "version": "1.0.0",
        "docs_url": "/docs",
    }


@app.get("/health", tags=["General"])
async def health_check():
    return {
        "status": "healthy",
        "r2_configured": settings.is_r2_configured,
        "active_recordings_count": len(recorder_manager._active_bots),
    }


@app.get("/storage/r2/status", response_model=R2ConfigStatusResponse, tags=["Storage"])
async def r2_status():
    """Kiểm tra trạng thái cấu hình của Cloudflare R2."""
    return R2ConfigStatusResponse(
        configured=settings.is_r2_configured,
        bucket_name=settings.R2_BUCKET_NAME if settings.is_r2_configured else None,
        endpoint_url=settings.R2_ENDPOINT_URL if settings.is_r2_configured else None,
        public_url_configured=bool(settings.R2_PUBLIC_URL),
    )


@app.post(
    "/recordings/start",
    response_model=StartRecordingResponse,
    status_code=status.HTTP_200_OK,
    tags=["Recordings"],
)
async def start_recording(request: StartRecordingRequest):
    """
    Bắt đầu ghi hình và âm thanh cho một phòng LiveKit.
    Bot sẽ tham gia phòng dưới dạng subscriber và ghi lại các track audio/screenshare.
    """
    room_name = request.room_name.strip()
    if not room_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tên phòng (room_name) không được để trống.",
        )

    if recorder_manager.is_recording(room_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Phòng '{room_name}' hiện đang được ghi hình.",
        )

    try:
        bot = await recorder_manager.start_recording(
            room_name=room_name,
            session_id=request.session_id,
        )
        return StartRecordingResponse(
            success=True,
            message=f"Đã bắt đầu ghi hình cho phòng '{room_name}'.",
            room_name=bot.room_name,
            session_id=bot.session_id,
            output_dir=str(bot.output_dir),
        )
    except Exception as e:
        logger.exception("Lỗi khi gọi start_recording: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Không thể bắt đầu ghi hình: {str(e)}",
        )


@app.post(
    "/recordings/stop",
    response_model=StopRecordingResponse,
    status_code=status.HTTP_200_OK,
    tags=["Recordings"],
)
async def stop_recording(request: StopRecordingRequest):
    """
    Dừng ghi hình phòng LiveKit, lưu metadata timeline và tự động upload toàn bộ
    file chưa qua xử lý (.wav, .webm, timeline.json) lên Cloudflare R2.
    """
    room_name = request.room_name.strip()
    if not room_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tên phòng (room_name) không được để trống.",
        )

    if not recorder_manager.is_recording(room_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy phiên ghi nào đang hoạt động cho phòng '{room_name}'.",
        )

    try:
        result = await recorder_manager.stop_recording(
            room_name=room_name,
            auto_upload_r2=True,
        )
        return StopRecordingResponse(
            success=True,
            message=f"Đã dừng ghi hình và hoàn thành upload file phòng '{room_name}' lên Cloudflare R2.",
            room_name=result["room_name"],
            session_id=result["session_id"],
            duration_sec=result["duration_sec"],
            local_output_dir=result["local_output_dir"],
            timeline=result["timeline"],
            uploaded_files=result["uploaded_files"],
        )
    except Exception as e:
        logger.exception("Lỗi khi gọi stop_recording: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi khi dừng ghi hình: {str(e)}",
        )


@app.get(
    "/recordings/active",
    response_model=ActiveRecordingsResponse,
    tags=["Recordings"],
)
async def get_active_recordings():
    """Lấy danh sách tất cả các phòng đang được bot ghi hình."""
    active_list = recorder_manager.list_active()
    return ActiveRecordingsResponse(
        total_active=len(active_list),
        recordings=active_list,
    )


@app.get(
    "/recordings/{room_name}/status",
    tags=["Recordings"],
)
async def get_room_status(room_name: str):
    """Kiểm tra trạng thái ghi hình của một phòng cụ thể."""
    bot = recorder_manager.get_bot(room_name)
    if not bot:
        return {
            "room_name": room_name,
            "is_recording": False,
            "message": "Không có phiên ghi nào đang hoạt động.",
        }

    return {
        "room_name": room_name,
        "is_recording": bot.is_running,
        "session_id": bot.session_id,
        "duration_sec": round(bot.now(), 2),
        "output_dir": str(bot.output_dir),
        "audio_segments_count": len(bot.audio_segments),
        "screen_segments_count": len(bot.screen_segments),
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )