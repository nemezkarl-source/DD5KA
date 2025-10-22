// DD::5::KA Panel JavaScript

class PanelController {
    constructor() {
        this.isRequestInProgress = false;
        this.ledTestSuccess = false;
        this.ledTestTime = null;
        this.init();
    }

    init() {
        this.bindEvents();
        this.startPolling();
        this.updateStatus();
    }

    bindEvents() {
        // Detector control buttons
        document.getElementById('detector-start').addEventListener('click', () => this.controlDetector('start'));
        document.getElementById('detector-stop').addEventListener('click', () => this.controlDetector('stop'));
        document.getElementById('detector-restart').addEventListener('click', () => this.controlDetector('restart'));
        
        // LED test button
        document.getElementById('led-test').addEventListener('click', () => this.testLED());
    }

    async controlDetector(action) {
        if (this.isRequestInProgress) return;
        
        this.isRequestInProgress = true;
        const button = document.getElementById(`detector-${action}`);
        const originalText = button.textContent;
        
        try {
            button.textContent = 'Выполняется...';
            button.disabled = true;
            
            const response = await this.fetchWithTimeout(`/api/detector/${action}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }, 7000);
            
            const result = await response.json();
            
            if (result.ok) {
                this.showToast(`Детектор ${action === 'start' ? 'запущен' : action === 'stop' ? 'остановлен' : 'перезапущен'}`, 'success');
                this.updateStatus();
            } else {
                this.showToast(`Ошибка: ${result.error || 'Неизвестная ошибка'}`, 'error');
            }
        } catch (error) {
            this.showToast(`Ошибка сети: ${error.message}`, 'error');
        } finally {
            button.textContent = originalText;
            button.disabled = false;
            this.isRequestInProgress = false;
        }
    }

    async testLED() {
        if (this.isRequestInProgress) return;
        
        this.isRequestInProgress = true;
        const button = document.getElementById('led-test');
        const originalText = button.textContent;
        
        try {
            button.textContent = 'Тестируем...';
            button.disabled = true;
            
            const response = await this.fetchWithTimeout('/api/led/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            }, 7000);
            
            const result = await response.json();
            
            if (result.ok) {
                this.ledTestSuccess = true;
                this.ledTestTime = Date.now();
                this.showToast('LED тест успешен', 'success');
            } else {
                this.ledTestSuccess = false;
                this.showToast(`LED тест не удался: ${result.error || 'Неизвестная ошибка'}`, 'error');
            }
        } catch (error) {
            this.ledTestSuccess = false;
            this.showToast(`Ошибка LED теста: ${error.message}`, 'error');
        } finally {
            button.textContent = originalText;
            button.disabled = false;
            this.isRequestInProgress = false;
        }
    }

    async updateStatus() {
        try {
            // Update detector status
            const detectorResponse = await this.fetchWithTimeout('/api/detector/status', {}, 5000);
            const detectorStatus = await detectorResponse.json();
            this.updateStatusDot('detector-status', detectorStatus.status === 'active');

            // Update health status (includes camera)
            const healthResponse = await this.fetchWithTimeout('/api/health', {}, 5000);
            const healthStatus = await healthResponse.json();
            this.updateStatusDot('camera-status', healthStatus.camera === 'ok');

            // Update LED status
            const ledOk = this.ledTestSuccess && (Date.now() - this.ledTestTime) < 5 * 60 * 1000; // 5 minutes
            this.updateStatusDot('led-status', ledOk);
            this.updateStatusDot('setkomet-status', ledOk); // Same as LED status

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
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    new PanelController();
});
