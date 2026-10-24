#!/usr/bin/env bash
# SafeLink: turn a Linux laptop (or Raspberry Pi) into an open "EMERGENCY_SOS" Wi-Fi hotspot
# that redirects every phone to the SOS page (captive portal).
#
#   sudo ./linux_captive.sh up   [wlan0]     start the hotspot
#   sudo ./linux_captive.sh down [wlan0]     stop it and give Wi-Fi back to the system
#
# Needs: sudo apt install hostapd dnsmasq     (do this BEFORE going offline)
# Then run the app in another terminal (port 80 so phone connectivity checks are caught):
#   sudo python3 safelink.py --role command --port 80 --captive --portal-ip 192.168.4.1 --no-dns
#
# NOTE: the laptop's Wi-Fi card must support AP mode ("iw list" should show "* AP" under
# supported interface modes). While the hotspot is up, this laptop is NOT connected to other Wi-Fi.
set -euo pipefail

ACTION="${1:-up}"
IFACE="${2:-wlan0}"
IP="192.168.4.1"
SSID="${SSID:-EMERGENCY_SOS}"

case "$ACTION" in
  up)
    for bin in hostapd dnsmasq; do
      command -v "$bin" >/dev/null || { echo "Missing $bin. Run: sudo apt install hostapd dnsmasq"; exit 1; }
    done
    nmcli dev set "$IFACE" managed no 2>/dev/null || true
    rfkill unblock wifi 2>/dev/null || true
    ip addr flush dev "$IFACE"
    ip addr add "$IP/24" dev "$IFACE"
    ip link set "$IFACE" up

    cat > /tmp/safelink-hostapd.conf <<EOF
interface=$IFACE
driver=nl80211
ssid=$SSID
hw_mode=g
channel=6
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
EOF

    # DHCP hands out addresses and points DNS at this laptop; address=/#/IP answers every
    # domain name with this laptop's IP, which is what triggers the "Sign in to network" popup.
    cat > /tmp/safelink-dnsmasq.conf <<EOF
interface=$IFACE
bind-interfaces
dhcp-range=192.168.4.10,192.168.4.250,255.255.255.0,12h
dhcp-option=3,$IP
dhcp-option=6,$IP
address=/#/$IP
no-resolv
EOF

    pkill -f safelink-hostapd.conf 2>/dev/null || true
    pkill -f safelink-dnsmasq.conf 2>/dev/null || true
    sleep 1
    hostapd -B /tmp/safelink-hostapd.conf
    dnsmasq -C /tmp/safelink-dnsmasq.conf
    echo
    echo "Hotspot '$SSID' is up on $IFACE ($IP)."
    echo "Now run:  sudo python3 safelink.py --role command --port 80 --captive --portal-ip $IP --no-dns"
    echo "If dnsmasq fails with 'address already in use', free port 53: sudo systemctl stop systemd-resolved"
    ;;
  down)
    pkill -f safelink-hostapd.conf 2>/dev/null || true
    pkill -f safelink-dnsmasq.conf 2>/dev/null || true
    ip addr flush dev "$IFACE" || true
    nmcli dev set "$IFACE" managed yes 2>/dev/null || true
    echo "Hotspot stopped."
    ;;
  *)
    echo "Usage: sudo $0 up|down [interface]"
    exit 1
    ;;
esac
