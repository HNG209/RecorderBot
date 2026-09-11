import asyncio
import json
import shutil
import time
import uuid
import wave
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Any, Optional

import numpy as np
from livekit import api, rtc

from app.config import settings, logger
from app.r2_storage import r2_storage

def can_use_gstreamer() -> bool:
    """Kiểm tra môi trường hiện tại có dùng được GStreamer không."""
    try:
        # pyrefly: ignore [missing-import]
        import gi
        gi.require_version("Gst", "1.0")
        # pyrefly: ignore [missing-import]
        from gi.repository import Gst
        Gst.init(None)
        logger.info("GStreamer import thành công, đang kiểm tra plugins...")

        registry = Gst.Registry.get()
        required = ["appsrc", "vp8enc", "webmmux", "videoconvert"]
        for name in required:
            found = registry.find_feature(name, Gst.ElementFactory)
            if not found:
                logger.warning("GStreamer plugin thiếu: '%s' → fallback sang PyAV", name)
                return False
            logger.debug("GStreamer plugin OK: %s", name)

        logger.info("GStreamer sẵn sàng (tất cả plugins đều có mặt)")
        return True
    except ImportError as e:
        logger.warning("Không thể import GStreamer (gi/PyGObject): %s → fallback sang PyAV", e)
        return False
    except Exception as e:
        logger.warning("Lỗi khi kiểm tra GStreamer: %s → fallback sang PyAV", e)
        return False

def create_bot_token(room_name: str, bot_identity: str) -> str:
    """Tạo LiveKit Access Token cho bot tham gia ghi hình."""
    return (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(bot_identity)
        .with_name("Recorder Bot")
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_subscribe=True,
                can_publish=False,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )

