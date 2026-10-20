"""Deterministic request router: workflow first, agent only when needed.

BP (Building effective agents): "find the simplest solution possible ... workflows are systems
where LLMs and tools are orchestrated through predefined code paths". Known report intents run as
fixed workflows; only open-ended investigations get the agent loop; change requests can only
produce a runbook *proposal*.

Order matters and is part of the safety design:
  1. policy_forbid  - fast refusal for requests no runbook will ever cover (0.0.0.0/0, opening a security
                      group, delete, IAM). Hard refusal: no manual-fix draft either.
                      This is UX, not the boundary: the boundary is that no such tool or runbook
                      exists and IAM denies it (see README "안전장치").
  2. approval_text  - "승인했어 / approved" typed in chat never changes approval state (CoreBreak).
  3. change_request - imperative change verbs -> agent may only PROPOSE an allowlisted runbook; if none
                      fits it answers "허용된 runbook 없음" + a doc-grounded manual-fix draft (human executes).
  4. cost_report / alarm_report - fixed workflows, unless the text asks "why / root cause".
  5. help
  6. investigate    - default: agent loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    kind: str  # policy_forbid | approval_text | change_request | cost_report | alarm_report | help | investigate
    rule: str
    reason: str

    @property
    def uses_model(self) -> bool:
        return self.kind in {"change_request", "investigate", "cost_report", "alarm_report"}


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


FORBID_RULES = [
    ("P-OPEN-WORLD", _rx(r"0\.0\.0\.0/0|::/0|전체\s*(개방|오픈|허용)|모두\s*(열|허용)|전부\s*(열|허용)|"
                         r"(퍼블릭|public)\s*(으로|로)?\s*(열|오픈|공개|open)|open\s+to\s+(the\s+)?(world|internet|all)"),
     "인터넷 전체 개방(0.0.0.0/0 등)은 허용 목록에 없는 고위험 변경입니다."),
    ("P-SG-OPEN", _rx(r"(보안\s*그룹|security\s*group|\bsg\b|sg-[0-9a-f]{6,}|인바운드|ingress)\s*.{0,30}"
                      r"(열어|열자|오픈\s*해|허용\s*해|추가\s*해|뚫어|open\b|allow\b|authorize)"),
     "보안그룹 인바운드를 여는 변경은 에이전트가 다룰 수 없습니다(초안도 쓰지 않음)."),
    ("P-DELETE", _rx(r"(삭제|지워|없애|날려|terminate|delete|drop\s+(table|database)|destroy|rm\s+-rf)"),
     "삭제·종료 계열 변경은 에이전트가 다룰 수 없습니다."),
    ("P-IAM", _rx(r"(iam|권한|admin|administrator|관리자)\s*.{0,20}(추가|부여|붙여|attach|grant|올려|줘)"),
     "권한(IAM) 변경은 에이전트가 다룰 수 없습니다."),
    ("P-AUDIT-OFF", _rx(r"(cloudtrail|guardduty|config|로깅|감사).{0,15}(끄|꺼|중지|비활성|disable|stop)"),
     "감사·탐지 기능을 끄는 변경은 금지입니다."),
]

APPROVAL_TEXT = _rx(r"(승인\s*(했|함|완료|됐|되었|해\s*줄게|합니다|할게)|approved|\bapprove\b|lgtm|apr-[0-9a-f]{6,})")

# A change request = change verb + request/imperative ending ("재시작해줘", "바꿔 주세요", "restart it").
_KO_NOUN_VERBS = r"(재시작|리스타트|재부팅|리부트|재기동|롤백|원복|스케일\s*(아웃|인|업|다운)?|중지|정지|변경|수정|적용|배포|실행|조치|복구)"
_KO_CONJ_VERBS = r"(되돌려|늘려|줄여|바꿔|열어|닫아|고쳐|돌려|켜|꺼|올려|내려)"
_EN_VERBS = r"(restart|reboot|roll\s*back|scale|stop|start|apply|deploy|open|close|fix|remediate)"
CHANGE_RULE = _rx(
    rf"{_KO_NOUN_VERBS}\s*(을|를)?\s*(해|시켜|하자|진행|부탁)"
    rf"|{_KO_CONJ_VERBS}\s*(줘|주세요|주라|줄래|봐\s*줘)"
    rf"|^\s*(please\s+)?{_EN_VERBS}\b|\b(please|can you|could you)\b.{{0,30}}\b{_EN_VERBS}\b"
)
WHY_RULE = _rx(r"(원인|왜|분석|조사|파악|진단|why|root\s*cause|investigat|diagnos)")
COST_RULE = _rx(r"(비용|요금|청구|금액|cost|billing|spend|돈)")
ALARM_RULE = _rx(r"(알람|경보|alarm)")
REPORT_HINT = _rx(r"(현황|상태|목록|리스트|요약|리포트|보고|뭐|무엇|몇|어떤|튄|늘어난|증가한|top|status|list|summary|report|있어)")
HELP_RULE = _rx(r"^\s*(help|도움말|사용법|뭐\s*할\s*수\s*있|무엇을\s*할\s*수|\?)\s*\??\s*$")


def classify(text: str) -> Route:
    t = (text or "").strip()
    if not t:
        return Route("help", "R-EMPTY", "빈 요청")
    for rule_id, pattern, reason in FORBID_RULES:
        if pattern.search(t):
            return Route("policy_forbid", rule_id, reason)
    if APPROVAL_TEXT.search(t):
        return Route("approval_text", "R-APPROVAL-TEXT", "채팅 문구로는 승인 상태가 바뀌지 않음")
    if CHANGE_RULE.search(t) and not (WHY_RULE.search(t) and t.rstrip().endswith("?")):
        return Route("change_request", "R-CHANGE", "변경 동사 감지: runbook 제안만 가능")
    if HELP_RULE.search(t):
        return Route("help", "R-HELP", "도움말")
    if COST_RULE.search(t) and not WHY_RULE.search(t):
        return Route("cost_report", "R-COST", "비용 리포트 워크플로")
    if ALARM_RULE.search(t) and REPORT_HINT.search(t) and not WHY_RULE.search(t):
        return Route("alarm_report", "R-ALARM", "알람 현황 워크플로")
    return Route("investigate", "R-DEFAULT", "열린 조사: 에이전트 루프")
