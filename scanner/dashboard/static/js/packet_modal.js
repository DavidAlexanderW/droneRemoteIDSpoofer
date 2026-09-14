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
    this.currentEncounterNodeId = null;
    this.packets = [];
    this.filterText = '';
    this.selectedPacket = null;
    this.nodesList = [];

    this.initListeners();
  }

  setNodesList(nodes) {
    this.nodesList = Array.isArray(nodes) ? nodes : [];
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

  async open(encounterId, nodeId = null) {
    this.currentEncounterId = encounterId;
    this.currentEncounterNodeId = nodeId;
    this.titleEl.textContent = `ENCOUNTER: ${encounterId}`;
    this.modal.style.display = 'flex';
    this.tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 40px; color: var(--text-muted);">Loading captured Remote ID packets...</td></tr>`;

    try {
      const resp = await fetch(`/api/encounters/${encounterId}/packets`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this.packets = data.packets || [];
      this.renderTable();
    } catch (err) {
      this.tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 40px; color: var(--accent-red);">Failed to load packets: ${err.message}</td></tr>`;
    }
  }

  hide() {
    if (this.modal) this.modal.style.display = 'none';
    if (this.drawer) this.drawer.style.display = 'none';
    this.selectedPacket = null;
  }

  renderTable() {
    if (!this.packets || this.packets.length === 0) {
      this.tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; padding: 40px; color: var(--text-muted);">No packets recorded for this encounter.</td></tr>`;
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
      const rateDesc = pkt.rate_desc || (pkt.rate_mbps ? `${pkt.rate_mbps} Mbps` : '--');

      // Build message block tags
      const blockTags = (pkt.decoded_messages || []).map((msg, idx) => {
        const type = msg.type || 'ASTM Block';
        const isZeroed = Boolean(
          msg.is_zeroed ||
          (msg.msg_type === 3 && (!msg.description || msg.description.trim() === '')) ||
          (msg.msg_type === 5 && (!msg.operator_id || msg.operator_id.trim() === '') && (!msg.id || msg.id.trim() === '')) ||
          (msg.msg_type === 0 && (!msg.id || msg.id.trim() === '') && (!msg.ua_type || msg.ua_type === 0)) ||
          (msg.msg_type === 1 && msg.lat == null && msg.lon == null && msg.height_m == null && msg.alt == null && (!msg.status || msg.status === 0)) ||
          (msg.msg_type === 4 && msg.pilot_lat == null && msg.pilot_lon == null && !msg.classification_type && (!msg.operator_location_type || msg.operator_location_type === 0))
        );

        let cls = 'basic-id';
        if (type.includes('Location')) cls = 'location';
        else if (type.includes('Operator')) cls = 'operator';
        else if (type.includes('Self')) cls = 'self-id';
        else if (type.includes('System')) cls = 'system';
        else if (type.includes('Auth')) cls = 'auth';

        if (isZeroed) {
          cls += ' zeroed';
        }

        let extra = '';
        if (isZeroed) {
          extra = ` <span class="zero-badge">⊘ ZEROED</span>`;
        } else if (type.includes('Basic ID') || msg.msg_type === 0) {
          const uaType = msg.ua_type_name ? ` (${msg.ua_type_name})` : '';
          extra = msg.id ? `: ${msg.id}${uaType}` : (uaType ? `: ${uaType}` : '');
        } else if (type.includes('Location') || msg.msg_type === 1) {
          if (msg.lat != null && msg.lon != null) {
            let rxTag = '';
            const receivingNodeId = pkt.node_id || this.currentEncounterNodeId;
            if (receivingNodeId && this.nodesList) {
              const rxNode = this.nodesList.find(n => n.node_id === receivingNodeId);
              if (rxNode && rxNode.latitude != null && rxNode.longitude != null) {
                const dM = calculateHaversineDistanceM(parseFloat(rxNode.latitude), parseFloat(rxNode.longitude), msg.lat, msg.lon);
                const dStr = dM >= 1000 ? `${(dM / 1000).toFixed(2)}km` : `${Math.round(dM)}m`;
                rxTag = ` • Rx:${dStr}`;
              }
            }
            let altTag = '';
            if (msg.geodetic_altitude_m != null) {
              altTag = ` • ${Math.round(msg.geodetic_altitude_m)}m MSL`;
            } else if (msg.alt != null) {
              altTag = ` • ${Math.round(msg.alt)}m MSL`;
            }
            if (msg.height_m != null) {
              const hType = msg.height_type === 1 ? 'AGL' : 'ATO';
              altTag += ` (H:${msg.height_m >= 0 ? '+' : ''}${Math.round(msg.height_m)}m ${hType})`;
            }
            let spdTag = (msg.speed_mps != null && msg.speed_mps > 0) ? ` • ${msg.speed_mps.toFixed(1)}m/s` : '';
            let statTag = msg.status_name ? ` • ${msg.status_name}` : '';
            extra = `: (${msg.lat.toFixed(4)}, ${msg.lon.toFixed(4)})${altTag}${spdTag}${statTag}${rxTag}`;
          } else {
            let altTag = '';
            if (msg.height_m != null) {
              const hType = msg.height_type === 1 ? 'AGL' : 'ATO';
              altTag = ` • H:${msg.height_m >= 0 ? '+' : ''}${Math.round(msg.height_m)}m ${hType}`;
            }
            let statTag = msg.status_name ? ` • ${msg.status_name}` : ' • No GPS Fix';
            let spdTag = (msg.speed_mps != null && msg.speed_mps > 0) ? ` • ${msg.speed_mps.toFixed(1)}m/s` : '';
            extra = `: ${statTag.replace(/^ • /, '')}${altTag}${spdTag}`;
          }
        } else if (type.includes('Operator') || msg.msg_type === 5) {
          const opId = msg.operator_id || msg.id;
          extra = opId ? `: ${opId}` : ': (Unset)';
        } else if (type.includes('Self') || msg.msg_type === 3) {
          const desc = msg.description || msg.desc;
          extra = desc ? `: "${desc}"` : ': (Empty)';
        } else if (type.includes('System') || msg.msg_type === 4) {
          const sysParts = [];
          if (msg.pilot_lat != null && msg.pilot_lon != null) {
            sysParts.push(`Pilot: (${msg.pilot_lat.toFixed(4)}, ${msg.pilot_lon.toFixed(4)})`);
          }
          if (msg.pilot_alt_m != null) {
            sysParts.push(`Alt:${Math.round(msg.pilot_alt_m)}m`);
          }
          if (msg.class_eu_name) {
            sysParts.push(msg.class_eu_name);
          } else if (msg.category_eu_name && msg.category_eu_name !== 'Undeclared') {
            sysParts.push(msg.category_eu_name);
          } else if (msg.classification_type_name) {
            sysParts.push(msg.classification_type_name);
          }
          extra = sysParts.length > 0 ? `: ${sysParts.join(' • ')}` : '';
        } else if (type.includes('Auth') || msg.msg_type === 2) {
          extra = `: Page ${msg.page_number || 0}/${msg.page_count || 1}`;
        } else if (msg.id) {
          extra = `: ${msg.id}`;
        }

        return `<span class="astm-tag ${cls}">[${type}${extra}]</span>`;
      }).join('');

      html += `
        <tr class="${isSelected ? 'selected' : ''}" data-index="${pkt.index}">
          <td><b>${pkt.index}</b></td>
          <td>${offsetMs}</td>
          <td><span class="pill-chip ${transport.toLowerCase()}">${transport}</span></td>
          <td>ch${channel}</td>
          <td><span class="pill-chip font-mono" style="background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); font-size: 11px; padding: 1px 6px;">${rateDesc}</span></td>
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
    this.drawerDecoded.textContent = JSON.stringify(pkt.decoded_messages || [], null, 2);

    // PHY Layer details
    const phyInfo = [];
    if (pkt.rate_desc) phyInfo.push(`PHY Rate / Modulation : ${pkt.rate_desc}`);
    if (pkt.rate_mbps != null) phyInfo.push(`Data Rate (Mbps)     : ${pkt.rate_mbps}`);
    if (pkt.modulation) phyInfo.push(`Modulation Scheme     : ${pkt.modulation}`);
    if (pkt.bandwidth_mhz != null) phyInfo.push(`Channel Bandwidth     : ${pkt.bandwidth_mhz} MHz`);
    if (pkt.mcs_index != null) phyInfo.push(`MCS Index             : ${pkt.mcs_index}`);
    if (pkt.guard_interval) phyInfo.push(`Guard Interval        : ${pkt.guard_interval}`);
    if (pkt.rssi_dbm != null) phyInfo.push(`Antenna Signal (RSSI) : ${pkt.rssi_dbm} dBm`);
    if (pkt.channel != null) phyInfo.push(`Broadcast Channel     : ${pkt.channel}`);

    const phyHeader = phyInfo.length > 0 ? `[PHY RF LAYER METRICS]\n${phyInfo.join('\n')}\n\n` : '';

    // Base64 payloads + Hex Dissection if available
    const b64List = pkt.messages_b64 || [];
    if (b64List.length > 0) {
      this.drawerRaw.textContent = phyHeader + b64List.map((b64, i) => {
        let hexStr = '';
        let isZeroed = false;
        try {
          const rawB = atob(b64);
          hexStr = Array.from(rawB).map(c => c.charCodeAt(0).toString(16).padStart(2, '0')).join(' ').toUpperCase();
          if (rawB.length >= 25) {
            isZeroed = rawB.slice(1, 25).split('').every(c => c.charCodeAt(0) === 0);
          }
        } catch (e) {}
        const blockName = pkt.decoded_messages && pkt.decoded_messages[i] ? ` - ${pkt.decoded_messages[i].type}` : '';
        const zeroTag = (isZeroed || (pkt.decoded_messages && pkt.decoded_messages[i]?.is_zeroed)) ? ' [⊘ ZEROED / EMPTY PAYLOAD]' : '';
        return `Block #${i + 1}${blockName}${zeroTag} (Base64):\n${b64}\nHex Dissection:\n${hexStr}`;
      }).join('\n\n');
    } else {
      this.drawerRaw.textContent = `${phyHeader}(Synthesized from recorded SQLite telemetry fix)\nLat: ${pkt.decoded_messages?.[1]?.lat || 'N/A'}, Lon: ${pkt.decoded_messages?.[1]?.lon || 'N/A'}`;
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
