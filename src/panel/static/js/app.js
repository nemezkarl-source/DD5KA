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
        this.initSpaViews();
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
            const response = await this.fetchWithTimeout('/api/logs/last?n=15', {}, 5000);
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

    initSpaViews() {
        // Навесить обработчики на все .nav-btn
        const navButtons = document.querySelectorAll('.nav-btn');
        navButtons.forEach(button => {
            button.addEventListener('click', () => {
                // Снять .is-active со всех кнопок
                navButtons.forEach(btn => btn.classList.remove('is-active'));
                // Поставить .is-active на выбранную кнопку
                button.classList.add('is-active');
                // Показать соответствующую вьюшку
                const viewName = button.getAttribute('data-view');
                this.showView(viewName);
            });
        });

        // При первом старте установить активной кнопку "Стрим" и показать view "stream"
        const streamButton = document.querySelector('[data-view="stream"]');
        if (streamButton) {
            streamButton.classList.add('is-active');
            this.showView('stream');
        }
    }

    showView(view) {
        // Скрыть все три секции
        const views = ['stream', 'photos', 'settings'];
        views.forEach(viewId => {
            const element = document.getElementById(`view-${viewId}`);
            if (element) {
                element.classList.add('hidden-view');
            }
        });

        // Показать выбранную секцию
        const targetView = document.getElementById(`view-${view}`);
        if (targetView) {
            targetView.classList.remove('hidden-view');
        }

        if (view === 'stream') {
            // Гарантировать, что стрим активен
            this.reconnectStream();
            this.hideFallback();
        } else {
            // Отключить поток для экономии трафика
            const img = document.getElementById('overlay');
            if (img) {
                img.src = '';
            }
        }
    }
}

// Gallery functionality (for /photos page)
class GalleryController {
    constructor() {
        this.currentOffset = 0;
        this.itemsPerPage = 60;
        this.init();
    }

    init() {
        const container = document.getElementById('gallery-container');
        if (!container) return; // Not on photos page

        this.loadGallery(this.itemsPerPage, 0);
        
        // Load more button handler
        const loadMoreBtn = document.getElementById('load-more-btn');
        if (loadMoreBtn) {
            loadMoreBtn.addEventListener('click', () => {
                this.loadGallery(this.itemsPerPage, this.currentOffset);
            });
        }
    }

    formatTime(timestamp) {
        const date = new Date(timestamp * 1000);
        return date.toLocaleTimeString('ru-RU', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
    }

    createGalleryItem(file) {
        const timeStr = this.formatTime(file.ts);
        return `
            <div class="gallery-item">
                <a href="/gallery/${file.file}" target="_blank">
                    <img src="/gallery/thumb/${file.file}" alt="Detection ${file.file}">
                    <div class="caption">${timeStr}</div>
                </a>
            </div>
        `;
    }

    async loadGallery(n = 60, offset = 0) {
        const container = document.getElementById('gallery-container');
        const loadingIndicator = document.getElementById('loading-indicator');
        const loadMoreBtn = document.getElementById('load-more-btn');
        
        if (!container) return;
        
        try {
            if (loadingIndicator) loadingIndicator.classList.remove('hidden');
            if (loadMoreBtn) loadMoreBtn.disabled = true;
            
            const response = await fetch(`/api/gallery/index?n=${n}&offset=${offset}`);
            const data = await response.json();
            
            if (data.files && data.files.length > 0) {
                const html = data.files.map(file => this.createGalleryItem(file)).join('');
                if (offset === 0) {
                    container.innerHTML = html;
                } else {
                    container.insertAdjacentHTML('beforeend', html);
                }
                this.currentOffset = offset + data.files.length;
                
                // Hide load more button if we've loaded all items
                if (this.currentOffset >= data.total && loadMoreBtn) {
                    loadMoreBtn.style.display = 'none';
                }
            } else if (offset === 0) {
                container.innerHTML = '<p style="text-align: center; color: #666;">Нет изображений в галерее</p>';
            }
            
        } catch (error) {
            console.error('Failed to load gallery:', error);
            if (offset === 0) {
                container.innerHTML = '<p style="text-align: center; color: #f00;">Ошибка загрузки галереи</p>';
            }
        } finally {
            if (loadingIndicator) loadingIndicator.classList.add('hidden');
            if (loadMoreBtn) loadMoreBtn.disabled = false;
        }
    }
}

// Settings functionality (for /settings page)
class SettingsController {
    constructor() {
        this.init();
    }

