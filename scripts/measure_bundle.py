"""Install the planned runtime dependencies into a temp dir and report their size (risk R1).

Usage: python scripts/measure_bundle.py [requirements-file] [--limit-mb N] [--python-version X.Y]

The default limit is Vercel's documented Python bundle limit: "The standard Python bundle
size limit is 500 MB uncompressed" (https://vercel.com/docs/functions/runtimes/python,
https://vercel.com/docs/functions/limitations#bundle-size-limits, checked 2026-10-26).
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERCEL_PYTHON_BUNDLE_LIMIT_MB = 500  # documented uncompressed limit (see module docstring)
# pip does NOT expand a manylinux tag to older glibc versions, so list each one we accept.
# Vercel's Python build image is Linux x86_64 (Amazon Linux 2023, glibc 2.34).
DEFAULT_PLATFORMS = ("manylinux_2_34_x86_64", "manylinux_2_28_x86_64", "manylinux2014_x86_64")
FALLBACK_PYTHON_VERSION = "3.12"  # Vercel's documented default when nothing is pinned
TOP_N = 15
MB = 1_048_576


def dir_size_mb(path: Path) -> float:
    """Total size of all files under `path`, in MB."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / MB


def default_python_version() -> str:
    """The project's pinned Python version (.python-version, which Vercel also reads)."""
    pinned = REPO_ROOT / ".python-version"
    return pinned.read_text(encoding="utf-8").strip() if pinned.is_file() else FALLBACK_PYTHON_VERSION


def parse_args() -> argparse.Namespace:
    """Parse the command line."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("requirements", nargs="?", type=Path,
                    default=Path(__file__).resolve().parent / "bundle_probe_requirements.txt")
    ap.add_argument("--limit-mb", type=float, default=VERCEL_PYTHON_BUNDLE_LIMIT_MB,
                    help="bundle size limit in MB (default: Vercel's documented Python limit, 500)")
    ap.add_argument("--python-version", default=default_python_version())
    ap.add_argument("--platform", action="append", dest="platforms",
                    help=f"wheel platform tag, repeatable (default: {', '.join(DEFAULT_PLATFORMS)})")
    return ap.parse_args()


def main() -> int:
    """Install into a target dir (Linux wheels, as on Vercel), print totals and top packages.

    Returns a non-zero exit code when the total exceeds the limit.
    """
    args = parse_args()
    platforms = args.platforms or list(DEFAULT_PLATFORMS)
    platform_flags = [flag for tag in platforms for flag in ("--platform", tag)]
    req = args.requirements.resolve()
    app_src_mb = dir_size_mb(REPO_ROOT / "src") + (REPO_ROOT / "app.py").stat().st_size / MB
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", tmp,
                        *platform_flags, "--python-version", args.python_version,
                        "--only-binary=:all:", "-r", str(req)], check=True, cwd=req.parent)
        root = Path(tmp)
        sizes = sorted(((dir_size_mb(p), p.name) for p in root.iterdir() if p.is_dir()), reverse=True)
        deps_mb = dir_size_mb(root)
    total = deps_mb + app_src_mb
    verdict = "OK" if total < args.limit_mb else "OVER"
    print(f"python {args.python_version} / {' '.join(platforms)} / {req.name}")
    print(f"TOTAL {total:.1f} MB (deps {deps_mb:.1f} + app src {app_src_mb:.1f}; "
          f"limit {args.limit_mb:g} MB) -> {verdict}")
    for mb, name in sizes[:TOP_N]:
        print(f"{mb:8.1f} MB  {name}")
    return 0 if verdict == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
