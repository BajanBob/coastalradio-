"""CAT control for tuning receivers (AIS/DSC in FM, HF in USB).

Receive-only by design: these classes only set frequency and receive mode.
There are no PTT or transmit commands anywhere in this program.

  icom     CI-V   (IC-9100 0x7C, IC-9700 0xA2, IC-7300 0x94 … or use "rig": "ic9700" etc.)
  yaesu    Yaesu  (FT-991A and other new-style ASCII CAT: FA + 9 digits)
  kenwood  Kenwood ASCII CAT (FA + 11 digits)
  manual   no CAT (e.g. FTM-510DR): you tune the radio by hand
"""

from __future__ import annotations

import time

CHANNELS = {"A": 161_975_000, "B": 162_025_000, "70": 156_525_000}  # AIS 1 (87B), AIS 2 (88B), DSC Ch70


MODES = ("FM", "USB", "LSB", "AM")


def fmt_freq(hz: int) -> str:
    return f"{hz / 1e6:.3f} MHz" if hz >= 30_000_000 else f"{hz / 1e3:.1f} kHz"


class Rig:
    kind = "manual"

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "manual tuning"
        self.ok = True

    def tune(self, hz: int, mode: str = "FM") -> bool:
        self.status = f"set the radio to {fmt_freq(hz)} {mode} by hand"
        return True

    def close(self) -> None:
        pass


class _SerialRig(Rig):
    def __init__(self, name: str, port: str, baud: int, stopbits: int = 1,
                 rts: bool | None = None, dtr: bool | None = None) -> None:
        super().__init__(name)
        try:
            import serial
        except ImportError as e:
            raise RuntimeError("CAT control needs pyserial: pip install pyserial") from e
        self.ser = serial.Serial(port, baud, timeout=0.3, stopbits=stopbits)
        # Never assert lines by default; some interfaces wire RTS/DTR to PTT.
        self.ser.rts = bool(rts) if rts is not None else False
        self.ser.dtr = bool(dtr) if dtr is not None else False
        self.status = f"connected on {port}"

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:  # noqa: BLE001
            pass


class IcomCIV(_SerialRig):
    kind = "icom"

    def __init__(self, name, port, baud=19200, address=0x7C, controller=0xE0, **kw) -> None:
        super().__init__(name, port, baud, **kw)
        self.addr, self.ctrl = address, controller

    def _cmd(self, payload: bytes) -> bool:
        frame = b"\xFE\xFE" + bytes([self.addr, self.ctrl]) + payload + b"\xFD"
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        deadline, buf = time.time() + 0.8, b""
        ok = b"\xFE\xFE" + bytes([self.ctrl, self.addr, 0xFB]) + b"\xFD"
        ng = b"\xFE\xFE" + bytes([self.ctrl, self.addr, 0xFA]) + b"\xFD"
        while time.time() < deadline:
            buf += self.ser.read(64)
            if ok in buf:
                return True
            if ng in buf:
                return False
        return False

    @staticmethod
    def bcd(hz: int) -> bytes:
        s = f"{hz:010d}"
        return bytes(int(s[i - 2]) << 4 | int(s[i - 1]) for i in range(10, 0, -2))

    CIV_MODE = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "FM": 0x05}

    def tune(self, hz: int, mode: str = "FM") -> bool:
        f_ok = self._cmd(b"\x05" + self.bcd(hz))
        m_ok = self._cmd(bytes([0x06, self.CIV_MODE[mode], 0x01]))  # mode, filter 1 (widest)
        self.ok = f_ok
        self.status = (f"tuned {fmt_freq(hz)} {mode}" if f_ok and m_ok else
                       f"tuned {fmt_freq(hz)} ({mode} mode not confirmed)" if f_ok else
                       "no reply from radio: check CI-V address, baud rate and USB port")
        return f_ok


