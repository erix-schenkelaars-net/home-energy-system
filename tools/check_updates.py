#!/usr/bin/env python3
"""check_updates.py — report what could be updated, change nothing.

For every service on every host: the base image (Dockerfile FROM / compose image:) against the
newest tag in the same series (Docker Hub, or ghcr.io), and every pinned pip package against PyPI --
newest release, newest in the same major, and the known vulnerabilities (PyPI's OSV data) of the
version we run. Hosts: this repo on the local machine, plus every remote in CHECK_UPDATES_REMOTES
(ssh aliases, default "pi5"), read over ssh from its ~/docker/<service>/ files. A running container
whose image differs from what its compose file now names is flagged: the pin only lands on `up -d`.

Update policy this report feeds (see the update runbook):
  * patch bumps are routine; minor/major only deliberately, one service at a time;
  * a release must be at least MIN_AGE_DAYS old before we take it -- the "age" column shows it.

Usage:
  python3 tools/check_updates.py              # full report
  python3 tools/check_updates.py --only-stale # hide packages that are already current
  python3 tools/check_updates.py read_p1 ...  # only these services
  python3 tools/check_updates.py --local-only # skip the remote hosts

Needs internet (Docker Hub + PyPI), so it is not part of the test suite.
"""
import os
import re
import sys
import subprocess
import json
import pathlib
import argparse
import urllib.request
from datetime import datetime, timezone

ROOT         = pathlib.Path(__file__).resolve().parent.parent
MIN_AGE_DAYS = 14
TIMEOUT_S    = 20
REMOTES      = os.environ.get("CHECK_UPDATES_REMOTES", "pi5").split()
FILE_GLOBS   = ("*/Dockerfile", "*/docker-compose*.yml", "*/requirements.txt")
_cache: dict = {}


def get_json(url: str):
    if url not in _cache:
        req = urllib.request.Request(url, headers={"User-Agent": "check_updates/1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            _cache[url] = json.load(r)
    return _cache[url]


def vkey(v: str) -> tuple:
    """Sortable key for dotted versions; non-numeric parts (rc, post1) sort as 0 after the digits."""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))


def age_days(iso: str | None) -> str:
    if not iso:
        return "?"
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return str((datetime.now(timezone.utc) - t).days)


# ---------------------------------------------------------------- hosts
def local_files() -> dict[str, str]:
    return {str(f.relative_to(ROOT)): f.read_text()
            for g in FILE_GLOBS for f in sorted(ROOT.glob(g))}


def remote_files(host: str) -> dict[str, str]:
    """All service files of one remote in a single ssh round trip, keyed 'service/file'."""
    globs = " ".join(f"~/docker/{g}" for g in FILE_GLOBS)
    cmd = (f'cd ~/docker && for f in {globs}; do [ -f "$f" ] || continue; '
           f'echo "@@@ ${{f#$HOME/docker/}}"; cat "$f"; done')
    out = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, cmd],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    files, cur = {}, None
    for line in out.splitlines(keepends=True):
        if line.startswith("@@@ "):
            cur = line[4:].strip()
            files[cur] = ""
        elif cur:
            files[cur] += line
    return files


