import { calculateHaversineDistanceM, calculateBearingDeg, getBearingCompass } from './map.js';

/**
 * Tactical Drone Remote ID Airspace Monitor - Deep Packet Inspector Controller
 * Manages the packet stream inspection modal, block filtering, and raw payload dissection.
 */

export class DeepPacketInspectorController {
  constructor() {
    this.modal = document.getElementById('packet-inspector-modal');
    this.titleEl = document.getElementById('modal-encounter-title');
    this.tbody = document.getElementById('packet-stream-tbody');
    this.showingCountEl = document.getElementById('modal-showing-count');
    this.totalCountEl = document.getElementById('modal-total-count');
    this.filterInput = document.getElementById('modal-filter-input');
    this.closeBtn = document.getElementById('btn-close-packet-modal');
    this.exportJsonBtn = document.getElementById('btn-modal-export-json');

    this.drawer = document.getElementById('packet-detail-drawer');
    this.drawerPktIdx = document.getElementById('drawer-pkt-idx');
    this.drawerDecoded = document.getElementById('drawer-decoded-json');
    this.drawerRaw = document.getElementById('drawer-raw-payload');
    this.drawerCloseBtn = document.getElementById('btn-close-drawer');

    this.currentEncounterId = null;
    this.packets = [];
    this.filterText = '';
    this.selectedPacket = null;
    this.receiverConfig = null;

    this.initListeners();
  }

  setReceiverConfig(config) {
    this.receiverConfig = config;
  }

  initListeners() {
    if (this.closeBtn) {
      this.closeBtn.addEventListener('click', () => this.hide());
    }

    if (this.filterInput) {
      this.filterInput.addEventListener('input', (e) => {
        this.filterText = e.target.value.trim().toLowerCase();
        this.renderTable();
      });
    }

    if (this.drawerCloseBtn) {
      this.drawerCloseBtn.addEventListener('click', () => {
        if (this.drawer) this.drawer.style.display = 'none';
      });
    }

    if (this.exportJsonBtn) {
      this.exportJsonBtn.addEventListener('click', () => {
        this.exportPacketsJson();
      });
    }

    // Close on backdrop click
    if (this.modal) {
      this.modal.addEventListener('click', (e) => {
        if (e.target === this.modal) this.hide();
      });
    }
  }

