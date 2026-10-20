"""Build the Lambda code bundle at synth time (no Docker needed for Lambdas).

Contents: aiops/ (only modules that do not need claude_agent_sdk are imported by the handlers),
runbooks/, fixtures/, config/, plus boto3 (pinned, newer than the Lambda-provided one: it must know the
bedrock-agentcore data plane) and tzdata. Pure-Python wheels, so the host platform does not matter.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[2]
BUILD_DIR = DEMO_DIR / "infra" / "build" / "lambda"
COPY = ("aiops", "runbooks", "fixtures", "config")
PIP = ("boto3==1.43.114", "tzdata==2026.5")


def build_lambda_bundle() -> Path:
    deps_dir = DEMO_DIR / "infra" / "build" / "lambda-deps"
    marker = deps_dir / ".pins"
    if not marker.exists() or marker.read_text() != "\n".join(PIP):
        shutil.rmtree(deps_dir, ignore_errors=True)
        deps_dir.mkdir(parents=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--no-compile", "--target", str(deps_dir),
                        "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12", *PIP], check=True)
        marker.write_text("\n".join(PIP))
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    shutil.copytree(deps_dir, BUILD_DIR, ignore=shutil.ignore_patterns(".pins", "__pycache__", "*.pyc"))
    for name in COPY:
        shutil.copytree(DEMO_DIR / name, BUILD_DIR / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "services.toml"))
    return BUILD_DIR
