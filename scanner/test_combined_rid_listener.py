#!/usr/bin/env python3
"""
Unit tests for combined_rid_listener.py:
1. Channel hopping sequence & timing calculations
2. ASTM F3411 payload decoding (Basic ID, Location, System, Operator ID, Self-ID)
3. SharedChannelState concurrency
"""

import unittest
import os
import json
import struct
import time
import tempfile
import sqlite3
from scanner.combined_rid_listener import (
    SharedChannelState,
    WifiChannelHopperThread,
    EncounterTracker,
    rehydrate_db_from_jsonl,
    decode_astm_message,
    parse_astm_payload,
    extract_radiotap_rssi,
    extract_radiotap_phy_info,
    SOCIAL_CHANNEL_2G,
    NON_SOCIAL_CHANNELS_2G,
    SOCIAL_CHANNEL_5G,
    NON_SOCIAL_CHANNELS_5G,
    get_band_for_channel,
    get_freq_for_channel,
    get_channel_for_freq,
)

class TestCombinedRIDListener(unittest.TestCase):

    def test_channel_helpers(self):
        self.assertEqual(get_band_for_channel(6), "2.4GHz")
        self.assertEqual(get_band_for_channel(1), "2.4GHz")
        self.assertEqual(get_band_for_channel(149), "5.8GHz")
        self.assertEqual(get_band_for_channel(157), "5.8GHz")
        self.assertEqual(get_freq_for_channel(6), 2437)
        self.assertEqual(get_freq_for_channel(149), 5745)
        self.assertEqual(get_channel_for_freq(2437), 6)
        self.assertEqual(get_channel_for_freq(2412), 1)
        self.assertEqual(get_channel_for_freq(2484), 14)
        self.assertEqual(get_channel_for_freq(5745), 149)
        self.assertEqual(get_channel_for_freq(5785), 157)

    def test_shared_channel_state(self):
        state = SharedChannelState(initial_channel=6)
        ch, band, freq, ts = state.get()
        self.assertEqual(ch, 6)
        self.assertEqual(band, "2.4GHz")
        self.assertEqual(freq, 2437)

        state.update(149)
        ch, band, freq, ts = state.get()
        self.assertEqual(ch, 149)
        self.assertEqual(band, "5.8GHz")
        self.assertEqual(freq, 5745)

    def test_shared_channel_state_transitions_and_drain_retention(self):
        state = SharedChannelState(initial_channel=6, drain_retention_ms=5.0)
        t0 = 1000.0

        # Start transition from Ch 6 to Ch 149
        state.start_switch(target_channel=149, source_channel=6)
        state.switch_start_time = t0

        # 1. Packet arriving 2ms into switch (within 5ms drain window) -> attributed to Ch 6
        ch, band, freq = state.resolve_channel(pkt_ts=t0 + 0.002)
        self.assertEqual(ch, 6)
        self.assertEqual(band, "2.4GHz")
        self.assertEqual(freq, 2437)

        # 2. Packet arriving 10ms into switch (past drain window, e.g. target RF lock) -> attributed to Ch 149
        ch, band, freq = state.resolve_channel(pkt_ts=t0 + 0.010)
        self.assertEqual(ch, 149)
        self.assertEqual(band, "5.8GHz")
        self.assertEqual(freq, 5745)

        # 3. Radiotap frequency ground-truth override (e.g. hardware header says 2437 MHz)
        ch, band, freq = state.resolve_channel(pkt_ts=t0 + 0.010, radiotap_freq=2437)
        self.assertEqual(ch, 6)
        self.assertEqual(band, "2.4GHz")
        self.assertEqual(freq, 2437)

        # 4. Finish transition
        state.finish_switch(target_channel=149)
        ch, band, freq = state.resolve_channel(pkt_ts=t0 + 0.050)
        self.assertEqual(ch, 149)
        self.assertEqual(band, "5.8GHz")
        self.assertEqual(freq, 5745)

    def test_wifi_channel_hopper_dwell_timing(self):
        state = SharedChannelState(initial_channel=6)
        hopper = WifiChannelHopperThread(
            interface="wlan_test",
            channel_state=state,
            non_social_ratio_k=1,
            social_dwell_ms=1000,
            non_social_dwell_ms=200,
            non_social_dwell_5g_ms=250,
        )
        self.assertEqual(hopper.social_dwell_s, 1.0)
        self.assertEqual(hopper.non_social_dwell_2g_s, 0.200)
        self.assertEqual(hopper.non_social_dwell_5g_s, 0.250)
        self.assertEqual(hopper.n_2g_non_social, 2)
        self.assertEqual(hopper.n_5g_non_social, 1)

    def test_decode_basic_id(self):
        # Header: (MsgType 0 << 4) | proto 2 = 0x02
        # ID Type: Serial number (1) << 4 | UA Type Helicopter (2) = 0x12
        # UAS ID: 20 bytes ASCII
        serial = b"15967200000000000001"
        block = bytes([0x02, 0x12]) + serial + b'\x00\x00\x00'
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Basic ID")
        self.assertEqual(decoded["id"], "15967200000000000001")
        self.assertEqual(decoded["id_type"], 1)
        self.assertEqual(decoded["ua_type"], 2)

    def test_decode_location(self):
        # Header: (MsgType 1 << 4) | proto 2 = 0x12
        # Status flags: 0x00
        # Track dir: 180 deg
        # Speed: 20 * 0.25 = 5.0 m/s (speed_mult=0)
        # VSpeed: 2 * 0.5 = 1.0 m/s
        # Lat: 47.3769 * 1e7 = 473769000
        # Lon: 8.5417 * 1e7 = 85417000
        # Alt: (450 + 1000) * 2 = 2900 (0x0B54)
        # GAlt: (460 + 1000) * 2 = 2920 (0x0B68)
        # Height: (50 + 1000) * 2 = 2100 (0x0834)
        lat_i = 473769000
        lon_i = 85417000
        block = struct.pack(
            '<BBBBBiiHHH6s',
            0x12, 0x00, 180, 20, 2,
            lat_i, lon_i,
            2900, 2920, 2100,
            b'\x00' * 6
        )
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Location")
        self.assertAlmostEqual(decoded["lat"], 47.3769, places=4)
        self.assertAlmostEqual(decoded["lon"], 8.5417, places=4)
        self.assertEqual(decoded["speed_mps"], 5.0)
        self.assertEqual(decoded["direction_deg"], 180)
        self.assertEqual(decoded["pressure_altitude_m"], 450.0)
        self.assertEqual(decoded["geodetic_altitude_m"], 460.0)
        self.assertEqual(decoded["height_m"], 50.0)

    def test_decode_message_pack(self):
        # Build 2-message pack: Basic ID + Location
        serial = b"TESTDRONE00000000001"
        b_id = bytes([0x02, 0x12]) + serial + b'\x00\x00\x00'
        loc = struct.pack(
            '<BBBBBiiHHH6s',
            0x12, 0x00, 90, 40, 0,
            470000000, 80000000,
            2500, 2500, 2100,
            b'\x00' * 6
        )
        # Pack header: 0xF2 (MsgType 0xF, ver 2), size=25 (0x19), count=2
        pack_payload = bytes([0xF2, 0x19, 0x02]) + b_id + loc
        parsed, raw_b64 = parse_astm_payload(pack_payload)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(len(raw_b64), 2)
        self.assertEqual(parsed[0]["type"], "Basic ID")
        self.assertEqual(parsed[0]["id"], "TESTDRONE00000000001")
        self.assertEqual(parsed[1]["type"], "Location")
        self.assertEqual(parsed[1]["direction_deg"], 90)

    def test_decode_auth_page_zero(self):
        # Header: (MsgType 2 << 4) | proto 2 = 0x22
        # AuthType: UAS ID Signature (1) << 4 | DataPage 0 = 0x10
        # LastPageIndex: 2
        # Length: 55
        # Timestamp: 3600 (seconds since 2019-01-01) -> Epoch: 1546300800 + 3600 = 1546304400
        # 17 bytes auth data
        auth_data = b"0123456789ABCDEFG"
        block = struct.pack('<BBBB I 17s', 0x22, 0x10, 2, 55, 3600, auth_data)
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Auth")
        self.assertEqual(decoded["auth_type"], 1)
        self.assertEqual(decoded["auth_type_name"], "UAS ID Signature")
        self.assertEqual(decoded["page_number"], 0)
        self.assertEqual(decoded["last_page_index"], 2)
        self.assertEqual(decoded["auth_data_length"], 55)
        self.assertEqual(decoded["auth_timestamp_epoch"], 1546304400)
        self.assertEqual(decoded["auth_data_hex"], auth_data.hex().upper())

    def test_decode_auth_continuation_page(self):
        # Page 1
        auth_data = b"CONTINUATION_PAGE_1_23B"
        block = bytes([0x22, 0x11]) + auth_data
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Auth")
        self.assertEqual(decoded["page_number"], 1)
        self.assertEqual(decoded["auth_data_hex"], auth_data.hex().upper())

    def test_decode_self_id(self):
        # Header: 0x32 (Type 3, Proto 2)
        # DescType: 1 (Emergency Status v2)
        # Desc: "Motor failure landing"
        desc = b"Motor failure landing\x00\x00"
        block = bytes([0x32, 0x01]) + desc
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Self-ID")
        self.assertEqual(decoded["desc_type"], 1)
        self.assertEqual(decoded["desc_type_name"], "Emergency Status (v2)")
        self.assertEqual(decoded["description"], "Motor failure landing")

    def test_decode_system(self):
        # Header: 0x42 (Type 4, Proto 2)
        # Flags: OperatorLocationType Live GNSS (1), ClassificationType EU (1) -> (1 << 2) | 1 = 0x05
        # Pilot Lat: 47.3769 * 1e7 = 473769000
        # Pilot Lon: 8.5417 * 1e7 = 85417000
        # AreaCount: 1
        # AreaRadius: 5 (50m)
        # AreaCeiling: (150 + 1000) * 2 = 2300
        # AreaFloor: (0 + 1000) * 2 = 2000
        # Byte 17: EU Category Open (1) << 4 | Class C1 (2) = 0x12
        # Pilot Alt: (430 + 1000) * 2 = 2860
        # Timestamp: 7200 -> Epoch: 1546300800 + 7200 = 1546308000
        block = struct.pack(
            '<BBiiH B HH B H I B',
            0x42, 0x05,
            473769000, 85417000,
            1, 5,
            2300, 2000,
            0x12,
            2860,
            7200,
            0x00
        )
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "System")
        self.assertEqual(decoded["operator_location_type_name"], "Live GNSS (Dynamic Pilot / GCS)")
        self.assertEqual(decoded["classification_type_name"], "European Union (EU)")
        self.assertAlmostEqual(decoded["pilot_lat"], 47.3769, places=4)
        self.assertAlmostEqual(decoded["pilot_lon"], 8.5417, places=4)
        self.assertEqual(decoded["area_radius_m"], 50)
        self.assertEqual(decoded["area_ceiling_m"], 150.0)
        self.assertEqual(decoded["area_floor_m"], 0.0)
        self.assertEqual(decoded["category_eu_name"], "Open")
        self.assertEqual(decoded["class_eu_name"], "Class 1")
        self.assertEqual(decoded["pilot_alt_m"], 430.0)
        self.assertEqual(decoded["system_timestamp_epoch"], 1546308000)

    def test_decode_operator_id(self):
        # Header: 0x52 (Type 5, Proto 2)
        # OpIdType: 0 (Operator ID)
        # OperatorId: "CHE87astd57qkgc4" (16-char public CAA registration number without secret 3-char PIN)
        op_id = b"CHE87astd57qkgc4"
        block = bytes([0x52, 0x00]) + op_id + (b'\x00' * 7) # 2 + 16 + 7 = 25 bytes
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["type"], "Operator ID")
        self.assertEqual(decoded["operator_id"], "CHE87astd57qkgc4")
        self.assertEqual(decoded["operator_id_type_name"], "Operator ID")

    def test_decode_location_accuracies_and_speeds(self):
        # Test high speed multiplier (speed_mult = 1) and accuracies
        # Byte 1: Status Airborne (2) << 4 | HeightType AGL (1) << 2 | EW Dir West (1) << 1 | SpeedMult (1) = 0x27
        # Dir: 45 (+180 = 225 deg)
        # Speed: 50 -> 63.75 + (50 * 0.75) = 101.25 m/s
        # VSpeed: -10 * 0.5 = -5.0 m/s (signed int8: -10 = 246 / 0xF6)
        # Accuracy B19: VertAcc <10m (5) << 4 | HorizAcc <3m (11) = 0x5B
        # Accuracy B20: BaroAcc <25m (4) << 4 | SpeedAcc <1m/s (3) = 0x43
        # TimeStamp: 1234 (123.4s)
        # Accuracy B23: TSAcc <0.2s (2) = 0x02
        block = struct.pack(
            '<BBBBb ii HHH BB H B B',
            0x12, 0x27, 45, 50, -10,
            470000000, 80000000,
            2000, 2000, 2000,
            0x5B, 0x43,
            1234, 0x02, 0x00
        )
        self.assertEqual(len(block), 25)
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["status_name"], "Airborne")
        self.assertEqual(decoded["direction_deg"], 225)
        self.assertAlmostEqual(decoded["speed_mps"], 101.25)
        self.assertEqual(decoded["vertical_speed_mps"], -5.0)
        self.assertEqual(decoded["height_type_name"], "Above Ground Level (AGL)")
        self.assertEqual(decoded["horizontal_accuracy_name"], "< 3 m")
        self.assertEqual(decoded["vertical_accuracy_name"], "< 10 m")
        self.assertEqual(decoded["baro_accuracy_name"], "< 25 m")
        self.assertEqual(decoded["speed_accuracy_name"], "< 1 m/s")
        self.assertEqual(decoded["timestamp_s"], 123.4)
        self.assertEqual(decoded["timestamp_accuracy_name"], "< 0.2 s")

    def test_security_malformed_message_packs(self):
        # 1. Zero msg_count in message pack (DA-01)
        zero_pack = bytes([0xF2, 0x19, 0x00])
        parsed, raw_b64 = parse_astm_payload(zero_pack)
        self.assertEqual(len(parsed), 0)

        # 2. Invalid msg_size != 25 (e.g. msg_size = 50)
        invalid_size_pack = bytes([0xF2, 50, 0x01]) + (b"\x00" * 50)
        parsed, raw_b64 = parse_astm_payload(invalid_size_pack)
        self.assertEqual(len(parsed), 0)

        # 3. Excessive msg_count > 9 (e.g. msg_count = 20)
        excess_pack = bytes([0xF2, 0x19, 20]) + (b"\x00" * 500)
        parsed, raw_b64 = parse_astm_payload(excess_pack)
        self.assertEqual(len(parsed), 0)

        # 4. Truncated pack (header says count=2, but buffer only has 1 message)
        truncated_pack = bytes([0xF2, 0x19, 0x02]) + (b"\x00" * 25)
        parsed, raw_b64 = parse_astm_payload(truncated_pack)
        self.assertEqual(len(parsed), 0)

    def test_security_terminal_ansi_escape_sanitization(self):
        # DA-03: String containing ANSI escape sequences (\x1b[2J) and non-printable control chars (\x07)
        malicious_serial = b"\x1b[2J\x1b[HATTACKER_ID\x07\x00"
        block = bytes([0x02, 0x12]) + malicious_serial.ljust(20, b'\x00') + b'\x00\x00\x00'
        decoded = decode_astm_message(block)
        self.assertIsNotNone(decoded)
        # Verify ANSI control chars were neutralized
        self.assertNotIn("\x1b", decoded["id"])
        self.assertNotIn("\x07", decoded["id"])
        self.assertIn("ATTACKER_ID", decoded["id"])

    def test_security_csv_formula_injection(self):
        # ANDR-01 / RDBP-03: Cell starting with formula operator
        from scanner.query_rid_db import sanitize_csv_cell
        self.assertEqual(sanitize_csv_cell("=cmd|' /C calc'!A0"), "'=cmd|' /C calc'!A0")
        self.assertEqual(sanitize_csv_cell("+12345"), "'+12345")
        self.assertEqual(sanitize_csv_cell("-12345"), "'-12345")
        self.assertEqual(sanitize_csv_cell("@SUM(A1:A10)"), "'@SUM(A1:A10)")
        self.assertEqual(sanitize_csv_cell("NORMAL_TEXT"), "NORMAL_TEXT")

    def test_ble_sniffer_json_event_processing(self):
        import queue
        import json
        event_queue = queue.Queue()
        # Simulated JSON record from nrf_bt_sniffer_json.py for BLE 5 Extended Remote ID
        raw_json_line = json.dumps({
            "timestamp": 1725448000.123,
            "mac": "FE:FB:89:DF:4A:3D",
            "rssi_dbm": -75,
            "pdu_type": "ADV_EXT_IND/AUX_ADV_IND",
            "remote_id": {
                "transport": "ble5_extended",
                "counter": 42,
                "parsed_messages": [
                    {"type": "Basic ID", "id": "Spoofed_Serial_12345", "raw_hex": "021253706f6f6665645f53657269616c5f3132333435000000"},
                    {"type": "Location", "lat": 47.3769, "lon": 8.5417, "alt": 450.0, "speed": 12.5, "heading": 180, "raw_hex": "1200b432021c3d18e8051759080b540b680834000017700000"}
                ]
            }
        })
        
        record = json.loads(raw_json_line)
        rid_info = record["remote_id"]
        parsed_msgs = rid_info.get("parsed_messages", [])
        transport_type = str(rid_info.get("transport", "bt5")).lower()
        pdu_type = str(record.get("pdu_type", "")).upper()
        transport = "bt5" if ("5" in transport_type or "ext" in transport_type or "AUX" in pdu_type) else "bt4"
        
        self.assertEqual(transport, "bt5")
        self.assertEqual(len(parsed_msgs), 2)
        self.assertEqual(parsed_msgs[0]["id"], "Spoofed_Serial_12345")
        self.assertEqual(parsed_msgs[1]["lat"], 47.3769)

    def test_schedule_coverage(self):
        # Verify that with k=1 (2 in 2.4G, 1 in 5.8G), 6 cycles cover all 12 2.4G non-social and all 6 5.8G non-social channels
        k = 1
        n_2g = 2 * k
        n_5g = k

        visited_2g = []
        visited_5g = []

        idx_2g = 0
        idx_5g = 0

        for cycle in range(6):
            for _ in range(n_2g):
                visited_2g.append(NON_SOCIAL_CHANNELS_2G[idx_2g % len(NON_SOCIAL_CHANNELS_2G)])
                idx_2g += 1
            for _ in range(n_5g):
                visited_5g.append(NON_SOCIAL_CHANNELS_5G[idx_5g % len(NON_SOCIAL_CHANNELS_5G)])
                idx_5g += 1

        self.assertEqual(len(visited_2g), 12)
        self.assertEqual(len(visited_5g), 6)
        self.assertEqual(set(visited_2g), set(NON_SOCIAL_CHANNELS_2G))
        self.assertEqual(set(visited_5g), set(NON_SOCIAL_CHANNELS_5G))

    def test_nordic_ble_packet_metadata_and_rssi(self):
        from scanner.sniffers.nrf_bt_sniffer_json import parse_packet_metadata
        # Build realistic nRF Sniffer v2 DLT_NORDIC_BLE header (17 bytes)
        # Board(1) + Len(2, e.g. 164 = 0x00A4) + Ver(1) + Cnt(2) + Type(1=0x06) + HdrLen(1=10) + Flags(1=0x01) + Ch(1=37) + RSSI(1=55 -> -55dBm) + EvtCnt(2) + DeltaTime(4)
        board = b'\x00'
        pkt_len = struct.pack('<H', 164)
        ver = b'\x02'
        cnt = struct.pack('<H', 123)
        pkt_type = b'\x06'
        hdr_len = b'\x0A'
        flags = b'\x01'
        rf_ch = bytes([37])
        rssi_magnitude = bytes([55]) # -55 dBm
        evt_cnt = struct.pack('<H', 1)
        delta_t = struct.pack('<I', 1000)
        
        nordic_hdr = board + pkt_len + ver + cnt + pkt_type + hdr_len + flags + rf_ch + rssi_magnitude + evt_cnt + delta_t
        self.assertEqual(len(nordic_hdr), 17)
        
        access_addr = b'\xd6\xbe\x89\x8e'
        pdu_hdr = b'\x07\x10' # ADV_EXT_IND / AUX_ADV_IND (0x07)
        
        # 1. Test Extended Header with AdvA + ADI (ext_hdr_len=9, flags=0x09: AdvA(bit0) | ADI(bit3))
        mac_bytes_le = bytes.fromhex("ABEB8E9B57CD") # CD:57:9B:8E:EB:AB
        adi_bytes = b'\x12\x34'
        ext_hdr_adva_adi = bytes([0x09, 0x09]) + mac_bytes_le + adi_bytes
        
        full_packet = nordic_hdr + access_addr + pdu_hdr + ext_hdr_adva_adi
        pdu_type_str, mac_addr, rssi_dbm, ch = parse_packet_metadata(full_packet)
        self.assertEqual(pdu_type_str, "ADV_EXT_IND/AUX_ADV_IND")
        self.assertEqual(mac_addr, "CD:57:9B:8E:EB:AB")
        self.assertEqual(rssi_dbm, -55)
        self.assertEqual(ch, 37)

        # 2. Test Extended Header without AdvA (e.g. primary ADV_EXT_IND with only AuxPtr: flags=0x10)
        ext_hdr_aux_only = bytes([0x04, 0x10]) + b'\x01\x02\x03'
        full_pkt_no_adva = nordic_hdr + access_addr + pdu_hdr + ext_hdr_aux_only
        pdu_type_str, mac_addr, rssi_dbm, ch = parse_packet_metadata(full_pkt_no_adva)
        self.assertEqual(pdu_type_str, "ADV_EXT_IND/AUX_ADV_IND")
        self.assertEqual(mac_addr, "UNKNOWN")

    def test_ble5_extended_header_and_rid_extraction(self):
        from scanner.sniffers.nrf_bt_sniffer_json import extract_remote_id_info, parse_packet_metadata
        # Test full end-to-end BLE 5 extended frame with AdvA and ASTM Remote ID Service Data
        # MAC: F1:D4:C5:1A:31:5A
        mac_bytes_le = bytes.fromhex("5A311AC5D4F1")
        # Extended Header: ext_hdr_len=9, flags=0x09 (AdvA + ADI)
        ext_hdr = bytes([0x09, 0x09]) + mac_bytes_le + b'\x99\x77'
        
        # Build ASTM Basic ID message: "Spoofed_Serial_25492" (20 bytes)
        serial_str = b"Spoofed_Serial_25492"
        basic_id_msg = bytes([0x02, 0x12]) + serial_str + b'\x00\x00\x00' # 25 bytes
        
        # AD Structure: Length(1B), Type(0x16 Service Data), UUID(0xFA 0xFF), AppCode(0x0D), Counter(42), Msg(25B)
        svc_payload = bytes([0x0D, 42]) + basic_id_msg
        ad_struct = bytes([len(svc_payload) + 3, 0x16, 0xFA, 0xFF]) + svc_payload
        
        access_addr = b'\xd6\xbe\x89\x8e'
        pdu_hdr = bytes([0x07, len(ext_hdr) + len(ad_struct)])
        
        nordic_hdr = b'\x00' * 9 + bytes([37, 60]) + b'\x00' * 6 # RSSI=-60, Ch=37
        full_packet = nordic_hdr + access_addr + pdu_hdr + ext_hdr + ad_struct
        
        pdu_type, mac, rssi, ch = parse_packet_metadata(full_packet)
        self.assertEqual(mac, "F1:D4:C5:1A:31:5A")
        
        rid_info = extract_remote_id_info(full_packet, pdu_type)
        self.assertIsNotNone(rid_info)
        self.assertEqual(rid_info["transport"], "ble5_extended")
        self.assertEqual(rid_info["counter"], 42)
        parsed = rid_info["parsed_messages"]
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["type"], "Basic ID")
        self.assertEqual(parsed[0]["id"], "Spoofed_Serial_25492")
        self.assertNotIn("toofed", parsed[0]["id"])

    def test_wifi_sniffer_frame_processing(self):
        # Construct realistic Wi-Fi 802.11 Beacon frame with Radiotap header
        # Radiotap header (18 bytes)
        rt_hdr = b'\x00\x00\x12\x00\x2e\x48\x00\x00\x10\x02\x85\x09\xa0\x00\xa0\x00\x00\x00'
        # 802.11 MAC Header: FrameControl (Beacon = 0x8000), Duration (0x0000), Addr1 (FF:FF:FF:FF:FF:FF), Addr2 (6 bytes), Addr3 (6 bytes), Seq (0x0000)
        mac_addr_bytes = bytes.fromhex("00C0CA9910EA")
        bssid_bytes = mac_addr_bytes
        mac_hdr = b'\x80\x00\x00\x00' + (b'\xff' * 6) + mac_addr_bytes + bssid_bytes + b'\x00\x00'
        # Beacon fixed parameters: Timestamp(8) + Interval(2) + Cap(2) = 12 bytes
        beacon_fixed = b'\x00' * 12
        # SSID IE: ID=0, Len=4, "RID1"
        ie_ssid = b'\x00\x04RID1'
        
        # Build 1 ASTM Basic ID message (25 bytes)
        serial = b"WIFI_TEST_DRONE_0001"
        basic_id_msg = bytes([0x02, 0x12]) + serial + b'\x00\x00\x00'
        # Pack header: 0xF2 (MsgType 0xF, ver 2), size=25 (0x19), count=1
        astm_pack = bytes([0xF2, 0x19, 0x01]) + basic_id_msg
        
        # Vendor Specific IE: 0xDD (Tag), Len, OUI(FA:0B:BC) + AppCode(0x0D) + Counter(42) + astm_pack
        vendor_payload = bytes([0x0D, 42]) + astm_pack
        oui_and_payload = b'\xfa\x0b\xbc' + vendor_payload
        ie_vendor = b'\xdd' + bytes([len(oui_and_payload)]) + oui_and_payload
        
        full_frame = rt_hdr + mac_hdr + beacon_fixed + ie_ssid + ie_vendor
        
        # Test simulated parsing logic from WifiSnifferThread
        vendor_ie_idx = -1
        search_offset = 0
        while True:
            idx = full_frame.find(b'\xfa\x0b\xbc', search_offset)
            if idx == -1: break
            if idx >= 2 and full_frame[idx - 2] == 0xDD:
                vendor_ie_idx = idx
                break
            search_offset = idx + 1
            
        self.assertNotEqual(vendor_ie_idx, -1)
        ie_len = full_frame[vendor_ie_idx - 1]
        vendor_data = full_frame[vendor_ie_idx + 3 : vendor_ie_idx + ie_len]
        self.assertEqual(vendor_data[0], 0x0D)
        counter = vendor_data[1]
        self.assertEqual(counter, 42)
        astm_payload = vendor_data[2:]
        
        parsed_msgs, b64_blocks = parse_astm_payload(astm_payload)
        self.assertEqual(len(parsed_msgs), 1)
        self.assertEqual(parsed_msgs[0]["type"], "Basic ID")
        self.assertEqual(parsed_msgs[0]["id"], "WIFI_TEST_DRONE_0001")
        self.assertEqual(extract_radiotap_rssi(full_frame), -96)

    def test_extract_radiotap_rssi_field_alignment_and_ranges(self):
        # 1. Standard Radiotap header with Flags, Rate, Channel (2437MHz, flags 0x00A0), dBm_AntSignal (-45 dBm)
        # Bitmask = 0x0000482E (FLAGS | RATE | CHANNEL | DBM_ANTSIGNAL | ANTENNA | RX_FLAGS)
        hdr1 = struct.pack('<BBHI', 0, 0, 18, 0x0000482E)
        hdr1 += bytes([0x10, 0x02, 0x85, 0x09, 0xa0, 0x00, 256 - 45, 0x00, 0x00, 0x00])
        self.assertEqual(extract_radiotap_rssi(hdr1), -45)

        # 2. Header with TSFT (8-byte microsecond timestamp where low byte is 0xEF = -17)
        # Bitmask = 0x0000002F (TSFT | FLAGS | RATE | CHANNEL | DBM_ANTSIGNAL)
        hdr2 = struct.pack('<BBHI', 0, 0, 23, 0x0000002F)
        hdr2 += bytes([0xef, 0xcd, 0xab, 0x90, 0x78, 0x56, 0x34, 0x12]) # TSFT
        hdr2 += bytes([0x00]) # Flags
        hdr2 += bytes([0x0c]) # Rate
        hdr2 += bytes([0x6c, 0x09, 0xa0, 0x00]) # Channel (2412 MHz, flags 0x00a0)
        hdr2 += bytes([256 - 73]) # dBm_AntSignal = -73 dBm
        self.assertEqual(extract_radiotap_rssi(hdr2), -73)

        # 3. Very strong signal (e.g. -5 dBm) and very weak signal (e.g. -105 dBm)
        hdr_strong = struct.pack('<BBHI', 0, 0, 18, 0x0000482E)
        hdr_strong += bytes([0x00, 0x02, 0x85, 0x09, 0xa0, 0x00, 256 - 5, 0x00, 0x00, 0x00])
        self.assertEqual(extract_radiotap_rssi(hdr_strong), -5)

        hdr_weak = struct.pack('<BBHI', 0, 0, 18, 0x0000482E)
        hdr_weak += bytes([0x00, 0x02, 0x85, 0x09, 0xa0, 0x00, 256 - 105, 0x00, 0x00, 0x00])
        self.assertEqual(extract_radiotap_rssi(hdr_weak), -105)

        # 4. No dBm_AntSignal in present bitmask
        hdr_no_rssi = struct.pack('<BBHI', 0, 0, 8, 0x00000004) # Only Rate
        hdr_no_rssi += bytes([0x02, 0x00, 0x00, 0x00])
        self.assertIsNone(extract_radiotap_rssi(hdr_no_rssi))

        # 5. Invalid/short frames
        self.assertIsNone(extract_radiotap_rssi(b''))
        self.assertIsNone(extract_radiotap_rssi(b'\x01\x00\x08\x00')) # Wrong version

    def test_extract_radiotap_rssi_extended_present_mask(self):
        # Header with 2 present words: Word 0 has bit 31 (Extended) and Bit 5 (dBm_AntSignal)
        w0 = 0x80000020
        w1 = 0x00000000
        hdr = struct.pack('<BBHII', 0, 0, 13, w0, w1)
        hdr += bytes([256 - 62]) # dBm_AntSignal immediately after w1
        self.assertEqual(extract_radiotap_rssi(hdr), -62)

    def test_encounter_tracker_multi_altitude_and_trajectory(self):
        import tempfile, sqlite3, json
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tracker = EncounterTracker(db_path=tmp.name, persist_interval_s=0.0)
            
            # Feed Location + System packet
            pkt = {
                "timestamp": 1789482000.0,
                "transport": "wifi",
                "channel": 6,
                "mac": "11:22:33:44:55:66",
                "rssi_dbm": -70,
                "counter": 42,
                "serial_number": "DRONE_ALT_TEST_01",
                "messages": [
                    {
                        "type": "Basic ID",
                        "id": "DRONE_ALT_TEST_01",
                    },
                    {
                        "type": "Location",
                        "lat": 47.3719,
                        "lon": 8.5312,
                        "geodetic_altitude_m": 540.0,
                        "pressure_altitude_m": 415.0,
                        "height_m": 80.0,
                        "height_type": 0,
                        "speed_mps": 15.0,
                        "direction_deg": 180,
                        "vertical_speed_mps": 1.5,
                    },
                    {
                        "type": "System",
                        "pilot_lat": 47.3715,
                        "pilot_lon": 8.5310,
                        "pilot_alt_m": 420.0,
                        "area_ceiling_m": 600.0,
                        "area_floor_m": 300.0,
                    }
                ]
            }
            enc_id = tracker.update_with_packet(pkt)
            tracker.finalize_all()

            with sqlite3.connect(tmp.name) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (enc_id,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["min_alt_m"], 540.0)
                self.assertEqual(row["max_alt_m"], 540.0)
                self.assertEqual(row["min_height_m"], 80.0)
                self.assertEqual(row["max_height_m"], 80.0)
                self.assertEqual(row["min_pressure_alt_m"], 415.0)
                self.assertEqual(row["max_pressure_alt_m"], 415.0)
                self.assertEqual(row["pilot_alt_m"], 420.0)
                self.assertEqual(row["area_ceil_m"], 600.0)
                self.assertEqual(row["area_floor_m"], 300.0)

                traj = json.loads(row["trajectory_json"])
                self.assertEqual(len(traj), 1)
                self.assertEqual(len(traj[0]), 12)
                self.assertEqual(traj[0][0], 47.3719)
                self.assertEqual(traj[0][1], 8.5312)
                self.assertEqual(traj[0][2], 540.0)
                self.assertEqual(traj[0][3], 15.0)
                self.assertEqual(traj[0][4], 180)
                self.assertEqual(traj[0][5], 1789482000.0)
                self.assertEqual(traj[0][6], 80.0) # height_m
                self.assertEqual(traj[0][7], 0)    # height_type
                self.assertEqual(traj[0][8], 415.0)# pressure_alt_m
                self.assertEqual(traj[0][9], 1.5)  # vert_spd
                self.assertEqual(traj[0][10], -70) # rssi_dbm
                self.assertEqual(traj[0][11], 42)  # counter

    def test_extract_radiotap_phy_info_legacy_rates(self):
        # 1. 1.0 Mbps DSSS (Rate = 2 -> 1.0 Mbps, Channel 2437 MHz, RSSI -50 dBm)
        # Bitmask = 0x0000002E (FLAGS | RATE | CHANNEL | DBM_ANTSIGNAL)
        # Length = 8 (hdr) + 1 (flags) + 1 (rate) + 4 (channel) + 1 (rssi) = 15 bytes
        hdr1 = struct.pack('<BBHI', 0, 0, 15, 0x0000002E)
        hdr1 += bytes([0x00, 0x02, 0x85, 0x09, 0xa0, 0x00, 256 - 50])
        phy1 = extract_radiotap_phy_info(hdr1)
        self.assertEqual(phy1["rate_mbps"], 1.0)
        self.assertEqual(phy1["modulation"], "DSSS")
        self.assertEqual(phy1["rate_desc"], "1.0 Mbps DSSS")
        self.assertEqual(phy1["frequency_mhz"], 2437)
        self.assertEqual(phy1["rssi_dbm"], -50)

        # 2. 6.0 Mbps OFDM (Rate = 12 -> 6.0 Mbps, Channel 5745 MHz with OFDM flag 0x0040)
        hdr2 = struct.pack('<BBHI', 0, 0, 15, 0x0000002E)
        hdr2 += bytes([0x00, 0x0c, 0x71, 0x16, 0x40, 0x01, 256 - 65])
        phy2 = extract_radiotap_phy_info(hdr2)
        self.assertEqual(phy2["rate_mbps"], 6.0)
        self.assertEqual(phy2["modulation"], "OFDM")
        self.assertEqual(phy2["rate_desc"], "6.0 Mbps OFDM")
        self.assertEqual(phy2["frequency_mhz"], 5745)
        self.assertEqual(phy2["rssi_dbm"], -65)

        # 3. 11.0 Mbps CCK (Rate = 22 -> 11.0 Mbps)
        hdr3 = struct.pack('<BBHI', 0, 0, 15, 0x0000002E)
        hdr3 += bytes([0x00, 0x16, 0x85, 0x09, 0x20, 0x00, 256 - 72])
        phy3 = extract_radiotap_phy_info(hdr3)
        self.assertEqual(phy3["rate_mbps"], 11.0)
        self.assertEqual(phy3["modulation"], "CCK")
        self.assertEqual(phy3["rate_desc"], "11.0 Mbps CCK")

    def test_extract_radiotap_phy_info_ht_mcs(self):
        # Header with MCS bitmask (Bit 19 = 0x00080000)
        # MCS 0 HT20 Long GI: known=0x07, flags=0x00 (20MHz, Long GI), mcs=0 -> 6.5 Mbps
        w0 = (1 << 19)
        hdr = struct.pack('<BBHI', 0, 0, 11, w0)
        hdr += bytes([0x07, 0x00, 0x00]) # known, flags (HT20 LGI), mcs=0
        phy = extract_radiotap_phy_info(hdr)
        self.assertEqual(phy["modulation"], "HT (802.11n)")
        self.assertEqual(phy["mcs_index"], 0)
        self.assertEqual(phy["bandwidth_mhz"], 20)
        self.assertEqual(phy["guard_interval"], "Long GI")
        self.assertEqual(phy["rate_mbps"], 6.5)
        self.assertIn("MCS 0", phy["rate_desc"])

        # MCS 7 HT40 Short GI: known=0x07, flags=0x05 (40MHz bit0=1, SGI bit2=1), mcs=7 -> 150.0 Mbps
        hdr2 = struct.pack('<BBHI', 0, 0, 11, w0)
        hdr2 += bytes([0x07, 0x05, 0x07]) # known, flags (HT40 SGI), mcs=7
        phy2 = extract_radiotap_phy_info(hdr2)
        self.assertEqual(phy2["mcs_index"], 7)
        self.assertEqual(phy2["bandwidth_mhz"], 40)
        self.assertEqual(phy2["guard_interval"], "Short GI")
        self.assertEqual(phy2["rate_mbps"], 150.0)

    def test_encounter_tracker_wifi_rates_persistence(self):
        import tempfile, sqlite3, json
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tracker = EncounterTracker(db_path=tmp.name, persist_interval_s=0.0)
            
            # Send 3 packets with 1.0 Mbps DSSS
            for i in range(3):
                pkt1 = {
                    "timestamp": 1000.0 + i,
                    "transport": "wifi",
                    "channel": 6,
                    "mac": "60:60:1F:AA:BB:CC",
                    "rssi_dbm": -55,
                    "rate_mbps": 1.0,
                    "modulation": "DSSS",
                    "rate_desc": "1.0 Mbps DSSS",
                    "serial_number": "WIFI_MOD_TEST",
                    "messages": [{"type": "Basic ID", "id": "WIFI_MOD_TEST"}]
                }
                enc_id = tracker.update_with_packet(pkt1)
            
            # Send 1 packet with 6.0 Mbps OFDM
            pkt2 = {
                "timestamp": 1005.0,
                "transport": "wifi",
                "channel": 6,
                "mac": "60:60:1F:AA:BB:CC",
                "rssi_dbm": -53,
                "rate_mbps": 6.0,
                "modulation": "OFDM",
                "rate_desc": "6.0 Mbps OFDM",
                "serial_number": "WIFI_MOD_TEST",
                "messages": [{"type": "Basic ID", "id": "WIFI_MOD_TEST"}]
            }
            tracker.update_with_packet(pkt2)
            tracker.finalize_all()

            with sqlite3.connect(tmp.name) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (enc_id,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["dominant_rate_mbps"], 1.0)
                self.assertEqual(row["dominant_modulation"], "DSSS")
                self.assertEqual(row["min_rate_mbps"], 1.0)
                self.assertEqual(row["max_rate_mbps"], 6.0)
                self.assertIn("1.0 Mbps DSSS", row["wifi_rates"])
                self.assertIn("6.0 Mbps OFDM", row["wifi_rates"])

                dist = json.loads(row["phy_rate_dist_json"])
                self.assertEqual(dist["1.0 Mbps DSSS"]["count"], 3)
                self.assertEqual(dist["1.0 Mbps DSSS"]["percent"], 75.0)
                self.assertEqual(dist["6.0 Mbps OFDM"]["count"], 1)
                self.assertEqual(dist["6.0 Mbps OFDM"]["percent"], 25.0)

    def test_rehydrate_db_from_jsonl(self):
        import tempfile
        import json
        import os
        import sqlite3

        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test_rehydrated.db")
        jsonl_path = os.path.join(temp_dir, "rid_packets_20260907.jsonl")

        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "transport": "wifi",
                "counter": 1,
                "mac": "00:0E:8E:9F:62:83",
                "serial": "1744510470",
                "channel": 6,
                "rssi_dbm": -65,
                "timestamp_iso": "2026-09-07T13:10:45.866025+00:00",
                "encounter_id": "ENC-20260907-131045-9F6283",
                "messages_b64": ["AhQxNzQ0NTEwNDcwAAAAAAAAAAAAAAAAAA=="],
            }) + "\n")
            f.write(json.dumps({
                "transport": "wifi",
                "counter": 2,
                "mac": "00:0E:8E:9F:62:83",
                "serial": "1744510470",
                "channel": 6,
                "rssi_dbm": -62,
                "timestamp_iso": "2026-09-07T13:10:55.000000+00:00",
                "encounter_id": "ENC-20260907-131045-9F6283",
                "messages_b64": ["AhQxNzQ0NTEwNDcwAAAAAAAAAAAAAAAAAA=="],
            }) + "\n")

        count = rehydrate_db_from_jsonl(db_path=db_path, log_dir=temp_dir, node_id="node-zurich-01")
        self.assertEqual(count, 1)

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM encounters WHERE encounter_id = 'ENC-20260907-131045-9F6283';").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["serial_number"], "1744510470")
            self.assertEqual(row["node_id"], "node-zurich-01")
            self.assertEqual(row["packet_count"], 2)
            self.assertEqual(row["is_active"], 0)
            self.assertEqual(row["first_seen_iso"], "2026-09-07T13:10:45.866025+00:00")

        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_ble5_system_message_timestamp_decoupled_from_reception_time(self):
        """Verify that BLE 5 System Message timestamps (e.g. 2019 epoch) do not corrupt encounter reception time."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            tracker = EncounterTracker(db_path=tmp.name, persist_interval_s=0.0, default_node_id="node-test-01")
            
            rx_ts_1 = 1789700000.0  # Physical reception time (2026)
            rx_ts_2 = 1789700002.0  # 2 seconds later
            
            # BLE 5 packet carrying System Message with claimed 2019 epoch timestamp
            pkt1 = {
                "timestamp": rx_ts_1,
                "reception_timestamp": rx_ts_1,
                "transport": "bt5",
                "channel": "BLE Ch 37",
                "mac": "ED:28:BE:34:3E:6A",
                "rssi_dbm": -55,
                "serial_number": "BLE5_SYS_TEST",
                "messages": [
                    {
                        "msg_type": 0,
                        "type": "Basic ID",
                        "id": "BLE5_SYS_TEST",
                        "ua_type_name": "Helicopter / Multirotor",
                    },
                    {
                        "msg_type": 1,
                        "type": "Location",
                        "lat": 47.3780,
                        "lon": 8.5410,
                        "geodetic_altitude_m": 450.0,
                        "speed_mps": 12.0,
                        "direction_deg": 90,
                    },
                    {
                        "msg_type": 4,
                        "type": "System",
                        "pilot_lat": 47.3769,
                        "pilot_lon": 8.5417,
                        "pilot_alt_m": 430.0,
                        "system_timestamp_epoch": 1546300818,  # Claimed 2019-01-01 00:00:18 UTC
                        "system_timestamp_iso": "2019-01-01T00:00:18+00:00",
                    }
                ]
            }
            
            enc_id1 = tracker.update_with_packet(pkt1)
            self.assertTrue(enc_id1.startswith("ENC-2026"), f"Encounter ID {enc_id1} should be timestamped in 2026, not 2019")
            
            # Send 2nd packet
            pkt2 = dict(pkt1)
            pkt2["timestamp"] = rx_ts_2
            pkt2["reception_timestamp"] = rx_ts_2
            enc_id2 = tracker.update_with_packet(pkt2)
            self.assertEqual(enc_id1, enc_id2, "Encounter should not split on claimed system timestamps")
            
            tracker.finalize_all()
            
            with sqlite3.connect(tmp.name) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM encounters WHERE encounter_id = ?", (enc_id1,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["first_seen"], rx_ts_1)
                self.assertEqual(row["last_seen"], rx_ts_2)
                self.assertEqual(row["duration_s"], 2.0)
                self.assertEqual(row["packet_count"], 2)
                self.assertTrue(row["first_seen_iso"].startswith("2026-"), f"first_seen_iso {row['first_seen_iso']} should start with 2026")
                self.assertTrue(row["last_seen_iso"].startswith("2026-"), f"last_seen_iso {row['last_seen_iso']} should start with 2026")

    def test_scanner_config_imports_and_loading(self):
        """Verify that load_scanner_config is genuinely imported and loads config parameters from disk."""
        import scanner.combined_rid_listener as crl
        # Verify it is not a dummy lambda
        self.assertNotEqual(crl.load_scanner_config.__name__, "<lambda>", "load_scanner_config must not be a fallback lambda")

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
            custom_cfg = {
                "node_id": "test-remote-node-99",
                "name": "Remote Station 99",
                "latitude": 46.9480,
                "longitude": 7.4474,
                "altitude_m": 540.0,
                "hub_ws_url": "ws://remote-hub.local:8000/stream/node",
                "ble_mode": "extended",
            }
            json.dump(custom_cfg, tmp)
            tmp.flush()

            loaded = crl.load_scanner_config(tmp.name)
            self.assertIsInstance(loaded, dict)
            self.assertGreater(len(loaded), 0, "Config dictionary must not be empty")
            self.assertEqual(loaded.get("node_id"), "test-remote-node-99")
            self.assertEqual(loaded.get("hub_ws_url"), "ws://remote-hub.local:8000/stream/node")
            self.assertAlmostEqual(loaded.get("latitude"), 46.9480)
            self.assertAlmostEqual(loaded.get("longitude"), 7.4474)

            os.unlink(tmp.name)

    def test_pcap_global_and_record_headers(self):
        """Verify standard 24-byte PCAP global header and 16-byte record header construction."""
        from scanner.pcap_logger import (
            build_pcap_global_header,
            build_pcap_record_header,
            DLT_IEEE802_11_RADIO,
            DLT_NORDIC_BLE,
            PCAP_MAGIC_MICROSECONDS,
            PCAP_GLOBAL_HEADER_LEN,
            PCAP_RECORD_HEADER_LEN,
        )
        # Wi-Fi Radiotap Header
        hdr_wifi = build_pcap_global_header(DLT_IEEE802_11_RADIO)
        self.assertEqual(len(hdr_wifi), PCAP_GLOBAL_HEADER_LEN)
        magic, v_maj, v_min, tz, sig, snaplen, net = struct.unpack("<IHHiIII", hdr_wifi)
        self.assertEqual(magic, PCAP_MAGIC_MICROSECONDS)
        self.assertEqual(v_maj, 2)
        self.assertEqual(v_min, 4)
        self.assertEqual(net, DLT_IEEE802_11_RADIO)

        # Bluetooth Header
        hdr_ble = build_pcap_global_header(DLT_NORDIC_BLE)
        self.assertEqual(len(hdr_ble), PCAP_GLOBAL_HEADER_LEN)
        _, _, _, _, _, _, net_ble = struct.unpack("<IHHiIII", hdr_ble)
        self.assertEqual(net_ble, DLT_NORDIC_BLE)

        # Record header
        ts = 1790683200.123456
        rec_hdr = build_pcap_record_header(ts, 120)
        self.assertEqual(len(rec_hdr), PCAP_RECORD_HEADER_LEN)
        sec, usec, incl_len, orig_len = struct.unpack("<IIII", rec_hdr)
        self.assertEqual(sec, 1790683200)
        self.assertEqual(usec, 123456)
        self.assertEqual(incl_len, 120)
        self.assertEqual(orig_len, 120)

    def test_daily_node_pcap_logger_separation_and_rotation(self):
        """Verify that DailyNodePcapLogger creates 1 daily file for Bluetooth and 1 for Wi-Fi per node."""
        from scanner.pcap_logger import DailyNodePcapLogger, DLT_IEEE802_11_RADIO, DLT_NORDIC_BLE

        with tempfile.TemporaryDirectory() as tmp_dir:
            node_id = "sensor-node-ch"
            logger = DailyNodePcapLogger(base_dir=tmp_dir, node_id=node_id, rotate_daily=True, quiet=True)

            # Day 1: 2026-09-28 12:00:00 UTC (1790596800.0)
            ts_day1 = 1790596800.0
            wifi_pkt_day1 = b"\x00\x00\x08\x00\x00\x00\x00\x00WIFI_RAW_FRAME_1"
            ble_pkt_day1 = b"\x00\x0a\x00\x02\x00\x00\x06\x0a\x01\x25\x37\x01\x00\x00\x00\x00\x00BLE_RAW_PKT_1"

            logger.write_packet("wifi", wifi_pkt_day1, ts=ts_day1)
            logger.write_packet("ble", ble_pkt_day1, ts=ts_day1)

            # Day 2: 2026-09-29 12:00:00 UTC (1790683200.0)
            ts_day2 = 1790683200.0
            wifi_pkt_day2 = b"\x00\x00\x08\x00\x00\x00\x00\x00WIFI_RAW_FRAME_2"
            ble_pkt_day2 = b"\x00\x0a\x00\x02\x00\x00\x06\x0a\x01\x25\x37\x01\x00\x00\x00\x00\x00BLE_RAW_PKT_2"

            logger.write_packet("wifi", wifi_pkt_day2, ts=ts_day2)
            logger.write_packet("ble", ble_pkt_day2, ts=ts_day2)
            logger.close()

            # Check files created in directory
            files = sorted(os.listdir(tmp_dir))
            expected_files = [
                f"{node_id}_ble_20260928.pcap",
                f"{node_id}_ble_20260929.pcap",
                f"{node_id}_wifi_20260928.pcap",
                f"{node_id}_wifi_20260929.pcap",
            ]
            self.assertEqual(files, expected_files, f"Expected daily files per node, found: {files}")

            # Verify contents of Wi-Fi Day 1 file
            wifi_day1_path = os.path.join(tmp_dir, f"{node_id}_wifi_20260928.pcap")
            with open(wifi_day1_path, "rb") as f:
                hdr = f.read(24)
                magic, _, _, _, _, _, net = struct.unpack("<IHHiIII", hdr)
                self.assertEqual(magic, 0xa1b2c3d4)
                self.assertEqual(net, DLT_IEEE802_11_RADIO)
                rec = f.read(16)
                s, us, ilen, _ = struct.unpack("<IIII", rec)
                self.assertEqual(s, int(ts_day1))
                self.assertEqual(ilen, len(wifi_pkt_day1))
                self.assertEqual(f.read(ilen), wifi_pkt_day1)
                self.assertEqual(len(f.read()), 0, "No trailing unexpected bytes")

            # Verify contents of BLE Day 1 file
            ble_day1_path = os.path.join(tmp_dir, f"{node_id}_ble_20260928.pcap")
            with open(ble_day1_path, "rb") as f:
                hdr = f.read(24)
                magic, _, _, _, _, _, net = struct.unpack("<IHHiIII", hdr)
                self.assertEqual(magic, 0xa1b2c3d4)
                self.assertEqual(net, DLT_NORDIC_BLE)
                rec = f.read(16)
                s, us, ilen, _ = struct.unpack("<IIII", rec)
                self.assertEqual(s, int(ts_day1))
                self.assertEqual(ilen, len(ble_pkt_day1))
                self.assertEqual(f.read(ilen), ble_pkt_day1)

    def test_pcap_append_to_existing_file(self):
        """Verify that reopening a logger appends packet records without duplicating the 24-byte global header."""
        from scanner.pcap_logger import DailyNodePcapLogger

        with tempfile.TemporaryDirectory() as tmp_dir:
            node_id = "test-node"
            ts = 1790683200.0  # 2026-09-29

            logger1 = DailyNodePcapLogger(base_dir=tmp_dir, node_id=node_id, rotate_daily=True, quiet=True)
            logger1.write_packet("wifi", b"PACKET_1", ts=ts)
            logger1.close()

            pcap_path = os.path.join(tmp_dir, f"{node_id}_wifi_20260929.pcap")
            size_after_one = os.path.getsize(pcap_path)
            self.assertEqual(size_after_one, 24 + 16 + len(b"PACKET_1"))

            # Re-open in second logger instance
            logger2 = DailyNodePcapLogger(base_dir=tmp_dir, node_id=node_id, rotate_daily=True, quiet=True)
            logger2.write_packet("wifi", b"PACKET_2_LONGER", ts=ts)
            logger2.close()

            size_after_two = os.path.getsize(pcap_path)
            self.assertEqual(size_after_two, size_after_one + 16 + len(b"PACKET_2_LONGER"))

            # Read back both packets
            with open(pcap_path, "rb") as f:
                hdr = f.read(24)
                magic, _, _, _, _, _, _ = struct.unpack("<IHHiIII", hdr)
                self.assertEqual(magic, 0xa1b2c3d4)

                # Packet 1
                rec1 = f.read(16)
                _, _, len1, _ = struct.unpack("<IIII", rec1)
                self.assertEqual(f.read(len1), b"PACKET_1")

                # Packet 2
                rec2 = f.read(16)
                _, _, len2, _ = struct.unpack("<IIII", rec2)
                self.assertEqual(f.read(len2), b"PACKET_2_LONGER")

    def test_unified_telemetry_logger_with_daily_pcap(self):
        """Verify that UnifiedTelemetryLogger logs Wi-Fi and Bluetooth to daily PCAPs per node."""
        from scanner.combined_rid_listener import UnifiedTelemetryLogger

        with tempfile.TemporaryDirectory() as tmp_dir:
            node_id = "node-alpha-01"
            utl = UnifiedTelemetryLogger(
                node_id=node_id,
                pcap_dir=tmp_dir,
                db_path=None,
                quiet=True,
            )
            self.assertIsNotNone(utl.pcap_logger)

            ts = 1790683200.0  # 2026-09-29
            # Wi-Fi event with raw_hex
            wifi_event = {
                "timestamp": ts,
                "transport": "wifi",
                "mac": "11:22:33:44:55:66",
                "raw_hex": "0000080000000000AABBCCDDEEFF",
            }
            utl.process_event(wifi_event)

            # BLE event with raw_bytes
            ble_event = {
                "timestamp": ts + 0.5,
                "transport": "bt5",
                "mac": "AA:BB:CC:DD:EE:FF",
                "raw_bytes": b"\x00\x0a\x00\x02\x00\x00\x06\x0a\x01\x25\x37\x01\x00\x00\x00\x00\x00PAYLOAD",
            }
            utl.process_event(ble_event)

            stats = utl.pcap_logger.get_stats()
            self.assertEqual(stats["wifi_packets"], 1)
            self.assertEqual(stats["ble_packets"], 1)
            self.assertEqual(stats["node_id"], node_id)

            utl.close()

            # Verify files on disk
            wifi_file = os.path.join(tmp_dir, f"{node_id}_wifi_20260929.pcap")
            ble_file = os.path.join(tmp_dir, f"{node_id}_ble_20260929.pcap")
            self.assertTrue(os.path.exists(wifi_file), f"Wi-Fi PCAP {wifi_file} should exist")
            self.assertTrue(os.path.exists(ble_file), f"BLE PCAP {ble_file} should exist")

    def test_central_multi_node_pcap_logging(self):
        """Verify that DailyNodePcapLogger can record packets from multiple nodes into distinct per-node daily files."""
        from scanner.pcap_logger import DailyNodePcapLogger

        with tempfile.TemporaryDirectory() as tmp_dir:
            central_logger = DailyNodePcapLogger(base_dir=tmp_dir, rotate_daily=True, quiet=True)
            ts = 1790683200.0  # 2026-09-29

            # Packet from Node 1 (Wi-Fi)
            central_logger.log_event({
                "node_id": "sensor-node-01",
                "transport": "wifi",
                "timestamp": ts,
                "mac": "11:11:11:11:11:11",
                "raw_hex": "0000080000000000AABBCCDDEEFF",
            })

            # Packet from Node 1 (BLE)
            central_logger.log_event({
                "node_id": "sensor-node-01",
                "transport": "bt5",
                "timestamp": ts + 1.0,
                "mac": "11:11:11:11:11:11",
                "raw_hex": "000a00020000060a012537010000000000D6BE898E0206111111111111",
            })

            # Packet from Node 2 (Wi-Fi)
            central_logger.log_event({
                "node_id": "sensor-node-02",
                "transport": "wifi",
                "timestamp": ts + 2.0,
                "mac": "22:22:22:22:22:22",
                "raw_hex": "0000080000000000222222222222",
            })

            # Packet from Node 2 (BLE)
            central_logger.log_event({
                "node_id": "sensor-node-02",
                "transport": "bt4",
                "timestamp": ts + 3.0,
                "mac": "22:22:22:22:22:22",
                "raw_hex": "000a00020000060a012537010000000000D6BE898E0206222222222222",
            })

            central_logger.close()

            files = sorted(os.listdir(tmp_dir))
            expected = [
                "sensor-node-01_ble_20260929.pcap",
                "sensor-node-01_wifi_20260929.pcap",
                "sensor-node-02_ble_20260929.pcap",
                "sensor-node-02_wifi_20260929.pcap",
            ]
            self.assertEqual(files, expected, f"Expected 4 distinct files for 2 nodes, found {files}")

    def test_encounter_tracker_zero_values_and_canonical_envelope(self):
        """Verifies that counter=0 and rssi_dbm=0 are preserved and not clobbered by falsy checks."""
        tmp_db = tempfile.mktemp(prefix="test_zero_enc_", suffix=".db")
        try:
            tracker = EncounterTracker(db_path=tmp_db, persist_interval_s=0.0)
            pkt = {
                "timestamp": 1788800000.0,
                "transport": "wifi",
                "channel": 6,
                "mac": "00:11:22:33:44:55",
                "serial_number": "ZERO-VAL-DRONE-01",
                "counter": 0,
                "msg_counter": 128,
                "rssi_dbm": 0,
                "rssi": -90,
                "messages": [
                    {
                        "type": "Basic ID",
                        "id": "ZERO-VAL-DRONE-01",
                        "id_type": 1,
                    },
                    {
                        "type": "Location",
                        "lat": 47.3769,
                        "lon": 8.5417,
                        "geodetic_altitude_m": 450.0,
                        "speed_mps": 5.0,
                        "direction_deg": 180.0,
                    }
                ],
            }
            eid = tracker.update_with_packet(pkt)
            self.assertIsNotNone(eid)
            enc = tracker.active_encounters.get("00:11:22:33:44:55")
            self.assertIsNotNone(enc)
            self.assertEqual(enc.get("counter"), 0)
            self.assertEqual(enc.get("last_rssi"), 0)
            self.assertEqual(enc.get("serial_number"), "ZERO-VAL-DRONE-01")
            self.assertTrue(len(enc.get("trajectory", [])) >= 1)
            first_pt = enc["trajectory"][0]
            # pt is [lat, lon, alt, spd, heading, round(ts, 2), h_m, h_type, p_alt, v_spd, pt_rssi, pt_counter]
            self.assertEqual(first_pt[10], 0, "Point RSSI should strictly be 0, not None or fallback")
            self.assertEqual(first_pt[11], 0, "Point counter should strictly be 0, not None or fallback")
            tracker.finalize_all()
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)

    def test_unknown_version_messages_not_parsed_as_known(self):
        """Verify that messages with unknown protocol versions (e.g. v3, v4) are NOT parsed into known fields."""
        # 1. Location message with unknown proto_ver = 3 (header 0x13)
        fake_loc_payload = bytes([0x13]) + b'\xAA\xBB\xCC\xDD' * 6
        decoded_loc = decode_astm_message(fake_loc_payload)
        self.assertIsNotNone(decoded_loc)
        self.assertEqual(decoded_loc["msg_type"], 0x1)
        self.assertEqual(decoded_loc["type"], "Location")
        self.assertEqual(decoded_loc["protocol_version"], 3)
        self.assertFalse(decoded_loc["is_known_version"])
        self.assertIn("Unknown Version", decoded_loc["proto_version_name"])
        self.assertIn("raw_payload_hex", decoded_loc)
        self.assertEqual(decoded_loc["raw_payload_hex"], fake_loc_payload[1:].hex().upper())
        # Ensure no bogus location fields were decoded
        self.assertNotIn("lat", decoded_loc)
        self.assertNotIn("lon", decoded_loc)
        self.assertNotIn("speed_mps", decoded_loc)
        self.assertNotIn("direction_deg", decoded_loc)
        self.assertNotIn("geodetic_altitude_m", decoded_loc)

        # 2. Basic ID message with unknown proto_ver = 4 (header 0x04)
        fake_basic_payload = bytes([0x04]) + b'\x11\x22\x33\x44' * 6
        decoded_basic = decode_astm_message(fake_basic_payload)
        self.assertIsNotNone(decoded_basic)
        self.assertEqual(decoded_basic["msg_type"], 0x0)
        self.assertEqual(decoded_basic["type"], "Basic ID")
        self.assertEqual(decoded_basic["protocol_version"], 4)
        self.assertFalse(decoded_basic["is_known_version"])
        self.assertNotIn("id", decoded_basic)
        self.assertNotIn("ua_type", decoded_basic)

    def test_message_pack_multiple_same_types_unknown_and_known_versions(self):
        """Verify that when a pack has multiple messages of the same type with known and unknown versions,
        only the known version is used to decode the serial and position, while both appear in packet inspection."""
        # Known Basic ID (v2)
        serial_raw = b"KNOWN_DRONE_1234\x00\x00\x00\x00"
        known_basic = bytes([0x02, 0x12]) + serial_raw[:20] + b'\x00\x00\x00'
        self.assertEqual(len(known_basic), 25)

        # Unknown Basic ID (v3) with garbage payload
        unknown_basic = bytes([0x03]) + b'\xFF\xEE\xDD\xCC' * 6
        self.assertEqual(len(unknown_basic), 25)

        # Known Location (v2): lat=47.3769 (473769000), lon=8.5417 (85417000), alt=450m (2900 raw), speed=10m/s (40 raw), dir=180
        known_loc = struct.pack(
            '<BBBBBiiHHH6s',
            0x12, 0x00, 180, 40, 0,
            473769000, 85417000,
            2900, 2900, 2100,
            b'\x00' * 6
        )
        self.assertEqual(len(known_loc), 25)

        # Unknown Location (v5) with arbitrary unknown format bytes
        unknown_loc = bytes([0x15]) + b'\xDE\xAD\xBE\xEF' * 6
        self.assertEqual(len(unknown_loc), 25)

        # Build pack with: [unknown_basic, known_basic, unknown_loc, known_loc]
        # Pack header: 0xF2 (MsgPack v2), single msg size 25 (0x19), count 4
        pack_payload = bytes([0xF2, 0x19, 0x04]) + unknown_basic + known_basic + unknown_loc + known_loc

        parsed, raw_b64 = parse_astm_payload(pack_payload)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(len(raw_b64), 4)

        # Verify inspection list contains all 4
        self.assertFalse(parsed[0]["is_known_version"])  # unknown_basic
        self.assertTrue(parsed[1]["is_known_version"])   # known_basic
        self.assertEqual(parsed[1]["id"], "KNOWN_DRONE_1234")
        self.assertFalse(parsed[2]["is_known_version"])  # unknown_loc
        self.assertTrue(parsed[3]["is_known_version"])   # known_loc
        self.assertAlmostEqual(parsed[3]["lat"], 47.3769, places=4)
        self.assertAlmostEqual(parsed[3]["lon"], 8.5417, places=4)

        # Now test EncounterTracker with this packet
        tmp_db = f"/tmp/test_multi_ver_{int(time.time()*1000)}.db"
        try:
            tracker = EncounterTracker(db_path=tmp_db, timeout_s=300.0)
            pkt = {
                "mac": "AA:BB:CC:11:22:33",
                "timestamp": time.time(),
                "transport": "bt5",
                "channel": 37,
                "rssi_dbm": -65,
                "messages": parsed,
                "messages_b64": raw_b64,
            }
            eid = tracker.update_with_packet(pkt)
            self.assertIsNotNone(eid)
            enc = tracker.active_encounters.get("AA:BB:CC:11:22:33")
            self.assertIsNotNone(enc)
            # Serial must be from the known version
            self.assertEqual(enc.get("serial_number"), "KNOWN_DRONE_1234")
            # Trajectory must have the fix from the known version location
            self.assertEqual(len(enc.get("trajectory", [])), 1)
            fix = enc["trajectory"][0]
            self.assertAlmostEqual(fix[0], 47.3769, places=4)
            self.assertAlmostEqual(fix[1], 8.5417, places=4)
            self.assertAlmostEqual(fix[2], 450.0, places=1)
            self.assertAlmostEqual(fix[3], 10.0, places=1)
            self.assertEqual(fix[4], 180)
            tracker.finalize_all()
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)


if __name__ == "__main__":
    unittest.main()

