"""In-memory ML models for anomaly diagnostics and short-horizon forecasts."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestRegressor

ANOMALY_FEATURES = [
    "PRB_Usage_pct",
    "Throughput_Mbps",
    "Latency_ms",
    "Call_Drop_Rate_pct",
    "Handover_Failure_Rate_pct",
    "Connected_Users",
]
FORECAST_FEATURES = [
    "Hour",
    "Day_Of_Week",
    "Is_Weekend",
    "Connected_Users",
    "PRB_Lag_1",
    "PRB_Lag_2",
    "PRB_Rolling_Mean_3",
]


class NetworkMLEngine:
    """Fit and serve ephemeral models from persisted PostgreSQL telemetry."""

    def __init__(
        self,
        anomaly_model: IsolationForest,
        prb_model: RandomForestRegressor,
        throughput_model: RandomForestRegressor,
    ) -> None:
        self.anomaly_model = anomaly_model
        self.prb_model = prb_model
        self.throughput_model = throughput_model

    @classmethod
    def train(cls, dataframe: pd.DataFrame) -> "NetworkMLEngine":
        """Train both anomaly and demand models from KPI history in memory."""
        cls._validate_dataframe(dataframe)
        ordered = dataframe.sort_values(["Tower_ID", "Timestamp"]).copy()
        anomaly_model = IsolationForest(
            n_estimators=160, contamination=0.025, random_state=42, n_jobs=-1
        )
        anomaly_model.fit(ordered[ANOMALY_FEATURES].astype(float))

        features = cls._forecast_features(ordered)
        valid = features.notna().all(axis=1)
        if int(valid.sum()) < 20:
            raise ValueError("At least 20 usable KPI observations are required to train.")
        prb_model = RandomForestRegressor(
            n_estimators=120, min_samples_leaf=2, random_state=42, n_jobs=-1
        )
        throughput_model = RandomForestRegressor(
            n_estimators=120, min_samples_leaf=2, random_state=42, n_jobs=-1
        )
        prb_model.fit(features.loc[valid], ordered.loc[valid, "PRB_Usage_pct"])
        throughput_model.fit(
            features.loc[valid], ordered.loc[valid, "Throughput_Mbps"]
        )
        return cls(anomaly_model, prb_model, throughput_model)

    def detect_anomalies(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Score KPI readings and return operational threshold diagnostics."""
        self._validate_dataframe(dataframe)
        result = dataframe.copy()
        matrix = result[ANOMALY_FEATURES].astype(float)
        decision = self.anomaly_model.decision_function(matrix)
        isolation_flag = self.anomaly_model.predict(matrix) == -1
        hard_flags = (
            (result["Call_Drop_Rate_pct"] > 3.0)
            | (result["Latency_ms"] > 120.0)
            | (result["PRB_Usage_pct"] >= 90.0)
            | (result["Handover_Failure_Rate_pct"] > 5.0)
            | result["Hardware_Status"].isin(["Degraded", "Outage"])
        )
        normalized_isolation = np.clip((0.15 - decision) / 0.5, 0, 1)
        result["Anomaly_Score"] = np.round(
            np.maximum(normalized_isolation, hard_flags.astype(float) * 0.72) * 100,
            1,
        )
        result["Is_Anomaly"] = isolation_flag | hard_flags.to_numpy()
        result["Root_Cause"] = result.apply(self._diagnose_row, axis=1)
        return result

    def forecast(self, dataframe: pd.DataFrame, hours: int = 24) -> pd.DataFrame:
        """Forecast network-wide hourly PRB and throughput for the next horizon."""
        self._validate_dataframe(dataframe)
        if hours < 1 or hours > 168:
            raise ValueError("hours must be between 1 and 168.")
        ordered = dataframe.sort_values(["Tower_ID", "Timestamp"])
        future_times = pd.date_range(
            start=pd.Timestamp(ordered["Timestamp"].max()) + pd.Timedelta(hours=1),
            periods=hours,
            freq="h",
        )
        tower_states: list[dict[str, object]] = []
        for tower_id, tower_history in ordered.groupby("Tower_ID", sort=False):
            last_rows = tower_history.tail(2)
            latest = last_rows.iloc[-1]
            latest_prb = float(latest["PRB_Usage_pct"])
            tower_states.append(
                {
                    "tower_id": tower_id,
                    "lag_1": latest_prb,
                    "lag_2": (
                        float(last_rows.iloc[-2]["PRB_Usage_pct"])
                        if len(last_rows) > 1
                        else latest_prb
                    ),
                    "rolling_prb": float(tower_history.tail(3)["PRB_Usage_pct"].mean()),
                    "users_by_hour": tower_history.groupby(
                        tower_history["Timestamp"].dt.hour
                    )["Connected_Users"].mean(),
                    "mean_users": float(tower_history["Connected_Users"].mean()),
                }
            )

        hourly_results: list[pd.DataFrame] = []
        for target_time in future_times:
            users = np.asarray(
                [
                    state["users_by_hour"].get(target_time.hour, state["mean_users"])
                    for state in tower_states
                ],
                dtype=float,
            )
            rolling = np.asarray(
                [state["rolling_prb"] for state in tower_states], dtype=float
            )
            features = pd.DataFrame(
                {
                    "Hour": target_time.hour,
                    "Day_Of_Week": target_time.dayofweek,
                    "Is_Weekend": int(target_time.dayofweek >= 5),
                    "Connected_Users": users,
                    "PRB_Lag_1": [state["lag_1"] for state in tower_states],
                    "PRB_Lag_2": [state["lag_2"] for state in tower_states],
                    "PRB_Rolling_Mean_3": rolling,
                },
                columns=FORECAST_FEATURES,
            )
            prb = np.clip(self.prb_model.predict(features), 0, 100)
            throughput = np.maximum(0, self.throughput_model.predict(features))
            hourly_results.append(
                pd.DataFrame(
                    {
                        "Timestamp": target_time,
                        "Tower_ID": [state["tower_id"] for state in tower_states],
                        "Predicted_PRB_Usage_pct": prb,
                        "Predicted_Throughput_Mbps": throughput,
                        "Connected_Users": users,
                    }
                )
            )
            for index, state in enumerate(tower_states):
                state["lag_2"] = state["lag_1"]
                state["lag_1"] = float(prb[index])
                state["rolling_prb"] = float((rolling[index] * 2 + prb[index]) / 3)

        return (
            pd.concat(hourly_results, ignore_index=True)
            .groupby("Timestamp", as_index=False)
            .agg(
                Predicted_PRB_Usage_pct=("Predicted_PRB_Usage_pct", "mean"),
                Predicted_Throughput_Mbps=("Predicted_Throughput_Mbps", "sum"),
                Predicted_Connected_Users=("Connected_Users", "sum"),
            )
            .sort_values("Timestamp", ignore_index=True)
        )

    @staticmethod
    def _validate_dataframe(dataframe: pd.DataFrame) -> None:
        required = {
            "Timestamp",
            "Tower_ID",
            *ANOMALY_FEATURES,
            "Hardware_Status",
        }
        missing = required.difference(dataframe.columns)
        if missing:
            raise ValueError(f"KPI data is missing columns: {sorted(missing)}")
        if dataframe.empty:
            raise ValueError("KPI data must contain at least one row.")

    @staticmethod
    def _forecast_features(dataframe: pd.DataFrame) -> pd.DataFrame:
        ordered = dataframe.sort_values(["Tower_ID", "Timestamp"])
        groups = ordered.groupby("Tower_ID", sort=False)
        return pd.DataFrame(
            {
                "Hour": ordered["Timestamp"].dt.hour + ordered["Timestamp"].dt.minute / 60,
                "Day_Of_Week": ordered["Timestamp"].dt.dayofweek,
                "Is_Weekend": (ordered["Timestamp"].dt.dayofweek >= 5).astype(int),
                "Connected_Users": ordered["Connected_Users"].astype(float),
                "PRB_Lag_1": groups["PRB_Usage_pct"].shift(1),
                "PRB_Lag_2": groups["PRB_Usage_pct"].shift(2),
                "PRB_Rolling_Mean_3": groups["PRB_Usage_pct"].transform(
                    lambda values: values.shift(1).rolling(3, min_periods=3).mean()
                ),
            },
            index=ordered.index,
        )[FORECAST_FEATURES]

    @staticmethod
    def _diagnose_row(row: pd.Series) -> str:
        causes: list[str] = []
        if row["Hardware_Status"] == "Outage":
            causes.append("Tower outage / service interruption")
        elif row["Hardware_Status"] == "Degraded":
            causes.append("Hardware degradation")
        if row["PRB_Usage_pct"] >= 85:
            causes.append("Radio resource congestion")
        if row["Latency_ms"] > 120:
            causes.append("High backhaul or radio latency")
        if row["Call_Drop_Rate_pct"] > 3:
            causes.append("Elevated call drops / coverage quality")
        if row["Handover_Failure_Rate_pct"] > 5:
            causes.append("Handover failure / mobility issue")
        return "; ".join(causes) if causes else "Multivariate KPI deviation"

