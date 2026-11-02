#!/usr/bin/env python3
"""check_updates.py — report what could be updated, change nothing.

For every service on every host: the base image (Dockerfile FROM / compose image:) against the
newest tag in the same series (Docker Hub, or ghcr.io), and every pinned pip package against PyPI --
newest release, the version to TAKE (newest in our major that is at least MIN_AGE_DAYS old), and the
known vulnerabilities (PyPI's OSV data) of the version we run and of the one to take. Hosts: this repo on the local machine, plus every remote in CHECK_UPDATES_REMOTES
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

Runs weekly from the pi user's crontab (Monday 07:30) and mails the report; it never updates
anything itself -- updates stay manual, one service at a time.

Needs internet (Docker Hub + PyPI), so it is not part of the test suite.
"""
import os
import re
import sys
import subprocess
import json
import pathlib
import argparse
import io
import contextlib
import urllib.request
from datetime import datetime, timezone

ROOT         = pathlib.Path(__file__).resolve().parent.parent
MIN_AGE_DAYS = 14
TIMEOUT_S    = 20
REMOTES      = os.environ.get("CHECK_UPDATES_REMOTES", "pi5").split()
FILE_GLOBS   = ("*/Dockerfile", "*/docker-compose*.yml", "*/requirements.txt", "*/test_*pub*.py")
_cache: dict = {}
TODO: dict[str, dict[str, dict]] = {}   # host label -> service -> what to change (summary + commands)


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


def pypi_report(name: str, version: str) -> tuple[str, str, str, int, int]:
    """(newest stable, its age, TAKE = newest in our major that is >= MIN_AGE_DAYS old,
    known vulns in our version, known vulns in TAKE)."""
    d = get_json(f"https://pypi.org/pypi/{name}/json")
    stable = {v: files for v, files in d["releases"].items()
              if files and re.fullmatch(r"[\d.]+(\.post\d+)?", v)
              and not all(f.get("yanked") for f in files)}
    newest = max(stable, key=vkey) if stable else d["info"]["version"]
    files = d["releases"].get(newest) or [{}]
    major = version.split(".")[0]

    def old_enough(v):
        a = age_days(stable[v][0].get("upload_time_iso_8601"))
        return a != "?" and int(a) >= MIN_AGE_DAYS
    take = max((v for v in stable if v.split(".")[0] == major and old_enough(v)), key=vkey, default="")
    if take and vkey(take) < vkey(version):
        take = version                      # never suggest a downgrade

    def vulns_of(v):
        try:
            return len(get_json(f"https://pypi.org/pypi/{name}/{v}/json").get("vulnerabilities") or [])
        except Exception:
            return -1
    return (newest, age_days(files[0].get("upload_time_iso_8601")), take, vulns_of(version),
            vulns_of(take) if take and take != version else vulns_of(version))


def python_take(tag: str) -> str:
    """For a python:3.X.Y-<suffix> image: the newest 3.X.Z released >= MIN_AGE_DAYS ago (python.org dates)."""
    m = re.fullmatch(r"(3\.\d+)\.(\d+)(-.+)?", tag)
    if not m:
        return ""
    minor, suffix = m.group(1), m.group(3) or ""
    rel = get_json("https://www.python.org/api/v2/downloads/release/?is_published=true")
    best = None
    for r in rel:
        mm = re.fullmatch(r"Python " + re.escape(minor) + r"\.(\d+)", r.get("name", ""))
        if mm and r.get("release_date") and int(age_days(r["release_date"])) >= MIN_AGE_DAYS:
            n = int(mm.group(1))
            best = n if best is None or n > best else best
    if best is None or best < int(m.group(2)):
        return tag
    return f"{minor}.{best}{suffix}"


def _todo(label: str, svc: str) -> dict:
    return TODO.setdefault(label, {}).setdefault(
        svc, {"lines": [], "from": [], "compose": [], "req": [], "redeploy": False})


def update_commands(host: str | None, svc: str, t: dict, files: dict[str, str]) -> list[str]:
    """Shell steps that apply this service's update the controlled way: bump the pins, keep the
    old image as :prev, rebuild, run the service's tests INSIDE the new image, then up -d."""
    base = "/home/pi/docker" if host is None else "~/docker"
    out = [f"ssh {host}"] if host else []
    out.append(f"cd {base}/{svc}")
    if any("mariadb" in n for _, n in t["compose"]) or (t["redeploy"] and "mariadb" in " ".join(t["lines"])):
        out.append("# FIRST a fresh dump: docker exec <db-container> mariadb-dump --all-databases "
                   "-uroot -p\"$ROOT_PW\" > ~/dump_$(date +%F).sql")
    if t["from"]:
        out.append("sed -i " + " ".join(f"-e 's|^FROM {o}$|FROM {n}|'" for o, n in t["from"]) + " Dockerfile")
    if t["req"]:
        out.append("sed -i " + " ".join(f"-e 's|^{p}=={o}$|{p}=={n}|'" for p, o, n in t["req"]) + " requirements.txt")
    if t["compose"]:
        out.append("sed -i " + " ".join(f"-e 's|image: {o}$|image: {n}|'" for o, n in t["compose"])
                   + " docker-compose.yml")
    compose = next((txt for pth, txt in files.items()
                    if pth.startswith(svc + "/docker-compose") and pth.endswith(".yml")), "")
    prof = re.search(r"profiles:\s*\[\s*\"?([\w-]+)", compose)
    dc = f"docker compose --profile {prof.group(1)}" if prof else "docker compose"
    if t["from"] or t["req"]:
        out.append(f'img=$({dc} config --images | head -1); docker tag "$img" "$img:prev"')
        out.append(f"{dc} build --no-cache")
        tests = sorted(p.partition("/")[2][:-3] for p in files
                       if p.startswith(svc + "/") and re.fullmatch(r"test_.*pub.*\.py", p.partition("/")[2]))
        for mod in tests:
            out.append(f'docker run --rm --entrypoint python3 -v "$PWD":/app -v "$PWD/../common":/app/common:ro '
                       f'-w /app "$img" -m unittest {mod}')
        out.append("# manual-only service (compose profile): build only, do NOT start it" if prof
                   else "docker compose up -d")
    elif t["compose"] or t["redeploy"]:
        out.append("docker compose pull && docker compose up -d")
    own_build = f"{svc}/Dockerfile" in files
    out.append("tail -f logs/debug_$(date +%F).log   # watch the first cycle" if own_build
               else "docker compose logs -f --tail 50   # watch it come up")
    out.append("# rollback: git revert, then " + ("docker tag \"$img:prev\" \"$img\" && docker compose up -d"
                                                if own_build else "docker compose up -d"))
    out.append("git commit -am '" + svc + ": update' && git push" if host
               else "cd .. && ./test-all.sh   # then commit (bundled)")
    return out


