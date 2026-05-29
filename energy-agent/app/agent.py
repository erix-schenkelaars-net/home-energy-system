import json
import logging
from datetime import datetime
from typing import Optional
from anthropic import Anthropic
from app.models import SystemStatus, AgentAdvice, SafetyConstraints

logger = logging.getLogger(__name__)


class EnergyAgent:
    """Claude-based energy optimization agent"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.conversation_history = []

        self.system_prompt = """Je bent een energieoptimalisatie-adviseur voor een Nederlands huishouden met:
- Growatt SPH5000 hybride omvormer
- Seplos 16 kWh LiFePO4 batterij
- 6.24 kWp zonnepanelen (12 oost + 12 west, 35°, azimuth 88°/272°)
- Warmtepomp (Sparrow)
- Dynamische energieprijzen via EnergyZero (EPEX spot + €0.11085 energiebelasting + €0.01 inkoopvergoeding)

De bestaande battery_optimizer (LP-model) draait al en maakt een dagplan van 96 kwartierslots (24 uur × 4).
Jij doet IN SCHADUWMODUS één dagelijkse beoordeling van dat plan — jij bestuurt NIETS, alleen analyseren en vergelijken.

## Contractmodus (HARD CONSTRAINT — niet onderhandelbaar):
- **Tot en met 2026-06-16: MINIMIZE_EXPORT modus**
  - BATTERY_FIRST+DISCHARGE (export naar net) is VERBODEN, ongeacht de prijs.
  - Batterij mag alleen ontladen om eigen last te dekken, nooit voor netexport.
- **Vanaf 2026-06-17: Powerpeers "Helemaal Dynamisch" contract**
  - Inkoop: EPEX spot + €0.01/kWh vergoeding + energiebelasting
  - Verkoop: EPEX spot + €0.01/kWh vergoeding (saldering nog tot eind 2026)
  - Volledige prijsarbitrage inclusief export naar net is toegestaan.
  - BATTERY_FIRST+DISCHARGE mag worden aanbevolen bij hoge prijzen + voldoende SoC.

## Growatt inverter-modi (exact deze termen gebruiken):
- **LOAD_FIRST**: Passief. PV → last → batterij/net. Batterij ontlaadt automatisch als PV tekortschiet.
- **BATTERY_FIRST+CHARGE**: Actief laden vanuit net + PV. Gebruik bij lage prijzen (≤ €0.23/kWh spot).
- **BATTERY_FIRST+DISCHARGE**: Actief ontladen/exporteren. Alleen in DYNAMIC_PRICE modus (v.a. 2026-06-17). Min SoC: 20%.
- **STANDBY**: Batterij volledig passief.

## Prijsdrempels (spot = EPEX excl. energiebelasting):
- Laden: spot ≤ €0.23/kWh → BATTERY_FIRST+CHARGE
- Neutraal: spot €0.23–€0.28 → LOAD_FIRST
- Ontladen: spot ≥ €0.28/kWh → LOAD_FIRST (batterij dekt last automatisch)
- Exporteren: spot ≥ €0.30/kWh + SoC > 20% → BATTERY_FIRST+DISCHARGE (alleen v.a. 2026-06-17)

## Batterijbegrenzing:
- Min SoC: 20% (nooit lager), Max SoC: 90%
- Max laad/ontlaadvermogen: 3 kW
- Rendement round-trip: ~90% — laad alleen als prijsspread ≥ €0.05/kWh

## PV-bewust beoordelen van laadkansen:
Controleer ALTIJD de SoC-trajectorie én de pv_kwh-kolom over de HELE dag vóórdat je concludeert dat "goedkoop laden gemist wordt".
Netopladen is alleen zinvol als ALLE drie voorwaarden gelden:
  1. Spotprijs ≤ €0.23/kWh
  2. SoC op dat moment < ~75% (genoeg ruimte over)
  3. De totale pv_kwh over alle dagslots (typisch 10–12 uur PV-productie) is NIET genoeg om de batterij zelf naar ≥85% te brengen

Praktische shortcut: kijk naar de SoC-kolom in het dagplan. Als de SoC ergens in de loop van de dag naar ≥85% stijgt zonder netopladen (charge_kw=0), dan doet PV het werk al — netopladen is overbodig.

Als PV de batterij sowieso vol laadt richting 90%, dan is LOAD_FIRST het CORRECTE gedrag — netopladen verspilt geld of botst met de 90%-limiet.

## LP-optimizer fallback detectie:
- Echte fallback: ALLE slots LOAD_FIRST, spotprijzen ≤ €0.23, EN de pv_kwh-kolom toont onvoldoende PV om de batterij naar ≥85% te brengen. Benoem dit expliciet.
- GEEN fallback (correct gedrag): ALLE slots LOAD_FIRST terwijl de pv_kwh-kolom laat zien dat de batterij via zon naar ≥85–90% loopt. Dit is de juiste LP-beslissing op zonnige dagen.

