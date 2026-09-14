/**
 * Tactical Drone Remote ID Airspace Monitor - Main Application Orchestrator
 * Integrates Map, Feed, Telemetry Inspector, Timeline Scrubber, Multi-Node Sensors, and WebSocket Live Feed.
 */

import { TacticalMapController } from './map.js';
import { EncountersFeedController, formatDmyDate } from './feed.js';
import { TelemetryInspectorController } from './inspector.js';
import { TimelineScrubberController } from './scrubber.js';
import { DeepPacketInspectorController } from './packet_modal.js';

class TacticalApp {
  constructor() {
    this.currentMode = 'live'; // 'live' or 'replay'
    this.selectedEncounterId = null;
    this.currentEncounter = null;
    this.ws = null;
    this.statsInterval = null;
    this.feedInterval = null;
    this.nodesInterval = null;
    this.dashboardConfig = null;
    this.nodesList = [];

    this.mapCtrl = null;
    this.feedCtrl = null;
    this.inspectorCtrl = null;
    this.scrubberCtrl = null;
    this.modalCtrl = null;

    this.init();
  }

  async init() {
    // 1. Initialize Sub-Controllers
    this.modalCtrl = new DeepPacketInspectorController();
    
    this.inspectorCtrl = new TelemetryInspectorController((encId) => {
      const enc = this.currentEncounter;
      const nodeId = enc && enc.encounter_id === encId ? enc.node_id : null;
      this.modalCtrl.open(encId, nodeId);
    });

    this.scrubberCtrl = new TimelineScrubberController((ptIndex, pt) => {
      if (this.selectedEncounterId) {
        this.mapCtrl.setScrubberFix(this.selectedEncounterId, ptIndex, pt);
        this.inspectorCtrl.updateLiveGauges(pt);
      }
    });

    this.feedCtrl = new EncountersFeedController('encounters-list', 'encounter-feed-count', (encId) => {
      this.selectEncounter(encId);
    });

    this.mapCtrl = new TacticalMapController(
      'leaflet-map',
      (encId) => this.selectEncounter(encId),
      (ptIndex, pt) => {
        this.scrubberCtrl.seekTo(ptIndex);
      },
      (nodeId, lat, lon) => {
        this.updateNodePosition(nodeId, lat, lon);
      }
    );

    // 2. Setup HUD Controls & Mode Switcher
    this.initHudControls();

    // 3. Setup Quick Map Controls
    this.initMapControls();

    // 4. Setup Settings Modal (Sensors & Viewport)
    this.initReceiverModal();

    // 5. Load Viewport Configuration from Disk
    await this.fetchDashboardConfig();

    // 6. Start Live Services
    this.fetchStats();
    this.fetchEncounters();
    await this.fetchNodes();
    this.connectWebSocket();

    this.statsInterval = setInterval(() => this.fetchStats(), 4000);
    this.feedInterval = setInterval(() => this.fetchEncounters(), 3000);
    this.nodesInterval = setInterval(() => this.fetchNodes(), 8000);
  }

  async fetchNodes() {
    try {
      const res = await fetch('/api/nodes');
      if (res.ok) {
        const data = await res.json();
        const nodes = (data && data.nodes) || [];
        this.nodesList = nodes;
        if (this.mapCtrl) this.mapCtrl.setNodesList(nodes);
        if (this.inspectorCtrl) this.inspectorCtrl.setNodesList(nodes);
        if (this.feedCtrl) this.feedCtrl.setNodesList(nodes);
        if (this.scrubberCtrl) this.scrubberCtrl.setNodesList(nodes);
        if (this.modalCtrl) this.modalCtrl.setNodesList(nodes);
        this.renderModalNodes();
      }
    } catch (e) {
      // Standalone mode or transient error
    }
  }

