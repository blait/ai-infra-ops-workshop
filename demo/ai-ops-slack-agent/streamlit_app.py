"""Run: .venv/bin/streamlit run streamlit_app.py (local operator UI)."""
from __future__ import annotations

from datetime import datetime, timezone
import re

import streamlit as st

from aiops.console import AwsConsole, display_text, new_session

st.set_page_config(page_title="Ops Studio · AgentCore", page_icon="◈", layout="wide")
st.markdown("""
<style>
.block-container {padding-top:4.5rem; max-width:1440px}
[data-testid="stSidebar"] {background:#142D3A}
[data-testid="stSidebar"] * {color:#E5EEF0}
[data-testid="stSidebar"] button {background:#203F4D; border-color:#45606B}
[data-testid="stSidebar"] input {color:#142D3A}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap:0.65rem}
[data-testid="stAppDeployButton"] {display:none}
h1 {font-weight:750; letter-spacing:-1.5px}
[data-testid="stMetric"] {background:white; border:1px solid #DFE7EC; border-radius:12px; padding:16px}
[data-testid="stMetricValue"] > div {font-size:clamp(1rem, 3vw, 2.25rem)}
[data-testid="stChatMessage"] {background:white; border:1px solid #E4EAED; border-radius:14px}
.eyebrow {font-size:12px; letter-spacing:2px; color:#147D72; font-weight:700}
.intro {font-size:17px; color:#627583; margin-bottom:22px}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def connect():
    return AwsConsole()


@st.cache_data(ttl=60)
def deployment():
    return connect().status()


if "sessions" not in st.session_state:
    sid = new_session()
    st.session_state.sessions = {sid: {"mode": "scenario", "messages": [], "approvals": [], "progress": []}}
    st.session_state.selected = sid

with st.sidebar:
    st.markdown("## ◈ Ops Studio")
    st.caption("MODULE 03 / AGENTCORE")
    mode_label = st.radio("데이터 소스", ["준비된 장애", "실제 AWS 계정"])
    mode = "scenario" if mode_label == "준비된 장애" else "aws_live"
    if st.button("＋ 새 조사", width="stretch"):
        sid = new_session(mode)
        st.session_state.sessions[sid] = {"mode": mode, "messages": [], "approvals": [], "progress": []}
        st.session_state.selected = sid
    candidates = [s for s, v in st.session_state.sessions.items() if v["mode"] == mode]
    if not candidates:
        sid = new_session(mode)
        st.session_state.sessions[sid] = {"mode": mode, "messages": [], "approvals": [], "progress": []}
        candidates = [sid]
    if st.session_state.selected not in candidates:
        st.session_state.selected = candidates[-1]
    sid = st.selectbox("조사 기록", candidates, key="selected",
                       format_func=lambda s: f"조사 {candidates.index(s) + 1} · {s[-6:]}")
    st.divider()
    try:
        console = connect()
        status = deployment()
    except Exception as exc:
        st.error("AWS 연결을 확인하세요.")
        st.code(display_text(str(exc)), wrap_lines=True)
        st.stop()
    for name, ready in status["checks"].items():
        st.markdown(f"{'🟢' if ready else '🟠'}  {name} · {'연결됨' if ready else '확인 필요'}")
    st.caption(f"{status['region']} · Runtime v{status['runtime_version']}")
    if st.button("연결 새로고침", width="stretch"):
        connect.clear()
        deployment.clear()
        st.rerun()
    st.divider()
    st.caption("로컬 운영자 콘솔\n\nAWS 자격 증명은 서버에서만 사용합니다.\n모델은 변경을 직접 실행할 수 없습니다.")

conversation = st.session_state.sessions[sid]
messages = conversation["messages"]
last = next((m.get("result", {}) for m in reversed(messages) if m["role"] == "assistant"), {})
meta = last.get("meta") or {}

st.markdown('<div class="eyebrow">AMAZON BEDROCK AGENTCORE × AGENT TOOLKIT FOR AWS</div>', unsafe_allow_html=True)
st.title("운영을 묻고, 근거로 판단하세요.")
st.markdown('<div class="intro">장애 조사부터 비용 분석, 사람의 승인을 거친 조치까지.</div>', unsafe_allow_html=True)
cols = st.columns(4)
cols[0].metric("실행 환경", "AgentCore", f"Runtime v{status['runtime_version']}", delta_color="off")
cols[1].metric("도구 정책", "ENFORCE" if status["checks"]["Policy"] else "확인 필요", "Gateway · Cedar", delta_color="off")
cols[2].metric("현재 데이터", "샘플 장애" if mode == "scenario" else "실제 AWS", "반복 시연용 픽스처" if mode == "scenario" else "관리형 AWS MCP Server", delta_color="off")
cols[3].metric("최근 응답", f"{meta['elapsed_s']}초" if meta.get("elapsed_s") is not None else "준비됨",
               f"근거 {len(meta.get('evidence_created') or [])}건", delta_color="off")

if not status["ready"]:
    st.warning("배포 연결을 먼저 확인하세요. Runtime·Gateway·Policy·Memory·AWS Toolkit이 모두 준비되어야 실행합니다.")
if mode == "scenario":
    st.caption("지표·로그·비용은 준비된 데이터입니다. 승인한 재시작은 전용 데모 EC2에 실제로 실행됩니다.")
else:
    st.caption("Agent Toolkit for AWS의 MCP Server로 현재 계정을 조회합니다. 이 모드의 도구 권한은 읽기 전용입니다.")

chat_tab, memory_tab, action_tab = st.tabs(["💬  조사와 대화", "◈  근거와 Memory", "✓  승인과 실행"])


def show_reply(result):
    text = display_text(result.get("response") or "응답이 없습니다.")
    if result.get("route") == "approval_text":
        text = (
            "**채팅 문구로는 승인되지 않습니다.** **승인과 실행** 탭에서 대상과 영향을 확인한 뒤 "
            "**승인하고 실행** 또는 **거절**을 선택하세요. 이 콘솔의 결정은 서버의 AWS IAM 호출자로 기록되며, "
            "승인 상태는 모델이나 대화 기록에 두지 않습니다."
        )
    if "┌─ 변경 승인 요청" in text:
        text = text.split("┌─ 변경 승인 요청", 1)[0].rstrip()
        text += "\n\n**승인과 실행** 탭에서 대상과 영향을 확인한 뒤 승인하거나 거절하세요."
    main, marker, draft = text.partition("======== 수동 조치 초안")
    main = re.sub(r"(?m)^\[([^]\n]+)\]", r"\n#### \1\n", main)
    main = re.sub(r"(?m)^•\s*", "- ", main)
    with st.container(height=420 if len(main) > 1800 else "content", border=False):
        st.markdown(main)
    if marker:
        with st.expander("수동 조치 초안 · 검증 필요 (실행은 사람이)"):
            st.code("수동 조치 초안" + draft, language="text", wrap_lines=True)
    m = result.get("meta") or {}
    route = {"investigate": "원인 조사", "cost_report": "비용 워크플로", "aws_live": "AWS MCP 조회",
             "change_request": "변경 제안", "policy_forbid": "정책 거부",
             "approval_text": "채팅 승인 차단"}.get(result.get("route"), result.get("route", ""))
    st.caption(f"{route} · {m.get('elapsed_s', '-')}초 · 모델 추정 비용 ${m.get('cost_usd') or 0:.3f}")
    with st.expander("호출한 도구와 처리 경로"):
        calls = m.get("gateway_calls") or []
        if calls:
            st.dataframe([{"Gateway 도구": c.get("gateway_tool"), "결과": c.get("outcome"), "시간(ms)": c.get("ms")}
                          for c in calls], hide_index=True, width="stretch")
        else:
            st.caption("이 응답에서는 Gateway 도구를 호출하지 않았습니다.")
        if m.get("memory"):
            st.json(m["memory"])


with chat_tab:
    prompt = None
    presets = (
        [("장애 원인 조사", "checkout-api 5xx 늘었어, 원인 봐줘"),
         ("비용 증가 확인", "어제 대비 비용이 튄 서비스는?"),
         ("재시작 제안", "재시작해줘")]
        if mode == "scenario" else
        [("EC2 운영 현황", "us-east-1 EC2 인스턴스의 이름·상태·타입과 전체 수를 조회해줘"),
         ("현재 알람", "us-east-1 CloudWatch 알람 현황을 조회해줘. ALARM 상태와 전체 수를 구분해줘"),
         ("최근 7일 비용", "최근 완료된 7일의 실제 AWS 비용을 서비스별로 조회해줘")]
    )
    for column, (label, text) in zip(st.columns(3), presets):
        if column.button(label, width="stretch", disabled=not status["ready"]):
            prompt = text
    if not messages:
        with st.container(border=True):
            st.markdown("#### 작은 질문에서 시작합니다.")
            st.write("질문을 선택하거나 아래에 입력하세요. 응답의 근거는 Memory에 남고, 변경 제안은 승인 전까지 대기합니다.")
    for message in messages:
        with st.chat_message(message["role"], avatar="🤖" if message["role"] == "assistant" else None):
            if message["role"] == "user":
                st.markdown(display_text(message["text"]))
            elif message.get("error"):
                st.error(display_text(message["error"]))
            else:
                show_reply(message["result"])
    typed = st.chat_input("예: checkout-api의 5xx가 늘었어. 근거를 모아 원인을 조사해줘.", disabled=not status["ready"])
    prompt = typed or prompt
    if prompt:
        messages.append({"role": "user", "text": prompt})
        with st.chat_message("user"):
            st.markdown(display_text(prompt))
        with st.chat_message("assistant", avatar="🤖"):
            progress = []
            with st.status("AgentCore에서 처리 중…", expanded=True) as box:
                def update(message):
                    progress.append(message)
                    st.write(display_text(message))
                try:
                    result = console.invoke(prompt, sid, mode, update)
                    messages.append({"role": "assistant", "result": result})
                    if result.get("approval_id") and result["approval_id"] not in conversation["approvals"]:
                        conversation["approvals"].append(result["approval_id"])
                    box.update(label="처리 완료", state="complete", expanded=False)
                except Exception as exc:
                    messages.append({"role": "assistant", "error": str(exc)})
                    box.update(label="요청을 완료하지 못했습니다.", state="error")
                conversation["progress"] = progress
        st.rerun()

with memory_tab:
    st.subheader("같은 조사의 맥락을 이어갑니다")
    st.caption("AgentCore Memory에서 직접 읽은 노트와 도구 결과입니다. 세션별로 분리하고 30일 보관합니다.")
    if st.button("Memory에서 불러오기"):
        try:
            conversation["memory"] = console.notes(sid)
        except Exception as exc:
            st.error(display_text(str(exc)))
    stored = conversation.get("memory")
    if stored:
        r = stored["restored"]
        columns = st.columns(3)
        for column, label, key in zip(columns, ["저장 이벤트", "노트 스냅숏", "근거"], ["events", "snapshots", "evidence"]):
            column.metric(label, r[key])
        notes_text = display_text(stored["markdown"])
        notes_text = re.sub(r"^# 조사 노트:.*", "### 조사 노트", notes_text, count=1)
        notes_text = re.sub(r"(?<!\\)\$", r"\\$", notes_text)
        st.markdown(notes_text)
        for evidence in stored["evidence"]:
            if evidence:
                with st.expander(f"{evidence['id']} · {evidence.get('tool', '')}"):
                    st.code(display_text(evidence.get("payload", "")), language="json", wrap_lines=True)
    else:
        st.info("조사를 실행한 뒤 Memory에서 불러오면 근거와 타임라인을 확인할 수 있습니다.")


@st.fragment(run_every="5s")
def approvals():
    if not conversation["approvals"]:
        st.info("아직 변경 제안이 없습니다. 준비된 장애를 조사한 뒤 '재시작해줘'를 입력하세요.")
        return
    for aid in conversation["approvals"]:
        try:
            completed = conversation.setdefault("completed_approvals", {})
            a = completed.get(aid) or console.approval(aid, sid)
            if (a["status"] in {"SUCCEEDED", "FAILED", "REJECTED", "EXPIRED"}
                    and a.get("flow_status") in {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}):
                completed[aid] = a
        except Exception as exc:
            st.error(display_text(str(exc)))
            continue
        with st.container(border=True):
            st.subheader(f"EC2 재시작 · {a['status']}")
            st.caption(f"승인 요청 {aid} · Step Functions {a.get('flow_status', '대기')}")
            if a["status"] == "PENDING" and a.get("flow_status") == "RUNNING":
                st.caption("워크플로가 승인 결정을 기다리고 있습니다. EC2 재시작은 아직 시작하지 않았습니다.")
            left, right = st.columns(2)
            left.markdown(f"**Runbook**  \n{a['document']}")
            right.markdown(f"**대상**  \n{display_text(a['params'].get('InstanceId', '-'))}")
            st.write(display_text(a["reason"]))
            st.caption("근거: " + ", ".join(a.get("evidence") or []))
            if a["status"] == "PENDING":
                expired = datetime.fromisoformat(a["expires_at"]) <= datetime.now(timezone.utc)
                st.warning("승인하면 전용 데모 EC2를 실제로 중지 후 시작합니다. 재부팅과 다르며 수 분 걸릴 수 있습니다.")
                confirmed = st.checkbox("대상과 stop/start 영향을 확인했습니다.", key=f"confirm-{aid}", disabled=expired)
                approve, reject = st.columns(2)
                decision = None
                if approve.button("승인하고 실행", key=f"approve-{aid}", type="primary",
                                  disabled=expired or not confirmed, width="stretch"):
                    decision = "approve"
                if reject.button("거절", key=f"reject-{aid}", disabled=expired, width="stretch"):
                    decision = "reject"
                if expired:
                    st.error("승인 유효 시간이 지났습니다. 새 제안을 요청하세요.")
                if decision:
                    try:
                        console.decide(aid, sid, decision)
                        st.rerun(scope="fragment")
                    except Exception as exc:
                        st.error(display_text(str(exc)))
            elif a["status"] in {"APPROVED", "EXECUTING"}:
                st.info("모델 밖의 실행기가 처리 중입니다. 5초마다 AWS 상태를 확인합니다.")
            if a["status"] in {"APPROVED", "REJECTED"} and a.get("flow_status") == "RUNNING":
                if st.button("결정 전달 재시도", key=f"retry-{aid}"):
                    try:
                        console.resume_decision(aid, sid)
                    except Exception as exc:
                        st.error(display_text(str(exc)))
            if a.get("verify"):
                verify = a["verify"]
                if verify.get("result") == "PASS":
                    st.success("실행 후 검증 통과")
                else:
                    st.warning(f"검증 결과: {verify.get('result', a['status'])}")
                st.write(display_text(verify.get("summary", "")))
                st.caption("지표·알람: 준비된 데이터 · 실행 및 EC2 상태: 실제 AWS")
                with st.expander("검증 상세"):
                    st.code(display_text(verify), language="json", wrap_lines=True)
            elif a["status"] == "REJECTED":
                st.info("요청을 거절했습니다. SSM 재시작은 실행하지 않았습니다.")
                if a.get("flow_status") == "SUCCEEDED":
                    st.caption("Step Functions SUCCEEDED는 거절 결정을 처리하고 워크플로가 정상 종료됐다는 뜻입니다.")
            elif a["status"] in {"EXPIRED", "FAILED"}:
                st.warning(f"{a['status']} · 상태와 실행 기록을 확인하세요.")
            with st.expander("AWS 감사 기록"):
                st.code(display_text(a), language="json", wrap_lines=True)


with action_tab:
    st.subheader("제안은 에이전트가, 결정은 운영자가")
    st.caption("이 로컬 콘솔은 서버의 AWS IAM 자격으로 승인합니다. Slack 사용자 ID를 대신 입력하지 않습니다.")
    approvals()

st.divider()
st.caption("Runtime → Gateway + Policy → AWS 도구 · Memory  |  승인 → Step Functions → SSM → 검증")
