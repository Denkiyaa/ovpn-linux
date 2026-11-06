#!/usr/bin/env python3
"""OVPN Tray - sistem tepsisinde çalışan basit OpenVPN arayüzü.

Her profil /etc/openvpn/client/ovpntray_<ad>.conf olarak kurulur (pkexec ile)
ve systemd'nin openvpn-client@ovpntray_<ad>.service birimiyle çalıştırılır.
Aynı anda tek profil bağlı olur.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt, QTimer, QProcess
from PyQt6.QtGui import QAction, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

APP_NAME = "OVPN Tray"
CONF_DIR = Path("/etc/openvpn/client")
PREFIX = "ovpntray_"
STATE_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ovpn-tray" / "state.json"
SOCKET_NAME = f"ovpn-tray-{os.getuid()}"

STATES = {
    "connected": ("Bağlı", "#2e9d4f"),
    "connecting": ("Bağlanıyor…", "#d08a14"),
    "failed": ("Bağlantı hatası", "#c8372d"),
    "disconnected": ("Bağlı değil", "#7d8590"),
}
TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")


def unit_name(slug):
    return f"openvpn-client@{PREFIX}{slug}.service"


def conf_path(slug):
    return CONF_DIR / f"{PREFIX}{slug}.conf"


def make_slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.translate(TR).lower()).strip("_")
    return s or "profil"


def installed_slugs():
    try:
        return sorted(p.name[len(PREFIX):-5] for p in CONF_DIR.glob(f"{PREFIX}*.conf"))
    except OSError:
        return []


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def unit_props(slug):
    try:
        out = subprocess.run(
            ["systemctl", "show", unit_name(slug), "-p", "ActiveState",
             "-p", "ActiveEnterTimestampMonotonic", "-p", "InvocationID"],
            capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def unit_state(props):
    a = props.get("ActiveState", "inactive")
    if a == "active":
        return "connected"
    if a in ("activating", "reloading", "deactivating"):
        return "connecting"
    if a == "failed":
        return "failed"
    return "disconnected"


def unit_log(slug, invocation):
    """Birimin son çalıştırmasına ait log satırları: [(saat, mesaj)]."""
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit_name(slug), "-n", "400", "-o", "json", "--no-pager",
             "--output-fields=MESSAGE,__REALTIME_TIMESTAMP,_SYSTEMD_INVOCATION_ID,INVOCATION_ID"],
            capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = []
    for raw in out.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        inv = e.get("_SYSTEMD_INVOCATION_ID") or e.get("INVOCATION_ID")
        if invocation and inv != invocation:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):  # binary mesaj
            msg = bytes(msg).decode(errors="replace")
        ts = datetime.fromtimestamp(int(e.get("__REALTIME_TIMESTAMP", 0)) / 1e6).strftime("%H:%M:%S")
        lines.append((ts, msg or ""))
    return lines


def fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def make_icon(color):
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(4, 4, 56, 56)
    # ortada beyaz kilit
    p.setBrush(QColor("white"))
    p.drawRoundedRect(20, 30, 24, 18, 3, 3)
    pen = p.pen()
    pen.setStyle(Qt.PenStyle.SolidLine)
    pen.setColor(QColor("white"))
    pen.setWidth(4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(24, 16, 16, 22, 0, 180 * 16)
    p.drawLine(24, 27, 24, 31)
    p.drawLine(40, 27, 40, 31)
    p.end()
    return QIcon(pm)


STYLE = """
QWidget#root { background: #14171c; }
QLabel { color: #e6e8eb; }
QLabel#muted { color: #8b949e; }
QLabel#title { color: #8b949e; font-size: 11pt; letter-spacing: 2px; }
QLabel#status { font-size: 18pt; font-weight: 600; }
QLabel#detail { color: #a7b0ba; font-size: 10.5pt; }
QLabel#section { color: #8b949e; font-size: 9pt; font-weight: 600; letter-spacing: 1px; }
QComboBox {
    background: #1f242b; color: #e6e8eb; border: 1px solid #2f3742; border-radius: 10px;
    padding: 9px 12px; font-size: 11pt;
}
QComboBox:hover { border-color: #3d4754; }
QComboBox::drop-down { border: none; width: 34px; }
QComboBox::down-arrow { image: url(%ARROW%); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: #1f242b; color: #e6e8eb; border: 1px solid #2f3742;
    selection-background-color: #2b3440; outline: none; padding: 4px;
}
QPushButton#ghost {
    background: transparent; color: #a7b0ba; border: 1px solid #2f3742; border-radius: 8px;
    padding: 6px 12px;
}
QPushButton#ghost:hover { background: #1f242b; color: #e6e8eb; }
QPushButton#ghost:disabled { color: #4a525c; border-color: #252b33; }
QPushButton#main {
    border: none; border-radius: 12px; padding: 13px; font-size: 12pt; font-weight: 600; color: white;
}
QPushButton#main:disabled { background: #2a3038; color: #5d6670; }
QPlainTextEdit {
    background: #0e1014; color: #b8c0c9; border: 1px solid #232931; border-radius: 10px;
    padding: 8px; font-size: 8.5pt;
    selection-background-color: #2b3440;
}
QScrollBar:vertical { background: transparent; width: 8px; margin: 4px; }
QScrollBar::handle:vertical { background: #2f3742; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""

def _arrow_file():
    """Açılır liste oku için küçük bir SVG dosyası üretir."""
    path = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ovpn-tray" / "arrow.svg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">'
                    '<path d="M2 4l4 4 4-4" fill="none" stroke="#a7b0ba" stroke-width="1.8" '
                    'stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return str(path)


BTN_COLORS = {"connect": ("#2e9d4f", "#35b05a"), "disconnect": ("#c8372d", "#dc4a3f")}


class StatusRing(QWidget):
    """Büyük yuvarlak durum göstergesi; bağlanırken nabız gibi atar."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(150, 150)
        self.color = QColor(STATES["disconnected"][1])
        self.pulse = False
        self.phase = 0.0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._tick)
        self.anim.start(40)

    def set_state(self, st):
        self.color = QColor(STATES[st][1])
        self.pulse = st == "connecting"
        self.update()

    def _tick(self):
        if self.pulse:
            self.phase = (self.phase + 0.04) % 1.0
            self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self.color)
        # dış hale
        glow = QColor(c)
        glow.setAlphaF(0.10 + (0.18 * (1 - self.phase) if self.pulse else 0.05))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        r = 75 if not self.pulse else 60 + 15 * self.phase
        p.drawEllipse(QPointF(75, 75), r, r)
        # halka
        ring = QColor(c)
        ring.setAlphaF(0.25)
        p.setBrush(ring)
        p.drawEllipse(QPointF(75, 75), 56, 56)
        p.setBrush(c)
        p.drawEllipse(QPointF(75, 75), 46, 46)
        # kilit
        pen = QPen(QColor("white"), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(62, 52, 26, 30, 0, 180 * 16)
        p.drawLine(62, 67, 62, 72)
        p.drawLine(88, 67, 88, 72)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(56, 70, 38, 28, 5, 5)
        p.setBrush(c)
        p.drawEllipse(QPointF(75, 82), 4, 4)
        p.end()


class TrayApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        self.setStyleSheet(STYLE.replace("%ARROW%", _arrow_file()))
        self.setFixedWidth(380)
        self.resize(380, 680)
        self.state = load_state()
        self.procs = []
        self.last_state = {}
        self.shown_log_key = None

        title = QLabel("OPENVPN")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.ring = StatusRing()
        self.status = QLabel()
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel()
        self.detail.setObjectName("detail")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)

        prof_label = QLabel("PROFİL")
        prof_label.setObjectName("section")
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self.on_selection)
        add_btn = QPushButton("＋  Dosya ekle")
        add_btn.setObjectName("ghost")
        add_btn.clicked.connect(self.add_profile)
        self.del_btn = QPushButton("Sil")
        self.del_btn.setObjectName("ghost")
        self.del_btn.clicked.connect(self.remove_profile)
        prof_btns = QHBoxLayout()
        prof_btns.setSpacing(8)
        prof_btns.addWidget(add_btn, 1)
        prof_btns.addWidget(self.del_btn)

        self.connect_btn = QPushButton("Bağlan")
        self.connect_btn.setObjectName("main")
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.clicked.connect(lambda: self.toggle())

        log_label = QLabel("BAĞLANTI GÜNLÜĞÜ")
        log_label.setObjectName("section")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setPlaceholderText("Bağlanınca adımlar burada satır satır görünür.")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 22)
        lay.setSpacing(0)
        lay.addWidget(title)
        lay.addSpacing(14)
        lay.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(10)
        lay.addWidget(self.status)
        lay.addSpacing(2)
        lay.addWidget(self.detail)
        lay.addSpacing(22)
        lay.addWidget(prof_label)
        lay.addSpacing(6)
        lay.addWidget(self.combo)
        lay.addSpacing(8)
        lay.addLayout(prof_btns)
        lay.addSpacing(16)
        lay.addWidget(self.connect_btn)
        lay.addSpacing(20)
        lay.addWidget(log_label)
        lay.addSpacing(6)
        lay.addWidget(self.log, 1)

        self.tray = QSystemTrayIcon(make_icon(STATES["disconnected"][1]), self)
        self.tray_menu = QMenu()
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.reload_profiles(select=self.state.get("last"))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    # --- profiller ---
    def profiles(self):
        names = self.state.get("profiles", {})
        return [(s, names.get(s, {}).get("name", s)) for s in installed_slugs()]

    def reload_profiles(self, select=None):
        self.combo.blockSignals(True)
        self.combo.clear()
        for slug, name in self.profiles():
            self.combo.addItem(name, slug)
        if select is not None:
            i = self.combo.findData(select)
            if i >= 0:
                self.combo.setCurrentIndex(i)
        self.combo.blockSignals(False)
        self.on_selection()

    def selected(self):
        return self.combo.currentData()

    def active_slug(self):
        """Şu an bağlı ya da bağlanmakta olan profil."""
        for slug, st in self.last_state.items():
            if st in ("connected", "connecting"):
                return slug
        return None

    def on_selection(self):
        slug = self.selected()
        if slug:
            self.state["last"] = slug
            save_state(self.state)
        self.shown_log_key = None
        self.refresh()

    def add_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "OpenVPN profili seç", str(Path.home()),
            "OpenVPN profili (*.ovpn *.conf);;Tüm dosyalar (*)")
        if not path:
            return
        try:
            data = Path(path).read_bytes().replace(b"\r\n", b"\n")
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Dosya okunamadı:\n{e}")
            return
        if not re.search(rb"^\s*remote\s", data, re.M) and b"<connection>" not in data:
            QMessageBox.warning(self, APP_NAME, "Bu dosya bir OpenVPN profili gibi görünmüyor (remote satırı yok).")
            return
        name = Path(path).stem
        slug = make_slug(name)
        if slug in installed_slugs():
            r = QMessageBox.question(self, APP_NAME, f"“{name}” zaten listede. Üzerine yazılsın mı?")
            if r != QMessageBox.StandardButton.Yes:
                return
        fd, tmp = tempfile.mkstemp(prefix="ovpn-tray-", suffix=".conf")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        restart = self.last_state.get(slug) in ("connected", "connecting")

        def done(code):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if code == 0:
                self.state.setdefault("profiles", {})[slug] = {"name": name, "source": path}
                save_state(self.state)
                self.reload_profiles(select=slug)
                if restart:
                    self.run(["systemctl", "restart", "--no-block", unit_name(slug)], "Yeniden bağlanma")

        self.run(["pkexec", "install", "-D", "-m", "600", "-o", "root", "-g", "root", tmp, str(conf_path(slug))],
                 "Profil ekleme", done)

    def remove_profile(self):
        slug = self.selected()
        if not slug:
            return
        name = self.combo.currentText()
        if QMessageBox.question(self, APP_NAME, f"“{name}” listeden silinsin mi?") != QMessageBox.StandardButton.Yes:
            return
        if self.last_state.get(slug) in ("connected", "connecting"):
            subprocess.run(["systemctl", "stop", unit_name(slug)], capture_output=True)

        def done(code):
            if code == 0:
                self.state.get("profiles", {}).pop(slug, None)
                save_state(self.state)
                self.reload_profiles()

        self.run(["pkexec", "rm", "-f", str(conf_path(slug))], "Profil silme", done)

    # --- bağlantı ---
    def toggle(self, slug=None):
        slug = slug or self.selected()
        if not slug:
            return
        if self.last_state.get(slug) in ("connected", "connecting"):
            self.run(["systemctl", "stop", "--no-block", unit_name(slug)], "Bağlantıyı kesme")
            return
        other = self.active_slug()
        if other and other != slug:  # tek seferde tek VPN
            subprocess.run(["systemctl", "stop", unit_name(other)], capture_output=True)
        self.shown_log_key = None
        self.run(["systemctl", "start", "--no-block", unit_name(slug)], "Bağlanma")

    def run(self, cmd, what, on_done=None):
        p = QProcess(self)

        def finished(code, _status):
            if p in self.procs:
                self.procs.remove(p)
            if code != 0:
                err = bytes(p.readAllStandardError()).decode(errors="replace").strip()
                if code not in (126, 127):  # pkexec penceresi iptal edildi
                    QMessageBox.warning(self, APP_NAME, f"{what} başarısız oldu (kod {code}).\n\n{err}")
            if on_done:
                on_done(code)
            self.refresh()

        p.finished.connect(finished)
        self.procs.append(p)
        p.start(cmd[0], cmd[1:])
        QTimer.singleShot(300, self.refresh)

    # --- yenileme ---
    def refresh(self):
        names = dict(self.profiles())
        props_sel = {}
        for slug in names:
            props = unit_props(slug)
            st = unit_state(props)
            prev = self.last_state.get(slug)
            if prev is not None and prev != st:
                n = names[slug]
                if st == "connected":
                    self.tray.showMessage(APP_NAME, f"{n} bağlandı", QSystemTrayIcon.MessageIcon.Information, 3000)
                elif st == "failed":
                    self.tray.showMessage(APP_NAME, f"{n} bağlanamadı", QSystemTrayIcon.MessageIcon.Warning, 5000)
                elif prev == "connected":
                    self.tray.showMessage(APP_NAME, f"{n} bağlantısı kesildi", QSystemTrayIcon.MessageIcon.Information, 3000)
            self.last_state[slug] = st
            if slug == self.selected():
                props_sel = props
        for gone in set(self.last_state) - set(names):
            del self.last_state[gone]

        slug = self.selected()
        st = self.last_state.get(slug, "disconnected") if slug else "disconnected"
        label, color = STATES[st]
        lines = unit_log(slug, props_sel.get("InvocationID")) if slug and self.isVisible() else []
        extra = []
        if st == "connected":
            ip = next((m.group(1) for _, l in reversed(lines)
                       if (m := re.search(r"net_addr_v4_add: (\d+\.\d+\.\d+\.\d+)", l))), None)
            if ip:
                extra.append(ip)
            try:
                since = int(props_sel.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
                if since:
                    extra.append(fmt_duration(time.monotonic() - since))
            except ValueError:
                pass
        if slug:
            self.status.setText(label)
            self.detail.setText("  ·  ".join(extra) if extra else self.combo.currentText())
        else:
            self.status.setText("Profil yok")
            self.detail.setText("Başlamak için bir .ovpn dosyası ekle")
        self.status.setStyleSheet(f"color: {color if st != 'disconnected' else '#e6e8eb'};")
        self.ring.set_state(st)
        busy = st in ("connected", "connecting")
        self.connect_btn.setEnabled(bool(slug))
        self.connect_btn.setText("Bağlantıyı kes" if busy else "Bağlan")
        base, hover = BTN_COLORS["disconnect" if busy else "connect"]
        self.connect_btn.setStyleSheet(
            f"QPushButton#main {{ background: {base}; }} QPushButton#main:hover {{ background: {hover}; }}"
            "QPushButton#main:disabled { background: #2a3038; color: #5d6670; }")
        self.del_btn.setEnabled(bool(slug))

        if self.isVisible():
            key = (slug, props_sel.get("InvocationID"), len(lines))
            if key != self.shown_log_key:
                self.shown_log_key = key
                sb = self.log.verticalScrollBar()
                at_bottom = sb.value() >= sb.maximum() - 4
                self.log.setPlainText("\n".join(f"{t}  {m}" for t, m in lines))
                if at_bottom:
                    sb.setValue(sb.maximum())

        # tepsi: genel durum
        overall = "disconnected"
        for s in self.last_state.values():
            if s == "connected":
                overall = "connected"
                break
            if s in ("connecting", "failed") and overall == "disconnected":
                overall = s
        self.tray.setIcon(make_icon(STATES[overall][1]))
        self.setWindowIcon(make_icon(STATES[overall][1]))
        act = self.active_slug()
        self.tray.setToolTip(f"{APP_NAME}\n" + (f"{names.get(act, act)}: {STATES[self.last_state[act]][0]}"
                                                 if act else "Bağlı değil"))
        self.rebuild_menu(names)

    def rebuild_menu(self, names):
        m = self.tray_menu
        m.clear()
        if not names:
            a = QAction("Profil yok", m)
            a.setEnabled(False)
            m.addAction(a)
        for slug, name in names.items():
            st = self.last_state.get(slug, "disconnected")
            busy = st in ("connected", "connecting")
            a = QAction(f"{'Kes: ' if busy else 'Bağlan: '}{name}" + (f"  ({STATES[st][0]})" if busy else ""), m)
            a.triggered.connect(lambda _=False, s=slug: self.toggle(s))
            m.addAction(a)
        m.addSeparator()
        show = QAction("Pencereyi göster", m)
        show.triggered.connect(self.show_window)
        m.addAction(show)
        quit_ = QAction("Çıkış", m)
        quit_.triggered.connect(QApplication.instance().quit)
        m.addAction(quit_)

    # --- pencere ---
    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.shown_log_key = None
        self.refresh()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_window()

    def closeEvent(self, ev):
        ev.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("ovpn-tray")
    app.setQuitOnLastWindowClosed(False)

    sock = QLocalSocket()
    sock.connectToServer(SOCKET_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SOCKET_NAME)
    server = QLocalServer()
    server.listen(SOCKET_NAME)

    w = TrayApp()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), w.show_window()))
    if "--hidden" not in sys.argv:
        w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
