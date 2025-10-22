#!/usr/bin/env python3
"""
DD-5KA Detector Daemon (CH4)
Heartbeat polling of panel /snapshot endpoint
"""

import hashlib
import json
import logging
import os
import random
import signal
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from os.path import abspath, join, dirname
from io import BytesIO
from PIL import Image

# Add src directory to sys.path for systemd execution
# (systemd runs daemon.py as script, not as module)
SRC_DIR = abspath(join(dirname(__file__), ".."))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Environment variables
MODEL = os.getenv("DETECTOR_MODEL", "").strip()
BACK = os.getenv("DETECTOR_BACKEND", "stub").strip().lower()
CONF = float(os.getenv("DETECTOR_CONF_MIN", "0.25"))
DET_PATH = os.getenv("DETECTIONS_PATH", "/home/nemez/DD5KA/logs/detections.jsonl")

# Import ultralytics only if needed
if BACK == "cpu":
    try:
        from ultralytics import YOLO
        ULTRALYTICS_AVAILABLE = True
    except ImportError:
        ULTRALYTICS_AVAILABLE = False
        print("ERROR: ultralytics not available for CPU backend")
        sys.exit(1)


class DetectorDaemon:
    def __init__(self):
        # Environment variables with defaults
        self.panel_base_url = os.getenv('PANEL_BASE_URL', 'http://127.0.0.1:8098')
        self.poll_sec = max(1, min(60, int(os.getenv('DETECTOR_POLL_SEC', '5'))))
        self.log_dir = os.getenv('LOG_DIR', 'logs')
        self.backend = BACK  # Use the global BACK variable
        
        # Retry configuration
        self.retry_base_ms = int(os.getenv('DETECTOR_RETRY_BASE_MS', '240'))
        self.retry_jitter = float(os.getenv('DETECTOR_RETRY_JITTER', '0.2'))
        self.fail_extra_ms = int(os.getenv('DETECTOR_FAIL_EXTRA_MS', '180'))
        
        # CPU inference parameters
        self.conf_min = CONF  # Use the global CONF variable
        self.allow_classes = os.getenv('DETECTOR_CLASS_ALLOW', 'drone,dron,дрон,uav')
        self.max_side = int(os.getenv('IMG_MAX_SIDE', '1280'))
        
        # Snapshot saving parameters
        self.save_dir = os.getenv('DETECTOR_SAVE_DIR', '/home/nemez/project_root/snaps')
        self.save_min_conf = float(os.getenv('DETECTOR_SAVE_MIN_CONF', '0.55'))
        
        # Alert debounce parameters
        self.alert_min_conf = float(os.getenv('DETECTOR_ALERT_MIN_CONF', '0.60'))
        self.alert_consec = int(os.getenv('DETECTOR_ALERT_CONSEC', '2'))
        
        # Debounce state
        self._last_boxes = []
        self._consecutive_count = 0
        
        # Class ID filter
        class_ids_str = os.getenv('DETECTOR_CLASS_IDS', '')
        self.class_id_allow = None
        if class_ids_str:
            try:
                self.class_id_allow = set(int(id.strip()) for id in class_ids_str.split(','))
            except ValueError:
                self.logger.warning(f"Invalid DETECTOR_CLASS_IDS: {class_ids_str}")
                self.class_id_allow = None
        
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
        
        # Detection log file - use DET_PATH
        self.detections_file = DET_PATH
        
        # Initialize model for CPU backend
        self.model = None
        if self.backend == 'cpu':
            try:
                self.logger.info(f"loading model from {MODEL}")
                self.model = YOLO(MODEL)
                # Sanity ping of class names
                names = getattr(getattr(self.model, "model", self.model), "names", {}) or {0: "drone"}
                self.logger.info("model loaded successfully")
            except Exception as e:
                self.logger.error(f"failed to load model: {e}")
                sys.exit(1)  # No fallback to stub
        
        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Log configuration
        self.logger.info(f"detector conf_min={self.conf_min}")
        
        self.running = True
        
    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM gracefully"""
        self.logger.info("detector stopping")
        self.running = False
        
    def _calculate_iou(self, box1, box2):
        """Calculate Intersection over Union (IoU) for two bounding boxes"""
        # box format: [x1, y1, x2, y2]
        x1_1, y1_1, x2_1, y2_1 = box1
        x1_2, y1_2, x2_2, y2_2 = box2
        
        # Calculate intersection
        x1_i = max(x1_1, x1_2)
        y1_i = max(y1_1, y1_2)
        x2_i = min(x2_1, x2_2)
        y2_i = min(y2_1, y2_2)
        
        if x2_i <= x1_i or y2_i <= y1_i:
            return 0.0
        
        intersection = (x2_i - x1_i) * (y2_i - y1_i)
        
        # Calculate areas
        area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
        area2 = (x2_2 - x1_2) * (y2_2 - y1_2)
        union = area1 + area2 - intersection
        
        return intersection / union if union > 0 else 0.0
    
    def _save_snapshot(self, jpeg_data, detection_data):
        """Save snapshot if any detection meets save threshold"""
        if not detection_data or "detections" not in detection_data:
            return None, None
        
        # Check if any detection meets save threshold
        should_save = any(det.get("conf", 0) >= self.save_min_conf for det in detection_data["detections"])
        if not should_save:
            return None, None
        
        try:
            # Calculate SHA1
            sha1_hash = hashlib.sha1(jpeg_data).hexdigest()
            
            # Create directory structure: YYYY/MM/DD/
            now = datetime.utcnow()
            date_path = os.path.join(
                self.save_dir,
                now.strftime("%Y"),
                now.strftime("%m"),
                now.strftime("%d")
            )
            os.makedirs(date_path, exist_ok=True)
            
            # Generate filename: ts_sha1.jpg
            timestamp = int(now.timestamp())
            filename = f"{timestamp}_{sha1_hash}.jpg"
            filepath = os.path.join(date_path, filename)
            
            # Save file
            with open(filepath, 'wb') as f:
                f.write(jpeg_data)
            
            self.logger.info(f"saved snapshot {filename}")
            return filepath, sha1_hash
            
        except Exception as e:
            self.logger.error(f"failed to save snapshot: {e}")
            return None, None
    
    def _check_alert_debounce(self, detection_data, image_path, image_sha1):
        """Check for alert conditions with debounce logic"""
        if not detection_data or "detections" not in detection_data:
            # Reset on empty detections
            self._last_boxes = []
            self._consecutive_count = 0
            return False
        
        current_boxes = []
        current_ts = datetime.utcnow().isoformat() + "Z"
        
        # Filter detections by alert confidence
        alert_detections = [det for det in detection_data["detections"] 
                           if det.get("conf", 0) >= self.alert_min_conf]
        
        if not alert_detections:
            # Reset if no high-confidence detections
            self._last_boxes = []
            self._consecutive_count = 0
            return False
        
        # Extract bounding boxes
        for det in alert_detections:
            if "bbox_xyxy" in det:
                current_boxes.append({
                    "bbox": det["bbox_xyxy"],
                    "conf": det.get("conf", 0),
                    "ts": current_ts
                })
        
        # Check for IoU overlap with previous frame
        has_overlap = False
        if self._last_boxes:
            for current_box in current_boxes:
                for last_box in self._last_boxes:
                    iou = self._calculate_iou(current_box["bbox"], last_box["bbox"])
                    if iou >= 0.5:
                        has_overlap = True
                        break
                if has_overlap:
                    break
        
        if has_overlap:
            self._consecutive_count += 1
        else:
            self._consecutive_count = 0
        
        # Update last boxes
        self._last_boxes = current_boxes
        
        # Check if alert should fire
        if self._consecutive_count >= self.alert_consec:
            # Fire alert
            alert_event = {
                "ts": current_ts,
                "type": "alert",
                "backend": "cpu",
                "image": {
                    "width": detection_data.get("image", {}).get("width", 0),
                    "height": detection_data.get("image", {}).get("height", 0)
                },
                "detections": alert_detections,
                "criteria": {
                    "consec": self.alert_consec,
                    "iou_min": 0.5,
                    "min_conf": self.alert_min_conf
                }
            }
            
            # Add image path and sha1 if available
            if image_path and image_sha1:
                alert_event["image"]["path"] = image_path
                alert_event["image"]["sha1"] = image_sha1
            
            # Write alert event
            try:
                with open(self.detections_file, 'a', encoding='utf-8', buffering=1) as f:
                    f.write(json.dumps(alert_event, ensure_ascii=False) + '\n')
                    f.flush()
                
                self.logger.info(f"alert fired (consec={self._consecutive_count}, dets={len(alert_detections)})")
                
                # Reset counter to avoid spam
                self._consecutive_count = 0
                return True
                
            except Exception as e:
                self.logger.error(f"failed to write alert: {e}")
        
        return False
        
    def _write_detection(self, ok, error_msg=None, detection_data=None):
        """Write detection event to JSONL file"""
        if self.backend == 'cpu' and detection_data:
            # CPU inference event
            event = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "type": "detection",
                "backend": "cpu",
                "model": {
                    "path": MODEL,
                    "framework": "ultralytics",
                    "version": "auto"
                },
                "image": detection_data.get("image", {}),
                "detections": detection_data.get("detections", [])
            }
            if "error" in detection_data:
                event["error"] = detection_data["error"]
        else:
            # Heartbeat event (stub mode only)
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
    
    def _handle_http_error(self, code):
        """Handle HTTP error codes consistently"""
        if code in [500, 503]:
            # Transient errors - log as INFO
            if code == 503:
                error_msg = "transient: HTTP 503 busy"
                self.logger.info("transient: HTTP 503 busy, retrying")
            else:
                error_msg = "transient: HTTP 500"
                self.logger.info(f"transient: HTTP {code} (busy/fail), retrying once")
            
            self._write_detection(False, error_msg)
            return False
        else:
            # Other HTTP errors - log as WARNING
            error_msg = f"HTTP {code}"
            self.logger.warning(f"detector heartbeat failed: {error_msg}")
            self._write_detection(False, error_msg)
            return False
    
    def _poll_panel(self):
        """Poll panel /snapshot endpoint with retry logic"""
        url = f"{self.panel_base_url}/snapshot"
        
        # First attempt
        success = self._attempt_snapshot(url)
        if success:
            return True
        
        # Retry attempt for 500/503 errors
        self.logger.info("transient: HTTP 500/503, retrying")
        
        # Calculate retry delay with jitter
        jitter = random.uniform(-self.retry_jitter, self.retry_jitter) * self.retry_base_ms
        delay_ms = max(10, int(self.retry_base_ms + jitter))
        time.sleep(delay_ms / 1000.0)
        
        # Second attempt
        success = self._attempt_snapshot(url)
        return success
    
    def _attempt_snapshot(self, url):
        """Single attempt to get snapshot from panel"""
        try:
            # Create request with custom headers
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'DD5KA-Detector/CH4')
            
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    jpeg_data = response.read()
                    
                    if self.backend == 'cpu' and self.model:
                        # CPU inference mode with direct ultralytics
                        try:
                            # Open image with PIL
                            img = Image.open(BytesIO(jpeg_data)).convert("RGB")
                            
                            # Run inference
                            r = self.model.predict(img, conf=CONF, iou=0.50, imgsz=640, device="cpu", verbose=False)[0]
                            
                            # Process detections
                            dets = []
                            boxes = r.boxes
                            if boxes is not None:
                                for i in range(int(boxes.shape[0])):
                                    xyxy = boxes.xyxy[i].tolist()
                                    conf = float(boxes.conf[i])
                                    # Normalize class name to "drone"
                                    dets.append({
                                        "class_id": 0,
                                        "class_name": "drone",
                                        "conf": round(conf, 3),
                                        "bbox_xyxy": [float(x) for x in xyxy]
                                    })
                            
                            # Create detection event
                            event = {
                                "ts": datetime.utcnow().isoformat(timespec="microseconds") + "Z",
                                "type": "detection",
                                "backend": "cpu",
                                "model": {
                                    "path": MODEL,
                                    "framework": "ultralytics",
                                    "version": "auto"
                                },
                                "image": {
                                    "width": img.width,
                                    "height": img.height
                                },
                                "detections": dets
                            }
                            
                            # Write to DETECTIONS_PATH with fsync
                            try:
                                with open(DET_PATH, 'a', encoding='utf-8', buffering=1) as f:
                                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
                                    f.flush()
                                    os.fsync(f.fileno())
                                
                                self.logger.info(f"infer dets={len(dets)}, conf>={CONF}")
                                
                                # Save snapshot if needed (existing logic)
                                image_path, image_sha1 = self._save_snapshot(jpeg_data, event)
                                
                                # Check for alert debounce (existing logic)
                                self._check_alert_debounce(event, image_path, image_sha1)
                                
                            except Exception as e:
                                self.logger.error(f"failed to write detection: {e}")
                            
                            return True
                            
                        except Exception as e:
                            self.logger.error(f"inference failed: {e}")
                            return False
                    else:
                        # Stub mode
                        self.logger.info("detector heartbeat (snapshot ok)")
                        self._write_detection(True)
                        return True
                else:
                    # Handle non-200 status codes
                    return self._handle_http_error(response.status)
                    
        except urllib.error.HTTPError as e:
            # Handle HTTP errors explicitly (must come before URLError)
            return self._handle_http_error(e.code)
                
        except urllib.error.URLError as e:
            # Check if this is actually an HTTPError that got caught as URLError
            if hasattr(e, 'code') and e.code in [500, 503]:
                # Redirect to HTTP error handling
                return self._handle_http_error(e.code)
            else:
                # Network/transport errors - log as WARNING
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
        self.logger.info(f"detector daemon starting (poll_sec={self.poll_sec}, panel={self.panel_base_url}, backend={BACK})")
        
        while self.running:
            try:
                success = self._poll_panel()
                
                # Add extra delay after failures to desynchronize with panel requests
                if not success:
                    extra_delay = random.uniform(0.8, 1.2) * self.fail_extra_ms / 1000.0
                    time.sleep(extra_delay)
                    
            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
                # Extra delay after unexpected errors too
                extra_delay = random.uniform(0.8, 1.2) * self.fail_extra_ms / 1000.0
                time.sleep(extra_delay)
            
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