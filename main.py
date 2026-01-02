import asyncio
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import yaml
import telnetlib3
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout

# Windows 수동 재접속용
if sys.platform.startswith("win"):
    import msvcrt


console = Console()

#--------------------------------
def now_kst_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")

def ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)

def safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)

PORT_RE = re.compile(r"\bGi\d+/\d+/\d+\b")  

MORE_PATTERNS = [
    re.compile(r"--More--", re.I),
    re.compile(r"\(q\)uit", re.I),
    re.compile(r"Press any key", re.I),
]


# 데이터 모델
@dataclass
class PortMapEntry:
    target: str = ""
    area: str = ""
    note: str = ""

@dataclass
class Alert:
    kind: str                 # 장애 종류
    port: str                 # 주기 양식 =>>>>Gi1/0/24
    score: int = 0            # loop score
    brief: str = ""           # 근거
    mapped: Optional[PortMapEntry] = None

@dataclass
class ParsedState:
    # 포트 상태: connected/down/err-disabled/disabled 
    port_status: Dict[str, str]
    # errdisable 이유
    errdisable_reason: Dict[str, str]
    # STP 힌트
    stp_last_change_port: Optional[str]
    # 최근 이벤트(로그 라인 정규화 전 단계: 문자열 리스트)
    recent_events: List[str]

# -----------------------------
# Telnet 세션
# -----------------------------
class TelnetSession:
    def __init__(self, host: str, port: int, name: str):
        self.host = host
        self.port = port
        self.name = name
        self.reader = None
        self.writer = None

    async def connect_and_login(self, username: str, password: str, enable_secret: str) -> None:
        self.reader, self.writer = await telnetlib3.open_connection(
            host=self.host,
            port=self.port,
            connect_minwait=0.1,
            connect_maxwait=1.0,
            timeout=6,
            encoding="utf8",
        )

        # 로그인 시퀀스: Username/login -> Password -> 프롬프트
        await self._login_flow(username, password)
        await self._ensure_enable(enable_secret)
        await self.run_cmd("terminal length 0", expect=r"#\s*$", timeout=6)
        await self.run_cmd("terminal width 511", expect=r"#\s*$", timeout=6)

    async def close(self) -> None:
        try:
            if self.writer:
                self.writer.write("exit\n")
                await self.writer.drain()
                self.writer.close()
        except Exception:
            pass
        self.reader = None
        self.writer = None

    async def _read_until(self, expect_regex: str, timeout: int = 8, max_bytes: int = 250_000) -> str:
        buf = ""
        expect = re.compile(expect_regex, re.I)

        while True:
            if len(buf) > max_bytes:
                return buf
            try:
                chunk = await asyncio.wait_for(self.reader.read(2048), timeout=timeout)
            except asyncio.TimeoutError:
                return buf

            if not chunk:
                return buf

            buf += chunk

            # paging 처리
            for pat in MORE_PATTERNS:
                if pat.search(buf):
                    self.writer.write(" ")
                    await self.writer.drain()
                    buf = pat.sub("", buf)

            if expect.search(buf):
                return buf

    async def _login_flow(self, username: str, password: str) -> None:
        for _ in range(12):
            out = await self._read_until(r"(Username:|login:|Password:|[>#]\s*$)", timeout=8)
            if re.search(r"(Username:|login:)", out, re.I):
                self.writer.write(username + "\n")
                await self.writer.drain()
                continue
            # Password가 떴고 아직 프롬프트가 아니라면
            if re.search(r"Password:", out, re.I) and not re.search(r"[>#]\s*$", out):
                self.writer.write(password + "\n")
                await self.writer.drain()
                continue
            if re.search(r"[>#]\s*$", out):
                return
        raise RuntimeError("Login failed: prompt not reached.")

    async def _ensure_enable(self, enable_secret: str) -> None:
        self.writer.write("\n")
        await self.writer.drain()
        out = await self._read_until(r"[>#]\s*$", timeout=6)
        if re.search(r"#\s*$", out):
            return

        # enable
        self.writer.write("enable\n")
        await self.writer.drain()
        out2 = await self._read_until(r"(Password:|#\s*$|denied|Invalid)", timeout=6)
        if re.search(r"#\s*$", out2):
            return
        if re.search(r"Password:", out2, re.I):
            self.writer.write(enable_secret + "\n")
            await self.writer.drain()
            out3 = await self._read_until(r"(#\s*$|denied|Invalid)", timeout=6)
            if re.search(r"#\s*$", out3):
                return
        raise RuntimeError("Enable failed.")

    async def run_cmd(self, cmd: str, expect: str = r"#\s*$", timeout: int = 12) -> str:
        if not self.writer:
            raise RuntimeError("Not connected.")
        self.writer.write(cmd + "\n")
        await self.writer.drain()
        return await self._read_until(expect, timeout=timeout)

