"""
Cost model — utilisation percentage to dollars, and breach accounting.

Every rate, capacity and policy value is read from the `config` table, so
re-pricing the system against a different instance type or region is a
row update, not a code change.

    percent -> provisioned units -> monthly cost

Breach accounting counts EPISODES as well as samples
----------------------------------------------------
"5% of samples exceeded the allocation" describes two very different
situations: fifty isolated one-sample spikes, or one sustained outage
lasting fifty samples. The second breaches an SLA in a way the first does
not, and a rate alone cannot distinguish them. `breach_stats` therefore
reports episode count and the longest episode alongside the rate.
"""

import numpy as np
import pandas as pd

import config


# ----------------------------------------------------------------------
# Capacity
# ----------------------------------------------------------------------
def resource_map():
    """target -> (unit label, node capacity, cost key). Built from config."""
    return {
        "cpu_percent": ("vCPUs", config.get_int("node.vcpus"), "cost.vcpu_hour"),
        "mem_percent": ("GB RAM", config.get_int("node.ram_gb"), "cost.gb_ram_hour"),
        "disk_read_mb_s": ("MB/s I/O", config.get_int("node.storage_gb"),
                         "cost.gb_storage_month"),
    }


def units_for_percent(target, percent):
    """A utilisation percentage of the node -> provisioned units."""
    mapping = resource_map()
    if target not in mapping:
        raise KeyError(f"unknown target '{target}'; expected {sorted(mapping)}")
    _, capacity, _ = mapping[target]
    return (float(percent) / 100.0) * capacity


# Kept for callers written against the previous API.
def vcpus_for_percent(cpu_percent):
    return units_for_percent("cpu_percent", cpu_percent)


def ram_gb_for_percent(mem_percent):
    return units_for_percent("mem_percent", mem_percent)


def io_throughput_for_percent(disk_read_mb_s):
    return units_for_percent("disk_read_mb_s", disk_read_mb_s)


# ----------------------------------------------------------------------
# Pricing
# ----------------------------------------------------------------------
def monthly_cost(vcpus, ram_gb, storage_gb):
    """Monthly dollars for an explicit allocation of the three resources."""
    hours = config.get_int("cost.hours_per_month")
    compute = vcpus * config.get_float("cost.vcpu_hour") * hours
    memory = ram_gb * config.get_float("cost.gb_ram_hour") * hours
    storage = storage_gb * config.get_float("cost.gb_storage_month")
    return {
        "compute": round(compute, 2),
        "memory": round(memory, 2),
        "storage": round(storage, 2),
        "total": round(compute + memory + storage, 2),
    }


def resource_monthly_cost(target, percent):
    """Monthly dollars for ONE resource at a given allocation."""
    mapping = resource_map()
    _, _, cost_key = mapping[target]
    units = units_for_percent(target, percent)
    rate = config.get_float(cost_key)

    # Storage is priced per GB-month; compute and memory per unit-hour.
    if target == "disk_read_mb_s":
        return round(units * rate, 2)
    return round(units * rate * config.get_int("cost.hours_per_month"), 2)


def cost_from_percentages(cpu_percent, mem_percent, disk_read_mb_s):
    return monthly_cost(
        units_for_percent("cpu_percent", cpu_percent),
        units_for_percent("mem_percent", mem_percent),
        units_for_percent("disk_read_mb_s", disk_read_mb_s),
    )


def cost_for_allocation(allocation):
    """{target: percent} -> the full monthly breakdown."""
    return monthly_cost(
        units_for_percent("cpu_percent", allocation.get("cpu_percent", 100.0)),
        units_for_percent("mem_percent", allocation.get("mem_percent", 100.0)),
        units_for_percent("disk_read_mb_s", allocation.get("disk_read_mb_s", 100.0)),
    )


def static_cost():
    """The over-provisioned baseline: 100% of the node, always."""
    return monthly_cost(
        config.get_int("node.vcpus"),
        config.get_int("node.ram_gb"),
        config.get_int("node.storage_gb"),
    )


# ----------------------------------------------------------------------
# Breach accounting
# ----------------------------------------------------------------------
def breach_stats(actual, allocation):
    """How badly an allocation under-serves observed demand."""
    values = np.asarray(actual, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"samples": 0, "breaches": 0, "breach_rate": 0.0,
                "episodes": 0, "longest_episode": 0, "worst_overshoot": 0.0}

    over = values > float(allocation)
    breaches = int(over.sum())

    # An episode is a run of consecutive breaching samples. Counting runs
    # rather than samples distinguishes a sustained outage from scattered
    # spikes, which a rate alone cannot.
    episodes, longest, current = 0, 0, 0
    for flag in over:
        if flag:
            current += 1
            if current == 1:
                episodes += 1
            longest = max(longest, current)
        else:
            current = 0

    return {
        "samples": int(values.size),
        "breaches": breaches,
        "breach_rate": round(100.0 * breaches / values.size, 3),
        "episodes": episodes,
        "longest_episode": longest,
        "worst_overshoot": round(float(np.max(values - allocation)) if breaches else 0.0, 3),
    }


