import json
import logging
import os
import subprocess
import sys
import time
from functools import partial
from os.path import abspath, join, dirname
from flask import Flask, Response, jsonify, request, send_file, stream_with_context, make_response, render_template

# Add src directory to sys.path for systemd execution
# (systemd runs app.py as script, not as module)
SRC_DIR = abspath(join(dirname(__file__), ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from panel.overlay import OverlayStream
from panel.camera import capture_jpeg, ensure_grabber, get_grabber_frame

def create_app():
    app = Flask(__name__)
    
    # Настройка логгера
    log_dir = "/home/nemez/project_root/logs"
    os.makedirs(log_dir, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f"{log_dir}/panel.log", mode='a'),
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info("panel started")
    
    # Log overlay environment variables
    overlay_env_vars = [
        "OVERLAY_DETECTIONS_FILE", "OVERLAY_MIN_CONF", "OVERLAY_TAIL_BYTES",
        "OVERLAY_MAX_SIDE", "OVERLAY_DET_MAX_AGE_MS", "OVERLAY_FPS", "OVERLAY_CAPTURE_FPS",
        "OVERLAY_YOLO_FALLBACK", "OVERLAY_YOLO_MODEL", "OVERLAY_YOLO_CONF",
        "OVERLAY_YOLO_IOU", "OVERLAY_YOLO_IMGSZ", "OVERLAY_YOLO_FPS"
    ]
    
    for var in overlay_env_vars:
        value = os.getenv(var)
        if value is not None:
            logger.info(f"overlay env {var}={value}")

    @app.get("/")
    def index():
        """Main panel page"""
        return render_template('index.html')

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    @app.get("/api/last")
    def last_event():
        LOGS_DIR = "/home/nemez/project_root/logs"
        DETECTIONS_JSONL = f"{LOGS_DIR}/detections.jsonl"
        
        try:
            # Read last non-empty line from end of file
            with open(DETECTIONS_JSONL, 'rb') as f:
                f.seek(0, 2)  # Go to end
                file_size = f.tell()
                
                if file_size == 0:
                    return {"error": "no events"}, 404
                
                # Read backwards to find last complete line
                chunk_size = min(8192, file_size)
                f.seek(max(0, file_size - chunk_size))
                chunk = f.read()
                
                # Find last newline
                last_newline = chunk.rfind(b'\n')
                if last_newline == -1:
                    # No newlines in chunk, read from beginning
                    f.seek(0)
                    chunk = f.read()
                    last_newline = chunk.rfind(b'\n')
                
                if last_newline == -1:
                    # Single line file
                    line = chunk.decode('utf-8', errors='ignore').strip()
                else:
                    # Extract last line
                    line = chunk[last_newline + 1:].decode('utf-8', errors='ignore').strip()
                
                if not line:
                    return {"error": "no events"}, 404
                
                # Parse JSON
                try:
                    event = json.loads(line)
                    return event, 200
                except json.JSONDecodeError:
                    return {"error": "invalid JSON"}, 500
                    
        except FileNotFoundError:
            return {"error": "detections file not found"}, 404
        except Exception as e:
            logger.error(f"failed to read last event: {e}")
            return {"error": "read failed"}, 500

    @app.get("/api/health")
    def api_health():
        """Health check with camera and detector status"""
        try:
            # Check if camera processes are running
            result = subprocess.run(
                ["pgrep", "-f", "rpicam-still"],
                capture_output=True,
                text=True
            )
            
            camera_processes = result.stdout.strip()
            camera_status = "busy" if camera_processes else "ok"
            
            # Check detector service status
            try:
                detector_result = subprocess.run(
                    ["systemctl", "is-active", "dd5ka-detector.service"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                detector_status = detector_result.stdout.strip()
            except Exception:
                detector_status = "unknown"
            
            response = {
                "status": "ok",
                "camera": camera_status,
                "detector": detector_status
            }
            
            if camera_processes:
                response["processes"] = len(camera_processes.split('\n')) if camera_processes else 0
                
            return response, 200
                
        except Exception as e:
            logger.error(f"health check failed: {e}")
            return {
                "status": "error",
                "camera": "error",
                "detector": "unknown"
            }, 500

    @app.get("/snapshot")
    def snapshot():
        try:
            max_side = int(os.getenv("SNAPSHOT_MAX_SIDE", "960"))
            # Если включён непрерывный захват (по умолчанию), отдаём кадр из глобального граббера.
            # ВАЖНО: больше не падаем в rpicam-still, чтобы не конфликтовать с rpicam-vid.
            use_grabber = os.getenv("SNAPSHOT_USE_GRABBER", os.getenv("OVERLAY_CONTINUOUS", "1")).strip() == "1"
            jpeg_data = None
            if use_grabber:
                ensure_grabber(max_side=max_side, fps=int(os.getenv("OVERLAY_CAPTURE_FPS", "8")))
                # Подождём немного первый кадр из граббера (без блокировок камеры)
                deadline = time.time() + float(os.getenv("SNAPSHOT_GRABBER_WAIT_S", "0.7"))
                while jpeg_data is None and time.time() < deadline:
                    jpeg_data = get_grabber_frame()
                    if jpeg_data:
                        break
                    time.sleep(0.02)
                if jpeg_data is None:
                    # Нет свежего кадра: сообщаем «занято», детектор ретраит
                    return jsonify({"error": "camera busy"}), 503
            else:
                # Явно запросили старый путь: разовый снимок с сериализацией
                jpeg_data = capture_jpeg(max_side=max_side)
            return Response(jpeg_data, mimetype="image/jpeg"), 200
        except Exception as e:
            logger.warning(f"snapshot failed: {e}")
            return jsonify({"error": "snapshot failed"}), 500

    @app.get("/stream")
    def stream():
        # Parse and validate query parameters with safe resolution fallback
        try:
            width = int(request.args.get('width', 2028))
            height = int(request.args.get('height', 1520))
        except (ValueError, TypeError):
            width, height = 2028, 1520
        
        # Only allow safe resolutions for IMX500
        if (width, height) not in [(2028, 1520), (4056, 3040)]:
            width, height = 2028, 1520
        
        try:
            # Use rpicam-vid for MJPEG streaming
            cmd = [
                "/usr/bin/rpicam-vid",
                "-n",  # no preview
                "-t", "0",  # continuous
                "--width", str(width),
                "--height", str(height),
                "--framerate", "15",
                "--bitrate", "2000000",  # 2Mbps
                "--inline",  # inline headers
                "-o", "-"  # output to stdout
            ]
            
            def generate():
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0
                    )
                    
                    # Stream MJPEG data
                    while True:
                        chunk = process.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                        
                except Exception as e:
                    logger.error(f"stream failed: {e}")
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    yield b"Stream error"
                    yield b"\r\n"
                finally:
                    if 'process' in locals():
                        process.terminate()
                        process.wait()
            
            return Response(
                stream_with_context(generate()),
                mimetype="multipart/x-mixed-replace; boundary=frame"
            )
            
        except Exception as e:
            logger.error(f"stream setup failed: {e}")
            return jsonify({"error": "stream failed"}), 500

    @app.get("/stream/overlay.mjpg")
    def stream_overlay():
        """MJPEG stream with detection overlays"""
        try:
            overlay_stream = OverlayStream(logger)
            return Response(
                stream_with_context(overlay_stream.generate_frames()),
                mimetype="multipart/x-mixed-replace; boundary=frame"
            )
        except Exception as e:
            logger.error(f"overlay stream failed: {e}")
            return jsonify({"error": "overlay stream failed"}), 500

    @app.route("/overlay.mjpg")
    def overlay_mjpeg():
        stream = OverlayStream(logger=app.logger)
        gen = stream.generate_frames()
        resp = Response(stream_with_context(gen),
                        mimetype="multipart/x-mixed-replace; boundary=frame",
                        direct_passthrough=True)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/stream/overlay.mjpg")
    def overlay_mjpeg_alias():
        return overlay_mjpeg()

    @app.route("/overlay.jpg")
    def overlay_single():
        stream = OverlayStream(logger=app.logger)
        frame = stream.generate_single_frame()
        resp = make_response(frame)
        resp.headers["Content-Type"] = "image/jpeg"
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.route("/stream/overlay.jpg")
    def overlay_single_alias():
        return overlay_single()

    # Detector control endpoints
    @app.get("/api/detector/status")
    def detector_status():
        """Get detector service status with detailed state"""
        try:
            # Get detailed status using systemctl show
            result = subprocess.run(
                ["systemctl", "show", "-p", "ActiveState", "-p", "SubState", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                active_state = "unknown"
                sub_state = "unknown"
                
                for line in lines:
                    if line.startswith("ActiveState="):
                        active_state = line.split("=", 1)[1]
                    elif line.startswith("SubState="):
                        sub_state = line.split("=", 1)[1]
                
                return {
                    "unit": "dd5ka-detector.service",
                    "active_state": active_state,
                    "sub_state": sub_state
                }, 200
            else:
                # Fallback to is-active if show fails
                result = subprocess.run(
                    ["systemctl", "is-active", "dd5ka-detector.service"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                status = result.stdout.strip()
                return {
                    "unit": "dd5ka-detector.service", 
                    "active_state": status,
                    "sub_state": "unknown"
                }, 200
                
        except Exception as e:
            logger.error(f"detector status check failed: {e}")
            return {"unit": "dd5ka-detector.service", "active_state": "unknown", "sub_state": "unknown", "error": str(e)}, 500

    @app.post("/api/detector/start")
    def detector_start():
        """Start detector service with sudo fallback"""
        try:
            # Try without sudo first
            result = subprocess.run(
                ["systemctl", "start", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector start successful (no sudo)")
                return {"ok": True}, 200
            
            # If failed, try with sudo
            logger.info("detector start failed without sudo, trying with sudo")
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector start successful (with sudo)")
                return {"ok": True}, 200
            else:
                error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                logger.error(f"detector start failed: {error_msg}")
                return {"ok": False, "error": error_msg}, 500
                
        except Exception as e:
            logger.error(f"detector start failed: {e}")
            return {"ok": False, "error": str(e)}, 500

    @app.post("/api/detector/stop")
    def detector_stop():
        """Stop detector service with sudo fallback"""
        try:
            # Try without sudo first
            result = subprocess.run(
                ["systemctl", "stop", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector stop successful (no sudo)")
                return {"ok": True}, 200
            
            # If failed, try with sudo
            logger.info("detector stop failed without sudo, trying with sudo")
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "stop", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector stop successful (with sudo)")
                return {"ok": True}, 200
            else:
                error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                logger.error(f"detector stop failed: {error_msg}")
                return {"ok": False, "error": error_msg}, 500
                
        except Exception as e:
            logger.error(f"detector stop failed: {e}")
            return {"ok": False, "error": str(e)}, 500

    @app.post("/api/detector/restart")
    def detector_restart():
        """Restart detector service with sudo fallback"""
        try:
            # Try without sudo first
            result = subprocess.run(
                ["systemctl", "restart", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector restart successful (no sudo)")
                return {"ok": True}, 200
            
            # If failed, try with sudo
            logger.info("detector restart failed without sudo, trying with sudo")
            result = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", "dd5ka-detector.service"],
                capture_output=True,
                text=True,
                timeout=7
            )
            
            if result.returncode == 0:
                logger.info("detector restart successful (with sudo)")
                return {"ok": True}, 200
            else:
                error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                logger.error(f"detector restart failed: {error_msg}")
                return {"ok": False, "error": error_msg}, 500
                
        except Exception as e:
            logger.error(f"detector restart failed: {e}")
            return {"ok": False, "error": str(e)}, 500

    # LED test endpoint
    @app.post("/api/led/test")
    def led_test():
        """Test LED on BCM GPIO17 with 200ms flash"""
        try:
            # Try to import lgpio
            try:
                import lgpio
            except ImportError:
                logger.error("lgpio not available")
                return {"ok": False, "error": "lgpio not available"}, 500

            # Open GPIO chip
            chip = lgpio.gpiochip_open(0)
            if chip < 0:
                logger.error("Failed to open GPIO chip")
                return {"ok": False, "error": "Failed to open GPIO chip"}, 500

            try:
                # Set GPIO17 as output
                lgpio.gpio_claim_output(chip, 17, 0)
                
                # Flash LED (200ms high, then low)
                lgpio.gpio_write(chip, 17, 1)
                time.sleep(0.2)
                lgpio.gpio_write(chip, 17, 0)
                
                logger.info("LED test successful")
                return {"ok": True}, 200
                
            finally:
                # Clean up
                lgpio.gpio_free(chip, 17)
                lgpio.gpiochip_close(chip)
                
        except Exception as e:
            logger.error(f"LED test failed: {e}")
            return {"ok": False, "error": str(e)}, 500

    # Logs endpoint
    @app.get("/api/logs/last")
    def logs_last():
        """Get last N events from detections.jsonl"""
        try:
            n = min(int(request.args.get('n', 10)), 50)  # Max 50 events
            LOGS_DIR = "/home/nemez/project_root/logs"
            DETECTIONS_JSONL = f"{LOGS_DIR}/detections.jsonl"
            
            events = []
            if os.path.exists(DETECTIONS_JSONL):
                with open(DETECTIONS_JSONL, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # Get last n non-empty lines
                    for line in reversed(lines[-n*2:]):  # Read more lines to account for empty ones
                        line = line.strip()
                        if line and len(events) < n:
                            try:
                                event = json.loads(line)
                                events.append(event)
                            except json.JSONDecodeError:
                                continue  # Skip malformed lines
            
            return {"events": events}, 200
            
        except Exception as e:
            logger.error(f"logs read failed: {e}")
            return {"events": [], "error": str(e)}, 500

    # NetworkManager status (stub)
    @app.get("/api/nm/status")
    def nm_status():
        """NetworkManager status stub"""
        return {
            "mode": "client",
            "ifname": "wlan0", 
            "connected": False,
            "ssid": None
        }, 200

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8098, threaded=True, use_reloader=False)