class RoomRecorderBot:
    """Bot kết nối vào phòng LiveKit để ghi lại các track âm thanh và chia sẻ màn hình."""

    def __init__(
        self,
        room_name: str,
        session_id: Optional[str] = None,
    ):
        self.room_name = room_name
        self.session_id = session_id or str(int(time.time()))

        # Tự sinh ngẫu nhiên recording_id dạng rec_<randomId> trong service
        self.recording_id = f"rec_{uuid.uuid4().hex[:8]}"
        # Đảm bảo không trùng thư mục trên local nếu bấm quay nhiều lần
        while (settings.RECORDINGS_DIR / self.room_name / self.session_id / self.recording_id).exists():
            self.recording_id = f"rec_{uuid.uuid4().hex[:8]}"

        self.output_dir = settings.RECORDINGS_DIR / self.room_name / self.session_id / self.recording_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.bot_identity = f"recorder-bot-{self.recording_id}"
        self.token = create_bot_token(self.room_name, self.bot_identity)

        self.room = rtc.Room()
        self.start_mono: Optional[float] = None
        self.start_wall_time: float = time.time()
        self.screen_segments: List[Dict[str, Any]] = [] # Phân đoạn screen share
        self.audio_segments: List[Dict[str, Any]] = [] # Phân đoạn audio
        self._tasks: List[asyncio.Task] = []
        self.is_running: bool = False

    def now(self) -> float:
        """Thời gian trôi qua (giây) kể từ khi bắt đầu ghi."""
        if self.start_mono is None:
            return 0.0
        return time.monotonic() - self.start_mono

    async def start(self) -> None:
        """Bắt đầu kết nối vào LiveKit room và kích hoạt lắng nghe các track."""
        if self.is_running:
            logger.warning("Bot cho room '%s' đã đang chạy.", self.room_name)
            return

        self.start_mono = time.monotonic()
        self.start_wall_time = time.time()
        self.is_running = True
        logger.info(
            "Khởi động RoomRecorderBot cho phòng '%s' (session=%s)...",
            self.room_name,
            self.session_id,
        )

        @self.room.on("track_subscribed")
        def on_track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            logger.info(
                "Subscribed track: kind=%s, source=%s từ participant=%s",
                track.kind,
                publication.source,
                participant.identity,
            )

            if track.kind == rtc.TrackKind.KIND_AUDIO:
                t = asyncio.create_task(
                    self._record_audio(track, participant)
                )
                self._tasks.append(t)

            elif (
                track.kind == rtc.TrackKind.KIND_VIDEO
                and publication.source == rtc.TrackSource.SOURCE_SCREENSHARE
            ):
                try:
                    if getattr(publication, "simulcasted", False):
                        publication.set_video_quality(rtc.VideoQuality.VIDEO_QUALITY_HIGH)
                        logger.info("Đã request VIDEO_QUALITY_HIGH cho screen %s", publication.sid)
                except Exception as e:
                    logger.warning("Không set được video quality: %s", e)

                t = asyncio.create_task(
                    self._record_screen(track, participant)
                )
                self._tasks.append(t)

        @self.room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant):
            logger.info("Participant đã vào phòng: %s", participant.identity)
            # Gửi trạng thái is_recording=True cho thành viên vừa vào phòng
            if self.is_running:
                asyncio.create_task(
                    self.publish_recording_status(
                        is_recording=True,
                        destination_identities=[participant.identity],
                    )
                )

        @self.room.on("participant_disconnected")
        def on_participant_disconnected(participant: rtc.RemoteParticipant):
            logger.info("Participant đã rời phòng: %s", participant.identity)

        # Kết nối tới LiveKit Server
        await self.room.connect(settings.LIVEKIT_URL, self.token)
        logger.info("Đã kết nối thành công tới phòng LiveKit: %s", self.room.name)

        # Phát trạng thái is_recording=True qua Data Channel cho toàn bộ phòng
        await self.publish_recording_status(is_recording=True)

        # Quét các tracks đã có sẵn trong phòng trước khi bot vào
        for participant in self.room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.track is not None:
                    on_track_subscribed(pub.track, pub, participant)

    async def _record_audio(
        self, track: rtc.Track, participant: rtc.RemoteParticipant
    ) -> None:
        """Ghi stream Audio ra file định dạng WAV."""
        safe_id = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in participant.identity
        )
        wav_path = self.output_dir / f"audio_{safe_id}_{int(self.now())}.wav"

        sample_rate = 48000
        num_channels = 1
        stream = rtc.AudioStream(track, sample_rate=sample_rate, num_channels=num_channels)

        frames_pcm: List[bytes] = []
        start_ts = self.now()
        logger.info("Bắt đầu ghi Audio: %.3fs -> %s", start_ts, wav_path.name)

        try:
            async for event in stream:
                frame = event.frame
                # frame.data: PCM int16
                frames_pcm.append(bytes(frame.data))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Lỗi khi đọc Audio stream từ %s: %s", participant.identity, e)
        finally:
            end_ts = self.now()
            if frames_pcm:
                pcm = b"".join(frames_pcm)
                with wave.open(str(wav_path), "wb") as wf:
                    wf.setnchannels(num_channels)
                    wf.setsampwidth(2)  # 16-bit PCM
                    wf.setframerate(sample_rate)
                    wf.writeframes(pcm)

                dur = round(end_ts - start_ts, 3)
                logger.info(
                    "Hoàn thành ghi Audio: %.3fs | duration=%.2fs | file=%s",
                    end_ts,
                    dur,
                    wav_path.name,
                )
                self.audio_segments.append({
                    "participant": participant.identity,
                    "start": round(start_ts, 3),
                    "end": round(end_ts, 3),
                    "duration": dur,
                    "file": wav_path.name,
                })
            else:
                logger.warning("Track Audio từ %s không có dữ liệu!", participant.identity)

    async def _record_screen_gstreamer(
        self, track: rtc.Track, participant: rtc.RemoteParticipant
    ) -> None:
        """Ghi screen share bằng GStreamer pipeline."""
        # pyrefly: ignore [missing-import]
        import gi
        gi.require_version("Gst", "1.0")
        # pyrefly: ignore [missing-import]
        from gi.repository import Gst, GLib
        import threading

        Gst.init(None)

        start_ts = self.now()
        safe_id = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in participant.identity
        )
        webm_path = self.output_dir / f"screen_{safe_id}_{int(start_ts)}.webm"

        stream = rtc.VideoStream(track)

        pipeline = None
        appsrc = None
        frame_count = 0
        first_frame_us: int | None = None
        main_loop = None
        loop_thread = None

        def on_bus_message(bus, message):
            t = message.type
            if t == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                logger.error("GStreamer ERROR: %s | debug: %s", err, debug)
                if main_loop:
                    main_loop.quit()
            elif t == Gst.MessageType.EOS:
                logger.info("GStreamer nhận EOS")
                if main_loop:
                    main_loop.quit()
            return True

        def start_pipeline(w: int, h: int):
            nonlocal pipeline, appsrc, main_loop, loop_thread

            pipeline_str = (
                f"appsrc name=src is-live=true format=time do-timestamp=false "
                f"! videoconvert "
                f"! vp8enc deadline=1 cpu-used=6 target-bitrate=2500000 "
                f"! webmmux "
                f"! filesink location={webm_path}"
            )

            pipeline = Gst.parse_launch(pipeline_str)
            appsrc = pipeline.get_by_name("src")

            caps = Gst.Caps.from_string(
                f"video/x-raw,format=RGB,width={w},height={h},framerate=30/1"
            )
            appsrc.set_property("caps", caps)
            appsrc.set_property("format", Gst.Format.TIME)

            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_bus_message)

            main_loop = GLib.MainLoop()
            loop_thread = threading.Thread(target=main_loop.run, daemon=True)
            loop_thread.start()

            pipeline.set_state(Gst.State.PLAYING)
            logger.info("GStreamer pipeline đã chạy: %dx%d → %s", w, h, webm_path.name)

        def push_frame(rgb: np.ndarray, frame_us: int):
            nonlocal frame_count, first_frame_us

            if appsrc is None:
                return

            if first_frame_us is None:
                first_frame_us = frame_us

            pts_ns = (frame_us - first_frame_us) * 1000  # µs → ns

            data = rgb.tobytes()
            buf = Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)
            buf.pts = pts_ns
            buf.duration = Gst.util_uint64_scale_int(1, Gst.SECOND, 30)

            ret = appsrc.emit("push-buffer", buf)
            if ret == Gst.FlowReturn.OK:
                frame_count += 1

        try:
            async for event in stream:
                frame = event.frame
                frame_bgra = frame.convert(rtc.VideoBufferType.BGRA)
                w, h = frame_bgra.width, frame_bgra.height
                w2 = w - (w % 2)
                h2 = h - (h % 2)

                arr = np.frombuffer(frame_bgra.data, dtype=np.uint8).reshape(h, w, 4)
                rgb = arr[:h2, :w2, :][:, :, [2, 1, 0]].copy()

                if pipeline is None:
                    start_pipeline(w2, h2)

                push_frame(rgb, event.timestamp_us)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Lỗi khi đọc Video stream từ %s: %s", participant.identity, e)
        finally:
            if appsrc is not None:
                appsrc.emit("end-of-stream")

            if loop_thread is not None:
                loop_thread.join(timeout=15.0)

            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)

            end_ts = self.now()
            real_dur = round(end_ts - start_ts, 3)

            seg = {
                "participant": participant.identity,
                "start": round(start_ts, 3),
                "end": round(end_ts, 3),
                "frames": frame_count,
                "real_duration_sec": real_dur,
                "file": webm_path.name if (frame_count > 0 and webm_path.exists()) else None,
            }
            self.screen_segments.append(seg)

            logger.info(
                "Hoàn thành ghi Screen (GStreamer): real=%.2fs | frames=%d | file=%s",
                real_dur,
                frame_count,
                seg.get("file"),
            )
        
    # Fallback nếu không chạy được GStreamer
    async def _record_screen_pyav(
        self, track: rtc.Track, participant: rtc.RemoteParticipant
    ) -> None:
        """Ghi stream Video chia sẻ màn hình ra file WebM (VP8) bằng 1 worker thread riêng."""
        # pyrefly: ignore [missing-import]
        import av
        import queue
        import threading

        start_ts = self.now()
        safe_id = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in participant.identity
        )
        webm_path = self.output_dir / f"screen_{safe_id}_{int(start_ts)}.webm"

        stream = rtc.VideoStream(track)

        # Queue truyền frame từ async task → worker thread
        # maxsize=90 ≈ giữ tối đa ~3 giây frame (30fps) để tránh RAM tăng vô hạn
        frame_queue: queue.Queue = queue.Queue(maxsize=90)

        # Các biến dùng chung (chỉ worker thread ghi, main thread chỉ đọc ở cuối)
        frame_count = 0
        last_pts = -1
        first_frame_us: int | None = None
        writer = None
        stream_out = None
        encode_error: Exception | None = None

        FPS = 30
        TIME_BASE = Fraction(1, 1_000_000)

        def encoder_worker():
            """Chạy trên 1 thread riêng, chuyên encode + mux."""
            nonlocal writer, stream_out, frame_count, last_pts, first_frame_us, encode_error

            try:
                while True:
                    item = frame_queue.get()
                    if item is None:          # sentinel → kết thúc
                        break

                    rgb, frame_us, w2, h2 = item

                    # Khởi tạo encoder khi nhận frame đầu tiên
                    if writer is None:
                        logger.info(
                            "Screen nhận frame đầu: %dx%d từ %s",
                            w2, h2, participant.identity,
                        )
                        writer = av.open(str(webm_path), mode="w", format="webm")
                        stream_out = writer.add_stream("libvpx", rate=FPS)
                        stream_out.width = w2
                        stream_out.height = h2
                        stream_out.pix_fmt = "yuv420p"
                        stream_out.time_base = TIME_BASE
                        stream_out.bit_rate = 2_500_000          # giảm nhẹ so với trước
                        stream_out.options = {
                            "deadline": "realtime",               # ưu tiên tốc độ
                            "cpu-used": "6",                      # 0=chậm-chất lượng cao, 8=nhanh
                            "crf": "18",
                            "threads": "2",
                        }
                        logger.info("Khởi tạo WebM encoder %dx%d -> %s", w2, h2, webm_path.name)

                    if first_frame_us is None:
                        first_frame_us = frame_us

                    pts = frame_us - first_frame_us
                    if pts <= last_pts:
                        pts = last_pts + 1
                    last_pts = pts

                    video_frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                    video_frame.pts = pts
                    video_frame.time_base = TIME_BASE

                    for packet in stream_out.encode(video_frame):
                        writer.mux(packet)

                    frame_count += 1

            except Exception as e:
                encode_error = e
                logger.exception("Lỗi trong encoder worker của %s: %s", participant.identity, e)
            finally:
                # Flush encoder và đóng file
                if writer is not None and stream_out is not None:
                    try:
                        for packet in stream_out.encode(None):
                            writer.mux(packet)
                        writer.close()
                        logger.info("Đã flush & đóng WebM: %s (%d frames)", webm_path.name, frame_count)
                    except Exception as e:
                        logger.warning("Lỗi khi flush/close video writer: %s", e)

        # Khởi động worker thread (daemon để không chặn process khi tắt app)
        worker = threading.Thread(
            target=encoder_worker,
            name=f"screen-encoder-{safe_id}",
            daemon=True,
        )
        worker.start()

        try:
            async for event in stream:
                frame = event.frame
                frame_bgra = frame.convert(rtc.VideoBufferType.BGRA)
                w, h = frame_bgra.width, frame_bgra.height
                w2 = w - (w % 2)
                h2 = h - (h % 2)

                arr = np.frombuffer(frame_bgra.data, dtype=np.uint8).reshape(h, w, 4)
                rgb = arr[:h2, :w2, :][:, :, [2, 1, 0]].copy()

                frame_us: int = event.timestamp_us

                # Đưa frame vào queue. Nếu queue đầy → bỏ frame (tránh block + tăng RAM)
                try:
                    frame_queue.put_nowait((rgb, frame_us, w2, h2))
                except queue.Full:
                    # Có thể log thỉnh thoảng nếu muốn theo dõi drop frame
                    pass

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Lỗi khi đọc Video stream từ %s: %s", participant.identity, e)
        finally:
            # Báo worker dừng lại
            try:
                frame_queue.put(None, timeout=1.0)
            except Exception:
                pass

            # Chờ worker flush xong (tối đa 12 giây)
            worker.join(timeout=12.0)

            if worker.is_alive():
                logger.warning(
                    "Encoder worker của %s vẫn còn chạy sau 12s, bỏ qua chờ thêm",
                    participant.identity,
                )

            end_ts = self.now()
            real_dur = round(end_ts - start_ts, 3)

            seg = {
                "participant": participant.identity,
                "start": round(start_ts, 3),
                "end": round(end_ts, 3),
                "frames": frame_count,
                "real_duration_sec": real_dur,
                "file": webm_path.name if (frame_count > 0 and webm_path.exists()) else None,
            }
            self.screen_segments.append(seg)

            if encode_error:
                logger.error("Encoder worker kết thúc với lỗi: %s", encode_error)

            logger.info(
                "Hoàn thành ghi Screen: real=%.2fs | frames=%d | file=%s",
                real_dur,
                frame_count,
                seg.get("file"),
            )
    
    async def _record_screen(
        self, track: rtc.Track, participant: rtc.RemoteParticipant
    ) -> None:
        """Tự động chọn GStreamer hoặc PyAV."""
        if can_use_gstreamer():
            logger.info("Sử dụng GStreamer")
            await self._record_screen_gstreamer(track, participant)
        else:
            logger.info("Sử dụng PyAV")
            await self._record_screen_pyav(track, participant)

    async def publish_recording_status(
        self,
        is_recording: bool,
        destination_identities: Optional[List[str]] = None,
    ) -> bool:
        """Phát trạng thái recording (is_recording) qua Data Channel của LiveKit tới các client trong phòng."""
        if not self.room or not self.room.isconnected():
            logger.debug(
                "Chưa thể gửi data channel: Bot chưa kết nối tới phòng '%s'",
                self.room_name,
            )
            return False

        payload_dict = {
            "type": "RECORDING_STATUS",
            "room_name": self.room_name,
            "is_recording": is_recording,
            "status": "recording" if is_recording else "stopped",
            "session_id": self.session_id,
            "recording_id": self.recording_id,
        }
        payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")

        try:
            dest = destination_identities or []
            await self.room.local_participant.publish_data(
                payload=payload_bytes,
                reliable=True,
                destination_identities=dest,
                topic="RECORDING_STATUS",
            )
            logger.info(
                "Đã phát qua Data Channel phòng '%s': is_recording=%s (topic='RECORDING_STATUS', dest=%s)",
                self.room_name,
                is_recording,
                dest if dest else "ALL",
            )
            return True
        except Exception as e:
            logger.error(
                "Lỗi khi phát Data Channel trạng thái recording cho phòng '%s': %s",
                self.room_name,
                e,
            )
            return False

    async def stop(self) -> Dict[str, Any]:
        """Dừng bot, huỷ các task ghi, ngắt kết nối LiveKit và lưu timeline.json."""
        self.is_running = False
        logger.info("Đang dừng bot ghi hình phòng '%s'...", self.room_name)

        # Phát thông báo is_recording=False qua data channel trước khi ngắt kết nối
        try:
            await self.publish_recording_status(is_recording=False)
            await asyncio.sleep(0.1)  # Đợi 100ms để gói tin truyền qua DataChannel
        except Exception as e:
            logger.warning("Lỗi khi phát trạng thái dừng qua Data Channel: %s", e)

        # Cancel toàn bộ tasks ghi stream
        for t in self._tasks:
            t.cancel()

        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Timeout khi chờ các task ghi kết thúc")
            self._tasks.clear()

        try:
            await self.room.disconnect()
        except Exception as e:
            logger.warning("Lỗi khi ngắt kết nối room: %s", e)

        duration = self.now() if self.start_mono is not None else 0.0

        meta = {
            "room": self.room_name,
            "session_id": self.session_id,
            "recording_id": self.recording_id,
            "start_time": self.start_wall_time,
            "duration_sec": round(duration, 3),
            "audio_segments": self.audio_segments,
            "screen_segments": self.screen_segments,
            "output_dir": str(self.output_dir),
        }

        # Lưu timeline metadata
        meta_path = self.output_dir / "timeline.json"
        try:
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info("Đã lưu timeline thành công -> %s", meta_path)
        except Exception as e:
            logger.error("Lỗi khi ghi file timeline.json: %s", e)

        return meta


