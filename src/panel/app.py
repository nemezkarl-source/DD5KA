import logging
import os
import subprocess
from flask import Flask, Response, jsonify, send_file

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
                ["rpicam-still", "-t", "1", "--nopreview", "-o", "/dev/shm/dd5ka_snapshot.jpg"],
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

    @app.get("/")
    def index():
        return Response("DD-5KA Panel: OK\n/healthz -> 200", mimetype="text/plain")

    return app

if __name__ == "__main__":
    # Локальный запуск для разработки: python app.py
    app = create_app()
    app.run(host="0.0.0.0", port=8098, debug=False, use_reloader=False)

