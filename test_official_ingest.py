import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from apix.services.ingestion import CollectionService, build_plan
from apix.services.cleaning import clean_raw_observations
from apix.models import CollectionRun, RawObservation, CanonicalFare

def main():
    print("[1/3] Resolving plan for DEL-BOM via permitted_web_json...")
    plan = build_plan(
        route_codes=["DEL-BOM"],
        source_codes=["permitted_web_json"],
        lead_windows=[7],
    )
    print(f"Plan resolved: routes={plan.routes}, sources={plan.sources}, lead_windows={plan.lead_windows}")

    print("[2/3] Provisioning CollectionRun record...")
    run = CollectionRun.objects.create(status="RUNNING")
    print(f"Created CollectionRun id={run.id}")

    print("[3/3] Executing CollectionService...")
    service = CollectionService()
    service.execute(run, plan)

    raw_obs = list(RawObservation.objects.filter(collection_run=run))
    if raw_obs:
        print("[4/4] Normalizing and cleaning raw observations into TimescaleDB...")
        report = clean_raw_observations(raw_obs, collection_run=run)
        run.refresh_from_db()
        print(f"Cleaning complete: {report.counters.written} canonical fares written, {report.counters.duplicates} duplicates dropped.")

    print("\n================ PIPELINE EXECUTION SUMMARY ================")
    print(f"Final Run Status : {run.status}")
    print(f"Raw Count        : {run.raw_count}")
    print(f"Canonical Count  : {CanonicalFare.objects.filter(collection_run=run).count()}")
    error = getattr(run, "error_message", getattr(run, "error", None))
    print(f"Run Error        : {error}")
    print("============================================================\n")

if __name__ == "__main__":
    main()
