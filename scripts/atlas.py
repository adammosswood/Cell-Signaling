"""Validate evidence records, apply reviewed changes, and build the offline atlas."""
import argparse
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
GRAPH = Path("data/pilot.json")
COLLECTIONS = {
    "papers": "id title year doi pmid url status",
    "nodes": "id label kind compartment summary",
    "edges": "id source target effect tier status summary evidence reason assumption missing_link test",
    "assays": "id node label modality measures validation controls limit evidence",
    "responses": "id node level summary evidence",
    "coverage": "id scope status summary evidence",
}
EVIDENCE = "paper locator context perturbation readout finding"
VALUES = {
    ("papers", "status"): "published preprint corrected retracted concern superseded notice",
    ("nodes", "kind"): "receptor hub junction messenger endpoint",
    ("edges", "effect"): "activation inhibition recruitment production depletion feedback",
    ("edges", "tier"): "demonstrated assembled hypothesis",
    ("edges", "status"): "active contested retired",
    ("responses", "level"): "cellular physiological",
    ("coverage", "status"): "pending reviewed no_qualifying_evidence no_effect",
}
MODALITIES = {"FRET", "BRET", "Single fluorophore", "Biochemical", "Imaging", "Electrophysiology"}
ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DOI = re.compile(r"10\.\d{4,9}/\S+\Z", re.I)
BLOCKED_SUPPORT = {"retracted", "concern", "superseded", "notice"}


