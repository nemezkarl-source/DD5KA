import logging
import os
import subprocess
from flask import Flask, Response, jsonify, send_file, stream_with_context

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
        return jsonify({"last_event": None, "status": "idle"}), 200

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
        try:
            subprocess.run(
                ["/usr/bin/rpicam-still", "-t", "1", "--nopreview", "-o", "/dev/shm/dd5ka_snapshot.jpg"],
                timeout=4,
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
        def generate_frames():
            proc = None
            try:
                logger.info("stream start")
                proc = subprocess.Popen(
                    ["/usr/bin/rpicam-vid", "--codec", "mjpeg", "-t", "0", "-o", "-", "--inline"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True
                )
                
                if proc.poll() is not None:
                    raise FileNotFoundError("rpicam-vid failed to start")
                
                buffer = b""
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    
                    buffer += chunk
                    
                    # Find JPEG frames (SOI 0xFFD8 to EOI 0xFFD9)
                    while True:
                        soi = buffer.find(b'\xff\xd8')
                        if soi == -1:
                            break
                        
                        eoi = buffer.find(b'\xff\xd9', soi)
                        if eoi == -1:
                            break
                        
                        frame = buffer[soi:eoi + 2]
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

