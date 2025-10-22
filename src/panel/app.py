import json
import logging
import os
import subprocess
import sys
import time
from functools import partial
from os.path import abspath, join, dirname
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

# Add src directory to sys.path for systemd execution
# (systemd runs app.py as script, not as module)
SRC_DIR = abspath(join(dirname(__file__), ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from panel.overlay import OverlayStream
from panel.camera import capture_jpeg, is_camera_busy

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
        """Health check with camera status"""
        try:
            # Check if camera processes are running
            result = subprocess.run(
                ["pgrep", "-f", "rpicam-still"],
                capture_output=True,
                text=True
            )
            
            camera_processes = result.stdout.strip()
            if camera_processes:
                return {
                    "status": "ok",
                    "camera": "busy",
                    "processes": len(camera_processes.split('\n')) if camera_processes else 0
                }, 200
            else:
                return {
                    "status": "ok", 
                    "camera": "ok"
                }, 200
                
        except Exception as e:
            logger.error(f"health check failed: {e}")
            return {
                "status": "error",
                "camera": "error"
            }, 500

    @app.get("/snapshot")
    def snapshot():
        try:
            max_side = int(os.getenv("SNAPSHOT_MAX_SIDE", "960"))
            
            # Check if camera is busy and handle queue
            if is_camera_busy():
                logger.warning("snapshot deferred: busy")
                return jsonify({"error": "camera busy"}), 503
            
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

    @app.get("/overlay.mjpg")
    def overlay_mjpg():
        stream = OverlayStream(logger=app.logger)
        return Response(
            stream.generate_frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame"
        )

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8098, debug=False)