class _AsciiCat(_SerialRig):
    digits = 9
    mode_cmds = {"LSB": "MD01;", "USB": "MD02;", "FM": "MD04;", "AM": "MD05;"}

    def _ask(self, cmd: str) -> str:
        self.ser.reset_input_buffer()
        self.ser.write(cmd.encode())
        deadline, buf = time.time() + 0.8, b""
        while time.time() < deadline and not buf.endswith(b";"):
            buf += self.ser.read(32)
        return buf.decode(errors="replace")

    def tune(self, hz: int, mode: str = "FM") -> bool:
        self.ser.write(f"FA{hz:0{self.digits}d};".encode())
        time.sleep(0.1)
        self.ser.write(self.mode_cmds[mode].encode())
        time.sleep(0.1)
        back = self._ask("FA;")
        self.ok = back.strip().startswith(f"FA{hz:0{self.digits}d}")
        self.status = (f"tuned {fmt_freq(hz)} {mode}" if self.ok else
                       "radio didn't confirm frequency: check CAT baud rate, port, and that "
                       f"{fmt_freq(hz)} is within its receive range")
        return self.ok


class YaesuCAT(_AsciiCat):
    kind = "yaesu"
    digits = 9  # FT-991A: MD0 + mode (1 LSB, 2 USB, 4 FM, 5 AM)


class KenwoodCAT(_AsciiCat):
    kind = "kenwood"
    digits = 11
    mode_cmds = {"LSB": "MD1;", "USB": "MD2;", "FM": "MD4;", "AM": "MD5;"}


# Model shortcuts: "rig": "ic9700" is the same as "rig": "icom" with the right CI-V address.
ICOM_MODELS = {"ic9100": 0x7C, "ic9700": 0xA2, "ic7300": 0x94, "ic7610": 0x98, "ic705": 0xA4,
               "ic7100": 0x88, "ic7851": 0x8E, "icr8600": 0x96, "icr75": 0x5A}
# What each model can receive, so the console can warn before tuning out of range (Hz).
COVERAGE = {
    "ic9700": [(144_000_000, 148_000_000), (430_000_000, 450_000_000), (1_240_000_000, 1_300_000_000)],
    "ic9100": [(30_000, 60_000_000), (136_000_000, 174_000_000), (420_000_000, 480_000_000),
               (1_240_000_000, 1_320_000_000)],
    "ft991a": [(30_000, 56_000_000), (118_000_000, 164_000_000), (420_000_000, 470_000_000)],
}


def in_coverage(model: str, hz: int) -> bool:
    ranges = COVERAGE.get((model or "").lower().replace("-", ""))
    return ranges is None or any(lo <= hz <= hi for lo, hi in ranges)


def make_rig(cfg: dict) -> Rig:
    kind = (cfg.get("rig") or "manual").lower().replace("-", "")
    if kind in ICOM_MODELS:
        cfg = dict(cfg, civ_address=cfg.get("civ_address") or hex(ICOM_MODELS[kind]),
                   model=cfg.get("model") or kind)
        kind = "icom"
    name = cfg.get("name", kind)
    if kind == "manual" or not cfg.get("port"):
        return Rig(name)
    common = {"stopbits": int(cfg.get("stopbits", 1)), "rts": cfg.get("rts"), "dtr": cfg.get("dtr")}
    if kind == "icom":
        addr = cfg.get("civ_address", "0x7C")
        addr = int(addr, 16) if isinstance(addr, str) else int(addr)
        return IcomCIV(name, cfg["port"], int(cfg.get("baud", 19200)), addr, **common)
    if kind == "yaesu":
        return YaesuCAT(name, cfg["port"], int(cfg.get("baud", 38400)), **common)
    if kind == "kenwood":
        return KenwoodCAT(name, cfg["port"], int(cfg.get("baud", 9600)), **common)
    raise ValueError(f"Unknown rig type '{kind}' (use icom, yaesu, kenwood or manual)")
