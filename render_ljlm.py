    ax.set_xlabel("u wind (kt)")
    ax.set_ylabel("v wind (kt)")

    station = data.get("station_name", "Ljubljana")
    station_id = data.get("station", 14015)
    nominal = _nominal_title(data)
    ax.set_title("Hodograph · 0–12 km AGL", loc="left", pad=18)
    ax.text(
        0.0, 1.012,
        f"{station} ({station_id})  ·  {nominal}",
        transform=ax.transAxes,
        ha="left", va="bottom", fontsize=9.5, color=MUTED,
    )

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        legend = ax.legend(
            handles, labels,
            loc="upper right",
            frameon=False,
            fontsize=8.5,
            handlelength=2.4,
        )
        for text in legend.get_texts():
            text.set_color(MUTED)

    # Compact diagnostic footer.
    shear = metpy_data.get("bulk_shear", {})
    srh = metpy_data.get("srh", {})
    footer = []
    for key, label in (("0_1km", "0–1"), ("0_3km", "0–3"), ("0_6km", "0–6")):
        item = shear.get(key, {})
        if _finite(item.get("magnitude_ms")):
            kt = (float(item["magnitude_ms"]) * units("m/s")).to("knots").magnitude
            footer.append(f"{label} km shear {kt:.0f} kt")
    rm_srh = srh.get("0_3km_right_mover", {})
    if _finite(rm_srh.get("total_m2s2")):
        footer.append(f"0–3 km SRH(RM) {float(rm_srh['total_m2s2']):.0f} m²/s²")

    if footer:
        ax.text(
            0.0, -0.105,
            "   ·   ".join(footer),
            transform=ax.transAxes,
            ha="left", va="top", fontsize=8.5, color=MUTED,
        )

    fig.savefig(output, bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


# -----------------------------------------------------------------------------
# PUBLIC ENTRY POINT
# -----------------------------------------------------------------------------

def render_products(input_path: str | os.PathLike, diagnostics_root="diagnostics"):
    input_path = Path(input_path)
    diagnostics_root = Path(diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    latest_skewt = diagnostics_root / "latest_skewt.png"
    latest_hodo = diagnostics_root / "latest_hodograph.png"
    archive_skewt, archive_hodo = _archive_paths(data, diagnostics_root)

    # Render archive products first, then copy by re-rendering latest names.
    # Re-rendering avoids file-copy platform assumptions and guarantees both
    # outputs are complete images if a run is interrupted.
    render_skewt(data, archive_skewt)
    render_hodograph(data, archive_hodo)
    render_skewt(data, latest_skewt)
    render_hodograph(data, latest_hodo)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "version": 1,
        "station": data.get("station"),
        "station_name": data.get("station_name"),
        "sounding_id": data.get("sounding_id"),
        "nominal_date": data.get("nominal_date"),
        "term": data.get("term"),
        "launch_time": data.get("launch_time"),
        "processed_at": data.get("processed_at"),
        "rendered_at": generated_at,
        "latest": {
            "skewt": latest_skewt.as_posix(),
            "hodograph": latest_hodo.as_posix(),
        },
        "archive": {
            "skewt": archive_skewt.as_posix(),
            "hodograph": archive_hodo.as_posix(),
        },
    }
    with (diagnostics_root / "latest_products.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("Rendered:", latest_skewt)
    print("Rendered:", latest_hodo)
    print("Archived:", archive_skewt)
    print("Archived:", archive_hodo)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Render LJLM Skew-T and hodograph PNG products.")
    parser.add_argument("--input", default="data/latest.json", help="Input sounding JSON")
    parser.add_argument("--diagnostics", default="diagnostics", help="Output diagnostics directory")
    args = parser.parse_args()
    render_products(args.input, args.diagnostics)


if __name__ == "__main__":
    main()