    init() {
        const settingsRoot = document.getElementById('settings-root');
        if (!settingsRoot) return; // Not on settings page

        this.loadSettings();
        this.bindEvents();
    }

    bindEvents() {
        // Panel settings form
        const panelForm = document.getElementById('panel-settings-form');
        if (panelForm) {
            panelForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.savePanelSettings();
            });
        }

        // Detector settings form
        const detectorForm = document.getElementById('detector-settings-form');
        if (detectorForm) {
            detectorForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.saveDetectorSettings();
            });
        }
    }

    async loadSettings() {
        try {
            // Load panel settings
            const panelResponse = await fetch('/api/settings/panel');
            if (panelResponse.ok) {
                const panelSettings = await panelResponse.json();
                this.populateForm('panel-settings-form', panelSettings);
            }

            // Load detector settings
            const detectorResponse = await fetch('/api/settings/detector');
            if (detectorResponse.ok) {
                const detectorSettings = await detectorResponse.json();
                this.populateForm('detector-settings-form', detectorSettings);
            }
        } catch (error) {
            console.error('Failed to load settings:', error);
            this.showToast('Ошибка загрузки настроек', 'error');
        }
    }

    populateForm(formId, settings) {
        const form = document.getElementById(formId);
        if (!form) return;

        Object.keys(settings).forEach(key => {
            const input = form.querySelector(`[name="${key}"]`);
            if (input) {
                input.value = settings[key];
            }
        });
    }

    async savePanelSettings() {
        const form = document.getElementById('panel-settings-form');
        const button = form.querySelector('button[type="submit"]');
        
        try {
            this.setButtonLoading(button, true);
            
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            
            // Convert string values to appropriate types
            if (data.overlay_min_conf) {
                data.overlay_min_conf = parseFloat(data.overlay_min_conf);
            }
            if (data.overlay_det_max_age_ms) {
                data.overlay_det_max_age_ms = parseInt(data.overlay_det_max_age_ms);
            }

            const response = await fetch('/api/settings/panel', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            });

            const result = await response.json();

            if (response.ok) {
                this.showToast('Настройки панели сохранены', 'success');
            } else {
                this.showToast(`Ошибка: ${result.error}`, 'error');
            }
        } catch (error) {
            console.error('Save panel settings failed:', error);
            this.showToast('Ошибка сохранения настроек панели', 'error');
        } finally {
            this.setButtonLoading(button, false);
        }
    }

    async saveDetectorSettings() {
        const form = document.getElementById('detector-settings-form');
        const button = form.querySelector('button[type="submit"]');
        
        try {
            this.setButtonLoading(button, true);
            
            const formData = new FormData(form);
            const data = Object.fromEntries(formData.entries());
            
            // Convert string values to appropriate types
            if (data.detector_conf_threshold) {
                data.detector_conf_threshold = parseFloat(data.detector_conf_threshold);
            }

            // Save detector settings
            const settingsResponse = await fetch('/api/settings/detector', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            });

            const settingsResult = await settingsResponse.json();

            if (!settingsResponse.ok) {
                this.showToast(`Ошибка сохранения: ${settingsResult.error}`, 'error');
                return;
            }

            // Restart detector service
            const restartResponse = await fetch('/api/detector/restart', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            const restartResult = await restartResponse.json();

            if (restartResponse.ok) {
                this.showToast('Настройки детектора сохранены и сервис перезапущен', 'success');
            } else {
                this.showToast(`Настройки сохранены, но ошибка перезапуска: ${restartResult.error}`, 'error');
            }
        } catch (error) {
            console.error('Save detector settings failed:', error);
            this.showToast('Ошибка сохранения настроек детектора', 'error');
        } finally {
            this.setButtonLoading(button, false);
        }
    }

    setButtonLoading(button, loading) {
        if (loading) {
            button.disabled = true;
            button.classList.add('loading');
        } else {
            button.disabled = false;
            button.classList.remove('loading');
        }
    }

    showToast(message, type = 'info') {
        // Create toast element if it doesn't exist
        let toast = document.getElementById('toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'toast';
            toast.className = 'toast hidden';
            document.body.appendChild(toast);
        }

        toast.textContent = message;
        toast.className = `toast ${type}`;
        
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 3000);
    }
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    new PanelController();
    new GalleryController();
    new SettingsController();
});

// CHANGELOG
// Переписан app.js: удалена логика /snapshot, добавлена логика fallback для /overlay.mjpg с таймером 1200ms, реализовано авто-переподключение потока, добавлены методы showFallback/hideFallback с плавным исчезновением
// Этап 2: SPA-переключение контента + 15 событий в логе