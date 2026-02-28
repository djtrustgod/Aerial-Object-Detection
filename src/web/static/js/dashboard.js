/**
 * Dashboard WebSocket client + Chart.js integration
 */

// --- WebSocket Stream ---
const canvas = document.getElementById('stream-canvas');
const ctx = canvas ? canvas.getContext('2d') : null;
const offlineOverlay = document.getElementById('stream-offline');

let streamWs = null;
let eventsWs = null;
let reconnectTimer = null;

function connectStream() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    streamWs = new WebSocket(`${protocol}//${location.host}/ws/stream`);
    streamWs.binaryType = 'arraybuffer';

    streamWs.onopen = () => {
        updateConnectionStatus(true);
        if (offlineOverlay) offlineOverlay.style.display = 'none';
    };

    streamWs.onmessage = (event) => {
        if (!ctx) return;
        const blob = new Blob([event.data], { type: 'image/jpeg' });
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
            canvas.width = img.width;
            canvas.height = img.height;
            ctx.drawImage(img, 0, 0);
            URL.revokeObjectURL(url);
        };
        img.src = url;
    };

    streamWs.onclose = () => {
        updateConnectionStatus(false);
        if (offlineOverlay) offlineOverlay.style.display = 'flex';
        scheduleReconnect();
    };

    streamWs.onerror = () => {
        streamWs.close();
    };
}

// --- WebSocket Events ---
const eventFeed = document.getElementById('event-feed');
const activeDetections = document.getElementById('active-detections');
let eventBuffer = [];
const MAX_EVENTS = 50;

// Chart.js stats
let statsChart = null;
const classCounts = { aircraft: 0, satellite: 0, uap: 0, unknown: 0 };

function connectEvents() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    eventsWs = new WebSocket(`${protocol}//${location.host}/ws/events`);

    eventsWs.onopen = () => {
        // Send a keepalive periodically
        setInterval(() => {
            if (eventsWs && eventsWs.readyState === WebSocket.OPEN) {
                eventsWs.send('ping');
            }
        }, 30000);
    };

    eventsWs.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleEvent(data);
        } catch (e) {
            // ignore parse errors
        }
    };

    eventsWs.onclose = () => {
        setTimeout(connectEvents, 5000);
    };
}

function handleEvent(data) {
    if (data.type === 'detection') {
        // Add to event feed
        eventBuffer.unshift(data);
        if (eventBuffer.length > MAX_EVENTS) eventBuffer.pop();
        renderEventFeed();

        // Update chart
        const cls = data.classification || 'unknown';
        if (cls in classCounts) {
            classCounts[cls]++;
            updateChart();
        }
    }
}

function renderEventFeed() {
    if (!eventFeed) return;

    if (eventBuffer.length === 0) {
        eventFeed.innerHTML = '<p class="empty-state">Waiting for events...</p>';
        return;
    }

    eventFeed.innerHTML = eventBuffer.map(e => `
        <div class="event-item">
            <span class="class-badge ${e.classification}">${e.classification}</span>
            <span>ID #${e.object_id}</span>
            <span>${(e.confidence * 100).toFixed(0)}%</span>
            <span>${e.speed ? e.speed.toFixed(1) + ' px/f' : ''}</span>
        </div>
    `).join('');
}

// --- Chart ---
function initChart() {
    const chartCanvas = document.getElementById('stats-chart');
    if (!chartCanvas) return;

    statsChart = new Chart(chartCanvas, {
        type: 'doughnut',
        data: {
            labels: ['Aircraft', 'Satellite', 'UAP', 'Unknown'],
            datasets: [{
                data: [0, 0, 0, 0],
                backgroundColor: [
                    'rgba(34, 197, 94, 0.7)',
                    'rgba(6, 182, 212, 0.7)',
                    'rgba(239, 68, 68, 0.7)',
                    'rgba(148, 163, 184, 0.4)',
                ],
                borderColor: [
                    'rgba(34, 197, 94, 1)',
                    'rgba(6, 182, 212, 1)',
                    'rgba(239, 68, 68, 1)',
                    'rgba(148, 163, 184, 1)',
                ],
                borderWidth: 1,
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: { color: '#94a3b8' },
                },
            },
        },
    });

    // Load initial stats
    loadInitialStats();
}

async function loadInitialStats() {
    try {
        const resp = await fetch('/api/event-stats');
        const stats = await resp.json();
        if (stats.by_class) {
            for (const [cls, count] of Object.entries(stats.by_class)) {
                if (cls in classCounts) classCounts[cls] = count;
            }
            updateChart();
        }
    } catch (e) {}
}

function updateChart() {
    if (!statsChart) return;
    statsChart.data.datasets[0].data = [
        classCounts.aircraft,
        classCounts.satellite,
        classCounts.uap,
        classCounts.unknown,
    ];
    statsChart.update();
}

// --- Stats Polling ---
async function pollStats() {
    try {
        const resp = await fetch('/api/stats');
        const stats = await resp.json();

        const fpsEl = document.getElementById('stream-fps');
        const tracksEl = document.getElementById('stream-tracks');
        if (fpsEl) fpsEl.textContent = `${stats.fps} FPS`;
        if (tracksEl) tracksEl.textContent = `${stats.active_tracks} tracks`;
        updateCameraStatus(stats.connected);

        const detEl = document.getElementById('detection-status');
        if (detEl) {
            if (!stats.schedule_enabled) {
                detEl.textContent = 'Detection On';
                detEl.className = 'badge';
            } else if (stats.detection_active) {
                detEl.textContent = 'Detection Active';
                detEl.className = 'badge detection-on';
            } else {
                detEl.textContent = 'Detection Scheduled';
                detEl.className = 'badge detection-off';
            }
        }
    } catch (e) {
        updateCameraStatus(false);
    }
}

// --- Connection Status ---
// Track both WebSocket and camera states independently
let wsConnected = false;
let cameraConnected = false;

function updateConnectionStatus(wsOnline) {
    wsConnected = wsOnline;
    renderConnectionStatus();
}

function updateCameraStatus(camOnline) {
    cameraConnected = camOnline;
    renderConnectionStatus();
}

function renderConnectionStatus() {
    const el = document.getElementById('connection-status');
    if (!el) return;

    if (!wsConnected) {
        el.textContent = 'Server Offline';
        el.className = 'status-indicator offline';
    } else if (!cameraConnected) {
        el.textContent = 'No Camera';
        el.className = 'status-indicator offline';
    } else {
        el.textContent = 'Camera Connected';
        el.className = 'status-indicator online';
    }
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectStream();
    }, 3000);
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    if (canvas) {
        connectStream();
        connectEvents();
        initChart();
        setInterval(pollStats, 2000);
        pollStats();
    }
});
