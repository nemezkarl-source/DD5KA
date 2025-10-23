// DD::5::KA Panel JavaScript

class PanelController {
    constructor() {
        this.isRequestInProgress = false;
        this.fallbackTimer = null;
        this.isFallbackVisible = false;
        this.firstFrameOk = false;
        this.lastReconnectAt = 0;
        this.init();
    }

    init() {
        this.bindEvents();
        this.startPolling();
        this.updateStatus();
        this.handleResize();
        this.initOverlayStream();
    }

    bindEvents() {
        // Detector control buttons
        document.getElementById('btn-start').addEventListener('click', () => this.controlDetector('start'));
        document.getElementById('btn-stop').addEventListener('click', () => this.controlDetector('stop'));
        document.getElementById('btn-restart').addEventListener('click', () => this.controlDetector('restart'));
        
        // LED test button
        document.getElementById('btn-led').addEventListener('click', () => this.testLED());
        
        // Window resize handler
        window.addEventListener('resize', () => this.debounceResize());
    }

    initOverlayStream() {
        const overlay = document.getElementById('overlay');
        const fallback = document.getElementById('overlay-fallback');
        
        if (!overlay || !fallback) return;

        // Handle stream errors
        overlay.addEventListener('error', () => {
            // мягко подождать и только потом реконнект
            if (this.fallbackTimer) clearTimeout(this.fallbackTimer);
            this.showFallback();
            setTimeout(() => this.reconnectStream(), 800);
        });

        // Handle successful stream load
        overlay.addEventListener('load', () => {
            // load у MJPEG срабатывает на первый кадр
            this.firstFrameOk = true;
            this.hideFallback();
        });

        // Start the stream
        this.reconnectStream();
    }


    showFallback() {
        const fallback = document.getElementById('overlay-fallback');
        if (!fallback || this.isFallbackVisible) return;

        console.log('Showing fallback overlay');
        fallback.classList.remove('hidden');
        this.isFallbackVisible = true;
    }

    hideFallback() {
        const fallback = document.getElementById('overlay-fallback');
        if (!fallback || !this.isFallbackVisible) return;

        console.log('Hiding fallback overlay');
        
        // Clear any pending timer
        if (this.fallbackTimer) {
            clearTimeout(this.fallbackTimer);
            this.fallbackTimer = null;
        }

        // Smooth fade out (150-250ms)
        fallback.style.transition = 'opacity 200ms ease-out';
        fallback.style.opacity = '0';
        
        setTimeout(() => {
            fallback.classList.add('hidden');
            fallback.style.transition = '';
            fallback.style.opacity = '';
            this.isFallbackVisible = false;
        }, 200);
    }

    reconnectStream() {
        const overlay = document.getElementById('overlay');
        if (!overlay) return;

        const now = Date.now();
        if (now - this.lastReconnectAt < 5000) { // не чаще, чем раз в 5с
            return;
        }
        this.lastReconnectAt = now;
        this.firstFrameOk = false;

        const url = '/overlay.mjpg?t=' + now; // cache-buster
        overlay.src = url;

        // watchdog R2: если за 1200 мс не пришёл первый load — показать fallback
        if (this.fallbackTimer) clearTimeout(this.fallbackTimer);
        this.fallbackTimer = setTimeout(() => {
            if (!this.firstFrameOk) this.showFallback();
        }, 1200);
    }

