"""命令列工具：將外部公開感測資料集（ESP-CSI / TI mmWave / Thermal CSV）轉為 Unified JSONL。

使用範例：
  python scripts/import_public_dataset.py --input my_esp_csi.csv --format esp_csi --scenario A1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import ScenarioId
from app.replay_engine import parse_esp_csi_csv, parse_thermal_csv, parse_ti_mmwave_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="公開感測資料集轉 Unified JSONL 匯入工具")
    parser.add_argument("--input", required=True, help="來源 CSV 檔案路徑")
    parser.add_argument(
        "--format",
        required=True,
        choices=["esp_csi", "ti_mmwave", "thermal_csv"],
        help="來源格式類型",
    )
    parser.add_argument(
        "--scenario",
        default="A1",
        choices=["A1", "C4", "B1", "C1", "B2"],
        help="預設綁定評估場景",
    )
    parser.add_argument("--output", default=None, help="輸出 JSONL 路徑（預設存於 datasets/uploaded/）")
    args = parser.parse_args()

    src_path = Path(args.input)
    if not src_path.exists():
        raise FileNotFoundError(f"找不到來源檔案：{src_path}")

    text = src_path.read_text(encoding="utf-8-sig", errors="replace")
    sid = ScenarioId(args.scenario)
    tid = f"imported_{src_path.stem}"

    if args.format == "esp_csi":
        track = parse_esp_csi_csv(text, track_id=tid, scenario_id=sid)
    elif args.format == "ti_mmwave":
        track = parse_ti_mmwave_csv(text, track_id=tid, scenario_id=sid)
    else:
        track = parse_thermal_csv(text, track_id=tid, scenario_id=sid)

    out_path = (
        Path(args.output)
        if args.output
        else (PROJECT_ROOT / "datasets" / "uploaded" / f"{track.track_id}.jsonl")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_line = json.dumps(
        {
            "_meta": True,
            "track_id": track.track_id,
            "title": track.title,
            "scenario_id": track.scenario_id.value,
            "dataset_source": track.dataset_source,
            "description": track.description,
            "fps_nominal": track.fps_nominal,
            "is_uploaded": True,
        },
        ensure_ascii=False,
    )
    lines = [meta_line] + [s.model_dump_json() for s in track.samples]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"成功轉換 {len(track.samples)} 幀至：{out_path}")


if __name__ == "__main__":
    main()
