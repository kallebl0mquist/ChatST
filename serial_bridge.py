#!/usr/bin/env python3
"""Serial Chat Bridge v1 (Atari <-> Raspi).

Reads block-framed chat requests from RS232 and returns one framed answer:

  CHATBEGIN\n
  MSG <role> <length>\n
  <exactly length bytes content>
  ...
  CHATEND\n
Response:
  ANS <length>\n
  <exactly length bytes content>

Notes:
- No local echo: incoming serial bytes are never mirrored back.
- Parser is length-based and tolerant of payload newlines.
- Unknown/garbage lines outside a block are ignored (helps after link noise).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

try:
    import serial  # pyserial
except Exception as exc:  # pragma: no cover
    print(f"FATAL: pyserial fehlt: {exc}", file=sys.stderr)
    sys.exit(2)


MSG_RE = re.compile(rb"^MSG\s+(user|assistant|system)\s+(\d+)\s*$")


@dataclass
class ChatMessage:
    role: str
    content: str


class ProtocolError(Exception):
    pass


def load_env_file(path: Path, override: bool = False) -> None:
    """Load KEY=VALUE pairs from a .env-style file into os.environ."""
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key:
            continue

        if (
            len(value) >= 2
            and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'"))
        ):
            value = value[1:-1]

        if override or key not in os.environ:
            os.environ[key] = value


class SerialLineReader:
    def __init__(self, ser: serial.Serial) -> None:
        self.ser = ser
        self._pending: Optional[int] = None

    def read_line(self, timeout_s: float) -> Optional[bytes]:
        """Read one CR/LF-terminated line (without trailing CR/LF), or None on timeout."""
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            if self._pending is not None:
                chunk = bytes([self._pending])
                self._pending = None
            else:
                chunk = self.ser.read(1)
            if not chunk:
                continue
            b = chunk[0]
            if b == 0x0A:  # LF
                return bytes(buf)
            if b == 0x0D:  # CR
                # Optional LF after CR (CRLF). Keep next non-LF byte for next call.
                nxt = self.ser.read(1)
                if nxt and nxt[0] != 0x0A:
                    self._pending = nxt[0]
                return bytes(buf)
            buf.append(b)
        return None

    def read_exact(self, n: int, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        out = bytearray()
        while len(out) < n and time.monotonic() < deadline:
            need = n - len(out)
            chunk = self.ser.read(need)
            if not chunk:
                continue
            out.extend(chunk)
        if len(out) != n:
            raise TimeoutError(f"Timeout while reading payload: got {len(out)}/{n} bytes")
        return bytes(out)


class ChatBridge:
    def __init__(
        self,
        ser: serial.Serial,
        model: str,
        temperature: float,
        max_completion_tokens: int,
        request_timeout_s: float,
        debug_serial: bool,
    ) -> None:
        self.ser = ser
        self.reader = SerialLineReader(ser)
        self.model = model
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self.request_timeout_s = request_timeout_s
        self.debug_serial = debug_serial

    def _dbg(self, msg: str) -> None:
        if self.debug_serial:
            print(f"[serial-debug] {msg}", flush=True)

    def run_forever(self) -> None:
        print("Bridge bereit. Warte auf CHATBEGIN ...", flush=True)
        while True:
            try:
                req = self._read_request_block()
                if req is None:
                    continue
                answer = self._ask_llm(req)
                self._send_answer(answer)
            except KeyboardInterrupt:
                print("\nAbbruch durch Benutzer.", flush=True)
                return
            except Exception as exc:
                err = f"[bridge-error] {exc}"
                print(err, file=sys.stderr, flush=True)
                self._send_answer(err)

    def _read_request_block(self) -> Optional[List[ChatMessage]]:
        # Sync: ignore everything until CHATBEGIN
        while True:
            line = self.reader.read_line(timeout_s=0.2)
            if line is None:
                return None
            self._dbg(f"RX line (sync): {line.decode('ascii', errors='replace')!r}")
            if line == b"CHATBEGIN":
                self._dbg("SYNC -> CHATBEGIN erkannt")
                break

        messages: List[ChatMessage] = []

        while True:
            line = self.reader.read_line(timeout_s=self.request_timeout_s)
            if line is None:
                raise TimeoutError("Timeout waiting for request lines")
            self._dbg(f"RX line: {line.decode('ascii', errors='replace')!r}")

            if line == b"CHATEND":
                if not messages:
                    raise ProtocolError("CHATEND without MSG")
                self._dbg(f"CHATEND -> {len(messages)} MSG gelesen")
                return messages

            m = MSG_RE.match(line)
            if not m:
                raise ProtocolError(f"Invalid header line: {line!r}")

            role = m.group(1).decode("ascii")
            length = int(m.group(2).decode("ascii"))
            if length < 0 or length > 50000:
                raise ProtocolError(f"Invalid length: {length}")

            self._dbg(f"MSG header role={role} len={length}")
            raw = self.reader.read_exact(length, timeout_s=self.request_timeout_s)
            preview = raw[:120].decode("ascii", errors="replace")
            if len(raw) > 120:
                preview += "..."
            self._dbg(f"MSG payload ({length}B): {preview!r}")

            # Optional single trailing line break after payload
            next_byte = self.ser.read(1)
            if next_byte == b"\r":
                maybe_lf = self.ser.read(1)
                if maybe_lf != b"\n" and maybe_lf:
                    # Not a normal CRLF separator; keep stream aligned best-effort
                    pass
            elif next_byte != b"\n" and next_byte:
                # No separator; this byte belongs to following stream data.
                # We cannot un-read with pyserial, so this is protocol violation.
                raise ProtocolError("Missing LF separator after payload")

            content = raw.decode("ascii", errors="replace")
            messages.append(ChatMessage(role=role, content=content))

    def _ask_llm(self, messages: List[ChatMessage]) -> str:
        # OpenAI Python SDK optional at runtime.
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError(
                "openai SDK fehlt. Installiere: pip install openai"
            ) from exc

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY ist nicht gesetzt")

        base_url = os.getenv("OPENAI_BASE_URL") or None
        client = OpenAI(api_key=api_key, base_url=base_url)

        chat_messages = [{"role": m.role, "content": m.content} for m in messages]

        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=chat_messages,
                temperature=self.temperature,
                max_completion_tokens=self.max_completion_tokens,
            )
        except Exception as exc:
            # Compatibility fallback for endpoints/models that still expect max_tokens.
            msg = str(exc)
            if "max_completion_tokens" in msg and "unsupported" in msg.lower():
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=chat_messages,
                    temperature=self.temperature,
                    max_tokens=self.max_completion_tokens,
                )
            else:
                raise

        txt = resp.choices[0].message.content or ""
        txt = txt.replace("\r\n", "\n").replace("\r", "\n")
        return txt

    def _send_answer(self, text: str) -> None:
        # Keep transport ASCII-clean for Atari/VT52 by replacing non-ASCII.
        payload = text.encode("ascii", errors="replace")
        header = f"ANS {len(payload)}\n".encode("ascii")
        self._dbg(f"TX header: {header.decode('ascii', errors='replace').rstrip()!r}")
        preview = payload[:120].decode("ascii", errors="replace")
        if len(payload) > 120:
            preview += "..."
        self._dbg(f"TX payload ({len(payload)}B): {preview!r}")
        self.ser.write(header)
        self.ser.write(payload)
        self.ser.write(b"\n")
        self.ser.flush()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Serial Chat Bridge v1")
    p.add_argument("--port", required=True, help="Serial device, z.B. /dev/ttyAMA0")
    p.add_argument("--baud", type=int, default=9600, help="Baudrate (default: 9600)")
    p.add_argument("--model", default="gpt-5.4-mini", help="OpenAI model")
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--max-completion-tokens", type=int, default=700)
    p.add_argument("--max-tokens", type=int, default=None, help="Legacy alias; overrides --max-completion-tokens")
    p.add_argument("--serial-timeout", type=float, default=0.1, help="Read timeout in seconds")
    p.add_argument("--request-timeout", type=float, default=15.0, help="Protocol timeout in seconds")
    p.add_argument("--debug-serial", action="store_true", help="Log serial protocol traffic")
    return p


def main() -> int:
    load_env_file(Path.cwd() / ".env", override=False)
    args = build_arg_parser().parse_args()

    ser = serial.Serial(
        port=args.port,
        baudrate=args.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=args.serial_timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )

    print(
        f"Serial offen: {args.port} {args.baud} 8N1 (xonxoff=off, rtscts=off)",
        flush=True,
    )

    mct = args.max_completion_tokens
    if args.max_tokens is not None:
        mct = args.max_tokens

    bridge = ChatBridge(
        ser=ser,
        model=args.model,
        temperature=args.temperature,
        max_completion_tokens=mct,
        request_timeout_s=args.request_timeout,
        debug_serial=args.debug_serial,
    )
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
