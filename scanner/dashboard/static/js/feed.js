/**
 * Tactical Drone Remote ID Airspace Monitor - Encounters Feed Controller
 * Renders flight cards, manages live search, filter tabs, Wi-Fi channel badges,
 * and highlights repeated encounters for the same aircraft (Serial / MAC).
 */

export class EncountersFeedController {
  constructor(listContainerId, countBadgeId, onSelectEncounter) {
    this.listContainer = document.getElementById(listContainerId);
    this.countBadge = document.getElementById(countBadgeId);
    this.onSelectEncounter = onSelectEncounter;
    
    this.encounters = [];
    this.activeFilter = 'all'; // 'all', 'active', 'bt', 'wifi'
    this.searchQuery = '';
    this.selectedEncounterId = null;

    this.initSearchAndFilters();
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
  }

  setEncounters(encountersList) {
    this.encounters = encountersList || [];
    this.render();
  }

  setSelectedEncounter(encounterId) {
    this.selectedEncounterId = encounterId;
    this.render();
  }

  filterEncounters() {
    return this.encounters.filter(enc => {
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
  }

  /**
   * Helper to format RF Channels with band hints
   */
  formatChannelBadge(transports = [], channels = []) {
    if (!channels || channels.length === 0) return '';
    const isWifi = transports.some(t => t.toLowerCase().includes('wifi') || t.toLowerCase().includes('nan'));
    const isBle = transports.some(t => t.toLowerCase().includes('bt') || t.toLowerCase().includes('ble'));

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

  render() {
    const filtered = this.filterEncounters();
    if (this.countBadge) {
      this.countBadge.textContent = filtered.length;
    }

    if (filtered.length === 0) {
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
    filtered.forEach(enc => {
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

      html += `
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
            <div class="card-transports">${transportChips} ${channelBadge}</div>
            <div class="card-stats">
              <span>Alt: <b>${maxAltStr}</b></span> · <span>Spd: <b>${maxSpeedStr}</b></span>
            </div>
          </div>
        </div>
      `;
    });

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