def report_host(label: str, files: dict[str, str], running: dict[str, set[str]],
                want: set[str], only_stale: bool) -> dict[str, int]:
    print(f"\n{'=' * 100}\n{label}\n{'=' * 100}\n")
    print(f"{'service':<20} {'current':<50} {'newest in series':<22} {'take (>=14 d)':<20} {'pushed d':>8}  {'next series':<14} note")
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
        take = ""
        if img.startswith("python:"):
            try:
                take = python_take(cur)
            except Exception:
                take = "?"
        mark = "" if newest in ("?", cur) else "  <-"
        if take and take not in ("?", cur):
            t = _todo(label, svc)
            t["lines"].append(f"python:{cur} -> python:{take}")
            t["from" if kind == "FROM" else "compose"].append((img, f"python:{take}"))
        elif not img.startswith("python:") and newest not in ("?", cur) and "pin it" not in note:
            t = _todo(label, svc)
            t["lines"].append(f"{img} -> {newest}  (check the release date first)")
            t["from" if kind == "FROM" else "compose"].append((img, f"{repo}:{newest}"))
        if "pin it" in note:
            _todo(label, svc)["lines"].append(f"{img} -- {note}")
        elif "-> up -d" in note:
            t = _todo(label, svc)
            t["lines"].append(f"{img} -- {note}")
            t["redeploy"] = True
        print(f"{svc:<20} {img:<50} {newest:<22} {take:<20} {age:>8}  {hint:<14} {note}{mark}")

    print(f"\n{'service':<20} {'package':<26} {'current':<14} {'vulns':>5}  {'take (>=14 d)':<14} {'vulns':>5}  {'newest':<14} {'age d':>6}")
    per_svc: dict[str, int] = {}
    for path, text in sorted(files.items()):
        svc, _, name = path.partition("/")
        if name != "requirements.txt" or (want and svc not in want):
            continue
        for pkg, op, ver in read_requirements(text):
            try:
                newest, age, take, vulns, vulns_take = pypi_report(pkg, ver)
            except Exception as e:
                print(f"{svc:<20} {pkg:<26} {ver:<14} lookup failed: {e}")
                continue
            current = vkey(newest) <= vkey(ver)
            if only_stale and current and vulns <= 0:
                continue
            per_svc[svc] = per_svc.get(svc, 0) + max(vulns, 0)
            if take and take != ver and op == "==":
                t = _todo(label, svc)
                minor = ver.split(".")[:2] != take.split(".")[:2] and not re.fullmatch(r"20\d\d\..*", ver)
                t["lines"].append(f"{pkg} {ver} -> {take}" + (f"  ({vulns} vulns now)" if vulns > 0 else "")
                                  + ("  [MINOR bump: deliberate, read the changelog]" if minor and vulns <= 0 else ""))
                t["req"].append((pkg, ver, take))
            shown_ver = ver if op == "==" else f"{op}{ver}"
            q = lambda n: "?" if n < 0 else n
            print(f"{svc:<20} {pkg:<26} {shown_ver:<14} {q(vulns):>5}  {take:<14} {q(vulns_take):>5}  "
                  f"{newest:<14} {age:>6}{'' if current else '  <-'}")
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
    hosts = [("local (this repo)", local_files(), running_images(None), None)]
    for h in ([] if a.local_only else REMOTES):
        try:
            hosts.append((f"{h} (~/docker over ssh)", remote_files(h), running_images(h), h))
        except Exception as e:
            print(f"\n{h}: unreachable over ssh ({e}) -- skipped")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        totals = {label: report_host(label, files, run, want, a.only_stale) for label, files, run, _ in hosts}

    # summary first: the weekly mail should say at a glance whether anything needs doing
    print(f"\nSUMMARY ({datetime.now():%Y-%m-%d}) -- versions to take are >= {MIN_AGE_DAYS} d old, same major")
    for label, files, _, host in hosts:
        per_svc, svcs = totals[label], TODO.get(label, {})
        n = sum(len(t["lines"]) for t in svcs.values())
        print(f"\n  {label}: {n} to take in {len(svcs)} services, "
              f"{sum(per_svc.values())} known vulnerabilities in pinned packages")
        for svc, t in svcs.items():
            print(f"\n    {svc}")
            for line in t["lines"]:
                print(f"      - {line}")
            if t["from"] or t["req"] or t["compose"] or t["redeploy"]:
                print("      commands:")
                for c in update_commands(host, svc, t, files):
                    print(f"        {c}")
    print("\n  Order and policy: one service at a time, low-risk readers first; battery/inverter control on a\n"
          "  calm day; take a mariadb-dump before any mariadb update; minor/major bumps only deliberately.")
    print(buf.getvalue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
