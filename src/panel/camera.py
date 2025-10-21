#!/usr/bin/env python3
"""
DD-5KA Camera Helper
Direct rpicam-still capture with retries and serialization
"""

import logging
import os
import random
import subprocess
import threading
import time
from typing import Optional


# Global lock for camera access serialization
SNAPSHOT_LOCK = threading.Lock()


def capture_jpeg(max_side: int = 1280, timeout_ms: int = 120, retries: int = 2) -> bytes:
    """
    Capture JPEG from camera with retries and backoff
    
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
    
    # Parse backoff intervals from environment
    backoff_str = os.getenv("SNAPSHOT_BACKOFF_MS", "100,220")
    try:
        backoff_intervals = [int(x.strip()) for x in backoff_str.split(",")]
        if len(backoff_intervals) < 2:
            backoff_intervals = [100, 220]  # Default fallback
    except (ValueError, AttributeError):
        backoff_intervals = [100, 220]  # Default fallback
    
    # Get extra command flags from environment
    extra_flags = os.getenv("SNAPSHOT_CMD_EXTRA", "").strip()
    extra_args = extra_flags.split() if extra_flags else []
    
    # Calculate dimensions for rpicam-still based on original aspect ratio
    src_w, src_h = 4056, 3040  # Original camera resolution
    if max(src_w, src_h) > max_side:
        k = max_side / max(src_w, src_h)
        w = int(src_w * k) // 2 * 2  # Even numbers for better encoding
        h = int(src_h * k) // 2 * 2
    else:
        w, h = src_w, src_h
    
    # Serialize camera access with global lock
    with SNAPSHOT_LOCK:
        total_start_time = time.time()
        
        for attempt in range(retries + 1):
            try:
                start_time = time.time()
                
                # Build rpicam-still command with extra flags
                cmd = [
                    "/usr/bin/rpicam-still",
                    "-n",  # no preview
                    "-o", "-",  # output to stdout
                    "-t", str(timeout_ms),
                    "--quality", "70",
                    "--thumb", "none",
                    "--width", str(w),
                    "--height", str(h),
                    "--immediate"  # Start capture immediately
                ] + extra_args
                
                # Execute capture with stderr capture for diagnostics
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=(timeout_ms / 1000) + 2,  # Add 2s buffer
                    check=True
                )
                
                capture_time = int((time.time() - start_time) * 1000)
                total_time = int((time.time() - total_start_time) * 1000)
                
                if len(result.stdout) < 1000:  # Too small for valid JPEG
                    raise ValueError(f"Invalid JPEG size: {len(result.stdout)} bytes")
                
                # Log success
                logger.info(f"snapshot captured {w}x{h} in {capture_time}ms (total: {total_time}ms)")
                return result.stdout
                
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
                if attempt < retries:
                    # Calculate backoff with jitter
                    if attempt < len(backoff_intervals):
                        base_delay = backoff_intervals[attempt]
                    else:
                        base_delay = 200  # Fallback for more attempts
                    
                    # Add jitter: ±20% of base delay
                    jitter = random.uniform(-0.2, 0.2) * base_delay
                    delay_ms = max(10, int(base_delay + jitter))  # Minimum 10ms
                    
                    # Capture stderr for diagnostics (first 200 chars)
                    stderr_info = ""
                    if hasattr(e, 'stderr') and e.stderr:
                        stderr_info = e.stderr.decode('utf-8', errors='ignore')[:200]
                    
                    logger.warning(f"snapshot retry #{attempt + 1} failed (code={getattr(e, 'returncode', 'unknown')}, stderr={stderr_info})")
                    logger.info(f"retry #{attempt + 1} backoff: {delay_ms}ms")
                    
                    time.sleep(delay_ms / 1000.0)
                else:
                    # Final attempt failed
                    stderr_info = ""
                    if hasattr(e, 'stderr') and e.stderr:
                        stderr_info = e.stderr.decode('utf-8', errors='ignore')[:200]
                    
                    total_time = int((time.time() - total_start_time) * 1000)
                    logger.error(f"snapshot failed after {retries + 1} attempts (total: {total_time}ms, stderr={stderr_info}): {e}")
                    raise Exception(f"Camera capture failed: {e}")
        
        raise Exception("Unexpected error in capture_jpeg")


def is_camera_busy() -> bool:
    """
    Check if camera is currently busy (lock is held)
    
    Returns:
        True if camera is busy, False otherwise
    """
    return SNAPSHOT_LOCK.locked()