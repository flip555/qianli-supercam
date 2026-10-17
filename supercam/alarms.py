"""Alarm rules.

A rule watches the whole frame or one region, on its maximum or average, and fires
once the condition has held for a dwell time. A fired rule stays fired until the
value clears a hysteresis band, so a noisy 160x120 sensor cannot flap.
"""
HYSTERESIS = 0.5          # degrees below/above the threshold before re-arming


def evaluate(rules, frame_stats, region_stats, now):
    """Update each rule's state in place; return the rules that fired this pass."""
    fired = []
    by_id = {r.get("id"): r for r in (region_stats or [])}
    for rule in rules:
        if not rule.get("enabled", True):
            rule["state"] = "idle"
            rule["held_since"] = 0
            continue
        metric = rule.get("metric") or "max"
        if rule.get("scope") == "region":
            st = by_id.get(rule.get("region_id"))
            if not st:
                rule["state"] = "idle"
                rule["held_since"] = 0
                continue
            value = st.get("max_c" if metric == "max" else "avg_c")
        else:
            value = (frame_stats or {}).get("max_c" if metric == "max" else "mean_c")
        if value is None:
            continue
        rule["value"] = value
        threshold = float(rule.get("threshold") or 0)
        dwell = max(0.0, float(rule.get("dwell_ms") or 0) / 1000.0)
        above = bool(rule.get("above", True))
        hit = (value >= threshold) if above else (value <= threshold)
        if hit:
            if not rule.get("held_since"):
                rule["held_since"] = now
            if rule.get("state") != "firing" and (now - rule["held_since"]) >= dwell:
                rule["state"] = "firing"
                rule["last_fired"] = now
                fired.append(dict(rule))
            elif rule.get("state") != "firing":
                rule["state"] = "pending"
        else:
            clear_at = (threshold - HYSTERESIS) if above else (threshold + HYSTERESIS)
            still_latched = (value > clear_at) if above else (value < clear_at)
            if rule.get("state") == "firing" and still_latched:
                pass                      # keep the latched state through the band
            else:
                rule["state"] = "idle"
                rule["held_since"] = 0
    return fired
