import json
import logging
import os
import subprocess
import time
from functools import partial
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

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
                    return jsonify({"event": None}), 200
                
                # Read backwards in blocks to find last non-empty line
                buffer = b""
                block_size = 8192
                pos = file_size
                
                while pos > 0:
                    read_size = min(block_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    buffer = chunk + buffer
                    
                    # Find last complete line
                    lines = buffer.split(b'\n')
                    if len(lines) > 1:
                        # Check if last line is non-empty
                        last_line = lines[-1].strip()
                        if last_line:
                            try:
                                event = json.loads(last_line.decode('utf-8'))
                                return jsonify({"event": event}), 200
                            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                                logger.warning(f"Failed to parse last event: {e}")
                                return jsonify({"event": None}), 200
                        else:
                            # Last line is empty, check previous line
                            for line in reversed(lines[:-1]):
                                line = line.strip()
                                if line:
                                    try:
                                        event = json.loads(line.decode('utf-8'))
                                        return jsonify({"event": event}), 200
                                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                                        logger.warning(f"Failed to parse event: {e}")
                                        return jsonify({"event": None}), 200
                            break
                
                return jsonify({"event": None}), 200
                
        except FileNotFoundError:
            return jsonify({"event": None}), 200
        except Exception as e:
            logger.warning(f"Failed to read detections file: {e}")
            return jsonify({"event": None}), 200

    @app.get("/api/health")
    def api_health():
        logger.info("health check")
        
        # Check camera status
        camera_status = "error"
        try:
            # Check if rpicam processes are running
            result = subprocess.run(["pgrep", "-f", "rpicam"], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                camera_status = "busy"
            else:
                # Check if media devices are accessible
                try:
                    with open("/dev/media0", "rb") as f:
                        pass
                    camera_status = "ok"
                except (FileNotFoundError, PermissionError):
                    camera_status = "error"
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            camera_status = "error"
        
        # Check detector status
        detector_status = "stopped"
        try:
            result = subprocess.run(["systemctl", "is-active", "dd5ka-detector.service"], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                status = result.stdout.strip()
                if status == "active":
                    detector_status = "running"
                elif status in ["activating", "reloading"]:
                    detector_status = "starting"
                else:
                    detector_status = "stopped"
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
            detector_status = "stopped"
        
        return jsonify({
            "camera": camera_status,
            "detector": detector_status
        }), 200

    @app.get("/api/snapshot")
    def api_snapshot():
        return jsonify({
            "timestamp": None,
            "label": None,
            "confidence": None,
            "image": None
        }), 200

    @app.get("/snapshot")
    def snapshot():
        def soft_guard():
            """Soft kill of rpicam processes"""
            subprocess.run(["pkill", "-f", "/usr/bin/rpicam-vid"], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "rpicam-jpeg"], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "/usr/bin/rpicam-still"], 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Soft guard before first attempt
        soft_guard()
        time.sleep(0.25)
        
        # First attempt
        try:
            subprocess.run(
                ["/usr/bin/rpicam-still", "-t", "2", "--nopreview", "-o", "/dev/shm/dd5ka_snapshot.jpg"],
                timeout=5,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info("snapshot captured")
            return send_file("/dev/shm/dd5ka_snapshot.jpg", mimetype="image/jpeg"), 200
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.warning(f"snapshot failed: {type(e).__name__}")
            
            # Retry: soft guard again and retry
            soft_guard()
            time.sleep(0.3)
            
            try:
                subprocess.run(
                    ["/usr/bin/rpicam-still", "-t", "2", "--nopreview", "-o", "/dev/shm/dd5ka_snapshot.jpg"],
                    timeout=5,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                logger.info("snapshot captured")
                return send_file("/dev/shm/dd5ka_snapshot.jpg", mimetype="image/jpeg"), 200
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning(f"snapshot failed: {type(e).__name__}")
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
            safe_mode = "no"
        else:
            safe_mode = "yes"
        
        fps = max(1, min(30, int(request.args.get('fps', 10))))
        quality = max(10, min(100, int(request.args.get('quality', 80))))
        
        def generate_frames():
            proc = None
            try:
                logger.info(f"stream start w={width} h={height} fps={fps} q={quality} (safe={safe_mode})")
                
                # Soft guard: kill existing rpicam-vid processes
                subprocess.run(["pkill", "-f", "/usr/bin/rpicam-vid"], 
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                proc = subprocess.Popen(
                    ["/usr/bin/rpicam-vid", "--codec", "mjpeg", "--inline", "--nopreview", 
                     "--width", str(width), "--height", str(height), "--framerate", str(fps),
                     "--quality", str(quality), "-t", "0", "-o", "-"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True
                )
                
                # Extended grace period for startup (500-800ms)
                time.sleep(0.7)
                if proc.poll() is not None:
                    logger.warning(f"rpicam-vid early exit, returncode: {proc.returncode}")
                    raise FileNotFoundError("rpicam-vid failed to start")
                
                buffer = bytearray()
                for chunk in iter(partial(proc.stdout.read, 4096), b""):
                    if not chunk:
                        break
                    
                    buffer.extend(chunk)
                    
                    # Find JPEG frames (SOI 0xFFD8 to EOI 0xFFD9)
                    while True:
                        soi = buffer.find(b'\xff\xd8')
                        if soi == -1:
                            break
                        
                        eoi = buffer.find(b'\xff\xd9', soi)
                        if eoi == -1:
                            break
                        
                        frame = bytes(buffer[soi:eoi + 2])
                        buffer = buffer[eoi + 2:]
                        
                        if len(frame) > 0:
                            yield f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode() + frame + b"\r\n"
                            
            except Exception as e:
                logger.warning(f"stream error: {type(e).__name__}")
            finally:
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    logger.info("stream stop")
        
        try:
            return Response(
                stream_with_context(generate_frames()),
                mimetype='multipart/x-mixed-replace; boundary=frame',
                headers={'Cache-Control': 'no-store'}
            )
        except FileNotFoundError:
            return jsonify({"error": "stream unavailable"}), 500

    @app.get("/")
    def index():
        return Response("DD-5KA Panel: OK\n/healthz -> 200", mimetype="text/plain")

    return app

if __name__ == "__main__":
    # Локальный запуск для разработки: python app.py
    app = create_app()
    app.run(host="0.0.0.0", port=8098, debug=False, use_reloader=False)

