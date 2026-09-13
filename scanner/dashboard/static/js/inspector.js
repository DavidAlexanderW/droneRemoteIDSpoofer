import { calculateHaversineDistanceM, calculateBearingDeg, getBearingCompass } from './map.js';

/**
 * Tactical Drone Remote ID Airspace Monitor - Telemetry Inspector Controller
 * Updates right drawer gauges, ASD-STAN conformance checks, interactive message block dissection,
 * and export triggers.
 */

export class TelemetryInspectorController {
  constructor(onOpenPacketModal) {
    this.onOpenPacketModal = onOpenPacketModal;
    this.currentEncounter = null;
    this.receiverConfig = null;
    this.emptyView = document.querySelector('.inspector-empty-state');
    this.detailsView = document.getElementById('target-telemetry-view');
    this.statusBadge = document.getElementById('inspector-status-badge');

    this.initActionButtons();
    this.initComplianceDrawers();
    this.initCopyHandlers();
  }

  setReceiverConfig(config) {
    this.receiverConfig = config;
    if (this.currentEncounter) {
      const traj = this.currentEncounter.trajectory || [];
      const latestPt = traj.length > 0 ? traj[traj.length - 1] : null;
      this.updateLiveGauges(latestPt, this.currentEncounter.avg_rssi_dbm);
    }
  }

