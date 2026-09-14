/**
 * Tactical Drone Remote ID Airspace Monitor - Leaflet Tactical Map Controller
 * Renders airspace flight tracks (muted background vs neon cyan selected),
 * discrete waypoint fix dots for the selected flight, pilot/GCS links,
 * configurable receiver station with tactical range rings,
 * and synchronizes timeline scrubbing with fix telemetry.
 */

export function calculateHaversineDistanceM(lat1, lon1, lat2, lon2) {
  const R = 6371000.0;
  const dLat = (lat2 - lat1) * Math.PI / 180.0;
  const dLon = (lon2 - lon1) * Math.PI / 180.0;
  const a = Math.sin(dLat / 2.0) ** 2 +
            Math.cos(lat1 * Math.PI / 180.0) * Math.cos(lat2 * Math.PI / 180.0) *
            (Math.sin(dLon / 2.0) ** 2);
  const c = 2.0 * Math.atan2(Math.sqrt(a), Math.sqrt(1.0 - a));
  return R * c;
}

export function calculateBearingDeg(lat1, lon1, lat2, lon2) {
  const phi1 = lat1 * Math.PI / 180.0;
  const phi2 = lat2 * Math.PI / 180.0;
  const dLon = (lon2 - lon1) * Math.PI / 180.0;
  const y = Math.sin(dLon) * Math.cos(phi2);
  const x = Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dLon);
  const theta = Math.atan2(y, x);
  return (theta * 180.0 / Math.PI + 360.0) % 360.0;
}

export function getBearingCompass(deg) {
  const directions = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  const idx = Math.round(deg / 22.5) % 16;
  return directions[idx];
}

export class TacticalMapController {
  constructor(containerId, onSelectEncounter, onSelectWaypoint, onUpdateNodePosition) {
    this.containerId = containerId;
    this.onSelectEncounter = onSelectEncounter;
    this.onSelectWaypoint = onSelectWaypoint;
    this.onUpdateNodePosition = onUpdateNodePosition;
    
    this.map = null;
    this.allEncounters = [];
    this.selectedEncounterId = null;
    this.dashboardConfig = null;
    this.nodesList = [];

    // Layer storage
    this.trackLayers = new Map();     // encounter_id -> Leaflet Polyline
    this.droneMarkers = new Map();    // encounter_id -> Leaflet Marker
    this.waypointLayers = new Map();  // encounter_id -> Leaflet LayerGroup
    this.pilotMarkers = new Map();    // encounter_id -> Leaflet Marker
    this.pilotLines = new Map();      // encounter_id -> Leaflet Polyline
    this.activeWaypointMarkers = [];  // Waypoint markers for selected flight

    // Active selected fix state
    this.activeFixIndex = null;
    this.activeFixPoint = null;
    this.isFixPopupOpen = false;

    // Multi-node receiver layers
    this.multiNodeMarkers = new Map();      // node_id -> Leaflet Marker
    this.multiNodeRingLayers = new Map();   // node_id -> Leaflet LayerGroup

    this.showTrails = true;
    this.showWaypoints = true;
    this.showRangeRings = true;

    this.initMap();
  }

  setDashboardConfig(config) {
    if (!config) return;
    this.dashboardConfig = config;
    this.showRangeRings = config.show_range_rings !== false;
    this.showTrails = config.show_trails !== false;
    this.showWaypoints = config.show_waypoints !== false;
  }

