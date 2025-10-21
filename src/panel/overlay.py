#!/usr/bin/env python3
"""
DD-5KA Panel Overlay Stream
MJPEG stream with detection overlays
"""

import json
import logging
import os
import time
import io
from typing import Optional, Dict, List
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

# Try to import OpenCV, fallback to PIL
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class OverlayStream:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.detections_file = "/home/nemez/DD5KA/logs/detections.jsonl"
        self.snapshot_url = "http://127.0.0.1:8098/snapshot"
        self.max_side = int(os.getenv('OVERLAY_MAX_SIDE', '1280'))
        
    def _get_last_detection(self) -> Optional[Dict]:
        """Get last detection event from JSONL file"""
        try:
            if not os.path.exists(self.detections_file):
                return None
                
            with open(self.detections_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            # Find last non-empty line
            for line in reversed(lines):
                line = line.strip()
                if line:
                    try:
                        event = json.loads(line)
                        if event.get("type") == "detection" and event.get("detections"):
                            return event
                    except json.JSONDecodeError:
                        continue
            return None
        except Exception as e:
            self.logger.warning(f"Failed to read detections: {e}")
            return None
    
    def _get_snapshot(self) -> Optional[bytes]:
        """Get snapshot JPEG data"""
        try:
            response = requests.get(self.snapshot_url, timeout=3)
            if response.status_code == 200:
                return response.content
            return None
        except Exception as e:
            self.logger.warning(f"Failed to get snapshot: {e}")
            return None
    
    def _draw_overlays_cv2(self, image_np: np.ndarray, detections: List[Dict]) -> np.ndarray:
        """Draw detection overlays using OpenCV"""
        for det in detections:
            bbox = det.get("bbox_xyxy", [])
            conf = det.get("conf", 0.0)
            
            if len(bbox) == 4:
                x1, y1, x2, y2 = map(int, bbox)
                # Draw rectangle
                cv2.rectangle(image_np, (x1, y1), (x2, y2), (0, 255, 0), 2)
                # Draw text
                text = f"DRON {conf:.2f}"
                cv2.putText(image_np, text, (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        return image_np
    
    def _draw_overlays_pil(self, image: Image.Image, detections: List[Dict]) -> Image.Image:
        """Draw detection overlays using PIL"""
        draw = ImageDraw.Draw(image)
        
        # Try to use a default font
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except:
            font = ImageFont.load_default()
        
        for det in detections:
            bbox = det.get("bbox_xyxy", [])
            conf = det.get("conf", 0.0)
            
            if len(bbox) == 4:
                x1, y1, x2, y2 = map(int, bbox)
                # Draw rectangle
                draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=2)
                # Draw text
                text = f"DRON {conf:.2f}"
                draw.text((x1, y1-20), text, fill=(0, 255, 0), font=font)
        return image
    
    def _resize_image(self, image: Image.Image) -> Image.Image:
        """Resize image if needed"""
        w, h = image.size
        max_dim = max(w, h)
        
        if max_dim > self.max_side:
            scale = self.max_side / max_dim
            new_w = int(w * scale)
            new_h = int(h * scale)
            return image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        return image
    
    def generate_frames(self):
        """Generate MJPEG frames with overlays"""
        while True:
            try:
                # Get snapshot
                jpeg_data = self._get_snapshot()
                if not jpeg_data:
                    time.sleep(0.1)
                    continue
                
                # Get last detection
                detection_event = self._get_last_detection()
                detections = detection_event.get("detections", []) if detection_event else []
                
                # Process image
                if CV2_AVAILABLE:
                    # OpenCV path
                    image_np = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
                    if image_np is not None:
                        # Draw overlays
                        if detections:
                            image_np = self._draw_overlays_cv2(image_np, detections)
                        
                        # Encode back to JPEG
                        _, jpeg_encoded = cv2.imencode('.jpg', image_np)
                        frame_data = jpeg_encoded.tobytes()
                    else:
                        frame_data = jpeg_data
                else:
                    # PIL path
                    image = Image.open(io.BytesIO(jpeg_data))
                    image = self._resize_image(image)
                    
                    # Draw overlays
                    if detections:
                        image = self._draw_overlays_pil(image, detections)
                    
                    # Convert back to JPEG
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=85)
                    frame_data = output.getvalue()
                
                # Yield MJPEG frame
                yield f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame_data)}\r\n\r\n".encode() + frame_data + b"\r\n"
                
                time.sleep(0.1)  # Small delay to prevent overwhelming
                
            except Exception as e:
                self.logger.warning(f"Overlay stream error: {e}")
                time.sleep(0.5)
