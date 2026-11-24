
#!/usr/bin/env python3

import argparse
import os
import struct
import sys
import time
import zlib

import serial


# Protocol
MAGIC = b"FILE"
HEADER_FORMAT = "<4sQI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

CHUNK_SIZE = 1024
ACK = b"\x06"
NAK = b"\x15"


def read_exact(ser, size):
    """Read exactly size bytes."""
    data = bytearray()

    while len(data) < size:
        chunk = ser.read(size - len(data))

        if not chunk:
            raise TimeoutError("Serial receive timeout")

        data.extend(chunk)

    return bytes(data)


def send_file(ser, filename):
    """Send a file over serial."""

    file_size = os.path.getsize(filename)

    crc = 0

    with open(filename, "rb") as f:
        while True:
            data = f.read(CHUNK_SIZE)
            if not data:
                break
            crc = zlib.crc32(data, crc)

    crc &= 0xFFFFFFFF

    print(f"Sending: {filename}")
    print(f"Size: {file_size} bytes")
    print(f"CRC32: {crc:08X}")

    # Send header
    header = struct.pack(
        HEADER_FORMAT,
        MAGIC,
        file_size,
        crc
    )

    ser.write(header)
    ser.flush()

    # Wait for receiver ready
    if read_exact(ser, 1) != ACK:
        raise RuntimeError("Receiver not ready")

    # Send data
    sent = 0

    with open(filename, "rb") as f:
        while True:
            data = f.read(CHUNK_SIZE)
            if not data:
                break

            ser.write(data)
            ser.flush()

            sent += len(data)

            print(
                f"\rSending: {sent}/{file_size} bytes "
                f"({sent * 100 / max(file_size, 1):.1f}%)",
                end=""
            )

    # Wait for final result
    result = read_exact(ser, 1)

    print()

    if result != ACK:
        raise RuntimeError("Transfer failed: CRC error")

    print("Transfer completed successfully.")


def receive_file(ser, filename):
    """Receive a file over serial."""

    print("Waiting for sender...")

    # Receive header
    header = read_exact(ser, HEADER_SIZE)

    magic, file_size, expected_crc = struct.unpack(
        HEADER_FORMAT,
        header
    )

    if magic != MAGIC:
        raise RuntimeError("Invalid protocol header")

    print(f"Receiving: {filename}")
    print(f"Size: {file_size} bytes")
    print(f"Expected CRC32: {expected_crc:08X}")

    # Tell sender we are ready
    ser.write(ACK)
    ser.flush()

    received = 0
    crc = 0

    # Write to temporary file
    temp_filename = filename + ".part"

    try:
        with open(temp_filename, "wb") as f:
            while received < file_size:

                size = min(CHUNK_SIZE, file_size - received)

                data = read_exact(ser, size)

                f.write(data)
                crc = zlib.crc32(data, crc)

                received += len(data)

                print(
                    f"\rReceiving: {received}/{file_size} bytes "
                    f"({received * 100 / max(file_size, 1):.1f}%)",
                    end=""
                )

        crc &= 0xFFFFFFFF

        print()

        if crc != expected_crc:
            ser.write(NAK)
            ser.flush()
            raise RuntimeError(
                f"CRC mismatch: expected {expected_crc:08X}, "
                f"got {crc:08X}"
            )

        os.replace(temp_filename, filename)

        ser.write(ACK)
        ser.flush()

        print("Transfer completed successfully.")

    except Exception:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)
        raise


def main():

    parser = argparse.ArgumentParser(
        description="Serial file transfer tool"
    )

    parser.add_argument(
        "mode",
        choices=["send", "receive"]
    )

    parser.add_argument(
        "port",
        help="Serial port (COM3 or /dev/ttyUSB0)"
    )

    parser.add_argument(
        "file",
        help="File to send or output filename"
    )

    parser.add_argument(
        "--baud",
        type=int,
        default=115200
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0
    )

    args = parser.parse_args()

    try:
        with serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=args.timeout,
            write_timeout=args.timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False
        ) as ser:

            # Allow USB serial devices to initialize
            time.sleep(0.5)

            if args.mode == "send":
                send_file(ser, args.file)

            else:
                receive_file(ser, args.file)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
