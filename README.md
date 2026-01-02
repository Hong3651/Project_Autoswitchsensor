# 🌐Project_Autoswitchsensor
# Switch-Guard (Backbone Loop/Link Failure Monitoring Project)

> **"Telnet 기반 실시간 탐지 + 포트 매핑(설계도) 기반 장애(루핑/단절) 조기 탐지 및 스냅샷 자동 저장"**

![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=flat&logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/OS-Windows-0078D6?style=flat&logo=windows&logoColor=white)
![Cisco](https://img.shields.io/badge/Network-Cisco-1BA0D7?style=flat&logo=cisco&logoColor=white)
![Telnet](https://img.shields.io/badge/Protocol-Telnet-555555?style=flat)

---

## 1. 프로젝트 개요 (Overview)

### 목적
본 프로젝트는 **백본 스위치에 Telnet으로 상시 접속**한 상태에서, 지정된 주기(기본 30초)로 네트워크 상태를 감지하여  
- **루핑(Loop) 징후**
- **네트워크 단절(Down / Err-disabled / Link flap 등)**
을 조기에 감지하고, **문제가 발생한 포트와 설계도(주기표) 기반의 구간 정보**를 한 화면에서 즉시 확인할 수 있도록 지원하는 경량 관제 프로그램입니다.

또한 이벤트 발생 시점의 근거를 **스냅샷(txt)로 자동 저장**하여, 현장 대응 및 사후 원인 분석에 활용할 수 있도록 설계했습니다.

### 문제 인식 (Problem Statement)
- **장애 탐지 지연**: 루핑/단절은 사용자 신고로 인지되는 경우가 많아, 대응이 늦어질 수 있음.
- **원인 포트 식별의 비효율**: 장애 발생 시 “어느 포트/어느 구간”인지 빠르게 좁히기 어렵고, 확인 과정이 수동/반복적임.
- **현장 요구사항**: 설계도/주기표에 “백본 포트 → 하위 스위치/구간”이 이미 명시되어 있으므로, **포트 번호 기반 식별**이 가장 빠르고 실용적임.

---

## 2. 해결 접근 (Approach)

### 2.1 실시간 관제 방식: Telnet 상시 연결 + 주기 폴링
프로그램 실행 시 백본 스위치에 Telnet으로 로그인하고 **enable 모드**(`#`)로 진입한 뒤, **30초 주기**로 핵심 진단 명령을 실행합니다.

- `show spanning-tree detail` : STP 변화/토폴로지 변화 관련 힌트 수집
- `show interfaces status` : 포트 상태(connected/down/err-disabled 등) 스냅샷
- `show errdisable status` : err-disabled 발생 시 reason 확인(가능한 범위)
- `show logging | last N` : MAC flap, STP, 링크 업/다운 등 이벤트 기반 근거 수집

정상 상태에서는 화면 갱신만 수행하고, 이상 징후가 감지되면 “집중 진단 및 스냅샷 저장”으로 전환합니다.

### 2.2 장애 식별 핵심: 포트번호 기반 매핑(설계도/주기표 연계)
장애 포트가 특정되면 `port_map.yaml`을 참조하여 해당 포트가 연결된 **하위 스위치/구간/위치 정보**를 즉시 함께 출력합니다.

- 예: `Gi1/0/24 → 3F-AccessSW-01 / 3층 사무실 / 주기표 3F-01 uplink`

이를 통해 “포트 번호 → 실제 구간”으로의 변환 시간을 줄이고, 현장 조치(차단/점검/케이블 확인 등)를 빠르게 연결합니다.

### 2.3 이벤트 스냅샷 저장: TXT 자동 저장 + 중복 방지(10분)
장애(루핑 의심/단절/err-disable 등)가 발생하면 해당 시점의 근거를 **TXT 스냅샷**으로 저장합니다.

- 포함 내용(요약 + 증거):
  - 발생 시각 / 장비 / 이벤트 유형
  - 문제 포트 / 포트 매핑(구간 정보)
  - 근거 요약(예: STP 변화 + 로그 이벤트)
  - 관련 명령의 원본 출력(raw)

또한 동일 이벤트가 지속될 때 로그가 과도하게 쌓이지 않도록,  
**(이벤트 종류, 포트)** 기준으로 **10분에 1회만 저장**하도록 제한했습니다.

### 2.4 운용 안정성: 수동 재접속
세션이 끊기면 자동 재접속 대신, 화면에 `DISCONNECTED` 상태를 표시하고 사용자가 `R` 키로 재접속을 트리거합니다.  
(운영 환경에서 계정 정보 자동 재시도 동작을 최소화하기 위한 선택)

---

## 3. 기대 효과 (Expected Outcome)

1. **장애 대응 시간 단축**: 사용자 신고 이전 단계에서 이상 징후를 조기 포착하고, 문제 포트를 즉시 특정
2. **현장 조치 효율화**: 포트 번호를 설계도 기반 구간 정보로 즉시 변환하여 원인 추적 시간 절감
3. **근거 기반 사후 분석**: 이벤트 시점의 명령 출력/로그를 스냅샷으로 보관하여 원인 규명 및 재발 방지에 기여
4. **경량 운용**: 서버 없이도 Windows 단말에서 상시 실행 가능한 형태로 구성

---

## 4. 개발 환경 (Environment)

- **기간**: 2025.08.01 ~ 2025.08.30
- **OS**: Windows
- **Language**: Python 3.x
- **Protocol**: Telnet (TCP/23)
- **Device**: Cisco,다산 (Backbone 기준)
- **Key Libraries**:
  - `telnetlib3` (Telnet 세션/입출력)
  - `rich` (콘솔 TUI 출력)
  - `pyyaml` (설정/포트맵 로딩)

---

## 5. 설치 및 실행 (Installation)

### 5.1 의존성 설치
```bash
pip install -r requirements.txt
