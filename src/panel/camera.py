#!/usr/bin/env python3
"""
DD-5KA Camera Helper
Direct rpicam-still capture with retries
"""

import logging
import os
import subprocess
import time
from typing import Optional


def capture_jpeg(max_side: int = 1280, timeout_ms: int = 800, retries: int = 2) -> bytes:
    """
    Capture JPEG from camera with retries
    
    Args:
        max_side: Maximum side dimension for resizing
        timeout_ms: Timeout for rpicam-still in milliseconds
        retries: Number of retry attempts
        
    Returns:
        JPEG data as bytes
        
    Raises:
        Exception: If all retries fail
    """
    logger = logging.getLogger("panel.camera")
    
    # Calculate dimensions for rpicam-still
    # Use max_side as width, let driver calculate height maintaining aspect ratio
    width = max_side
    
    for attempt in range(retries + 1):
        try:
            start_time = time.time()
            
            # Build rpicam-still command
            cmd = [
                "/usr/bin/rpicam-still",
                "-n",  # no preview
                "-o", "-",  # output to stdout
                "-t", str(timeout_ms),
                "--quality", "85",
                "--thumb", "none",
                "--width", str(width)
            ]
            
            # Execute capture
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=(timeout_ms / 1000) + 2,  # Add 2s buffer
                check=True
            )
            
            capture_time = int((time.time() - start_time) * 1000)
            
            if len(result.stdout) < 1000:  # Too small for valid JPEG
                raise ValueError(f"Invalid JPEG size: {len(result.stdout)} bytes")
            
            # Log success
            logger.info(f"snapshot captured {width}×? in {capture_time}ms")
            return result.stdout
            
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
            if attempt < retries:
                logger.warning(f"snapshot retry #{attempt + 1} failed: {e}")
                time.sleep(0.1)  # Brief pause before retry
            else:
                logger.error(f"snapshot failed after {retries + 1} attempts: {e}")
                raise Exception(f"Camera capture failed: {e}")
    
    raise Exception("Unexpected error in capture_jpeg")
