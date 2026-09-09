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
  constructor(containerId, onSelectEncounter, onSelectWaypoint, onUpdateReceiverLocation, onOpenReceiverConfig) {
    this.containerId = containerId;
    this.onSelectEncounter = onSelectEncounter;
    this.onSelectWaypoint = onSelectWaypoint;
    this.onUpdateReceiverLocation = onUpdateReceiverLocation;
    this.onOpenReceiverConfig = onOpenReceiverConfig;
    
    this.map = null;
    this.allEncounters = [];
    this.selectedEncounterId = null;
    this.receiverConfig = null;

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

    // Receiver layers
    this.receiverMarker = null;
    this.rangeRingLayers = L.layerGroup();
    this.receiverVectorLine = null;

    this.showTrails = true;
    this.showWaypoints = true;
    this.showRangeRings = true;
    this.pickLocationMode = false;

    this.initMap();
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

    // Track mouse coordinates for HUD overlay & handle Map Pick mode
    const coordEl = document.getElementById('map-cursor-coords');
    this.map.on('mousemove', (e) => {
      if (coordEl) {
        const lat = e.latlng.lat.toFixed(5);
        const lon = e.latlng.lng.toFixed(5);
        coordEl.textContent = `${lat}° N, ${lon}° E`;
      }
    });

    this.map.on('click', (e) => {
      if (this.pickLocationMode) {
        this.pickLocationMode = false;
        document.body.classList.remove('map-picking-cursor');
        if (this.onUpdateReceiverLocation) {
          const updated = Object.assign({}, this.receiverConfig || {}, {
            latitude: parseFloat(e.latlng.lat.toFixed(6)),
            longitude: parseFloat(e.latlng.lng.toFixed(6)),
          });
          this.setReceiverConfig(updated);
          this.onUpdateReceiverLocation(updated);
        }
      }
    });
  }

  enableMapPickMode() {
    if (this.receiverConfig && this.receiverConfig.locked) {
      alert("Receiver location is locked in receiver_config.json on disk. Edit the config file on disk ('locked': false) to enable repositioning.");
      return;
    }
    this.pickLocationMode = true;
    document.body.classList.add('map-picking-cursor');
    const coordEl = document.getElementById('map-cursor-coords');
    if (coordEl) {
      coordEl.textContent = '👉 CLICK ANYWHERE ON MAP TO SET RECEIVER LOCATION';
    }
  }

  /**
   * Configures and renders the receiver sensor station
   */
  setReceiverConfig(config) {
    if (!config) return;
    this.receiverConfig = Object.assign({}, this.receiverConfig || {}, config);
    this.showRangeRings = this.receiverConfig.show_range_rings !== false;
    this.renderReceiverStation();
  }

  createReceiverIcon(name = 'Sensor Station', isLocked = false) {
    const lockBadge = isLocked ? `
      <div style="position: absolute; top: -3px; right: -3px; background: #ef4444; border: 1.5px solid #ffffff; border-radius: 50%; width: 14px; height: 14px; display: flex; align-items: center; justify-content: center; font-size: 8px; box-shadow: 0 0 6px rgba(239, 68, 68, 0.8);">
        🔒
      </div>
    ` : '';

    const labelText = isLocked ? `🔒 ${name}` : name;

    const svgIcon = `
      <div class="receiver-marker-container" style="position: relative; display: flex; flex-direction: column; align-items: center;">
        <div class="receiver-marker-icon" style="width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; position: relative;">
          <svg viewBox="0 0 36 36" width="34" height="34" fill="none">
            <!-- Outer beacon wave -->
            <circle cx="18" cy="18" r="16" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.8" class="rx-pulse-ring" />
            <!-- Inner radar base -->
            <circle cx="18" cy="18" r="7" fill="#f59e0b" stroke="#ffffff" stroke-width="1.5" />
            <!-- Antenna tower mast -->
            <line x1="18" y1="4" x2="18" y2="18" stroke="#ffffff" stroke-width="2" stroke-linecap="round" />
            <polygon points="18,3 15,9 21,9" fill="#ffffff" />
            <!-- Dish curves -->
            <path d="M12 11 A 8 8 0 0 1 24 11" stroke="#ffffff" stroke-width="1.5" fill="none" stroke-linecap="round"/>
          </svg>
          ${lockBadge}
        </div>
        <div class="receiver-marker-label" style="margin-top: -2px;">${labelText}</div>
      </div>
    `;

    return L.divIcon({
      html: svgIcon,
      className: 'custom-receiver-icon',
      iconSize: [36, 46],
      iconAnchor: [18, 18],
      popupAnchor: [0, -18],
    });
  }

  renderReceiverStation() {
    if (!this.receiverConfig || !this.receiverConfig.enabled) {
      if (this.receiverMarker) {
        this.map.removeLayer(this.receiverMarker);
        this.receiverMarker = null;
      }
      this.rangeRingLayers.clearLayers();
      if (this.receiverVectorLine) {
        this.map.removeLayer(this.receiverVectorLine);
        this.receiverVectorLine = null;
      }
      return;
    }

    const isLocked = Boolean(this.receiverConfig && this.receiverConfig.locked);
    const lat = this.receiverConfig.latitude;
    const lon = this.receiverConfig.longitude;
    if (lat == null || lon == null || isNaN(lat) || isNaN(lon)) return;

    const latlng = [lat, lon];
    const name = this.receiverConfig.name || 'Tactical Sensor Station';
    const alt = this.receiverConfig.altitude_m != null ? `${this.receiverConfig.altitude_m.toFixed(1)}m MSL` : 'N/A';

    // 1. Update or create receiver marker
    if (!this.receiverMarker) {
      this.receiverMarker = L.marker(latlng, {
        icon: this.createReceiverIcon(name, isLocked),
        draggable: !isLocked,
        zIndexOffset: 3000,
      }).addTo(this.map);

      this.receiverMarker.on('dragend', (e) => {
        if (this.receiverConfig && this.receiverConfig.locked) return;
        const newPos = e.target.getLatLng();
        this.receiverConfig.latitude = parseFloat(newPos.lat.toFixed(6));
        this.receiverConfig.longitude = parseFloat(newPos.lng.toFixed(6));
        this.renderRangeRings();
        this.renderReceiverVector();
        if (this.onUpdateReceiverLocation) {
          this.onUpdateReceiverLocation(this.receiverConfig);
        }
      });
    } else {
      this.receiverMarker.setLatLng(latlng);
      this.receiverMarker.setIcon(this.createReceiverIcon(name, isLocked));
      if (this.receiverMarker.dragging) {
        if (isLocked) {
          this.receiverMarker.dragging.disable();
        } else {
          this.receiverMarker.dragging.enable();
        }
      }
    }

    const lockStatusHtml = isLocked
      ? `<span style="font-size: 10px; color: #f87171; font-weight: 700; background: rgba(239, 68, 68, 0.15); padding: 2px 6px; border-radius: 3px; border: 1px solid rgba(239, 68, 68, 0.3);">🔒 Locked via receiver_config.json</span>`
      : `<span style="font-size: 10px; color: #64748b;">Drag to reposition</span>`;

    this.receiverMarker.bindPopup(`
      <div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 6px; color: #080c16; line-height: 1.5; min-width: 210px;">
        <div style="font-weight: 800; color: #d97706; margin-bottom: 4px; display: flex; align-items: center; justify-content: space-between; gap: 4px;">
          <span>📡 ${name}</span>
          ${isLocked ? '<span style="font-size: 9px; background: #ef4444; color: #fff; padding: 1px 4px; border-radius: 3px;">LOCKED</span>' : ''}
        </div>
        <b>Role:</b> Drone Remote ID Receiver Station<br/>
        <b>Latitude:</b> ${lat.toFixed(6)}° N<br/>
        <b>Longitude:</b> ${lon.toFixed(6)}° E<br/>
        <b>Altitude:</b> ${alt}<br/>
        <b>Position Lock:</b> ${isLocked ? '<span style="color: #dc2626; font-weight: 700;">Locked (On Disk)</span>' : '<span style="color: #16a34a; font-weight: 700;">Unlocked</span>'}<br/>
        <div style="margin-top: 8px; padding-top: 6px; border-top: 1px solid #e2e8f0; display: flex; justify-content: space-between; align-items: center;">
          ${lockStatusHtml}
          <button id="popup-btn-edit-rx" style="background: #f59e0b; color: #ffffff; border: none; border-radius: 3px; font-size: 10px; font-weight: 700; padding: 2px 6px; cursor: pointer;">Config</button>
        </div>
      </div>
    `);

    this.receiverMarker.on('popupopen', () => {
      const btn = document.getElementById('popup-btn-edit-rx');
      if (btn && this.onOpenReceiverConfig) {
        btn.onclick = () => this.onOpenReceiverConfig();
      }
    });

    // 2. Render range rings
    this.renderRangeRings();

    // 3. Update receiver vector if target selected
    this.renderReceiverVector();
  }

  renderRangeRings() {
    this.rangeRingLayers.clearLayers();
    if (!this.showRangeRings || !this.receiverConfig || !this.receiverConfig.enabled) return;

    const lat = this.receiverConfig.latitude;
    const lon = this.receiverConfig.longitude;
    if (lat == null || lon == null) return;

    const rxName = this.receiverConfig.name || 'Receiver Station';
    const rings = this.receiverConfig.range_rings_m || [500, 1000, 2500, 5000];

    rings.forEach((radiusM, idx) => {
      const radiusKm = radiusM >= 1000 ? `${(radiusM / 1000).toFixed(1)} km` : `${radiusM} m`;
      
      // 1. Circle Polygon (non-interactive visual radar overlay)
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

      this.rangeRingLayers.addLayer(ring);

      // 2. Clear Cardinal Distance Label on North Perimeter of Circle
      const labelLat = lat + (radiusM / 111320.0);
      const labelMarker = L.marker([labelLat, lon], {
        icon: L.divIcon({
          html: `<div class="range-ring-pill font-mono">⭕ ${radiusKm}</div>`,
          className: 'custom-ring-label-wrapper',
          iconSize: [64, 20],
          iconAnchor: [32, 10],
        }),
        interactive: false,
        zIndexOffset: 600,
      });

      this.rangeRingLayers.addLayer(labelMarker);
    });

    if (!this.map.hasLayer(this.rangeRingLayers)) {
      this.rangeRingLayers.addTo(this.map);
    }
  }

  toggleRangeRings(show) {
    this.showRangeRings = show;
    if (show) {
      this.renderRangeRings();
    } else {
      this.rangeRingLayers.clearLayers();
    }
  }

  centerOnReceiver() {
    if (!this.receiverConfig || this.receiverConfig.latitude == null) return;
    this.map.flyTo([this.receiverConfig.latitude, this.receiverConfig.longitude], 16, {
      animate: true,
      duration: 0.8,
    });
    if (this.receiverMarker) {
      setTimeout(() => this.receiverMarker.openPopup(), 400);
    }
  }

  /**
   * Draws a dashed vector line between receiver and currently selected target aircraft
   */
  renderReceiverVector(targetLatLng = null) {
    if (this.receiverVectorLine) {
      this.map.removeLayer(this.receiverVectorLine);
      this.receiverVectorLine = null;
    }

    if (!this.receiverConfig || !this.receiverConfig.enabled) return;
    const rxLat = this.receiverConfig.latitude;
    const rxLon = this.receiverConfig.longitude;
    if (rxLat == null || rxLon == null) return;

    let target = targetLatLng;
    if (!target && this.selectedEncounterId) {
      const enc = this.allEncounters.find(e => e.encounter_id === this.selectedEncounterId);
      if (enc) {
        if (enc.trajectory && enc.trajectory.length > 0) {
          const last = enc.trajectory[enc.trajectory.length - 1];
          target = [last[0], last[1]];
        } else if (enc.latest_position) {
          target = [enc.latest_position.lat, enc.latest_position.lon];
        }
      }
    }

    if (target && target.length >= 2 && !isNaN(target[0]) && !isNaN(target[1])) {
      const distM = calculateHaversineDistanceM(rxLat, rxLon, target[0], target[1]);
      const bearing = calculateBearingDeg(rxLat, rxLon, target[0], target[1]);
      const distStr = distM >= 1000 ? `${(distM / 1000).toFixed(2)} km` : `${Math.round(distM)} m`;
      const compass = getBearingCompass(bearing);

      this.receiverVectorLine = L.polyline([[rxLat, rxLon], target], {
        color: '#f59e0b',
        weight: 1.5,
        dashArray: '3, 5',
        opacity: 0.65,
        className: 'rx-vector-line',
      }).bindTooltip(`Range: ${distStr} · ${Math.round(bearing)}° ${compass}`, {
        sticky: true,
        className: 'custom-range-tooltip',
      });

      this.receiverVectorLine.addTo(this.map);
    }
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
  createPilotIcon() {
    const svgIcon = `
      <div class="pilot-marker-icon" style="width: 24px; height: 24px;">
        <svg viewBox="0 0 24 24" width="24" height="24" fill="#10b981" stroke="#ffffff" stroke-width="1.5">
          <circle cx="12" cy="7" r="4" />
          <path d="M5.5 21a6.5 6.5 0 0 1 13 0H5.5z" />
        </svg>
      </div>
    `;
    return L.divIcon({
      html: svgIcon,
      className: 'custom-pilot-icon',
      iconSize: [24, 24],
      iconAnchor: [12, 12],
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
      const idx = this.allEncounters.findIndex(e => e.encounter_id === encounterId);
      if (idx >= 0) {
        this.allEncounters[idx] = Object.assign({}, this.allEncounters[idx], fullEncounter);
      } else {
        this.allEncounters.push(fullEncounter);
      }
    } else {
      // If full trajectory isn't loaded yet for this encounter, fetch it right away
      const existing = this.allEncounters.find(e => e.encounter_id === encounterId);
      if (!existing || !existing.trajectory || existing.trajectory.length === 0) {
        try {
          const resp = await fetch(`/api/encounters/${encounterId}`);
          if (resp.ok) {
            const data = await resp.json();
            const idx = this.allEncounters.findIndex(e => e.encounter_id === encounterId);
            if (idx >= 0) {
              this.allEncounters[idx] = Object.assign({}, this.allEncounters[idx], data);
            } else {
              this.allEncounters.push(data);
            }
          }
        } catch (e) {
          console.error('Failed to load trajectory for selected encounter:', e);
        }
      }
    }

    this.renderAirspace();
    this.renderReceiverVector();

    // Fit map bounds to selected flight trajectory
    const selected = this.allEncounters.find(e => e.encounter_id === encounterId);
    if (selected && selected.trajectory && selected.trajectory.length > 0) {
      const validPts = selected.trajectory.filter(pt => Array.isArray(pt) && pt.length >= 2 && !isNaN(pt[0]) && !isNaN(pt[1]));
      if (validPts.length > 0) {
        const bounds = L.latLngBounds(validPts.map(pt => [pt[0], pt[1]]));
        this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
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

    // Re-render receiver station
    this.renderReceiverStation();

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

    if (!selectedEnc.trajectory || selectedEnc.trajectory.length === 0) {
      fetch(`/api/encounters/${selectedEnc.encounter_id}`)
        .then(r => r.json())
        .then(data => {
          if (this.selectedEncounterId === selectedEnc.encounter_id) {
            selectedEnc.trajectory = data.trajectory || [];
            Object.assign(selectedEnc, data);
            this.renderAirspace();
            const validPts = selectedEnc.trajectory.filter(pt => Array.isArray(pt) && pt.length >= 2);
            if (validPts.length > 0) {
              const bounds = L.latLngBounds(validPts.map(pt => [pt[0], pt[1]]));
              this.map.fitBounds(bounds, { padding: [60, 60], maxZoom: 17 });
            }
          }
        })
        .catch(err => console.error('Error fetching selected flight trajectory:', err));
    }

    const selectedDroneKey = selectedEnc.serial_number || selectedEnc.mac;

    const sameDroneEncounters = this.allEncounters.filter(e => {
      if (e.encounter_id === this.selectedEncounterId) return false;
      const key = e.serial_number || e.mac;
      return Boolean(key && selectedDroneKey && key === selectedDroneKey);
    });

    // 1. Render other flights by the SAME drone in muted slate grey (#475569)
    sameDroneEncounters.forEach(async (enc) => {
      if (!enc.trajectory || enc.trajectory.length === 0) {
        try {
          const r = await fetch(`/api/encounters/${enc.encounter_id}`);
          if (r.ok) {
            const data = await r.json();
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

    if (traj.length === 0) {
      const pos = encounter.latest_position;
      if (pos && pos.lat != null && pos.lon != null && !isNaN(pos.lat) && !isNaN(pos.lon)) {
        const marker = L.marker([pos.lat, pos.lon], {
          icon: this.createDroneIcon(encId, pos.heading_deg || 0, isSelected, encounter.is_active, displayLabel),
          zIndexOffset: isSelected ? 2000 : 300,
        }).addTo(this.map);

        marker.on('click', () => {
          if (this.onSelectEncounter) this.onSelectEncounter(encId);
        });
        this.droneMarkers.set(encId, marker);
      }
      return;
    }

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
    if (isSelected) {
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

        // Calculate distance from receiver if receiver configured
        let rxPopupHtml = '';
        let rxTooltipText = '';
        if (this.receiverConfig && this.receiverConfig.enabled && this.receiverConfig.latitude != null) {
          const groundDistM = calculateHaversineDistanceM(this.receiverConfig.latitude, this.receiverConfig.longitude, pt[0], pt[1]);
          const brg = calculateBearingDeg(this.receiverConfig.latitude, this.receiverConfig.longitude, pt[0], pt[1]);
          const compass = getBearingCompass(brg);
          
          let slantRangeM = groundDistM;
          let deltaAltStr = '';
          if (pt[2] != null && this.receiverConfig.altitude_m != null) {
            const dAlt = pt[2] - this.receiverConfig.altitude_m;
            slantRangeM = Math.sqrt(groundDistM ** 2 + dAlt ** 2);
            deltaAltStr = ` (Δh: ${dAlt >= 0 ? '+' : ''}${Math.round(dAlt)}m)`;
          }

          const slantStr = slantRangeM >= 1000 ? `${(slantRangeM / 1000).toFixed(2)} km` : `${Math.round(slantRangeM)} m`;
          const groundStr = groundDistM >= 1000 ? `${(groundDistM / 1000).toFixed(2)} km` : `${Math.round(groundDistM)} m`;

          rxPopupHtml = `
            <div style="background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.35); border-radius: 4px; padding: 4px 6px; margin: 4px 0;">
              <div style="font-weight: 700; color: #d97706; display: flex; justify-content: space-between;">
                <span>📡 Receiver Distance:</span>
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

      // 3. Pilot / GCS Home Location & Link Line
      if (encounter.pilot_lat != null && encounter.pilot_lon != null) {
        const pilotLatLng = [encounter.pilot_lat, encounter.pilot_lon];
        const pilotMarker = L.marker(pilotLatLng, {
          icon: this.createPilotIcon(),
          zIndexOffset: 1500,
        }).bindPopup(`
          <div style="font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px; color: #080c16;">
            <b>Pilot / GCS Home Location</b><br/>
            <b>Lat:</b> ${encounter.pilot_lat.toFixed(5)}<br/>
            <b>Lon:</b> ${encounter.pilot_lon.toFixed(5)}<br/>
            <b>Alt:</b> ${encounter.pilot_alt_m ? encounter.pilot_alt_m.toFixed(1) + 'm' : 'N/A'}
          </div>
        `);
        pilotMarker.addTo(this.map);
        this.pilotMarkers.set(encId, pilotMarker);

        const pilotLine = L.polyline([pilotLatLng, latlngs[0]], {
          color: '#10b981',
          weight: 1.5,
          dashArray: '4, 4',
          opacity: 0.7,
        }).addTo(this.map);
        this.pilotLines.set(encId, pilotLine);
      }
    }

    // 4. Aircraft Marker at latest position
    const latestPt = traj[traj.length - 1];
    if (latestPt) {
      const marker = L.marker([latestPt[0], latestPt[1]], {
        icon: this.createDroneIcon(encId, latestPt[4] || 0, isSelected, encounter.is_active, displayLabel),
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

    // Include receiver location in bounds if present
    if (this.receiverConfig && this.receiverConfig.enabled && this.receiverConfig.latitude != null) {
      bounds.extend([this.receiverConfig.latitude, this.receiverConfig.longitude]);
      count++;
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
      });
    } else {
      this.allEncounters.forEach(enc => {
        if (enc.latest_position && !isNaN(enc.latest_position.lat) && !isNaN(enc.latest_position.lon)) {
          bounds.extend([enc.latest_position.lat, enc.latest_position.lon]);
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