# -----------------------------
# 파서(Cisco 중심, 없으면 최대한 best-effort)
# -----------------------------
def parse_show_interfaces_status(text: str) -> Dict[str, str]:
    status = {}
    lines = text.splitlines()
    for ln in lines:
        # 포트로 시작하는 라인들
        m = re.match(r"^\s*(Gi\d+/\d+/\d+)\s+(.+)$", ln)
        if not m:
            continue
        port = m.group(1)
        rest = m.group(2)
        st = None
        for cand in ["connected", "notconnect", "disabled", "err-disabled", "inactive", "suspended", "monitoring", "down"]:
            if re.search(rf"\b{re.escape(cand)}\b", rest, re.I):
                st = cand.lower()
                break
        if st:
            status[port] = st
    return status

def parse_show_errdisable_status(text: str) -> Dict[str, str]:
    reasons = {}
    current_reason = None
    for ln in text.splitlines():
        if ln.strip() and not ln.startswith(" ") and not ln.lower().startswith(("port", "----", "errdisable")):
            maybe_reason = ln.strip()
            if len(maybe_reason) <= 30 and " " not in maybe_reason:
                current_reason = maybe_reason

        for port in PORT_RE.findall(ln):
            if current_reason:
                reasons[port] = current_reason

        mt = re.match(r"^\s*(Gi\d+/\d+/\d+)\s+\S+\s+(\S+)\s*$", ln)
        if mt:
            port, reason = mt.group(1), mt.group(2)
            reasons[port] = reason
    return reasons

def parse_show_spanning_tree_detail(text: str) -> Optional[str]:
    for ln in text.splitlines():
        if "last" in ln.lower() and "change" in ln.lower() and "from" in ln.lower():
            m = PORT_RE.search(ln)
            if m:
                return m.group(0)
    m2 = re.search(r"\bfrom\s+(Gi\d+/\d+/\d+)\b", text, re.I)
    return m2.group(1) if m2 else None

def parse_show_logging(text: str) -> List[str]:
    events = []
    for ln in text.splitlines():
        low = ln.lower()
        if any(k in low for k in [
            "spantree", "span-tree", "topology change", "tcn",
            "mac flap", "macflap", "matm", "move",
            "link", "lineproto", "updown", "changed state",
            "errdisable", "bpdu", "storm", "broadcast"
        ]):
            events.append(ln.strip())
    return events[-100:]  # 너무 길면 컷
# -----------------------------
# 탐지(루핑/단절)
# -----------------------------
def build_loop_suspects(parsed: ParsedState) -> List[Tuple[str, int, str]]:
    """
    루핑 의심 포트 후보 생성(스코어링)
    반환: [(port, score, brief), ...] score 내림차순
    """
    scores: Dict[str, int] = {}

    # STP last change 포트는 강한 신호
    if parsed.stp_last_change_port:
        p = parsed.stp_last_change_port
        scores[p] = scores.get(p, 0) + 60

    # 로그에서 MAC flap / STP / storm 등 포트 언급을 가중
    for ev in parsed.recent_events:
        ports = PORT_RE.findall(ev)
        low = ev.lower()
        for p in ports:
            add = 0
            if "mac" in low and ("flap" in low or "move" in low or "matm" in low):
                add += 50
            if "topology" in low or "tcn" in low or "spantree" in low:
                add += 40
            if "storm" in low or "broadcast" in low:
                add += 30
            if "updown" in low or "changed state" in low:
                add += 15
            if add:
                scores[p] = scores.get(p, 0) + add

    # 결과 정리
    res = []
    for p, sc in scores.items():
        brief = "STP/MAC/LOG 기반"
        res.append((p, min(sc, 100), brief))
    res.sort(key=lambda x: x[1], reverse=True)
    return res[:8]