  setNodesList(nodes) {
    if (!Array.isArray(nodes) || !this.map) return;
    this.nodesList = nodes;
    const activeNodeIds = new Set();

    nodes.forEach(node => {
      const nodeId = node.node_id;
      if (!nodeId) return;
      activeNodeIds.add(nodeId);

      const lat = parseFloat(node.latitude);
      const lon = parseFloat(node.longitude);
      if (isNaN(lat) || isNaN(lon)) return;

      const name = node.name || nodeId;
      const status = node.status || 'ONLINE';
      const statusColor = status === 'ONLINE' ? '#10b981' : (status === 'DEGRADED' ? '#f59e0b' : '#ef4444');
      const isOnline = status === 'ONLINE';
      const isLocked = Boolean(node.locked);

      const createNodeIcon = () => {
        const lockBadge = isLocked ? `
          <div style="position: absolute; top: -3px; right: -3px; background: #ef4444; border: 1.5px solid #ffffff; border-radius: 50%; width: 14px; height: 14px; display: flex; align-items: center; justify-content: center; font-size: 8px; box-shadow: 0 0 6px rgba(239, 68, 68, 0.8);">
            🔒
          </div>
        ` : `
          <div style="position: absolute; top: -3px; right: -3px; background: #10b981; border: 1.5px solid #ffffff; border-radius: 50%; width: 14px; height: 14px; display: flex; align-items: center; justify-content: center; font-size: 8px; box-shadow: 0 0 6px rgba(16, 185, 129, 0.8);">
            🔓
          </div>
        `;

        return L.divIcon({
          html: `
            <div class="receiver-marker-container" style="position: relative; display: flex; flex-direction: column; align-items: center;">
              <div class="receiver-marker-icon" style="width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; position: relative;">
                <svg viewBox="0 0 36 36" width="34" height="34" fill="none">
                  <circle cx="18" cy="18" r="16" stroke="${statusColor}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.8" class="rx-pulse-ring" />
                  <circle cx="18" cy="18" r="7" fill="${statusColor}" stroke="#ffffff" stroke-width="1.5" />
                  <line x1="18" y1="4" x2="18" y2="18" stroke="#ffffff" stroke-width="2" stroke-linecap="round" />
                  <polygon points="18,3 15,9 21,9" fill="#ffffff" />
                </svg>
                ${lockBadge}
              </div>
              <div class="receiver-marker-label" style="font-size: 10px; font-weight: 700; color: #cbd5e1; background: rgba(8, 12, 22, 0.85); padding: 1px 5px; border-radius: 3px; margin-top: 1px; border: 1px solid ${statusColor}44;">
                ${name}
              </div>
            </div>
          `,
          className: 'custom-multi-node-icon',
          iconSize: [36, 44],
          iconAnchor: [18, 16],
          popupAnchor: [0, -16],
        });
      };

      const popupHtml = `
        <div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 6px; color: #080c16; line-height: 1.5; min-width: 220px;">
          <div style="font-weight: 800; color: #0284c7; margin-bottom: 4px; display: flex; justify-content: space-between; align-items: center; gap: 6px;">
            <span>📡 ${name}</span>
            <span style="font-size: 9px; font-weight: 800; color: #fff; background: ${statusColor}; padding: 1px 5px; border-radius: 3px;">${status}</span>
          </div>
          <b>Node ID:</b> ${nodeId}<br/>
          <b>Coordinates:</b> ${lat.toFixed(6)}° N, ${lon.toFixed(6)}° E<br/>
          <b>Altitude:</b> ${parseFloat(node.altitude_m || 0).toFixed(1)}m MSL<br/>
          <b>Packets Received:</b> ${(node.packets_received_total || 0).toLocaleString()}<br/>
          <b>Disk Lock:</b> ${isLocked ? '<span style="color: #ef4444; font-weight: 700;">🔒 Locked (scanner_config.json)</span>' : '<span style="color: #10b981; font-weight: 700;">🔓 Unlocked (Draggable)</span>'}<br/>
          <div style="margin-top: 6px; padding-top: 4px; border-top: 1px solid #e2e8f0; font-size: 9.5px; color: ${isLocked ? '#ef4444' : '#059669'};">
            ${isLocked ? 'Position write-protected on scanner disk. Set "locked": false in scanner_config.json to reposition.' : '👉 Drag icon on map to calibrate position.'}
          </div>
        </div>
      `;

      // 1. Create or update Node Marker
      if (!this.multiNodeMarkers.has(nodeId)) {
        const marker = L.marker([lat, lon], {
          icon: createNodeIcon(),
          draggable: !isLocked,
          zIndexOffset: 2500,
        }).addTo(this.map);

        marker.bindPopup(popupHtml);

        marker.on('dragend', (e) => {
          if (node.locked) return;
          const newPos = e.target.getLatLng();
          const newLat = parseFloat(newPos.lat.toFixed(6));
          const newLon = parseFloat(newPos.lng.toFixed(6));
          node.latitude = newLat;
          node.longitude = newLon;
          if (this.onUpdateNodePosition) {
            this.onUpdateNodePosition(nodeId, newLat, newLon);
          }
        });

        this.multiNodeMarkers.set(nodeId, marker);
      } else {
        const marker = this.multiNodeMarkers.get(nodeId);
        marker.setLatLng([lat, lon]);
        marker.setIcon(createNodeIcon());
        marker.bindPopup(popupHtml);
        if (marker.dragging) {
          if (isLocked) marker.dragging.disable();
          else marker.dragging.enable();
        }
      }

      // 2. Render Range Rings for this node
      let ringGroup = this.multiNodeRingLayers.get(nodeId);
      if (!ringGroup) {
        ringGroup = L.layerGroup();
        if (this.showRangeRings) ringGroup.addTo(this.map);
        this.multiNodeRingLayers.set(nodeId, ringGroup);
      }
      ringGroup.clearLayers();

      if (this.showRangeRings && isOnline) {
        let rings = [500, 1000, 2500, 5000];
        try {
          if (node.range_rings_json) rings = JSON.parse(node.range_rings_json);
        } catch (e) {}

        rings.forEach((radiusM, idx) => {
          const radiusKm = radiusM >= 1000 ? `${(radiusM / 1000).toFixed(1)} km` : `${radiusM} m`;

          // 1. Circle Polygon (tactical amber radar overlay)
          const ring = L.circle([lat, lon], {
            radius: radiusM,
            color: '#f59e0b',
            weight: 1.3,
            opacity: 0.55,
            fill: true,
            fillColor: '#f59e0b',
            fillOpacity: 0.018 * (4 - Math.min(3, idx)),
            dashArray: '5, 6',
            interactive: false,
          });
          ringGroup.addLayer(ring);

          // 2. Clear Cardinal Distance Label on North Perimeter of Circle
          const dLat = (radiusM / 6371000.0) * (180.0 / Math.PI);
          const labelLatLng = [lat + dLat, lon];

          const labelMarker = L.marker(labelLatLng, {
            icon: L.divIcon({
              html: `<div class="range-ring-pill">${radiusKm}</div>`,
              className: 'custom-range-ring-label',
              iconSize: [60, 16],
              iconAnchor: [30, 8],
            }),
            interactive: false,
            zIndexOffset: 100,
          });
          ringGroup.addLayer(labelMarker);
        });
      }
    });

    // Clean up removed nodes
    for (const [nodeId, marker] of this.multiNodeMarkers.entries()) {
      if (!activeNodeIds.has(nodeId)) {
        this.map.removeLayer(marker);
        this.multiNodeMarkers.delete(nodeId);
        if (this.multiNodeRingLayers.has(nodeId)) {
          this.map.removeLayer(this.multiNodeRingLayers.get(nodeId));
          this.multiNodeRingLayers.delete(nodeId);
        }
      }
    }
  }

