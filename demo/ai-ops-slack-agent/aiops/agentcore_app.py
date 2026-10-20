"""The agent "brain" behind the Amazon Bedrock AgentCore Runtime HTTP contract.

Contract (docs: runtime-http-protocol-contract): container listens on 0.0.0.0:8080, ARM64,
`GET /ping` -> {"status": "Healthy"}, `POST /invocations` with a JSON payload. AgentCore passes the
session id in `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`; the Slack worker Lambda sets it to
"slack-thread-<channel>-<thread_ts>", so one Slack thread = one runtime session = one Memory session.

Deployed wiring (infra/ sets these env vars; unset = local mode, same as the CLI):
  AIOPS_GATEWAY_URL + AIOPS_GATEWAY_TARGETS -> tools via AgentCore Gateway (SigV4) + Cedar policy
  AIOPS_MEMORY_ID                           -> thread notes/evidence in AgentCore Memory
The runtime has no approval store, no executor and no Slack token: it returns the reply (and the
approval id if it proposed a change); the worker Lambda posts it to Slack.

Request:  {"prompt": str, "user_id": str, "channel_id": str, "stream": bool}
Response: JSON {"status","response","blocks","route","approval_id","meta"} or, with stream=true,
          text/event-stream of {"type":"progress","message"} ... {"type":"result", ...}

  python -m aiops.agentcore_app      # local: http://127.0.0.1:8080/ping
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .config import load_settings
from .pipeline import OpsPipeline

log = logging.getLogger("aiops.agentcore")
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
_pipeline: OpsPipeline | None = None


def build_pipeline() -> OpsPipeline:
    settings = load_settings()
    remote = memory = None
    if settings.gateway_url:
        from .gateway_client import GatewayToolClient, parse_targets

        remote = GatewayToolClient(settings.gateway_url, parse_targets(settings.gateway_targets),
                                   region=settings.bedrock_region or "us-east-1")
    pipeline = OpsPipeline(settings, remote=remote)
    if settings.memory_id:
        from .memory import AgentCoreMemory, NotesMemorySync

        memory = NotesMemorySync(AgentCoreMemory(settings.memory_id, settings.bedrock_region or "us-east-1"),
                                 pipeline.notes, pipeline.audit)
        pipeline.memory = memory
    pipeline.audit.write("runtime_started", mode=settings.mode, model=settings.model,
                         gateway=bool(remote), memory=bool(memory), tools_targets=settings.gateway_targets)
    return pipeline


def get_pipeline() -> OpsPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline()
    return _pipeline


def _result(reply) -> dict:
    meta = {k: reply.meta.get(k) for k in ("route_rule", "run_id", "elapsed_s", "cost_usd", "tool_calls",
                                            "hook_denials", "evidence_created", "proposals", "gateway_calls",
                                            "memory", "decision", "manual_fix")}
    return {"status": "success", "response": reply.text, "blocks": reply.blocks, "route": reply.route,
            "approval_id": reply.approval_id, "meta": meta}


async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})


async def invocations(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"status": "error", "error": "body must be JSON"}, status_code=400)
    prompt = (payload or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return JSONResponse({"status": "error", "error": "'prompt' (string) is required"}, status_code=400)
    mode = payload.get("mode", "scenario")
    if mode not in {"scenario", "aws_live"}:
        return JSONResponse({"status": "error", "error": "mode must be scenario or aws_live"}, status_code=400)
    thread_id = request.headers.get(SESSION_HEADER) or payload.get("thread_id") or "agentcore-default"
    kwargs = dict(thread_id=thread_id, user_id=str(payload.get("user_id") or "agentcore"), text=prompt,
                  channel_id=str(payload.get("channel_id") or "agentcore"), mode=mode)
    pipeline = get_pipeline()

    if not payload.get("stream"):
        reply = await pipeline.handle(**kwargs)
        return JSONResponse(_result(reply))

    queue: asyncio.Queue = asyncio.Queue()

    async def progress(message: str) -> None:
        await queue.put({"type": "progress", "message": message})

    async def run() -> None:
        try:
            reply = await pipeline.handle(progress=progress, **kwargs)
            await queue.put({"type": "result", **_result(reply)})
        except Exception as exc:  # noqa: BLE001
            log.exception("invocation failed")
            await queue.put({"type": "result", "status": "error", "error": repr(exc)[:500]})
        await queue.put(None)

    async def events():
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            await task

    return StreamingResponse(events(), media_type="text/event-stream")


app = Starlette(routes=[Route("/ping", ping, methods=["GET"]), Route("/invocations", invocations, methods=["POST"])])


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=os.environ.get("AIOPS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