def build_down_impacts(prev: Optional[ParsedState], curr: ParsedState) -> List[Alert]:
    alerts: List[Alert] = []
    if prev is None:
        return alerts

    for port, st in curr.port_status.items():
        prev_st = prev.port_status.get(port)

        # 새로 down/err-disabled로 바뀐 경우만 알림
        if prev_st != st and st in ("down", "notconnect", "disabled", "err-disabled"):
            kind = "ERRDISABLED" if st == "err-disabled" else "PORT_DOWN"
            reason = curr.errdisable_reason.get(port, "")
            brief = f"status={st}" + (f", reason={reason}" if reason else "")
            alerts.append(Alert(kind=kind, port=port, brief=brief))
    return alerts

# -----------------------------
# 스냅샷(TXT) 저장 + 중복 방지
# -----------------------------
class SnapshotStore:
    def __init__(self, root_dir: str, dedupe_minutes: int):
        self.root_dir = root_dir
        self.dedupe = dt.timedelta(minutes=dedupe_minutes)
        self.last_saved: Dict[Tuple[str, str], dt.datetime] = {}  # (kind, port) -> time

    def should_save(self, kind: str, port: str) -> bool:
        key = (kind, port)
        now = dt.datetime.now()
        t = self.last_saved.get(key)
        if t and (now - t) < self.dedupe:
            return False
        self.last_saved[key] = now
        return True

    def save_txt(
        self,
        device_name: str,
        alert: Alert,
        port_map: Dict[str, PortMapEntry],
        raw_outputs: Dict[str, str],
        suspects: List[Tuple[str, int, str]],
        impacts: List[Alert],
        events: List[str],
    ) -> str:
        day_dir = os.path.join(self.root_dir, dt.datetime.now().strftime("%Y%m%d"))
        ensure_dir(day_dir)

        mapped = port_map.get(alert.port, PortMapEntry())
        fname = f"{safe_filename(device_name)}_{stamp()}_{alert.kind}_{safe_filename(alert.port)}.txt"
        path = os.path.join(day_dir, fname)

        with open(path, "w", encoding="utf-8") as f:
            f.write("==== SWITCH-GUARD SNAPSHOT ====\n")
            f.write(f"TIME: {now_kst_str()}\n")
            f.write(f"DEVICE: {device_name}\n")
            f.write(f"ALERT: {alert.kind}\n")
            f.write(f"PORT: {alert.port}\n")
            f.write(f"MAPPING: target={mapped.target} | area={mapped.area} | note={mapped.note}\n")
            if alert.score:
                f.write(f"SCORE: {alert.score}\n")
            if alert.brief:
                f.write(f"BRIEF: {alert.brief}\n")
            f.write("\n---- TOP SUSPECTS ----\n")
            for p, sc, br in suspects[:5]:
                mp = port_map.get(p, PortMapEntry())
                f.write(f"- {p}  score={sc}  target={mp.target}  area={mp.area}  why={br}\n")

            f.write("\n---- IMPACTS (DOWN/ERRDISABLE CHANGES) ----\n")
            for a in impacts[:20]:
                mp = port_map.get(a.port, PortMapEntry())
                f.write(f"- {a.kind}  {a.port}  target={mp.target}  {a.brief}\n")

            f.write("\n---- RECENT EVENTS (FILTERED) ----\n")
            for ev in events[-80:]:
                f.write(ev + "\n")

            f.write("\n\n==== RAW OUTPUTS ====\n")
            for cmd, out in raw_outputs.items():
                f.write("\n" + "=" * 90 + "\n")
                f.write(f"CMD: {cmd}\n")
                f.write("=" * 90 + "\n")
                f.write(out)
                f.write("\n")

        return path