  async updateNodePosition(nodeId, lat, lon) {
    try {
      const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/position`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ latitude: lat, longitude: lon }),
      });
      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `Server returned HTTP ${resp.status}`);
      }
      const data = await resp.json();
      console.log(`[+] Sensor ${nodeId} position calibrated to:`, lat, lon, data);
      await this.fetchNodes();
    } catch (err) {
      console.error(`Failed to update position for sensor ${nodeId}:`, err);
      alert(`Could not save position for node ${nodeId}: ${err.message}`);
      await this.fetchNodes(); // Revert marker on map
    }
  }

  initHudControls() {
    const btnLive = document.getElementById('btn-mode-live');
    const btnReplay = document.getElementById('btn-mode-replay');

    if (btnLive) {
      btnLive.addEventListener('click', () => {
        // If a historical closed flight is currently selected, deselect it to return to live airspace
        if (this.currentMode === 'replay' || (this.selectedEncounterId && this.currentEncounter && !this.currentEncounter.is_active)) {
          this.deselectEncounter();
        }
        this.setMode('live');
      });
    }

    if (btnReplay) {
      btnReplay.addEventListener('click', () => {
        // If no encounter is selected, pick the most relevant historical flight from the feed
        if (!this.selectedEncounterId) {
          const encounters = this.feedCtrl.encounters || [];
          const target = encounters.find(e => !e.is_active) || encounters[0];
          if (target) {
            this.selectEncounter(target.encounter_id);
          }
        }
        this.setMode('replay');
      });
    }

    const btnCloseTrack = document.getElementById('btn-close-track');
    if (btnCloseTrack) {
      btnCloseTrack.addEventListener('click', () => {
        this.deselectEncounter();
      });
    }
  }

  initMapControls() {
    const btnRecenter = document.getElementById('btn-recenter');
    if (btnRecenter) {
      btnRecenter.addEventListener('click', () => {
        this.mapCtrl.fitAirspace();
      });
    }

    const btnCenterOperator = document.getElementById('btn-center-operator');
    if (btnCenterOperator) {
      btnCenterOperator.addEventListener('click', () => {
        this.mapCtrl.centerOnOperator();
      });
    }

    const btnCenterReceiver = document.getElementById('btn-center-receiver');
    if (btnCenterReceiver) {
      btnCenterReceiver.addEventListener('click', () => {
        this.mapCtrl.centerOnNodes();
      });
    }

    const btnToggleRings = document.getElementById('btn-toggle-rings');
    if (btnToggleRings) {
      btnToggleRings.addEventListener('click', () => {
        const isActive = btnToggleRings.classList.toggle('active');
        this.mapCtrl.toggleRangeRings(isActive);
      });
    }

    const btnTrails = document.getElementById('btn-toggle-paths');
    if (btnTrails) {
      btnTrails.addEventListener('click', () => {
        const isActive = btnTrails.classList.toggle('active');
        this.mapCtrl.toggleTrails(isActive);
      });
    }

    const btnWaypoints = document.getElementById('btn-toggle-waypoints');
    if (btnWaypoints) {
      btnWaypoints.addEventListener('click', () => {
        const isActive = btnWaypoints.classList.toggle('active');
        this.mapCtrl.toggleWaypoints(isActive);
      });
    }

    const btnOpenRxConfig = document.getElementById('btn-open-receiver-config');
    if (btnOpenRxConfig) {
      btnOpenRxConfig.addEventListener('click', () => {
        this.openReceiverModal();
      });
    }
  }

  /**
   * Initializes Sensors & Viewport Configuration Modal
   */
  initReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    const form = document.getElementById('form-receiver-config');
    const btnClose = document.getElementById('btn-close-receiver-modal');
    const btnCancel = document.getElementById('btn-rx-cancel');
    const btnGps = document.getElementById('btn-rx-gps-detect');
    const btnFitSensors = document.getElementById('btn-rx-fit-sensors');

    if (btnClose) {
      btnClose.addEventListener('click', () => this.closeReceiverModal());
    }
    if (btnCancel) {
      btnCancel.addEventListener('click', () => this.closeReceiverModal());
    }

    // Modal background click closes modal
    if (modal) {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) this.closeReceiverModal();
      });
    }

    // Browser GPS Auto-Detect for center position
    if (btnGps) {
      btnGps.addEventListener('click', () => {
        if ('geolocation' in navigator) {
          btnGps.textContent = '⏳ Acquiring GPS...';
          navigator.geolocation.getCurrentPosition(
            (pos) => {
              const latEl = document.getElementById('cfg-rx-lat');
              const lonEl = document.getElementById('cfg-rx-lon');
              if (latEl) latEl.value = pos.coords.latitude.toFixed(6);
              if (lonEl) lonEl.value = pos.coords.longitude.toFixed(6);
              btnGps.innerHTML = `
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="12 2 15 9 22 12 15 15 12 22 9 15 2 12 9 9"/></svg> Use Browser GPS
              `;
            },
            (err) => {
              alert(`Could not acquire GPS position: ${err.message}`);
              btnGps.innerHTML = `
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="12 2 15 9 22 12 15 15 12 22 9 15 2 12 9 9"/></svg> Use Browser GPS
              `;
            },
            { enableHighAccuracy: true, timeout: 8000 }
          );
        } else {
          alert('Geolocation is not supported by your browser.');
        }
      });
    }

    // Fit on connected sensors for center position
    if (btnFitSensors) {
      btnFitSensors.addEventListener('click', () => {
        if (!this.nodesList || this.nodesList.length === 0) {
          alert('No sensor nodes are currently registered.');
          return;
        }
        let totalLat = 0;
        let totalLon = 0;
        let validCount = 0;
        this.nodesList.forEach(n => {
          const lat = parseFloat(n.latitude);
          const lon = parseFloat(n.longitude);
          if (!isNaN(lat) && !isNaN(lon)) {
            totalLat += lat;
            totalLon += lon;
            validCount++;
          }
        });
        if (validCount > 0) {
          const avgLat = (totalLat / validCount).toFixed(6);
          const avgLon = (totalLon / validCount).toFixed(6);
          const latEl = document.getElementById('cfg-rx-lat');
          const lonEl = document.getElementById('cfg-rx-lon');
          if (latEl) latEl.value = avgLat;
          if (lonEl) lonEl.value = avgLon;
        }
      });
    }

    // Form Submission -> Save directly to disk via POST /api/config/dashboard
    if (form) {
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const title = document.getElementById('cfg-rx-name').value.trim() || 'Tactical Drone Remote ID Radar';
        const lat = parseFloat(document.getElementById('cfg-rx-lat').value);
        const lon = parseFloat(document.getElementById('cfg-rx-lon').value);
        const zoom = parseInt(document.getElementById('cfg-rx-zoom').value, 10) || 13;
        const showRings = document.getElementById('cfg-rx-show-rings').checked;
        const showTrails = document.getElementById('cfg-rx-show-trails').checked;
        const showWaypoints = document.getElementById('cfg-rx-show-waypoints').checked;

        const updatedConfig = {
          title,
          center_latitude: lat,
          center_longitude: lon,
          default_zoom: zoom,
          show_range_rings: showRings,
          show_trails: showTrails,
          show_waypoints: showWaypoints,
        };

        const saveBtn = document.getElementById('btn-rx-save');
        if (saveBtn) saveBtn.textContent = '💾 Saving...';

        await this.saveDashboardConfig(updatedConfig);

        if (saveBtn) saveBtn.textContent = '💾 Save Viewport Settings';
        this.closeReceiverModal();
      });
    }
  }

  renderModalNodes() {
    const container = document.getElementById('modal-nodes-list');
    const countEl = document.getElementById('modal-nodes-count');
    if (!container) return;

    if (countEl) countEl.textContent = this.nodesList.length;

    if (this.nodesList.length === 0) {
      container.innerHTML = `
        <div style="font-size: 11px; color: var(--text-muted); padding: 12px; text-align: center;">
          No sensor nodes connected. Start <code>drone-scanner</code> service to stream telemetry.
        </div>
      `;
      return;
    }

    container.innerHTML = this.nodesList.map(node => {
      const isLocked = Boolean(node.locked);
      const status = node.status || 'ONLINE';
      const statusColor = status === 'ONLINE' ? '#10b981' : (status === 'DEGRADED' ? '#f59e0b' : '#ef4444');
      const lat = parseFloat(node.latitude);
      const lon = parseFloat(node.longitude);
      const coordsStr = (!isNaN(lat) && !isNaN(lon)) ? `${lat.toFixed(5)}°, ${lon.toFixed(5)}°` : 'No Fix';
      const altStr = node.altitude_m != null ? `${parseFloat(node.altitude_m).toFixed(1)}m` : '--';
      const pkts = (node.packets_received_total || 0).toLocaleString();

      return `
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; background: rgba(255,255,255,0.03); border: 1px solid var(--border-subtle); border-radius: 5px; padding: 6px 10px; font-size: 11px;">
          <div style="display: flex; flex-direction: column; gap: 2px;">
            <div style="display: flex; align-items: center; gap: 6px;">
              <span style="font-weight: 700; color: #38bdf8;">📡 ${node.name || node.node_id}</span>
              <span style="font-size: 9px; font-weight: 800; color: #fff; background: ${statusColor}; padding: 1px 4px; border-radius: 3px;">${status}</span>
            </div>
            <div style="color: var(--text-muted); font-size: 10px;">
              ID: <code>${node.node_id}</code> · Pos: ${coordsStr} · Alt: ${altStr} · Pkts: ${pkts}
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 6px;">
            ${isLocked ? `
              <span style="font-size: 10px; font-weight: 700; color: #ef4444; background: rgba(239, 68, 68, 0.15); border: 1px solid #ef444455; padding: 2px 6px; border-radius: 4px;" title="Position write-protected on scanner disk in scanner_config.json">
                🔒 LOCKED
              </span>
            ` : `
              <span style="font-size: 10px; font-weight: 700; color: #10b981; background: rgba(16, 185, 129, 0.15); border: 1px solid #10b98155; padding: 2px 6px; border-radius: 4px;" title="Unlocked: drag station icon on map to calibrate">
                🔓 DRAGGABLE
              </span>
            `}
          </div>
        </div>
      `;
    }).join('');
  }

  openReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    if (!modal) return;

    this.renderModalNodes();

    const cfg = this.dashboardConfig || {
      title: 'Tactical Drone Remote ID Radar & Airspace Monitor',
      center_latitude: 47.3769,
      center_longitude: 8.5417,
      default_zoom: 13,
      show_range_rings: true,
      show_trails: true,
      show_waypoints: true,
    };

    const nameEl = document.getElementById('cfg-rx-name');
    const latEl = document.getElementById('cfg-rx-lat');
    const lonEl = document.getElementById('cfg-rx-lon');
    const zoomEl = document.getElementById('cfg-rx-zoom');
    const showRingsEl = document.getElementById('cfg-rx-show-rings');
    const showTrailsEl = document.getElementById('cfg-rx-show-trails');
    const showWaypointsEl = document.getElementById('cfg-rx-show-waypoints');

    if (nameEl) nameEl.value = cfg.title || '';
    if (latEl) latEl.value = cfg.center_latitude != null ? cfg.center_latitude : 47.3769;
    if (lonEl) lonEl.value = cfg.center_longitude != null ? cfg.center_longitude : 8.5417;
    if (zoomEl) zoomEl.value = cfg.default_zoom != null ? cfg.default_zoom : 13;
    if (showRingsEl) showRingsEl.checked = cfg.show_range_rings !== false;
    if (showTrailsEl) showTrailsEl.checked = cfg.show_trails !== false;
    if (showWaypointsEl) showWaypointsEl.checked = cfg.show_waypoints !== false;

    modal.style.display = 'flex';
  }

  closeReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    if (modal) modal.style.display = 'none';
  }

  /**
   * Fetches dashboard & viewport configuration from disk via /api/config/dashboard
   */
  async fetchDashboardConfig() {
    try {
      const resp = await fetch('/api/config/dashboard');
      if (!resp.ok) return;
      const data = await resp.json();
      const cfg = data.dashboard || data;
      this.dashboardConfig = cfg;
      if (this.mapCtrl) this.mapCtrl.setDashboardConfig(cfg);

      const btnToggleRings = document.getElementById('btn-toggle-rings');
      if (btnToggleRings && cfg.show_range_rings !== undefined) {
        btnToggleRings.classList.toggle('active', Boolean(cfg.show_range_rings));
      }
      const btnTrails = document.getElementById('btn-toggle-paths');
      if (btnTrails && cfg.show_trails !== undefined) {
        btnTrails.classList.toggle('active', Boolean(cfg.show_trails));
      }
      const btnWaypoints = document.getElementById('btn-toggle-waypoints');
      if (btnWaypoints && cfg.show_waypoints !== undefined) {
        btnWaypoints.classList.toggle('active', Boolean(cfg.show_waypoints));
      }
    } catch (e) {
      console.warn('Failed to load dashboard configuration:', e);
    }
  }

  /**
   * Saves dashboard & viewport configuration to disk via POST /api/config/dashboard
   */
  async saveDashboardConfig(configData) {
    try {
      const resp = await fetch('/api/config/dashboard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configData),
      });

      if (!resp.ok) {
        const errJson = await resp.json().catch(() => ({}));
        throw new Error(errJson.detail || `Server returned HTTP ${resp.status}`);
      }

      const resJson = await resp.json();
      const savedConfig = resJson.dashboard || resJson;
      this.dashboardConfig = savedConfig;
      if (this.mapCtrl) this.mapCtrl.setDashboardConfig(savedConfig);
      return savedConfig;
    } catch (err) {
      console.error('Failed to save dashboard config to disk:', err);
      alert(`Error saving dashboard config: ${err.message}`);
    }
  }

  setMode(mode) {
    this.currentMode = mode;
    const btnLive = document.getElementById('btn-mode-live');
    const btnReplay = document.getElementById('btn-mode-replay');
    const wsBadge = document.getElementById('ws-status-badge');
    const wsText = document.getElementById('ws-status-text');

    if (mode === 'live') {
      if (btnLive) btnLive.classList.add('active');
      if (btnReplay) btnReplay.classList.remove('active');
      if (wsBadge) wsBadge.className = 'connection-badge online';
      if (wsText) wsText.textContent = 'LIVE FEED';
    } else {
      if (btnReplay) btnReplay.classList.add('active');
      if (btnLive) btnLive.classList.remove('active');
      if (wsBadge) wsBadge.className = 'connection-badge replay-mode';
      if (wsText) wsText.textContent = 'REPLAY MODE';
    }
  }

  /**
   * Fetches global airspace stats from /api/stats
   */
  async fetchStats() {
    try {
      const resp = await fetch('/api/stats');
      if (!resp.ok) return;
      const data = await resp.json();

      document.getElementById('stat-active-drones').textContent = data.active_encounters || 0;
      document.getElementById('stat-total-encounters').textContent = data.total_encounters || 0;
      document.getElementById('stat-total-packets').textContent = (data.total_packets || 0).toLocaleString();

      const tb = data.transports_breakdown || {};
      document.getElementById('stat-bt4').textContent = tb.bt4 || 0;
      document.getElementById('stat-bt5').textContent = tb.bt5 || 0;
      document.getElementById('stat-wifi').textContent = tb.wifi || 0;
      document.getElementById('stat-nan').textContent = tb.nan || 0;
    } catch (e) {
      // Ignore transient fetch errors
    }
  }

  /**
   * Fetches encounters feed from /api/encounters
   */
  async fetchEncounters() {
    try {
      const resp = await fetch('/api/encounters?limit=100');
      if (!resp.ok) return;
      const data = await resp.json();
      const encounters = data.encounters || [];
      this.feedCtrl.setEncounters(encounters);
      this.mapCtrl.setAllEncounters(encounters);
    } catch (e) {
      // Ignore transient errors
    }
  }

  /**
   * Selects an encounter for detailed telemetry inspection, full trajectory rendering, and replay
   */
  async selectEncounter(encounterId) {
    this.selectedEncounterId = encounterId;
    this.feedCtrl.setSelectedEncounter(encounterId);

    // Show Track Lock Banner
    const banner = document.getElementById('target-track-banner');
    const bannerId = document.getElementById('target-banner-id');
    const bannerOp = document.getElementById('target-banner-op');

    try {
      const resp = await fetch(`/api/encounters/${encounterId}`);
      if (!resp.ok) return;
      const encounter = await resp.json();
      this.currentEncounter = encounter;

      // Automatically switch HUD mode and connection badge based on active vs historical flight
      if (encounter.is_active) {
        this.setMode('live');
      } else {
        this.setMode('replay');
      }

      if (banner) {
        banner.style.display = 'flex';
        bannerId.textContent = encounter.serial_number || encounter.mac || encounter.encounter_id;
        bannerOp.textContent = encounter.operator_id ? `[${encounter.operator_id}]` : '';
        const bannerDate = document.getElementById('target-banner-date');
        if (bannerDate) {
          const firstSeen = encounter.first_seen_iso || encounter.first_seen || encounter.last_seen_iso || encounter.last_seen;
          const dmy = formatDmyDate(firstSeen);
          bannerDate.textContent = dmy ? `📅 ${dmy}` : '';
        }
      }

      // Update Map Controller with full encounter trajectory & waypoints
      this.mapCtrl.setSelectedEncounter(encounterId, encounter);

      // 1. Populate Telemetry Inspector
      this.inspectorCtrl.setNodesList(this.nodesList);
      this.inspectorCtrl.setEncounter(encounter);

      // 2. Load Trajectory into Timeline Scrubber
      this.scrubberCtrl.setNodesList(this.nodesList);
      this.scrubberCtrl.setEncounterNodeId(encounter.node_id || null);
      this.scrubberCtrl.setTrajectory(encounter.trajectory || []);

    } catch (err) {
      console.error('Error selecting encounter:', err);
    }
  }

  deselectEncounter() {
    this.selectedEncounterId = null;
    this.currentEncounter = null;
    this.mapCtrl.setSelectedEncounter(null);
    this.feedCtrl.setSelectedEncounter(null);
    this.inspectorCtrl.setEncounter(null);
    this.scrubberCtrl.hide();

    const banner = document.getElementById('target-track-banner');
    if (banner) banner.style.display = 'none';

    this.setMode('live');
  }

  /**
   * Real-time WebSocket connection to /ws/live
   */
  connectWebSocket() {
    const wsBadge = document.getElementById('ws-status-badge');
    const wsText = document.getElementById('ws-status-text');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/live`;

    try {
      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        this.setMode(this.currentMode);
      };

      this.ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === 'live_telemetry') {
            const activeDrones = payload.active_drones || [];
            
            // Update active count HUD
            const activeCountEl = document.getElementById('stat-active-drones');
            if (activeCountEl) activeCountEl.textContent = payload.active_count || activeDrones.length;

            // Update live markers
            activeDrones.forEach(drone => {
              this.mapCtrl.updateLiveDrone(drone);
              // If currently inspected drone received new fix in live mode, update gauges
              if (this.selectedEncounterId === drone.encounter_id && drone.latest_position) {
                const lp = drone.latest_position;
                this.inspectorCtrl.updateLiveGauges([
                  lp.lat,
                  lp.lon,
                  lp.alt_m,
                  lp.speed_mps,
                  lp.heading_deg,
                  lp.timestamp,
                  lp.height_m,
                  lp.height_type,
                  lp.pressure_alt_m,
                  lp.vert_speed_mps,
                  lp.rssi_dbm,
                ], drone.avg_rssi_dbm);
              }
            });
          }
        } catch (e) {}
      };

      this.ws.onclose = () => {
        if (wsBadge) wsBadge.className = 'connection-badge offline';
        if (wsText) wsText.textContent = 'RECONNECTING';
        // Auto-reconnect in 3s
        setTimeout(() => this.connectWebSocket(), 3000);
      };

      this.ws.onerror = () => {
        if (wsBadge) wsBadge.className = 'connection-badge offline';
        if (wsText) wsText.textContent = 'WS ERROR';
      };

    } catch (e) {
      setTimeout(() => this.connectWebSocket(), 3000);
    }
  }
}

// Instantiate application on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  window.app = new TacticalApp();
});

