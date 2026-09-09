import { calculateHaversineDistanceM, calculateBearingDeg, getBearingCompass } from './map.js';

/**
 * Tactical Drone Remote ID Airspace Monitor - Timeline Scrubber Controller
 * Manages historic flight replay, step controls, playback speed, and slider synchronization.
 */

export class TimelineScrubberController {
  constructor(onSeek) {
    this.onSeek = onSeek;
    this.container = document.getElementById('scrubber-bar');
    this.rangeSlider = document.getElementById('scrubber-range');
    this.playBtn = document.getElementById('btn-scrub-play');
    this.prevBtn = document.getElementById('btn-scrub-prev');
    this.nextBtn = document.getElementById('btn-scrub-next');
    this.speedBtn = document.getElementById('btn-scrub-speed');
    
    this.currTimeEl = document.getElementById('scrub-curr-time');
    this.totalTimeEl = document.getElementById('scrub-total-time');
    this.pointIndexEl = document.getElementById('scrub-point-index');
    this.pointAltEl = document.getElementById('scrub-point-alt');
    this.pointSpeedEl = document.getElementById('scrub-point-speed');
    this.pointRxEl = document.getElementById('scrub-point-rx');

    this.trajectory = [];
    this.currentIndex = 0;
    this.isPlaying = false;
    this.playbackInterval = null;
    this.speedMultipliers = [1, 2, 5, 10];
    this.speedIndex = 0;
    this.receiverConfig = null;

    this.initControls();
  }

  setReceiverConfig(config) {
    this.receiverConfig = config;
    if (this.trajectory && this.trajectory.length > 0) {
      this.updateRxDist(this.trajectory[this.currentIndex]);
    }
  }

  initControls() {
    if (this.rangeSlider) {
      this.rangeSlider.addEventListener('input', (e) => {
        const idx = parseInt(e.target.value, 10);
        this.seekTo(idx);
      });
    }

    if (this.playBtn) {
      this.playBtn.addEventListener('click', () => {
        this.togglePlay();
      });
    }

    if (this.prevBtn) {
      this.prevBtn.addEventListener('click', () => {
        this.seekTo(Math.max(0, this.currentIndex - 1));
      });
    }

    if (this.nextBtn) {
      this.nextBtn.addEventListener('click', () => {
        this.seekTo(Math.min(this.trajectory.length - 1, this.currentIndex + 1));
      });
    }

    if (this.speedBtn) {
      this.speedBtn.addEventListener('click', () => {
        this.speedIndex = (this.speedIndex + 1) % this.speedMultipliers.length;
        const mult = this.speedMultipliers[this.speedIndex];
        this.speedBtn.textContent = `${mult}x`;
        if (this.isPlaying) {
          this.pause();
          this.play();
        }
      });
    }
  }

  setTrajectory(trajectory) {
    this.pause();
    this.trajectory = trajectory || [];
    this.currentIndex = 0;

    if (this.trajectory.length > 0) {
      this.show();
      this.rangeSlider.min = 0;
      this.rangeSlider.max = this.trajectory.length - 1;
      this.rangeSlider.value = 0;

      // Update total time
      const t0 = this.trajectory[0][5] || 0;
      const tEnd = this.trajectory[this.trajectory.length - 1][5] || t0;
      const totalSec = Math.round(tEnd - t0);
      this.totalTimeEl.textContent = this.formatDuration(totalSec);

      this.seekTo(0);
    } else {
      this.hide();
    }
  }

  show() {
    if (this.container) this.container.style.display = 'grid';
    document.body.classList.add('has-scrubber');
  }

  hide() {
    this.pause();
    if (this.container) this.container.style.display = 'none';
    document.body.classList.remove('has-scrubber');
  }

  seekTo(index) {
    if (!this.trajectory || this.trajectory.length === 0) return;
    this.currentIndex = Math.max(0, Math.min(index, this.trajectory.length - 1));
    this.rangeSlider.value = this.currentIndex;

    const pt = this.trajectory[this.currentIndex];
    // pt: [lat, lon, alt, speed, heading, ts]
    const t0 = this.trajectory[0][5] || 0;
    const currT = pt[5] || t0;
    const elapsedSec = Math.round(currT - t0);

    this.currTimeEl.textContent = this.formatDuration(elapsedSec);
    this.pointIndexEl.textContent = `${this.currentIndex + 1}/${this.trajectory.length}`;
    
    const altMsl = pt[2] != null ? Math.round(pt[2]) : '--';
    const repHeight = (pt.length > 6 && pt[6] != null) ? Math.round(pt[6]) : null;
    const hType = (pt.length > 7 && pt[7] === 1) ? 'AGL' : 'ATO';
    this.pointAltEl.textContent = repHeight != null ? `${altMsl} (H: ${repHeight >= 0 ? '+' : ''}${repHeight}${hType})` : `${altMsl}`;
    this.pointSpeedEl.textContent = pt[3] != null ? pt[3].toFixed(1) : '--';

    this.updateRxDist(pt);

    if (this.onSeek) {
      this.onSeek(this.currentIndex, pt);
    }
  }

  updateRxDist(pt) {
    if (!this.pointRxEl) return;
    if (!pt || !this.receiverConfig || !this.receiverConfig.enabled || this.receiverConfig.latitude == null) {
      this.pointRxEl.textContent = '--';
      return;
    }

    const groundM = calculateHaversineDistanceM(this.receiverConfig.latitude, this.receiverConfig.longitude, pt[0], pt[1]);
    const brg = calculateBearingDeg(this.receiverConfig.latitude, this.receiverConfig.longitude, pt[0], pt[1]);
    
    let slantM = groundM;
    if (pt[2] != null && this.receiverConfig.altitude_m != null) {
      const dAlt = pt[2] - this.receiverConfig.altitude_m;
      slantM = Math.sqrt(groundM ** 2 + dAlt ** 2);
    }

    const dStr = slantM >= 1000 ? `${(slantM / 1000).toFixed(2)}km` : `${Math.round(slantM)}m`;
    const compass = getBearingCompass(brg);
    this.pointRxEl.textContent = `${dStr} (${Math.round(brg)}° ${compass})`;
  }

  togglePlay() {
    if (this.isPlaying) {
      this.pause();
    } else {
      this.play();
    }
  }

  play() {
    if (!this.trajectory || this.trajectory.length <= 1) return;
    this.isPlaying = true;
    this.playBtn.textContent = '⏸';
    this.playBtn.style.background = '#f59e0b';

    // If at end, loop to start
    if (this.currentIndex >= this.trajectory.length - 1) {
      this.seekTo(0);
    }

    const mult = this.speedMultipliers[this.speedIndex];
    const intervalMs = Math.max(100, 1000 / mult);

    this.playbackInterval = setInterval(() => {
      if (this.currentIndex < this.trajectory.length - 1) {
        this.seekTo(this.currentIndex + 1);
      } else {
        this.pause();
      }
    }, intervalMs);
  }

  pause() {
    this.isPlaying = false;
    if (this.playbackInterval) {
      clearInterval(this.playbackInterval);
      this.playbackInterval = null;
    }
    if (this.playBtn) {
      this.playBtn.textContent = '▶';
      this.playBtn.style.background = 'var(--accent-cyan)';
    }
  }

  formatDuration(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
}
