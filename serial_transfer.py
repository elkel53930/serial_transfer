
#!/usr/bin/env python3

import argparse
import os
import struct
import sys
import time
import zlib

import serial


# ---------------------------------------------------------------- protocol
#
# Every message is framed as:
#
#   +--------+--------+--------+--------+---------+--------+
#   | SYNC   | TYPE   | SEQ    | LENGTH | PAYLOAD | CRC32  |
#   | 2 byte | 1 byte | 4 byte | 2 byte | LENGTH  | 4 byte |
#   +--------+--------+--------+--------+---------+--------+
#
# SYNC    : 0xAA 0x55.  Lets the receiver find frame boundaries again after
#           a lost or inserted byte.  This link (CH340 at 2Mbps, no flow
#           control) both corrupts and drops bytes, so resynchronisation and
#           per-chunk CRCs are required.
# TYPE    : 'H' header, 'D' data chunk, 'E' end of pass, 'Q' status query,
#           'R' result (receiver -> sender).
# SEQ     : chunk sequence number (0 for H); pass token for E/Q/R.
# LENGTH  : payload length.
# CRC32   : CRC32 over TYPE + SEQ + LENGTH + PAYLOAD.
#
# The sender streams every data chunk back-to-back instead of waiting for an
# ACK after each chunk.  This is essential: the CH340 holds the tail bytes of
# a write until the *next* write, so a request/response "send one chunk then
# wait for ACK" protocol deadlocks on every chunk until the retransmit timer
# fires (observed as ~300 ms per chunk).  Streaming keeps writes flowing so the
# adapter always flushes, and lost chunks are recovered afterwards.
#
# Transfer is a sequence of "passes":
#   1. header handshake (H, wait for an ACK result)
#   2. sender streams data frames (all chunks, or just the missing ones), then
#      an END frame (E), tagged with a pass token
#   3. receiver replies with a framed RESULT (R): DONE, FAIL, or NAK plus the
#      list of chunks still missing
#   4. sender streams only those chunks again; repeat until DONE or the retry
#      budget is exhausted.  If a result is lost, the sender sends a Q query and
#      the receiver answers with its current status.
#
# Results and END markers are repeated (REPEAT) and results are tagged with the
# pass token so stale copies from an earlier pass can never be mistaken for the
# current one.  A padding byte follows every write so the byte the CH340 holds
# back is never a meaningful one.

FRAME_MAGIC = b"\xAA\x55"
FRAME_HEADER_FORMAT = "<2sBIH"          # SYNC + TYPE + SEQ + LENGTH
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER_FORMAT)
FRAME_CRC_FORMAT = "<I"
FRAME_CRC_SIZE = struct.calcsize(FRAME_CRC_FORMAT)
FRAME_META_FORMAT = "<BIH"              # TYPE + SEQ + LENGTH (CRC input)

FRAME_TYPE_HEADER = 0x48                # 'H'
FRAME_TYPE_DATA = 0x44                  # 'D'
FRAME_TYPE_END = 0x45                   # 'E'
FRAME_TYPE_RESULT = 0x52                # 'R' (receiver -> sender status)
FRAME_TYPE_QUERY = 0x51                 # 'Q' (sender asks for current status)

HEADER_PAYLOAD_FORMAT = "<QI"           # file size + whole-file CRC32
HEADER_PAYLOAD_SIZE = struct.calcsize(HEADER_PAYLOAD_FORMAT)

MISSING_MAX_PAYLOAD = 0xFFFF            # LENGTH field is 16-bit
MISSING_COUNT_FORMAT = "<I"
MISSING_SEQ_FORMAT = "<I"

CHUNK_SIZE = 1024
# Inter-frame delay as a fraction of the on-wire time of a frame (10 bits per
# byte).  0.5 was the fastest setting that did not make the loss rate worse;
# it still leaves the link dominated by the CH340's inherent corruption.
PACE_FACTOR = 0.5
HEADER_RETRIES = 8          # header handshake attempts
MAX_PASSES = 30             # data/retransmission passes
STALL_LIMIT = 12            # consecutive passes without progress before abort

HEADER_TIMEOUT = 2.0
RECV_FRAME_TIMEOUT = 0.3
# Generous: after streaming a pass the sender waits while the receiver drains
# its (paced) backlog, which can take several seconds for large files.
FINAL_TIMEOUT = 10.0
# How many times each response / end marker is repeated.  The link corrupts or
# drops roughly a third of frames, so redundancy keeps control messages from
# being lost and avoids long timeout stalls.
REPEAT = 3
# Abort the receiver if no valid frame arrives for this long during a pass.
PASS_IDLE_TIMEOUT = 30.0

ACK = b"\x06"
NAK = b"\x15"
DONE = b"\x04"
FAIL = b"\x18"

PAD = b"\x00"


