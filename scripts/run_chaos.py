from __future__ import annotations

import argparse

from reliability_lab.chaos import load_query_records, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    args = parser.parse_args()
    config = load_config(args.config)
    metrics = run_simulation(config, load_query_records())
    metrics.write_json(args.out)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
