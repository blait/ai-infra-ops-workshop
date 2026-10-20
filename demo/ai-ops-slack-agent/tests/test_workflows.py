from aiops.agent import RunResult
from aiops.workflows import extract_numbers, numbers_grounded

from .conftest import REQUESTER


def test_numbers_grounded_gate():
    table = "EC2 - Other  58.40  213.70  +155.30  +265.9%\n합계 753.76 916.83 +163.07"
    assert numbers_grounded("EC2 - Other가 155.30달러(+265.9%) 늘었습니다.", [table])[0]
    assert numbers_grounded("EC2 - Other가 약 266% 늘었습니다.", [table])[0]  # rounding allowed
    ok, bad = numbers_grounded("EC2 - Other가 512.00달러 늘었습니다.", [table])
    assert not ok and bad == [512.0]
    assert extract_numbers("+$1,234.50 and -3%") == [1234.5, -3.0]


async def test_cost_report_template_without_llm(pipeline):
    reply = await pipeline.handle(thread_id="w1", user_id=REQUESTER, text="어제 대비 비용이 튄 서비스는?")
    assert reply.route == "cost_report"
    assert "EC2 - Other" in reply.text and "+$155.30" in reply.text and "+265.9%" in reply.text
    assert reply.meta["summary_source"] == "template"
    assert pipeline.runner.calls == []  # no model call at all
    assert pipeline.notes.load("w1")["reports"][0]["kind"] == "cost_report"


async def test_cost_report_llm_summary_passes_gate(pipeline, runner):
    pipeline.workflows.use_llm = True

    async def good(spec, ctx, tools):
        return RunResult(name=spec.name, text="어제 EC2 - Other 비용이 155.30달러(+265.9%) 늘어 증가분 대부분을 차지했습니다.")

    runner.on("workflow_summary", good)
    reply = await pipeline.handle(thread_id="w2", user_id=REQUESTER, text="비용 리포트")
    assert reply.meta["summary_source"] == "llm"
    assert "155.30달러" in reply.text and "게이트 통과" in reply.text


async def test_cost_report_hallucinated_number_falls_back_to_template(pipeline, runner):
    pipeline.workflows.use_llm = True

    async def bad(spec, ctx, tools):
        return RunResult(name=spec.name, text="어제 EC2 - Other 비용이 4,812.00달러 늘었습니다.")

    runner.on("workflow_summary", bad)
    reply = await pipeline.handle(thread_id="w3", user_id=REQUESTER, text="비용 리포트")
    assert reply.meta["summary_source"] == "template"
    assert "4,812" not in reply.text.split("```")[0]
    assert "표에 없는 숫자" in reply.text
    assert any(r["event"] == "workflow_gate_failed" for r in pipeline.audit.read())


async def test_alarm_report(pipeline):
    reply = await pipeline.handle(thread_id="w4", user_id=REQUESTER, text="알람 현황")
    assert reply.route == "alarm_report"
    assert "checkout-api-5xx-rate-high" in reply.text and "ALARM 상태 알람은 3건" in reply.text
