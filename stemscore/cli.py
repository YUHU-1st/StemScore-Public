from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .constants import DEFAULT_PROJECTS_ROOT
from .inventory import LocalRuntime
from .training.catalog import build_catalog
from .workflow import Workflow, find_project


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="stemscore", description="StemScore 本地 AI 扒谱流水线")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建项目并完成第 1 步")
    create.add_argument("source", type=Path)
    create.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    create.add_argument("--title")
    run = commands.add_parser("run", help="运行一个已解锁步骤")
    run.add_argument("project", type=Path)
    run.add_argument("stage", type=int, choices=range(1, 6))
    approve = commands.add_parser("approve", help="审查通过一个步骤")
    approve.add_argument("project", type=Path)
    approve.add_argument("stage", type=int, choices=range(1, 6))
    status = commands.add_parser("status", help="输出项目状态")
    status.add_argument("project", type=Path)
    inventory = commands.add_parser("inventory", help="扫描本地 MSST/UVR 模型")
    inventory.add_argument("output", type=Path)
    catalog = commands.add_parser("catalog", help="建立训练数据目录")
    catalog.add_argument("output", type=Path)
    catalog.add_argument("roots", nargs="+", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "create":
        workflow = Workflow.create(args.source, args.projects_root, args.title)
        workflow.run_stage(1, print)
        print(workflow.manifest.path)
        return 0
    if args.command == "inventory":
        LocalRuntime.defaults().save_report(args.output)
        print(args.output.resolve())
        return 0
    if args.command == "catalog":
        payload = build_catalog(args.roots, args.output)
        print(json.dumps(payload["counts"], ensure_ascii=False))
        return 0
    workflow = Workflow.load(find_project(args.project))
    if args.command == "run":
        workflow.run_stage(args.stage, print)
    elif args.command == "approve":
        workflow.approve(args.stage)
    elif args.command == "status":
        print(json.dumps([asdict(stage) for stage in workflow.manifest.stages], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

