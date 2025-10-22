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
        
        # YOLO fallback environment variables
        self.yolo_fallback = os.getenv('OVERLAY_YOLO_FALLBACK', '0') == '1'
        self.yolo_model_path = os.getenv('OVERLAY_YOLO_MODEL', '/home/nemez/DD5KA/models/cpu/best.pt')
        self.yolo_conf = float(os.getenv('OVERLAY_YOLO_CONF', '0.12'))
        self.yolo_iou = float(os.getenv('OVERLAY_YOLO_IOU', '0.50'))
        self.yolo_imgsz = int(os.getenv('OVERLAY_YOLO_IMGSZ', '640'))
        self.yolo_fps = int(os.getenv('OVERLAY_YOLO_FPS', '2'))
        
        # Calculate intervals
        self.output_interval = 1.0 / self.output_fps
        self.capture_interval = 1.0 / self.capture_fps
        self.yolo_interval = 1.0 / self.yolo_fps
        
        # Frame cache
        self.last_ok_frame: Optional[bytes] = None
        self.last_capture_time = 0.0
        self.last_error_log_time = 0.0
        
        # YOLO fallback state
        self._yolo_model = None
        self._last_yolo_inference = 0.0
        self._last_yolo_detections = []
        
        # Detection file reader state
        self._det_fp: Optional[io.TextIOWrapper] = None
        self._det_inode: Optional[int] = None
        self._det_pos: int = 0
        self._last_event: Optional[Dict] = None
        
        # Font cache
        self._font = None
        self._font_large = None
        # Last frame stats for logging в генераторе
        self._last_dets_count = 0
        self._last_draw_ms = 0
        
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
    
    def _load_yolo_model(self):
        """Lazy load YOLO model"""
        if self._yolo_model is None and self.yolo_fallback:
            try:
                from ultralytics import YOLO
                self._yolo_model = YOLO(self.yolo_model_path)
                self.logger.info(f"YOLO fallback model loaded: {self.yolo_model_path}")
            except Exception as e:
                self.logger.warning(f"Failed to load YOLO model: {e}")
                self._yolo_model = False  # Mark as failed to avoid retries
    
    def _run_yolo_inference(self, image_data: bytes) -> List[Dict]:
        """Run YOLO inference on image data"""
        if not self.yolo_fallback or self._yolo_model is False:
            return []
        
        current_time = time.time()
        if current_time - self._last_yolo_inference < self.yolo_interval:
            return self._last_yolo_detections
        
        try:
            # Lazy load model
            self._load_yolo_model()
            if self._yolo_model is None or self._yolo_model is False:
                return []
            
            start_infer = time.time()
            
            # Convert bytes to PIL Image
            image = Image.open(io.BytesIO(image_data))
            
            # Run inference
            results = self._yolo_model.predict(
                image, 
                conf=self.yolo_conf, 
                iou=self.yolo_iou, 
                imgsz=self.yolo_imgsz,
                verbose=False
            )
            
            infer_time = int((time.time() - start_infer) * 1000)
            
            # Process results
            detections = []
            for result in results:
                if result.boxes is not None:
                    boxes = result.boxes.xyxy.cpu().numpy()
                    confs = result.boxes.conf.cpu().numpy()
                    class_ids = result.boxes.cls.cpu().numpy()
                    
                    for i in range(len(boxes)):
                        class_id = int(class_ids[i])
                        class_name = result.names.get(class_id, f"class_{class_id}")
                        
                        # Check if class name contains "dron" or "drone"
                        display_name = "DRON" if "dron" in class_name.lower() else class_name
                        
                        detections.append({
                            "bbox_xyxy": boxes[i].tolist(),
                            "conf": float(confs[i]),
                            "class_name": display_name,
                            "class_id": class_id
                        })
            
            self._last_yolo_inference = current_time
            self._last_yolo_detections = detections
            
            self.logger.info(f"overlay fallback yolo: dets={len(detections)}, conf={self.yolo_conf}, imgsz={self.yolo_imgsz}, infer_ms={infer_time}")
            
            return detections
            
        except Exception as e:
            self.logger.warning(f"YOLO inference failed: {e}")
            return []
    
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
        last_send_time = 0.0
        last_frame_data = None
        
        while True:
            try:
                current_time = time.time()
                
                # First frame: send immediately, even if camera unavailable
                if last_send_time == 0.0:
                    frame_data = self.make_frame_bytes()
                    last_frame_data = frame_data
                    last_send_time = current_time
                    # Лог первого кадра с реальной статистикой
                    self.logger.info(
                        f"overlay frame sent: bytes={len(frame_data)}, dets={self._last_dets_count}, "
                        f"draw_ms={self._last_draw_ms}, fresh=True"
                    )
                    
                    # Yield first frame
                    yield f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame_data)}\r\n\r\n".encode() + frame_data + b"\r\n"
                    time.sleep(self.output_interval)
                    continue
                
                # Check if it's time to send next frame
                if current_time - last_send_time >= self.output_interval:
                    # Check for timeout (2.5s silence)
                    if current_time - last_send_time > 2.5:
                        # Send keepalive
                        yield b"--frame\r\nContent-Type: text/plain\r\n\r\n# keepalive\r\n"
                        self.logger.info("overlay keepalive sent")
                        last_send_time = current_time
                        time.sleep(self.output_interval)
                        continue
                    
                    # Try to get fresh frame
                    fresh_frame = self.make_frame_bytes()
                    if fresh_frame:
                        frame_data = fresh_frame
                        last_frame_data = frame_data
                    else:
                        # Reuse last frame if no fresh data
                        frame_data = last_frame_data or self._create_no_frame()
                    
                    # Лог последующих кадров с реальной статистикой
                    fresh = fresh_frame is not None
                    self.logger.info(
                        f"overlay frame sent: bytes={len(frame_data)}, dets={self._last_dets_count}, "
                        f"draw_ms={self._last_draw_ms}, fresh={fresh}"
                    )
                    
                    # Yield frame
                    yield f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame_data)}\r\n\r\n".encode() + frame_data + b"\r\n"
                    last_send_time = current_time
                
                time.sleep(0.1)  # Small sleep to prevent busy waiting
                
            except Exception as e:
                self.logger.warning(f"overlay stream error: {e}")
                # Generate keepalive on error
                yield b"--frame\r\nContent-Type: text/plain\r\n\r\n# keepalive\r\n"
                time.sleep(0.5)
    
    def make_frame_bytes(self) -> bytes:
        """Generate a single JPEG frame with overlays"""
        start_draw = time.time()
        
        # Get snapshot
        jpeg_data = self._get_snapshot()
        if not jpeg_data:
            jpeg_data = self._create_no_frame()
        
        # Get recent detection with age check
        detection_event = self._get_recent_detection()
        detections = []
        age_ms = 0
        
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
        
        # YOLO fallback: if no fresh detections, try YOLO inference
        if not detections and self.yolo_fallback:
            yolo_detections = self._run_yolo_inference(jpeg_data)
            if yolo_detections:
                detections = yolo_detections
        
        # Process image
        if CV2_AVAILABLE:
            # OpenCV path
            image_np = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
            if image_np is not None:
                # Calculate scale factors if we have detection event
                scale_x = scale_y = 1.0
                if detection_event and "image" in detection_event:
                    evt_w = max(1, int(detection_event["image"].get("width", 0)))
                    evt_h = max(1, int(detection_event["image"].get("height", 0)))
                    frame_h, frame_w = image_np.shape[:2]
                    scale_x = frame_w / evt_w
                    scale_y = frame_h / evt_h
                
                # Draw overlays
                if detections:
                    image_np = self._draw_overlays_cv2(image_np, detections, scale_x, scale_y)
                
                # Add timestamp in top-right corner for live stream indication
                timestamp = datetime.now().strftime("%H:%M:%S")
                cv2.putText(image_np, timestamp, (image_np.shape[1] - 100, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                
                # Encode back to JPEG
                _, jpeg_encoded = cv2.imencode('.jpg', image_np, [cv2.IMWRITE_JPEG_QUALITY, 80])
                frame_data = jpeg_encoded.tobytes()
            else:
                frame_data = jpeg_data
        else:
            # PIL path
            image = Image.open(io.BytesIO(jpeg_data))
            
            # Calculate scale factors if we have detection event
            scale_x = scale_y = 1.0
            if detection_event and "image" in detection_event:
                evt_w = max(1, int(detection_event["image"].get("width", 0)))
                evt_h = max(1, int(detection_event["image"].get("height", 0)))
                frame_w, frame_h = image.size
                scale_x = frame_w / evt_w
                scale_y = frame_h / evt_h
            
            # Draw overlays
            if detections:
                image = self._draw_overlays_pil(image, detections, scale_x, scale_y)
            
            # Add timestamp in top-right corner for live stream indication
            timestamp = datetime.now().strftime("%H:%M:%S")
            draw = ImageDraw.Draw(image)
            font = self._get_font(12)
            draw.text((image.size[0] - 100, 10), timestamp, fill=(255, 255, 255), font=font)
            
            # Convert back to JPEG
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=80)
            frame_data = output.getvalue()
        
        # Сохраняем статистику для логов
        self._last_dets_count = len(detections)
        self._last_draw_ms = int((time.time() - start_draw) * 1000)
        return frame_data
    
    def generate_single_frame(self) -> bytes:
        """Generate a single frame - alias for make_frame_bytes()"""
        return self.make_frame_bytes()
    
    def render_single_frame(self) -> bytes:
        """Render a single frame with overlays for /overlay.jpg endpoint"""
        start_time = time.time()
        
        # Get current snapshot with one retry if needed
        jpeg_data = self._get_snapshot()
        if not jpeg_data:
            # One retry after output_interval
            time.sleep(self.output_interval)
            jpeg_data = self._get_snapshot()
        
        if not jpeg_data:
            jpeg_data = self._create_no_frame()
        
        # Get recent detection with age check
        detection_event = self._get_recent_detection()
        detections = []
        age_ms = 0
        
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
                if detection_event and "image" in detection_event:
                    evt_w = max(1, int(detection_event["image"].get("width", 0)))
                    evt_h = max(1, int(detection_event["image"].get("height", 0)))
                    frame_h, frame_w = image_np.shape[:2]
                    scale_x = frame_w / evt_w
                    scale_y = frame_h / evt_h
                
                # Draw overlays
                if detections:
                    image_np = self._draw_overlays_cv2(image_np, detections, scale_x, scale_y)
                
                # Add timestamp in top-right corner for live stream indication
                timestamp = datetime.now().strftime("%H:%M:%S")
                cv2.putText(image_np, timestamp, (image_np.shape[1] - 100, 30), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                
                # Encode back to JPEG
                _, jpeg_encoded = cv2.imencode('.jpg', image_np, [cv2.IMWRITE_JPEG_QUALITY, 80])
                frame_data = jpeg_encoded.tobytes()
            else:
                frame_data = jpeg_data
        else:
            # PIL path
            image = Image.open(io.BytesIO(jpeg_data))
            
            # Calculate scale factors if we have detection event
            scale_x = scale_y = 1.0
            if detection_event and "image" in detection_event:
                evt_w = max(1, int(detection_event["image"].get("width", 0)))
                evt_h = max(1, int(detection_event["image"].get("height", 0)))
                frame_w, frame_h = image.size
                scale_x = frame_w / evt_w
                scale_y = frame_h / evt_h
            
            # Draw overlays
            if detections:
                image = self._draw_overlays_pil(image, detections, scale_x, scale_y)
            
            # Add timestamp in top-right corner for live stream indication
            timestamp = datetime.now().strftime("%H:%M:%S")
            draw = ImageDraw.Draw(image)
            font = self._get_font(12)
            draw.text((image.size[0] - 100, 10), timestamp, fill=(255, 255, 255), font=font)
            
            # Convert back to JPEG
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=80)
            frame_data = output.getvalue()
        
        draw_time = int((time.time() - start_draw) * 1000)
        self.logger.info(f"overlay single frame: dets={len(detections)}, age_ms={age_ms}, draw_ms={draw_time}")
        
        return frame_data
    
    def __del__(self):
        """Cleanup file handle"""
        if self._det_fp:
            self._det_fp.close()