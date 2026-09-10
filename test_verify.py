import sys
import tempfile
import os
from pathlib import Path

# Đảm bảo stdout hỗ trợ utf-8 trên Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
import main
from app.config import settings
from app.r2_storage import r2_storage
from app.recorder import recorder_manager, RoomRecorderBot

def test_app():
    print("[TEST] Testing FastAPI app routes...")
    client = TestClient(main.app)

    # 1. Health check
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    print("[PASS] GET /health ->", res.json())

    # 2. Storage status
    res = client.get("/storage/r2/status")
    assert res.status_code == 200, f"Storage status failed: {res.text}"
    print("[PASS] GET /storage/r2/status ->", res.json())

    # 3. Active recordings
    res = client.get("/recordings/active")
    assert res.status_code == 200, f"Active recordings failed: {res.text}"
    print("[PASS] GET /recordings/active ->", res.json())

    # 4. Stop when not recording -> 404
    res = client.post("/recordings/stop", json={"room_name": "non_existent_room"})
    assert res.status_code == 404, f"Expected 404 for non-existent room: {res.text}"
    print("[PASS] POST /recordings/stop (non-existent room handled with 404)")

    # 5. Room status
    res = client.get("/recordings/test-room/status")
    assert res.status_code == 200
    print("[PASS] GET /recordings/test-room/status ->", res.json())

    # 6. Test R2 upload directly
    print("[TEST] Testing Cloudflare R2 upload...")
    if r2_storage.is_available:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            test_file = tmp_path / "test_ping.txt"
            test_file.write_text("LiveKit Recorder Bot - Cloudflare R2 Test Ping", encoding="utf-8")
            
            upload_res = r2_storage.upload_file(test_file, "tests/test_ping.txt")
            print("[PASS] Cloudflare R2 upload result:", upload_res)
            assert upload_res is not None, "Failed to upload test file to R2"
            assert upload_res["object_key"] == "tests/test_ping.txt"

    # 7. Test RoomRecorderBot structure & anti-conflict on multiple recordings
    print("[TEST] Testing RoomRecorderBot instantiation and anti-conflict...")
    bot1 = RoomRecorderBot("test-room-123", session_id="test_session")
    assert bot1.room_name == "test-room-123"
    assert bot1.session_id == "test_session"
    assert bot1.recording_id.startswith("rec_")
    assert bot1.output_dir == settings.RECORDINGS_DIR / "test-room-123" / "test_session" / bot1.recording_id
    assert bot1.output_dir.exists()
    print(f"[PASS] RoomRecorderBot 1 initialized -> recording_id={bot1.recording_id}, path={bot1.output_dir}")

    # Bấm quay lần 2 trong cùng session_id: không được trùng lặp
    bot2 = RoomRecorderBot("test-room-123", session_id="test_session")
    assert bot2.recording_id.startswith("rec_")
    assert bot2.recording_id != bot1.recording_id
    assert bot2.output_dir != bot1.output_dir
    assert bot2.output_dir.exists()
    print(f"[PASS] RoomRecorderBot 2 (anti-conflict) -> recording_id={bot2.recording_id}, path={bot2.output_dir}")

    # 8. Test Webhook payload generation
    print("[TEST] Testing Webhook payload format...")
    from app.webhook import build_webhook_payload, build_payload_from_session
    mock_data = {
        "room_name": "meet-test-xyz",
        "session_id": "sess-999",
        "recording_id": "rec_abc123",
        "duration_sec": 42.5,
    }
    payload = build_webhook_payload(mock_data)
    assert payload["folder"] == "meet-test-xyz/sess-999/rec_abc123"
    assert payload["recording_id"] == "rec_abc123"
    assert payload["r2"]["folder_prefix"] == "meet-test-xyz/sess-999/rec_abc123"
    assert payload["r2"]["timeline_key"] == "meet-test-xyz/sess-999/rec_abc123/timeline.json"
    print("[PASS] Webhook payload format verified:", payload)

    # 9. Test Local cleanup logic
    print("[TEST] Testing local directory cleanup logic...")
    dummy_dir = settings.RECORDINGS_DIR / "test-clean-room" / "test-clean-sess" / "rec_test_clean"
    dummy_dir.mkdir(parents=True, exist_ok=True)
    dummy_file = dummy_dir / "timeline.json"
    dummy_file.write_text("{}", encoding="utf-8")
    assert dummy_dir.exists()
    assert dummy_file.exists()

    recorder_manager._cleanup_local_dir(dummy_dir)
    assert not dummy_dir.exists(), "Thư mục recording local chưa được dọn dẹp"
    assert not (settings.RECORDINGS_DIR / "test-clean-room").exists(), "Thư mục cha trống chưa được dọn dẹp"
    print("[PASS] Local cleanup logic verified (recording dir & empty parents removed)")

    # Dọn dẹp bot1, bot2 tạo trong lúc test
    recorder_manager._cleanup_local_dir(bot1.output_dir)
    recorder_manager._cleanup_local_dir(bot2.output_dir)

    # 10. Test asynchronous stop via API
    print("[TEST] Testing asynchronous stop via API...")
    fake_bot = RoomRecorderBot("test-async-room", session_id="test_sess")
    fake_bot.is_running = True
    recorder_manager._active_bots["test-async-room"] = fake_bot
    assert recorder_manager.is_recording("test-async-room")

    # Gọi API stop: phải trả về 204 ngay lập tức
    res = client.post("/recordings/stop", json={"room_name": "test-async-room"})
    assert res.status_code == 204
    # Xác nhận ngay lập tức room không còn đang ghi
    assert not recorder_manager.is_recording("test-async-room")
    print("[PASS] POST /recordings/stop returned 204 immediately and room marked stopped")

    # Dọn dẹp fake_bot directory
    recorder_manager._cleanup_local_dir(fake_bot.output_dir)

    print("\n[ALL TESTS PASSED SUCCESSFULLY!]")

if __name__ == "__main__":
    test_app()