    async controlDetector(action) {
        if (this.isRequestInProgress) return;
        
        this.isRequestInProgress = true;
        const button = document.getElementById(`btn-${action}`);
        const originalText = button.textContent;
        
        try {
            button.textContent = '...';
            button.disabled = true;
            button.classList.add('btn-loading');
            
            const response = await this.fetchWithTimeout(`/api/detector/${action}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }, 7000);
            
            const result = await response.json();
            
            if (result.ok) {
                const actionText = action === 'start' ? 'STARTED' : action === 'stop' ? 'STOPPED' : 'RESTARTED';
                this.showToast(`Detector ${actionText}`, 'success');
                // Update status immediately after success
                setTimeout(() => this.updateStatus(), 500);
            } else {
                this.showToast(`Error: ${result.error || 'Unknown error'}`, 'error');
            }
        } catch (error) {
            if (error.name === 'AbortError') {
                this.showToast('Timeout: Request took too long', 'error');
            } else {
                this.showToast(`Network error: ${error.message}`, 'error');
            }
        } finally {
            button.textContent = originalText;
            button.disabled = false;
            button.classList.remove('btn-loading');
            this.isRequestInProgress = false;
        }
    }

    async testLED() {
        if (this.isRequestInProgress) return;
        
        this.isRequestInProgress = true;
        const button = document.getElementById('btn-led');
        const originalText = button.textContent;
        
        try {
            button.textContent = '...';
            button.disabled = true;
            button.classList.add('btn-loading');
            
            const response = await this.fetchWithTimeout('/api/led/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }, 7000);
            
            const result = await response.json();
            
            if (result.ok) {
                this.showToast('LED OK', 'success');
                // Update LED status immediately
                setTimeout(() => this.updateStatus(), 500);
            } else {
                this.showToast(`LED Error: ${result.error || 'Unknown error'}`, 'error');
            }
        } catch (error) {
            if (error.name === 'AbortError') {
                this.showToast('LED Timeout', 'error');
            } else {
                this.showToast(`LED Error: ${error.message}`, 'error');
            }
        } finally {
            button.textContent = originalText;
            button.disabled = false;
            button.classList.remove('btn-loading');
            this.isRequestInProgress = false;
        }
    }

    async updateStatus() {
        try {
            // Update detector status
            const detectorResponse = await this.fetchWithTimeout('/api/detector/status', {}, 5000);
            const detectorStatus = await detectorResponse.json();
            const isDetectorActive = detectorStatus.active_state === 'active';
            this.updateStatusDot('detector-status', isDetectorActive);

            // Update health status (includes camera)
            const healthResponse = await this.fetchWithTimeout('/api/health', {}, 5000);
            const healthStatus = await healthResponse.json();
            this.updateStatusDot('camera-status', healthStatus.camera === 'ok');

            // Update LED status from server
            const ledResponse = await this.fetchWithTimeout('/api/led/status', {}, 5000);
            const ledStatus = await ledResponse.json();
            this.updateStatusDot('led-status', ledStatus.ok);
            this.updateStatusDot('setkomet-status', ledStatus.ok); // Same as LED status

            // Update network status
            const nmResponse = await this.fetchWithTimeout('/api/nm/status', {}, 5000);
            const nmStatus = await nmResponse.json();
            this.updateNetworkStatus(nmStatus);

        } catch (error) {
            console.error('Status update failed:', error);
        }
    }

    async updateEvents() {
        try {
            const response = await this.fetchWithTimeout('/api/logs/last?n=10', {}, 5000);
            const events = await response.json();
            this.renderEvents(events.events || []);
        } catch (error) {
            console.error('Events update failed:', error);
        }
    }

    updateStatusDot(elementId, isOk) {
        const element = document.getElementById(elementId);
        if (element) {
            element.className = `status-dot ${isOk ? 'ok' : 'bad'}`;
        }
    }

    updateNetworkStatus(status) {
        document.getElementById('nm-mode').textContent = status.mode || '—';
        document.getElementById('nm-ifname').textContent = status.ifname || '—';
        document.getElementById('nm-ssid').textContent = status.ssid || '—';
        document.getElementById('nm-connected').textContent = status.connected ? 'Да' : 'Нет';
    }

    renderEvents(events) {
        const container = document.getElementById('events-list');
        if (!container) return;

        if (events.length === 0) {
            container.innerHTML = '<div class="event-item"><span class="event-time">—</span><span class="event-class">—</span><span class="event-conf">—</span><span class="event-bbox">—</span></div>';
            return;
        }

        container.innerHTML = events.map(event => {
            const timestamp = new Date(event.ts).toLocaleTimeString('ru-RU');
            const detections = event.detections || [];
            const firstDetection = detections[0];
            
            return `
                <div class="event-item">
                    <span class="event-time">${timestamp}</span>
                    <span class="event-class">${firstDetection ? firstDetection.class_name : '—'}</span>
                    <span class="event-conf">${firstDetection ? (firstDetection.conf * 100).toFixed(1) + '%' : '—'}</span>
                    <span class="event-bbox">${firstDetection ? `[${firstDetection.bbox_xyxy.map(x => Math.round(x)).join(',')}]` : '—'}</span>
                </div>
            `;
        }).join('');
    }

    showToast(message, type = 'info') {
        const toast = document.getElementById('toast');
        if (!toast) return;

        toast.textContent = message;
        toast.className = `toast ${type}`;
        
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 3000);
    }

    async fetchWithTimeout(url, options = {}, timeout = 5000) {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeout);
        
        try {
            const response = await fetch(url, {
                ...options,
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            return response;
        } catch (error) {
            clearTimeout(timeoutId);
            throw error;
        }
    }

    startPolling() {
        // Status polling every 2 seconds
        setInterval(() => {
            this.updateStatus();
        }, 2000);

        // Events polling every 3 seconds
        setInterval(() => {
            this.updateEvents();
        }, 3000);
    }

    handleResize() {
        // Ensure stream image fits properly
        const overlay = document.getElementById('overlay');
        if (overlay) {
            overlay.style.maxWidth = '100%';
            overlay.style.maxHeight = '100%';
            overlay.style.objectFit = 'contain';
        }
    }

    debounceResize() {
        clearTimeout(this.resizeTimeout);
        this.resizeTimeout = setTimeout(() => {
            this.handleResize();
        }, 100);
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    new PanelController();
});

// CHANGELOG
// Переписан app.js: удалена логика /snapshot, добавлена логика fallback для /overlay.mjpg с таймером 1200ms, реализовано авто-переподключение потока, добавлены методы showFallback/hideFallback с плавным исчезновением