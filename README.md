# Serial File Transfer

A Python-based file transfer tool using serial communication (UART).

Supports Windows (`COMx`) and Linux (`/dev/ttyXXX`) with the same script.

## Features

- Cross-platform: Windows and Linux
- Binary file transfer
- Configurable baud rate
- Frame synchronisation and resynchronisation on a byte-slip
- Per-chunk CRC32 verification
- Selective retransmission of missing/corrupt chunks
- Whole-file CRC32 verification
- Temporary file (`.part`) during reception
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
python serial_transfer.py receive /dev/ttyUSB1 firmware.bin
```

### Options

| Option | Default | Description |
|---|---:|---|
| `--baud` | 921600 | Serial baud rate |
| `--timeout` | 5.0 | Serial read/write timeout in seconds |

Example with a different baud rate:

```bash
python serial_transfer.py send /dev/ttyUSB0 firmware.bin --baud 2000000
```

## Serial Configuration

| Parameter | Value |
|---|---|
| Baud rate | 921600 (default) |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |

Both sender and receiver must use the same serial configuration.

## Protocol

Every message is a frame:

```text
+--------+--------+--------+--------+---------+--------+
| SYNC   | TYPE   | SEQ    | LENGTH | PAYLOAD | CRC32  |
| 2 byte | 1 byte | 4 byte | 2 byte | LENGTH  | 4 byte |
+--------+--------+--------+--------+---------+--------+
```

| Field | Description |
|---|---|
| SYNC | `0xAA 0x55`. Used to find frame boundaries again after a lost/inserted byte. |
| TYPE | `H` header, `D` data chunk, `E` end of pass, `Q` status query, `R` result. |
| SEQ | Chunk sequence number, or a pass token for `E`/`Q`/`R`. |
| LENGTH | Payload length. |
| CRC32 | CRC32 over `TYPE + SEQ + LENGTH + PAYLOAD`. |

All integers are little-endian. A frame whose CRC does not match is discarded;
the reader scans forward for the next `SYNC` and continues.

### Transfer stages

1. **Header handshake.** The sender sends an `H` frame (file size + whole-file
   CRC32) and waits for an `ACK` result.
2. **Data passes.** The sender streams `D` frames (all chunks, or only the
   missing ones) and then an `E` frame.
3. **Result.** The receiver replies with an `R` frame:
   - `DONE` – every chunk received and the whole-file CRC matched.
   - `FAIL` – every chunk received but the whole-file CRC failed.
   - `NAK` + missing list – the chunks it still needs.
4. The sender retransmits only the missing chunks and repeats from step 2.

If a result is lost, the sender sends a `Q` query and the receiver answers with
its current state, so nothing has to be resent unnecessarily. Results are
tagged with a pass token (`SEQ`) and repeated; stale results from an earlier
pass are ignored.

### Responses

| Result | Value | Meaning |
|---|---|---|
| ACK | `0x06` | Header accepted |
| NAK | `0x15` | Retransmit the listed chunks |
| DONE | `0x04` | Whole file received and verified |
| FAIL | `0x18` | Whole-file CRC failed |

## Transfer Sequence

```text
Sender                            Receiver
  |------ H (size, crc) ---------->|
  |<------------- ACK --------------|
  |------ D chunk 0 -------------->|
  |------ D chunk 1 -------------->|
  |            ...                 |
  |------ E ---------------------->|
  |<-------- NAK + missing --------|   (only if something is missing)
  |------ D (missing chunks) ----->|
  |------ E ---------------------->|
  |<------------- DONE ------------|
```

The sender streams chunks back to back instead of waiting for an ACK after each
one. This is deliberate: many USB-serial bridges (e.g. CH340) hold the tail
bytes of a write until the next write, so a "send one chunk then wait for an
ACK" protocol stalls on every chunk until a retransmit timer fires.

## Output File Handling

During reception, data is written to:

```text
<filename>.part
```

After successful whole-file CRC32 verification, the temporary file is renamed to
the specified output filename. If reception fails, the temporary file is
deleted.

## Performance and Limitations

- There is no hardware flow control (a 2-wire TX/RX/GND link is assumed).
- At 2 Mbps a CH340-based link is unreliable without flow control: a large
  fraction of chunks (commonly 30%+ on the tested setup) are corrupted or
  dropped. The per-chunk CRC and selective retransmission recover all of them,
  so the transfer always completes, but throughput drops to roughly
  50-60 KB/s because of the repeated passes.
- At 921600 bps the same link transfers with no retransmissions at all, so
  prefer a lower baud rate unless the higher rate is required.
- Sender and receiver must be started separately; the receiver waits for the
  sender.
