#!/usr/bin/env python3
"""
DD-5KA Panel Overlay Stream
MJPEG stream with detection overlays
"""

import json
import logging
import os
import stat
import time
import io
from datetime import datetime
from typing import Optional, Dict, List, Tuple
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .camera import capture_jpeg

# Try to import OpenCV, fallback to PIL
try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class OverlayStream:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        
        # Environment variables
        self.detections_file = os.getenv('OVERLAY_DETECTIONS_FILE', '/home/nemez/project_root/logs/detections.jsonl')
        self.min_conf = float(os.getenv('OVERLAY_MIN_CONF', '0.25'))
        self.tail_bytes = int(os.getenv('OVERLAY_TAIL_BYTES', '65536'))
        self.max_side = int(os.getenv('OVERLAY_MAX_SIDE', '640'))
        self.det_max_age_ms = int(os.getenv('OVERLAY_DET_MAX_AGE_MS', '4000'))
        self.output_fps = int(os.getenv('OVERLAY_FPS', '4'))
        self.capture_fps = int(os.getenv('OVERLAY_CAPTURE_FPS', '2'))
        
        # Calculate intervals
        self.output_interval = 1.0 / self.output_fps
        self.capture_interval = 1.0 / self.capture_fps
        
        # Frame cache
        self.last_ok_frame: Optional[bytes] = None
        self.last_capture_time = 0.0
        self.last_error_log_time = 0.0
        
        # Detection file reader state
        self._det_fp: Optional[io.TextIOWrapper] = None
        self._det_inode: Optional[int] = None
        self._det_pos: int = 0
        self._last_event: Optional[Dict] = None
        
        # Font cache
        self._font = None
        self._font_large = None
        
    def _get_font(self, size: int = 16):
        """Get cached font"""
        if size == 16 and self._font is not None:
            return self._font
        elif size > 16 and self._font_large is not None:
            return self._font_large
            
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
            if size == 16:
                self._font = font
            else:
                self._font_large = font
            return font
        except:
            return ImageFont.load_default()
    
    def _get_recent_detection(self) -> Optional[Dict]:
        """Read recent detection events from tail of file efficiently"""
        try:
            if not os.path.exists(self.detections_file):
                return None
                
            with open(self.detections_file, 'r', encoding='utf-8') as f:
                # Seek to tail
                f.seek(0, 2)  # End of file
                file_size = f.tell()
                seek_pos = max(0, file_size - self.tail_bytes)
                f.seek(seek_pos)
                
                # Read tail content
                tail_content = f.read()
                
                # Split into lines and process from end
                lines = tail_content.strip().split('\n')
                
                # Process lines from end to find most recent detection with detections
                for line in reversed(lines):
                    if not line.strip():
                        continue
                        
                    try:
                        event = json.loads(line)
                        if event.get("type") != "detection":
                            continue
                            
                        # Filter detections by confidence
                        all_detections = event.get("detections", [])
                        filtered_detections = []
                        for det in all_detections:
                            conf = float(det.get("conf", 0))
                            if conf >= self.min_conf:
                                filtered_detections.append(det)
                        
                        # Only return if we have filtered detections
                        if filtered_detections:
                            event["detections"] = filtered_detections
                            return event
                            
                    except json.JSONDecodeError:
                        continue
                    except (ValueError, TypeError) as e:
                        self.logger.warning(f"Failed to parse detection data: {e}")
                        continue
                        
            return None
            
        except Exception as e:
            self.logger.warning(f"Failed to read recent detections: {e}")
            return None
    
    def _get_snapshot(self, non_blocking: bool = False) -> Optional[bytes]:
        """Get snapshot JPEG data with rate limiting and error handling"""
        current_time = time.time()
        
        # If non_blocking, don't capture new frame, just return cached
        if non_blocking:
            return self.last_ok_frame
        
        # Check if we should capture a new frame
        if current_time - self.last_capture_time >= self.capture_interval:
            try:
                jpeg_data = capture_jpeg(max_side=self.max_side)
                if jpeg_data:
                    self.last_ok_frame = jpeg_data
                    self.last_capture_time = current_time
                    return jpeg_data
            except Exception as e:
                # Throttled error logging (once every 5 seconds)
                if current_time - self.last_error_log_time >= 5.0:
                    self.logger.warning(f"no frame, using last_ok_frame: {e}")
                    self.last_error_log_time = current_time
                pass
        
        # Return last successful frame or None
        return self.last_ok_frame
    
    
    def _draw_overlays_cv2(self, image_np: np.ndarray, detections: List[Dict], scale_x: float, scale_y: float) -> np.ndarray:
        """Draw detection overlays using OpenCV"""
        for det in detections:
            bbox = det.get("bbox_xyxy", [])
            conf = det.get("conf", 0.0)
            class_name = det.get("class_name", "OBJ")
            
            if len(bbox) == 4:
                # Scale coordinates to current frame
                x1 = int(bbox[0] * scale_x)
                y1 = int(bbox[1] * scale_y)
                x2 = int(bbox[2] * scale_x)
                y2 = int(bbox[3] * scale_y)
                
                # Clamp to frame bounds
                h, w = image_np.shape[:2]
                x1 = max(0, min(x1, w-1))
                y1 = max(0, min(y1, h-1))
                x2 = max(0, min(x2, w-1))
                y2 = max(0, min(y2, h-1))
                
                # Draw rectangle
                cv2.rectangle(image_np, (x1, y1), (x2, y2), (0, 255, 0), 2)
                # Draw text
                text = f"{class_name} {conf:.2f}"
                cv2.putText(image_np, text, (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        return image_np
    
    def _draw_overlays_pil(self, image: Image.Image, detections: List[Dict], scale_x: float, scale_y: float) -> Image.Image:
        """Draw detection overlays using PIL with background for text"""
        draw = ImageDraw.Draw(image)
        font = self._get_font(16)
        
        for det in detections:
            bbox = det.get("bbox_xyxy", [])
            conf = det.get("conf", 0.0)
            class_name = det.get("class_name", "OBJ")
            
            if len(bbox) == 4:
                # Scale coordinates to current frame
                x1 = int(bbox[0] * scale_x)
                y1 = int(bbox[1] * scale_y)
                x2 = int(bbox[2] * scale_x)
                y2 = int(bbox[3] * scale_y)
                
                # Clamp to frame bounds
                w, h = image.size
                x1 = max(0, min(x1, w-1))
                y1 = max(0, min(y1, h-1))
                x2 = max(0, min(x2, w-1))
                y2 = max(0, min(y2, h-1))
                
                # Draw rectangle
                draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=2)
                
                # Draw text with background
                text = f"{class_name} {conf:.2f}"
                bbox_text = draw.textbbox((x1, y1-20), text, font=font)
                # Draw background rectangle for text
                draw.rectangle(bbox_text, fill=(0, 0, 0, 128))
                draw.text((x1, y1-20), text, fill=(0, 255, 0), font=font)
        return image
    
    def _create_no_frame(self) -> bytes:
        """Create black placeholder frame"""
        if CV2_AVAILABLE:
            # OpenCV path
            img = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(img, "NO FRAME", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
            _, jpeg_encoded = cv2.imencode('.jpg', img)
            return jpeg_encoded.tobytes()
        else:
            # PIL path
            img = Image.new('RGB', (640, 480), (0, 0, 0))
            draw = ImageDraw.Draw(img)
            font = self._get_font(24)
            draw.text((200, 200), "NO FRAME", fill=(255, 255, 255), font=font)
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=85)
            return output.getvalue()
    
    def generate_frames(self):
        """Generate MJPEG frames with overlays"""
        first = True
        
        while True:
            try:
                # Get snapshot - instant first frame logic
                if first:
                    # First frame: use cached frame or create placeholder, no blocking
                    jpeg_data = self.last_ok_frame
                    if not jpeg_data:
                        jpeg_data = self._create_no_frame()
                else:
                    # Subsequent frames: use non-blocking if not time for new capture
                    current_time = time.time()
                    if current_time - self.last_capture_time >= self.capture_interval:
                        jpeg_data = self._get_snapshot()  # Blocking capture
                    else:
                        jpeg_data = self._get_snapshot(non_blocking=True)  # Non-blocking
                    
                    if not jpeg_data:
                        jpeg_data = self._create_no_frame()
                
                # Get recent detection with age check
                detection_event = self._get_recent_detection()
                detections = []
                age_ms = 0
                fresh = time.time() - self.last_capture_time < self.capture_interval
                
                if detection_event:
                    # Calculate age from event timestamp
                    try:
                        event_ts = datetime.fromisoformat(detection_event["ts"].replace('Z', '+00:00'))
                        now_utc = datetime.now(event_ts.tzinfo)
                        age_ms = int((now_utc - event_ts).total_seconds() * 1000)
                        
                        # Only use if within age limit
                        if age_ms <= self.det_max_age_ms:
                            detections = detection_event.get("detections", [])
                    except Exception as e:
                        self.logger.warning(f"Failed to parse event timestamp: {e}")
                        detection_event = None
                
                # Process image
                start_draw = time.time()
                if CV2_AVAILABLE:
                    # OpenCV path
                    image_np = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
                    if image_np is not None:
                        # Calculate scale factors if we have detection event
                        scale_x = scale_y = 1.0
                        evt_wh = "0x0"
                        if detection_event and "image" in detection_event:
                            evt_w = detection_event["image"].get("width", 0)
                            evt_h = detection_event["image"].get("height", 0)
                            evt_wh = f"{evt_w}x{evt_h}"
                            frame_h, frame_w = image_np.shape[:2]
                            # Scaling safety: protect against evt_w/evt_h == 0
                            if evt_w > 0 and evt_h > 0:
                                scale_x = frame_w / evt_w
                                scale_y = frame_h / evt_h
                        
                        # Draw overlays
                        if detections:
                            image_np = self._draw_overlays_cv2(image_np, detections, scale_x, scale_y)
                        
                        # Encode back to JPEG
                        _, jpeg_encoded = cv2.imencode('.jpg', image_np)
                        frame_data = jpeg_encoded.tobytes()
                        
                        # Get frame dimensions for logging
                        frame_wh = f"{frame_w}x{frame_h}"
                    else:
                        frame_data = jpeg_data
                        frame_wh = "0x0"
                else:
                    # PIL path
                    image = Image.open(io.BytesIO(jpeg_data))
                    
                    # Calculate scale factors if we have detection event
                    scale_x = scale_y = 1.0
                    evt_wh = "0x0"
                    if detection_event and "image" in detection_event:
                        evt_w = detection_event["image"].get("width", 0)
                        evt_h = detection_event["image"].get("height", 0)
                        evt_wh = f"{evt_w}x{evt_h}"
                        frame_w, frame_h = image.size
                        # Scaling safety: protect against evt_w/evt_h == 0
                        if evt_w > 0 and evt_h > 0:
                            scale_x = frame_w / evt_w
                            scale_y = frame_h / evt_h
                    
                    # Draw overlays
                    if detections:
                        image = self._draw_overlays_pil(image, detections, scale_x, scale_y)
                    
                    # Convert back to JPEG
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=70)
                    frame_data = output.getvalue()
                    
                    # Get frame dimensions for logging
                    frame_w, frame_h = image.size
                    frame_wh = f"{frame_w}x{frame_h}"
                
                draw_time = int((time.time() - start_draw) * 1000)
                src_ts = detection_event.get("ts", "") if detection_event else ""
                self.logger.info(f"overlay frame: dets={len(detections)}, age_ms={age_ms}, draw_ms={draw_time}, fresh={fresh}, first={first}")
                
                # Yield MJPEG frame with proper headers (boundary consistency)
                yield f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame_data)}\r\n\r\n".encode() + frame_data + b"\r\n"
                
                # After first yield, set first=False
                if first:
                    first = False
                
                time.sleep(self.output_interval)
                
            except Exception as e:
                self.logger.warning(f"Overlay stream error: {e}")
                time.sleep(0.5)
    
    def __del__(self):
        """Cleanup file handle"""
        if self._det_fp:
            self._det_fp.close()