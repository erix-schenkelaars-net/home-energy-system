import os
import asyncio
import logging
import csv
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import time

from app.models import (
    SystemStatus,
    AgentAdvice,
    SafetyConstraints,
    HealthStatus
)
from app.agent import EnergyAgent
from app.db_client import DBClient

# Setup logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/app/logs/agent.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Energy Agent", version="0.3.0")

agent: Optional[EnergyAgent] = None
db_client: Optional[DBClient] = None
start_time = time.time()
advice_count = 0
comparison_log_file = "/app/logs/comparison.csv"
_last_reset_date: Optional[str] = None   # tracks date of last 06:00 conversation reset

# Cached last advice — updated by background task
last_advice: Optional[dict] = None
last_advice_time: Optional[datetime] = None

DAILY_RUN_HOUR   = int(os.getenv("DAILY_RUN_HOUR",   "6"))
DAILY_RUN_MINUTE = int(os.getenv("DAILY_RUN_MINUTE", "30"))
DB_RETRY_COUNT = int(os.getenv("DB_RETRY_COUNT", "3"))
DB_RETRY_DELAY_S = int(os.getenv("DB_RETRY_DELAY_S", "30"))

SAFETY_CONSTRAINTS = SafetyConstraints(
    min_soc_pct=20.0,
    max_soc_pct=95.0,
    max_charge_rate_kw=3.0,
    max_discharge_rate_kw=3.0
)


def build_status_from_db(row: dict) -> SystemStatus:
    """Map a DB row to SystemStatus"""
    ts = row.get("ts") or datetime.now()
    age = (datetime.now() - ts).total_seconds() if isinstance(ts, datetime) else None

    pv_kw = (row.get("sph_pv_power_tot_w") or 0.0) / 1000
    grid_import_w = row.get("p1_power_import_w") or 0.0
    grid_export_w = row.get("p1_power_export_w") or 0.0
    grid_kw = (grid_import_w - grid_export_w) / 1000

    bat_dir = row.get("seplos_direction") or "idle"
    bat_power_w = abs(row.get("seplos_power_w") or 0.0)
    bat_kw = bat_power_w / 1000
    bat_net_kw = bat_kw if bat_dir == "discharge" else -bat_kw

    load_kw = pv_kw + grid_kw + bat_net_kw
    hp_power_w = row.get("sparrow_input_power_w") or row.get("sparrow_output_power_w") or 0.0

    return SystemStatus(
        timestamp=ts,
        data_age_seconds=age,
        soc_pct=row.get("seplos_soc_pct") or 0.0,
        battery_direction=bat_dir,
        battery_power_kw=round(bat_kw, 3),
        pv_kw=round(pv_kw, 3),
        pv_today_kwh=row.get("sph_pv_energy_today_kwh"),
        grid_kw=round(grid_kw, 3),
        load_kw=round(max(load_kw, 0.0), 3),
        tariff_eur_kwh=0.25,  # overwritten by caller if schedule available
        inverter_state="online" if pv_kw > 0 or bat_kw > 0 else "standby",
        heat_pump_status=row.get("sparrow_status"),
        heat_pump_power_kw=round(hp_power_w / 1000, 3),
        outside_temp_c=row.get("sparrow_outside_temp_c"),
    )


def apply_current_price(status: SystemStatus, schedule: Optional[list]) -> SystemStatus:
    """Replace hardcoded tariff with actual spot price from battery_schedule (kwartierslot)."""
    if not schedule:
        return status
    now = datetime.now()
    now_quarter = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    now_str = now_quarter.strftime("%Y-%m-%d %H:%M")
    for slot in schedule:
        slot_dt = slot.get("slot_dt")
        if slot_dt and str(slot_dt)[:16] == now_str:
            price = slot.get("price_eur_kwh")
            if price:
                status.tariff_eur_kwh = round(float(price), 4)
            break
    return status


