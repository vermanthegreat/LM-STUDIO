"""Run exactly one explicitly confirmed company-website research job."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import AppConfig
from repositories.factory import get_contact_store
from research_job_models import ResearchJobStatus
from services.research_provider_composition import build_research_job_runner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one company-website research job")
    parser.add_argument("--run-one", action="store_true", help="confirm one queued company-website job")
    parser.add_argument("--worker-id", default="company-website-operator")
    return parser


def _summary(result: object) -> str:
    final_status = getattr(result, "final_status", None)
    status = getattr(final_status, "value", final_status) or "unknown"
    return (
        f"research job completed: status={status} "
        f"sources={getattr(result, 'materialized_source_count', 0)} "
        f"person_candidates={getattr(result, 'materialized_person_candidate_count', 0)} "
        f"contact_candidates={getattr(result, 'materialized_contact_candidate_count', 0)}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.run_one:
        print("refusing to run without --run-one")
        return 2
    if not isinstance(args.worker_id, str) or not args.worker_id.strip():
        print("invalid worker ID")
        return 2

    try:
        config = AppConfig.from_env()
        repository = get_contact_store(config)
        repository.init_db()
        queued = repository.list_research_jobs(status=ResearchJobStatus.QUEUED, limit=100)
        if len(queued) != 1 or queued[0].adapter_key != "company_website":
            print("refusing to run: exactly one queued company-website job is required")
            return 2
        runner = build_research_job_runner(repository=repository, worker_id=args.worker_id)
        result = runner.run_next(worker_id=args.worker_id)
        if result is None:
            print("research job was not available")
            return 1
        print(_summary(result))
        return 0 if result.terminal else 1
    except Exception:
        print("company-website research execution failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
