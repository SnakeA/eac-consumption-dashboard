# Measuring channels

The service point exposes 11 measuring channels. Most are zero or not billable for a
domestic customer, so `app.py` only offers three by default and hides the rest behind the
sidebar's **Show all channels** toggle.

Everything below was measured against the live account, not inferred from the channel
names. Figures cover the current meter's full life so far (serial `1446643`,
2025-12-09 → 2026-07-24).

## What each channel carries

| Channel | Measured | Granularity | Lag | Status |
|---|---|---|---|---|
| `S-KWH-24H` | 1067 kWh | daily | 10 d | **Default** — headline consumption |
| `S-KWH-NORMAL` | 789 kWh | daily | 10 d | **Offered** — peak-rate portion |
| `S-KWH-OFFPEAK` | 277 kWh | daily | 10 d | **Offered** — off-peak portion (26%) |
| `KWH-30MIN-LP-IMP` | populated | 30-min | **3 d** | **Used** for the intraday chart |
| `S-KVAH-24H` | 1309 kVAh | daily | 10 d | Hidden — apparent energy, informational |
| `S-KVRH-24H-EXP` | 533 kVArh | daily | 10 d | Hidden — reactive energy, informational |
| `S-KWH-EXP` | **0.0** | daily | 10 d | Hidden — export, no solar |
| `KWH-30MIN-LP-EXP` | **0.0** | 30-min | 3 d | Hidden — export, no solar |
| `S-KVAH-24H-EXP` | **0.0** | daily | 10 d | Hidden — export, no solar |
| `S-KVRH-24H` | 1.0 | daily | 10 d | Hidden — effectively zero |
| `S-KWH` | 10 reads | per billing period | inactive | Old meter — kept for history |

## Things that will bite you

**`NORMAL` + `OFFPEAK` = `24H`.** Only two of those three are independent. Totals reconcile
to 1066 vs 1067 kWh over 219 days; per-day differences of ±1 kWh are rounding, because every
daily channel reports whole kWh. Don't treat a ±1 mismatch as a bug.

**The `EXP` suffix is not consistent.** On the kWh channels it means genuine export. On the
*reactive* pair it's a lagging/leading convention instead — `S-KVRH-24H-EXP` holds the real
data (533 kVArh) while `S-KVRH-24H` is ~0, the opposite of what the name suggests. Any logic
that filters channels by matching on `EXP` will get this pair backwards.

**Freshness differs by channel.** The daily channels run ~10 days behind, but the 30-minute
load profile is only ~3 days behind. So the load profile can reconstruct daily totals that
the daily channel hasn't published yet — with one caveat: the daily channel's value dated
*D* equals the load-profile sum for day *D−1* (consumption is stamped with the following
day's boundary read). With that offset applied the two agree to within 1.1%.

**The load-profile endpoint caps at 1000 records** (~20.8 days). Anything longer needs
chunked requests.

**Interior gaps exist but are rare.** 9 missing days out of 228 since the meter went in:
2025-12-27, 2025-12-29, 2026-01-25, 2026-05-09 → 05-13, 2026-07-12.

## Power factor

kWh / kVAh = **0.815**. Technically interesting, but domestic customers in Cyprus aren't
billed on reactive power — that applies to commercial and industrial tariffs. Informational
only, which is why the kVAh/kVArh channels are hidden rather than charted.

## TODO: re-enable these when solar is installed

The four export channels are hidden purely because they read zero today. Once a PV system is
generating, they become the most interesting channels on the meter — bring them back:

1. Add `S-KWH-EXP` to `CHANNEL_ORDER` / `CHANNEL_NAMES` in `app.py` (suggested label:
   *"Exported to grid"*).
2. Chart import vs export together, and derive **self-consumption** —
   `generation − export`, i.e. how much PV output is used on site rather than sold back.
3. Use `KWH-30MIN-LP-EXP` alongside `KWH-30MIN-LP-IMP` on the intraday chart. Overlaying the
   two half-hourly curves is what shows whether load is actually being shifted into the
   generation window.
4. Re-check `S-KVAH-24H-EXP` at the same time — inverters affect apparent power, so it may
   stop reading zero.
5. Revisit the net-metering arrangement in the cost model: exported units are usually
   credited at a different rate from imported ones, so a single €/kWh figure stops working.