def _load_recent_history(days: int = 7) -> str:
    """Load comparison history from comparison.csv and return a summary."""
    if not os.path.exists(comparison_log_file):
        return "No comparison history available."
    cutoff = datetime.now() - timedelta(days=days)
    rows = []
    try:
        with open(comparison_log_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    if datetime.fromisoformat(row['timestamp']) >= cutoff:
                        rows.append(row)
                except Exception:
                    continue
    except Exception:
        return "Comparison history not readable."
    if not rows:
        return "No recent analyses (agent just started)."
    total  = len(rows)
    agrees = sum(1 for r in rows if r.get('agrees_with_optimizer') == 'True')
    agree_pct = round(agrees / total * 100) if total else 0
    agent_actions: dict = {}
    for r in rows:
        a = r.get('agent_action', '')
        agent_actions[a] = agent_actions.get(a, 0) + 1
    lines = [
        f"Last {days} days: {total} daily analyses, {agree_pct}% agrees with optimizer.",
        f"Agent actions distribution: {dict(sorted(agent_actions.items(), key=lambda x: -x[1]))}",
        "Last 3 analyses:",
    ]
    for r in rows[-3:]:
        lines.append(
            f"  {r.get('timestamp','')[:16]}: agent={r.get('agent_action','')} "
            f"opt={r.get('optimizer_action','')} agrees={r.get('agrees_with_optimizer','')}"
        )
    return "\n".join(lines)


async def _fetch_with_retry() -> Optional[dict]:
    """Fetch latest DB row, retrying on failure."""
    for attempt in range(1, DB_RETRY_COUNT + 1):
        try:
            row = db_client.get_latest()
            if row:
                return row
            logger.warning(
                f"DB: no data (attempt {attempt}/{DB_RETRY_COUNT})"
            )
        except Exception as e:
            logger.error(
                f"DB: error fetching row (attempt {attempt}/{DB_RETRY_COUNT}): {e}"
            )
        if attempt < DB_RETRY_COUNT:
            logger.info(f"DB: retry in {DB_RETRY_DELAY_S}s…")
            await asyncio.sleep(DB_RETRY_DELAY_S)
    logger.warning(
        f"DB: no data after {DB_RETRY_COUNT} attempts — cycle skipped"
    )
    return None


async def run_daily_analysis():
    """Run one daily analysis: fetch full day schedule and ask Claude for evaluation."""
    global last_advice, last_advice_time, advice_count

    try:
        agent.reset_conversation()

        row = await _fetch_with_retry()
        if not row:
            return

        status = build_status_from_db(row)

        schedule = None
        try:
            schedule = db_client.get_schedule(slots=96)
        except Exception as e:
            logger.error(f"Schedule fetch failed: {e}")

        status = apply_current_price(status, schedule)
        history = _load_recent_history(days=7)

        advice = agent.get_advice(status, SAFETY_CONSTRAINTS, schedule, history=history)
        advice_count += 1
        last_advice_time = datetime.now()
        last_advice = {
            "timestamp": last_advice_time.isoformat(),
            "status": status.model_dump(),
            "advice": advice.model_dump(),
            "mode": "shadow"
        }
        logger.info(
            f"[daily] Advice: {advice.action} | agrees={advice.agrees_with_optimizer} "
            f"| SoC={status.soc_pct}% PV={status.pv_kw}kW"
        )

        optimizer_action = schedule[0].get("action", "") if schedule else ""
        with open(comparison_log_file, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([
                status.timestamp.isoformat(),
                f"{status.data_age_seconds:.0f}" if status.data_age_seconds else "",
                f"{status.soc_pct:.1f}",
                f"{status.pv_kw:.3f}",
                f"{status.load_kw:.3f}",
                f"{status.grid_kw:.3f}",
                status.battery_direction or "",
                f"{status.battery_power_kw:.3f}" if status.battery_power_kw else "",
                f"{status.heat_pump_power_kw:.3f}" if status.heat_pump_power_kw else "",
                f"{status.tariff_eur_kwh:.4f}",
                optimizer_action,
                advice.action,
                advice.reason,
                f"{advice.confidence_pct:.0f}",
                advice.agrees_with_optimizer,
                "",
                "shadow"
            ])

    except Exception as e:
        logger.error(f"Daily analysis unexpected error: {e}", exc_info=True)


async def advice_loop():
    """Daily analysis: run immediately at startup, then once per day at DAILY_RUN_HOUR:DAILY_RUN_MINUTE."""
    logger.info(f"Advice loop started — daily at {DAILY_RUN_HOUR:02d}:{DAILY_RUN_MINUTE:02d}")

    # Run immediately at startup
    logger.info("Running first analysis at startup...")
    await run_daily_analysis()

    while True:
        now = datetime.now()
        target = now.replace(hour=DAILY_RUN_HOUR, minute=DAILY_RUN_MINUTE, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        logger.info(f"Next daily analysis at {target.strftime('%Y-%m-%d %H:%M')} "
                    f"(in {sleep_secs/3600:.1f}h)")
        await asyncio.sleep(sleep_secs)
        await run_daily_analysis()


@app.on_event("startup")
async def startup_event():
    global agent, db_client

    logger.info("Starting Energy Agent")

    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        logger.error("CLAUDE_API_KEY not set!")
        raise RuntimeError("CLAUDE_API_KEY environment variable required")

    agent = EnergyAgent(api_key=api_key)
    db_client = DBClient()

    if not db_client.is_connected():
        logger.warning("DB not reachable at startup — will retry")

    if not os.path.exists(comparison_log_file):
        with open(comparison_log_file, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'data_age_s', 'soc_pct', 'pv_kw', 'load_kw', 'grid_kw',
                'battery_direction', 'battery_power_kw', 'heat_pump_kw', 'tariff',
                'optimizer_action', 'agent_action', 'agent_reason', 'agent_confidence',
                'agrees_with_optimizer', 'similarity_pct', 'mode'
            ])

    # Start background advice loop
    asyncio.create_task(advice_loop())

    logger.info(f"Energy Agent started — daily at {DAILY_RUN_HOUR:02d}:{DAILY_RUN_MINUTE:02d} + immediately at startup")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Energy Agent shutdown")


@app.get("/health")
async def health_check() -> HealthStatus:
    return HealthStatus(
        agent_running=agent is not None,
        db_connected=db_client.is_connected() if db_client else False,
        claude_available=agent is not None,
        uptime_seconds=time.time() - start_time,
        total_advices=advice_count,
        shadow_mode=True,
        last_advice_time=last_advice_time
    )


@app.get("/status")
async def get_current_status() -> SystemStatus:
    """Get latest energy system status from DB"""
    if not db_client:
        raise HTTPException(status_code=503, detail="DB client not initialized")
    row = db_client.get_latest()
    if not row:
        raise HTTPException(status_code=503, detail="No data in database")
    return build_status_from_db(row)


@app.get("/advice")
async def get_last_advice() -> dict:
    """Return the last scheduled advice (no API call)"""
    if not last_advice:
        raise HTTPException(status_code=503, detail="No advice yet — check back after the first interval")
    return last_advice


@app.post("/advice/now")
async def get_advice_now() -> dict:
    """Force an immediate Claude API call (costs credits)"""
    global advice_count, last_advice, last_advice_time

    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    row = db_client.get_latest()
    if not row:
        raise HTTPException(status_code=503, detail="No data in database")

    status = build_status_from_db(row)
    schedule = db_client.get_schedule(slots=96)
    status = apply_current_price(status, schedule)
    history = _load_recent_history(days=7)
    advice = agent.get_advice(status, SAFETY_CONSTRAINTS, schedule, history=history)
    advice_count += 1
    last_advice_time = datetime.now()
    last_advice = {
        "timestamp": last_advice_time.isoformat(),
        "status": status.model_dump(),
        "advice": advice.model_dump(),
        "mode": "shadow"
    }
    logger.info(f"[on-demand] Advice: {advice.action} | {advice.reason}")
    return last_advice


@app.get("/logs/comparison.csv")
async def download_comparison_log():
    if not os.path.exists(comparison_log_file):
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(
        path=comparison_log_file,
        filename=f"comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        media_type="text/csv"
    )


@app.get("/metrics")
async def get_metrics():
    return {
        "uptime_seconds": time.time() - start_time,
        "total_advices": advice_count,
        "daily_run_time": f"{DAILY_RUN_HOUR:02d}:{DAILY_RUN_MINUTE:02d}",
        "last_advice_time": last_advice_time.isoformat() if last_advice_time else None,
        "mode": "shadow",
        "db_connected": db_client.is_connected() if db_client else False
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5052)