class Invalid(ValueError):
    """Input cannot be published."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fields(record, names, label):
    require(isinstance(record, dict) and set(record) == set(names.split()), f"{label}: unexpected or missing fields")


def valid_url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (ValueError, TypeError):
        return False


def evidence_supports(evidence, papers):
    return any(papers[item["paper"]]["status"] not in BLOCKED_SUPPORT for item in evidence)


def validate(graph):
    fields(graph, " ".join(COLLECTIONS), "Graph")
    index = {}
    for name, keys in COLLECTIONS.items():
        records = graph[name]
        require(isinstance(records, list), f"{name}: expected array")
        index[name] = {}
        for record in records:
            fields(record, keys, name)
            ident = record["id"]
            require(isinstance(ident, str) and ID.fullmatch(ident), f"{name}: invalid ID")
            require(ident not in index[name], f"{name}: duplicate {ident}")
            index[name][ident] = record
            for key, value in record.items():
                if key in {"year", "evidence"}:
                    continue
                require(isinstance(value, str) and len(value) <= 3000, f"{ident}.{key}: invalid text")
                if key not in {"doi", "pmid", "reason", "assumption", "missing_link", "test"}:
                    require(value.strip(), f"{ident}.{key}: empty")
                if (name, key) in VALUES:
                    require(value in VALUES[name, key].split(), f"{ident}.{key}: unexpected value")
    papers, nodes = index["papers"], index["nodes"]
    identifiers = set()
    for paper in papers.values():
        ident = paper["id"]
        require(type(paper["year"]) is int and 1600 <= paper["year"] <= date.today().year + 1, f"{ident}: invalid year")
        doi, pmid = paper["doi"], paper["pmid"]
        require((doi or pmid) and (not doi or DOI.fullmatch(doi)) and (not pmid or pmid.isascii() and pmid.isdecimal()), f"{ident}: invalid DOI or PMID")
        require(valid_url(paper["url"]), f"{ident}: invalid URL")
        for key, value in (("doi", doi.lower()), ("pmid", pmid)):
            if value:
                require((key, value) not in identifiers, f"{ident}: duplicate {key}")
                identifiers.add((key, value))
    for name in ("edges", "assays", "responses", "coverage"):
        for record in graph[name]:
            ident, evidence = record["id"], record["evidence"]
            required = name != "coverage" or record["status"] == "no_effect"
            require(isinstance(evidence, list) and (evidence or not required), f"{ident}: evidence is required")
            for item in evidence:
                fields(item, EVIDENCE, ident)
                for key, value in item.items():
                    require(isinstance(value, str) and value.strip() and len(value) <= 3000, f"{ident}: invalid evidence {key}")
                require(item["paper"] in papers, f"{ident}: unknown paper {item['paper']}")
            if name in {"assays", "responses"}:
                require(record["node"] in nodes, f"{ident}: unknown junction")
            if name == "assays":
                require(record["modality"] in MODALITIES, f"{ident}: unknown modality")
            if name == "edges":
                require(record["source"] in nodes and record["target"] in nodes, f"{ident}: unknown edge endpoint")
                require(record["status"] == "active" or record["reason"].strip(), f"{ident}: status requires a reason")
                require(record["status"] == "retired" or evidence_supports(evidence, papers), f"{ident}: no current support")
                if record["tier"] == "hypothesis":
                    require(all(record[key].strip() for key in ("assumption", "missing_link", "test")), f"{ident}: incomplete hypothesis")
    return index


def validate_registry(registry):
    fields(registry, "sources receptors notes", "Registry")
    require(all(isinstance(registry[key], list) for key in registry), "Registry entries must be arrays")
    sources = {}
    for source in registry["sources"]:
        fields(source, "name url release", "Registry source")
        require(source["name"] not in sources and valid_url(source["url"]) and source["release"], "Invalid or duplicate registry source")
        sources[source["name"]] = source
    seen = set()
    for record in registry["receptors"]:
        expected = "id label gene family subfamily aliases source" + (" url" if "url" in record else "")
        fields(record, expected, "Receptor")
        ident = record["id"]
        require(isinstance(ident, str) and ID.fullmatch(ident) and ident not in seen, f"{ident}: invalid receptor ID")
        require(all(isinstance(record[key], str) and record[key].strip() for key in ("label", "gene", "subfamily")), f"{ident}: missing receptor metadata")
        require(isinstance(record["aliases"], list) and all(isinstance(alias, str) for alias in record["aliases"]), f"{ident}: invalid aliases")
        require(record["family"] in {"GPCR", "RTK"} and record["source"] in sources, f"{ident}: invalid family or source")
        require("url" not in record or valid_url(record["url"]), f"{ident}: invalid receptor URL")
        seen.add(ident)
    require(seen and all(isinstance(note, str) for note in registry["notes"]), "Registry is empty or has invalid notes")


def aliases(paper):
    return {value for value in (paper["id"], paper["doi"].lower(), paper["pmid"]) if value}


def apply_proposal(graph, proposal, review, candidate):
    """Accept only a matching independent review and an evidence-linked, additive patch."""
    index = validate(graph)
    fields(proposal, "candidate summary changes", "Proposal")
    fields(review, "proposal verdict reason", "Review")
    require(review["verdict"] == "approve" and review["proposal"] == digest(proposal), "Independent approval of this exact proposal is required")
    require(isinstance(review["reason"], str) and review["reason"].strip(), "Independent review reason is required")
    require(proposal["candidate"] == candidate["id"] and isinstance(proposal["summary"], str) and 0 < len(proposal["summary"].strip()) <= 1000, "Invalid proposal source or summary")
    changes = proposal["changes"]
    require(isinstance(changes, dict) and changes and set(changes) <= set(COLLECTIONS), "Unknown or empty patch")
    require(all(isinstance(value, list) for value in changes.values()), "Each patch collection must be an array")
    require(0 < sum(map(len, changes.values())) <= 25, "Patch exceeds the bounded batch size")
    for collection, records in changes.items():
        for record in records:
            fields(record, COLLECTIONS[collection], collection)
            require(isinstance(record["id"], str), "Missing record ID")
    source = candidate["paper"]
    source_ids = aliases(source) | set(candidate.get("aliases", []))
    affected = set(candidate.get("related", []))
    linked = {ident for ident, paper in index["papers"].items() if aliases(paper) & affected}
    notice_type = candidate.get("metadata", {}).get("type")
    notice_targets = {"retraction": "retracted", "correction": "corrected", "concern": "concern"}
    if candidate["kind"] == "notice":
        require(not candidate.get("metadata", {}).get("requires_review") and notice_type in notice_targets and linked,
                "Publication notice is not unambiguous")
    result = deepcopy(index)
    for name, records in changes.items():
        seen = set()
        for record in records:
            ident = record["id"]
            require(ident not in seen, f"Duplicate patch record {ident}")
            seen.add(ident)
            previous = index[name].get(ident)
            if name == "papers":
                if aliases(record) & source_ids and (not previous or previous["status"] == record["status"]):
                    expected_status = source["status"] if not previous else previous["status"]
                    require(record["status"] == expected_status, "Source paper status does not match discovery")
                    require((source["doi"] and record["doi"].lower() == source["doi"].lower()) or (source["pmid"] and record["pmid"] == source["pmid"]), "Paper identity does not match discovery")
                    require(not previous or record == previous, "Existing bibliographic record changed")
                else:
                    require(previous and set(previous.items()) - {("status", previous["status"])} == set(record.items()) - {("status", record["status"])}, "Unrelated or rewritten paper")
                    require(bool(aliases(previous) & affected), "Publication notice or journal version does not reference this paper")
                    allowed = {"retraction": "retracted", "correction": "corrected", "concern": "concern", "journal-version": "superseded"}
                    require(record["status"] == allowed.get(candidate.get("metadata", {}).get("type")), "Unverified paper status transition")
                    require(previous["status"] != "retracted", "Retraction reversal requires maintainer review")
                    if notice_type == "journal-version":
                        require(previous["status"] == "preprint", "Only a preprint can be superseded")
                    if notice_type == "correction":
                        require(previous["status"] != "concern", "Clearing a concern requires maintainer review")
            elif candidate["kind"] == "notice":
                require(previous and any(item["paper"] in linked for item in previous.get("evidence", [])),
                        f"{ident}: notice cannot revise unrelated evidence")
            if name == "edges" and previous:
                require(all(previous[key] == record[key] for key in ("source", "target", "effect")), f"{ident}: changed molecular identity")
                require(previous["status"] != "retired" or record["status"] == "retired", f"{ident}: restoring a retired record requires maintainer review")
            if name in {"assays", "responses"} and previous:
                require(previous["node"] == record["node"], f"{ident}: changed junction")
            if previous and "evidence" in record:
                require(isinstance(record["evidence"], list) and all(item in record["evidence"] for item in previous["evidence"]), f"{ident}: prior evidence must be retained")
            result[name][ident] = record
    graph_after = {name: list(records.values()) for name, records in result.items()}
    validate(graph_after)
    source_papers = {ident for ident, paper in result["papers"].items() if aliases(paper) & source_ids}
    require(source_papers, "Source publication is absent")
    for name in ("edges", "assays", "responses", "coverage"):
        for record in changes.get(name, []):
            require(any(item["paper"] in source_papers for item in record["evidence"]), f"{record['id']}: missing evidence from the candidate")
    connected = {endpoint for edge in graph_after["edges"] for endpoint in (edge["source"], edge["target"])}
    for record in changes.get("nodes", []):
        require(record["id"] not in index["nodes"] and record["id"] in connected, "Node changes require a new connected junction")
    if candidate["kind"] == "notice":
        require(all(result["papers"][ident]["status"] == notice_targets[notice_type] for ident in linked),
                "Publication notice requires updating the affected paper status")
        impacted = [edge for edge in graph["edges"] if edge["status"] != "retired" and any(item["paper"] in linked for item in edge["evidence"])]
        for edge in impacted:
            updated = result["edges"][edge["id"]]
            require(updated != edge, f"Notice requires re-evaluation of {edge['id']}")
    require(graph_after != graph, "No evidence changes")
    return graph_after


def build(repo, output):
    graph = read(repo / GRAPH)
    validate(graph)
    registry = read(repo / "data/receptors.json")
    validate_registry(registry)
    log = read(repo / "data/changelog.json")
    payload = json.dumps({"graph": graph, "registry": registry, "history": log}, ensure_ascii=False)
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = (repo / "web/index.html").read_text(encoding="utf-8")
    for marker, content in (
        ("/* ATLAS_CSS */", (repo / "web/style.css").read_text(encoding="utf-8")),
        ("<!-- ATLAS_DATA -->", payload),
        ("/* ATLAS_JS */", (repo / "web/app.js").read_text(encoding="utf-8")),
    ):
        require(page.count(marker) == 1, f"Missing or repeated template marker {marker}")
        page = page.replace(marker, content)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", type=Path, default=ROOT / "dist/atlas.html")
    args = cli.parse_args()
    build(ROOT, args.output)
