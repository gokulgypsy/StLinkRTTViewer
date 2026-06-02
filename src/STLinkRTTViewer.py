"""
RTT Viewer — PySide6 (Qt6) GUI
Uses OpenOCD + gdb-multiarch. No pyocd, no ELF needed.

Requirements:
    sudo apt install openocd gdb-multiarch
    pip install PySide6

Run:
    python stlink_rtt_gui.py
"""

import sys, os, time, queue, shutil, subprocess, threading, socket, argparse
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QLineEdit, QPushButton, QRadioButton,
    QButtonGroup, QTabWidget, QPlainTextEdit, QStatusBar,
    QFileDialog, QMessageBox, QFrame, QSizePolicy, QTextEdit,
    QScrollArea
)
from PySide6.QtCore import Qt, QThread, Signal, QSortFilterProxyModel, QStringListModel
from PySide6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor

# ─────────────────────────────────────────────────────────────────────────────
#  Paths — overridable via CLI args or env vars
# ─────────────────────────────────────────────────────────────────────────────
OPENOCD_BIN      = ""          # resolved at startup
OPENOCD_SCRIPTS  = ""          # resolved at startup
GDB_BIN          = ""          # resolved at startup
DEFAULT_RTT_RAM  = "0x20000000"
DEFAULT_RTT_SIZE = "0x30000"
GDB_PORT, TCL_PORT, TEL_PORT, RTT_PORT = 50000, 50001, 50002, 9090

# Common non-default install locations
OPENOCD_BIN_CANDIDATES = [
    "openocd",                          # system PATH
    "/usr/local/bin/openocd",
    "/opt/openocd/bin/openocd",
    os.path.expanduser("~/.local/bin/openocd"),
]
OPENOCD_SCRIPTS_CANDIDATES = [
    "/usr/share/openocd/scripts",
    "/usr/local/share/openocd/scripts",
    "/opt/openocd/share/openocd/scripts",
    os.path.expanduser("~/.local/share/openocd/scripts"),
]
GDB_CANDIDATES = [
    "arm-none-eabi-gdb",
    "gdb-multiarch",
    "gdb",
    "/usr/local/bin/arm-none-eabi-gdb",
    "/opt/gcc-arm/bin/arm-none-eabi-gdb",
]

def resolve_openocd_bin(override=None):
    if override:
        return override
    for c in OPENOCD_BIN_CANDIDATES:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return "openocd"

def resolve_openocd_scripts(override=None):
    if override:
        return override
    # Try to find scripts next to the binary first
    bin_path = shutil.which(OPENOCD_BIN) or OPENOCD_BIN
    if bin_path:
        # e.g. /usr/bin/openocd -> /usr/share/openocd/scripts
        prefix = os.path.dirname(os.path.dirname(bin_path))
        candidate = os.path.join(prefix, "share", "openocd", "scripts")
        if os.path.isdir(candidate):
            return candidate
    for c in OPENOCD_SCRIPTS_CANDIDATES:
        if os.path.isdir(c):
            return c
    return "/usr/share/openocd/scripts"

def resolve_gdb(override=None):
    if override:
        return override
    for c in GDB_CANDIDATES:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def list_cfgs(subdir):
    d = os.path.join(OPENOCD_SCRIPTS, subdir)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".cfg"))

def find_gdb():
    if GDB_BIN:
        return GDB_BIN
    return resolve_gdb()

