import json
import logging
import os
import subprocess
import sys
import time
import threading
import requests
from datetime import datetime
from functools import partial
from os.path import abspath, join, dirname
from flask import Flask, Response, jsonify, request, send_file, stream_with_context, make_response, render_template, send_from_directory
from PIL import Image

# Add src directory to sys.path for systemd execution
# (systemd runs app.py as script, not as module)
SRC_DIR = abspath(join(dirname(__file__), ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from panel.overlay import OverlayStream
from panel.camera import capture_jpeg, ensure_grabber, get_grabber_frame

# LED Configuration
LED_CHIP_INDEX = 0
LED_PIN_BCM = 17
LED_ACTIVE_HIGH = True  # 1=ON, 0=OFF
DETECTIONS_FILE = "/home/nemez/project_root/logs/detections.jsonl"
LED_LAST_OK_FILE = "/home/nemez/project_root/logs/last_led_ok.txt"
LED_CLASSES = [cls.strip().lower() for cls in (os.getenv("LED_DETECTION_CLASSES") or "drone").split(",")]

# Gallery Configuration
GALLERY_DIR = "/home/nemez/project_root/logs/gallery"
THUMBS_DIR = f"{GALLERY_DIR}/thumbs"
GALLERY_MAX_ITEMS = 1000

class LedBlinker:
    """Thread-safe LED blinking with GPIO control"""
    
    def __init__(self, logger):
        self.logger = logger
        self._blink_lock = threading.Lock()
    
    def blink(self, duration_s=1.0):
        """Blink LED for specified duration (thread-safe)"""
        # Try to acquire lock, return immediately if already blinking
        if not self._blink_lock.acquire(blocking=False):
            self.logger.info("LED blink already in progress, skipping")
            return False
            
        try:
            # Try to import lgpio
            try:
                import lgpio
            except ImportError:
                self.logger.error("lgpio not available")
                return False

            # Open GPIO chip
            chip = lgpio.gpiochip_open(LED_CHIP_INDEX)
            if chip < 0:
                self.logger.error("Failed to open GPIO chip")
                return False

            try:
                # Set GPIO pin as output, start in OFF state
                lgpio.gpio_claim_output(chip, LED_PIN_BCM, 0)
                
                # Turn ON
                if LED_ACTIVE_HIGH:
                    lgpio.gpio_write(chip, LED_PIN_BCM, 1)
                else:
                    lgpio.gpio_write(chip, LED_PIN_BCM, 0)
                
                # Wait for duration
                time.sleep(duration_s)
                
                # Turn OFF
                if LED_ACTIVE_HIGH:
                    lgpio.gpio_write(chip, LED_PIN_BCM, 0)
                else:
                    lgpio.gpio_write(chip, LED_PIN_BCM, 1)
                
                # Write success timestamp
                try:
                    with open(LED_LAST_OK_FILE, 'w') as f:
                        f.write(datetime.utcnow().isoformat() + 'Z')
                except Exception as e:
                    self.logger.warning(f"Failed to write LED timestamp: {e}")
                
                self.logger.info(f"LED blinked for {duration_s}s")
                return True
                
            finally:
                # Clean up GPIO
                try:
                    lgpio.gpio_free(chip, LED_PIN_BCM)
                    lgpio.gpiochip_close(chip)
                except Exception as e:
                    self.logger.error(f"GPIO cleanup failed: {e}")
                    
        except Exception as e:
            self.logger.error(f"LED blink failed: {e}")
            return False
        finally:
            self._blink_lock.release()

class DetectionTailThread:
    """Background thread to monitor detection file and trigger LED blinks"""
    
    def __init__(self, led_blinker, logger):
        self.led_blinker = led_blinker
        self.logger = logger
        self._stop_event = threading.Event()
        self._last_position = 0
        self._last_inode = None
        self._last_blink_time = 0
        self._debounce_ms = 1000  # Don't blink more than once per second
        
    def start(self):
        """Start the detection monitoring thread"""
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        self.logger.info("Detection tail thread started")
        
    def stop(self):
        """Stop the detection monitoring thread"""
        self._stop_event.set()
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2)
        self.logger.info("Detection tail thread stopped")
        
    def _monitor_loop(self):
        """Main monitoring loop"""
        while not self._stop_event.is_set():
            try:
                self._check_detections()
                time.sleep(0.25)  # Check every 250ms
        except Exception as e:
            self.logger.error(f"Detection monitoring error: {e}")
            time.sleep(1)

class GalleryCollector:
    """Background thread that monitors detections.jsonl and collects gallery images"""
    
    def __init__(self, logger):
        self.logger = logger
        self.running = False
        self.thread = None
        self.last_inode = None
        
    def start(self):
        """Start the gallery collector thread"""
        if self.running:
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._collect_loop, daemon=True)
        self.thread.start()
        self.logger.info("Gallery collector started")
        
    def stop(self):
        """Stop the gallery collector thread"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        self.logger.info("Gallery collector stopped")
        
    def _collect_loop(self):
        """Main collection loop"""
        while self.running:
            try:
                self._tail_detections()
                time.sleep(1)  # Check every second
            except Exception as e:
                self.logger.error(f"Gallery collector error: {e}")
                time.sleep(5)  # Wait longer on error
                
    def _tail_detections(self):
        """Tail detections.jsonl file and process new lines"""
        try:
            if not os.path.exists(DETECTIONS_FILE):
                return
                
            # Check if file was rotated/truncated
            current_inode = os.stat(DETECTIONS_FILE).st_ino
            if self.last_inode is not None and current_inode != self.last_inode:
                self.logger.info("Detections file rotated, reconnecting")
                self.last_inode = current_inode
                return
                
            self.last_inode = current_inode
            
            # Read new lines from EOF
            with open(DETECTIONS_FILE, 'r') as f:
                f.seek(0, 2)  # Seek to EOF
                while self.running:
                    line = f.readline()
                    if not line:
                        break
                    self._process_detection(line.strip())
                    
        except Exception as e:
            self.logger.error(f"Tail detections failed: {e}")
            
    def _process_detection(self, line):
        """Process a single detection line"""
        try:
            if not line:
                return
                
            data = json.loads(line)
            if not data.get('detections'):
                return
                
            # Download overlay image
            timestamp = datetime.now()
            filename = timestamp.strftime("%Y%m%d_%H%M%S%f")[:-3] + ".jpg"  # milliseconds
            filepath = os.path.join(GALLERY_DIR, filename)
            
            # Download from overlay endpoint
            response = requests.get("http://127.0.0.1:8098/overlay.jpg", timeout=5)
            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                    
                # Create thumbnail
                self._create_thumbnail(filepath, filename)
                
                # Cleanup old files
                self._cleanup_old_files()
                
                self.logger.info(f"Saved gallery image: {filename}")
            else:
                self.logger.warning(f"Failed to download overlay: {response.status_code}")
                
        except Exception as e:
            self.logger.error(f"Process detection failed: {e}")
            
    def _create_thumbnail(self, image_path, filename):
        """Create thumbnail for gallery image"""
        try:
            thumb_path = os.path.join(THUMBS_DIR, filename)
            
            with Image.open(image_path) as img:
                # Calculate thumbnail size (max 320px on larger side)
                img.thumbnail((320, 320), Image.Resampling.LANCZOS)
                img.save(thumb_path, 'JPEG', quality=85)
                
        except Exception as e:
            self.logger.error(f"Create thumbnail failed: {e}")
            
    def _cleanup_old_files(self):
        """Remove old files to maintain GALLERY_MAX_ITEMS limit"""
        try:
            # Get all files sorted by modification time (oldest first)
            files = []
            for filename in os.listdir(GALLERY_DIR):
                if filename.endswith('.jpg'):
                    filepath = os.path.join(GALLERY_DIR, filename)
                    mtime = os.path.getmtime(filepath)
                    files.append((mtime, filename))
                    
            files.sort()  # Oldest first
            
            # Remove excess files
            while len(files) > GALLERY_MAX_ITEMS:
                _, old_filename = files.pop(0)
                
                # Remove original
                old_filepath = os.path.join(GALLERY_DIR, old_filename)
                if os.path.exists(old_filepath):
                    os.remove(old_filepath)
                    
                # Remove thumbnail
                old_thumbpath = os.path.join(THUMBS_DIR, old_filename)
                if os.path.exists(old_thumbpath):
                    os.remove(old_thumbpath)
                    
                self.logger.info(f"Cleaned up old file: {old_filename}")
                
        except Exception as e:
            self.logger.error(f"Cleanup old files failed: {e}")
                
    def _check_detections(self):
        """Check for new detections and trigger LED if needed"""
        try:
            if not os.path.exists(DETECTIONS_FILE):
                return
                
            # Get current file stats
            stat = os.stat(DETECTIONS_FILE)
            current_inode = stat.st_ino
            current_size = stat.st_size
            
            # Initialize position to EOF if first time
            if self._last_inode is None:
                self._last_position = current_size
                self._last_inode = current_inode
                self.logger.info("Detection tail started from EOF")
                return
                
            # If file was rotated/truncated, reset position
            if self._last_inode != current_inode:
                self._last_position = 0
                self._last_inode = current_inode
                self.logger.info("Detection file rotated, resetting position")
                
            # If file is smaller than last position, it was truncated
            if current_size < self._last_position:
                self._last_position = 0
                self.logger.info("Detection file truncated, resetting position")
                
            # Read new content
            if current_size > self._last_position:
                with open(DETECTIONS_FILE, 'r') as f:
                    f.seek(self._last_position)
                    new_content = f.read()
                    self._last_position = current_size
                    
                    # Check for valid JSON lines with class filtering
                    lines = new_content.strip().split('\n')
                    matching_detections = 0
                    
                    for line in lines:
                        line = line.strip()
                        if line:
                            try:
                                event = json.loads(line)
                                # Check if this is a detection event with matching class
                                if self._should_trigger_led(event):
                                    matching_detections += 1
                            except json.JSONDecodeError:
                                continue
                    
                    # Trigger LED if we have matching detections
                    if matching_detections > 0:
                        current_time = time.time() * 1000  # Convert to ms
                        if current_time - self._last_blink_time >= self._debounce_ms:
                            self.logger.info(f"New drone detection(s) found, triggering LED blink")
                            self.led_blinker.blink(1.0)
                            self._last_blink_time = current_time
                        else:
                            self.logger.debug("LED blink debounced")
                            
        except Exception as e:
            self.logger.error(f"Error checking detections: {e}")
            
    def _should_trigger_led(self, event):
        """Check if event should trigger LED based on class filtering"""
        try:
            # Check if this is a detection event
            if event.get("type") != "detection":
                return False
                
            # Get detections array
            detections = event.get("detections", [])
            if not detections:
                return False
                
            # Check each detection for matching class
            for detection in detections:
                # Try different possible class field names
                class_name = (detection.get("class") or 
                             detection.get("label") or 
                             detection.get("name") or 
                             detection.get("class_name") or "").strip().lower()
                
                if class_name in LED_CLASSES:
                    return True
                    
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking detection class: {e}")
            return False

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
    
    # Initialize LED components
    led_blinker = LedBlinker(logger)
    detection_tail = DetectionTailThread(led_blinker, logger)
    detection_tail.start()
    
    # Initialize gallery components
    os.makedirs(GALLERY_DIR, exist_ok=True)
    os.makedirs(THUMBS_DIR, exist_ok=True)
    gallery_collector = GalleryCollector(logger)
    gallery_collector.start()
    
    # Store references for cleanup
    app.led_blinker = led_blinker
    app.detection_tail = detection_tail
    app.gallery_collector = gallery_collector
    
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
            # Use rpicam-vid for MJPEG streaming with proper cleanup
            cmd = [
                "/usr/bin/rpicam-vid",
                "-n",  # no preview
                "-t", "0",  # continuous
                "--width", str(width),
                "--height", str(height),
                "--framerate", "15",
                "--bitrate", "2000000",  # 2Mbps
                "--codec", "mjpeg",  # Explicit MJPEG codec
                "--inline",  # inline headers
                "-o", "-"  # output to stdout
            ]
            
            def generate():
                process = None
                try:
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                        start_new_session=True  # Prevent zombie processes
                    )
                    
                    # Stream MJPEG data
                    while True:
                        chunk = process.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                        
                except GeneratorExit:
                    # Client disconnected, clean up process
                    logger.info("Stream client disconnected, cleaning up process")
                except Exception as e:
                    logger.error(f"stream failed: {e}")
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    yield b"Stream error"
                    yield b"\r\n"
                finally:
                    if process:
                        try:
                            # Close stdout first
                            if process.stdout:
                                process.stdout.close()
                            # Terminate process gracefully
                            process.terminate()
                            # Wait for termination with timeout
                            try:
                                process.wait(timeout=1.0)
                            except subprocess.TimeoutExpired:
                                # Force kill if graceful termination failed
                                logger.warning("Process termination timeout, force killing")
                                import signal
                                import os
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                except (OSError, ProcessLookupError):
                                    pass  # Process already dead
                        except Exception as cleanup_error:
                            logger.error(f"Process cleanup failed: {cleanup_error}")
            
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
        """Test LED with 1s blink"""
        try:
            success = app.led_blinker.blink(1.0)
            if success:
                return {"ok": True}, 200
            else:
                return {"ok": False, "error": "LED blink failed"}, 500
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

    # LED status endpoint
    @app.get("/api/led/status")
    def led_status():
        """Get LED status based on last successful blink"""
        try:
            if os.path.exists(LED_LAST_OK_FILE):
                with open(LED_LAST_OK_FILE, 'r') as f:
                    last_ok_ts = f.read().strip()
                
                # Check if timestamp is within last 10 minutes
                try:
                    last_ok_time = datetime.fromisoformat(last_ok_ts.replace('Z', '+00:00'))
                    now = datetime.utcnow().replace(tzinfo=last_ok_time.tzinfo)
                    time_diff = (now - last_ok_time).total_seconds()
                    ok = time_diff < 600  # 10 minutes
                except Exception:
                    ok = False
            else:
                last_ok_ts = None
                ok = False
                
            return {
                "ok": ok,
                "last_ok_ts": last_ok_ts
            }, 200
            
        except Exception as e:
            logger.error(f"LED status check failed: {e}")
            return {
                "ok": False,
                "last_ok_ts": None
            }, 500

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

    # Gallery routes
    @app.get("/photos")
    def photos():
        """Photos gallery page"""
        return render_template('photos.html')

    @app.get("/api/gallery/index")
    def gallery_index():
        """Get gallery index with pagination"""
        try:
            n = int(request.args.get('n', 60))
            offset = int(request.args.get('offset', 0))
            
            # Get all gallery files
            files = []
            if os.path.exists(GALLERY_DIR):
                for filename in os.listdir(GALLERY_DIR):
                    if filename.endswith('.jpg'):
                        filepath = os.path.join(GALLERY_DIR, filename)
                        if os.path.exists(filepath):
                            mtime = os.path.getmtime(filepath)
                            size = os.path.getsize(filepath)
                            files.append({
                                'file': filename,
                                'ts': mtime,
                                'size': size
                            })
            
            # Sort by timestamp descending (newest first)
            files.sort(key=lambda x: x['ts'], reverse=True)
            
            # Apply pagination
            total = len(files)
            files = files[offset:offset + n]
            
            return {
                'files': files,
                'total': total
            }, 200
            
        except Exception as e:
            logger.error(f"Gallery index failed: {e}")
            return {'error': str(e)}, 500

    @app.get("/gallery/<filename>")
    def gallery_image(filename):
        """Serve gallery image"""
        try:
            return send_from_directory(GALLERY_DIR, filename)
        except Exception as e:
            logger.error(f"Gallery image failed: {e}")
            return {'error': 'File not found'}, 404

    @app.get("/gallery/thumb/<filename>")
    def gallery_thumb(filename):
        """Serve gallery thumbnail"""
        try:
            return send_from_directory(THUMBS_DIR, filename)
        except Exception as e:
            logger.error(f"Gallery thumb failed: {e}")
            return {'error': 'Thumbnail not found'}, 404

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8098, threaded=True, use_reloader=False)

# CHANGELOG
# Добавлена страница "Фото детекций" с галереей изображений:
# - GalleryCollector: фоновый поток для сбора изображений из detections.jsonl
# - API endpoints: /api/gallery/index, /gallery/<filename>, /gallery/thumb/<filename>
# - Автоматическое создание миниатюр и ретенция файлов (GALLERY_MAX_ITEMS=1000)
# - Устойчивость к log-rotate и обработка ошибок