#!/usr/bin/env python3
"""Build, validate, smoke-test and publish the MatGUI demo queries.

The demo queries live as plain ``.rq`` files under ``demo-queries/``. Each
sub-directory becomes a workspace folder; ``demo-queries/workspace.json`` holds
the workspace IRI, the folder labels and the endpoint aliases.

Every query file starts with a small metadata header (``#+ key: value`` lines,
the same convention grlc uses)::

    #+ name: Operational points per country
    #+ description: Counts the operational points in RINF per member state.
    #+ endpoint: era

``name`` is required, ``endpoint`` is required unless the folder (or one of its
parents) defines a default endpoint. ``endpoint`` may be an alias defined in
``workspace.json`` or a full URL. The header lines are stripped from the query
text that is stored in the workspace.

The generated RDF follows the model used by MatGUI's SPARQL workspace backend
(yasgui:Workspace / yasgui:WorkspaceFolder / yasgui:ManagedQuery /
yasgui:ManagedQueryVersion), so the queries can be browsed in MatGUI's query
browser once the Turtle file is uploaded to the demo dataset.

Sub-commands:

    validate      parse all queries and check metadata (fails on errors)
    build         write the workspace as Turtle
    smoke-test    execute every query against its endpoint and report
    publish       replace the default graph of the demo dataset (Graph Store Protocol)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "demo-queries"
MANIFEST = QUERIES_DIR / "workspace.json"

USER_AGENT = "MatGUI-demo-queries/1.0 (https://github.com/Matdata-eu/matgui-matdata)"

HEADER_RE = re.compile(r"^#\+\s*([A-Za-z_-]+)\s*:\s*(.*?)\s*$")
QUERY_FORM_RE = re.compile(r"\b(SELECT|CONSTRUCT|ASK|DESCRIBE)\b", re.IGNORECASE)


@dataclass
class Folder:
    path: str  # e.g. "era-rinf/operational-points"
    label: str
    endpoint: str | None = None


@dataclass
class DemoQuery:
    file: Path
    folder_path: str  # "" for the workspace root
    name: str
    endpoint: str
    text: str
    description: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def rel_path(self) -> str:
        return self.file.relative_to(REPO_ROOT).as_posix()

    @property
    def slug(self) -> str:
        return self.file.relative_to(QUERIES_DIR).with_suffix("").as_posix()

    @property
    def query_form(self) -> str:
        # Strip comments, IRIs and strings so keywords inside them are ignored.
        cleaned = re.sub(r"#[^\n]*", "", self.text)
        cleaned = re.sub(r"<[^>\s]*>", "<>", cleaned)
        cleaned = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', cleaned)
        match = QUERY_FORM_RE.search(cleaned)
        return match.group(1).upper() if match else "SELECT"


# --------------------------------------------------------------------------- loading


def load_manifest() -> dict:
    with MANIFEST.open(encoding="utf-8") as fh:
        return json.load(fh)


def resolve_endpoint(value: str | None, aliases: dict) -> str | None:
    if not value:
        return None
    return aliases.get(value, value)


def load_folders(manifest: dict) -> dict[str, Folder]:
    aliases = manifest.get("endpoints", {})
    folders: dict[str, Folder] = {}
    for path, cfg in manifest.get("folders", {}).items():
        folders[path] = Folder(path=path, label=cfg["label"], endpoint=resolve_endpoint(cfg.get("endpoint"), aliases))
    return folders


def default_endpoint_for(folder_path: str, folders: dict[str, Folder]) -> str | None:
    parts = folder_path.split("/") if folder_path else []
    while parts:
        folder = folders.get("/".join(parts))
        if folder and folder.endpoint:
            return folder.endpoint
        parts.pop()
    return None


def parse_query_file(path: Path, folders: dict[str, Folder], aliases: dict) -> DemoQuery:
    lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    meta: dict[str, str] = {}
    body_start = 0
    for idx, line in enumerate(lines):
        match = HEADER_RE.match(line)
        if not match:
            body_start = idx
            break
        meta[match.group(1).lower()] = match.group(2)
    else:
        body_start = len(lines)

    text = "\n".join(lines[body_start:]).strip("\n") + "\n"
    folder_path = path.parent.relative_to(QUERIES_DIR).as_posix()
    if folder_path == ".":
        folder_path = ""

    endpoint = resolve_endpoint(meta.get("endpoint"), aliases) or default_endpoint_for(folder_path, folders)
    return DemoQuery(
        file=path,
        folder_path=folder_path,
        name=meta.get("name", "").strip(),
        endpoint=endpoint or "",
        text=text,
        description=meta.get("description") or None,
        meta=meta,
    )


def load_queries() -> tuple[dict, dict[str, Folder], list[DemoQuery]]:
    manifest = load_manifest()
    folders = load_folders(manifest)
    aliases = manifest.get("endpoints", {})
    files = sorted(p for p in QUERIES_DIR.rglob("*") if p.suffix in (".rq", ".sparql"))
    queries = [parse_query_file(p, folders, aliases) for p in files]
    return manifest, folders, queries


# --------------------------------------------------------------------------- validate


def strip_literals_and_iris(text: str) -> str:
    text = re.sub(r'"""[\s\S]*?"""', '""', text)
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", text)
    text = re.sub(r"<[^<>\s]*>", "<>", text)
    return re.sub(r"#[^\n]*", "", text)


def declared_prefixes(text: str) -> set[str]:
    return set(re.findall(r"PREFIX\s+([A-Za-z][\w.-]*)?:", text, re.IGNORECASE))


def used_prefixes(text: str) -> set[str]:
    # rdflib silently predeclares rdf:, rdfs:, xsd:, ... but most endpoints do not,
    # so require every prefix to be declared explicitly.
    body = re.sub(r"PREFIX\s+[\w.-]*:", "", strip_literals_and_iris(text), flags=re.IGNORECASE)
    return set(re.findall(r"(?<![\w?$:.-])([A-Za-z][\w-]*):", body))


def validate(args: argparse.Namespace) -> int:
    from rdflib.plugins.sparql import prepareQuery

    _, folders, queries = load_queries()
    errors: list[str] = []
    seen_labels: dict[tuple[str, str], str] = {}

    for q in queries:
        where = q.rel_path
        if not q.name:
            errors.append(f"{where}: missing '#+ name:' header")
        if not q.endpoint:
            errors.append(f"{where}: no endpoint (set '#+ endpoint:' or a folder default)")
        elif not re.match(r"^https?://", q.endpoint):
            errors.append(f"{where}: endpoint '{q.endpoint}' is not a URL or known alias")
        unknown = set(q.meta) - {"name", "description", "endpoint"}
        if unknown:
            errors.append(f"{where}: unknown header keys {sorted(unknown)}")

        # Every directory on the path must have a label in workspace.json
        parts = q.folder_path.split("/") if q.folder_path else []
        for i in range(len(parts)):
            sub = "/".join(parts[: i + 1])
            if sub not in folders:
                errors.append(f"{where}: folder '{sub}' has no entry in workspace.json")

        key = (q.folder_path, q.name.lower())
        if q.name and key in seen_labels:
            errors.append(f"{where}: duplicate name '{q.name}' in folder (also {seen_labels[key]})")
        seen_labels[key] = where

        undeclared = sorted(used_prefixes(q.text) - declared_prefixes(q.text))
        if undeclared:
            errors.append(f"{where}: undeclared prefix(es) {undeclared}")

        try:
            prepareQuery(q.text)
        except Exception as exc:  # rdflib raises a variety of parse errors
            errors.append(f"{where}: SPARQL syntax error: {str(exc).splitlines()[0]}")

    for err in errors:
        print(f"::error::{err}" if os.environ.get("GITHUB_ACTIONS") else f"ERROR {err}")

    print(f"Validated {len(queries)} queries in {len({q.folder_path for q in queries})} folders: "
          f"{len(errors)} error(s)")
    return 1 if errors else 0


# --------------------------------------------------------------------------- build


def folder_iri(workspace_iri: str, folder_path: str) -> str:
    # Must match mintFolderIri() in MatGUI's SparqlWorkspaceBackend.
    return f"{workspace_iri.rstrip('/')}/folder/{quote(folder_path, safe='')}"


def query_iri(workspace_iri: str, q: DemoQuery) -> str:
    return f"{workspace_iri.rstrip('/')}/query/{quote(q.slug, safe='/')}"


def version_iri(workspace_iri: str, q: DemoQuery) -> str:
    digest = hashlib.sha256(f"{q.slug}\n{q.endpoint}\n{q.text}".encode("utf-8")).hexdigest()[:16]
    return f"{workspace_iri.strip()}_mq_v_{digest}"


def git_timestamp(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def build_graph(manifest: dict, folders: dict[str, Folder], queries: list[DemoQuery]):
    from rdflib import BNode, Graph, Literal, Namespace, URIRef
    from rdflib.namespace import DCTERMS, PROV, RDF, RDFS, SKOS, XSD

    YASGUI = Namespace("https://matdata.eu/ns/yasgui#")
    SPIN = Namespace("http://spinrdf.org/spin#")
    SD = Namespace("http://www.w3.org/ns/sparql-service-description#")

    g = Graph()
    for prefix, ns in {
        "yasgui": YASGUI, "spin": SPIN, "sd": SD, "dcterms": DCTERMS,
        "prov": PROV, "skos": SKOS, "rdfs": RDFS, "xsd": XSD,
    }.items():
        g.bind(prefix, ns)

    ws_iri = manifest["workspaceIri"]
    ws = URIRef(ws_iri)
    g.add((ws, RDF.type, YASGUI.Workspace))
    g.add((ws, RDFS.label, Literal(manifest.get("label", "MatGUI demo queries"))))
    if manifest.get("description"):
        g.add((ws, DCTERMS.description, Literal(manifest["description"])))

    used_folders = {q.folder_path for q in queries if q.folder_path}
    all_paths = set()
    for path in used_folders:
        parts = path.split("/")
        all_paths.update("/".join(parts[: i + 1]) for i in range(len(parts)))

    for path in sorted(all_paths):
        f = URIRef(folder_iri(ws_iri, path))
        g.add((f, RDF.type, YASGUI.WorkspaceFolder))
        g.add((f, SKOS.inScheme, ws))
        g.add((f, RDFS.label, Literal(folders[path].label)))
        if "/" in path:
            g.add((f, SKOS.broader, URIRef(folder_iri(ws_iri, path.rsplit("/", 1)[0]))))

    source_base = manifest.get("sourceBaseUrl")
    fallback_now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    services: dict[str, URIRef | BNode] = {}

    for q in queries:
        mq = URIRef(query_iri(ws_iri, q))
        container = URIRef(folder_iri(ws_iri, q.folder_path)) if q.folder_path else ws
        g.add((mq, RDF.type, YASGUI.ManagedQuery))
        g.add((mq, RDFS.label, Literal(q.name)))
        g.add((mq, DCTERMS.isPartOf, container))
        if source_base:
            g.add((mq, DCTERMS.source, URIRef(f"{source_base.rstrip('/')}/{quote(q.rel_path)}")))

        v = URIRef(version_iri(ws_iri, q))
        g.add((v, RDF.type, YASGUI.ManagedQueryVersion))
        g.add((v, DCTERMS.isVersionOf, mq))
        g.add((v, DCTERMS.created, Literal(git_timestamp(q.file) or fallback_now, datatype=XSD.dateTime)))
        g.add((v, SPIN.text, Literal(q.text)))
        if q.description:
            g.add((v, DCTERMS.description, Literal(q.description)))

        svc = services.get(q.endpoint)
        if svc is None:
            svc = URIRef(f"{ws_iri.rstrip('/')}/service/{hashlib.sha256(q.endpoint.encode()).hexdigest()[:12]}")
            services[q.endpoint] = svc
            g.add((svc, RDF.type, SD.Service))
            g.add((svc, SD.endpoint, URIRef(q.endpoint)))
        g.add((v, PROV.used, svc))

    return g


def build(args: argparse.Namespace) -> int:
    manifest, folders, queries = load_queries()
    g = build_graph(manifest, folders, queries)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(out), format="turtle", encoding="utf-8")
    print(f"Wrote {len(g)} triples for {len(queries)} queries to {out}")
    return 0


# --------------------------------------------------------------------------- smoke test


def run_query(endpoint: str, text: str, form: str, timeout: int) -> tuple[bool, str, float]:
    accept = {
        "SELECT": "application/sparql-results+json",
        "ASK": "application/sparql-results+json",
    }.get(form, "application/n-triples, text/turtle;q=0.9")
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    encoded = urllib.parse.urlencode({"query": text})
    start = time.monotonic()

    def send(url: str, method: str):
        if method == "GET":
            sep = "&" if "?" in url else "?"
            req = urllib.request.Request(f"{url}{sep}{encoded}", method="GET", headers=headers)
        else:
            req = urllib.request.Request(url, data=encoded.encode("utf-8"), method="POST", headers={
                **headers, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            })
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read(), res.headers.get("Content-Type", "")

    url, method, note = endpoint, "POST", ""
    for _ in range(3):
        try:
            body, ctype = send(url, method)
            break
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code in (301, 302, 303, 307, 308) and location:
                # urllib does not re-POST on 307/308; follow manually and report the new URL
                url = urllib.parse.urljoin(url, location)
                note += f" (redirected to {url})"
                continue
            if exc.code in (403, 405) and method == "POST":
                method, note = "GET", note + " (via GET)"
                continue
            detail = exc.read().decode("utf-8", "replace").strip().splitlines()
            reason = (detail[0] if detail else str(exc.reason))[:200]
            return False, f"HTTP {exc.code}: {reason}{note}", time.monotonic() - start
        except Exception as exc:  # timeouts, DNS, TLS, ...
            return False, f"{type(exc).__name__}: {exc}"[:200] + note, time.monotonic() - start
    else:
        return False, f"too many redirects{note}", time.monotonic() - start
    elapsed = time.monotonic() - start

    if form in ("SELECT", "ASK"):
        try:
            parsed = json.loads(body)
        except ValueError:
            return False, f"non-JSON response ({ctype})", elapsed
        if form == "ASK":
            return True, f"boolean={parsed.get('boolean')}{note}", elapsed
        rows = len(parsed.get("results", {}).get("bindings", []))
        return rows > 0, f"{rows} rows{note}", elapsed

    triples = sum(1 for line in body.decode("utf-8", "replace").splitlines()
                  if line.strip() and not line.lstrip().startswith(("@prefix", "PREFIX", "#")))
    return triples > 0, f"~{triples} lines of RDF{note}", elapsed


def smoke_test(args: argparse.Namespace) -> int:
    _, _, queries = load_queries()
    if args.only:
        queries = [q for q in queries if any(s in q.rel_path for s in args.only)]

    results = []
    for q in queries:
        ok, detail, elapsed = run_query(q.endpoint, q.text, q.query_form, args.timeout)
        status = "ok" if ok else ("empty" if detail.startswith(("0 rows", "~0 ")) else "FAIL")
        results.append((q, status, detail, elapsed))
        print(f"[{status:5}] {elapsed:6.1f}s {q.rel_path} — {detail}", flush=True)

    counts = {s: sum(1 for r in results if r[1] == s) for s in ("ok", "empty", "FAIL")}
    summary = [
        "## Demo query smoke test",
        "",
        f"{counts['ok']} ok · {counts['empty']} empty result · {counts['FAIL']} failed (of {len(results)})",
        "",
        "| Status | Query | Endpoint | Result | Time |",
        "|---|---|---|---|---|",
    ]
    order = {"FAIL": 0, "empty": 1, "ok": 2}
    for q, status, detail, elapsed in sorted(results, key=lambda r: (order[r[1]], r[0].rel_path)):
        icon = {"ok": "✅", "empty": "⚠️", "FAIL": "❌"}[status]
        detail_md = detail.replace("|", "\\|")
        summary.append(f"| {icon} | `{q.slug}` | {urllib.parse.urlparse(q.endpoint).netloc} | {detail_md} | {elapsed:.1f}s |")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    else:
        print("\n".join(summary))

    if args.strict and counts["FAIL"]:
        return 1
    return 0


# --------------------------------------------------------------------------- publish


def http(method: str, url: str, body: bytes | None, headers: dict, timeout: int = 120) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def publish(args: argparse.Namespace) -> int:
    dataset = args.dataset.rstrip("/")
    turtle = Path(args.file).read_bytes()

    headers = {"Content-Type": "text/turtle; charset=utf-8"}
    user, password = os.environ.get("JENA_USERNAME"), os.environ.get("JENA_PASSWORD")
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    # Graph Store Protocol: PUT replaces the default graph, which is where MatGUI's
    # SPARQL workspace backend reads and writes its data.
    status, body = http("PUT", f"{dataset}/data?default", turtle, headers)
    if status >= 300:
        print(f"::error::Upload failed with HTTP {status}: {body[:500]}")
        return 1
    print(f"Uploaded {len(turtle)} bytes to {dataset} (HTTP {status})")

    count_query = (
        "SELECT (COUNT(?q) AS ?n) WHERE { ?q a <https://matdata.eu/ns/yasgui#ManagedQuery> }"
    )
    status, body = http(
        "POST", f"{dataset}/sparql",
        urllib.parse.urlencode({"query": count_query}).encode(),
        {"Accept": "application/sparql-results+json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    if status >= 300:
        print(f"::warning::Could not verify upload (HTTP {status}): {body[:300]}")
        return 0
    n = json.loads(body)["results"]["bindings"][0]["n"]["value"]
    print(f"Dataset now contains {n} managed queries")
    return 0


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="parse queries and check metadata").set_defaults(func=validate)

    p_build = sub.add_parser("build", help="write the workspace as Turtle")
    p_build.add_argument("--out", default=str(REPO_ROOT / "build" / "matgui-demo.ttl"))
    p_build.set_defaults(func=build)

    p_smoke = sub.add_parser("smoke-test", help="run every query against its endpoint")
    p_smoke.add_argument("--timeout", type=int, default=90)
    p_smoke.add_argument("--strict", action="store_true", help="exit non-zero when a query fails")
    p_smoke.add_argument("--only", nargs="*", help="only run queries whose path contains one of these strings")
    p_smoke.set_defaults(func=smoke_test)

    p_pub = sub.add_parser("publish", help="upload the Turtle file to the demo dataset")
    p_pub.add_argument("--dataset", default=os.environ.get("JENA_DATASET_URL", "https://jena.matdata.eu/matgui-demo"))
    p_pub.add_argument("--file", default=str(REPO_ROOT / "build" / "matgui-demo.ttl"))
    p_pub.set_defaults(func=publish)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
