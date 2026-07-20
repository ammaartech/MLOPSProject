"""
Cost model: converts utilization % into real AWS dollar costs.

We model a single reference node (NODE_VCPUS / NODE_RAM_GB /
NODE_STORAGE_GB). A utilization percentage is turned into actual
provisioned units, then priced with current AWS on-demand rates.
"""

from config import (
    COST_PER_VCPU_HOUR,
    COST_PER_GB_RAM_HOUR,
    COST_PER_GB_STORAGE_MONTH,
    NODE_VCPUS,
    NODE_RAM_GB,
    NODE_STORAGE_GB,
    HOURS_PER_MONTH,
)


def vcpus_for_percent(cpu_percent):
    """A CPU % of the node -> number of vCPUs that represents."""
    return (cpu_percent / 100.0) * NODE_VCPUS


def ram_gb_for_percent(mem_percent):
    return (mem_percent / 100.0) * NODE_RAM_GB


def storage_gb_for_percent(disk_percent):
    return (disk_percent / 100.0) * NODE_STORAGE_GB


def monthly_cost(vcpus, ram_gb, storage_gb):
    """Total monthly $ for a given allocation of the three resources."""
    compute = vcpus * COST_PER_VCPU_HOUR * HOURS_PER_MONTH
    memory = ram_gb * COST_PER_GB_RAM_HOUR * HOURS_PER_MONTH
    storage = storage_gb * COST_PER_GB_STORAGE_MONTH
    return {
        "compute": round(compute, 2),
        "memory": round(memory, 2),
        "storage": round(storage, 2),
        "total": round(compute + memory + storage, 2),
    }


def cost_from_percentages(cpu_percent, mem_percent, disk_percent):
    """Convenience: utilization % -> monthly $ breakdown."""
    return monthly_cost(
        vcpus_for_percent(cpu_percent),
        ram_gb_for_percent(mem_percent),
        storage_gb_for_percent(disk_percent),
    )


if __name__ == "__main__":
    # Sanity check: cost of running the node fully provisioned (100%)
    full = monthly_cost(NODE_VCPUS, NODE_RAM_GB, NODE_STORAGE_GB)
    print("Full node (100% provisioned) monthly cost:")
    for k, v in full.items():
        print(f"  {k:8s}: ${v}")