## Jouw dagelijkse schaduwmodus-taak:
Beoordeel het volledige 96-slot kwartierplan van de optimizer. Geef aan:
1. Is de algemene strategie logisch gegeven de prijzen en PV-verwachting?
2. Zijn er specifieke kwartieren waar jij anders zou adviseren, en waarom?
3. Wat is jouw aanbeveling voor het HUIDIGE kwartier (de eerste slot in de lijst)?

Je output moet ALTIJD geldige JSON zijn:
{
  "action": "LOAD_FIRST|BATTERY_FIRST+CHARGE|BATTERY_FIRST+DISCHARGE|STANDBY",
  "target_power_kw": null of getal (0-3),
  "target_soc_pct": null of getal,
  "duration_minutes": null of getal,
  "priority": "high|normal|low",
  "reason": "Nederlands, max 3 zinnen — dagbeoordeling + advies huidig kwartier",
  "confidence_pct": 0-100,
  "expected_benefit": "beschrijving of null",
  "agrees_with_optimizer": true of false
}"""

    def get_advice(self, status: SystemStatus, constraints: SafetyConstraints,
                   schedule: Optional[list] = None,
                   history: Optional[str] = None) -> AgentAdvice:
        """Daily evaluation of the full 96-slot quarter-hour schedule."""

        # Volledig dagplan — alle slots compact weergeven
        schedule_text = "Geen schedule beschikbaar."
        if schedule:
            lines = []
            for slot in schedule:
                dt   = slot.get("slot_dt", "?")
                act  = slot.get("action", "?")
                price = slot.get("price_eur_kwh")
                pv    = slot.get("pv_kwh")
                load  = slot.get("load_kwh")
                soc_s = slot.get("soc_start_pct")
                soc_e = slot.get("soc_end_pct")
                if all(v is not None for v in [price, pv, load, soc_s, soc_e]):
                    lines.append(
                        f"  {str(dt)[11:16]} {act:<28} €{price:.3f} "
                        f"PV{pv:.2f} L{load:.2f} SoC{soc_s:.0f}→{soc_e:.0f}%"
                    )
                else:
                    lines.append(f"  {str(dt)[11:16]} {act}")
            schedule_text = "\n".join(lines)

        history_text = f"\n=== VERGELIJKINGSHISTORIE ===\n{history}\n" if history else ""

        user_message = f"""=== DAGELIJKSE SCHADUWMODUS ANALYSE — {status.timestamp.strftime('%Y-%m-%d %H:%M')} ===

HUIDIGE SYSTEEMSTATUS (gemeten):
- Batterij SoC: {status.soc_pct:.1f}% | {status.battery_direction} @ {status.battery_power_kw:.2f} kW
- PV nu: {status.pv_kw:.3f} kW | vandaag totaal: {status.pv_today_kwh or '?'} kWh
- Huislast: {status.load_kw:.3f} kW | Net: {status.grid_kw:+.3f} kW
- Tarief huidig kwartier: €{status.tariff_eur_kwh:.4f}/kWh
- Warmtepomp: {status.heat_pump_status or 'onbekend'} ({status.heat_pump_power_kw:.2f} kW)
- Buitentemperatuur: {status.outside_temp_c or '?'} °C
{history_text}
=== OPTIMIZER DAGPLAN ({len(schedule) if schedule else 0} kwartierslots) ===
Formaat: HH:MM actie  €spot  PV(kWh) Last(kWh) SoC%
{schedule_text}

Beoordeel het volledige dagplan. Geef ALLEEN JSON, geen extra tekst."""

        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=600,
                system=self.system_prompt,
                messages=self.conversation_history
            )

            assistant_message = response.content[0].text
            self.conversation_history.append({"role": "assistant", "content": assistant_message})

            if len(self.conversation_history) > 20:
                self.conversation_history = self.conversation_history[-20:]

            # Strip markdown code fences if present
            clean = assistant_message.strip()
            if clean.startswith("```"):
                clean = clean.split("```", 2)[1]
                if clean.startswith("json"):
                    clean = clean[4:]
                clean = clean.rsplit("```", 1)[0].strip()

            advice_dict = json.loads(clean)
            advice = AgentAdvice(**advice_dict)

            agrees = advice_dict.get("agrees_with_optimizer", None)
            agree_str = "✓ agrees" if agrees else ("✗ disagrees" if agrees is False else "?")
            logger.info(f"Agent: {advice.action} [{agree_str}] — {advice.reason}")
            return advice

        except json.JSONDecodeError:
            logger.error(f"Failed to parse Claude response: {assistant_message}")
            return AgentAdvice(action="LOAD_FIRST", reason="Parse error", confidence_pct=0.0)
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return AgentAdvice(action="LOAD_FIRST", reason="Claude API unavailable", confidence_pct=0.0)

    def reset_conversation(self):
        self.conversation_history = []
        logger.info("Conversation history reset")
