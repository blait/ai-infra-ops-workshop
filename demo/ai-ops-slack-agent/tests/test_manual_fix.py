"""Manual remediation draft: schema, code gate, reviewer application, formatting, router, pipeline paths."""

import json
from dataclasses import replace

import pytest

from aiops import manual_fix as mf
from aiops import prompts
from aiops.agent import ManualFix, RunResult
from aiops.docs import DocsError, GatewayDocsClient, KnowledgeMcpClient, norm_url, parse_read, parse_search
from aiops.formatting import manual_fix_render
from aiops.pipeline import OpsPipeline
from aiops.router import classify
from aiops.tools import RunContext, build_tools

from .conftest import I1, REQUESTER, call_tool

DOC_RDS = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Managing.html"
DOC_CW = "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.AuroraMonitoring.Metrics.html"
INVENTED = "https://docs.aws.amazon.com/made-up/page.html"


def search_text(*urls: str) -> str:
    return json.dumps({"content": {"result": [
        {"rank_order": n, "title": f"doc {n}", "url": u,
         "context": "aws rds modify-db-cluster-parameter-group --db-cluster-parameter-group-name name "
                    "--parameters ParameterName=max_connections,ParameterValue=value,ApplyMethod=pending-reboot"}
        for n, u in enumerate(urls, 1)]}})


class FakeDocs:
    def __init__(self, urls=(DOC_RDS, DOC_CW)):
        self.urls = urls
        self.calls: list[tuple[str, dict]] = []

    async def call(self, remote_tool, args):
        self.calls.append((remote_tool, args))
        if remote_tool == "aws___search_documentation":
            return search_text(*self.urls), {"tool": remote_tool, "via": "fake", "ms": 1, "outcome": "ok"}
        url = args["requests"][0]["url"]
        return json.dumps({"content": {"result": [{"status": "SUCCESS", "url": url, "content": "# page\nbody",
                                                   "truncated": False, "end_index": 10, "total_length": 10}]}}), \
            {"tool": remote_tool, "via": "fake", "ms": 1, "outcome": "ok"}


def item(**kw):
    base = {"purpose": "p", "command": "", "console_path": "", "read_only": True, "risk_note": "r",
            "doc_urls": [DOC_RDS]}
    return {**base, **kw}


HITS = {DOC_RDS: {"title": "rds", "text": "modify-db-cluster-parameter-group"}, DOC_CW: {"title": "cw", "text": "x"}}


# ------------------------------------------------------------------------------ schema
def test_schema_shapes():
    step = mf.MANUAL_FIX_SCHEMA["properties"]["steps"]["items"]
    assert {"purpose", "command", "console_path", "read_only", "risk_note", "doc_urls", "phase"} <= set(step["properties"])
    assert step["properties"]["phase"]["enum"] == ["precheck", "change"]
    assert {"steps", "verification", "rollback"} <= set(mf.MANUAL_FIX_SCHEMA["required"])
    assert "no_runbook" in prompts.CHANGE_SCHEMA["properties"]["decision"]["enum"]
    assert set(mf.FIX_REVIEW_SCHEMA["properties"]["items"]["items"]["required"]) == {"ref", "verdict", "destructive", "note"}


# ------------------------------------------------------------------------------ gate
def test_gate_removes_steps_without_tool_returned_doc_url():
    draft = {"applicable": True, "summary": "s", "steps": [
        item(phase="change", command="aws rds modify-db-cluster-parameter-group --db-cluster-parameter-group-name <g>",
             read_only=False, doc_urls=[INVENTED]),                      # invented URL only -> removed
        item(phase="precheck", command="aws rds describe-db-cluster-parameters --db-cluster-parameter-group-name <g>",
             doc_urls=[DOC_RDS + "#anchor", INVENTED]),                  # one valid (fragment ignored) -> kept
        item(phase="precheck", command="aws cloudwatch get-metric-statistics", doc_urls=[]),  # none -> removed
    ], "verification": [], "rollback": []}
    out, issues = mf.gate_manual_fix(draft, HITS)
    assert [s["command"].split()[2] for s in out["steps"]] == ["describe-db-cluster-parameters"]
    assert out["steps"][0]["doc_urls"] == [DOC_RDS]
    assert any("근거 문서 URL 없음 → 삭제" in i for i in issues)
    assert any("도구 결과에 없는 URL 제거" in i for i in issues)
    assert out["doc_urls"] == [DOC_RDS] and out["applicable"] is False  # no change step left


