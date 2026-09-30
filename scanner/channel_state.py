#!/usr/bin/env python3
"""
Wi-Fi Channel and Spectrum State Management
Contains 2.4 GHz and 5.8 GHz channel definitions, frequency mapping helpers,
and the transition-aware SharedChannelState manager with drain retention window.
"""

import threading
import time
from typing import Optional, Tuple

# Wi-Fi Channel State & Hopping Definitions
SOCIAL_CHANNEL_2G = 6
NON_SOCIAL_CHANNELS_2G = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]

SOCIAL_CHANNEL_5G = 149
NON_SOCIAL_CHANNELS_5G = [153, 157, 161, 165, 169, 173]


def get_band_for_channel(ch: int) -> str:
    """Returns the RF frequency band string for an 802.11 channel number."""
    if ch <= 14:
        return "2.4GHz"
    elif ch >= 36:
        return "5.8GHz" if ch >= 149 else "5GHz"
    return "Unknown"


def get_freq_for_channel(ch: int) -> int:
    """Returns center frequency in MHz for an 802.11 channel number."""
    if ch == 14:
        return 2484
    elif 1 <= ch <= 13:
        return 2407 + 5 * ch
    elif ch >= 36:
        return 5000 + 5 * ch
    return 0


def get_channel_for_freq(freq: int) -> int:
    """Calculates 802.11 channel number from center frequency in MHz."""
    if freq == 2484:
        return 14
    elif 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    elif 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return 0


class SharedChannelState:
    """
    Thread-safe state holding the current active Wi-Fi channel, band, and frequency.
    Supports transition-aware packet attribution:
    - Retains previous channel for drain_retention_s (default: 5ms) after switch start.
    - Honors physical radiotap frequency header as ground-truth when available.
    """
    def __init__(self, initial_channel: int = 6, drain_retention_ms: float = 5.0):
        self.lock = threading.Lock()
        self.channel = initial_channel
        self.band = get_band_for_channel(initial_channel)
        self.frequency = get_freq_for_channel(initial_channel)
        self.previous_channel = initial_channel
        self.previous_band = self.band
        self.previous_frequency = self.frequency
        self.target_channel = initial_channel
        self.target_band = self.band
        self.target_frequency = self.frequency
        self.is_switching = False
        self.switch_start_time = 0.0
        self.drain_retention_s = max(0.0, drain_retention_ms / 1000.0)
        self.last_switch_time = time.time()
        self.total_switches = 0

    def start_switch(self, target_channel: int, source_channel: Optional[int] = None):
        """Signals start of physical channel transition."""
        with self.lock:
            src = source_channel if source_channel is not None else self.channel
            self.previous_channel = src
            self.previous_band = get_band_for_channel(src)
            self.previous_frequency = get_freq_for_channel(src)
            self.target_channel = target_channel
            self.target_band = get_band_for_channel(target_channel)
            self.target_frequency = get_freq_for_channel(target_channel)
            self.is_switching = True
            self.switch_start_time = time.time()

    def finish_switch(self, target_channel: int):
        """Signals completion of channel switch command."""
        with self.lock:
            self.channel = target_channel
            self.band = get_band_for_channel(target_channel)
            self.frequency = get_freq_for_channel(target_channel)
            self.target_channel = target_channel
            self.is_switching = False
            self.last_switch_time = time.time()
            self.total_switches += 1

    def update(self, new_channel: int):
        """Direct channel update (backward-compatible)."""
        with self.lock:
            self.previous_channel = self.channel
            self.previous_band = self.band
            self.previous_frequency = self.frequency
            self.channel = new_channel
            self.band = get_band_for_channel(new_channel)
            self.frequency = get_freq_for_channel(new_channel)
            self.target_channel = new_channel
            self.target_band = self.band
            self.target_frequency = self.frequency
            self.is_switching = False
            self.last_switch_time = time.time()
            self.total_switches += 1

    def resolve_channel(self, pkt_ts: float, radiotap_freq: Optional[int] = None) -> Tuple[int, str, int]:
        """
        Resolves the true physical channel, band, and frequency for a captured frame:
        1. If valid radiotap_freq is present, resolves directly from radiotap.
        2. If switching and packet arrived within drain retention window (< 5ms), attributes to previous channel.
        3. If switching and packet arrived after drain window, attributes to target channel.
        4. Otherwise returns the currently active channel.
        """
        if radiotap_freq and radiotap_freq > 0:
            ch = get_channel_for_freq(radiotap_freq)
            if ch > 0:
                return ch, get_band_for_channel(ch), radiotap_freq

        with self.lock:
            if self.is_switching:
                elapsed = pkt_ts - self.switch_start_time
                if elapsed < self.drain_retention_s:
                    return self.previous_channel, self.previous_band, self.previous_frequency
                else:
                    return self.target_channel, self.target_band, self.target_frequency
            return self.channel, self.band, self.frequency

    def get(self) -> Tuple[int, str, int, float]:
        with self.lock:
            return self.channel, self.band, self.frequency, self.last_switch_time