class RecorderManager:
    """Quản lý các instance RoomRecorderBot đang hoạt động theo tên phòng."""

    def __init__(self):
        self._active_bots: Dict[str, RoomRecorderBot] = {}
        self._background_tasks: set = set()

    def is_recording(self, room_name: str) -> bool:
        return room_name in self._active_bots and self._active_bots[room_name].is_running

    def get_bot(self, room_name: str) -> Optional[RoomRecorderBot]:
        return self._active_bots.get(room_name)

    def list_active(self) -> List[Dict[str, Any]]:
        results = []
        for room_name, bot in self._active_bots.items():
            results.append({
                "room_name": room_name,
                "session_id": bot.session_id,
                "recording_id": bot.recording_id,
                "status": "recording" if bot.is_running else "stopping",
                "duration_sec": round(bot.now(), 2),
                "output_dir": str(bot.output_dir),
                "audio_segments_count": len(bot.audio_segments),
                "screen_segments_count": len(bot.screen_segments),
            })
        return results

    async def start_recording(
        self,
        room_name: str,
        session_id: Optional[str] = None,
    ) -> RoomRecorderBot:
        """Khởi chạy ghi âm/hình cho một phòng LiveKit."""
        if self.is_recording(room_name):
            raise ValueError(f"Phòng '{room_name}' hiện đang được ghi hình.")

        bot = RoomRecorderBot(
            room_name=room_name,
            session_id=session_id,
        )
        self._active_bots[room_name] = bot

        try:
            await bot.start()
            return bot
        except Exception as e:
            self._active_bots.pop(room_name, None)
            logger.exception("Không thể bắt đầu ghi phòng '%s': %s", room_name, e)
            raise e

    def _cleanup_local_dir(self, output_dir: Path) -> None:
        """Xóa thư mục recording local và các thư mục cha nếu trống."""
        if not output_dir.exists():
            return

        for attempt in range(3):
            try:
                shutil.rmtree(output_dir)
                logger.info("Đã xóa thư mục local: %s", output_dir)
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.3)
                else:
                    logger.warning("Không thể xóa thư mục local %s: %s", output_dir, e)
                    return

        # Dọn dẹp thư mục cha (session_id và room_name) nếu không còn file/thư mục con nào
        try:
            session_dir = output_dir.parent
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()
                logger.info("Đã dọn dẹp thư mục session trống: %s", session_dir)
                room_dir = session_dir.parent
                if room_dir.exists() and not any(room_dir.iterdir()):
                    room_dir.rmdir()
                    logger.info("Đã dọn dẹp thư mục room trống: %s", room_dir)
        except Exception as e:
            logger.debug("Lỗi dọn dẹp thư mục cha trống: %s", e)

    async def _finish_stop_and_upload(
        self,
        bot: RoomRecorderBot,
        auto_upload_r2: bool = True,
        cleanup_local: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Dừng bot, upload R2 và dọn dẹp local."""
        # 1. Dừng bot & lấy timeline
        timeline = await bot.stop()

        # 2. Upload toàn bộ file chưa qua xử lý lên Cloudflare R2
        uploaded_files = []
        if auto_upload_r2:
            r2_prefix = f"{bot.room_name}/{bot.session_id}/{bot.recording_id}"
            logger.info(
                "Đang upload toàn bộ file thô từ %s lên Cloudflare R2 (prefix='%s')...",
                bot.output_dir,
                r2_prefix,
            )
            uploaded_files = await r2_storage.upload_directory(bot.output_dir, prefix=r2_prefix)

            # 3. Dọn dẹp thư mục local sau khi upload R2 hoàn tất
            should_cleanup = (
                cleanup_local
                if cleanup_local is not None
                else settings.CLEANUP_LOCAL_AFTER_UPLOAD
            )
            if should_cleanup and bot.output_dir.exists():
                if settings.is_r2_configured:
                    logger.info(
                        "Đang dọn dẹp thư mục local sau khi hoàn tất upload R2: %s",
                        bot.output_dir,
                    )
                    self._cleanup_local_dir(bot.output_dir)
                else:
                    logger.warning(
                        "Bỏ qua dọn dẹp thư mục local %s vì Cloudflare R2 chưa được cấu hình.",
                        bot.output_dir,
                    )

        return {
            "room_name": bot.room_name,
            "session_id": bot.session_id,
            "recording_id": bot.recording_id,
            "duration_sec": timeline.get("duration_sec", 0.0),
            "local_output_dir": str(bot.output_dir),
            "timeline": timeline,
            "uploaded_files": uploaded_files,
        }

    async def stop_recording(
        self,
        room_name: str,
        auto_upload_r2: bool = True,
        cleanup_local: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Dừng ghi phòng LiveKit và upload tất cả file thô lên Cloudflare R2 (chờ hoàn tất)."""
        bot = self._active_bots.pop(room_name, None)
        if not bot:
            raise ValueError(f"Không tìm thấy phiên ghi nào đang hoạt động cho phòng '{room_name}'.")

        return await self._finish_stop_and_upload(
            bot=bot,
            auto_upload_r2=auto_upload_r2,
            cleanup_local=cleanup_local,
        )

    def pop_bot(self, room_name: str) -> RoomRecorderBot:
        """Tách bot ra khỏi danh sách active ngay lập tức (không I/O, không block).
        Dùng kết hợp với FastAPI BackgroundTasks để đảm bảo 204 trả về trước
        khi bất kỳ tác vụ nặng nào bắt đầu chạy.
        """
        bot = self._active_bots.pop(room_name, None)
        if not bot:
            raise ValueError(f"Không tìm thấy phiên ghi nào đang hoạt động cho phòng '{room_name}'.")
        bot.is_running = False
        return bot

    async def run_stop_background_task(
        self,
        bot: RoomRecorderBot,
        auto_upload_r2: bool = True,
        cleanup_local: Optional[bool] = None,
        webhook_callback: bool = True,
    ) -> None:
        """Coroutine thực hiện toàn bộ tác vụ dừng bot, upload R2 và bắn webhook.
        Được gọi bởi FastAPI BackgroundTasks sau khi response 204 đã gửi xong.
        """
        try:
            result = await self._finish_stop_and_upload(
                bot=bot,
                auto_upload_r2=auto_upload_r2,
                cleanup_local=cleanup_local,
            )
            if webhook_callback:
                try:
                    from app.webhook import send_post_process_webhook
                    await send_post_process_webhook(result)
                except Exception as wh_err:
                    logger.error(
                        "Lỗi khi bắn webhook sau khi stop phòng %s: %s",
                        bot.room_name,
                        wh_err,
                    )
        except Exception as e:
            logger.exception(
                "Lỗi trong tác vụ background dừng phòng %s: %s",
                bot.room_name,
                e,
            )

    async def stop_recording_background(
        self,
        room_name: str,
        auto_upload_r2: bool = True,
        cleanup_local: Optional[bool] = None,
        webhook_callback: bool = True,
    ) -> RoomRecorderBot:
        """
        Dừng ghi phòng LiveKit ngay lập tức (trả về bot ngay)
        và chạy tác vụ dừng bot, upload R2, dọn dẹp local, bắn webhook ở chế độ bất đồng bộ ngầm.
        """
        bot = self._active_bots.pop(room_name, None)
        if not bot:
            raise ValueError(f"Không tìm thấy phiên ghi nào đang hoạt động cho phòng '{room_name}'.")

        bot.is_running = False

        async def _task_runner():
            try:
                result = await self._finish_stop_and_upload(
                    bot=bot,
                    auto_upload_r2=auto_upload_r2,
                    cleanup_local=cleanup_local,
                )
                if webhook_callback:
                    try:
                        from app.webhook import send_post_process_webhook
                        await send_post_process_webhook(result)
                    except Exception as wh_err:
                        logger.error(
                            "Lỗi khi bắn webhook sau khi stop phòng %s: %s",
                            bot.room_name,
                            wh_err,
                        )
            except Exception as e:
                logger.exception(
                    "Lỗi trong tác vụ background dừng phòng %s: %s",
                    bot.room_name,
                    e,
                )

        task = asyncio.create_task(_task_runner())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return bot


# Singleton manager instance
recorder_manager = RecorderManager()
