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

class MJPEGGrabber:
    """
    Continuous MJPEG grabber using rpicam-vid --codec mjpeg -t 0 -o -
    Parses concatenated JPEG frames (SOI 0xFFD8 ... EOI 0xFFD9) from stdout.
    Thread-safe: last_frame is updated atomically.
    """
    def __init__(self, width: int, height: int, fps: int = 8, extra_args: Optional[list] = None):
        self.logger = logging.getLogger("panel.camera.mjpeg")
        self.width = width
        self.height = height
        self.fps = fps
        self.extra_args = extra_args or []
        self.proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_frame: Optional[bytes] = None

    def start(self):
        if self.proc is not None:
            return
        cmd = [
            "/usr/bin/rpicam-vid",
            "-n",
            "-t", "0",
            "--codec", "mjpeg",
            "--width", str(self.width),
            "--height", str(self.height),
            "--framerate", str(self.fps),
            "-o", "-"
        ] + self.extra_args
        self.logger.info(f"starting MJPEG grabber: {' '.join(cmd)}")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop, name="mjpeg_reader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        self._thread = None

    def _reader_loop(self):
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"
        stdout = self.proc.stdout if self.proc else None
        last_log = 0.0
        while not self._stop.is_set() and stdout and not stdout.closed:
            chunk = stdout.read(4096)
            if not chunk:
                time.sleep(0.005)
                continue
            self._buf.extend(chunk)
            # Extract complete JPEGs from buffer
            while True:
                start = self._buf.find(SOI)
                if start == -1:
                    # no SOI yet, keep buffer from last 1KB to avoid unbounded growth
                    if len(self._buf) > 4096:
                        self._buf = self._buf[-4096:]
                    break
                end = self._buf.find(EOI, start + 2)
                if end == -1:
                    # wait for more data
                    # keep tail starting at SOI
                    if start > 0:
                        self._buf = self._buf[start:]
                    break
                # include EOI marker
                frame = bytes(self._buf[start:end+2])
                # cut consumed data
                self._buf = self._buf[end+2:]
                with self._lock:
                    self._last_frame = frame
                now = time.time()
                if now - last_log > 5.0:
                    self.logger.info(f"mjpeg frame ok: {len(frame)} bytes")
                    last_log = now

    def get_last_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._last_frame


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