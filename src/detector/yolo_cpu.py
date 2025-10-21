#!/usr/bin/env python3
"""
DD-5KA YOLO CPU Inference Helper
"""

import logging
import os
import time
from typing import Dict, List, Optional, Tuple
import numpy as np
from PIL import Image
import io

try:
    from ultralytics import YOLO
    ULTRALYTICS_AVAILABLE = True
except ImportError:
    ULTRALYTICS_AVAILABLE = False


class YOLOCPUInference:
    def __init__(self, model_path: str, logger: logging.Logger, min_conf: float = 0.55, 
                 allow_classes: str = "drone,dron,дрон,uav", max_side: int = 1280):
        self.model_path = model_path
        self.logger = logger
        self.min_conf = min_conf
        self.allow_classes = set(cls.strip().lower() for cls in allow_classes.split(','))
        self.max_side = max_side
        self.model = None
        self.model_loaded = False
        self.load_error = None
        
    def _load_model(self) -> bool:
        """Load YOLO model (lazy initialization)"""
        if self.model_loaded:
            return True
            
        if not ULTRALYTICS_AVAILABLE:
            self.load_error = "ultralytics not available"
            self.logger.error("ultralytics not available")
            return False
            
        try:
            self.logger.info(f"loading model from {self.model_path}")
            self.model = YOLO(self.model_path)
            self.model_loaded = True
            self.logger.info("model loaded successfully")
            return True
        except Exception as e:
            self.load_error = str(e)
            self.logger.error(f"failed to load model: {e}")
            return False
    
    def _normalize_class_name(self, class_id: int) -> Optional[str]:
        """Normalize class name to 'drone' for synonyms"""
        if not self.model or not hasattr(self.model, 'names'):
            return None
            
        names = self.model.names
        if class_id in names:
            class_name = names[class_id].lower()
            # Check for drone synonyms
            if class_name in {"dron", "drone", "дрон", "uav"}:
                return "drone"
            return class_name
        return None
    
    def infer_from_jpeg(self, jpeg_data: bytes) -> Dict:
        """Run inference on JPEG data"""
        if not self._load_model():
            return {
                "error": f"model not loaded: {self.load_error}",
                "detections": []
            }
        
        try:
            # Convert JPEG to numpy array
            image = Image.open(io.BytesIO(jpeg_data))
            orig_w, orig_h = image.width, image.height
            
            # Resize if needed
            max_dim = max(orig_w, orig_h)
            if max_dim > self.max_side:
                scale = self.max_side / max_dim
                new_w = int(orig_w * scale)
                new_h = int(orig_h * scale)
                image_resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
                image_np = np.array(image_resized)
                resized_dims = [new_w, new_h]
            else:
                image_np = np.array(image)
                resized_dims = [orig_w, orig_h]
            
            # Run inference
            start_time = time.time()
            results = self.model(image_np)
            infer_time = int((time.time() - start_time) * 1000)
            
            # Parse and filter results
            detections = []
            if results and len(results) > 0:
                boxes = results[0].boxes
                if boxes is not None:
                    # Calculate scale factors for bbox conversion
                    scale_x = orig_w / resized_dims[0]
                    scale_y = orig_h / resized_dims[1]
                    
                    for i in range(len(boxes)):
                        xyxy_resized = boxes.xyxy[i].cpu().numpy()
                        conf = round(float(boxes.conf[i].cpu().numpy()), 3)
                        cls = int(boxes.cls[i].cpu().numpy())
                        
                        # Filter by confidence
                        if conf < self.min_conf:
                            continue
                        
                        # Normalize class name
                        class_name = self._normalize_class_name(cls)
                        
                        # Filter by class (if class_name is available)
                        if class_name is not None:
                            # Check both original and normalized names
                            original_name = self.model.names.get(cls, "").lower() if hasattr(self.model, 'names') else ""
                            if class_name not in self.allow_classes and original_name not in self.allow_classes:
                                continue
                        
                        # Scale bbox back to original coordinates
                        xyxy_orig = [
                            xyxy_resized[0] * scale_x,
                            xyxy_resized[1] * scale_y,
                            xyxy_resized[2] * scale_x,
                            xyxy_resized[3] * scale_y
                        ]
                        
                        detections.append({
                            "class_id": cls,
                            "class_name": class_name,
                            "conf": conf,
                            "bbox_xyxy": xyxy_orig
                        })
            
            # Log with filtering info
            allow_str = ",".join(sorted(self.allow_classes))
            self.logger.info(f"infer {infer_time}ms, dets={len(detections)}, conf>={self.min_conf}, classes={allow_str}")
            
            return {
                "image": {
                    "width": orig_w,
                    "height": orig_h
                },
                "detections": detections,
                "perf": {
                    "infer_ms": infer_time,
                    "resized": resized_dims
                }
            }
            
        except Exception as e:
            self.logger.error(f"inference failed: {e}")
            return {
                "error": str(e),
                "detections": []
            }