  initMap() {
    // Default center: Zurich / Switzerland (47.3769, 8.5417)
    this.map = L.map(this.containerId, {
      center: [47.3769, 8.5417],
      zoom: 14,
      zoomControl: false,
      attributionControl: false,
    });

    // Custom tactical zoom control on top right
    L.control.zoom({ position: 'topright' }).addTo(this.map);

    // Standard OpenStreetMap tile layer (100% free, zero API key required)
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(this.map);

    // Track mouse coordinates for HUD overlay
    const coordEl = document.getElementById('map-cursor-coords');
    this.map.on('mousemove', (e) => {
      if (coordEl) {
        const lat = e.latlng.lat.toFixed(5);
        const lon = e.latlng.lng.toFixed(5);
        coordEl.textContent = `${lat}° N, ${lon}° E`;
      }
    });
  }

  toggleRangeRings(show) {
    this.showRangeRings = show;
    this.multiNodeRingLayers.forEach(layer => {
      if (show) layer.addTo(this.map);
      else this.map.removeLayer(layer);
    });
  }

  centerOnNodes() {
    if (this.nodesList && this.nodesList.length > 0) {
      const bounds = L.latLngBounds([]);
      this.nodesList.forEach(n => {
        const lat = parseFloat(n.latitude);
        const lon = parseFloat(n.longitude);
        if (!isNaN(lat) && !isNaN(lon)) bounds.extend([lat, lon]);
      });
      if (bounds.isValid()) {
        this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 14 });
        return;
      }
    }
    if (this.dashboardConfig && this.dashboardConfig.center_latitude != null) {
      this.map.flyTo([this.dashboardConfig.center_latitude, this.dashboardConfig.center_longitude], this.dashboardConfig.default_zoom || 13);
    }
  }

  /**
   * Draws a dashed vector line between the receiving sensor node and currently selected target aircraft
   */
  renderReceiverVector(targetLatLng = null) {
    if (this.receiverVectorLine) {
      this.map.removeLayer(this.receiverVectorLine);
      this.receiverVectorLine = null;
    }

    if (!this.selectedEncounterId) return;
    const enc = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
    if (!enc || !enc.node_id || !this.nodesList) return;

    const rxNode = this.nodesList.find(n => n.node_id === enc.node_id);
    if (!rxNode || rxNode.latitude == null || rxNode.longitude == null) return;

    let target = targetLatLng;
    if (!target) {
      if (enc.trajectory && enc.trajectory.length > 0) {
        const last = enc.trajectory[enc.trajectory.length - 1];
        target = [last[0], last[1]];
      } else if (enc.latest_position) {
        target = [enc.latest_position.lat, enc.latest_position.lon];
      }
    }

    if (!target || target.length < 2 || isNaN(target[0]) || isNaN(target[1])) return;

    const rxLat = parseFloat(rxNode.latitude);
    const rxLon = parseFloat(rxNode.longitude);
    const nodeName = rxNode.name || rxNode.node_id;

    const distM = calculateHaversineDistanceM(rxLat, rxLon, target[0], target[1]);
    const bearing = calculateBearingDeg(rxLat, rxLon, target[0], target[1]);
    const distStr = distM >= 1000 ? `${(distM / 1000).toFixed(2)} km` : `${Math.round(distM)} m`;
    const compass = getBearingCompass(bearing);

    this.receiverVectorLine = L.polyline([[rxLat, rxLon], target], {
      color: '#f59e0b',
      weight: 1.5,
      dashArray: '3, 5',
      opacity: 0.75,
      className: 'rx-vector-line',
    }).bindTooltip(`📡 ${nodeName} → Target: ${distStr} · ${Math.round(bearing)}° ${compass}`, {
      sticky: true,
      className: 'custom-range-tooltip',
    });

    this.receiverVectorLine.addTo(this.map);
  }

  /**
   * Generates a sleek SVG drone icon rotated to the aircraft's heading.
   */
  createDroneIcon(encounterId, headingDeg = 0, isSelected = false, isActive = false, label = '') {
    let color = '#64748b'; // Muted slate by default
    if (isSelected) {
      color = '#00f0ff';   // Neon Cyan for selected target
    } else if (isActive) {
      color = '#38bdf8';   // Active blue
    }

    const svgIcon = `
      <div class="drone-marker-container ${isSelected ? 'selected-target' : 'bg-target'}" style="position: relative; display: flex; flex-direction: column; align-items: center;">
        <div class="drone-marker-svg ${isSelected ? 'active-pulse' : ''}" style="transform: rotate(${headingDeg}deg); width: 30px; height: 30px; display: flex; align-items: center; justify-content: center;">
          <svg viewBox="0 0 36 36" width="30" height="30" fill="none">
            <!-- Center body -->
            <circle cx="18" cy="18" r="5" fill="${color}" stroke="#ffffff" stroke-width="1.5" />
            <!-- Heading triangle -->
            <polygon points="18,4 13,15 23,15" fill="${color}" />
            <!-- Quadcopter arms -->
            <line x1="8" y1="8" x2="28" y2="28" stroke="${color}" stroke-width="2" stroke-linecap="round" />
            <line x1="28" y1="8" x2="8" y2="28" stroke="${color}" stroke-width="2" stroke-linecap="round" />
            <!-- Motor rotors -->
            <circle cx="8" cy="8" r="3" fill="#0f172a" stroke="${color}" stroke-width="1.5" />
            <circle cx="28" cy="8" r="3" fill="#0f172a" stroke="${color}" stroke-width="1.5" />
            <circle cx="8" cy="28" r="3" fill="#0f172a" stroke="${color}" stroke-width="1.5" />
            <circle cx="28" cy="28" r="3" fill="#0f172a" stroke="${color}" stroke-width="1.5" />
          </svg>
        </div>
        ${label ? `<div class="drone-marker-label ${isSelected ? 'selected-label' : ''}" style="margin-top: -2px;">${label}</div>` : ''}
      </div>
    `;

    return L.divIcon({
      html: svgIcon,
      className: 'custom-drone-icon',
      iconSize: [36, 44],
      iconAnchor: [18, 18],
      popupAnchor: [0, -18],
    });
  }

  /**
   * Pilot / GCS Home location icon
   */
  createPilotIcon(label = 'PILOT / GCS') {
    const svgIcon = `
      <div class="pilot-marker-container" style="position: relative; display: flex; flex-direction: column; align-items: center; cursor: pointer; user-select: none;">
        <div class="pilot-pulse" style="position: absolute; width: 34px; height: 34px; top: 0; border-radius: 50%; background: rgba(16, 185, 129, 0.4); animation: pilotRadarPulse 2s infinite ease-out;"></div>
        <div class="pilot-marker-circle" style="position: relative; width: 34px; height: 34px; background: #080c16; border: 2.5px solid #10b981; border-radius: 50%; display: flex; align-items: center; justify-content: center; box-shadow: 0 0 16px rgba(16, 185, 129, 0.9);">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="#10b981" stroke="#ffffff" stroke-width="1.2">
            <circle cx="12" cy="7" r="4" />
            <path d="M5.5 21a6.5 6.5 0 0 1 13 0H5.5z" />
          </svg>
        </div>
        <div class="pilot-marker-badge" style="margin-top: 3px; background: rgba(8, 12, 22, 0.92); border: 1px solid #10b981; color: #34d399; font-family: 'JetBrains Mono', monospace; font-size: 10px; font-weight: 800; padding: 1px 6px; border-radius: 3px; white-space: nowrap; box-shadow: 0 2px 6px rgba(0,0,0,0.8); letter-spacing: 0.5px;">
          ${label}
        </div>
      </div>
    `;
    return L.divIcon({
      html: svgIcon,
      className: 'custom-pilot-icon',
      iconSize: [80, 56],
      iconAnchor: [40, 17],
      popupAnchor: [0, -20],
    });
  }

  /**
   * Updates full encounters list from feed or live stream, merging and preserving cached trajectories
   */
  setAllEncounters(encountersList) {
    if (!encountersList) return;
    
    // Merge new encounters into allEncounters while preserving any loaded trajectories
    const merged = encountersList.map(newEnc => {
      const existing = this.allEncounters.find(e => e.encounter_id === newEnc.encounter_id);
      if (existing) {
        const traj = (newEnc.trajectory && newEnc.trajectory.length > 0)
          ? newEnc.trajectory
          : (existing.trajectory || []);
        return Object.assign({}, existing, newEnc, { trajectory: traj });
      }
      return newEnc;
    });

    // Check if we need a full re-render
    let needsRerender = true;
    if (this.selectedEncounterId) {
      const oldSelected = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
      const newSelected = merged.find(e => e.encounter_id === this.selectedEncounterId);
      if (oldSelected && newSelected &&
          oldSelected.packet_count === newSelected.packet_count &&
          (oldSelected.trajectory || []).length === (newSelected.trajectory || []).length) {
        needsRerender = false;
      }
    }

    this.allEncounters = merged;
    if (needsRerender) {
      this.renderAirspace();
    }
  }

  /**
   * Selects a specific flight, highlighting its trajectory in neon cyan with discrete waypoints,
   * while rendering background sister tracks in muted grey.
   */
  async setSelectedEncounter(encounterId, fullEncounter = null) {
    if (this.selectedEncounterId !== encounterId) {
      this.selectedEncounterId = encounterId;
      this.activeFixIndex = null;
      this.activeFixPoint = null;
      this.isFixPopupOpen = false;
    }

    if (!encounterId) {
      this.renderAirspace();
      this.renderReceiverVector(null);
      return;
    }

    if (fullEncounter) {
      fullEncounter._detailsLoaded = true;
      const idx = this.allEncounters.findIndex(e => e.encounter_id === encounterId);
      if (idx >= 0) {
        this.allEncounters[idx] = Object.assign({}, this.allEncounters[idx], fullEncounter);
      } else {
        this.allEncounters.push(fullEncounter);
      }
    } else {
      // If full details aren't loaded yet for this encounter, fetch them once
      const existing = this.allEncounters.find(e => e.encounter_id === encounterId);
      if (!existing || !existing._detailsLoaded) {
        try {
          const resp = await fetch(`/api/encounters/${encounterId}`);
          if (resp.ok) {
            const data = await resp.json();
            data._detailsLoaded = true;
            const idx = this.allEncounters.findIndex(e => e.encounter_id === encounterId);
            if (idx >= 0) {
              this.allEncounters[idx] = Object.assign({}, this.allEncounters[idx], data);
            } else {
              this.allEncounters.push(data);
            }
          }
        } catch (e) {
          console.error('Failed to load details for selected encounter:', e);
        }
      }
    }

    this.renderAirspace();
    this.renderReceiverVector();

    // Fit map bounds / pan to selected flight trajectory or pilot location
    const selected = this.allEncounters.find(e => e.encounter_id === encounterId);
    if (selected) {
      const validPts = (selected.trajectory || []).filter(pt => Array.isArray(pt) && pt.length >= 2 && !isNaN(pt[0]) && !isNaN(pt[1]));
      if (validPts.length > 0) {
        const bounds = L.latLngBounds(validPts.map(pt => [pt[0], pt[1]]));
        if (selected.pilot_lat != null && selected.pilot_lon != null && !isNaN(selected.pilot_lat) && !isNaN(selected.pilot_lon)) {
          bounds.extend([selected.pilot_lat, selected.pilot_lon]);
        }
        this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
      } else if (selected.latest_position && selected.latest_position.lat != null && selected.latest_position.lon != null && !isNaN(selected.latest_position.lat) && !isNaN(selected.latest_position.lon)) {
        if (selected.pilot_lat != null && selected.pilot_lon != null && !isNaN(selected.pilot_lat) && !isNaN(selected.pilot_lon)) {
          const bounds = L.latLngBounds([
            [selected.latest_position.lat, selected.latest_position.lon],
            [selected.pilot_lat, selected.pilot_lon]
          ]);
          this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
        } else {
          this.map.flyTo([selected.latest_position.lat, selected.latest_position.lon], 16, { animate: true });
        }
      } else if (selected.pilot_lat != null && selected.pilot_lon != null && !isNaN(selected.pilot_lat) && !isNaN(selected.pilot_lon)) {
        this.map.flyTo([selected.pilot_lat, selected.pilot_lon], 16, { animate: true });
      }
    }
  }

  /**
   * Real-time update for live active drones from WebSocket
   */
  updateLiveDrone(drone) {
    if (!drone || !drone.encounter_id) return;
    const encId = drone.encounter_id;
    const idx = this.allEncounters.findIndex(e => e.encounter_id === encId);
    if (idx >= 0) {
      this.allEncounters[idx] = Object.assign({}, this.allEncounters[idx], drone);
    } else {
      this.allEncounters.unshift(drone);
    }

    if (this.selectedEncounterId === encId || !this.selectedEncounterId) {
      this.renderAirspace();
      if (this.selectedEncounterId === encId && drone.latest_position) {
        this.renderReceiverVector([drone.latest_position.lat, drone.latest_position.lon]);
      }
    }
  }

  /**
   * Main rendering routine
   */
  renderAirspace() {
    this.clearAllLayers();

    if (!this.allEncounters || this.allEncounters.length === 0) return;

    // If NO encounter is selected:
    if (!this.selectedEncounterId) {
      const hasActive = this.allEncounters.some(e => e.is_active);
      if (hasActive) {
        this.allEncounters.forEach(enc => {
          if (enc.is_active) {
            this.renderFlightTrack(enc, false);
          }
        });
      } else {
        this.allEncounters.slice(0, 15).forEach(enc => {
          this.renderFlightTrack(enc, false);
        });
      }
      return;
    }

    // When an encounter IS selected:
    const selectedEnc = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
    if (!selectedEnc) return;

    const selectedDroneKey = selectedEnc.serial_number || selectedEnc.mac;

    const sameDroneEncounters = this.allEncounters.filter(e => {
      if (e.encounter_id === this.selectedEncounterId) return false;
      const key = e.serial_number || e.mac;
      return Boolean(key && selectedDroneKey && key === selectedDroneKey);
    });

    // 1. Render other flights by the SAME drone in muted slate grey (#475569)
    sameDroneEncounters.forEach(async (enc) => {
      if (!enc._detailsLoaded && (!enc.trajectory || enc.trajectory.length === 0)) {
        enc._detailsLoaded = true;
        try {
          const r = await fetch(`/api/encounters/${enc.encounter_id}`);
          if (r.ok) {
            const data = await r.json();
            data._detailsLoaded = true;
            enc.trajectory = data.trajectory || [];
            Object.assign(enc, data);
            if (this.selectedEncounterId === selectedEnc.encounter_id) {
              this.renderFlightTrack(enc, false);
            }
          }
        } catch (e) {}
      } else {
        this.renderFlightTrack(enc, false);
      }
    });

    // 2. Render selected flight on top in glowing neon cyan (#00f0ff) with discrete waypoints
    this.renderFlightTrack(selectedEnc, true);
  }

  /**
   * Renders an individual flight's track, discrete waypoint dots, pilot marker, and aircraft icon
   */
  renderFlightTrack(encounter, isSelected = false) {
    const encId = encounter.encounter_id;
    const rawTraj = encounter.trajectory || [];
    const traj = rawTraj.filter(pt => Array.isArray(pt) && pt.length >= 2 && !isNaN(pt[0]) && !isNaN(pt[1]));
    const displayLabel = encounter.serial_number ? encounter.serial_number.slice(-6) : (encounter.mac ? encounter.mac.slice(-5) : '');

    const latlngs = traj.map(pt => [pt[0], pt[1]]);

    // 1. Trajectory Polyline Trail
    if (latlngs.length >= 1) {
      let polyline;
      if (isSelected) {
        polyline = L.polyline(latlngs, {
          color: '#00f0ff',
          weight: 4.5,
          opacity: 1.0,
          className: 'selected-flight-trail',
        });
      } else {
        polyline = L.polyline(latlngs, {
          color: '#475569',
          weight: 2.2,
          opacity: 0.55,
          dashArray: '4, 4',
          className: 'background-flight-trail',
        });
      }

      if (this.showTrails) {
        polyline.addTo(this.map);
      }

      polyline.on('click', () => {
        if (this.onSelectEncounter) this.onSelectEncounter(encId);
      });

      this.trackLayers.set(encId, polyline);
    }

    // 2. Discrete Waypoint Fix Dots (for selected encounter)
    if (isSelected && traj.length > 0) {
      const waypointGroup = L.layerGroup();
      this.activeWaypointMarkers = [];

      traj.forEach((pt, idx) => {
        const ptLatLng = [pt[0], pt[1]];
        const alt = pt[2] != null ? `${pt[2].toFixed(1)}m MSL` : 'N/A';
        const repHeight = (pt.length > 6 && pt[6] != null) ? pt[6] : null;
        const heightType = (pt.length > 7 && pt[7] != null) ? pt[7] : 0;
        const typeLabel = heightType === 1 ? 'AGL' : 'Above Takeoff';
        const pressAlt = (pt.length > 8 && pt[8] != null) ? `${pt[8].toFixed(1)}m` : null;
        const vertSpd = (pt.length > 9 && pt[9] != null) ? `${pt[9] >= 0 ? '+' : ''}${pt[9].toFixed(1)} m/s` : null;
        
        let heightStr = '';
        if (repHeight != null) {
          const h = Math.round(repHeight);
          heightStr += `<br/><b>Height (Reported):</b> ${h >= 0 ? '+' + h : h}m <span style="font-size:10px; color:#0284c7;">(${typeLabel})</span>`;
        }
        if (pt[2] != null && encounter.pilot_alt_m != null) {
          const hCalc = Math.round(pt[2] - encounter.pilot_alt_m);
          heightStr += `<br/><b>Height (Calc ATO):</b> ${hCalc >= 0 ? '+' + hCalc : hCalc}m`;
        }

        const pressAltStr = pressAlt ? `<br/><b>Pressure Alt:</b> ${pressAlt}` : '';
        const vertSpdStr = vertSpd ? `<br/><b>Vertical Speed:</b> ${vertSpd}` : '';

        // Calculate distance from receiving sensor node if registered
        let rxPopupHtml = '';
        let rxTooltipText = '';
        let rxNode = null;
        if (encounter.node_id && this.nodesList) {
          rxNode = this.nodesList.find(n => n.node_id === encounter.node_id);
        }
        if (rxNode && rxNode.latitude != null && rxNode.longitude != null) {
          const rxLat = parseFloat(rxNode.latitude);
          const rxLon = parseFloat(rxNode.longitude);
          const rxAlt = parseFloat(rxNode.altitude_m || 0);
          const groundDistM = calculateHaversineDistanceM(rxLat, rxLon, pt[0], pt[1]);
          const brg = calculateBearingDeg(rxLat, rxLon, pt[0], pt[1]);
          const compass = getBearingCompass(brg);
          
          let slantRangeM = groundDistM;
          let deltaAltStr = '';
          if (pt[2] != null) {
            const dAlt = pt[2] - rxAlt;
            slantRangeM = Math.sqrt(groundDistM ** 2 + dAlt ** 2);
            deltaAltStr = ` (Δh: ${dAlt >= 0 ? '+' : ''}${Math.round(dAlt)}m)`;
          }

          const slantStr = slantRangeM >= 1000 ? `${(slantRangeM / 1000).toFixed(2)} km` : `${Math.round(slantRangeM)} m`;
          const groundStr = groundDistM >= 1000 ? `${(groundDistM / 1000).toFixed(2)} km` : `${Math.round(groundDistM)} m`;

          rxPopupHtml = `
            <div style="background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.35); border-radius: 4px; padding: 4px 6px; margin: 4px 0;">
              <div style="font-weight: 700; color: #d97706; display: flex; justify-content: space-between;">
                <span>📡 Sensor (${rxNode.name || rxNode.node_id}):</span>
                <span style="color: #b45309; font-size: 10px;">${Math.round(brg)}° ${compass}</span>
              </div>
              <b>Slant Range:</b> <span style="color: #d97706; font-weight: 700;">${slantStr}</span>${deltaAltStr}<br/>
              <b>Ground Dist:</b> ${groundStr}
            </div>
          `;
          rxTooltipText = ` • Rx: ${slantStr} (${Math.round(brg)}° ${compass})`;
        }

        const speed = pt[3] != null ? `${pt[3].toFixed(1)} m/s` : 'N/A';
        const heading = pt[4] != null ? `${pt[4]}°` : 'N/A';
        const timeStr = pt[5] ? new Date(pt[5] * 1000).toISOString().substr(11, 8) : '';

        const dotIcon = L.divIcon({
          html: `<div class="waypoint-dot-marker" id="waypoint-dot-${idx}" title="Packet Fix #${idx + 1}"></div>`,
          className: 'custom-waypoint-dot',
          iconSize: [12, 12],
          iconAnchor: [6, 6],
        });

        const dotMarker = L.marker(ptLatLng, { icon: dotIcon, zIndexOffset: 1000 });
        dotMarker.bindPopup(`
          <div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px; color: #080c16; line-height: 1.5; min-width: 200px;">
            <div style="font-weight: 800; color: #0284c7; border-bottom: 1px solid #e2e8f0; padding-bottom: 2px; margin-bottom: 4px; display: flex; justify-content: space-between;">
              <span>Fix #${idx + 1} of ${traj.length}</span>
              ${timeStr ? `<span style="font-size: 10px; color: #64748b;">${timeStr}</span>` : ''}
            </div>
            <b>Altitude (MSL):</b> ${alt}${pressAltStr}${heightStr}${vertSpdStr}<br/>
            ${rxPopupHtml}
            <b>Speed:</b> ${speed}<br/>
            <b>Track:</b> ${heading}<br/>
            <b>Coords:</b> ${pt[0].toFixed(5)}, ${pt[1].toFixed(5)}
          </div>
        `, {
          autoPan: false,
        });

        const hTooltip = repHeight != null ? ` • H: ${repHeight >= 0 ? '+' : ''}${Math.round(repHeight)}m (${heightType === 1 ? 'AGL' : 'ATO'})` : '';
        dotMarker.bindTooltip(`Fix #${idx + 1} • Alt: ${alt}${hTooltip} • Spd: ${speed}${rxTooltipText}`, {
          className: 'waypoint-leaflet-tooltip',
          direction: 'top',
          offset: [0, -6],
        });

        dotMarker.on('click', () => {
          this.activeFixIndex = idx;
          this.activeFixPoint = pt;
          this.isFixPopupOpen = true;
          if (this.onSelectWaypoint) {
            this.onSelectWaypoint(idx, pt);
          }
          this.setScrubberFix(encId, idx, pt);
        });

        dotMarker.on('popupopen', () => {
          this.activeFixIndex = idx;
          this.activeFixPoint = pt;
          this.isFixPopupOpen = true;
          document.querySelectorAll('.waypoint-dot-marker').forEach(el => el.classList.remove('active-fix-dot'));
          const dotEl = document.getElementById(`waypoint-dot-${idx}`);
          if (dotEl) dotEl.classList.add('active-fix-dot');
        });

        dotMarker.on('popupclose', () => {
          if (this.activeFixIndex === idx) {
            this.isFixPopupOpen = false;
          }
        });

        this.activeWaypointMarkers.push(dotMarker);
        waypointGroup.addLayer(dotMarker);
      });

      if (this.showWaypoints) {
        waypointGroup.addTo(this.map);
      }
      this.waypointLayers.set(encId, waypointGroup);

      // Restore active fix marker highlight and popup state across re-renders
      if (this.activeFixIndex != null && this.activeWaypointMarkers[this.activeFixIndex]) {
        const dotEl = document.getElementById(`waypoint-dot-${this.activeFixIndex}`);
        if (dotEl) dotEl.classList.add('active-fix-dot');
        if (this.isFixPopupOpen) {
          setTimeout(() => {
            if (this.activeWaypointMarkers[this.activeFixIndex]) {
              this.activeWaypointMarkers[this.activeFixIndex].openPopup();
            }
          }, 10);
        }
      }
    }

    // 3. Pilot / GCS Home Location & Link Line (rendered for selected flight or active flights with pilot coordinates)
    if (encounter.pilot_lat != null && encounter.pilot_lon != null && !isNaN(encounter.pilot_lat) && !isNaN(encounter.pilot_lon)) {
      if (isSelected || encounter.is_active) {
        const pilotLatLng = [encounter.pilot_lat, encounter.pilot_lon];
        const pilotMarker = L.marker(pilotLatLng, {
          icon: this.createPilotIcon('PILOT / GCS'),
          zIndexOffset: isSelected ? 3000 : 1500,
        }).bindPopup(`
          <div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 6px; color: #080c16; min-width: 190px;">
            <div style="font-weight: 800; color: #059669; border-bottom: 1px solid #e2e8f0; padding-bottom: 3px; margin-bottom: 5px;">
              🎯 Pilot / GCS Home Location
            </div>
            <b>Aircraft:</b> ${displayLabel || encounter.encounter_id}<br/>
            <b>Latitude:</b> ${encounter.pilot_lat.toFixed(6)}°<br/>
            <b>Longitude:</b> ${encounter.pilot_lon.toFixed(6)}°<br/>
            <b>Altitude:</b> ${encounter.pilot_alt_m != null ? encounter.pilot_alt_m.toFixed(1) + 'm MSL' : 'N/A'}
          </div>
        `);
        pilotMarker.bindTooltip(`<b>Pilot / GCS Location</b><br/>${encounter.pilot_lat.toFixed(5)}, ${encounter.pilot_lon.toFixed(5)}`, {
          direction: 'top',
          offset: [0, -18],
          className: 'waypoint-leaflet-tooltip'
        });
        pilotMarker.addTo(this.map);
        this.pilotMarkers.set(encId, pilotMarker);

        // Find best anchor position for the link line (first track point or latest position)
        let droneAnchor = null;
        if (latlngs.length > 0) {
          droneAnchor = latlngs[0];
        } else if (encounter.latest_position && encounter.latest_position.lat != null && encounter.latest_position.lon != null && !isNaN(encounter.latest_position.lat) && !isNaN(encounter.latest_position.lon)) {
          droneAnchor = [encounter.latest_position.lat, encounter.latest_position.lon];
        }

        if (droneAnchor) {
          const pilotLine = L.polyline([pilotLatLng, droneAnchor], {
            color: '#10b981',
            weight: 1.5,
            dashArray: '4, 4',
            opacity: 0.7,
          }).addTo(this.map);
          this.pilotLines.set(encId, pilotLine);
        }
      }
    }

    // 4. Aircraft Marker (either latest trajectory point or latest_position)
    let dronePos = null;
    let droneHeading = 0;
    if (traj.length > 0) {
      const latestPt = traj[traj.length - 1];
      dronePos = [latestPt[0], latestPt[1]];
      droneHeading = latestPt[4] || 0;
    } else if (encounter.latest_position && encounter.latest_position.lat != null && encounter.latest_position.lon != null && !isNaN(encounter.latest_position.lat) && !isNaN(encounter.latest_position.lon)) {
      dronePos = [encounter.latest_position.lat, encounter.latest_position.lon];
      droneHeading = encounter.latest_position.heading_deg || 0;
    }

    if (dronePos) {
      const marker = L.marker(dronePos, {
        icon: this.createDroneIcon(encId, droneHeading, isSelected, encounter.is_active, displayLabel),
        zIndexOffset: isSelected ? 2500 : 300,
      }).addTo(this.map);

      const modelTag = (encounter.drone_make || encounter.drone_model) ? ` · ${[encounter.drone_make, encounter.drone_model].filter(Boolean).join(' ')}` : '';
      marker.bindTooltip(`<b>${displayLabel || encId}</b>${modelTag}`, {
        direction: 'top',
        offset: [0, -12],
        className: 'drone-map-tooltip'
      });

      marker.on('click', () => {
        if (this.onSelectEncounter) this.onSelectEncounter(encId);
      });

      this.droneMarkers.set(encId, marker);
    }
  }

  /**
   * Highlights a specific waypoint fix when scrubbing the timeline
   */
  setScrubberFix(encounterId, ptIndex, pt) {
    if (!pt) return;
    this.activeFixIndex = ptIndex;
    this.activeFixPoint = pt;
    this.isFixPopupOpen = true;

    const latlng = [pt[0], pt[1]];
    const heading = pt[4] || 0;

    const enc = this.allEncounters.find(e => e.encounter_id === encounterId);
    const displayLabel = enc && enc.serial_number ? enc.serial_number.slice(-6) : '';

    if (this.droneMarkers.has(encounterId)) {
      const marker = this.droneMarkers.get(encounterId);
      marker.setLatLng(latlng);
      marker.setIcon(this.createDroneIcon(encounterId, heading, true, true, displayLabel));
    }

    // Update receiver vector to current scrubbed fix
    this.renderReceiverVector(latlng);

    document.querySelectorAll('.waypoint-dot-marker').forEach(el => el.classList.remove('active-fix-dot'));
    const activeDotEl = document.getElementById(`waypoint-dot-${ptIndex}`);
    if (activeDotEl) {
      activeDotEl.classList.add('active-fix-dot');
    }

    if (this.activeWaypointMarkers && this.activeWaypointMarkers[ptIndex]) {
      this.activeWaypointMarkers[ptIndex].openPopup();
    }
  }

  clearAllLayers() {
    this.trackLayers.forEach(l => this.map.removeLayer(l));
    this.trackLayers.clear();
    this.droneMarkers.forEach(m => this.map.removeLayer(m));
    this.droneMarkers.clear();
    this.waypointLayers.forEach(w => this.map.removeLayer(w));
    this.waypointLayers.clear();
    this.pilotMarkers.forEach(p => this.map.removeLayer(p));
    this.pilotMarkers.clear();
    this.pilotLines.forEach(pl => this.map.removeLayer(pl));
    this.pilotLines.clear();
    this.activeWaypointMarkers = [];
    if (this.receiverVectorLine) {
      this.map.removeLayer(this.receiverVectorLine);
      this.receiverVectorLine = null;
    }
  }

  toggleTrails(show) {
    this.showTrails = show;
    this.trackLayers.forEach(trail => {
      if (show) trail.addTo(this.map);
      else this.map.removeLayer(trail);
    });
  }

  toggleWaypoints(show) {
    this.showWaypoints = show;
    this.waypointLayers.forEach(layer => {
      if (show) layer.addTo(this.map);
      else this.map.removeLayer(layer);
    });
  }

  fitAirspace() {
    const bounds = L.latLngBounds([]);
    let count = 0;

    // Include registered sensor nodes in bounds if present
    if (this.nodesList && this.nodesList.length > 0) {
      this.nodesList.forEach(node => {
        const lat = parseFloat(node.latitude);
        const lon = parseFloat(node.longitude);
        if (!isNaN(lat) && !isNaN(lon)) {
          bounds.extend([lat, lon]);
          count++;
        }
      });
    }

    if (this.selectedEncounterId) {
      const selectedEnc = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
      const selectedDroneKey = selectedEnc ? (selectedEnc.serial_number || selectedEnc.mac) : null;
      const relevant = this.allEncounters.filter(e => {
        if (e.encounter_id === this.selectedEncounterId) return true;
        const k = e.serial_number || e.mac;
        return Boolean(k && selectedDroneKey && k === selectedDroneKey);
      });

      relevant.forEach(enc => {
        (enc.trajectory || []).forEach(pt => {
          if (Array.isArray(pt) && pt.length >= 2 && !isNaN(pt[0]) && !isNaN(pt[1])) {
            bounds.extend([pt[0], pt[1]]);
            count++;
          }
        });
        if (enc.latest_position && enc.latest_position.lat != null && enc.latest_position.lon != null && !isNaN(enc.latest_position.lat) && !isNaN(enc.latest_position.lon)) {
          bounds.extend([enc.latest_position.lat, enc.latest_position.lon]);
          count++;
        }
        if (enc.pilot_lat != null && enc.pilot_lon != null && !isNaN(enc.pilot_lat) && !isNaN(enc.pilot_lon)) {
          bounds.extend([enc.pilot_lat, enc.pilot_lon]);
          count++;
        }
      });
    } else {
      this.allEncounters.forEach(enc => {
        if (enc.latest_position && !isNaN(enc.latest_position.lat) && !isNaN(enc.latest_position.lon)) {
          bounds.extend([enc.latest_position.lat, enc.latest_position.lon]);
          count++;
        }
        if (enc.pilot_lat != null && enc.pilot_lon != null && !isNaN(enc.pilot_lat) && !isNaN(enc.pilot_lon)) {
          bounds.extend([enc.pilot_lat, enc.pilot_lon]);
          count++;
        }
      });
    }

    if (count > 0) {
      this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 16 });
    }
  }

  centerOnOperator() {
    let targetEnc = null;
    if (this.selectedEncounterId) {
      targetEnc = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
    }
    
    if (!targetEnc || targetEnc.pilot_lat == null || targetEnc.pilot_lon == null) {
      targetEnc = this.allEncounters.find(e => e.pilot_lat != null && e.pilot_lon != null && !isNaN(e.pilot_lat) && !isNaN(e.pilot_lon));
    }

    if (targetEnc && targetEnc.pilot_lat != null && targetEnc.pilot_lon != null && !isNaN(targetEnc.pilot_lat) && !isNaN(targetEnc.pilot_lon)) {
      this.map.flyTo([targetEnc.pilot_lat, targetEnc.pilot_lon], 17, {
        animate: true,
        duration: 0.8,
      });

      const marker = this.pilotMarkers.get(targetEnc.encounter_id);
      if (marker) {
        setTimeout(() => marker.openPopup(), 400);
      }
    } else {
      const coordEl = document.getElementById('map-cursor-coords');
      if (coordEl) {
        const prev = coordEl.textContent;
        coordEl.textContent = 'NO GCS / PILOT COORDINATES RECORDED';
        setTimeout(() => { if (coordEl) coordEl.textContent = prev; }, 2500);
      }
    }
  }
}
