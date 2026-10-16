"""Бэктест правила Тейлора КПМ ДДКП на истории 2016–2026.

Две версии правила (ρ = 0,75; φπ = 1,5; φy = 0,5; нейтральная ставка — оценки ЦБ):
  * «реальное время» — только информация на момент решения: текущая инфляция
    (0,75·БИПЦ + 0,25·ИПЦ г/г) и односторонний HP-разрыв выпуска;
  * «ex post» — спецификация КПМ: инфляция через 3 квартала (фактическая) и двусторонний HP-разрыв.

Для каждой версии: прогноз на 1 шаг (с фактическим лагом ставки), динамическая симуляция
с 2016Q1 и ставка без сглаживания. Бенчмарк — «ставка не меняется».

Запуск: python src/backtest.py (после download_data.py). Результат: outputs/backtest*.png, outputs/backtest.xlsx
"""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from taylor_rule import OUT, RULES, apply_rule, build_dataset

START = pd.Period("2016Q1", "Q")
END = pd.Period("2026Q3", "Q")
SUBPERIODS = [("2016Q1", "2019Q4", "2016–2019: дезинфляция"),
              ("2020Q1", "2021Q4", "2020–2021: пандемия"),
              ("2022Q1", "2022Q4", "2022: шок"),
              ("2023Q1", "2026Q3", "2023–2026: ужесточение и смягчение")]
NO_CHANGE = 0.25  # |Δ| меньше этого (п.п.) — «без изменений»

VERSIONS = {
    "Реальное время": dict(expect="cur", measure="pi_mix", gap_col="gap_hp1"),
    "Ex post (КПМ)": dict(expect="fwd", measure="pi_mix", gap_col="gap_hp2"),
}


def simulate(notional: pd.Series, i0: float, rho: float) -> pd.Series:
    out, prev = {}, i0
    for t, n in notional.items():
        prev = rho * prev + (1 - rho) * n
        out[t] = prev
    return pd.Series(out)


def direction(x: pd.Series) -> pd.Series:
    return np.sign(x.where(x.abs() >= NO_CHANGE, 0.0))


def metrics(actual: pd.Series, fitted: pd.Series, prev: pd.Series) -> dict:
    m = actual.notna() & fitted.notna()
    e = (actual - fitted)[m]
    hit = (direction(actual - prev) == direction(fitted - prev))[m]
    return {"N": int(m.sum()), "ME (факт − правило)": e.mean(), "MAE": e.abs().mean(),
            "RMSE": np.sqrt((e**2).mean()), "Направление угадано, %": 100 * hit.mean()}


def main() -> None:
    df, _ = build_dataset()
    rule = next(r for r in RULES if r.name == "КПМ ДДКП")
    d = df.loc[START:END]
    actual, prev = d["key_rate"], df["key_rate"].shift(1).loc[START:END]

    series = {"Факт": actual}
    for name, spec in VERSIONS.items():
        r = apply_rule(df, rule, spec["expect"], spec["measure"], "rbar_cbr", spec["gap_col"]).loc[START:END]
        series[f"{name} :: πe"] = r["pi_e"]
        series[f"{name} :: без сглаживания"] = r["notional"]
        series[f"{name} :: на 1 шаг"] = r["one_step"]
        series[f"{name} :: динамика с 2016Q1"] = simulate(r["notional"], prev.iloc[0], rule.rho)
    series["Наивный (i_{t-1})"] = prev
    res = pd.DataFrame(series)

    rows = []
    for lo, hi, label in [(str(START), str(END), "Весь период 2016–2026")] + SUBPERIODS:
        sl = slice(pd.Period(lo, "Q"), pd.Period(hi, "Q"))
        for col in [c for c in res.columns if "на 1 шаг" in c or "динамика" in c or "Наивный" in c]:
            rows.append({"период": label, "ряд": col, **metrics(res.loc[sl, "Факт"], res.loc[sl, col], prev.loc[sl])})
    stats = pd.DataFrame(rows)

    # ---- графики
    x0 = d.index.to_timestamp(how="start")
    x = x0 + pd.Timedelta(days=45)
    fig, axes = plt.subplots(3, 1, figsize=(12, 11), sharex=True, gridspec_kw={"height_ratios": [3, 3, 2]})
    colors = {"Реальное время": "tab:green", "Ex post (КПМ)": "tab:blue"}
    for ax, kind in zip(axes[:2], ["на 1 шаг", "динамика с 2016Q1"]):
        ax.step(x0, actual, where="post", color="black", lw=2, label="Ключевая ставка (ср. за квартал)")
        for name, c in colors.items():
            ax.plot(x, res[f"{name} :: {kind}"], color=c, lw=1.6, label=f"{name}: {kind}")
            ax.plot(x, res[f"{name} :: без сглаживания"], color=c, lw=0.8, ls="--", alpha=0.6,
                    label=f"{name}: без сглаживания")
        ax.plot(x, d["neutral_nominal_cbr"], color="grey", lw=1, ls=":", label="Нейтральная ставка (ЦБ)")
        ax.set_ylabel("% годовых")
        ax.set_ylim(0, 30)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7.5, loc="upper left", ncol=2)
    axes[0].set_title("Бэктест правила КПМ ДДКП, 2016–2026: прогноз на 1 шаг (с фактическим лагом ставки)")
    axes[1].set_title("Динамическая симуляция с 2016Q1 (правило на собственных лагах)")

    ax = axes[2]
    w = 35
    for k, (name, c) in enumerate(colors.items()):
        gap = actual - res[f"{name} :: без сглаживания"]
        ax.bar(x + pd.Timedelta(days=(k - 0.5) * w), gap, width=w, color=c, alpha=0.7, label=f"{name}")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Направленность ДКП: факт − ставка по правилу без сглаживания (>0 — жёстче правила)")
    ax.set_ylabel("п.п.")
    ax.set_ylim(-20, 15)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="lower left")
    for lo, hi, _ in SUBPERIODS[1:]:
        for a in axes:
            a.axvline(pd.Period(lo, "Q").to_timestamp(), color="grey", lw=0.6, ls="-.")
    fig.tight_layout()
    fig.savefig(OUT / "backtest.png", dpi=150)
    plt.close(fig)

    with pd.ExcelWriter(OUT / "backtest.xlsx") as xw:
        stats.round(2).to_excel(xw, sheet_name="Метрики", index=False)
        t = res.join(d[["cpi_yoy", "core_yoy", "pi_mix", "gap_hp1", "gap_hp2", "neutral_nominal_cbr"]])
        t.index = t.index.astype(str)
        t.round(3).to_excel(xw, sheet_name="Ряды")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 40)
    print(stats.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
