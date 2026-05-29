from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class SystemStatus(BaseModel):
    """Current energy system state"""
    timestamp: datetime
    data_age_seconds: Optional[float] = None   # How old is the DB row
    # Battery (Seplos)
    soc_pct: float
    battery_direction: Optional[str] = None    # charge / discharge / idle
    battery_power_kw: Optional[float] = None
    # Solar (Growatt SPH)
    pv_kw: float
    pv_today_kwh: Optional[float] = None
    # Grid (P1 meter)
    grid_kw: float                             # positive = import, negative = export
    # Load
    load_kw: float
    # Tariff
    tariff_eur_kwh: float
    # Inverter
    inverter_state: str
    # Heat pump (Sparrow)
    heat_pump_status: Optional[str] = None
    heat_pump_power_kw: Optional[float] = None
    outside_temp_c: Optional[float] = None


class SafetyConstraints(BaseModel):
    """Safety limits for battery and inverter"""
    min_soc_pct: float = 20.0
    max_soc_pct: float = 90.0
    max_charge_rate_kw: float = 3.0
    max_discharge_rate_kw: float = 3.0
    grid_import_max_kw: Optional[float] = None


class AgentAdvice(BaseModel):
    """Advice from Claude agent"""
    action: str          # LOAD_FIRST|BATTERY_FIRST+CHARGE|BATTERY_FIRST+DISCHARGE|STANDBY
    target_power_kw: Optional[float] = None
    target_soc_pct: Optional[float] = None
    duration_minutes: Optional[int] = None
    priority: str = "normal"
    reason: str
    confidence_pct: float = 75.0
    expected_benefit: Optional[str] = None
    agrees_with_optimizer: Optional[bool] = None


class ComparisonLog(BaseModel):
    """Log entry comparing agent advice vs current system"""
    timestamp: datetime
    system_status: SystemStatus
    agent_advice: AgentAdvice
    current_action: str
    current_power_kw: Optional[float] = None
    similarity_pct: float
    notes: Optional[str] = None


class HealthStatus(BaseModel):
    """Health and status of agent system"""
    agent_running: bool
    db_connected: bool
    claude_available: bool
    last_advice_time: Optional[datetime] = None
    total_advices: int = 0
    shadow_mode: bool = True
    uptime_seconds: float