def test_gate_orders_derives_read_only_and_flags_destructive():
    draft = {"applicable": True, "summary": "s", "steps": [
        item(phase="change", command="aws ec2 revoke-security-group-ingress --group-id <sg>", read_only=False),
        item(phase="precheck", command="aws rds modify-db-instance --db-instance-identifier x", read_only=True),
        item(phase="precheck", command="aws rds describe-db-instances", read_only=False),
    ], "verification": [item(command="aws rds reboot-db-instance --db-instance-identifier x")],
        "rollback": [item(console_path="RDS > Parameter groups > Edit", read_only=False)]}
    out, issues = mf.gate_manual_fix(draft, HITS)
    phases = [(s["phase"], s["command"].split()[2]) for s in out["steps"]]
    assert phases == [("precheck", "describe-db-instances"), ("change", "revoke-security-group-ingress"),
                      ("change", "modify-db-instance")]  # read-only confirmation first, mislabeled precheck moved
    assert [s["ref"] for s in out["steps"]] == ["S1", "S2", "S3"]
    assert out["steps"][0]["read_only"] is True  # model said False, verb says describe
    assert out["steps"][1]["destructive"] is True and out["steps"][2]["destructive"] is False
    assert out["verification"] == []  # a reboot is not a verification step
    assert any("검증 단계에 변경 명령 → 삭제" in i for i in issues)
    assert out["rollback"][0]["ref"] == "R1" and out["applicable"] is True


def test_gate_redacts_secrets_and_unseen_resource_ids_and_drops_open_world():
    evidence = "error on i-0a1b2c3d4e5f60001"
    draft = {"applicable": True, "summary": "s", "steps": [
        item(phase="change", read_only=False, command=f"aws ec2 stop-instances --instance-ids {I1} i-0bbbbbbbbbbbbbbbb"),
        item(phase="change", read_only=False,
             command="aws rds modify-db-cluster --master-user-password Sup3rS3cret! --cluster x "
                     "--kms arn:aws:kms:us-east-1:123456789012:key/abc AKIAABCDEFGHIJKLMNOP"),
        item(phase="change", read_only=False,
             command="aws ec2 authorize-security-group-ingress --group-id <sg> --cidr 0.0.0.0/0"),
    ], "verification": [], "rollback": []}
    out, issues = mf.gate_manual_fix(draft, HITS, evidence)
    c1, c2 = out["steps"][0]["command"], out["steps"][1]["command"]
    assert I1 in c1 and "i-0bbbbbbbbbbbbbbbb" not in c1 and "<instance-id>" in c1  # value from evidence kept
    assert "Sup3rS3cret" not in c2 and "<secret>" in c2 and "AKIA" not in c2 and "<arn>" in c2
    assert len(out["steps"]) == 2 and any("정책 금지" in i for i in issues)
    assert out["steps"][0]["destructive"] is True and out["steps"][1]["destructive"] is False  # stop- flagged


def test_command_is_read_only():
    assert mf.command_is_read_only("aws --region us-east-1 rds describe-db-instances") is True
    assert mf.command_is_read_only("aws logs start-query --log-group-name g") is True
    assert mf.command_is_read_only("aws rds modify-db-cluster-parameter-group") is False
    assert mf.command_is_read_only("kubectl get pods") is None
    assert mf.command_is_read_only("SHOW max_connections;") is True
    assert mf.command_is_read_only("psql -h <host> -c \"SELECT count(*) FROM pg_stat_activity\"") is True
    assert mf.command_is_read_only("SELECT 1; DROP TABLE x") is False
    assert not mf.DESTRUCTIVE.search("aws rds modify-db-parameter-group --parameters ApplyMethod=pending-reboot ")
    assert mf.DESTRUCTIVE.search("aws rds reboot-db-instance --db-instance-identifier x")


# ------------------------------------------------------------------------------ reviewer application
def gated_fix():
    draft = {"applicable": True, "summary": "s", "steps": [
        item(phase="precheck", command="aws rds describe-db-clusters"),
        item(phase="change", read_only=False, command="aws rds modify-db-cluster-parameter-group --x <g>")],
        "verification": [item(command="aws cloudwatch get-metric-statistics --metric-name DatabaseConnections")],
        "rollback": [item(read_only=False, command="aws rds reset-db-cluster-parameter-group --x <g>")]}
    return mf.gate_manual_fix(draft, HITS)[0]