def apply_safety_floor(candidate, actual, max_breach_rate=None):
    """Raise `candidate` until the breach rate on recent actuals complies.

    The forecast proposes a lean allocation; this is the constraint that
    makes it safe. When the two disagree sharply, that gap is the real
    cost-versus-reliability tension in the system.
    """
    max_breach_rate = (max_breach_rate if max_breach_rate is not None
                       else config.get_float("policy.max_breach_rate"))

    values = np.asarray(actual, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return round(float(candidate), 2), {"floored": False,
                                            "reason": "no recent actuals"}

    start = float(candidate)
    step = config.get_float("policy.sweep_step")
    for level in np.arange(start, 100.0 + step, step):
        if breach_stats(values, level)["breach_rate"] <= max_breach_rate:
            floored = level > start + 1e-9
            return round(float(min(level, 100.0)), 2), {
                "floored": floored,
                "raised_by": round(float(level - start), 2) if floored else 0.0,
                "reason": (f"raised from {start:.2f}% to hold breaches at or "
                           f"below {max_breach_rate}%" if floored
                           else "forecast allocation already complies"),
            }

    return 100.0, {"floored": True, "raised_by": round(100.0 - start, 2),
                   "reason": f"even 100% cannot hold {max_breach_rate}%"}


def sweep(target, actual, start=None, stop=None, step=None):
    """Cost against breach rate across the allocation range.

    The curve behind the cost/SLA tradeoff: every level, what it costs,
    and how often it would have failed.
    """
    start = start if start is not None else config.get_float("policy.sweep_start")
    stop = stop if stop is not None else config.get_float("policy.sweep_stop")
    step = step if step is not None else config.get_float("policy.sweep_step")
    max_breach = config.get_float("policy.max_breach_rate")

    rows = []
    for level in np.arange(start, stop + step, step):
        stats = breach_stats(actual, level)
        rows.append({
            "allocation_percent": round(float(level), 2),
            "monthly_cost": resource_monthly_cost(target, level),
            "breach_rate": stats["breach_rate"],
            "episodes": stats["episodes"],
            "longest_episode": stats["longest_episode"],
            "sla_met": stats["breach_rate"] <= max_breach,
        })
    return pd.DataFrame(rows)


def sla_minimum(curve):
    """The cheapest allocation on the curve that still meets the SLA."""
    compliant = curve[curve["sla_met"]]
    if compliant.empty:
        return None
    return compliant.iloc[0].to_dict()


if __name__ == "__main__":
    print("PRICING BASIS (from the config table)")
    for key in ("cost.vcpu_hour", "cost.gb_ram_hour", "cost.gb_storage_month",
                "cost.hours_per_month", "node.vcpus", "node.ram_gb",
                "node.storage_gb"):
        print(f"  {key:26s} {config.get(key)}")

    full = static_cost()
    print("\nFULL NODE (100% provisioned)")
    for key, value in full.items():
        print(f"  {key:9s} ${value}")

    print("\nPER-RESOURCE COST AT SEVERAL ALLOCATIONS")
    print(f"  {'allocation':>10} {'cpu $':>10} {'memory $':>10} {'disk I/O $':>10}")
    for level in (50, 70, 85, 100):
        print(f"  {level:>9}% "
              f"{resource_monthly_cost('cpu_percent', level):>10} "
              f"{resource_monthly_cost('mem_percent', level):>10} "
              f"{resource_monthly_cost('disk_read_mb_s', level):>10}")

    from model.features import load_clean_frame

    frame = load_clean_frame()
    if not frame.empty:
        actual = frame["cpu_percent"].to_numpy()
        print(f"\nBREACH ACCOUNTING — cpu_percent over {len(actual)} samples")
        print(f"  {'alloc':>6} {'rate':>7} {'episodes':>9} {'longest':>8} {'worst over':>11}")
        for level in (50, 70, 85, 95, 100):
            s = breach_stats(actual, level)
            print(f"  {level:>5}% {s['breach_rate']:>6}% {s['episodes']:>9} "
                  f"{s['longest_episode']:>8} {s['worst_overshoot']:>11}")

        curve = sweep("cpu_percent", actual)
        minimum = sla_minimum(curve)
        if minimum:
            print(f"\n  cheapest CPU allocation meeting the "
                  f"{config.get_float('policy.max_breach_rate')}% SLA: "
                  f"{minimum['allocation_percent']}% "
                  f"at ${minimum['monthly_cost']}/mo "
                  f"({minimum['breach_rate']}% breaches)")