def running_images(host: str | None) -> dict[str, set[str]]:
    """service dir -> image refs of its running containers (from the compose working_dir label)."""
    cmd = ["docker", "ps", "--format", '{{.Label "com.docker.compose.project.working_dir"}}\t{{.Image}}']
    if host:
        cmd = ["ssh", "-o", "ConnectTimeout=10", host, " ".join(cmd[:3]) + " '" + cmd[3] + "'"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return {}
    res: dict[str, set[str]] = {}
    for line in out.splitlines():
        wd, _, img = line.partition("\t")
        if wd:
            res.setdefault(pathlib.PurePosixPath(wd).name, set()).add(img)
    return res


# ---------------------------------------------------------------- images
def find_images(files: dict[str, str]) -> list[tuple[str, str, str]]:
    """(service, image, 'FROM' or 'compose')."""
    out = []
    for path, text in sorted(files.items()):
        svc, _, name = path.partition("/")
        if name == "Dockerfile":
            m = re.search(r"^FROM\s+(\S+)", text, re.M)
            if m:
                out.append((svc, m.group(1), "FROM"))
        elif name.startswith("docker-compose"):
            out += [(svc, m.group(1), "compose") for m in re.finditer(r"^\s*image:\s*(\S+)", text, re.M)]
    return out


def _hub_tags(repo: str, prefix: str, pages: int = 5) -> list[tuple[str, str | None]]:
    if repo.startswith("ghcr.io/"):
        return [(t, None) for t in _ghcr_tags(repo[len("ghcr.io/"):]) if t.startswith(prefix)]
    tags, url = [], f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&name={prefix}"
    for _ in range(pages):
        d = get_json(url)
        tags += [(t["name"], t.get("last_updated")) for t in d.get("results", [])]
        url = d.get("next")
        if not url:
            break
    return tags


def _ghcr_tags(repo: str) -> list[str]:
    """ghcr.io tag list (anonymous token; ghcr gives no dates)."""
    key = f"ghcr:{repo}"
    if key not in _cache:
        tok = get_json(f"https://ghcr.io/token?scope=repository:{repo}:pull")["token"]
        tags, url = [], f"https://ghcr.io/v2/{repo}/tags/list?n=1000"
        while url:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                tags += json.load(r).get("tags") or []
                link = r.headers.get("Link", "")
            m = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
            url = f"https://ghcr.io{m.group(1)}" if m else None
        _cache[key] = tags
    return _cache[key]


def _series(tags, head: str, suffix: str):
    pat = re.compile(re.escape(head) + r"\.(\d+)" + re.escape(suffix))
    return [(n, u) for n, u in tags if pat.fullmatch(n)]


def image_report(image: str) -> tuple[str, str, str, str]:
    """(newest in series, days since that tag was last pushed, next-series hint, note)."""
    repo, _, tag = image.partition(":")
    if "/" not in repo:
        repo = f"library/{repo}"
    if tag in ("", "latest"):
        return "?", "?", "", "'latest' -> pin it"
    m = re.fullmatch(r"([\d.]+?)(-.+)?", tag)
    if not m:
        return "?", "?", "", "tag not understood"
    num, suffix = m.group(1), m.group(2) or ""
    # Floating tag (python:3.11-slim, phpmyadmin:5.2): a <tag>.N series exists under it.
    series = _series(_hub_tags(repo, num + "."), num, suffix)
    if series:
        head, note = num, "floating tag -> pin it"
    else:                                   # pinned: free the last number, 3.13.14-slim -> 3.13.N
        head = num.rsplit(".", 1)[0]
        series = _series(_hub_tags(repo, head + "."), head, suffix)
        note = ""
    if not series:
        return "?", "?", "", "no tags found"
    newest, upd = max(series, key=lambda x: vkey(x[0]))
    parts = head.split(".")
    nxt = ".".join(parts[:-1] + [str(int(parts[-1]) + 1)])
    hint = f"{nxt} exists" if _series(_hub_tags(repo, nxt, 1), nxt, suffix) or \
        any(n == nxt + suffix for n, _ in _hub_tags(repo, nxt, 1)) else ""
    return newest, age_days(upd), hint, note


# ---------------------------------------------------------------- pip
REQ = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(==|>=)\s*([^\s;#]+)")


def read_requirements(text: str) -> list[tuple[str, str, str]]:
    out = []
    for line in text.splitlines():
        m = REQ.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def pypi_report(name: str, version: str) -> tuple[str, str, str, int]:
    """(newest stable, its age, newest in our major, number of known vulns in our version)."""
    d = get_json(f"https://pypi.org/pypi/{name}/json")
    stable = [v for v, files in d["releases"].items()
              if files and re.fullmatch(r"[\d.]+(\.post\d+)?", v)
              and not all(f.get("yanked") for f in files)]
    newest = max(stable, key=vkey) if stable else d["info"]["version"]
    files = d["releases"].get(newest) or [{}]
    major = version.split(".")[0]
    same_major = max((v for v in stable if v.split(".")[0] == major), key=vkey, default="")
    try:
        vulns = len(get_json(f"https://pypi.org/pypi/{name}/{version}/json").get("vulnerabilities") or [])
    except Exception:
        vulns = -1
    return newest, age_days(files[0].get("upload_time_iso_8601")), same_major, vulns


def report_host(label: str, files: dict[str, str], running: dict[str, set[str]],
                want: set[str], only_stale: bool) -> dict[str, int]:
    print(f"\n{'=' * 100}\n{label}\n{'=' * 100}\n")
    print(f"{'service':<20} {'current':<50} {'newest in series':<22} {'pushed d':>8}  {'next series':<14} note")
    for svc, img, kind in find_images(files):
        if want and svc not in want:
            continue
        try:
            newest, age, hint, note = image_report(img)
        except Exception as e:
            newest, age, hint, note = "?", "?", "", f"lookup failed: {e}"
        if kind == "compose" and ":" not in img and f"{svc}/Dockerfile" in files:
            continue                        # our own build's local name, covered by its FROM line
        repo = img.split(":", 1)[0]
        other = sorted(r for r in running.get(svc, ()) if r.split(":", 1)[0] == repo and r != img)
        if kind == "compose" and other:
            note = (note + "; " if note else "") + f"running {','.join(other)} -> up -d"
        cur = img.split(":", 1)[1] if ":" in img else img
        mark = "" if newest in ("?", cur) else "  <-"
        print(f"{svc:<20} {img:<50} {newest:<22} {age:>8}  {hint:<14} {note}{mark}")

    print(f"\n{'service':<20} {'package':<26} {'current':<14} {'same major':<14} {'newest':<14} {'age d':>6} {'vulns':>5}")
    per_svc: dict[str, int] = {}
    for path, text in sorted(files.items()):
        svc, _, name = path.partition("/")
        if name != "requirements.txt" or (want and svc not in want):
            continue
        for pkg, op, ver in read_requirements(text):
            try:
                newest, age, same_major, vulns = pypi_report(pkg, ver)
            except Exception as e:
                print(f"{svc:<20} {pkg:<26} {ver:<14} lookup failed: {e}")
                continue
            current = vkey(newest) <= vkey(ver)
            if only_stale and current and vulns <= 0:
                continue
            per_svc[svc] = per_svc.get(svc, 0) + max(vulns, 0)
            shown_ver = ver if op == "==" else f"{op}{ver}"
            print(f"{svc:<20} {pkg:<26} {shown_ver:<14} {same_major:<14} {newest:<14} {age:>6} "
                  f"{('?' if vulns < 0 else vulns):>5}{'' if current else '  <-'}")
    return per_svc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("services", nargs="*")
    ap.add_argument("--only-stale", action="store_true")
    ap.add_argument("--local-only", action="store_true")
    a = ap.parse_args()
    want = set(a.services)

    print("Images: 'pushed d' = days since the registry last re-pushed that tag -- official images are\n"
          "rebuilt often, so it is NOT the release age; check the upstream release date before taking\n"
          f"it (policy: >= {MIN_AGE_DAYS} d). Packages: vulns = known advisories for the version we pin\n"
          "(PyPI/OSV). '<-' = something newer exists.")
    hosts = [("local (this repo)", local_files(), running_images(None))]
    for h in ([] if a.local_only else REMOTES):
        try:
            hosts.append((f"{h} (~/docker over ssh)", remote_files(h), running_images(h)))
        except Exception as e:
            print(f"\n{h}: unreachable over ssh ({e}) -- skipped")
    totals = {label: report_host(label, files, run, want, a.only_stale) for label, files, run in hosts}

    print("\nknown vulnerabilities in pinned packages:")
    for label, per_svc in totals.items():
        detail = ", ".join(f"{k} {v}" for k, v in sorted(per_svc.items()) if v) or "none"
        print(f"  {label:<26} {sum(per_svc.values()):>4}   ({detail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
