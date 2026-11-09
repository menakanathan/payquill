"""Run the scheduler once from the command line: send due reminders, release
scheduled payouts, and sync statuses from PayPal. Useful with a local cron.
(The web app also runs this job itself every SCHEDULER_INTERVAL_SECONDS.)

    python -m payquill.jobs
"""

from .app import build_service


def main() -> None:
    service = build_service()
    result = service.run_scheduler()
    print(f"[payquill] {result['today']}: {result['reminders_sent']} reminders sent, "
          f"{result['payouts_released']} payouts released")


if __name__ == "__main__":
    main()
