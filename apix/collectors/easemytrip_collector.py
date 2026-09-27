from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo
from apix.collectors.base import BaseCollector, CollectionTask
from apix.collectors.playwright_collector import PlaywrightCollector
from apix.collectors.registry import SourceRegistry
from apix.enums import SourceKind

IST = ZoneInfo("Asia/Kolkata")

@SourceRegistry.register("permitted_web_json", kind=SourceKind.PERMITTED_WEB, replace=True)
class EaseMyTripCollector(PlaywrightCollector):
    source_code = "permitted_web_json"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fare_response_pattern = "AirAvail_Lights/AirBus_New"

    def search_url(self, task: CollectionTask) -> str:
        formatted_date = task.departure_date.strftime("%d/%m/%Y")
        return (
            f"https://flight.easemytrip.com/FlightList/Index?"
            f"srch={task.origin}-City-India|{task.destination}-City-India|{formatted_date}"
            f"&px=1-0-0&cbn=0&ar=e&isSplit=false"
        )

    def extract(self, payload: dict[str, Any] | list[Any], task: CollectionTask) -> list[dict[str, Any]]:
        target_payload = None

        if isinstance(payload, dict):
            if "dctFltDtl" in payload:
                target_payload = payload
            elif "captured" in payload and isinstance(payload["captured"], list):
                for item in payload["captured"]:
                    body = item.get("body")
                    if isinstance(body, dict) and "dctFltDtl" in body:
                        target_payload = body
                        break

        if target_payload:
            return self.extract_fares(target_payload, task)
        return []

    def _parse_emt_datetime(self, date_str: str | None, time_str: str | None, fallback: datetime) -> str:
        if not date_str or not time_str:
            return fallback.isoformat()
        clean_date = date_str.strip()
        clean_time = time_str.strip()
        try:
            # Matches 'Sun-04Oct2026 18:40'
            dt = datetime.strptime(f"{clean_date} {clean_time}", "%a-%d%b%Y %H:%M")
            return dt.replace(tzinfo=IST).isoformat()
        except Exception:
            return fallback.isoformat()

    def extract_fares(self, raw_payload: Dict[str, Any], task: CollectionTask) -> List[Dict[str, Any]]:
        flt_details = raw_payload.get("dctFltDtl", {})
        flight_rows = []
        j_block = raw_payload.get("j")
        if isinstance(j_block, list) and len(j_block) > 0:
            flight_rows = j_block[0].get("s", [])

        fares: List[Dict[str, Any]] = []

        for item in flight_rows:
            b_legs = item.get("b", [])
            if not b_legs:
                continue
            leg_indices = b_legs[0].get("FL", [])
            if not leg_indices:
                continue

            seg = flt_details.get(str(leg_indices[0]))
            if not seg:
                continue

            airline_code = str(seg.get("AC", "")).strip().upper()
            flight_num = str(seg.get("FN", "")).strip()

            dep_dt_iso = self._parse_emt_datetime(seg.get("DDT"), seg.get("DTM"), task.departure_date)
            arr_dt_iso = self._parse_emt_datetime(seg.get("ADT"), seg.get("ATM"), task.departure_date)

            lst_fr = item.get("lstFr", [])
            if not lst_fr:
                continue

            primary_fare = lst_fr[0]
            base_fare = float(primary_fare.get("BF", 0.0))
            tax = float(primary_fare.get("TTXMP", 0.0))
            total_fare = float(primary_fare.get("TF", base_fare + tax))
            seats = int(primary_fare.get("SeatAv", 9))

            fares.append({
                "carrier_iata": airline_code,
                "flight_number": flight_num,
                "origin": task.origin,
                "destination": task.destination,
                "departure_local": dep_dt_iso,
                "arrival_local": arr_dt_iso,
                "base_fare": base_fare,
                "taxes_fees": tax,
                "total_fare": total_fare,
                "currency": task.currency or "INR",
                "seats_remaining": seats,
                "cabin": getattr(task, "cabin", "economy") or "economy",
                "inventory_status": "AVAILABLE",
            })

        return fares
