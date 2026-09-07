"""Subprocess worker that runs a single vtracer trace.

Isolated on purpose. The vtracer cp314 wheel **segfaults whenever any keyword
argument is passed** (0.6.15 / PyO3 arg-parsing mismatch on Python 3.14), and a
segfault kills the entire interpreter. Running each trace in a short-lived child
means one pathological image can never take down an unattended beat — the same
containment rationale as ``farm.spawn_farm_job``.

Params arrive as one JSON object on argv so the parent stays the only place that
knows about config.

Usage (not intended for humans):
    python -m src.microstock._vtrace_worker <in.png> <out.svg> '<params-json>'
"""

from __future__ import annotations

import json
import sys

# Positional order required by vtracer.convert_image_to_svg_py after image/out paths.
PARAM_ORDER = (
    "colormode",
    "hierarchical",
    "mode",
    "filter_speckle",
    "color_precision",
    "layer_difference",
    "corner_threshold",
    "length_threshold",
    "max_iterations",
    "splice_threshold",
    "path_precision",
)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: _vtrace_worker <in.png> <out.svg> <params-json>", file=sys.stderr)
        return 2
    in_png, out_svg, params_json = argv[1], argv[2], argv[3]
    params = json.loads(params_json)

    import vtracer

    # MUST be positional — keyword args segfault this wheel.
    args = [params.get(key) for key in PARAM_ORDER]
    vtracer.convert_image_to_svg_py(in_png, out_svg, *args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