def test_apply_review_drops_ungrounded_and_unreviewed_and_only_adds_flags():
    review = {"reviewer_note": "ok", "items": [
        {"ref": "S1", "verdict": "grounded", "destructive": False, "note": "발췌에 있음"},
        {"ref": "S2", "verdict": "grounded", "destructive": True, "note": "재부팅 필요"},
        {"ref": "V1", "verdict": "ungrounded", "destructive": False, "note": "다른 서비스 문서"}]}  # R1 not judged
    out, issues = mf.apply_review(gated_fix(), review)
    assert [s["ref"] for s in out["steps"]] == ["S1", "S2"] and out["steps"][1]["destructive"] is True
    assert out["verification"] == [] and out["rollback"] == []
    assert any(i.startswith("근거 없음 → 삭제: V1") for i in issues)
    assert any(i.startswith("검토 누락 → 삭제: R1") for i in issues)


def test_apply_review_failure_shows_nothing():
    out, issues = mf.apply_review(gated_fix(), None)
    assert out["steps"] == [] and out["applicable"] is False and "검토자 실행 실패" in issues[0]


# ------------------------------------------------------------------------------ formatting
def test_formatting_separate_block_with_title_warning_and_doc_urls():
    out, _ = mf.apply_review(gated_fix(), {"reviewer_note": "n", "items": [
        {"ref": r, "verdict": "grounded", "destructive": r == "S2", "note": "ok"} for r in ("S1", "S2", "V1", "R1")]})
    blocks, text = manual_fix_render(ManualFix(fix=out, issues=["근거 없음 → 삭제: S9 x"], runs=[]))
    header = [b for b in blocks if b["type"] == "header"][0]
    assert header["text"]["text"] == "수동 조치 초안 · 검증 필요 (실행은 사람이)"
    body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "⚠️ S2" in body and "사전 확인 (읽기 전용)" in body and "검증 방법" in body and "되돌리기" in body
    assert f"<{DOC_RDS}|" in body and "&lt;g&gt;" in body  # placeholder escaped for Slack, link not escaped
    assert body.index("*1. 사전 확인") < body.index("*2. 변경*") < body.index("*3. 검증 방법") < body.index("*4. 되돌리기")
    assert "수동 조치 초안 · 검증 필요 (실행은 사람이)" in text and "$ aws rds describe-db-clusters" in text
    assert DOC_RDS in text and "근거 없음 → 삭제" in text
    empty_blocks, empty_text = manual_fix_render(None)
    assert empty_blocks == [] and empty_text == ""


# ------------------------------------------------------------------------------ router
@pytest.mark.parametrize("text,kind", [
    ("보안그룹 열어줘", "policy_forbid"),
    ("checkout-api 보안 그룹 인바운드 443 추가해줘", "policy_forbid"),
    ("sg-0c4f1e2d3b4a59687 22번 포트 열어 주세요", "policy_forbid"),
    ("보안그룹 변경이 원인이야?", "investigate"),
    ("checkout DB 커넥션 풀 늘려줘", "change_request"),
])
def test_router_sg_open_is_hard_refusal(text, kind):
    assert classify(text).kind == kind


# ------------------------------------------------------------------------------ doc clients / tool wrapper
def test_parse_search_and_read_and_norm_url():
    rows = parse_search(search_text(DOC_RDS))
    assert rows[0]["url"] == DOC_RDS and "modify-db-cluster-parameter-group" in rows[0]["context"]
    read = parse_read(json.dumps({"content": {"result": [{"status": "SUCCESS", "url": DOC_RDS, "content": "c"}]}}))
    assert read[0]["content"] == "c"
    assert norm_url(DOC_RDS + "/#x?y") == norm_url(DOC_RDS)
    with pytest.raises(DocsError):
        parse_search("not json")


def test_knowledge_client_posts_tools_call():
    seen = {}

    class Resp:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"isError": False, "content": [
                {"type": "text", "text": search_text(DOC_RDS)}]}}).encode()

    def opener(req, timeout):
        seen["body"] = json.loads(req.data)
        return Resp()

    text, info = KnowledgeMcpClient(opener=opener).call_sync("aws___search_documentation", {"search_phrase": "x"})
    assert seen["body"]["method"] == "tools/call" and seen["body"]["params"]["name"] == "aws___search_documentation"
    assert parse_search(text)[0]["url"] == DOC_RDS and info["via"] == "direct"
    with pytest.raises(DocsError):
        KnowledgeMcpClient(opener=opener).call_sync("aws___retrieve_skill", {})  # only search/read