# -----------------------------
# UI
# -----------------------------
def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=5),
        Layout(name="body", ratio=1),
        Layout(name="bottom", size=12),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    return layout

def render_ui(
    status_line: str,
    suspects: List[Tuple[str, int, str]],
    impacts: List[Alert],
    events: List[str],
    port_map: Dict[str, PortMapEntry],
) -> Layout:
    layout = make_layout()

    layout["top"].update(Panel(status_line, title="Status"))

    # Left: suspects
    t1 = Table(title="Loop Suspects (Top)", expand=True)
    t1.add_column("Port")
    t1.add_column("Score", justify="right")
    t1.add_column("Target/Area")
    t1.add_column("Why")
    for p, sc, br in suspects[:8]:
        mp = port_map.get(p, PortMapEntry())
        t1.add_row(p, str(sc), f"{mp.target} / {mp.area}", br)
    layout["left"].update(t1)

    # Right: impacts
    t2 = Table(title="Down / Errdisabled (New Changes)", expand=True)
    t2.add_column("Type")
    t2.add_column("Port")
    t2.add_column("Target/Area")
    t2.add_column("Detail")
    for a in impacts[:12]:
        mp = port_map.get(a.port, PortMapEntry())
        t2.add_row(a.kind, a.port, f"{mp.target} / {mp.area}", a.brief)
    layout["right"].update(t2)

    # Bottom: events
    t3 = Table(title="Recent Events (Filtered)", expand=True)
    t3.add_column("Event")
    for ev in events[-20:]:
        t3.add_row(ev)
    layout["bottom"].update(t3)

    return layout

# -----------------------------
# 설정 로드
# -----------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_port_map(path: str) -> Dict[str, PortMapEntry]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    pm: Dict[str, PortMapEntry] = {}
    for port, info in (data.get("ports") or {}).items():
        pm[port] = PortMapEntry(
            target=str(info.get("target", "")),
            area=str(info.get("area", "")),
            note=str(info.get("note", "")),
        )
    return pm

# -----------------------------
# 키 입력(Windows)
# -----------------------------
def get_key_nonblocking() -> Optional[str]:
    if not sys.platform.startswith("win"):
        return None
    if msvcrt.kbhit():
        ch = msvcrt.getwch()
        return ch
    return None

# -----------------------------
# 메인 루프
# -----------------------------
async def poll_once(session: TelnetSession, log_last_lines: int) -> Tuple[ParsedState, Dict[str, str]]:
    cmds = [
        "show spanning-tree detail",
        "show interfaces status",
        "show errdisable status",
        f"show logging | last {log_last_lines}",
    ]
    raw: Dict[str, str] = {}
    for cmd in cmds:
        raw[cmd] = await session.run_cmd(cmd, expect=r"#\s*$", timeout=20)

    parsed = ParsedState(
        port_status=parse_show_interfaces_status(raw["show interfaces status"]),
        errdisable_reason=parse_show_errdisable_status(raw["show errdisable status"]),
        stp_last_change_port=parse_show_spanning_tree_detail(raw["show spanning-tree detail"]),
        recent_events=parse_show_logging(raw[f"show logging | last {log_last_lines}"]),
    )
    return parsed, raw

