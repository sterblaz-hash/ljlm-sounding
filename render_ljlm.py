        ax.text(
            0.0, -0.105,
            "   ·   ".join(footer),
            transform=ax.transAxes,
            ha="left", va="top",
            fontsize=8.5, color=MUTED,
        )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)

def _render_all_for_data(data: dict, paths: dict[str, Path]):
    render_skewt(data, paths["skewt"])
    render_lowlevel(data, paths["lowlevel"])
    # compatibility copy for current Apps Script, which still looks for skewt_zoom
    shutil.copyfile(paths["lowlevel"], paths["skewt_zoom"])
    render_thetae(data, paths["thetae"])
    render_hodograph(data, paths["hodograph"])


def render_products(input_path: str | os.PathLike, diagnostics_root="diagnostics"):
    input_path = Path(input_path)
    diagnostics_root = Path(diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    latest = _latest_paths(diagnostics_root)
    archive = _archive_paths(data, diagnostics_root)

    _render_all_for_data(data, archive)
    _render_all_for_data(data, latest)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "version": 2,
        "station": data.get("station"),
        "station_name": data.get("station_name"),
        "sounding_id": data.get("sounding_id"),
        "nominal_date": data.get("nominal_date"),
        "term": data.get("term"),
        "launch_time": data.get("launch_time"),
        "processed_at": data.get("processed_at"),
        "rendered_at": generated_at,
        "latest": {k: v.as_posix() for k, v in latest.items()},
        "archive": {k: v.as_posix() for k, v in archive.items()},
        # compatibility aliases for existing Apps Script
        "skewt": latest["skewt"].as_posix(),
        "skewt_zoom": latest["skewt_zoom"].as_posix(),
        "lowlevel": latest["lowlevel"].as_posix(),
        "thetae": latest["thetae"].as_posix(),
        "hodograph": latest["hodograph"].as_posix(),
    }
    with (diagnostics_root / "latest_products.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("Rendered latest products to", diagnostics_root)
    return manifest


def backfill_products(data_root="data", diagnostics_root="diagnostics"):
    data_root = Path(data_root)
    diagnostics_root = Path(diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    count = 0
    failures = []
    for path in sorted(data_root.rglob("*.json")):
        if path.name in {"latest.json", "status.json", "latest_products.json"}:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "levels" not in data:
                continue
            archive = _archive_paths(data, diagnostics_root)
            _render_all_for_data(data, archive)
            count += 1
            print(f"Backfilled {path}")
        except Exception as exc:
            failures.append((str(path), str(exc)))
            print(f"Backfill failed for {path}: {exc}")

    print(f"Backfilled {count} soundings.")
    if failures:
        print("Failures:")
        for path, exc in failures:
            print(f"- {path}: {exc}")
    return {"count": count, "failures": failures}


def main():
    parser = argparse.ArgumentParser(description="Render improved LJLM sounding graphics.")
    parser.add_argument("--input", default="data/latest.json", help="Input sounding JSON")
    parser.add_argument("--diagnostics", default="diagnostics", help="Output diagnostics directory")
    parser.add_argument("--data-root", default="data", help="Data root for backfill mode")
    parser.add_argument("--backfill", action="store_true", help="Backfill all archived sounding JSON files")
    args = parser.parse_args()

    if args.backfill:
        backfill_products(args.data_root, args.diagnostics)
    else:
        render_products(args.input, args.diagnostics)


if __name__ == "__main__":
    main()