def kill_stale():
    for name in ("openocd", "gdb-multiarch", "arm-none-eabi-gdb"):
        subprocess.call(["pkill", "-f", name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)

# ─────────────────────────────────────────────────────────────────────────────
#  Backend engine (QThread)
# ─────────────────────────────────────────────────────────────────────────────
class RTTEngine(QThread):
    log_signal  = Signal(str, str)
    rtt_signal  = Signal(str)
    done_signal = Signal(bool)

    def __init__(self, cfg):
        super().__init__()
        self.cfg      = cfg
        self._stop    = threading.Event()
        self._ocd     = None
        self._gdb     = None
        self._sock    = None
        self.bytes_rx = 0

    def stop(self):
        self._stop.set()
        self._cleanup()

    def _log(self, tag, msg):
        self.log_signal.emit(tag, msg)

    def run(self):
        try:
            kill_stale()
            cfg      = self.cfg
            gdb_bin  = cfg["gdb_bin"]
            iface    = cfg["iface"]
            target   = cfg["target"]
            rtt_addr = cfg.get("rtt_addr") or DEFAULT_RTT_RAM
            if not cfg.get("rtt_addr"):
                self._log("info", f"RTT addr not set — scanning from {rtt_addr}")

            # OpenOCD
            self._log("info", f"OpenOCD: probe={iface}  target={target}")
            ocd_cmd = (
                f"{OPENOCD_BIN} "
                f'-c "gdb_port {GDB_PORT}" '
                f'-c "tcl_port {TCL_PORT}" '
                f'-c "telnet_port {TEL_PORT}" '
                f"-s {OPENOCD_SCRIPTS} "
                f"-f interface/{iface}.cfg "
                f"-f target/{target}.cfg"
            )
            self._ocd = subprocess.Popen(
                ocd_cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            ocd_lines = []
            def _ocd_reader():
                for raw in iter(self._ocd.stdout.readline, b""):
                    l = raw.decode(errors="replace").rstrip()
                    ocd_lines.append(l)
                    if "Error:" in l:   self._log("err",  l.strip())
                    elif "Warn :" in l: self._log("warn", l.strip())
                    else:               self._log("dim",  l.strip())
            threading.Thread(target=_ocd_reader, daemon=True).start()

            if not self._wait_ocd(ocd_lines):
                self._log("err", "OpenOCD failed — wrong probe or target?")
                for l in ocd_lines[-8:]: self._log("dim", f"  {l}")
                self._cleanup()
                self.done_signal.emit(False)
                return

            # GDB
            self._log("info", "Setting up RTT...")
            self._gdb = subprocess.Popen(
                [gdb_bin, "--quiet"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1
            )
            gdb_q = queue.Queue()
            threading.Thread(target=lambda: [
                gdb_q.put(l.rstrip())
                for l in iter(self._gdb.stdout.readline, "")
            ], daemon=True).start()

            cmds = [f"target extended-remote localhost:{GDB_PORT}"]
            if cfg.get("reset_mode", True):
                cmds.append("monitor reset halt")
            cmds += [
                f'monitor rtt setup {rtt_addr} {DEFAULT_RTT_SIZE} "SEGGER RTT"',
                f"monitor rtt server start {RTT_PORT} 0",
                "monitor rtt start",
                "continue",
            ]
            for cmd in cmds:
                if self._stop.is_set(): return
                self._log("dim", f"(gdb) {cmd}")
                self._gdb.stdin.write(cmd + "\n")
                self._gdb.stdin.flush()
                time.sleep(0.7 if "reset halt" in cmd or cmd == "continue" else 0.2)

            self._wait_rtt(gdb_q)

            if not self._connect_rtt():
                self._log("err", "RTT server not reachable.")
                self._cleanup()
                self.done_signal.emit(False)
                return

            self._log("info", "✓ RTT streaming active")
            self.done_signal.emit(True)
            self._stream_rtt()

        except Exception as e:
            self._log("err", f"Engine error: {e}")
            self._cleanup()
            self.done_signal.emit(False)

    def _wait_ocd(self, lines, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for l in lines[-20:]:
                if "Listening on port" in l and str(GDB_PORT) in l:
                    self._log("info", "OpenOCD ready.")
                    return True
            time.sleep(0.25)
        return False

    def _wait_rtt(self, q, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = q.get(timeout=0.3)
                if "port" in line.lower() and "rtt" in line.lower():
                    self._log("info", line.strip())
                    return
            except queue.Empty:
                pass

    def _connect_rtt(self):
        for _ in range(30):
            if self._stop.is_set(): return False
            try:
                self._sock = socket.create_connection(("localhost", RTT_PORT), timeout=2)
                return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        return False

    def _stream_rtt(self):
        self._sock.settimeout(0.1)
        buf = ""
        try:
            while not self._stop.is_set():
                try:
                    data = self._sock.recv(4096)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not data: break
                self.bytes_rx += len(data)
                text = buf + data.decode("utf-8", errors="replace")
                lines = text.split("\n")
                buf = lines[-1]
                for line in lines[:-1]:
                    self.rtt_signal.emit(line)
        finally:
            try: self._sock.close()
            except Exception: pass

    def _cleanup(self):
        try:
            if self._sock: self._sock.close()
        except Exception: pass
        try:
            if self._gdb:
                self._gdb.stdin.write("quit\ny\n")
                self._gdb.stdin.flush()
                self._gdb.terminate()
        except Exception: pass
        try:
            if self._ocd:
                self._ocd.terminate()
                self._ocd.wait(timeout=3)
        except Exception: pass
        kill_stale()

# ─────────────────────────────────────────────────────────────────────────────
#  Searchable ComboBox
# ─────────────────────────────────────────────────────────────────────────────
class SearchComboBox(QComboBox):
    """
    Editable combobox with live search.
    Typing filters the dropdown list without auto-committing.
    User must click an item or press Enter to select.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setCompleter(None)   # disable built-in completer

        self._all_items = []
        self._committed = ""

        self.lineEdit().setPlaceholderText("type to search…")
        self.lineEdit().textEdited.connect(self._on_text_edited)
        self.activated.connect(self._on_activated)

    def setItems(self, items):
        self._all_items = list(items)
        self.blockSignals(True)
        self.clear()
        self.addItems(items)
        self.blockSignals(False)
        if items:
            self._commit(items[0])

    def currentValue(self):
        return self._committed

    def setCurrentValue(self, value):
        if value in self._all_items:
            self._commit(value)

    def _on_text_edited(self, text):
        fl = text.lower()
        filtered = [i for i in self._all_items if fl in i.lower()] if fl else self._all_items
        self.blockSignals(True)
        self.clear()
        self.addItems(filtered)
        self.blockSignals(False)
        self.lineEdit().setText(text)   # keep what user typed
        if filtered:
            self.showPopup()

    def _on_activated(self, index):
        value = self.itemText(index)
        self._commit(value)

    def _commit(self, value):
        self._committed = value
        self.blockSignals(True)
        self.clear()
        self.addItems(self._all_items)
        idx = self._all_items.index(value) if value in self._all_items else 0
        self.setCurrentIndex(idx)
        self.blockSignals(False)
        self.lineEdit().setText(value)

# ─────────────────────────────────────────────────────────────────────────────
#  Stylesheet  — J-Link RTT Viewer look (light grey toolbar, white terminal)
# ─────────────────────────────────────────────────────────────────────────────
QSS = """
QMainWindow, QWidget {
    background: #f0f0f0;
    color: #1a1a1a;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 9pt;
}
/* toolbar strip */
QFrame#toolbar {
    background: #e4e4e4;
    border-bottom: 1px solid #bbb;
}
QFrame#toolbar QLabel {
    color: #333;
    font-weight: bold;
    font-size: 9pt;
}
QComboBox, QLineEdit {
    background: white;
    color: #1a1a1a;
    border: 1px solid #aaa;
    border-radius: 2px;
    padding: 2px 5px;
    selection-background-color: #0078d4;
    selection-color: white;
}
QComboBox:focus, QLineEdit:focus {
    border-color: #0078d4;
}
QComboBox QAbstractItemView {
    background: white;
    color: #1a1a1a;
    border: 1px solid #aaa;
    selection-background-color: #0078d4;
    selection-color: white;
}
QComboBox::drop-down { border: none; width: 18px; }
/* buttons */
QPushButton {
    background: #d6d6d6;
    color: #1a1a1a;
    border: 1px solid #aaa;
    border-radius: 2px;
    padding: 3px 12px;
    font-size: 9pt;
}
QPushButton:hover   { background: #c8c8c8; }
QPushButton:pressed { background: #b8b8b8; }
QPushButton:disabled { color: #bbb; background: #efefef; border-color: #ddd; }
QComboBox:disabled, QLineEdit:disabled {
    background: #f5f5f5; color: #bbb; border-color: #ddd;
}
QRadioButton:disabled { color: #bbb; }
QPushButton#connect {
    background: #0078d4; color: white; border-color: #005fa3;
    font-weight: bold;
}
QPushButton#connect:hover    { background: #006bbf; }
QPushButton#connect:pressed  { background: #005299; }
QPushButton#connect:disabled {
    background: #cce0f5; color: #7aaed6;
    border-color: #aacfee; font-weight: bold;
}
QPushButton#disconnect {
    background: #c42b2b; color: white; border-color: #a02020;
    font-weight: bold;
}
QPushButton#disconnect:hover    { background: #b02525; }
QPushButton#disconnect:pressed  { background: #8a1c1c; }
QPushButton#disconnect:disabled {
    background: #f5cccc; color: #d47a7a;
    border-color: #eeaaaa; font-weight: bold;
}
/* radio */
QRadioButton { color: #1a1a1a; font-size: 9pt; }
QRadioButton::indicator {
    width: 13px; height: 13px;
}
/* tabs — JLink style */
QTabWidget::pane {
    border: none;
    border-top: 1px solid #bbb;
    background: white;
}
QTabBar::tab {
    background: #d8d8d8;
    color: #444;
    padding: 4px 16px;
    border: 1px solid #bbb;
    border-bottom: none;
    margin-right: 2px;
    font-size: 9pt;
}
QTabBar::tab:selected {
    background: white;
    color: #000;
    font-weight: bold;
    border-bottom: 1px solid white;
}
QTabBar::tab:hover:!selected { background: #ccc; }
/* terminal — pure white, monospace, black text */
QPlainTextEdit {
    background: white;
    color: #000;
    border: none;
    font-family: "Courier New", Courier, monospace;
    font-size: 9pt;
    selection-background-color: #0078d4;
    selection-color: white;
}
/* help tab */
QTextEdit {
    background: white;
    color: #1a1a1a;
    border: none;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 9pt;
}
/* scrollbars */
QScrollBar:vertical {
    background: #f0f0f0; width: 12px; border: none;
}
QScrollBar::handle:vertical {
    background: #bbb; border-radius: 6px; min-height: 20px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background: #999; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
/* status bar */
QStatusBar {
    background: #dcdcdc;
    color: #555;
    border-top: 1px solid #bbb;
    font-size: 9pt;
}
QStatusBar QLabel { color: #555; padding: 0 4px; }
/* bottom bar */
QFrame#bottombar {
    background: #e4e4e4;
    border-top: 1px solid #bbb;
}
QFrame#bottombar QLabel { color: #333; }
/* separator */
QFrame[frameShape="5"] { color: #bbb; }
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Help HTML content
# ─────────────────────────────────────────────────────────────────────────────
HELP_HTML = """
<html><body style="font-family:Arial,sans-serif; font-size:9pt; color:#1a1a1a; margin:16px;">

<h2 style="color:#0078d4; margin-bottom:4px;">RTT Viewer — Help &amp; Prerequisites</h2>
<hr style="border:none; border-top:1px solid #ccc; margin-bottom:12px;">

<h3 style="color:#333;">Command-line options</h3>
<p>You can override tool paths when launching:</p>
<pre style="background:#f5f5f5; border:1px solid #ddd; padding:8px; border-radius:3px;">
python stlink_rtt_gui.py --openocd /custom/path/openocd
                         --openocd-scripts /custom/path/openocd/scripts
                         --gdb /custom/path/arm-none-eabi-gdb
</pre>
<p>If not specified, paths are auto-detected from <code>PATH</code> and common install locations.</p>
<p>When built as a single binary with PyInstaller:</p>
<pre style="background:#f5f5f5; border:1px solid #ddd; padding:8px; border-radius:3px;">
./RTTViewer --openocd /opt/openocd/bin/openocd --gdb /opt/gcc-arm/bin/arm-none-eabi-gdb
</pre>
<hr style="border:none; border-top:1px solid #ccc; margin:10px 0;">

<h3 style="color:#333;">What is this?</h3>
<p>
RTT Viewer reads <b>SEGGER Real-Time Transfer (RTT)</b> debug output from a microcontroller
over a debug probe (ST-Link, J-Link, CMSIS-DAP, etc.) using <b>OpenOCD</b> and <b>GDB</b>.
No SEGGER software required.
</p>

<h3 style="color:#333; margin-top:14px;">Prerequisites</h3>

<table cellspacing="0" cellpadding="6" style="border-collapse:collapse; width:100%;">
<tr style="background:#f5f5f5;">
  <td style="border:1px solid #ddd; font-weight:bold; width:160px;">Package</td>
  <td style="border:1px solid #ddd; font-weight:bold; width:220px;">Install command</td>
  <td style="border:1px solid #ddd;">Purpose</td>
</tr>
<tr>
  <td style="border:1px solid #ddd;"><code>openocd</code></td>
  <td style="border:1px solid #ddd;"><code>sudo apt install openocd</code></td>
  <td style="border:1px solid #ddd;">Talks to the debug probe over USB, controls the chip, starts RTT TCP server</td>
</tr>
<tr style="background:#f5f5f5;">
  <td style="border:1px solid #ddd;"><code>gdb-multiarch</code></td>
  <td style="border:1px solid #ddd;"><code>sudo apt install gdb-multiarch</code></td>
  <td style="border:1px solid #ddd;">Sends reset / RTT setup commands to OpenOCD</td>
</tr>
<tr>
  <td style="border:1px solid #ddd;"><code>PySide6</code></td>
  <td style="border:1px solid #ddd;"><code>pip install PySide6</code></td>
  <td style="border:1px solid #ddd;">This GUI (Qt6 Python binding, LGPL)</td>
</tr>
<tr style="background:#f5f5f5;">
  <td style="border:1px solid #ddd;"><code>python3</code></td>
  <td style="border:1px solid #ddd;">usually pre-installed</td>
  <td style="border:1px solid #ddd;">Runtime</td>
</tr>
</table>

<p style="margin-top:10px;"><b>Install all at once:</b></p>
<pre style="background:#f5f5f5; border:1px solid #ddd; padding:8px; border-radius:3px;">
sudo apt install openocd gdb-multiarch
pip install PySide6
</pre>

<h3 style="color:#333; margin-top:14px;">How to use</h3>
<ol>
  <li>Connect your debug probe (ST-Link, J-Link, CMSIS-DAP…) to USB and to the target board.</li>
  <li>Select <b>Probe</b> — the interface cfg matching your probe (e.g. <code>stlink</code>, <code>jlink</code>, <code>cmsis-dap</code>).<br>
      Type in the box to search. The list is read from your OpenOCD install.</li>
  <li>Select <b>Target</b> — the chip cfg (e.g. <code>stm32u5x</code>, <code>stm32f4x</code>).<br>
      Type in the box to search.</li>
  <li>Optionally set <b>RTT addr</b> if you know the exact address of the <code>_SEGGER_RTT</code>
      control block. Leave blank to auto-scan RAM.</li>
  <li>Choose <b>Start mode</b>:
      <ul>
        <li><b>Reset + start</b> — halts the chip, resets it, then runs from the beginning. Use this normally.</li>
        <li><b>Continue</b> — attaches to an already-running target without reset.</li>
      </ul>
  </li>
  <li>Click <b>Connect</b>. RTT output appears in the <b>Terminal</b> tab.</li>
  <li>Click <b>Disconnect</b> or close the window to stop cleanly.</li>
</ol>

<h3 style="color:#333; margin-top:14px;">Finding the right Target cfg</h3>
<p>If you get an <i>"unexpected idcode"</i> error, run this to find the correct cfg:</p>
<pre style="background:#f5f5f5; border:1px solid #ddd; padding:8px; border-radius:3px;">
grep -rl "&lt;your-idcode&gt;" /usr/share/openocd/scripts/target/
</pre>
<p>Replace <code>&lt;your-idcode&gt;</code> with the hex value shown in the error (e.g. <code>0x2ba01477</code>).</p>

<h3 style="color:#333; margin-top:14px;">Firmware requirement</h3>
<p>
Your firmware must include the <b>SEGGER RTT library</b> (<code>SEGGER_RTT.c</code>).
Available free from <a href="https://www.segger.com/products/debug-probes/j-link/technology/about-real-time-transfer/">SEGGER</a>
or via Zephyr's built-in RTT logger (<code>CONFIG_USE_SEGGER_RTT=y</code>).
</p>

<h3 style="color:#333; margin-top:14px;">Terminal colours</h3>
<table cellspacing="0" cellpadding="5" style="border-collapse:collapse;">
<tr><td style="border:1px solid #ddd; padding:4px 10px; color:#cc0000; font-weight:bold;">Red</td>
    <td style="border:1px solid #ddd; padding:4px 10px;">Lines containing: error, fault, assert, hardfault</td></tr>
<tr><td style="border:1px solid #ddd; padding:4px 10px; color:#cc7700; font-weight:bold;">Orange</td>
    <td style="border:1px solid #ddd; padding:4px 10px;">Lines containing: warn, wrn</td></tr>
<tr><td style="border:1px solid #ddd; padding:4px 10px; color:#000;">Black</td>
    <td style="border:1px solid #ddd; padding:4px 10px;">Normal output</td></tr>
</table>

<br>
</body></html>
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RTT Viewer")
        self.setMinimumSize(980, 620)
        self.setStyleSheet(QSS)

        self._engine      = None
        self._paused_term = False
        self._paused_log  = False
        self._line_count  = 0
        self._byte_count  = 0

        self._ifaces  = list_cfgs("interface")
        self._targets = list_cfgs("target")

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(0,0,0,0)

        # ── toolbar ──────────────────────────────────────────────────────────
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar.setFixedHeight(46)
        tbl = QHBoxLayout(toolbar)
        tbl.setContentsMargins(10,6,10,6)
        tbl.setSpacing(8)

        # Probe
        tbl.addWidget(QLabel("Probe:"))
        self._iface_cb = SearchComboBox()
        self._iface_cb.setItems(self._ifaces)
        self._iface_cb.setMinimumWidth(155)
        self._iface_cb.setMaximumWidth(200)
        default_iface = next((i for i in self._ifaces if i == "stlink"),
                             self._ifaces[0] if self._ifaces else "")
        self._iface_cb.setCurrentValue(default_iface)
        tbl.addWidget(self._iface_cb)

        tbl.addWidget(self._vsep())

        # Target
        tbl.addWidget(QLabel("Target:"))
        self._target_cb = SearchComboBox()
        self._target_cb.setItems(self._targets)
        self._target_cb.setMinimumWidth(165)
        self._target_cb.setMaximumWidth(210)
        default_target = next((t for t in self._targets if t == "stm32u5x"),
                              self._targets[0] if self._targets else "")
        self._target_cb.setCurrentValue(default_target)
        tbl.addWidget(self._target_cb)

        tbl.addWidget(self._vsep())

        # RTT addr
        tbl.addWidget(QLabel("RTT addr:"))
        self._addr_edit = QLineEdit()
        self._addr_edit.setPlaceholderText("optional")
        self._addr_edit.setFixedWidth(130)
        tbl.addWidget(self._addr_edit)

        tbl.addWidget(self._vsep())

        # Start mode
        tbl.addWidget(QLabel("Start:"))
        self._mode_grp = QButtonGroup(self)
        self._rb_reset = QRadioButton("Reset + start")
        self._rb_cont  = QRadioButton("Continue")
        self._rb_reset.setChecked(True)
        self._mode_grp.addButton(self._rb_reset, 0)
        self._mode_grp.addButton(self._rb_cont,  1)
        tbl.addWidget(self._rb_reset)
        tbl.addWidget(self._rb_cont)

        tbl.addStretch()

        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setObjectName("connect")
        self._btn_connect.setFixedWidth(88)
        self._btn_connect.clicked.connect(self._do_connect)
        tbl.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setObjectName("disconnect")
        self._btn_disconnect.setFixedWidth(96)
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._do_disconnect)
        tbl.addWidget(self._btn_disconnect)

        root.addWidget(toolbar)

        # ── tabs ─────────────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        root.addWidget(self._tabs, stretch=1)

        # ── Terminal tab ──────────────────────────────────────────────────
        term_widget = QWidget()
        term_layout = QVBoxLayout(term_widget)
        term_layout.setContentsMargins(0,0,0,0)
        term_layout.setSpacing(0)

        # tab toolbar
        term_bar = QFrame()
        term_bar.setStyleSheet("background:#f5f5f5; border-bottom:1px solid #ddd;")
        term_bar_l = QHBoxLayout(term_bar)
        term_bar_l.setContentsMargins(6,3,6,3)
        term_bar_l.setSpacing(4)
        self._btn_clear_term = QPushButton("Clear")
        self._btn_clear_term.setFixedWidth(55)
        self._btn_clear_term.setEnabled(False)
        self._btn_clear_term.clicked.connect(lambda: self._term.clear())
        self._btn_pause_term = QPushButton("Pause")
        self._btn_pause_term.setFixedWidth(55)
        self._btn_pause_term.setEnabled(False)
        self._btn_pause_term.setCheckable(True)
        self._btn_pause_term.clicked.connect(self._toggle_pause_term)
        term_bar_l.addWidget(self._btn_clear_term)
        term_bar_l.addWidget(self._btn_pause_term)
        term_bar_l.addStretch()
        term_layout.addWidget(term_bar)

        self._term = QPlainTextEdit()
        self._term.setReadOnly(True)
        self._term.setLineWrapMode(QPlainTextEdit.NoWrap)
        term_layout.addWidget(self._term)
        self._tabs.addTab(term_widget, "Terminal")

        # ── Viewer Log tab ────────────────────────────────────────────────
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0,0,0,0)
        log_layout.setSpacing(0)

        log_bar = QFrame()
        log_bar.setStyleSheet("background:#f5f5f5; border-bottom:1px solid #ddd;")
        log_bar_l = QHBoxLayout(log_bar)
        log_bar_l.setContentsMargins(6,3,6,3)
        log_bar_l.setSpacing(4)
        self._btn_clear_log = QPushButton("Clear")
        self._btn_clear_log.setFixedWidth(55)
        self._btn_clear_log.setEnabled(False)
        self._btn_clear_log.clicked.connect(lambda: self._logbox.clear())
        self._btn_pause_log = QPushButton("Pause")
        self._btn_pause_log.setFixedWidth(55)
        self._btn_pause_log.setEnabled(False)
        self._btn_pause_log.setCheckable(True)
        self._btn_pause_log.clicked.connect(self._toggle_pause_log)
        log_bar_l.addWidget(self._btn_clear_log)
        log_bar_l.addWidget(self._btn_pause_log)
        log_bar_l.addStretch()
        log_layout.addWidget(log_bar)

        self._logbox = QPlainTextEdit()
        self._logbox.setReadOnly(True)
        log_layout.addWidget(self._logbox)
        self._tabs.addTab(log_widget, "Viewer Log")

        # ── Help tab ──────────────────────────────────────────────────────
        help_view = QTextEdit()
        help_view.setReadOnly(True)
        help_view.setHtml(HELP_HTML)
        self._tabs.addTab(help_view, "Help")

        # ── bottom bar ───────────────────────────────────────────────────────
        bot = QFrame()
        bot.setObjectName("bottombar")
        bl = QHBoxLayout(bot)
        bl.setContentsMargins(8,4,8,4)
        bl.setSpacing(6)

        self._btn_save = QPushButton("Save log…")
        self._btn_save.setFixedWidth(80)
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._save_log)
        bl.addWidget(self._btn_save)

        bl.addStretch()

        bl.addWidget(QLabel("Send:"))
        self._send_edit = QLineEdit()
        self._send_edit.setPlaceholderText("send to target (Enter)")
        self._send_edit.setEnabled(False)
        self._send_edit.setFixedWidth(300)
        self._send_edit.returnPressed.connect(self._do_send)
        bl.addWidget(self._send_edit)

        self._btn_send = QPushButton("Send")
        self._btn_send.setFixedWidth(55)
        self._btn_send.setEnabled(False)
        self._btn_send.clicked.connect(self._do_send)
        bl.addWidget(self._btn_send)

        root.addWidget(bot)

        # ── status bar ───────────────────────────────────────────────────────
        sb = self.statusBar()
        self._lbl_status = QLabel("Disconnected")
        self._lbl_info   = QLabel("")
        self._lbl_lines  = QLabel("")
        self._lbl_bytes  = QLabel("")
        sb.addWidget(self._lbl_status)
        sb.addWidget(QLabel("|"))
        sb.addWidget(self._lbl_info)
        sb.addPermanentWidget(self._lbl_lines)
        sb.addPermanentWidget(self._lbl_bytes)

        # Connect tab signal now that all widgets exist
        self._tabs.currentChanged.connect(self._on_tab_changed)
        # Apply initial disconnected state explicitly
        self._on_disconnected()

    def _vsep(self):
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setFixedWidth(1)
        return f

    # ── connect ───────────────────────────────────────────────────────────────
    def _do_connect(self):
        gdb_bin = find_gdb()
        if not gdb_bin:
            QMessageBox.critical(self, "GDB not found",
                "Install gdb-multiarch:\n\n  sudo apt install gdb-multiarch")
            return
        iface  = self._iface_cb.currentValue()
        target = self._target_cb.currentValue()
        if not iface or not target:
            QMessageBox.warning(self, "Missing", "Select Probe and Target.")
            return

        self._lbl_info.setText(f"probe: {iface}   target: {target}   gdb: {gdb_bin}")
        self._set_status("Connecting…", "#cc7700")
        self._btn_connect.setEnabled(False)
        self._btn_disconnect.setEnabled(False)
        self._append_log("info", f"Connecting — probe={iface}  target={target}  gdb={gdb_bin}")

        self._engine = RTTEngine({
            "gdb_bin":    gdb_bin,
            "iface":      iface,
            "target":     target,
            "rtt_addr":   self._addr_edit.text().strip() or None,
            "reset_mode": self._rb_reset.isChecked(),
        })
        self._engine.log_signal.connect(self._append_log)
        self._engine.rtt_signal.connect(self._append_rtt)
        self._engine.done_signal.connect(self._on_engine_done)
        self._engine.start()

    def _do_disconnect(self):
        if self._engine:
            self._engine.stop()
            self._engine = None
        self._on_disconnected()

    def _on_engine_done(self, ok):
        if ok:
            self._on_connected()
        else:
            self._btn_connect.setEnabled(True)
            self._set_status("Failed — see Viewer Log", "#cc0000")

    def _on_connected(self):
        self._set_status("Connected", "#007700")
        self._iface_cb.setEnabled(False)
        self._target_cb.setEnabled(False)
        self._addr_edit.setEnabled(False)
        self._rb_reset.setEnabled(False)
        self._rb_cont.setEnabled(False)
        self._btn_connect.setEnabled(False)
        self._btn_disconnect.setEnabled(True)
        # per-tab controls
        self._btn_clear_term.setEnabled(True)
        self._btn_pause_term.setEnabled(True)
        self._btn_clear_log.setEnabled(True)
        self._btn_pause_log.setEnabled(True)
        # bottom bar
        self._btn_save.setEnabled(True)
        self._send_edit.setEnabled(True)
        self._btn_send.setEnabled(True)

    def _on_disconnected(self):
        self._set_status("Disconnected", "#555")
        self._iface_cb.setEnabled(True)
        self._target_cb.setEnabled(True)
        self._addr_edit.setEnabled(True)
        self._rb_reset.setEnabled(True)
        self._rb_cont.setEnabled(True)
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        # per-tab controls
        self._btn_clear_term.setEnabled(False)
        self._btn_pause_term.setEnabled(False)
        self._btn_clear_log.setEnabled(False)
        self._btn_pause_log.setEnabled(False)
        # bottom bar
        self._btn_save.setEnabled(False)
        self._send_edit.setEnabled(False)
        self._btn_send.setEnabled(False)
        self._paused_term = False
        self._paused_log  = False

    def _toggle_pause_term(self, checked):
        self._paused_term = checked
        self._btn_pause_term.setText("Resume" if checked else "Pause")

    def _toggle_pause_log(self, checked):
        self._paused_log = checked
        self._btn_pause_log.setText("Resume" if checked else "Pause")

    # ── terminal ──────────────────────────────────────────────────────────────
    def _append_rtt(self, text):
        if self._paused_term:
            return
        lo = text.lower()
        if any(w in lo for w in ("error","fault","assert","hardfault")):
            color = "#cc0000"
        elif any(w in lo for w in ("warn","wrn")):
            color = "#cc7700"
        else:
            color = "#000000"

        ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        fmt = QTextCharFormat()
        cur = self._term.textCursor()
        cur.movePosition(QTextCursor.End)

        # timestamp in grey
        fmt.setForeground(QColor("#999"))
        cur.insertText(f"{ts}  ", fmt)

        fmt.setForeground(QColor(color))
        cur.insertText(text + "\n", fmt)

        self._term.setTextCursor(cur)
        self._term.ensureCursorVisible()

        self._line_count += 1
        self._byte_count += len(text)
        self._lbl_lines.setText(f"Lines: {self._line_count:,}  ")
        self._lbl_bytes.setText(f"Bytes: {self._byte_count:,}  ")

    def _append_log(self, tag, msg):
        if self._paused_log:
            return
        colors = {
            "info": "#000080",
            "warn": "#cc7700",
            "err":  "#cc0000",
            "dim":  "#888888",
        }
        color = colors.get(tag, "#000")
        ts    = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        fmt   = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur = self._logbox.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(f"{ts}  {msg}\n", fmt)
        self._logbox.setTextCursor(cur)
        self._logbox.ensureCursorVisible()

    # ── controls ──────────────────────────────────────────────────────────────
    def _on_tab_changed(self, index):
        """Disable bottom bar when Help tab (index 2) is active."""
        on_help = (index == 2)
        connected = self._btn_disconnect.isEnabled()
        active = connected and not on_help
        self._btn_save.setEnabled(active)
        self._send_edit.setEnabled(active)
        self._btn_send.setEnabled(active)

    def _set_status(self, text, color):
        self._lbl_status.setText(text)
        self._lbl_status.setStyleSheet(f"color:{color}; font-weight:bold;")

    def _clear_term(self):
        self._term.clear()
        self._line_count = 0
        self._lbl_lines.setText("")

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save RTT log",
            f"rtt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text files (*.txt);;All files (*)"
        )
        if path:
            with open(path,"w") as f:
                f.write(self._term.toPlainText())
            self._append_log("info", f"Log saved: {path}")

    def _do_send(self):
        msg = self._send_edit.text().strip()
        if not msg: return
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#0055aa"))
        cur = self._term.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(f"> {msg}\n", fmt)
        self._term.setTextCursor(cur)
        self._send_edit.clear()

    def closeEvent(self, event):
        if self._engine:
            self._engine.stop()
        event.accept()

# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="RTT Viewer — OpenOCD + GDB based SEGGER RTT terminal"
    )
    p.add_argument(
        "--openocd",
        default=None,
        metavar="PATH",
        help="Path to openocd binary  (default: auto-detect from PATH)"
    )
    p.add_argument(
        "--openocd-scripts",
        default=None,
        metavar="PATH",
        help="Path to openocd scripts dir  (default: auto-detect)"
    )
    p.add_argument(
        "--gdb",
        default=None,
        metavar="PATH",
        help="Path to GDB binary  (default: auto-detect gdb-multiarch / arm-none-eabi-gdb)"
    )
    # allow Qt to consume its own args first
    return p.parse_known_args()

if __name__ == "__main__":
    args, qt_args = parse_args()

    # Resolve globals from args or auto-detect
    OPENOCD_BIN     = resolve_openocd_bin(args.openocd)
    OPENOCD_SCRIPTS = resolve_openocd_scripts(args.openocd_scripts)
    GDB_BIN         = resolve_gdb(args.gdb) or ""

    # Patch into module globals so engine picks them up
    import __main__
    __main__.OPENOCD_BIN     = OPENOCD_BIN
    __main__.OPENOCD_SCRIPTS = OPENOCD_SCRIPTS
    __main__.GDB_BIN         = GDB_BIN

    app = QApplication([sys.argv[0]] + qt_args)
    app.setStyle("Fusion")
    win = MainWindow()

    # Show resolved paths in viewer log on startup
    win._append_log("info", f"openocd     : {OPENOCD_BIN}")
    win._append_log("info", f"ocd scripts : {OPENOCD_SCRIPTS}")
    win._append_log("info", f"gdb         : {GDB_BIN or '(not found)'}")

    win.show()
    sys.exit(app.exec())