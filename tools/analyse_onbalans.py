
#!/usr/bin/env python3

import sys
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


# ============================================================
# INSTELLINGEN
# ============================================================

BATTERY_CAPACITY_KWH = 16.0

MIN_SOC = 0.20
MAX_SOC = 0.90
INITIAL_SOC = 0.50

MAX_POWER_KW = 3.0

# Totale AC -> batterij -> AC efficiency
ROUND_TRIP_EFFICIENCY = 0.83

# Verdeel round-trip efficiency over laden/ontladen
CHARGE_EFFICIENCY = np.sqrt(ROUND_TRIP_EFFICIENCY)
DISCHARGE_EFFICIENCY = np.sqrt(ROUND_TRIP_EFFICIENCY)

INTERVAL_HOURS = 0.25

# Economische kosten van batterijdegradatie
# €1200 / (6000 cycli * 10 kWh)
DEGRADATION_COST_PER_KWH = 0.01786

# None = geen dagelijkse cycluslimiet
MAX_CYCLES_PER_DAY = None


# ============================================================
# CSV INLEZEN
# ============================================================

def load_data(filename):

    print()
    print("=" * 70)
    print("CSV INLEZEN")
    print("=" * 70)

    print(f"Bestand: {filename}")

    df = pd.read_csv(
        filename,
        sep=None,
        engine="python"
    )

    df.columns = [
        c.strip().replace("\ufeff", "")
        for c in df.columns
    ]

    print(f"Rijen gevonden: {len(df)}")

    print("\nKolommen:")

    for c in df.columns:
        print(f"   {c!r}")

    time_col = "Timeinterval Start Loc"

    if time_col not in df.columns:
        raise RuntimeError(
            f"Kolom ontbreekt: {time_col}"
        )

    df["time"] = pd.to_datetime(
        df[time_col],
        errors="coerce"
    )

    numeric_columns = [
        "Price Shortage",
        "Price Surplus",
        "Price Dispatch Up",
        "Price Dispatch Down",
        "Regulation State"
    ]

    for col in numeric_columns:

        if col in df.columns:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

    df = (
        df
        .sort_values("time")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# BATTERIJ
# ============================================================

class Battery:

    def __init__(self):

        self.min_energy = (
            BATTERY_CAPACITY_KWH * MIN_SOC
        )

        self.max_energy = (
            BATTERY_CAPACITY_KWH * MAX_SOC
        )

        self.energy = (
            BATTERY_CAPACITY_KWH * INITIAL_SOC
        )

        self.total_charge = 0.0
        self.total_discharge = 0.0

        self.gross_revenue = 0.0
        self.degradation_cost = 0.0

        self.day_throughput = 0.0
        self.current_day = None

    @property
    def soc(self):

        return (
            self.energy /
            BATTERY_CAPACITY_KWH
        )

    @property
    def net_revenue(self):

        return (
            self.gross_revenue -
            self.degradation_cost
        )

    @property
    def full_cycles(self):

        usable = (
            BATTERY_CAPACITY_KWH *
            (MAX_SOC - MIN_SOC)
        )

        if usable <= 0:
            return 0

        return (
            self.total_discharge /
            usable
        )

    def reset_day(self, day):

        if self.current_day != day:

            self.current_day = day
            self.day_throughput = 0.0

    def allowed_to_cycle(self, day):

        self.reset_day(day)

        if MAX_CYCLES_PER_DAY is None:
            return True

        usable = (
            BATTERY_CAPACITY_KWH *
            (MAX_SOC - MIN_SOC)
        )

        max_throughput = (
            MAX_CYCLES_PER_DAY *
            usable
        )

        return (
            self.day_throughput
            < max_throughput
        )

    # --------------------------------------------------------
    # LADEN
    # --------------------------------------------------------

    def charge(
        self,
        power_kw,
        price_eur_kwh,
        day
    ):

        if not self.allowed_to_cycle(day):
            return 0.0

        power_kw = min(
            power_kw,
            MAX_POWER_KW
        )

        grid_energy = (
            power_kw *
            INTERVAL_HOURS
        )

        available_capacity = (
            self.max_energy -
            self.energy
        )

        max_grid_energy = (
            available_capacity /
            CHARGE_EFFICIENCY
        )

        grid_energy = min(
            grid_energy,
            max_grid_energy
        )

        if grid_energy <= 0:
            return 0.0

        stored_energy = (
            grid_energy *
            CHARGE_EFFICIENCY
        )

        self.energy += stored_energy

        self.total_charge += grid_energy

        self.day_throughput += grid_energy

        # Kosten energie
        self.gross_revenue -= (
            grid_energy *
            price_eur_kwh
        )

        # Degradatie
        self.degradation_cost += (
            grid_energy *
            DEGRADATION_COST_PER_KWH
        )

        return grid_energy

    # --------------------------------------------------------
    # ONTLADEN
    # --------------------------------------------------------

    def discharge(
        self,
        power_kw,
        price_eur_kwh,
        day
    ):

        if not self.allowed_to_cycle(day):
            return 0.0

        power_kw = min(
            power_kw,
            MAX_POWER_KW
        )

        grid_energy = (
            power_kw *
            INTERVAL_HOURS
        )

        available_battery_energy = (
            self.energy -
            self.min_energy
        )

        required_battery_energy = (
            grid_energy /
            DISCHARGE_EFFICIENCY
        )

        battery_energy = min(
            required_battery_energy,
            available_battery_energy
        )

        if battery_energy <= 0:
            return 0.0

        grid_energy = (
            battery_energy *
            DISCHARGE_EFFICIENCY
        )

        self.energy -= battery_energy

        self.total_discharge += grid_energy

        self.day_throughput += grid_energy

        # Opbrengst
        self.gross_revenue += (
            grid_energy *
            price_eur_kwh
        )

        # Degradatie
        self.degradation_cost += (
            grid_energy *
            DEGRADATION_COST_PER_KWH
        )

        return grid_energy


# ============================================================
# RESULTAAT PRINTEN
# ============================================================

def print_result(
    title,
    battery,
    result
):

    print()
    print("=" * 70)
    print(title)
    print("=" * 70)

    print(
        f"Bruto opbrengst:       "
        f"€ {battery.gross_revenue:,.2f}"
    )

    print(
        f"Degradatiekosten:      "
        f"€ {battery.degradation_cost:,.2f}"
    )

    print(
        f"NETTO opbrengst:       "
        f"€ {battery.net_revenue:,.2f}"
    )

    print(
        f"Geladen:               "
        f"{battery.total_charge:,.2f} kWh"
    )

    print(
        f"Ontladen:              "
        f"{battery.total_discharge:,.2f} kWh"
    )

    print(
        f"Equivalent full cycles:"
        f" {battery.full_cycles:,.1f}"
    )

    print(
        f"Gemiddeld per dag:     "
        f"{battery.full_cycles / 365:.2f}"
        f" cycli"
    )

    print(
        f"Eind SOC:              "
        f"{battery.soc * 100:.1f}%"
    )

    print()

    print("Actieverdeling:")

    counts = (
        result["action"]
        .value_counts()
    )

    for action, count in counts.items():

        print(
            f"  {action:12s}"
            f"{count:7d} kwartieren"
            f" = {count * 0.25:8.1f} uur"
        )


# ============================================================
# SCENARIO 1
#
# OFFICIËLE PASSIEVE RICHTING
#
# UP   -> DISCHARGE
# DOWN -> CHARGE
# UP_AND_DOWN -> IDLE
# STABLE -> IDLE
#
# ============================================================

def passive_strategy(df):

    battery = Battery()

    records = []

    for _, row in df.iterrows():

        condition = str(
            row["Regulating Condition"]
        ).strip().upper()

        shortage = row[
            "Price Shortage"
        ]

        surplus = row[
            "Price Surplus"
        ]

        day = row["time"].date()

        action = "IDLE"

        price = np.nan

        energy = 0.0

        # ----------------------------------------------------
        # UP
        # ----------------------------------------------------

        if (
            condition == "UP"
            and pd.notna(shortage)
        ):

            price = shortage / 1000.0

            energy = battery.discharge(
                MAX_POWER_KW,
                price,
                day
            )

            action = "DISCHARGE"

        # ----------------------------------------------------
        # DOWN
        # ----------------------------------------------------

        elif (
            condition == "DOWN"
            and pd.notna(surplus)
        ):

            price = surplus / 1000.0

            energy = battery.charge(
                MAX_POWER_KW,
                price,
                day
            )

            action = "CHARGE"

        # ----------------------------------------------------
        # UP_AND_DOWN
        #
        # GEEN PASSIEVE ACTIE
        # ----------------------------------------------------

        elif condition == "UP_AND_DOWN":

            action = "IDLE"

        # ----------------------------------------------------
        # STABLE
        # ----------------------------------------------------

        else:

            action = "IDLE"

        records.append({
            "time": row["time"],
            "regulating_condition": condition,
            "price": price,
            "action": action,
            "energy_kwh": energy,
            "soc": battery.soc,
            "gross_revenue":
                battery.gross_revenue,
            "degradation":
                battery.degradation_cost,
            "net_revenue":
                battery.net_revenue
        })

    result = pd.DataFrame(records)

    print_result(
        "SCENARIO 1 - PASSIEVE ONBALANS",
        battery,
        result
    )

    result.to_csv(
        "resultaat_passief_v3.csv",
        index=False
    )

    return result


# ============================================================
# SCENARIO 2
#
# PERFECT FORESIGHT
#
# BELANGRIJK:
# Ook hier wordt UP_AND_DOWN NIET als gewone
# laad/verkoopprijs gebruikt.
#
# We gebruiken alleen:
#
# UP   -> shortage
# DOWN -> surplus
#
# en zoeken achteraf de optimale batterijplanning.
#
# ============================================================

def perfect_foresight(df):

    print()
    print("Bereken perfecte foresight...")

    n = len(df)

    # --------------------------------------------------------
    # Prijzen
    # --------------------------------------------------------

    charge_price = (
        df["Price Surplus"]
        .fillna(0)
        .values
        / 1000.0
    )

    discharge_price = (
        df["Price Shortage"]
        .fillna(0)
        .values
        / 1000.0
    )

    condition = (
        df["Regulating Condition"]
        .astype(str)
        .str.strip()
        .str.upper()
        .values
    )

    # --------------------------------------------------------
    # SOC grid
    # --------------------------------------------------------

    STATES = 201

    soc_grid = np.linspace(
        MIN_SOC,
        MAX_SOC,
        STATES
    )

    energy_grid = (
        soc_grid *
        BATTERY_CAPACITY_KWH
    )

    # --------------------------------------------------------
    # Dynamische programmering
    # --------------------------------------------------------

    value = np.zeros(
        STATES
    )

    policies = []

    for i in range(n - 1, -1, -1):

        new_value = np.full(
            STATES,
            -np.inf
        )

        new_policy = []

        for s, energy in enumerate(
            energy_grid
        ):

            # ----------------------------------------------
            # IDLE
            # ----------------------------------------------

            best_value = value[s]

            best_action = "IDLE"

            # ----------------------------------------------
            # CHARGE
            #
            # Alleen wanneer TenneT DOWN is
            # ----------------------------------------------

            if (
                condition[i] == "DOWN"
                and pd.notna(
                    df.iloc[i]["Price Surplus"]
                )
            ):

                grid_energy = (
                    MAX_POWER_KW *
                    INTERVAL_HOURS
                )

                stored_energy = (
                    grid_energy *
                    CHARGE_EFFICIENCY
                )

                new_energy = (
                    energy +
                    stored_energy
                )

                if new_energy <= (
                    BATTERY_CAPACITY_KWH *
                    MAX_SOC
                ):

                    j = np.argmin(
                        abs(
                            energy_grid -
                            new_energy
                        )
                    )

                    candidate = (
                        value[j]
                        - grid_energy *
                        charge_price[i]
                        - grid_energy *
                        DEGRADATION_COST_PER_KWH
                    )

                    if candidate > best_value:

                        best_value = candidate
                        best_action = "CHARGE"

            # ----------------------------------------------
            # DISCHARGE
            #
            # Alleen wanneer TenneT UP is
            # ----------------------------------------------

            if (
                condition[i] == "UP"
                and pd.notna(
                    df.iloc[i]["Price Shortage"]
                )
            ):

                grid_energy = (
                    MAX_POWER_KW *
                    INTERVAL_HOURS
                )

                battery_energy = (
                    grid_energy /
                    DISCHARGE_EFFICIENCY
                )

                new_energy = (
                    energy -
                    battery_energy
                )

                if new_energy >= (
                    BATTERY_CAPACITY_KWH *
                    MIN_SOC
                ):

                    j = np.argmin(
                        abs(
                            energy_grid -
                            new_energy
                        )
                    )

                    candidate = (
                        value[j]
                        + grid_energy *
                        discharge_price[i]
                        - grid_energy *
                        DEGRADATION_COST_PER_KWH
                    )

                    if candidate > best_value:

                        best_value = candidate
                        best_action = "DISCHARGE"

            new_value[s] = best_value

            new_policy.append(
                best_action
            )

        value = new_value

        policies.append(
            new_policy
        )

    # --------------------------------------------------------
    # Simulatie vooruit
    # --------------------------------------------------------

    battery = Battery()

    state = np.argmin(
        abs(
            soc_grid -
            INITIAL_SOC
        )
    )

    records = []

    for i in range(n):

        action = policies[
            n - 1 - i
        ][state]

        day = df.iloc[i]["time"].date()

        price = np.nan

        energy = 0.0

        # ----------------------------------------------------
        # CHARGE
        # ----------------------------------------------------

        if action == "CHARGE":

            price = charge_price[i]

            energy = battery.charge(
                MAX_POWER_KW,
                price,
                day
            )

        # ----------------------------------------------------
        # DISCHARGE
        # ----------------------------------------------------

        elif action == "DISCHARGE":

            price = discharge_price[i]

            energy = battery.discharge(
                MAX_POWER_KW,
                price,
                day
            )

        records.append({
            "time": df.iloc[i]["time"],
            "regulating_condition":
                condition[i],
            "price": price,
            "action": action,
            "energy_kwh": energy,
            "soc": battery.soc,
            "gross_revenue":
                battery.gross_revenue,
            "degradation":
                battery.degradation_cost,
            "net_revenue":
                battery.net_revenue
        })

        state = np.argmin(
            abs(
                soc_grid -
                battery.soc
            )
        )

    result = pd.DataFrame(records)

    print_result(
        "SCENARIO 2 - PERFECT FORESIGHT",
        battery,
        result
    )

    result.to_csv(
        "resultaat_perfect_v3.csv",
        index=False
    )

    return result


# ============================================================
# SOC GRAFIEK
# ============================================================

def plot_soc(
    result,
    title,
    filename
):

    plt.figure(
        figsize=(14, 6)
    )

    plt.plot(
        result["time"],
        result["soc"] * 100
    )

    plt.xlabel(
        "Datum"
    )

    plt.ylabel(
        "SOC (%)"
    )

    plt.title(title)

    plt.ylim(
        MIN_SOC * 100 - 2,
        MAX_SOC * 100 + 2
    )

    plt.grid(True)

    plt.tight_layout()

    plt.savefig(
        filename,
        dpi=150
    )

    plt.close()


# ============================================================
# PRIJS DUURCURVE
# ============================================================

def price_duration_curve(df):

    shortage = (
        df.loc[
            df["Regulating Condition"]
            == "UP",
            "Price Shortage"
        ]
        .dropna()
        .abs()
    )

    surplus = (
        df.loc[
            df["Regulating Condition"]
            == "DOWN",
            "Price Surplus"
        ]
        .dropna()
        .abs()
    )

    prices = pd.concat([
        shortage,
        surplus
    ])

    prices = (
        prices
        .sort_values(
            ascending=False
        )
        .values
    )

    hours = (
        np.arange(
            1,
            len(prices) + 1
        ) * 0.25
    )

    plt.figure(
        figsize=(12, 7)
    )

    plt.plot(
        hours,
        prices / 1000.0
    )

    plt.xlabel(
        "Aantal uren"
    )

    plt.ylabel(
        "Prijs (€/kWh)"
    )

    plt.title(
        "TenneT onbalansprijsduurcurve 2025"
    )

    plt.grid(True)

    plt.tight_layout()

    plt.savefig(
        "prijsduurcurve_v3.png",
        dpi=150
    )

    plt.close()


# ============================================================
# SAMENVATTING
# ============================================================

def summary(
    passive,
    perfect
):

    passive_final = (
        passive.iloc[-1]["net_revenue"]
    )

    perfect_final = (
        perfect.iloc[-1]["net_revenue"]
    )

    print()
    print("=" * 70)
    print("SAMENVATTING")
    print("=" * 70)

    print(
        f"{'Strategie':30s}"
        f"{'Netto opbrengst':>18s}"
    )

    print("-" * 50)

    print(
        f"{'Passieve onbalans':30s}"
        f"€ {passive_final:>14,.2f}"
    )

    print(
        f"{'Perfect foresight':30s}"
        f"€ {perfect_final:>14,.2f}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if len(sys.argv) != 2:

        print()
        print(
            "Gebruik:"
        )

        print(
            "python3 analyse_onbalans_v3.py "
            "settlement_prices.csv"
        )

        sys.exit(1)

    filename = sys.argv[1]

    if not os.path.exists(filename):

        print(
            f"Bestand bestaat niet: {filename}"
        )

        sys.exit(1)

    df = load_data(filename)

    required = [
        "Price Shortage",
        "Price Surplus",
        "Regulating Condition"
    ]

    missing = [
        col
        for col in required
        if col not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Ontbrekende kolommen: "
            + ", ".join(missing)
        )

    # --------------------------------------------------------
    # BASISSTATISTIEKEN
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("BASISSTATISTIEKEN")
    print("=" * 70)

    print(
        f"Periode: "
        f"{df['time'].min()}"
        f" -> "
        f"{df['time'].max()}"
    )

    print(
        f"Kwartieren: "
        f"{len(df):,}"
    )

    print(
        f"Hoogste shortage: "
        f"€{df['Price Shortage'].max()/1000:.3f}/kWh"
    )

    print(
        f"Laagste shortage: "
        f"€{df['Price Shortage'].min()/1000:.3f}/kWh"
    )

    print(
        f"Hoogste surplus: "
        f"€{df['Price Surplus'].max()/1000:.3f}/kWh"
    )

    print(
        f"Laagste surplus: "
        f"€{df['Price Surplus'].min()/1000:.3f}/kWh"
    )

    print()
    print("Regulation conditions:")

    counts = (
        df["Regulating Condition"]
        .value_counts()
    )

    for condition, count in counts.items():

        percentage = (
            count /
            len(df) *
            100
        )

        print(
            f"  {condition:15s}"
            f"{count:7d}"
            f" ({percentage:5.1f}%)"
        )

    # --------------------------------------------------------
    # GRAFIEK
    # --------------------------------------------------------

    price_duration_curve(df)

    # --------------------------------------------------------
    # SCENARIO'S
    # --------------------------------------------------------

    passive = passive_strategy(df)

    perfect = perfect_foresight(df)

    # --------------------------------------------------------
    # SOC GRAFIEKEN
    # --------------------------------------------------------

    plot_soc(
        passive,
        "SOC - passieve onbalans",
        "soc_passief_v3.png"
    )

    plot_soc(
        perfect,
        "SOC - perfecte foresight",
        "soc_perfect_v3.png"
    )

    # --------------------------------------------------------
    # SAMENVATTING
    # --------------------------------------------------------

    summary(
        passive,
        perfect
    )

    # --------------------------------------------------------
    # BESTANDEN
    # --------------------------------------------------------

    print()
    print("=" * 70)
    print("GEMAAKTE BESTANDEN")
    print("=" * 70)

    print(
        "  prijsduurcurve_v3.png"
    )

    print(
        "  soc_passief_v3.png"
    )

    print(
        "  soc_perfect_v3.png"
    )

    print(
        "  resultaat_passief_v3.csv"
    )

    print(
        "  resultaat_perfect_v3.csv"
    )

    print()


if __name__ == "__main__":
    main()