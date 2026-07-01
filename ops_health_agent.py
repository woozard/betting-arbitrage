"""Scheduler job: monitor book scanners + arb engine; auto-remediate safe failures."""

from utils.config import OPS_HEALTH_CHECK_ENABLED
from utils.logger import Logger
from utils.ops_health import run_health_cycle


def main():
    logger = Logger.get_logger("ops-health")
    if not OPS_HEALTH_CHECK_ENABLED:
        logger.info("Ops health agent disabled (OPS_HEALTH_CHECK_ENABLED=false)")
        return

    logger.info("========== Ops Health Check (START) ==========")
    summary = run_health_cycle(logger=logger)
    logger.info(
        f"Ops health: {len(summary['issues'])} issue(s), "
        f"{len(summary['remediated'])} remediation(s)"
    )
    logger.info("========== Ops Health Check (END) ==========")


if __name__ == "__main__":
    main()
