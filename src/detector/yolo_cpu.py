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
    def __init__(self, model_path: str, logger: logging.Logger):
        self.model_path = model_path
        self.logger = logger
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
    
    def _get_class_name(self, class_id: int) -> Optional[str]:
        """Get class name by ID, try to match 'drone' case-insensitive"""
        if not self.model or not hasattr(self.model, 'names'):
            return None
            
        names = self.model.names
        if class_id in names:
            class_name = names[class_id]
            # Try to match 'drone' case-insensitive
            if 'drone' in class_name.lower():
                return 'drone'
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
            image_np = np.array(image)
            
            # Run inference
            start_time = time.time()
            results = self.model(image_np)
            infer_time = int((time.time() - start_time) * 1000)
            
            # Parse results
            detections = []
            if results and len(results) > 0:
                boxes = results[0].boxes
                if boxes is not None:
                    for i in range(len(boxes)):
                        xyxy = boxes.xyxy[i].cpu().numpy().tolist()
                        conf = float(boxes.conf[i].cpu().numpy())
                        cls = int(boxes.cls[i].cpu().numpy())
                        
                        class_name = self._get_class_name(cls)
                        
                        detections.append({
                            "class_id": cls,
                            "class_name": class_name,
                            "conf": conf,
                            "bbox_xyxy": xyxy
                        })
            
            self.logger.info(f"infer {infer_time}ms, {len(detections)} dets")
            
            return {
                "image": {
                    "width": image.width,
                    "height": image.height
                },
                "detections": detections
            }
            
        except Exception as e:
            self.logger.error(f"inference failed: {e}")
            return {
                "error": str(e),
                "detections": []
            }