  initCopyHandlers() {
    // 1. Mini copy buttons
    document.querySelectorAll('.btn-copy-mini').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const targetId = btn.dataset.target;
        if (!targetId) return;
        const targetEl = document.getElementById(targetId);
        if (!targetEl) return;
        const val = targetEl.textContent.trim();
        if (val && val !== '--' && val !== 'N/A' && val !== 'NOT BROADCAST') {
          this.copyToClipboard(val, btn);
        }
      });
    });

    // 2. Click or double click to copy directly on identifier values
    document.querySelectorAll('.id-val.selectable-text').forEach(el => {
      el.addEventListener('click', () => {
        const text = el.textContent.trim();
        if (text && text !== '--' && text !== 'N/A' && text !== 'NOT BROADCAST') {
          // If the user isn't actively selecting a subset of text
          const sel = window.getSelection().toString();
          if (!sel) {
            const btn = el.parentElement ? el.parentElement.querySelector('.btn-copy-mini') : null;
            this.copyToClipboard(text, btn);
          }
        }
      });
    });
  }

  async copyToClipboard(text, triggerBtn = null) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for non-secure contexts
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }

      // Visual button feedback
      if (triggerBtn) {
        triggerBtn.classList.add('copied');
        const originalHtml = triggerBtn.innerHTML;
        triggerBtn.innerHTML = '✔';
        setTimeout(() => {
          triggerBtn.classList.remove('copied');
          triggerBtn.innerHTML = originalHtml;
        }, 1500);
      }

      this.showToast(`COPIED TO CLIPBOARD: ${text}`);
    } catch (err) {
      console.error('Failed to copy text:', err);
    }
  }

  showToast(message) {
    // Remove any existing toast
    const existing = document.querySelector('.copy-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'copy-toast';
    toast.innerHTML = `<span>📋</span> <span>${message}</span>`;
    document.body.appendChild(toast);

    setTimeout(() => {
      if (toast && toast.parentElement) {
        toast.remove();
      }
    }, 2200);
  }

  initActionButtons() {
    const btnPackets = document.getElementById('btn-open-packet-inspector');
    if (btnPackets) {
      btnPackets.addEventListener('click', () => {
        if (this.currentEncounter && this.onOpenPacketModal) {
          this.onOpenPacketModal(this.currentEncounter.encounter_id);
        }
      });
    }

    const btnGeoJson = document.getElementById('btn-export-geojson');
    if (btnGeoJson) {
      btnGeoJson.addEventListener('click', () => {
        if (this.currentEncounter) {
          window.location.href = `/api/export/${this.currentEncounter.encounter_id}/geojson`;
        }
      });
    }

    const btnCsv = document.getElementById('btn-export-csv');
    if (btnCsv) {
      btnCsv.addEventListener('click', () => {
        if (this.currentEncounter) {
          window.location.href = `/api/export/${this.currentEncounter.encounter_id}/csv`;
        }
      });
    }

    const btnFaa = document.getElementById('btn-query-faa');
    if (btnFaa) {
      btnFaa.addEventListener('click', () => {
        this.queryFaaDoc();
      });
    }
  }

  async queryFaaDoc() {
    if (!this.currentEncounter) return;
    const serial = this.currentEncounter.serial_number;
    const btnFaa = document.getElementById('btn-query-faa');
    const resultBox = document.getElementById('insp-faa-result');
    const statusPill = document.getElementById('insp-faa-status-pill');
    const bodyEl = document.getElementById('insp-faa-body');

    if (!serial || serial === 'N/A' || serial === '--' || serial === 'NOT BROADCAST') {
      this.showToast('No Serial Number available to query FAA DOC Registry');
      return;
    }

    if (btnFaa) {
      btnFaa.disabled = true;
      btnFaa.classList.add('loading');
      btnFaa.innerHTML = `<span class="faa-spinner"></span> <span>Querying...</span>`;
    }

    if (resultBox) {
      resultBox.style.display = 'block';
    }
    if (statusPill) {
      statusPill.className = 'comp-status-pill optional';
      statusPill.textContent = 'QUERYING...';
    }
    if (bodyEl) {
      bodyEl.innerHTML = `
        <div style="font-size: 11px; color: var(--text-secondary); padding: 4px 0; display: flex; align-items: center; gap: 6px;">
          <span class="faa-spinner-small"></span> Querying FAA Declaration of Compliance Registry (uasdoc.faa.gov)...
        </div>
      `;
    }

    try {
      const resp = await fetch(`/api/faa_lookup?serial=${encodeURIComponent(serial)}`);
      const data = await resp.json();

      if (data.found) {
        if (statusPill) {
          statusPill.className = 'comp-status-pill compliant';
          statusPill.textContent = data.doc_status || 'ACCEPTED';
        }
        if (data.make || data.model) {
          const full = [data.make, data.model].filter(Boolean).join(' ');
          const modelEl = document.getElementById('insp-drone-model');
          if (modelEl) {
            modelEl.textContent = full;
            modelEl.style.color = 'var(--accent-green)';
          }
          if (this.currentEncounter) {
            this.currentEncounter.drone_make = data.make;
            this.currentEncounter.drone_model = data.model;
          }
        }
        if (bodyEl) {
          bodyEl.innerHTML = `
            <div class="faa-grid font-mono">
              <div class="faa-field">
                <span class="faa-key">MANUFACTURER / MAKE:</span>
                <span class="faa-val highlight-green">${data.make || 'N/A'}</span>
              </div>
              <div class="faa-field">
                <span class="faa-key">MODEL:</span>
                <span class="faa-val highlight-cyan">${data.model || 'N/A'}</span>
              </div>
              ${data.series ? `
              <div class="faa-field">
                <span class="faa-key">SERIES:</span>
                <span class="faa-val">${data.series}</span>
              </div>` : ''}
              <div class="faa-field">
                <span class="faa-key">DOC TRACKING #:</span>
                <span class="faa-val highlight-amber">${data.tracking_number || 'N/A'}</span>
              </div>
              ${data.applicant ? `
              <div class="faa-field">
                <span class="faa-key">APPLICANT / SUBMITTER:</span>
                <span class="faa-val" style="font-size: 10px; color: var(--text-secondary);">${data.applicant}</span>
              </div>` : ''}
              ${data.category ? `
              <div class="faa-field">
                <span class="faa-key">DOC CATEGORY:</span>
                <span class="faa-val" style="font-size: 10px;">${data.category}</span>
              </div>` : ''}
            </div>
          `;
        }
      } else {
        if (statusPill) {
          statusPill.className = 'comp-status-pill missing';
          statusPill.textContent = 'NOT FOUND';
        }
        if (bodyEl) {
          bodyEl.innerHTML = `
            <div style="font-size: 11px; color: var(--text-muted); line-height: 1.4;">
              <span style="color: #f59e0b;">No matching Declaration of Compliance</span> found in official FAA database for serial <code style="color: var(--accent-cyan); font-weight: bold;">${serial}</code>.
              <br/><small style="color: var(--text-secondary);">(Foreign, legacy, custom, or unlisted aircraft/broadcast module).</small>
            </div>
          `;
        }
      }
    } catch (err) {
      if (statusPill) {
        statusPill.className = 'comp-status-pill missing';
        statusPill.textContent = 'FAILED';
      }
      if (bodyEl) {
        bodyEl.innerHTML = `
          <div style="font-size: 11px; color: #f87171;">
            Failed to query FAA Registry: ${err.message || 'Network error'}
          </div>
        `;
      }
    } finally {
      if (btnFaa) {
        btnFaa.disabled = false;
        btnFaa.classList.remove('loading');
        btnFaa.innerHTML = `<span class="faa-btn-icon">🦅</span> <span class="faa-btn-text">Query FAA DOC</span>`;
      }
    }
  }

  initComplianceDrawers() {
    const items = document.querySelectorAll('.compliance-item.clickable');
    items.forEach(item => {
      item.addEventListener('click', (e) => {
        // Toggle active drawer
        const drawer = item.querySelector('.comp-drawer');
        const chevron = item.querySelector('.comp-chevron');
        if (!drawer) return;

        const isVisible = drawer.style.display === 'block';
        drawer.style.display = isVisible ? 'none' : 'block';
        if (chevron) {
          chevron.textContent = isVisible ? '▾' : '▴';
        }
        item.classList.toggle('drawer-open', !isVisible);
      });
    });
  }

  setEncounter(encounter) {
    this.currentEncounter = encounter;
    if (!encounter) {
      if (this.emptyView) this.emptyView.style.display = 'flex';
      if (this.detailsView) this.detailsView.style.display = 'none';
      if (this.statusBadge) {
        this.statusBadge.textContent = 'IDLE';
        this.statusBadge.className = 'badge-tag';
      }
      return;
    }

    if (this.emptyView) this.emptyView.style.display = 'none';
    if (this.detailsView) this.detailsView.style.display = 'block';

    if (this.statusBadge) {
      this.statusBadge.textContent = encounter.is_active ? 'LIVE TARGET' : 'CLOSED FLIGHT';
      this.statusBadge.className = `badge-tag ${encounter.is_active ? 'active' : ''}`;
    }

    // Identity
    document.getElementById('insp-serial').textContent = encounter.serial_number || 'N/A';
    
    // Make & Model
    const modelEl = document.getElementById('insp-drone-model');
    if (modelEl) {
      if (encounter.drone_make || encounter.drone_model) {
        const make = encounter.drone_make || '';
        const model = encounter.drone_model || '';
        const full = [make, model].filter(Boolean).join(' ');
        modelEl.textContent = full || 'UNKNOWN';
        modelEl.style.color = 'var(--accent-amber)';
      } else {
        modelEl.textContent = 'NOT IDENTIFIED';
        modelEl.style.color = 'var(--text-muted)';
      }
    }

    document.getElementById('insp-mac').textContent = encounter.mac || 'N/A';
    document.getElementById('insp-operator').textContent = encounter.operator_id || 'NOT BROADCAST';
    document.getElementById('insp-selfid').textContent = encounter.self_id_desc || 'N/A';

    // RF Transports & Channels
    const transportsStr = (encounter.transports || []).map(t => t.toUpperCase()).join(', ');
    const channelsStr = (encounter.channels || []).join(', ');
    const rfEl = document.getElementById('insp-rf-channels');
    if (rfEl) {
      rfEl.textContent = `${transportsStr || 'N/A'} (CH: ${channelsStr || 'N/A'})`;
    }

    // Wi-Fi / PHY Rates & Modulation
    const phyRow = document.getElementById('insp-phy-rates-row');
    const phyEl = document.getElementById('insp-phy-rates');
    if (phyRow && phyEl) {
      if (encounter.wifi_rates && encounter.wifi_rates.length > 0) {
        phyEl.textContent = encounter.wifi_rates.join(', ');
        phyRow.style.display = 'flex';
      } else {
        phyRow.style.display = 'none';
      }
    }

    // Gauges max values
    document.getElementById('insp-max-alt').textContent = encounter.max_alt_m != null ? Math.round(encounter.max_alt_m) : '--';
    document.getElementById('insp-max-speed').textContent = encounter.max_speed_mps != null ? Math.round(encounter.max_speed_mps) : '--';
    document.getElementById('insp-avg-rssi').textContent = encounter.avg_rssi_dbm != null ? Math.round(encounter.avg_rssi_dbm) : '--';
    document.getElementById('insp-pilot-alt').textContent = encounter.pilot_alt_m != null ? Math.round(encounter.pilot_alt_m) : '--';

    // Check latest point for current gauge readings
    const traj = encounter.trajectory || [];
    const latestPt = traj.length > 0 ? traj[traj.length - 1] : null;
    this.updateLiveGauges(latestPt, encounter.avg_rssi_dbm);

    // Conformance checklist & detailed drawers
    this.updateComplianceChecklist(encounter);

    // Pilot coordinates
    if (encounter.pilot_lat != null && encounter.pilot_lon != null) {
      document.getElementById('insp-pilot-lat').textContent = encounter.pilot_lat.toFixed(6);
      document.getElementById('insp-pilot-lon').textContent = encounter.pilot_lon.toFixed(6);
    } else {
      document.getElementById('insp-pilot-lat').textContent = 'N/A';
      document.getElementById('insp-pilot-lon').textContent = 'N/A';
    }

    // Reset FAA DOC Registry section
    const faaBox = document.getElementById('insp-faa-result');
    if (faaBox) faaBox.style.display = 'none';
    const btnFaa = document.getElementById('btn-query-faa');
    if (btnFaa) {
      const hasSerial = Boolean(encounter.serial_number && encounter.serial_number !== 'N/A' && encounter.serial_number !== '--');
      btnFaa.disabled = !hasSerial;
      btnFaa.title = hasSerial ? 'Query official FAA Declaration of Compliance Registry' : 'No Serial Number detected for this drone';
      btnFaa.classList.toggle('disabled', !hasSerial);
    }
  }

  updateLiveGauges(pt, defaultRssi = null) {
    if (!pt) {
      const enc = this.currentEncounter;
      const alt = (enc && enc.max_alt_m != null) ? Math.round(enc.max_alt_m) : '--';
      const speed = (enc && enc.max_speed_mps != null && enc.max_speed_mps > 0) ? enc.max_speed_mps.toFixed(1) : '--';
      let heightStr = '--';
      if (enc && enc.max_height_m != null) {
        const h = Math.round(enc.max_height_m);
        heightStr = h >= 0 ? `+${h} ATO` : `${h} ATO`;
      }
      document.getElementById('insp-alt').textContent = alt;
      document.getElementById('insp-height').textContent = heightStr;
      document.getElementById('insp-speed').textContent = speed;
      document.getElementById('insp-heading').textContent = '--';
      document.getElementById('insp-heading-compass').textContent = '--';
      document.getElementById('insp-rssi').textContent = defaultRssi != null ? Math.round(defaultRssi) : '--';
      
      const rxRangeEl = document.getElementById('insp-rx-range');
      const rxUnitEl = document.getElementById('insp-rx-unit');
      const rxBearingEl = document.getElementById('insp-rx-bearing');
      if (rxRangeEl) rxRangeEl.textContent = '--';
      if (rxUnitEl) rxUnitEl.textContent = 'm';
      if (rxBearingEl) rxBearingEl.textContent = 'Bearing: --';
      return;
    }

    // pt: [lat, lon, alt, speed, heading, ts]
    const alt = pt[2] != null ? Math.round(pt[2]) : '--';
    const speed = pt[3] != null ? pt[3].toFixed(1) : '--';
    const heading = pt[4] != null ? Math.round(pt[4]) : '--';
    const compass = pt[4] != null ? this.getCompassDirection(pt[4]) : '--';

    // Calculate Height Above Takeoff / Pilot (ATO / AGL)
    let heightStr = '--';
    const repHeight = (pt.length > 6 && pt[6] != null) ? pt[6] : null;
    const heightType = (pt.length > 7 && pt[7] != null) ? pt[7] : 0;
    const typeTag = heightType === 1 ? 'AGL' : 'ATO';

    if (repHeight != null) {
      const h = Math.round(repHeight);
      heightStr = h >= 0 ? `+${h} ${typeTag}` : `${h} ${typeTag}`;
    } else if (pt[2] != null && this.currentEncounter && this.currentEncounter.pilot_alt_m != null) {
      const h = Math.round(pt[2] - this.currentEncounter.pilot_alt_m);
      heightStr = h >= 0 ? `+${h} ATO` : `${h} ATO`;
    } else if (pt[2] != null) {
      heightStr = Math.round(pt[2]);
    }

    document.getElementById('insp-alt').textContent = alt;
    document.getElementById('insp-height').textContent = heightStr;
    document.getElementById('insp-speed').textContent = speed;
    document.getElementById('insp-heading').textContent = heading;
    document.getElementById('insp-heading-compass').textContent = compass;
    document.getElementById('insp-rssi').textContent = defaultRssi != null ? Math.round(defaultRssi) : '--';

    // Calculate Slant Range and Bearing to Receiver
    let rxRangeStr = '--';
    let rxUnitStr = 'm';
    let rxBearingStr = 'Bearing: --';

    if (this.receiverConfig && this.receiverConfig.enabled && this.receiverConfig.latitude != null && pt && pt[0] != null && pt[1] != null) {
      const rxLat = this.receiverConfig.latitude;
      const rxLon = this.receiverConfig.longitude;
      const rxAlt = this.receiverConfig.altitude_m || 0;
      const droneAlt = pt[2] || 0;

      const groundDist = calculateHaversineDistanceM(rxLat, rxLon, pt[0], pt[1]);
      const deltaAlt = droneAlt - rxAlt;
      const slantRange = Math.sqrt(groundDist * groundDist + deltaAlt * deltaAlt);
      const bearing = calculateBearingDeg(rxLat, rxLon, pt[0], pt[1]);
      const compassDir = getBearingCompass(bearing);

      if (slantRange >= 1000) {
        rxRangeStr = (slantRange / 1000).toFixed(2);
        rxUnitStr = 'km';
      } else {
        rxRangeStr = Math.round(slantRange);
        rxUnitStr = 'm';
      }
      rxBearingStr = `Bearing: ${Math.round(bearing)}° (${compassDir})`;
    }

    const rxRangeEl = document.getElementById('insp-rx-range');
    const rxUnitEl = document.getElementById('insp-rx-unit');
    const rxBearingEl = document.getElementById('insp-rx-bearing');
    if (rxRangeEl) rxRangeEl.textContent = rxRangeStr;
    if (rxUnitEl) rxUnitEl.textContent = rxUnitStr;
    if (rxBearingEl) rxBearingEl.textContent = rxBearingStr;
  }

  getCompassDirection(deg) {
    const directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
    const index = Math.round(((deg %= 360) < 0 ? deg + 360 : deg) / 45) % 8;
    return directions[index];
  }

  updateComplianceChecklist(encounter) {
    const conf = encounter.conformance_blocks || {};
    const traj = encounter.trajectory || [];
    const latestPt = traj.length > 0 ? traj[traj.length - 1] : null;

    // Helper to resolve block status strictly from empirical data
    const getStatus = (key, hasData) => {
      if (conf[key]) return conf[key];
      if (hasData) return 'passed';
      return 'failed';
    };

    const basicStatus = getStatus('basic_id', Boolean(encounter.serial_number));
    const locStatus = getStatus('location', Boolean(traj.length > 0));
    const sysStatus = getStatus('system', Boolean(encounter.pilot_lat != null && encounter.pilot_lon != null));
    const opStatus = getStatus('operator', Boolean(encounter.operator_id));
    const selfStatus = getStatus('self_id', Boolean(encounter.self_id_desc));
    const authStatus = getStatus('auth', false);

    // 1. Basic ID [Msg 0x0] (Mandatory under ASD-STAN prEN 4709-002)
    const compBasic = document.getElementById('comp-basic-id');
    const descBasic = document.getElementById('comp-basic-id-desc');
    if (compBasic) {
      if (basicStatus === 'passed') {
        compBasic.className = 'compliance-item clickable passed';
        compBasic.querySelector('.comp-icon').textContent = '✔';
        if (descBasic) descBasic.textContent = encounter.serial_number || 'ANSI/CTA-2063-A';
      } else if (basicStatus === 'zeroed') {
        compBasic.className = 'compliance-item clickable zeroed';
        compBasic.querySelector('.comp-icon').textContent = '⊘';
        if (descBasic) descBasic.innerHTML = '<span style="color:#fbbf24; font-style:italic;">⊘ Zeroed (Non-Compliant)</span>';
      } else {
        compBasic.className = 'compliance-item clickable failed';
        compBasic.querySelector('.comp-icon').textContent = '✖';
        if (descBasic) descBasic.textContent = 'Not Broadcast (Missing)';
      }
      
      const drawer = document.getElementById('drawer-comp-basic-id');
      if (drawer) {
        const valStatus = basicStatus === 'passed' ? 'Active / Populated' : (basicStatus === 'zeroed' ? '⊘ Non-Compliant (0x00 placeholder)' : 'Not Broadcast (Non-Compliant)');
        drawer.innerHTML = `
          <div class="comp-field-row"><span class="k">UAS ID / Serial:</span> <span class="v font-mono ${basicStatus === 'passed' ? 'highlight-cyan' : ''}">${encounter.serial_number || (basicStatus === 'zeroed' ? '⊘ Zeroed (0x00)' : 'None')}</span></div>
          <div class="comp-field-row"><span class="k">ID Type:</span> <span class="v">Serial Number (ANSI/CTA-2063-A)</span></div>
          <div class="comp-field-row"><span class="k">UA Type:</span> <span class="v">Helicopter / Multirotor</span></div>
          <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono ${basicStatus === 'passed' ? 'highlight-green' : (basicStatus === 'zeroed' ? 'highlight-amber' : '')}">${valStatus}</span></div>
          <div class="comp-field-row"><span class="k">Protocol:</span> <span class="v">ASTM F3411-22 / ASD-STAN</span></div>
        `;
      }
    }

    // 2. Location / Vector [Msg 0x1] (Mandatory)
    const compLoc = document.getElementById('comp-location');
    const descLoc = document.getElementById('comp-location-desc');
    if (compLoc) {
      if (locStatus === 'passed') {
        compLoc.className = 'compliance-item clickable passed';
        compLoc.querySelector('.comp-icon').textContent = '✔';
        if (descLoc) {
          if (latestPt) {
            descLoc.textContent = `${latestPt[0].toFixed(4)}°, ${latestPt[1].toFixed(4)}°`;
          } else {
            const telemetryParts = [];
            if (encounter.max_height_m != null) telemetryParts.push(`H:${Math.round(encounter.max_height_m)}m`);
            if (encounter.max_speed_mps != null && encounter.max_speed_mps > 0) telemetryParts.push(`Spd:${Math.round(encounter.max_speed_mps)}m/s`);
            descLoc.textContent = telemetryParts.length > 0 
              ? `${telemetryParts.join(' · ')} (No GNSS Fix)` 
              : 'Transmitted (No 2D Fix)';
          }
        }
      } else if (locStatus === 'zeroed') {
        compLoc.className = 'compliance-item clickable zeroed';
        compLoc.querySelector('.comp-icon').textContent = '⊘';
        const hTag = encounter.max_height_m != null ? `H:${Math.round(encounter.max_height_m)}m` : 'Airborne';
        if (descLoc) descLoc.innerHTML = `<span style="color:#fbbf24; font-style:italic;">⊘ Zeroed Coords (${hTag} • Non-Compliant)</span>`;
      } else {
        compLoc.className = 'compliance-item clickable failed';
        compLoc.querySelector('.comp-icon').textContent = '✖';
        if (descLoc) descLoc.textContent = 'Not Broadcast (Missing)';
      }

      const drawer = document.getElementById('drawer-comp-location');
      if (drawer) {
        const lat = latestPt ? latestPt[0].toFixed(6) : (locStatus === 'zeroed' ? '⊘ Zeroed (No 3D Lock)' : 'N/A');
        const lon = latestPt ? latestPt[1].toFixed(6) : (locStatus === 'zeroed' ? '⊘ Zeroed (No 3D Lock)' : 'N/A');
        const alt = latestPt && latestPt[2] != null ? `${latestPt[2]} m` : (encounter.max_alt_m != null ? `${encounter.max_alt_m} m` : 'N/A');
        const spd = latestPt && latestPt[3] != null ? `${latestPt[3]} m/s` : (encounter.max_speed_mps != null ? `${encounter.max_speed_mps} m/s` : 'N/A');
        const hdg = latestPt && latestPt[4] != null ? `${latestPt[4]}°` : 'N/A';
        const h_rep = (latestPt && latestPt.length > 6 && latestPt[6] != null) ? `${latestPt[6]} m (${latestPt[7] === 1 ? 'AGL' : 'Above Takeoff'})` : (encounter.max_height_m != null ? `${encounter.max_height_m} m` : 'N/A');
        const p_alt = (latestPt && latestPt.length > 8 && latestPt[8] != null) ? `${latestPt[8]} m` : 'N/A';
        const v_spd = (latestPt && latestPt.length > 9 && latestPt[9] != null) ? `${latestPt[9] >= 0 ? '+' : ''}${latestPt[9]} m/s` : 'N/A';
        const valStatus = locStatus === 'passed' ? 'Active 3D GNSS Fix' : (locStatus === 'zeroed' ? '⊘ Non-Compliant (No 3D GNSS / Zeroed Coordinates)' : 'Not Broadcast (Non-Compliant)');

        drawer.innerHTML = `
          <div class="comp-field-row"><span class="k">Latitude / Longitude:</span> <span class="v font-mono ${locStatus === 'passed' ? 'highlight-cyan' : ''}">${lat}${latestPt ? '°, ' + lon + '°' : ''}</span></div>
          <div class="comp-field-row"><span class="k">Geodetic Altitude (MSL):</span> <span class="v font-mono">${alt}</span></div>
          <div class="comp-field-row"><span class="k">Pressure Altitude (Baro):</span> <span class="v font-mono">${p_alt}</span></div>
          <div class="comp-field-row"><span class="k">Reported Relative Height:</span> <span class="v font-mono">${h_rep}</span></div>
          <div class="comp-field-row"><span class="k">Speed / Track:</span> <span class="v font-mono">${spd} @ ${hdg}</span></div>
          <div class="comp-field-row"><span class="k">Vertical Speed:</span> <span class="v font-mono">${v_spd}</span></div>
          <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono ${locStatus === 'passed' ? 'highlight-green' : (locStatus === 'zeroed' ? 'highlight-amber' : '')}">${valStatus}</span></div>
        `;
      }
    }

    // 3. System / GCS [Msg 0x4] (Mandatory under ASD-STAN)
    const compSys = document.getElementById('comp-system');
    const descSys = document.getElementById('comp-system-desc');
    if (compSys) {
      if (sysStatus === 'passed') {
        compSys.className = 'compliance-item clickable passed';
        compSys.querySelector('.comp-icon').textContent = '✔';
        if (descSys) {
          if (encounter.pilot_lat != null && encounter.pilot_lon != null) {
            descSys.textContent = `Pilot: ${encounter.pilot_lat.toFixed(4)}°, ${encounter.pilot_lon.toFixed(4)}°`;
          } else if (encounter.area_ceil_m != null || encounter.area_floor_m != null) {
            descSys.textContent = 'Area Limits Defined (No Pilot Coords)';
          } else {
            descSys.textContent = 'System Data Populated';
          }
        }
      } else if (sysStatus === 'zeroed') {
        compSys.className = 'compliance-item clickable zeroed';
        compSys.querySelector('.comp-icon').textContent = '⊘';
        if (descSys) descSys.innerHTML = '<span style="color:#fbbf24; font-style:italic;">⊘ Zeroed Pilot (Non-Compliant)</span>';
      } else {
        compSys.className = 'compliance-item clickable failed';
        compSys.querySelector('.comp-icon').textContent = '✖';
        if (descSys) descSys.textContent = 'Not Broadcast (Missing)';
      }

      const drawer = document.getElementById('drawer-comp-system');
      if (drawer) {
        const ceilFloor = (encounter.area_ceil_m != null || encounter.area_floor_m != null)
          ? `Ceil: ${encounter.area_ceil_m != null ? encounter.area_ceil_m + 'm' : 'None'} · Floor: ${encounter.area_floor_m != null ? encounter.area_floor_m + 'm' : 'None'}`
          : 'None (Unrestricted Area)';
        const valStatus = sysStatus === 'passed' ? 'Live GCS / Pilot Position' : (sysStatus === 'zeroed' ? '⊘ Non-Compliant (Zeroed Pilot 0.000000°)' : 'Not Broadcast (Non-Compliant)');

        drawer.innerHTML = `
          <div class="comp-field-row"><span class="k">Pilot / GCS Latitude:</span> <span class="v font-mono ${sysStatus === 'passed' ? 'highlight-cyan' : ''}">${encounter.pilot_lat != null ? encounter.pilot_lat.toFixed(6) + '°' : (sysStatus === 'zeroed' ? '⊘ Zeroed (0.000000°)' : 'Not Broadcast')}</span></div>
          <div class="comp-field-row"><span class="k">Pilot / GCS Longitude:</span> <span class="v font-mono ${sysStatus === 'passed' ? 'highlight-cyan' : ''}">${encounter.pilot_lon != null ? encounter.pilot_lon.toFixed(6) + '°' : (sysStatus === 'zeroed' ? '⊘ Zeroed (0.000000°)' : 'Not Broadcast')}</span></div>
          <div class="comp-field-row"><span class="k">Pilot Ground Alt (MSL):</span> <span class="v font-mono">${encounter.pilot_alt_m != null ? encounter.pilot_alt_m + ' m' : 'N/A'}</span></div>
          <div class="comp-field-row"><span class="k">Operational Area Limits:</span> <span class="v font-mono">${ceilFloor}</span></div>
          <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono ${sysStatus === 'passed' ? 'highlight-green' : (sysStatus === 'zeroed' ? 'highlight-amber' : '')}">${valStatus}</span></div>
        `;
      }
    }

    // 4. Operator ID [Msg 0x5] (Mandatory under ASD-STAN EU Direct RID)
    const compOp = document.getElementById('comp-operator');
    const descOp = document.getElementById('comp-operator-desc');
    if (compOp) {
      if (opStatus === 'passed') {
        compOp.className = 'compliance-item clickable passed';
        compOp.querySelector('.comp-icon').textContent = '✔';
        if (descOp) descOp.textContent = encounter.operator_id;
      } else if (opStatus === 'zeroed') {
        compOp.className = 'compliance-item clickable zeroed';
        compOp.querySelector('.comp-icon').textContent = '⊘';
        if (descOp) descOp.innerHTML = '<span style="color:#fbbf24; font-style:italic;">⊘ Unset / Zeroed (Non-Compliant)</span>';
      } else {
        compOp.className = 'compliance-item clickable failed';
        compOp.querySelector('.comp-icon').textContent = '✖';
        if (descOp) descOp.textContent = 'Not Broadcast (Missing)';
      }

      const drawer = document.getElementById('drawer-comp-operator');
      if (drawer) {
        const valStatus = opStatus === 'passed' ? 'Registered Public ID' : (opStatus === 'zeroed' ? '⊘ Non-Compliant (0x00 unconfigured placeholder)' : 'Not Broadcast (Non-Compliant)');
        drawer.innerHTML = `
          <div class="comp-field-row"><span class="k">Operator Registration ID:</span> <span class="v font-mono ${opStatus === 'passed' ? 'highlight-green' : ''}">${encounter.operator_id || (opStatus === 'zeroed' ? '⊘ Zeroed (Unconfigured)' : 'Not Broadcast')}</span></div>
          <div class="comp-field-row"><span class="k">Registration Authority:</span> <span class="v">National Aviation Authority (EASA / CAA)</span></div>
          <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono ${opStatus === 'passed' ? 'highlight-green' : (opStatus === 'zeroed' ? 'highlight-amber' : '')}">${valStatus}</span></div>
        `;
      }
    }

    // 5. Self-ID [Msg 0x3] (OPTIONAL in ASD-STAN)
    const compSelf = document.getElementById('comp-self-id');
    const descSelf = document.getElementById('comp-self-id-desc');
    if (compSelf) {
      if (selfStatus === 'passed') {
        compSelf.className = 'compliance-item optional clickable passed';
        compSelf.querySelector('.comp-icon').textContent = '✔';
        if (descSelf) descSelf.textContent = `"${encounter.self_id_desc}"`;
      } else if (selfStatus === 'zeroed') {
        compSelf.className = 'compliance-item optional clickable zeroed';
        compSelf.querySelector('.comp-icon').textContent = '⊘';
        if (descSelf) descSelf.innerHTML = '<span style="color:#fbbf24; font-style:italic;">⊘ Broadcast (Empty / 0x00)</span>';
      } else {
        compSelf.className = 'compliance-item optional clickable optional-item';
        compSelf.querySelector('.comp-icon').textContent = '○';
        if (descSelf) descSelf.textContent = 'Not Broadcast (Optional)';
      }

      const drawer = document.getElementById('drawer-comp-self-id');
      if (drawer) {
        if (selfStatus === 'passed') {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Description:</span> <span class="v font-mono highlight-cyan">"${encounter.self_id_desc}"</span></div>
            <div class="comp-field-row"><span class="k">Description Type:</span> <span class="v">Text / Flight Purpose</span></div>
            <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono highlight-green">Mission Purpose Populated</span></div>
          `;
        } else if (selfStatus === 'zeroed') {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Description:</span> <span class="v font-mono highlight-amber">⊘ Zeroed (Empty String)</span></div>
            <div class="comp-field-row"><span class="k">Description Type:</span> <span class="v">Text / Flight Purpose</span></div>
            <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono highlight-amber">⊘ Broadcast but Empty (0x00 placeholder)</span></div>
          `;
        } else {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Description:</span> <span class="v font-mono">None (Optional)</span></div>
            <div class="comp-field-row"><span class="k">Description Type:</span> <span class="v">Text / Flight Purpose</span></div>
          `;
        }
      }
    }

    // 6. Auth [Msg 0x2] (OPTIONAL in ASD-STAN)
    const compAuth = document.getElementById('comp-auth');
    const descAuth = document.getElementById('comp-auth-desc');
    if (compAuth) {
      if (authStatus === 'passed') {
        compAuth.className = 'compliance-item optional clickable passed';
        compAuth.querySelector('.comp-icon').textContent = '✔';
        if (descAuth) descAuth.textContent = 'Digital Signature Verified';
      } else if (authStatus === 'zeroed') {
        compAuth.className = 'compliance-item optional clickable zeroed';
        compAuth.querySelector('.comp-icon').textContent = '⊘';
        if (descAuth) descAuth.innerHTML = '<span style="color:#fbbf24; font-style:italic;">⊘ Broadcast (Zeroed)</span>';
      } else {
        compAuth.className = 'compliance-item optional clickable optional-item';
        compAuth.querySelector('.comp-icon').textContent = '○';
        if (descAuth) descAuth.textContent = 'Not Broadcast (Optional)';
      }

      const drawer = document.getElementById('drawer-comp-auth');
      if (drawer) {
        if (authStatus === 'passed') {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Auth Data:</span> <span class="v highlight-green">Signature Attached</span></div>
            <div class="comp-field-row"><span class="k">Auth Type:</span> <span class="v">UAS ID / Operator Signature (0x0)</span></div>
            <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono highlight-green">Digital Signature Verified</span></div>
          `;
        } else if (authStatus === 'zeroed') {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Auth Data:</span> <span class="v highlight-amber">⊘ Zeroed (0x00)</span></div>
            <div class="comp-field-row"><span class="k">Auth Type:</span> <span class="v">UAS ID / Operator Signature (0x0)</span></div>
            <div class="comp-field-row"><span class="k">Block Status:</span> <span class="v font-mono highlight-amber">⊘ Transmitted as 0x00 placeholder</span></div>
          `;
        } else {
          drawer.innerHTML = `
            <div class="comp-field-row"><span class="k">Auth Data:</span> <span class="v">Not Transmitted (Optional for Direct RID)</span></div>
            <div class="comp-field-row"><span class="k">Auth Type:</span> <span class="v">UAS ID / Operator Signature (0x0)</span></div>
          `;
        }
      }
    }

    // Overall ASD-STAN Compliance Evaluation
    const overallBadge = document.getElementById('comp-overall-badge');
    if (overallBadge) {
      const allPassed = (basicStatus === 'passed' && locStatus === 'passed' && sysStatus === 'passed' && opStatus === 'passed');
      const anyZeroed = (basicStatus === 'zeroed' || locStatus === 'zeroed' || sysStatus === 'zeroed' || opStatus === 'zeroed');
      if (allPassed) {
        overallBadge.textContent = 'ASD-STAN COMPLIANT';
        overallBadge.className = 'comp-status-pill compliant';
      } else {
        overallBadge.textContent = 'NON-COMPLIANT';
        overallBadge.className = 'comp-status-pill non-compliant';
      }
    }
  }
}