def build_frame(frame_type, seq, payload):
    """Serialise one framed message."""
    meta = struct.pack(FRAME_META_FORMAT, frame_type, seq, len(payload))
    crc = zlib.crc32(meta + payload) & 0xFFFFFFFF
    return (
        FRAME_MAGIC
        + meta
        + payload
        + struct.pack(FRAME_CRC_FORMAT, crc)
    )


def send_result(ser, status, missing=None, token=0):
    """Send a framed result message, tagged with the pass token, repeated."""
    payload = status
    if status == NAK and missing:
        payload += encode_missing(missing)
    frame = build_frame(FRAME_TYPE_RESULT, token, payload)
    ser.write((frame + PAD) * REPEAT)
    ser.flush()


def read_result(ser, timeout, token):
    """Read the result for `token`.

    Results are framed (CRC) and tagged with the pass token.  Stale results
    left over from an earlier pass are ignored because their token differs,
    so a missed flush can never be mistaken for the current pass's result.
    Returns (status, missing) or None on timeout.
    """
    reader = FrameReader(ser, timeout)
    deadline = time.monotonic() + timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        reader.timeout = min(remaining, RECV_FRAME_TIMEOUT)
        status, frame_type, seq, payload = reader.read_frame(
            max_payload=MISSING_MAX_PAYLOAD
        )

        if (
            status == "ok"
            and frame_type == FRAME_TYPE_RESULT
            and seq == token
            and payload
        ):
            st = payload[:1]
            missing = decode_missing(payload[1:]) if st == NAK else []
            ser.reset_input_buffer()
            return st, missing

        # "timeout"/"bad"/other frame/stale token: keep trying.


class FrameReader:
    """Buffered frame reader that resynchronises on the SYNC marker."""

    def __init__(self, ser, timeout):
        self.ser = ser
        self.timeout = timeout
        self.buffer = bytearray()

    def _fill(self, need):
        """Try to buffer at least `need` bytes. Return False on timeout."""
        while len(self.buffer) < need:
            previous = self.ser.timeout
            self.ser.timeout = self.timeout
            try:
                data = self.ser.read(max(1, self.ser.in_waiting))
            finally:
                self.ser.timeout = previous

            if not data:
                return False

            self.buffer.extend(data)

        return True

    def read_frame(self, max_payload=CHUNK_SIZE):
        """Return (status, type, seq, payload).

        status is one of:
          "ok"      - a valid frame was decoded
          "bad"     - a corrupt frame was skipped (a chunk may be missing)
          "timeout" - no (complete) frame arrived within the timeout
        """
        while True:
            index = self.buffer.find(FRAME_MAGIC)

            if index < 0:
                keep = len(FRAME_MAGIC) - 1
                if len(self.buffer) > keep:
                    del self.buffer[:len(self.buffer) - keep]
                if not self._fill(len(self.buffer) + 1):
                    self.buffer.clear()
                    return ("timeout", None, None, b"")
                continue

            if index > 0:
                del self.buffer[:index]

            if not self._fill(FRAME_HEADER_SIZE):
                self.buffer.clear()
                return ("timeout", None, None, b"")

            _, frame_type, seq, length = struct.unpack(
                FRAME_HEADER_FORMAT,
                bytes(self.buffer[:FRAME_HEADER_SIZE])
            )

            if length > max_payload:
                self.buffer.clear()
                return ("bad", None, None, b"")

            total = FRAME_HEADER_SIZE + length + FRAME_CRC_SIZE

            if not self._fill(total):
                self.buffer.clear()
                return ("timeout", None, None, b"")

            payload = bytes(
                self.buffer[FRAME_HEADER_SIZE:FRAME_HEADER_SIZE + length]
            )
            (received_crc,) = struct.unpack(
                FRAME_CRC_FORMAT,
                bytes(self.buffer[FRAME_HEADER_SIZE + length:total])
            )
            calculated_crc = zlib.crc32(
                struct.pack(FRAME_META_FORMAT, frame_type, seq, length)
                + payload
            ) & 0xFFFFFFFF

            if calculated_crc != received_crc:
                self.buffer.clear()
                return ("bad", None, None, b"")

            del self.buffer[:total]
            return ("ok", frame_type, seq, payload)


def chunk_length(file_size, seq):
    """Length of the chunk with sequence number `seq`."""
    return min(CHUNK_SIZE, file_size - seq * CHUNK_SIZE)


def file_crc(filename):
    crc = 0
    with open(filename, "rb") as f:
        while True:
            data = f.read(65536)
            if not data:
                break
            crc = zlib.crc32(data, crc)
    return crc & 0xFFFFFFFF


def encode_missing(missing):
    parts = [struct.pack(MISSING_COUNT_FORMAT, len(missing))]
    parts.extend(struct.pack(MISSING_SEQ_FORMAT, seq) for seq in missing)
    return b"".join(parts)


