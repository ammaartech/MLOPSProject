from collector.psutil_logger import run_logger, log_once
from crud.metrics_crud import (
    read_all,
    read_latest,
    count_metrics,
    update_metric,
    delete_metric,
    purge_before,
)


def print_rows(rows):
    if not rows:
        print("No records found.")
        return
    print(f"\n{'ID':<4}{'Timestamp':<22}{'CPU%':>7}{'MEM%':>7}{'DISK%':>7}")
    print("-" * 50)
    for r in rows:
        # r = (id, ts, cpu, mem_pct, mem_mb, disk_pct, disk_gb)
        print(f"{r[0]:<4}{r[1]:<22}{r[2]:>7.1f}{r[3]:>7.1f}{r[5]:>7.1f}")


def collector_menu():
    while True:
        print("\n---------- Collector ----------")
        print("1. Log one sample now")
        print("2. Run logger for a fixed duration")
        print("3. Run logger continuously (Ctrl+C to stop)")
        print("4. Back")

        choice = input("Enter your choice: ").strip()

        match choice:
            case "1":
                log_once()
            case "2":
                secs = int(input("Duration in seconds: "))
                run_logger(duration=secs)
            case "3":
                run_logger()
            case "4":
                break
            case _:
                print("Invalid choice.")


def data_menu():
    while True:
        print("\n---------- Data / CRUD ----------")
        print("1. View all records")
        print("2. View latest N records")
        print("3. Total record count")
        print("4. Update a field on a record")
        print("5. Delete a record")
        print("6. Purge records before a timestamp")
        print("7. Back")

        choice = input("Enter your choice: ").strip()

        match choice:
            case "1":
                print_rows(read_all())
            case "2":
                n = int(input("How many? "))
                print_rows(read_latest(n))
            case "3":
                print("Total records:", count_metrics())
            case "4":
                mid = int(input("Record ID: "))
                field = input("Field (cpu_percent/mem_percent/mem_used_mb/disk_percent/disk_used_gb): ").strip()
                value = float(input("New value: "))
                result = update_metric(mid, field, value)
                print("Updated." if result else "Update failed.")
            case "5":
                mid = int(input("Record ID: "))
                result = delete_metric(mid)
                print("Deleted." if result else "Delete failed.")
            case "6":
                cutoff = input("Delete records before (e.g. 2026-07-17T15:00:00): ").strip()
                result = purge_before(cutoff)
                print(f"Purged {result} record(s).")
            case "7":
                break
            case _:
                print("Invalid choice.")


def main():
    while True:
        print("\n========== Predictive Resource Monitor ==========")
        print("1. Collector (log metrics)")
        print("2. Data / CRUD (view & manage metrics)")
        print("3. Exit")

        choice = input("Enter your choice: ").strip()

        match choice:
            case "1":
                collector_menu()
            case "2":
                data_menu()
            case "3":
                print("Exiting...")
                break
            case _:
                print("Invalid choice.")


if __name__ == "__main__":
    main()