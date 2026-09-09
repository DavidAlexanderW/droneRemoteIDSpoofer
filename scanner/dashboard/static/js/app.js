/**
 * Tactical Drone Remote ID Airspace Monitor - Main Application Orchestrator
 * Integrates Map, Feed, Telemetry Inspector, Timeline Scrubber, and WebSocket Live Feed.
 */

import { TacticalMapController } from './map.js';
import { EncountersFeedController } from './feed.js';
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
    this.receiverConfig = null;

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
      this.modalCtrl.open(encId);
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
      (updatedConfig) => {
        this.saveReceiverConfig(updatedConfig);
      },
      () => {
        this.openReceiverModal();
      }
    );

    // 2. Setup HUD Controls & Mode Switcher
    this.initHudControls();

    // 3. Setup Quick Map Controls
    this.initMapControls();

    // 4. Setup Receiver Configuration Modal
    this.initReceiverModal();

    // 5. Load Receiver Configuration from Disk
    await this.fetchReceiverConfig();

    // 6. Start Live Services
    this.fetchStats();
    this.fetchEncounters();
    this.connectWebSocket();

    this.statsInterval = setInterval(() => this.fetchStats(), 4000);
    this.feedInterval = setInterval(() => this.fetchEncounters(), 3000);
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
        this.mapCtrl.centerOnReceiver();
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
   * Initializes Receiver Configuration Modal & Disk Persistence Handlers
   */
  initReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    const form = document.getElementById('form-receiver-config');
    const btnClose = document.getElementById('btn-close-receiver-modal');
    const btnCancel = document.getElementById('btn-rx-cancel');
    const btnGps = document.getElementById('btn-rx-gps-detect');
    const btnMapPick = document.getElementById('btn-rx-map-pick');

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

    // Browser GPS Auto-Detect
    if (btnGps) {
      btnGps.addEventListener('click', () => {
        if ('geolocation' in navigator) {
          btnGps.textContent = '⏳ Acquiring GPS...';
          navigator.geolocation.getCurrentPosition(
            (pos) => {
              document.getElementById('cfg-rx-lat').value = pos.coords.latitude.toFixed(6);
              document.getElementById('cfg-rx-lon').value = pos.coords.longitude.toFixed(6);
              if (pos.coords.altitude != null) {
                document.getElementById('cfg-rx-alt').value = pos.coords.altitude.toFixed(1);
              }
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

    // Interactive Map Pick Mode
    if (btnMapPick) {
      btnMapPick.addEventListener('click', () => {
        this.closeReceiverModal();
        this.mapCtrl.enableMapPickMode();
      });
    }

    // Form Submission -> Save directly to disk via POST /api/config/receiver
    if (form) {
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const name = document.getElementById('cfg-rx-name').value.trim() || 'Tactical Sensor Station';
        const lat = parseFloat(document.getElementById('cfg-rx-lat').value);
        const lon = parseFloat(document.getElementById('cfg-rx-lon').value);
        const alt = parseFloat(document.getElementById('cfg-rx-alt').value) || 0.0;
        
        const ringsRaw = document.getElementById('cfg-rx-rings').value;
        const rings = ringsRaw
          .split(',')
          .map(r => parseFloat(r.trim()))
          .filter(r => !isNaN(r) && r > 0);

        const showRings = document.getElementById('cfg-rx-show-rings').checked;
        const enabled = document.getElementById('cfg-rx-enabled').checked;

        const updatedConfig = {
          name,
          latitude: lat,
          longitude: lon,
          altitude_m: alt,
          range_rings_m: rings.length > 0 ? rings : [500, 1000, 2500, 5000],
          show_range_rings: showRings,
          enabled,
        };

        const saveBtn = document.getElementById('btn-rx-save');
        if (saveBtn) saveBtn.textContent = '💾 Saving...';

        await this.saveReceiverConfig(updatedConfig);

        if (saveBtn) saveBtn.textContent = '💾 Save to Disk';
        this.closeReceiverModal();
      });
    }
  }

  openReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    if (!modal) return;

    const cfg = this.receiverConfig || {
      name: 'Tactical Receiver Station',
      latitude: 47.3769,
      longitude: 8.5417,
      altitude_m: 450.0,
      range_rings_m: [500, 1000, 2500, 5000],
      show_range_rings: true,
      enabled: true,
      locked: false,
    };

    const isLocked = Boolean(cfg.locked);

    const nameEl = document.getElementById('cfg-rx-name');
    const latEl = document.getElementById('cfg-rx-lat');
    const lonEl = document.getElementById('cfg-rx-lon');
    const altEl = document.getElementById('cfg-rx-alt');
    const ringsEl = document.getElementById('cfg-rx-rings');
    const showRingsEl = document.getElementById('cfg-rx-show-rings');
    const enabledEl = document.getElementById('cfg-rx-enabled');
    const lockBadgeEl = document.getElementById('modal-rx-lock-badge');
    const lockBannerEl = document.getElementById('modal-rx-locked-banner');
    const btnGps = document.getElementById('btn-rx-gps-detect');
    const btnMapPick = document.getElementById('btn-rx-map-pick');
    const btnSave = document.getElementById('btn-rx-save');

    if (nameEl) nameEl.value = cfg.name || '';
    if (latEl) latEl.value = cfg.latitude != null ? cfg.latitude : '';
    if (lonEl) lonEl.value = cfg.longitude != null ? cfg.longitude : '';
    if (altEl) altEl.value = cfg.altitude_m != null ? cfg.altitude_m : '';
    if (ringsEl) ringsEl.value = (cfg.range_rings_m || [500, 1000, 2500, 5000]).join(', ');
    if (showRingsEl) showRingsEl.checked = cfg.show_range_rings !== false;
    if (enabledEl) enabledEl.checked = cfg.enabled !== false;

    // Lock Status Display & Input Write-Protection
    if (lockBadgeEl) {
      if (isLocked) {
        lockBadgeEl.textContent = 'LOCKED ON DISK';
        lockBadgeEl.style.background = 'rgba(239, 68, 68, 0.2)';
        lockBadgeEl.style.color = '#f87171';
        lockBadgeEl.style.borderColor = '#ef4444';
      } else {
        lockBadgeEl.textContent = 'UNLOCKED';
        lockBadgeEl.style.background = 'rgba(16, 185, 129, 0.2)';
        lockBadgeEl.style.color = '#34d399';
        lockBadgeEl.style.borderColor = '#10b981';
      }
    }

    if (lockBannerEl) {
      lockBannerEl.style.display = isLocked ? 'flex' : 'none';
    }

    // Disable editing controls if locked via configuration file on disk
    [nameEl, latEl, lonEl, altEl, ringsEl, showRingsEl, enabledEl].forEach(el => {
      if (el) el.disabled = isLocked;
    });

    if (btnGps) btnGps.disabled = isLocked;
    if (btnMapPick) btnMapPick.disabled = isLocked;
    if (btnSave) {
      btnSave.disabled = isLocked;
      btnSave.textContent = isLocked ? '🔒 Locked on Disk' : '💾 Save to Disk';
      btnSave.title = isLocked ? 'Receiver parameters are locked via receiver_config.json on disk' : 'Save parameters to receiver_config.json on disk';
    }

    modal.style.display = 'flex';
  }

  closeReceiverModal() {
    const modal = document.getElementById('receiver-config-modal');
    if (modal) modal.style.display = 'none';
  }

  /**
   * Fetches receiver configuration from disk via /api/config/receiver
   */
  async fetchReceiverConfig() {
    try {
      const resp = await fetch('/api/config/receiver');
      if (!resp.ok) return;
      const data = await resp.json();
      this.receiverConfig = data;
      this.mapCtrl.setReceiverConfig(data);
      this.inspectorCtrl.setReceiverConfig(data);
      this.scrubberCtrl.setReceiverConfig(data);
      this.modalCtrl.setReceiverConfig(data);
    } catch (e) {
      console.warn('Failed to load receiver configuration:', e);
    }
  }

  /**
   * Saves receiver configuration to disk via POST /api/config/receiver
   */
  async saveReceiverConfig(configData) {
    try {
      const resp = await fetch('/api/config/receiver', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configData),
      });

      if (!resp.ok) {
        throw new Error(`Server returned HTTP ${resp.status}`);
      }

      const resJson = await resp.json();
      const savedConfig = resJson.receiver || resJson;
      this.receiverConfig = savedConfig;
      this.mapCtrl.setReceiverConfig(savedConfig);
      this.inspectorCtrl.setReceiverConfig(savedConfig);
      this.scrubberCtrl.setReceiverConfig(savedConfig);
      this.modalCtrl.setReceiverConfig(savedConfig);
      return savedConfig;
    } catch (err) {
      console.error('Failed to save receiver config to disk:', err);
      alert(`Error saving receiver config: ${err.message}`);
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
      }

      // Update Map Controller with full encounter trajectory & waypoints
      this.mapCtrl.setSelectedEncounter(encounterId, encounter);

      // 1. Populate Telemetry Inspector
      this.inspectorCtrl.setReceiverConfig(this.receiverConfig);
      this.inspectorCtrl.setEncounter(encounter);

      // 2. Load Trajectory into Timeline Scrubber
      this.scrubberCtrl.setReceiverConfig(this.receiverConfig);
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
                this.inspectorCtrl.updateLiveGauges([
                  drone.latest_position.lat,
                  drone.latest_position.lon,
                  drone.latest_position.alt_m,
                  drone.latest_position.speed_mps,
                  drone.latest_position.heading_deg,
                  drone.latest_position.timestamp,
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

