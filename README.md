# STLinkRTTViewer

STLinkRTTViewer is a lightweight SEGGER RTT terminal for STM32 and other ARM targets using OpenOCD and GDB.

The tool provides a graphical interface for viewing RTT logs without requiring SEGGER RTT Viewer.

## Features

* ST-Link support
* OpenOCD backend
* SEGGER RTT terminal
* Searchable Probe selection
* Searchable Target selection
* Connect / Disconnect controls
* RTT log save
* Colorized log output
* Built-in Help tab
* Linux standalone executable available

## Requirements

Install:

```bash id="s02a0h"
sudo apt install openocd gdb-multiarch
```

## Usage

1. Connect ST-Link to the target board.
2. Launch STLinkRTTViewer.
3. Select Probe.
4. Select Target.
5. Optionally enter RTT address.
6. Click Connect.
7. RTT output appears in the Terminal tab.

## Built-in Help

The application contains a Help tab with:

* Setup instructions
* OpenOCD requirements
* RTT usage guide
* Troubleshooting information
* Target configuration guidance

## Notes

* OpenOCD must support your target device.
* Firmware must include SEGGER RTT support.
* OpenOCD and GDB must be installed on the system.

## License

MIT License
