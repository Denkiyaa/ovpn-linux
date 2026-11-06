#!/bin/sh
# OVPN Tray kurulumu - bir kez çalıştır:  sudo ./setup.sh
set -e
if [ "$(id -u)" -ne 0 ] || [ -z "$SUDO_USER" ]; then
    echo "Kullanım: sudo ./setup.sh" >&2
    exit 1
fi
U="$SUDO_USER"
HOME_DIR=$(getent passwd "$U" | cut -d: -f6)
APP_DIR=$(cd "$(dirname "$0")" && pwd)

# 1) bağımlılıklar
pacman -S --needed --noconfirm openvpn python-pyqt6

# 2) polkit kuralı: kullanıcı yalnızca OVPN Tray birimlerini şifresiz başlatıp durdurabilir
install -d -m 750 /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-ovpn-tray.rules <<RULES
// OVPN Tray: $U kullanıcısı OVPN Tray VPN birimlerini şifresiz başlatıp durdurabilir
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "$U") {
        var unit = action.lookup("unit");
        var verb = action.lookup("verb");
        if (/^openvpn-client@ovpntray_[a-z0-9_]+\.service$/.test(unit) &&
            (verb == "start" || verb == "stop" || verb == "restart")) {
            return polkit.Result.YES;
        }
    }
});
RULES

# 3) başlat menüsü kısayolu
DESKTOP="$HOME_DIR/.local/share/applications/ovpn-tray.desktop"
install -d -o "$U" -g "$(id -gn "$U")" "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=OVPN Tray
Comment=OpenVPN bağlantılarını yönet
Exec=/usr/bin/python3 $APP_DIR/ovpn_tray.py
Icon=network-vpn
Terminal=false
Categories=Network;
DESK
chown "$U:$(id -gn "$U")" "$DESKTOP"

echo "Kurulum tamam. Başlat menüsünden 'OVPN Tray' ile açabilirsin."
