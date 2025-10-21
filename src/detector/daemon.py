#!/usr/bin/env python3
"""
DD-5KA Detector Daemon (CH4)
Heartbeat polling of panel /snapshot endpoint
"""

import json
import logging
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime
from os.path import abspath, join, dirname

# Add src directory to sys.path for systemd execution
# (systemd runs daemon.py as script, not as module)
SRC_DIR = abspath(join(dirname(__file__), ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from detector.yolo_cpu import YOLOCPUInference


class DetectorDaemon:
    def __init__(self):
        # Environment variables with defaults
        self.panel_base_url = os.getenv('PANEL_BASE_URL', 'http://127.0.0.1:8098')
        self.poll_sec = max(1, min(60, int(os.getenv('DETECTOR_POLL_SEC', '5'))))
        self.log_dir = os.getenv('LOG_DIR', 'logs')
        self.backend = os.getenv('DD5KA_BACKEND', 'stub')
        
        # Ensure log directory exists
        os.makedirs(self.log_dir, exist_ok=True)
        
        # Setup logging
        self.logger = logging.getLogger("detector")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        
        if not self.logger.handlers:
            handler = logging.FileHandler(f"{self.log_dir}/detector.log", mode="a")
            formatter = logging.Formatter('%(asctime)s - detector - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        # Detection log file
        self.detections_file = f"{self.log_dir}/detections.jsonl"
        
        # Initialize YOLO inference if CPU backend
        self.yolo_inference = None
        if self.backend == 'cpu':
            model_path = "/home/nemez/DD5KA/models/cpu/best.pt"
            self.yolo_inference = YOLOCPUInference(model_path, self.logger)
        
        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        self.running = True
        
    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM gracefully"""
        self.logger.info("detector stopping")
        self.running = False
        
    def _write_detection(self, ok, error_msg=None, detection_data=None):
        """Write detection event to JSONL file"""
        if self.backend == 'cpu' and detection_data:
            # CPU inference event
            event = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "type": "detection",
                "backend": "cpu",
                "model": {
                    "path": "/home/nemez/DD5KA/models/cpu/best.pt",
                    "framework": "ultralytics",
                    "version": "auto"
                },
                "image": detection_data.get("image", {}),
                "detections": detection_data.get("detections", [])
            }
            if "error" in detection_data:
                event["error"] = detection_data["error"]
        else:
            # Heartbeat event (stub mode or fallback)
            event = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "type": "heartbeat",
                "ok": ok
            }
            if not ok and error_msg:
                event["error"] = error_msg
                
        try:
            with open(self.detections_file, 'a', encoding='utf-8', buffering=1) as f:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')
                f.flush()
        except Exception as e:
            self.logger.error(f"Failed to write detection: {e}")
    
    def _poll_panel(self):
        """Poll panel /snapshot endpoint"""
        url = f"{self.panel_base_url}/snapshot"
        
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    jpeg_data = response.read()
                    
                    if self.backend == 'cpu' and self.yolo_inference:
                        # CPU inference mode
                        detection_data = self.yolo_inference.infer_from_jpeg(jpeg_data)
                        self._write_detection(True, detection_data=detection_data)
                        return True
                    else:
                        # Stub mode
                        self.logger.info("detector heartbeat (snapshot ok)")
                        self._write_detection(True)
                        return True
                else:
                    error_msg = f"HTTP {response.status}"
                    self.logger.warning(f"detector heartbeat failed: {error_msg}")
                    self._write_detection(False, error_msg)
                    return False
                    
        except urllib.error.URLError as e:
            error_msg = f"URL error: {str(e)}"
            self.logger.warning(f"detector heartbeat failed: {error_msg}")
            self._write_detection(False, error_msg)
            return False
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.warning(f"detector heartbeat failed: {error_msg}")
            self._write_detection(False, error_msg)
            return False
    
    def run(self):
        """Main daemon loop"""
        self.logger.info(f"detector daemon starting (poll_sec={self.poll_sec}, panel={self.panel_base_url}, backend={self.backend})")
        
        while self.running:
            try:
                self._poll_panel()
            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
            
            # Sleep with interruption check
            for _ in range(self.poll_sec * 10):  # Check every 100ms
                if not self.running:
                    break
                time.sleep(0.1)
        
        self.logger.info("detector daemon stopped")


def main():
    daemon = DetectorDaemon()
    daemon.run()


if __name__ == "__main__":
    main()
