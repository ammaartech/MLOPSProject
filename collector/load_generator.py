import time
import math
import multiprocessing
from datetime import datetime


def _burn(duration):
    """Fully occupy one CPU core for `duration` seconds."""
    end = time.time() + duration
    x = 0.0
    while time.time() < end:
        x += math.sqrt(12345.678)  # meaningless work, just burns cycles


def _spawn_load(intensity, duration):
    """
    intensity = number of cores to stress (0 = idle).
    Spawns that many burn processes for `duration` seconds.
    """
    procs = []
    for _ in range(intensity):
        p = multiprocessing.Process(target=_burn, args=(duration,))
        p.start()
        procs.append(p)
    for p in procs:
        p.join()


def wave_pattern(total_minutes=15, step_seconds=15):
    """
    Generates a repeating wave of CPU load so the collector records
    a forecastable pattern (rise -> peak -> fall -> idle -> repeat).
    Run this in one terminal while the logger runs in another.
    """
    max_cores = multiprocessing.cpu_count()
    print(f"Load generator started | cores available={max_cores} | "
          f"duration={total_minutes}min")

    start = time.time()
    tick = 0
    try:
        while (time.time() - start) < total_minutes * 60:
            # sine wave over ~2 min period, mapped to 0..max_cores
            phase = math.sin(tick / 8.0 * math.pi)          # -1..1
            level = int(round((phase + 1) / 2 * max_cores))  # 0..max_cores
            level = max(0, min(max_cores, level))

            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] load level: {level}/{max_cores} cores")

            if level > 0:
                _spawn_load(level, step_seconds)
            else:
                time.sleep(step_seconds)  # idle trough

            tick += 1
    except KeyboardInterrupt:
        print("\nLoad generator stopped by user.")
    print("Load generation complete.")


if __name__ == "__main__":
    # ~15 min of waves; adjust total_minutes as you like
    wave_pattern(total_minutes=15, step_seconds=15)