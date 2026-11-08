import argparse
import json
from pathlib import Path
import sys


def self_check(report):
    import numpy as np
    from muter import __version__
    from muter.audio import Model, MODEL, SR, write_json
    from importlib.metadata import version
    result = {"app_version": __version__, "model_dir": str(MODEL), "microphone_opened": False}
    try:
        model = Model()
        model.load()
        voice = (.05 * np.sin(2 * np.pi * 180 * np.arange(SR * 3) / SR)).astype(np.float32)
        y, stats = model.process(np.zeros(SR, np.float32), voice)
        result.update({"ok": bool(len(y) == SR and np.isfinite(y).all()), "samples": len(y),
                       "onnxruntime": version("onnxruntime"), "providers": model.vl.chunk.get_providers(),
                       "processing": stats})
        from muter.denoise import GTCRNModel
        from muter.pipeline import GuardedModel
        clean, denoise_stats = GTCRNModel().process(np.zeros(SR, np.float32))
        stronger, strong_stats = GuardedModel(model, smooth=True).process(np.zeros(SR, np.float32), voice)
        result["environment_denoise"] = {"samples": len(clean), "stats": denoise_stats}
        result["enhanced_isolation"] = {"samples": len(stronger), "stats": strong_stats}
        result["ok"] = (result["ok"] and len(clean) == len(stronger) == SR
                        and bool(np.isfinite(clean).all()) and bool(np.isfinite(stronger).all()))
    except Exception as exc:
        result.update({"ok": False, "error": repr(exc)})
    Path(report).parent.mkdir(parents=True, exist_ok=True)
    write_json(report, result)
    return 0 if result["ok"] else 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", metavar="REPORT_JSON")
    parser.add_argument("--route-watch", nargs=2, metavar=("PID", "STATE"))
    parser.add_argument("--snapshot-audio")
    parser.add_argument("--restore-audio")
    args = parser.parse_args()
    if args.snapshot_audio or args.restore_audio:
        from muter.routing import system_snapshot
        system_snapshot(args.snapshot_audio or args.restore_audio, bool(args.restore_audio))
        sys.exit(0)
    if args.route_watch:
        from muter.routing import watch_route
        watch_route(int(args.route_watch[0]), args.route_watch[1])
        sys.exit(0)
    if args.self_check:
        sys.exit(self_check(args.self_check))
    from muter.app import main
    main()
