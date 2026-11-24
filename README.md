# Serial File Transfer

A simple Python-based file transfer tool using serial communication (UART).

Supports Windows (`COMx`) and Linux (`/dev/ttyXXX`) with the same script.

## Features

- Cross-platform: Windows and Linux
- Binary file transfer
- Configurable baud rate
- CRC32 checksum verification
- File size verification
- Temporary file (`.part`) during reception
- Transfer completion notification (ACK/NAK)
- No external dependencies except `pyserial`

## Requirements

- Python 3.8+
- pyserial

Install dependencies:

```bash
pip install pyserial
```

## Usage

```text
python serial_transfer.py <mode> <port> <file> [options]
```

### Send a file

Windows:

```powershell
python serial_transfer.py send COM3 firmware.bin
```

Linux:

```bash
python serial_transfer.py send /dev/ttyUSB0 firmware.bin
```

### Receive a file

Windows:

```powershell
python serial_transfer.py receive COM3 firmware.bin
```

Linux:

```bash
python serial_transfer.py receive /dev/ttyUSB0 firmware.bin
```

### Options

| Option | Default | Description |
|---|---:|---|
| `--baud` | 115200 | Serial baud rate |
| `--timeout` | 30.0 | Serial read/write timeout in seconds |

Example with a different baud rate:

```bash
python serial_transfer.py send /dev/ttyUSB0 firmware.bin --baud 921600
```

## Serial Configuration

| Parameter | Value |
|---|---|
| Baud rate | 115200 (default) |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |

Both sender and receiver must use the same serial configuration.

## Protocol

The transfer protocol consists of three stages.

### 1. Header

The sender transmits a 16-byte header.

| Field | Size | Description |
|---|---:|---|
| Magic | 4 bytes | Protocol identifier (`FILE`) |
| File Size | 8 bytes | File size in bytes |
| CRC32 | 4 bytes | CRC32 of the entire file |

All integer fields use little-endian byte order.

### 2. Payload

The sender transmits the file contents in chunks of up to 1024 bytes.

The receiver reads the specified file size and calculates CRC32 while writing the data.

### 3. Completion

The receiver verifies the calculated CRC32 against the value in the header.

| Response | Value | Meaning |
|---|---|---|
| ACK | `0x06` | Transfer successful / ready |
| NAK | `0x15` | CRC verification failed |

The receiver sends an initial ACK after validating the header and a final ACK after successful CRC verification.

## Transfer Sequence

```text
Sender                         Receiver
  |                                |
  |---------- Header ------------->|
  |                                |
  |<----------- ACK ---------------|
  |                                |
  |---------- Payload ------------>|
  |                                |
  |       CRC32 verification       |
  |                                |
  |<----------- ACK ---------------|
  |                                |
  |       Transfer complete        |
```

## Output File Handling

During reception, data is written to:

```text
<filename>.part
```

After successful CRC32 verification, the temporary file is renamed to the specified output filename.

If reception fails, the temporary file is deleted.

## Limitations

- No per-chunk ACK or retransmission.
- No automatic recovery from communication errors.
- A CRC mismatch requires retransmitting the entire file.
- Sender and receiver must be started separately.
- Only one file is transferred per