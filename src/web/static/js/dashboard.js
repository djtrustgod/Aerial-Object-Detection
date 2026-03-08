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
let totalDetections = 0;
let hourlyData = new Array(24).fill(0);
let hourlySlots = [];  // rolling 24-hour slot keys like "2026-03-07 20"

function buildHourlySlots() {
    const slots = [];
    const now = new Date();
    // Start from 23 hours ago, build 24 slots up to current hour
    for (let i = 23; i >= 0; i--) {
        const d = new Date(now.getTime() - i * 3600000);
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        const hh = String(d.getHours()).padStart(2, '0');
        slots.push(`${yyyy}-${mm}-${dd} ${hh}`);
    }
    return slots;
}

function getHourLabels(slots) {
    return slots.map(s => s.slice(11, 13));  // extract "HH"
}

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

        // Update chart — increment current hour's bar
        totalDetections++;
        // Current hour is always the last slot in the rolling window
        hourlyData[hourlyData.length - 1]++;
        updateChart();
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
            <span>ID #${e.object_id}</span>
            <span>${e.speed ? e.speed.toFixed(1) + ' px/f' : ''}</span>
            <span>${e.trajectory_length ? e.trajectory_length + ' pts' : ''}</span>
        </div>
    `).join('');
}

// --- Chart ---
function initChart() {
    const chartCanvas = document.getElementById('stats-chart');
    if (!chartCanvas) return;

    hourlySlots = buildHourlySlots();

    statsChart = new Chart(chartCanvas, {
        type: 'bar',
        data: {
            labels: getHourLabels(hourlySlots),
            datasets: [{
                label: 'Events',
                data: hourlyData,
                backgroundColor: 'rgba(34, 197, 94, 0.6)',
                borderColor: 'rgba(34, 197, 94, 1)',
                borderWidth: 1,
                borderRadius: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
            },
            scales: {
                x: {
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { display: false },
                },
                y: {
                    beginAtZero: true,
                    ticks: {
                        color: '#64748b',
                        precision: 0,
                    },
                    grid: { color: 'rgba(100, 116, 139, 0.15)' },
                },
            },
        },
    });

    // Load initial stats
    loadInitialStats();
    // Refresh hourly data every 60s to stay in sync
    setInterval(loadInitialStats, 60000);
}

async function loadInitialStats() {
    try {
        const resp = await fetch('/api/event-stats');
        const stats = await resp.json();
        totalDetections = stats.total || 0;
        // Rebuild slots in case hour has rolled over
        hourlySlots = buildHourlySlots();
        hourlyData = hourlySlots.map(slot => (stats.hourly_map && stats.hourly_map[slot]) || 0);
        if (statsChart) {
            statsChart.data.labels = getHourLabels(hourlySlots);
        }
        updateChart();
    } catch (e) {}
}

function updateChart() {
    if (!statsChart) return;
    statsChart.data.datasets[0].data = hourlyData;
    statsChart.update();
}

// --- Detection Toggle ---
async function toggleDetection() {
    const btn = document.getElementById('detection-toggle');
    const currentlyActive = btn && btn.textContent.trim() === 'Stop Detection';
    await fetch('/api/detection/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !currentlyActive }),
    });
    await pollStats();
}

// --- Stats Polling ---
async function pollStats() {
    try {
        const resp = await fetch('/api/stats');
        const stats = await resp.json();

        updateCameraStatus(stats.connected);
        updateActiveDetections(stats.active_tracks);

        const detEl = document.getElementById('detection-toggle');
        if (detEl) {
            if (stats.detection_active) {
                detEl.textContent = 'Stop Detection';
                detEl.className = 'badge detection-on';
            } else {
                detEl.textContent = 'Enable Detection';
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

function updateActiveDetections(count) {
    if (!activeDetections) return;
    if (count === 0) {
        activeDetections.innerHTML = '<p class="empty-state">No active detections</p>';
    } else {
        activeDetections.innerHTML = `<div class="event-item"><span>${count} object${count !== 1 ? 's' : ''} currently tracked</span></div>`;
    }
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectStream();
    }, 3000);
}

// --- Load initial recent events from API ---
async function loadRecentEvents() {
    try {
        const resp = await fetch('/api/events?limit=50');
        const events = await resp.json();
        eventBuffer = events.map(e => ({
            type: 'detection',
            event_id: e.event_id,
            object_id: e.object_id,
            speed: e.avg_speed,
            trajectory_length: e.trajectory_length,
        }));
        renderEventFeed();
    } catch (e) {}
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    if (canvas) {
        connectStream();
        connectEvents();
        initChart();
        loadRecentEvents();
        setInterval(pollStats, 2000);
        pollStats();
    }
});
