import { calculateHaversineDistanceM } from './map.js';

/**
 * Tactical Drone Remote ID Airspace Monitor - Encounters Feed Controller
 * Renders flight cards, manages live search, filter tabs, Wi-Fi channel badges,
 * configurable chronological / receiver distance sorting, and highlights repeated encounters.
 */

export class EncountersFeedController {
  constructor(listContainerId, countBadgeId, onSelectEncounter) {
    this.listContainer = document.getElementById(listContainerId);
    this.countBadge = document.getElementById(countBadgeId);
    this.onSelectEncounter = onSelectEncounter;
    
    this.encounters = [];
    this.activeFilter = 'all'; // 'all', 'active', 'bt', 'wifi'
    this.sortBy = 'time_desc'; // 'time_desc', 'time_asc', 'duration_desc', 'dist_asc', 'dist_desc', 'packets_desc'
    this.searchQuery = '';
    this.selectedEncounterId = null;
    this.nodesList = [];

    this.initSearchAndFilters();
  }

  setNodesList(nodes) {
    this.nodesList = Array.isArray(nodes) ? nodes : [];
    if (this.sortBy.startsWith('dist_')) {
      this.render();
    }
  }

  initSearchAndFilters() {
    const searchInput = document.getElementById('feed-search-input');
    const clearBtn = document.getElementById('feed-search-clear');

    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        this.searchQuery = e.target.value.trim().toLowerCase();
        if (clearBtn) clearBtn.style.display = this.searchQuery ? 'block' : 'none';
        this.render();
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        if (searchInput) searchInput.value = '';
        this.searchQuery = '';
        clearBtn.style.display = 'none';
        this.render();
      });
    }

    const filterTabs = document.querySelectorAll('.filter-tab');
    filterTabs.forEach(tab => {
      tab.addEventListener('click', () => {
        filterTabs.forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        this.activeFilter = tab.dataset.filter;
        this.render();
      });
    });

    const sortSelect = document.getElementById('feed-sort-select');
    if (sortSelect) {
      sortSelect.addEventListener('change', (e) => {
        this.sortBy = e.target.value;
        this.render();
      });
    }
  }

  setEncounters(encountersList) {
    this.encounters = encountersList || [];
    this.render();
  }

  setSelectedEncounter(encounterId) {
    this.selectedEncounterId = encounterId;
    this.render();
  }

  getEncounterDistanceM(enc) {
    if (!enc || !enc.node_id || !this.nodesList || this.nodesList.length === 0) {
      return null;
    }

    const rxNode = this.nodesList.find(n => n.node_id === enc.node_id);
    if (!rxNode || rxNode.latitude == null || rxNode.longitude == null) {
      return null;
    }

    let targetLat = null;
    let targetLon = null;
    let isPilot = false;

    // 1. Check latest position of drone
    if (enc.latest_position && enc.latest_position.lat != null && enc.latest_position.lon != null && !isNaN(enc.latest_position.lat) && !isNaN(enc.latest_position.lon)) {
      targetLat = enc.latest_position.lat;
      targetLon = enc.latest_position.lon;
    } else if (enc.trajectory && enc.trajectory.length > 0) {
      // 2. Check latest trajectory point
      const valid = enc.trajectory.filter(pt => Array.isArray(pt) && pt.length >= 2 && !isNaN(pt[0]) && !isNaN(pt[1]));
      if (valid.length > 0) {
        const last = valid[valid.length - 1];
        targetLat = last[0];
        targetLon = last[1];
      }
    } else if (enc.pilot_lat != null && enc.pilot_lon != null && !isNaN(enc.pilot_lat) && !isNaN(enc.pilot_lon)) {
      // 3. Fallback to pilot / GCS coordinates
      targetLat = enc.pilot_lat;
      targetLon = enc.pilot_lon;
      isPilot = true;
    }

    if (targetLat == null || targetLon == null) return null;

    const rxLat = parseFloat(rxNode.latitude);
    const rxLon = parseFloat(rxNode.longitude);
    return {
      distM: calculateHaversineDistanceM(rxLat, rxLon, targetLat, targetLon),
      isPilot,
      nodeName: rxNode.name || rxNode.node_id,
    };
  }

  filterAndSortEncounters() {
    const filtered = this.encounters.filter(enc => {
      // 1. Tab filter
      if (this.activeFilter === 'active' && !enc.is_active) return false;
      if (this.activeFilter === 'bt') {
        const hasBt = (enc.transports || []).some(t => t.includes('bt') || t.includes('ble'));
        if (!hasBt) return false;
      }
      if (this.activeFilter === 'wifi') {
        const hasWifi = (enc.transports || []).some(t => t.includes('wifi') || t.includes('nan'));
        if (!hasWifi) return false;
      }

      // 2. Search query filter
      if (this.searchQuery) {
        const mac = (enc.mac || '').toLowerCase();
        const serial = (enc.serial_number || '').toLowerCase();
        const op = (enc.operator_id || '').toLowerCase();
        const encId = (enc.encounter_id || '').toLowerCase();
        const make = (enc.drone_make || '').toLowerCase();
        const model = (enc.drone_model || '').toLowerCase();
        const match = mac.includes(this.searchQuery) ||
                      serial.includes(this.searchQuery) ||
                      op.includes(this.searchQuery) ||
                      encId.includes(this.searchQuery) ||
                      make.includes(this.searchQuery) ||
                      model.includes(this.searchQuery);
        if (!match) return false;
      }

      return true;
    });

    // Compute distance info for each encounter
    filtered.forEach(enc => {
      const dInfo = this.getEncounterDistanceM(enc);
      enc._distInfo = dInfo;
      enc._distM = dInfo ? dInfo.distM : null;
      enc._distIsPilot = dInfo ? dInfo.isPilot : false;
    });

    // Sort according to active sort mode
    if (this.sortBy === 'dist_asc') {
      const withDist = filtered.filter(e => e._distM != null).sort((a, b) => a._distM - b._distM);
      const withoutDist = filtered.filter(e => e._distM == null).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
      return { withDist, withoutDist, isDistanceMode: true };
    }

    if (this.sortBy === 'dist_desc') {
      const withDist = filtered.filter(e => e._distM != null).sort((a, b) => b._distM - a._distM);
      const withoutDist = filtered.filter(e => e._distM == null).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
      return { withDist, withoutDist, isDistanceMode: true };
    }

    if (this.sortBy === 'time_asc') {
      const sorted = [...filtered].sort((a, b) => (a.first_seen || a.last_seen || 0) - (b.first_seen || b.last_seen || 0));
      return { withDist: sorted, withoutDist: [], isDistanceMode: false };
    }

    if (this.sortBy === 'duration_desc') {
      const sorted = [...filtered].sort((a, b) => {
        const durA = a.duration_s != null ? a.duration_s : ((a.last_seen || 0) - (a.first_seen || 0));
        const durB = b.duration_s != null ? b.duration_s : ((b.last_seen || 0) - (b.first_seen || 0));
        return durB - durA;
      });
      return { withDist: sorted, withoutDist: [], isDistanceMode: false };
    }

    if (this.sortBy === 'packets_desc') {
      const sorted = [...filtered].sort((a, b) => (b.packet_count || 0) - (a.packet_count || 0));
      return { withDist: sorted, withoutDist: [], isDistanceMode: false };
    }

    // Default: 'time_desc' (Latest Seen First)
    const sorted = [...filtered].sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
    return { withDist: sorted, withoutDist: [], isDistanceMode: false };
  }

  /**
   * Helper to format RF Channels with band hints
   */
  formatChannelBadge(transports = [], channels = []) {
    if (!channels || channels.length === 0) return '';
    const isWifi = transports.some(t => t.toLowerCase().includes('wifi') || t.toLowerCase().includes('nan'));

    const formattedList = channels.map(ch => {
      const num = parseInt(ch, 10);
      if (isWifi && !isNaN(num)) {
        if (num >= 1 && num <= 14) return `CH ${num} (2.4G)`;
        if (num >= 36) return `CH ${num} (5.8G)`;
      }
      return `CH ${ch}`;
    });

    return `<span class="pill-chip channel-pill" title="Radio Frequency Channels">${formattedList.join(', ')}</span>`;
  }

  renderCard(enc, isDistanceMode, aircraftFlightCounts, selectedAircraftKey) {
    const isSelected = enc.encounter_id === this.selectedEncounterId;
    const aircraftKey = enc.serial_number || enc.mac;
    const totalDroneFlights = aircraftKey ? (aircraftFlightCounts.get(aircraftKey) || 1) : 1;
    const isSameAircraft = selectedAircraftKey && (aircraftKey === selectedAircraftKey) && !isSelected;
    const isDimmed = selectedAircraftKey && (aircraftKey !== selectedAircraftKey) && !isSelected;

    const statusClass = enc.is_active ? 'active' : '';
    const displaySerial = enc.serial_number || enc.mac || enc.encounter_id.slice(0, 12);
    const displayTime = enc.last_seen_iso ? new Date(enc.last_seen_iso).toLocaleTimeString() : '--:--';
    const durationStr = enc.duration_s != null ? `${Math.round(enc.duration_s)}s` : '';
    const pktCount = enc.packet_count || 0;
    const pktStr = `${pktCount} pkt${pktCount === 1 ? '' : 's'}`;
    const metaDurationPkts = durationStr ? `${durationStr} · ${pktStr}` : pktStr;

    const maxAltStr = enc.max_alt_m != null ? `${Math.round(enc.max_alt_m)}m` : 'N/A';
    const maxSpeedStr = enc.max_speed_mps != null ? `${Math.round(enc.max_speed_mps)}m/s` : 'N/A';

    // Sensor Distance Badge
    let distBadge = '';
    if (enc._distM != null) {
      const distStr = enc._distM >= 1000 ? `${(enc._distM / 1000).toFixed(2)} km` : `${Math.round(enc._distM)} m`;
      const isPilot = Boolean(enc._distIsPilot);
      const nodeName = enc._distInfo && enc._distInfo.nodeName ? enc._distInfo.nodeName : 'Sensor';
      distBadge = `<span class="pill-chip rx-dist-pill ${isDistanceMode ? 'highlight-sort' : ''}" title="${isPilot ? 'Pilot / GCS Distance' : 'Aircraft Distance'} to 📡 ${nodeName}">📡 ${isPilot ? 'GCS: ' : ''}${distStr}</span>`;
    } else if (isDistanceMode) {
      distBadge = `<span class="pill-chip rx-dist-pill no-fix" title="No GNSS position coordinates recorded">📡 No Range</span>`;
    }

    // Transports & Channel Chips
    const transportChips = (enc.transports || []).map(t => {
      const clean = t.toLowerCase().trim();
      return `<span class="pill-chip ${clean}">${clean.toUpperCase()}</span>`;
    }).join(' ');

    const channelBadge = this.formatChannelBadge(enc.transports, enc.channels);

    // Operator ID Badge
    const operatorHtml = enc.operator_id ? `
      <div class="card-operator-row">
        <span class="operator-badge selectable-text" title="Public CAA Registered Operator ID (Select to copy)">
          <span class="caa-tag">CAA</span> ${enc.operator_id}
        </span>
      </div>
    ` : '';

    // Drone Make & Model Badge
    let modelHtml = '';
    if (enc.drone_make || enc.drone_model) {
      const fullModel = [enc.drone_make, enc.drone_model].filter(Boolean).join(' ');
      modelHtml = `
        <div class="card-model-row">
          <span class="drone-model-pill" title="Inferred / Registered Drone Make & Model">
            <span class="drone-icon">🚁</span> ${fullModel}
          </span>
        </div>
      `;
    }

    // Same drone repeated encounters indicator
    let sameDroneBadge = '';
    if (isSameAircraft) {
      sameDroneBadge = `<span class="same-drone-pill">🔁 SAME AIRCRAFT</span>`;
    } else if (totalDroneFlights > 1) {
      sameDroneBadge = `<span class="repeat-count-pill" title="Total ${totalDroneFlights} flights detected for this aircraft">🔁 ${totalDroneFlights} FLIGHTS</span>`;
    }

    let cardClasses = 'encounter-card';
    if (isSelected) cardClasses += ' selected';
    if (isSameAircraft) cardClasses += ' same-aircraft-highlight';
    if (isDimmed) cardClasses += ' dimmed-card';

    return `
      <div class="${cardClasses}" data-id="${enc.encounter_id}" data-key="${aircraftKey || ''}">
        <div class="card-top">
          <div class="card-target-id">
            <span class="card-status-indicator ${statusClass}"></span>
            <span class="card-serial selectable-text" title="Aircraft Identifier (Select to copy)">${displaySerial}</span>
          </div>
          <div class="card-time">${displayTime} (${metaDurationPkts})</div>
        </div>

        ${sameDroneBadge}
        ${modelHtml}
        ${operatorHtml}

        <div class="card-meta-row">
          <div class="card-transports">${transportChips} ${channelBadge} ${distBadge}</div>
          <div class="card-stats">
            <span>Alt: <b>${maxAltStr}</b></span> · <span>Spd: <b>${maxSpeedStr}</b></span>
          </div>
        </div>
      </div>
    `;
  }

  render() {
    const { withDist, withoutDist, isDistanceMode } = this.filterAndSortEncounters();
    const totalCount = withDist.length + withoutDist.length;

    if (this.countBadge) {
      this.countBadge.textContent = totalCount;
    }

    if (totalCount === 0) {
      this.listContainer.innerHTML = `
        <div class="empty-state">
          <p>No encounters matching current filter.</p>
        </div>
      `;
      return;
    }

    // Count flights per aircraft identifier (Serial Number or MAC)
    const aircraftFlightCounts = new Map();
    this.encounters.forEach(enc => {
      const key = enc.serial_number || enc.mac;
      if (key) {
        aircraftFlightCounts.set(key, (aircraftFlightCounts.get(key) || 0) + 1);
      }
    });

    // Find selected aircraft key
    const selectedEnc = this.selectedEncounterId 
      ? this.encounters.find(e => e.encounter_id === this.selectedEncounterId)
      : null;
    const selectedAircraftKey = selectedEnc ? (selectedEnc.serial_number || selectedEnc.mac) : null;

    let html = '';

    // 1. Render primary sorted encounters list
    withDist.forEach(enc => {
      html += this.renderCard(enc, isDistanceMode, aircraftFlightCounts, selectedAircraftKey);
    });

    // 2. Render encounters without distance fixes in a distinct section when sorting by distance
    if (isDistanceMode && withoutDist.length > 0) {
      html += `
        <div class="feed-section-divider">
          <span class="divider-line"></span>
          <span class="divider-text">NO GNSS RANGE FIX (${withoutDist.length})</span>
          <span class="divider-line"></span>
        </div>
      `;
      withoutDist.forEach(enc => {
        html += this.renderCard(enc, isDistanceMode, aircraftFlightCounts, selectedAircraftKey);
      });
    }

    this.listContainer.innerHTML = html;

    // Attach click listeners to cards
    const cards = this.listContainer.querySelectorAll('.encounter-card');
    cards.forEach(card => {
      card.addEventListener('click', (e) => {
        // If user is currently highlighting text with cursor, don't interrupt text selection
        const selection = window.getSelection();
        if (selection && selection.toString().trim().length > 0) {
          return;
        }

        const id = card.dataset.id;
        this.setSelectedEncounter(id);
        if (this.onSelectEncounter) {
          this.onSelectEncounter(id);
        }
      });
    });
  }
}