def test_gateway_docs_client_uses_target_prefixed_name_and_reports_policy_denial():
    class Gw:
        def __init__(self, res):
            self.res, self.sent = res, None

        def rpc(self, method, params):
            self.sent = (method, params)
            return self.res

    ok = Gw({"result": {"content": [{"type": "text", "text": search_text(DOC_RDS)}]}})
    text, info = GatewayDocsClient(ok, "awsdocs").call_sync("aws___search_documentation", {"search_phrase": "x"})
    assert ok.sent[1]["name"] == "awsdocs___aws___search_documentation" and info["outcome"] == "ok"
    denied = Gw({"result": {"isError": True, "content": [{"type": "text", "text":
                "Tool Execution Denied: Tool call not allowed due to policy enforcement"}]}})
    with pytest.raises(DocsError) as e:
        GatewayDocsClient(denied, "awsdocs").call_sync("aws___search_documentation", {"search_phrase": "x"})
    assert e.value.outcome == "denied" and e.value.info["gateway_tool"].startswith("awsdocs___")


async def test_doc_tool_wrapper_records_only_returned_urls(settings):
    p = OpsPipeline(settings, docs=FakeDocs())
    ctx = RunContext(thread_id="d1", user_id=REQUESTER, route="change_request")
    tools = build_tools(ctx, p.deps)
    out = await call_tool(tools, "aws_docs_search", {"search_phrase": "rds max_connections", "limit": 9})
    assert [r["url"] for r in out["results"]] == [DOC_RDS, DOC_CW] and set(ctx.doc_hits) == {DOC_RDS, DOC_CW}
    bad = await call_tool(tools, "aws_docs_read", {"url": "https://example.com/x"})
    assert bad.get("_is_error")
    read = await call_tool(tools, "aws_docs_read", {"url": DOC_RDS})
    assert read["url"] == DOC_RDS and "body" in ctx.doc_hits[DOC_RDS]["text"]
    assert any(r["event"] == "doc_tool_result" for r in p.audit.read())


# ------------------------------------------------------------------------------ pipeline paths
def drafter_fake(urls=(DOC_RDS,), invented=True):
    async def fn(spec, ctx, tools):
        assert set(spec.tools) == {"aws_docs_search", "aws_docs_read", "evidence_get"}
        await call_tool(tools, "aws_docs_search", {"search_phrase": "aurora max_connections"})
        steps = [item(phase="precheck", command="aws rds describe-db-cluster-parameters --db-cluster-parameter-group-name <g>",
                      doc_urls=list(urls)),
                 item(phase="change", read_only=False,
                      command="aws rds modify-db-cluster-parameter-group --db-cluster-parameter-group-name <g> "
                              "--parameters ParameterName=max_connections,ParameterValue=<new-value>,ApplyMethod=pending-reboot",
                      doc_urls=list(urls))]
        if invented:
            steps.append(item(phase="change", read_only=False, command="aws rds failover-db-cluster",
                              doc_urls=[INVENTED]))
        return RunResult(name=spec.name, cost_usd=0.02, structured={
            "applicable": True, "summary": "max_connections 확인 후 조정", "steps": steps,
            "verification": [item(command="aws rds describe-db-cluster-parameters --source user", doc_urls=list(urls))],
            "rollback": [item(read_only=False, command="aws rds modify-db-cluster-parameter-group --x <old-value>",
                              doc_urls=list(urls))]})
    return fn


async def reviewer_fake(spec, ctx, tools):
    assert spec.tools == [] and spec.agents is None  # tool-less
    assert "modify-db-cluster-parameter-group" in spec.prompt and DOC_RDS in spec.prompt  # sees doc snippets
    assert "failover-db-cluster" not in spec.prompt  # gate removed the invented-URL step before review
    return RunResult(name=spec.name, cost_usd=0.01, structured={"reviewer_note": "발췌와 일치", "items": [
        {"ref": r, "verdict": "grounded", "destructive": False, "note": "ok"} for r in ("S1", "S2", "V1", "R1")]})


