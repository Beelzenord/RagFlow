"""Ask the evaluation's questions and score the answers.

    python3 eval/run.py                      # all cases, against the local stack
    python3 eval/run.py --seed               # upload missing test documents, tag them for the run
    python3 eval/run.py --only anchor-day-se --repeat 3
    python3 eval/run.py --compare eval/reports/<earlier>.json
    python3 eval/run.py --rescore eval/reports/<earlier>.json   # new scoring, saved answers

Talks to the query service directly (POST /query, as the chat asks) with the
SERVICE_API_KEY from .env, which is never printed. Standard library only. Each
run costs a few cents of LLM time and writes eval/reports/<time>.json.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from scoring import score, summarise  # noqa: E402

REPORTS = HERE / "reports"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


# --- Talking to the services ----------------------------------------------------

def service_key() -> str:
    if os.environ.get("SERVICE_API_KEY"):
        return os.environ["SERVICE_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("SERVICE_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def request(method: str, url: str, key: str, body: dict | None = None, timeout: float = 120,
            data: bytes | None = None, content_type: str | None = None) -> tuple[int, object]:
    headers = {"x-api-key": key} if key else {}
    if body is not None:
        data = json.dumps(body).encode()
        content_type = "application/json"
    if content_type:
        headers["content-type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode(errors="replace")


def multipart(path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


# --- The test documents ------------------------------------------------------------

def expected_audience(spec) -> dict:
    if spec == "global":
        return {"everyone": True, "countries": [], "groups": []}
    return {"everyone": False, "countries": sorted(spec), "groups": []}


def current_audience(doc: dict) -> dict:
    a = doc.get("audience") or {}
    return {
        "everyone": bool(a.get("everyone")),
        "countries": sorted(c.get("code") for c in a.get("countries") or []),
        "groups": sorted(g.get("id") for g in a.get("groups") or []),
    }


def find_document(ingestion: str, key: str, filename: str) -> dict | None:
    q = urllib.parse.urlencode({"q": filename, "limit": 50})
    status, data = request("GET", f"{ingestion}/documents?{q}", key)
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"could not list documents from {ingestion} (HTTP {status})")
    matches = [d for d in data.get("documents", []) if d.get("original_filename") == filename]
    # The newest indexed copy, if there are several.
    matches.sort(key=lambda d: (d.get("status") in ("completed", "degraded"), d.get("created_at") or ""), reverse=True)
    return matches[0] if matches else None


def preflight(ingestion: str, key: str, fixtures: dict) -> list[str]:
    problems = []
    for filename, spec in fixtures["documents"].items():
        doc = find_document(ingestion, key, filename)
        if not doc:
            problems.append(f"{filename}: not uploaded")
        elif doc.get("status") not in ("completed", "degraded"):
            problems.append(f"{filename}: status {doc.get('status')}")
        elif current_audience(doc) != expected_audience(spec):
            problems.append(f"{filename}: tagged {current_audience(doc)}, expected {expected_audience(spec)}")
    return problems


def seed(ingestion: str, key: str, fixtures: dict) -> list[tuple[str, str, dict]]:
    """Upload the missing test documents and give every one its expected tags.

    Returns (id, filename, tags before) for each document whose tags it changed,
    so they can be put back after the run - someone may have tagged their copy
    differently on purpose.
    """
    folder = ROOT / fixtures["folder"]
    changed: list[tuple[str, str, dict]] = []
    codes = sorted({c for spec in fixtures["documents"].values() if spec != "global" for c in spec})
    for code in codes:
        status, data = request("POST", f"{ingestion}/markets/countries", key, {"code": code})
        if status != 200:
            raise SystemExit(f"could not add market {code}: HTTP {status} {data}")
    for filename, spec in fixtures["documents"].items():
        doc = find_document(ingestion, key, filename)
        if not doc:
            body, ctype = multipart(folder / filename)
            status, data = request("POST", f"{ingestion}/ingest", key, data=body, content_type=ctype, timeout=300)
            if status != 202:
                raise SystemExit(f"upload of {filename} failed: HTTP {status} {data}")
            print(f"  uploaded {filename}")
            doc_id = data["document_id"]
            for _ in range(120):
                _, doc = request("GET", f"{ingestion}/documents/{doc_id}", key)
                if isinstance(doc, dict) and doc.get("status") in ("completed", "degraded", "failed"):
                    break
                time.sleep(3)
            if not isinstance(doc, dict) or doc.get("status") == "failed":
                raise SystemExit(f"{filename} did not index: {doc}")
        before = current_audience(doc)
        if before != expected_audience(spec):
            status, data = request("PUT", f"{ingestion}/documents/{doc['id']}/audience", key, expected_audience(spec))
            if status != 200:
                restore(ingestion, key, changed)
                raise SystemExit(f"could not tag {filename}: HTTP {status} {data}")
            changed.append((doc["id"], filename, before))
            print(f"  tagged {filename} as {spec} for this run")
    return changed


def restore(ingestion: str, key: str, changed: list[tuple[str, str, dict]]) -> None:
    """Put back the tags seed() replaced. A tag that can no longer be restored
    (its group deleted meanwhile) is reported, with what it was."""
    for doc_id, filename, before in changed:
        status, data = request("PUT", f"{ingestion}/documents/{doc_id}/audience", key, before)
        if status == 200:
            print(f"  restored the tags on {filename}")
        else:
            print(f"  COULD NOT restore {filename} to {before}: HTTP {status} {data}")


# --- Asking and reporting ----------------------------------------------------------

def ask(query: str, key: str, case: dict) -> dict:
    # Test questions are not what people ask: kept out of the question log.
    body = {"question": case["question"], "log": False}
    if case.get("market"):
        body["market"] = case["market"]
    started = time.monotonic()
    status, data = request("POST", f"{query}/query", key, body)
    took = round(time.monotonic() - started, 2)
    if status != 200 or not isinstance(data, dict):
        return {"error": f"HTTP {status}: {data}", "answer": "", "cited": [], "seconds": took}
    cited = [c.get("filename") for c in data.get("citations") or []]
    return {"answer": data.get("answer") or "", "cited": cited, "seconds": took,
            "search_query": data.get("search_query")}


def rescore(path: Path) -> int:
    """For a change to the scoring or to a case's patterns: no questions asked,
    no cost, no tags touched."""
    old = json.loads(path.read_text())
    cases = {c["id"]: c for c in json.loads((HERE / "cases.json").read_text())["cases"]}
    results = []
    for r in old["results"]:
        case = cases.get(r["id"])
        if not case:
            continue
        verdict = score(case, r.get("answer", ""), r.get("cited", [])) if not r.get("error") else {
            "passed": False, "checks": {}, "reasons": [r["error"]]}
        results.append({**r, **verdict})
        if not verdict["passed"]:
            print(f"FAIL  {r['id']:<24} - " + "; ".join(verdict["reasons"]))
    summary = summarise(results)
    print("\n" + "  ".join(f"{name}: {rate(part)}" for name, part in summary.items()))
    out = path.resolve().with_name(path.stem + "-rescored.json")
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    print(f"Report: {out.relative_to(ROOT)}")
    return 0


def rate(part: dict) -> str:
    return f"{part['passed']}/{part['total']}" + (f" ({100 * part['passed'] // part['total']}%)" if part["total"] else "")


def compare(old_path: Path, results: list[dict]) -> None:
    old = json.loads(old_path.read_text())
    before = {r["id"]: r["passed"] for r in old["results"]}
    fixed = [r["id"] for r in results if r["passed"] and before.get(r["id"]) is False]
    broke = [r["id"] for r in results if not r["passed"] and before.get(r["id"]) is True]
    print(f"\nCompared with {old_path.name} ({old['summary']['cases']['passed']}/{old['summary']['cases']['total']} then):")
    print(f"  now passing: {', '.join(fixed) or 'none'}")
    print(f"  now failing: {', '.join(broke) or 'none'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query-url", default=os.environ.get("EVAL_QUERY_URL", "http://localhost:8002"))
    ap.add_argument("--ingestion-url", default=os.environ.get("EVAL_INGESTION_URL", "http://localhost:8001"))
    ap.add_argument("--seed", action="store_true",
                    help="upload missing test documents and tag them for the run (localhost only)")
    ap.add_argument("--keep-tags", action="store_true",
                    help="with --seed: leave the expected tags in place instead of restoring the old ones")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--repeat", type=int, default=1, help="ask each case this many times")
    ap.add_argument("--compare", type=Path, help="an earlier report to compare with")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--rescore", type=Path,
                    help="score a saved report's answers again with the current cases and scoring - asks nothing")
    args = ap.parse_args()
    if args.rescore:
        return rescore(args.rescore)

    key = service_key()
    cases = json.loads((HERE / "cases.json").read_text())["cases"]
    fixtures = json.loads((HERE / "fixtures.json").read_text())
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    changed: list[tuple[str, str, dict]] = []
    if args.seed:
        hosts = {urllib.parse.urlparse(u).hostname for u in (args.query_url, args.ingestion_url)}
        if not hosts <= LOCAL_HOSTS:
            raise SystemExit("--seed only uploads to a local stack; refusing " + ", ".join(sorted(hosts)))
        changed = seed(args.ingestion_url, key, fixtures)
    try:
        if not args.skip_preflight:
            problems = preflight(args.ingestion_url, key, fixtures)
            if problems:
                print("The test documents are not ready:\n  " + "\n  ".join(problems))
                print("Run with --seed to upload them and tag them for the run (local stack only).")
                return 2
        return run_cases(args, key, cases)
    finally:
        # Even after Ctrl-C: tags someone chose on purpose come back.
        if changed and not args.keep_tags:
            restore(args.ingestion_url, key, changed)


def run_cases(args: argparse.Namespace, key: str, cases: list[dict]) -> int:
    results = []
    for case in cases:
        for attempt in range(args.repeat):
            got = ask(args.query_url, key, case)
            verdict = score(case, got["answer"], got["cited"]) if "error" not in got else {
                "passed": False, "checks": {}, "reasons": [got["error"]]}
            results.append({"id": case["id"], "attempt": attempt + 1, **verdict, **got})
            mark = "PASS" if verdict["passed"] else "FAIL"
            why = "" if verdict["passed"] else "  - " + "; ".join(verdict["reasons"])
            print(f"{mark}  {case['id']:<24} {got['seconds']:>5}s{why}")

    summary = summarise(results)
    print("\n" + "  ".join(f"{name}: {rate(part)}" for name, part in summary.items()))

    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1))
    print(f"Report: {path.relative_to(ROOT)}")
    if args.compare:
        compare(args.compare, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
