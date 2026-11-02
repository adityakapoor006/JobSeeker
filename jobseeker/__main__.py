"""python -m jobseeker: run once. --tailor <job id> tailors a Stretch role on request."""
import argparse
import json

from . import db, liveness, pipeline
from .config import load_settings, save_settings
from .resume import load_resume


def main() -> None:
    parser = argparse.ArgumentParser(prog="jobseeker")
    parser.add_argument("--tailor", metavar="JOB_ID", help="tailor one role, e.g. a Stretch role")
    parser.add_argument("--setup", action="store_true", help="choose job locations")
    parser.add_argument("--check", action="store_true", help="check every stored role is still open")
    args = parser.parse_args()
    if args.check:
        with db.connect() as conn:
            print(json.dumps(liveness.verify_jobs(conn)))
        return
    if args.setup:
        settings = load_settings()
        default = ", ".join(settings["locations"])
        answer = input(f"Job locations, comma separated [{default}]: ").strip()
        if answer:
            settings["locations"] = [city.strip() for city in answer.split(",") if city.strip()]
        save_settings(settings)
        print("Locations:", ", ".join(settings["locations"]))
        return
    if args.tailor:
        resume, _ = load_resume()
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.tailor,)).fetchone()
            if not row:
                raise SystemExit(f"No job with id {args.tailor}")
            print(pipeline.tailor(conn, dict(row), resume))
        return
    print(json.dumps(pipeline.run(), indent=2))


if __name__ == "__main__":
    main()