def decode_missing(payload):
    if len(payload) < struct.calcsize(MISSING_COUNT_FORMAT):
        return []
    (count,) = struct.unpack(
        MISSING_COUNT_FORMAT, payload[:struct.calcsize(MISSING_COUNT_FORMAT)]
    )
    size = struct.calcsize(MISSING_COUNT_FORMAT)
    step = struct.calcsize(MISSING_SEQ_FORMAT)
    missing = []
    for _ in range(count):
        if size + step > len(payload):
            break
        (seq,) = struct.unpack(MISSING_SEQ_FORMAT, payload[size:size + step])
        missing.append(seq)
        size += step
    return missing


def send_chunks(ser, filename, file_size, seqs):
    """Stream the requested chunks, paced below the line rate.

    The Python receiver cannot reliably sustain the full 2Mbps (reading,
    CRC-checking and writing each chunk takes longer than the 4 ms the wire
    needs per chunk), so sending at line rate overflows its buffers and ~30%
    of frames are lost.  Pacing to a fraction of the line rate keeps the
    receiver in step, which drastically cuts the number of retransmissions.
    """
    baud = ser.baudrate or 115200
    count = 0
    with open(filename, "rb") as f:
        for seq in seqs:
            f.seek(seq * CHUNK_SIZE)
            data = f.read(CHUNK_SIZE)
            frame = build_frame(FRAME_TYPE_DATA, seq, data)
            ser.write(frame)
            count += 1
            # 10 bits per byte; PACE_FACTOR > 1 slows below the line rate.
            time.sleep(len(frame) * 10.0 / baud * PACE_FACTOR)
    ser.flush()
    return count


def send_file(ser, filename):
    """Send a file over serial."""

    file_size = os.path.getsize(filename)
    total_crc = file_crc(filename)
    num_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

    print(f"Sending: {filename}")
    print(f"Size: {file_size} bytes")
    print(f"CRC32: {total_crc:08X}")

    # ----- header handshake
    header = build_frame(
        FRAME_TYPE_HEADER,
        0,
        struct.pack(HEADER_PAYLOAD_FORMAT, file_size, total_crc)
    )

    ready = False
    for attempt in range(HEADER_RETRIES + 1):
        ser.write((header + PAD) * REPEAT)
        ser.flush()

        result = read_result(ser, HEADER_TIMEOUT, 0)
        if result is not None and result[0] == ACK:
            ready = True
            break
        if attempt < HEADER_RETRIES:
            print(
                f"\nHeader not acknowledged, retrying "
                f"({attempt + 1}/{HEADER_RETRIES})..."
            )

    if not ready:
        raise RuntimeError("Receiver not ready (header not acknowledged)")

    # ----- data passes
    missing = None
    last_missing_count = num_chunks
    stall = 0
    full_retries = 0

    for pass_index in range(MAX_PASSES + 1):
        token = pass_index + 1

        if missing is None:
            seqs = range(num_chunks)
        else:
            seqs = missing

        send_chunks(ser, filename, file_size, seqs)
        # END frame repeated (with padding so its tail is not held back).
        ser.write((build_frame(FRAME_TYPE_END, token, b"") + PAD) * REPEAT)
        ser.flush()

        result = read_result(ser, FINAL_TIMEOUT, token)

        # If the result was lost, ask the receiver for its current status
        # instead of blindly resending everything.
        query_attempt = 0
        while result is None and query_attempt < 3:
            ser.write(
                (build_frame(FRAME_TYPE_QUERY, token, b"") + PAD) * REPEAT
            )
            ser.flush()
            result = read_result(ser, FINAL_TIMEOUT, token)
            query_attempt += 1

        if result is not None:
            status, new_missing = result

            if status == DONE:
                print()
                print("Transfer completed successfully.")
                return

            if status == FAIL:
                raise RuntimeError(
                    "Receiver reported a whole-file CRC failure"
                )

            if status == NAK:
                if len(new_missing) >= last_missing_count:
                    stall += 1
                else:
                    stall = 0
                last_missing_count = len(new_missing)
                missing = new_missing or None
                full_retries = 0
                print(
                    f"\rPass {pass_index + 1}: retransmitting "
                    f"{len(new_missing)} chunk(s)...",
                    end=""
                )
                if stall > STALL_LIMIT:
                    raise RuntimeError(
                        "No progress on retransmission (give up)"
                    )
                continue

        # No usable result.  Retry only the same set of chunks; never fall
        # back to resending the whole file (that would be very wasteful).
        stall += 1
        if missing is None:
            full_retries += 1
            if full_retries > 2:
                raise RuntimeError(
                    "Receiver did not respond to the header/data pass"
                )
        print(
            f"\nPass {pass_index + 1}: no usable result, "
            "retrying the same chunks..."
        )
        if stall > STALL_LIMIT:
            raise RuntimeError("Transfer failed after repeated retries")

    raise RuntimeError("Transfer failed after too many retransmission passes")