  async open(encounterId) {
    this.currentEncounterId = encounterId;
    this.titleEl.textContent = `ENCOUNTER: ${encounterId}`;
    this.modal.style.display = 'flex';
    this.tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 40px; color: var(--text-muted);">Loading captured Remote ID packets...</td></tr>`;

    try {
      const resp = await fetch(`/api/encounters/${encounterId}/packets`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this.packets = data.packets || [];
      this.renderTable();
    } catch (err) {
      this.tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 40px; color: var(--accent-red);">Failed to load packets: ${err.message}</td></tr>`;
    }
  }

  hide() {
    if (this.modal) this.modal.style.display = 'none';
    if (this.drawer) this.drawer.style.display = 'none';
    this.selectedPacket = null;
  }

  renderTable() {
    if (!this.packets || this.packets.length === 0) {
      this.tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 40px; color: var(--text-muted);">No packets recorded for this encounter.</td></tr>`;
      this.totalCountEl.textContent = '0';
      this.showingCountEl.textContent = '0';
      return;
    }

    const filtered = this.packets.filter(pkt => {
      if (!this.filterText) return true;
      const jsonStr = JSON.stringify(pkt).toLowerCase();
      return jsonStr.includes(this.filterText);
    });

    this.totalCountEl.textContent = this.packets.length;
    this.showingCountEl.textContent = filtered.length;

    let html = '';
    filtered.forEach(pkt => {
      const isSelected = this.selectedPacket && this.selectedPacket.index === pkt.index;
      const offsetMs = pkt.time_offset_ms != null ? `+${pkt.time_offset_ms}ms` : '--';
      const transport = pkt.transport ? pkt.transport.toUpperCase() : 'BLE';
      const channel = pkt.channel || 'N/A';
      const rssi = pkt.rssi_dbm != null ? `${pkt.rssi_dbm} dBm` : 'N/A';

      // Build message block tags
      const blockTags = (pkt.decoded_messages || []).map(msg => {
        const type = msg.type || 'ASTM Block';
        let cls = 'basic-id';
        if (type.includes('Location')) cls = 'location';
        else if (type.includes('Operator')) cls = 'operator';
        else if (type.includes('Self')) cls = 'self-id';
        else if (type.includes('System')) cls = 'system';
        else if (type.includes('Auth')) cls = 'auth';

        let extra = '';
        if (msg.id) {
          extra = `: ${msg.id}`;
        } else if (msg.operator_id) {
          extra = `: ${msg.operator_id}`;
        } else if (msg.lat != null && msg.lon != null) {
          let rxTag = '';
          if (this.receiverConfig && this.receiverConfig.enabled && this.receiverConfig.latitude != null) {
            const dM = calculateHaversineDistanceM(this.receiverConfig.latitude, this.receiverConfig.longitude, msg.lat, msg.lon);
            const dStr = dM >= 1000 ? `${(dM / 1000).toFixed(2)}km` : `${Math.round(dM)}m`;
            rxTag = ` • Rx:${dStr}`;
          }
          let altTag = '';
          if (msg.geodetic_altitude_m != null) {
            altTag = ` • ${Math.round(msg.geodetic_altitude_m)}m MSL`;
          }
          if (msg.height_m != null) {
            const hType = msg.height_type === 1 ? 'AGL' : 'ATO';
            altTag += ` (H:${msg.height_m >= 0 ? '+' : ''}${Math.round(msg.height_m)}m ${hType})`;
          }
          extra = `: (${msg.lat.toFixed(4)}, ${msg.lon.toFixed(4)})${altTag}${rxTag}`;
        } else if (type.includes('System')) {
          let sysInfo = '';
          if (msg.pilot_lat != null && msg.pilot_lon != null) {
            sysInfo += `Pilot: (${msg.pilot_lat.toFixed(4)}, ${msg.pilot_lon.toFixed(4)})`;
          }
          if (msg.pilot_alt_m != null) {
            sysInfo += ` • Alt:${Math.round(msg.pilot_alt_m)}m`;
          }
          if (msg.category_eu_name) {
            sysInfo += ` • ${msg.category_eu_name}`;
          }
          extra = sysInfo ? `: ${sysInfo}` : '';
        } else if (msg.description) {
          extra = `: ${msg.description}`;
        }

        return `<span class="astm-tag ${cls}">[${type}${extra}]</span>`;
      }).join('');

      html += `
        <tr class="${isSelected ? 'selected' : ''}" data-index="${pkt.index}">
          <td><b>${pkt.index}</b></td>
          <td>${offsetMs}</td>
          <td><span class="pill-chip ${transport.toLowerCase()}">${transport}</span></td>
          <td>ch${channel}</td>
          <td>${rssi}</td>
          <td>${blockTags || '<span style="color:var(--text-muted);">(No Decoded Blocks)</span>'}</td>
          <td><button class="btn-inspect-pkt">Dissect</button></td>
        </tr>
      `;
    });

    this.tbody.innerHTML = html;

    // Attach row click listeners
    const rows = this.tbody.querySelectorAll('tr');
    rows.forEach(row => {
      row.addEventListener('click', () => {
        const idx = parseInt(row.dataset.index, 10);
        const pkt = this.packets.find(p => p.index === idx);
        if (pkt) this.inspectPacket(pkt);
      });
    });
  }

  inspectPacket(pkt) {
    this.selectedPacket = pkt;
    this.renderTable(); // updates selected row highlight

    if (!this.drawer) return;
    this.drawer.style.display = 'flex';
    this.drawerPktIdx.textContent = pkt.index;

    // Decoded JSON
    this.drawerDecoded.textContent = JSON.stringify(pkt.decoded_messages || {}, null, 2);

    // Base64 payloads if available
    const b64List = pkt.messages_b64 || [];
    if (b64List.length > 0) {
      this.drawerRaw.textContent = b64List.map((b64, i) => `Block #${i + 1} (Base64):\n${b64}`).join('\n\n');
    } else {
      this.drawerRaw.textContent = `(Synthesized from recorded SQLite telemetry fix)\nLat: ${pkt.decoded_messages?.[1]?.lat || 'N/A'}, Lon: ${pkt.decoded_messages?.[1]?.lon || 'N/A'}`;
    }
  }

  exportPacketsJson() {
    if (!this.packets || this.packets.length === 0) return;
    const blob = new Blob([JSON.stringify(this.packets, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `packets_${this.currentEncounterId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