async def test_no_runbook_change_request_returns_draft(settings, runner):
    p = OpsPipeline(settings, runner=runner, docs=FakeDocs())
    runner.deps = p.deps

    async def change(spec, ctx, tools):
        await call_tool(tools, "runbook_list", {})
        return RunResult(name=spec.name, cost_usd=0.01, structured={
            "decision": "no_runbook", "message": "DB 커넥션 설정을 바꾸는 runbook이 없습니다."})

    runner.on("change", change)
    runner.on("fix-drafter", drafter_fake())
    runner.on("fix-reviewer", reviewer_fake)
    reply = await p.handle(thread_id="m1", user_id=REQUESTER, text="checkout DB 커넥션 풀 늘려줘", channel_id="C1")
    assert [c.name for c in runner.calls] == ["change", "fix-drafter", "fix-reviewer"]
    assert reply.route == "change_request" and reply.approval_id is None
    assert reply.text.startswith("*허용된 runbook 없음*")
    assert "수동 조치 초안 · 검증 필요 (실행은 사람이)" in reply.text and DOC_RDS in reply.text
    fix = reply.meta["manual_fix"]["fix"]
    assert [s["ref"] for s in fix["steps"]] == ["S1", "S2"] and fix["doc_urls"] == [DOC_RDS]
    assert any("도구 결과에 없는 URL" in i or "URL 없음" in i for i in reply.meta["manual_fix"]["issues"])
    assert reply.meta["cost_usd"] == pytest.approx(0.04)
    assert not p.approvals.store.list("m1")  # a draft never creates an approval record
    events = [r["event"] for r in p.audit.read()]
    assert "no_runbook" in events and "manual_fix_gate" in events and "manual_fix_review" in events


async def test_runbook_path_and_declined_do_not_draft(settings, runner):
    p = OpsPipeline(settings, runner=runner, docs=FakeDocs())
    runner.deps = p.deps

    async def declined(spec, ctx, tools):
        return RunResult(name=spec.name, structured={"decision": "declined", "message": "근거 부족"})

    runner.on("change", declined)
    reply = await p.handle(thread_id="m2", user_id=REQUESTER, text="재시작해줘")
    assert [c.name for c in runner.calls] == ["change"] and reply.meta["manual_fix"] is None


async def test_policy_forbid_sg_open_has_no_draft_and_no_model(settings, runner):
    p = OpsPipeline(settings, runner=runner, docs=FakeDocs())
    reply = await p.handle(thread_id="m3", user_id=REQUESTER, text="보안그룹 열어줘")
    assert reply.route == "policy_forbid" and runner.calls == []
    assert "수동 조치 초안" not in reply.text and "manual_fix" not in reply.meta


async def test_investigation_includes_draft_when_enabled(settings, runner):
    from .test_pipeline import evaluator_fake, investigator_fake, reviser_fake

    p = OpsPipeline(replace(settings, manual_fix_draft=True), runner=runner, docs=FakeDocs())
    runner.deps = p.deps
    runner.on("investigator", investigator_fake())
    runner.on("evaluator", evaluator_fake)
    runner.on("reviser", reviser_fake)
    runner.on("fix-drafter", drafter_fake(invented=False))
    runner.on("fix-reviewer", reviewer_fake)
    reply = await p.handle(thread_id="m4", user_id=REQUESTER, text="checkout-api 5xx 늘었어, 원인 봐줘")
    assert [c.name for c in runner.calls] == ["investigator", "evaluator", "reviser", "fix-drafter", "fix-reviewer"]
    assert reply.meta["manual_fix"]["fix"]["steps"] and "수동 조치 초안" in reply.text
    assert any(b["type"] == "header" for b in reply.blocks)
    assert reply.meta["cost_usd"] == pytest.approx(0.1 + 0.02 + 0.01 + 0.02 + 0.01)


async def test_drafter_failure_is_reported_not_fatal(settings, runner):
    p = OpsPipeline(settings, runner=runner, docs=FakeDocs())
    runner.deps = p.deps
    runner.on("change", lambda spec, ctx, tools: _ret(RunResult(name="change", structured={
        "decision": "no_runbook", "message": "허용된 runbook 없음"})))
    reply = await p.handle(thread_id="m5", user_id=REQUESTER, text="checkout DB 커넥션 풀 늘려줘")
    assert "초안을 만들지 못했습니다" in reply.text and reply.meta["manual_fix"]["error"]


async def _ret(x):
    return x
