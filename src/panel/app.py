import logging
import os
from flask import Flask, Response

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

    @app.get("/")
    def index():
        return Response("DD-5KA Panel: OK\n/healthz -> 200", mimetype="text/plain")

    return app

if __name__ == "__main__":
    # Локальный запуск для разработки: python app.py
    app = create_app()
    app.run(host="0.0.0.0", port=8098)