async def main():
    # config/port_map 파일 준비
    if not os.path.exists("config.yaml"):
        console.print("config.yaml이 없습니다. config.example.yaml을 복사해 config.yaml로 만드세요.")
        return
    if not os.path.exists("port_map.yaml"):
        console.print("port_map.yaml이 없습니다. port_map.example.yaml을 복사해 port_map.yaml로 만드세요.")
        return

    cfg = load_config("config.yaml")
    port_map = load_port_map("port_map.yaml")

    device = cfg["backbone"]
    device_name = device["name"]
    host = device["ip"]
    port = int(device.get("port", 23))

    poll_interval = int(cfg.get("poll_interval_sec", 30))
    log_last_lines = int(cfg.get("log_last_lines", 80))
    dedupe_min = int(cfg.get("snapshot_dedupe_minutes", 10))
    snapshot_dir = str(cfg.get("snapshot_dir", "snapshots"))

    ensure_dir(snapshot_dir)
    store = SnapshotStore(snapshot_dir, dedupe_min)

    # 자격증명은 실행 시 입력
    console.print("[bold]Enter credentials (not saved to disk)[/bold]")
    username = console.input("Username: ").strip()
    password = console.input("Password: ").strip()
    enable_secret = console.input("Enable secret: ").strip()

    session = TelnetSession(host=host, port=port, name=device_name)

    connected = False
    prev_state: Optional[ParsedState] = None
    last_poll_time: Optional[dt.datetime] = None
    recent_events: List[str] = []

    async def do_connect():
        nonlocal connected, prev_state, last_poll_time, recent_events
        try:
            await session.connect_and_login(username, password, enable_secret)
            connected = True
            prev_state = None
            last_poll_time = None
            recent_events = []
        except Exception as e:
            connected = False
            console.print(f"[red]Connect failed:[/red] {e}")

    await do_connect()

    with Live(console=console, refresh_per_second=6) as live:
        while True:
            key = get_key_nonblocking()
            if key in ("q", "Q"):
                await session.close()
                return

            if not connected:
                status_line = f"[DISCONNECTED] {device_name} ({host}:{port})  | Press [R] to reconnect, [Q] to quit"
                live.update(render_ui(status_line, [], [], [], port_map))
                if key in ("r", "R"):
                    await session.close()
                    await do_connect()
                await asyncio.sleep(0.2)
                continue

            now = dt.datetime.now()
            # 폴링 타이밍(30초)
            if (last_poll_time is None) or ((now - last_poll_time).total_seconds() >= poll_interval):
                try:
                    curr_state, raw = await poll_once(session, log_last_lines)
                    last_poll_time = now

                    # 이벤트 목록 누적(최근만 유지)
                    for ev in curr_state.recent_events:
                        if ev and (ev not in recent_events):
                            recent_events.append(ev)
                    recent_events = recent_events[-120:]

                    suspects = build_loop_suspects(curr_state)
                    impacts = build_down_impacts(prev_state, curr_state)

                    # 루핑 경보(점수 기반)
                    alerts_to_save: List[Alert] = []
                    if suspects and suspects[0][1] >= 80:
                        p, sc, br = suspects[0]
                        alerts_to_save.append(Alert(kind="LOOP_SUSPECTED", port=p, score=sc, brief=br))

                    # 단절 경보
                    alerts_to_save.extend(impacts)

                    # 스냅샷 저장(10분 중복 방지)
                    for a in alerts_to_save:
                        if store.should_save(a.kind, a.port):
                            path = store.save_txt(
                                device_name=device_name,
                                alert=a,
                                port_map=port_map,
                                raw_outputs=raw,
                                suspects=suspects,
                                impacts=impacts,
                                events=recent_events,
                            )
                            # 화면용 이벤트에 기록
                            recent_events.append(f"{now_kst_str()} [SNAPSHOT SAVED] {a.kind} {a.port} -> {path}")
                            recent_events = recent_events[-120:]

                    prev_state = curr_state

                    status_line = f"[CONNECTED] {device_name}  poll={poll_interval}s  dedupe={dedupe_min}m  | [Q] quit"
                    live.update(render_ui(status_line, suspects, impacts, recent_events, port_map))

                except Exception as e:
                    # 연결 끊김/명령 실패 시 DISCONNECTED로 전환
                    connected = False
                    recent_events.append(f"{now_kst_str()} [ERROR] {e}")
                    recent_events = recent_events[-120:]
                    try:
                        await session.close()
                    except Exception:
                        pass
                    continue

            # 폴링 사이에도 UI는 계속 유지
            await asyncio.sleep(0.2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