def receive_file(ser, filename):
    """Receive a file over serial."""

    print("Waiting for sender...")

    reader = FrameReader(ser, HEADER_TIMEOUT)

    # ----- header handshake
    file_size = None
    expected_crc = None

    while file_size is None:
        status, frame_type, _seq, payload = reader.read_frame(
            max_payload=HEADER_PAYLOAD_SIZE
        )

        if status == "ok":
            if (
                frame_type == FRAME_TYPE_HEADER
                and len(payload) == HEADER_PAYLOAD_SIZE
            ):
                file_size, expected_crc = struct.unpack(
                    HEADER_PAYLOAD_FORMAT, payload
                )
                break
            send_result(ser, NAK)
        elif status == "bad":
            send_result(ser, NAK)
        # On "timeout" keep waiting (the sender may not have started).

    print(f"Receiving: {filename}")
    print(f"Size: {file_size} bytes")
    print(f"Expected CRC32: {expected_crc:08X}")

    send_result(ser, ACK)

    num_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    received = bytearray(num_chunks)
    received_count = 0

    temp_filename = filename + ".part"

    reader.timeout = RECV_FRAME_TIMEOUT
    last_missing_count = num_chunks
    stall = 0

    try:
        with open(temp_filename, "wb") as f:
            if file_size:
                f.truncate(file_size)

            last_activity = time.monotonic()
            ended = False

            def try_finalize(token):
                """Verify and finalise the file; caller returns afterwards."""
                f.flush()
                actual_crc = file_crc(temp_filename)
                if actual_crc != expected_crc:
                    send_result(ser, FAIL, token=token)
                    raise RuntimeError(
                        f"CRC mismatch: expected "
                        f"{expected_crc:08X}, got {actual_crc:08X}"
                    )
                os.replace(temp_filename, filename)
                send_result(ser, DONE, token=token)
                print()
                print("Transfer completed successfully.")

            while True:
                status, frame_type, seq, payload = reader.read_frame()

                if status == "timeout":
                    if time.monotonic() - last_activity > PASS_IDLE_TIMEOUT:
                        raise RuntimeError(
                            "Timeout waiting for data from sender"
                        )
                    continue

                last_activity = time.monotonic()

                if status == "bad":
                    # Corrupt frame: the affected chunk will show up as missing.
                    continue

                if frame_type == FRAME_TYPE_DATA:
                    # A fresh data frame starts a new pass.
                    ended = False
                    if (
                        0 <= seq < num_chunks
                        and not received[seq]
                        and len(payload) == chunk_length(file_size, seq)
                    ):
                        f.seek(seq * CHUNK_SIZE)
                        f.write(payload)
                        received[seq] = 1
                        received_count += 1

                        # Throttle progress output: printing every chunk is a
                        # real bottleneck and makes the receiver fall behind.
                        if received_count % 64 == 0:
                            print(
                                f"\rReceiving: {received_count}/{num_chunks} "
                                f"chunks "
                                f"({received_count * 100 / num_chunks:.1f}%)",
                                end=""
                            )
                    # Duplicate or out-of-range chunks are ignored.
                    continue

                if frame_type == FRAME_TYPE_QUERY:
                    # The sender lost our result and is asking for the current
                    # status.  Answer without needing a retransmit.
                    if received_count == num_chunks:
                        try_finalize(seq)
                        return
                    missing = [
                        seq for seq in range(num_chunks) if not received[seq]
                    ]
                    send_result(ser, NAK, missing, seq)
                    last_activity = time.monotonic()
                    continue

                if frame_type == FRAME_TYPE_END:
                    if ended:
                        # Duplicate of an END we already answered this pass.
                        continue
                    ended = True

                    if received_count == num_chunks:
                        try_finalize(seq)
                        return

                    missing = [
                        seq for seq in range(num_chunks) if not received[seq]
                    ]

                    if len(missing) >= last_missing_count:
                        stall += 1
                    else:
                        stall = 0
                    last_missing_count = len(missing)

                    if stall > STALL_LIMIT:
                        raise RuntimeError(
                            "No progress receiving missing chunks"
                        )

                    # Ask for the missing chunks (framed NAK + list, repeated).
                    send_result(ser, NAK, missing, seq)
                    last_activity = time.monotonic()
                    continue

                # Unknown frame type: ignore.

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
        default=921600
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0
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

            # Allow USB serial devices to initialize, then discard any stale
            # bytes left over from a previous run.
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            if args.mode == "send":
                send_file(ser, args.file)

            else:
                receive_file(ser, args.file)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
