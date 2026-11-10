"""Discover literature and integrity notices without interpreting scientific claims."""

import csv
from datetime import date, datetime, timedelta, timezone
import gzip
from hashlib import sha256
from html.parser import HTMLParser
import io
import json
import os
import re
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

EUROPE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CROSSREF = "https://api.crossref.org/v1/works"
RETRACTION_WATCH = "https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv"
OVERLAP = timedelta(days=14)
PAGE_SIZE = 100
MAX_RESPONSE = 24 * 1024 * 1024
RETRACTION_WATCH_LIMIT = 128 * 1024 * 1024

DEFAULT_QUERIES = {
    "gpcr": 'TITLE_ABS:(GPCR OR "G protein-coupled receptor" OR "G protein-coupled receptors" OR'
    ' "olfactory receptor" OR "taste receptor" OR opsin) AND TITLE_ABS:(signaling OR signalling'
    ' OR transduction OR coupling OR effector OR trafficking)',
    "rtk": 'TITLE_ABS:("receptor tyrosine kinase" OR "receptor tyrosine kinases" OR TrkB OR NTRK2'
    ' OR EGFR OR ERBB OR FGFR OR VEGFR OR PDGFR OR IGF1R OR INSR OR DDR1 OR DDR2 OR ROR1'
    ' OR ROR2 OR RYK OR EPHA OR EPHB OR ALK OR LTK OR AXL OR MERTK OR MET OR MST1R OR RET'
    ' OR TIE2 OR TEK OR NTRK1 OR NTRK3 OR PTK7 OR ROS1 OR MUSK OR CSF1R OR FLT3 OR KIT)'
    ' AND TITLE_ABS:(signaling OR signalling OR effector OR phosphorylation OR trafficking)',
    "g-proteins": 'TITLE_ABS:(Gq OR GNAQ OR GNA11 OR GNAS OR GNAI1 OR GNAI2 OR GNAI3'
    ' OR GNAO1 OR GNAL OR GNA12 OR GNA13 OR'
    ' "G protein beta gamma" OR arrestin) AND TITLE_ABS:(signaling OR signalling OR coupling'
    ' OR transduction OR effector)',
    "pilot": 'TITLE_ABS:(HTR2A OR "5-HT2A" OR NTRK2 OR TrkB OR GNAQ OR'
    ' "phospholipase C beta" OR "phospholipase C gamma") AND TITLE_ABS:(Gq OR PLC OR Ca2+'
    ' OR PKC OR RhoA OR Trio OR Shc OR phosphoinositide OR biosensor)',
    "assays": 'TITLE_ABS:(FRET OR BRET OR "single fluorophore" OR "single fluorescent protein"'
    ' OR chemigenetic OR "kinase translocation reporter") AND TITLE_ABS:(biosensor OR biosensors)'
    ' AND TITLE_ABS:(signaling OR signalling OR kinase OR receptor OR calcium OR RhoA OR'
    ' "second messenger" OR cAMP OR cGMP)',
}

NOTICE_TYPES = {
    "retraction": "retraction",
    "retraction-of-publication": "retraction",
    "correction": "correction",
    "published-erratum": "correction",
    "erratum": "correction",
    "corrigendum": "correction",
    "expression-of-concern": "concern",
}
NON_PRIMARY_TYPES = {"review", "systematic review", "meta-analysis", "editorial", "comment"}
NOTICE_PUBLICATION_TYPES = {"published erratum", "retraction of publication", "expression of concern"}


class DiscoveryError(RuntimeError):
    """A source failed before we could account for all candidate records."""


def fetch_bytes(url, *, limit=MAX_RESPONSE):
    """Bound retrieval and retry transient HTTP failures; never swallow a missing page."""
    headers = {
        "User-Agent": "Cell-Signaling atlas (https://github.com/adammosswood/Cell-Signaling)",
        "Accept-Encoding": "gzip",
    }
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=35) as response:
                if response.headers.get("Content-Encoding") == "gzip":
                    with gzip.GzipFile(fileobj=response) as stream:
                        content = stream.read(limit + 1)
                else:
                    content = response.read(limit + 1)
            if len(content) > limit:
                raise DiscoveryError(f"Response exceeds size limit for {urlsplit(url).hostname}")
            return content
        except HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After", "")
            sleep(min(int(retry_after), 60) if retry_after.isdecimal() else 2 ** attempt)
        except (TimeoutError, URLError):
            if attempt == 2:
                raise
            sleep(2 ** attempt)


def endpoint(base, params):
    return f"{base}?{urlencode(params)}"


def crossref_url(params):
    address = os.getenv("CROSSREF_MAILTO")
    if address:
        params = dict(params, mailto=address)
    return endpoint(CROSSREF, params)


def normalize_doi(value):
    value = unquote(str(value or "")).strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def normalize_pmid(value):
    value = str(value or "").strip()
    return value if value.isascii() and value.isdecimal() and value != "0" else ""


def aliases(doi="", pmid="", pmcid=""):
    return {
        f"{kind}:{value}"
        for kind, value in (("doi", normalize_doi(doi)), ("pmid", normalize_pmid(pmid)),
                            ("pmcid", str(pmcid or "").upper()))
        if value
    }


def stable_id(prefix, key):
    return f"{prefix}-{sha256(key.encode('utf-8')).hexdigest()[:20]}"


def source_window(checkpoint, name, end):
    mark = date.fromisoformat(checkpoint[name]) if name in checkpoint else end
    if mark > end:
        raise DiscoveryError(f"Checkpoint for {name} is in the future")
    return f"[{mark - OVERLAP} TO {end}]"


def europe_pages(query, fetch):
    cursor = "*"
    seen_cursors = {cursor}
    yielded = 0
    while True:
        url = endpoint(f"{EUROPE}/search", {
            "query": query, "resultType": "core", "format": "json",
            "pageSize": PAGE_SIZE, "cursorMark": cursor,
        })
        page = json.loads(fetch(url))
        results = page["resultList"]["result"]
        total = int(page["hitCount"])
        yielded += len(results)
        yield from results
        if yielded >= total:
            return
        cursor = page.get("nextCursorMark")
        if not results or not cursor or cursor in seen_cursors:
            raise DiscoveryError("Europe PMC pagination ended before all reported results")
        seen_cursors.add(cursor)


def crossref_pages(filters, fetch):
    cursor = "*"
    seen_cursors = {cursor}
    yielded = 0
    while True:
        params = {"filter": ",".join(filters), "rows": PAGE_SIZE, "cursor": cursor}
        page = json.loads(fetch(crossref_url(params)))["message"]
        results = page["items"]
        total = int(page["total-results"])
        yielded += len(results)
        yield from results
        if yielded >= total:
            return
        cursor = page.get("next-cursor")
        if not results or not cursor or cursor in seen_cursors:
            raise DiscoveryError("Crossref pagination ended before all reported results")
        seen_cursors.add(cursor)


def batches(values, size=20):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def paper_url(doi, pmid, pmcid, source="", source_id=""):
    if doi:
        return f"https://doi.org/{quote(doi, safe='/;:()[]')}"
    if pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if pmcid:
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    return f"https://europepmc.org/article/{quote(source, safe='')}/{quote(source_id, safe='')}"


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []

    def handle_data(self, data):
        self.fragments.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "br", "div", "h1", "h2", "h3", "li", "sec", "jats:p", "jats:sec"}:
            self.fragments.append(" ")


def plain_text(value):
    parser = TextOnly()
    parser.feed(str(value or ""))
    return " ".join("".join(parser.fragments).split())


def europe_candidate(record, topic):
    source = record["source"]
    source_id = str(record["id"])
    doi = normalize_doi(record.get("doi"))
    pmid = normalize_pmid(record.get("pmid", source_id if source == "MED" else ""))
    pmcid = str(record.get("pmcid", "")).upper()
    if not re.fullmatch(r"PMC\d+", pmcid):
        pmcid = ""
    record_aliases = aliases(doi, pmid, pmcid) | {f"europe-pmc:{source.lower()}:{source_id.lower()}"}
    key = next((alias for alias in (f"doi:{doi}", f"pmid:{pmid}", f"pmcid:{pmcid}")
                if alias in record_aliases), min(record_aliases))
    source_types = record.get("pubTypeList", {}).get("pubType") or str(record.get("pubType", "")).split(";")
    types = sorted({str(item).strip().lower() for item in source_types if str(item).strip()})
    preprint = source == "PPR" or any(item.startswith("preprint") for item in types)
    pub_date = str(record.get("firstPublicationDate") or record.get("pubYear") or "")
    year = year_from_dates(pub_date)
    return {
        "id": stable_id("publication", key), "kind": "publication",
        "paper": {
            "id": stable_id("paper", key), "title": plain_text(record.get("title")) or source_id,
            "doi": doi, "pmid": pmid, "year": year, "status": "preprint" if preprint else "published",
            "url": paper_url(doi, pmid, pmcid, source, source_id),
        },
        "aliases": sorted(record_aliases), "related": [], "excerpts": [],
        "metadata": {
            "topics": [topic], "source": "europe-pmc", "europe_pmc_id": source_id,
            "europe_pmc_source": source, "pmcid": pmcid, "open_access": record.get("isOpenAccess") == "Y",
            "publication_types": types, "publication_date": pub_date,
            "first_indexed": str(record.get("firstIndexDate") or ""),
            "non_primary": bool(NON_PRIMARY_TYPES.intersection(types)),
            "requires_review": bool(not (doi or pmid) or year is None or
                                    {"retracted publication", "preprint-withdrawal"}.intersection(types)),
        },
    }


def candidate_order(candidate):
    paper = candidate["paper"]
    metadata = candidate["metadata"]
    return (not paper["doi"], not paper["pmid"], not metadata["open_access"], candidate["id"])


def merge_publications(candidates):
    groups = {}
    alias_groups = {}
    for candidate in candidates:
        group_id = candidate["id"]
        matched_groups = {alias_groups[a] for a in candidate["aliases"] if a in alias_groups}
        merged = [candidate]
        for prior_id in matched_groups:
            merged.append(groups.pop(prior_id))
        preferred = min(merged, key=candidate_order)
        preferred["aliases"] = sorted({alias for item in merged for alias in item["aliases"]})
        preferred["metadata"]["topics"] = sorted({
            topic for item in merged for topic in item["metadata"]["topics"]
        })
        preferred["metadata"]["publication_types"] = sorted({
            name for item in merged for name in item["metadata"]["publication_types"]
        })
        open_record = next((item for item in merged if item["metadata"]["open_access"]
                            and item["metadata"]["pmcid"]), None)
        if open_record:
            preferred["metadata"]["pmcid"] = open_record["metadata"]["pmcid"]
            preferred["metadata"]["open_access"] = True
        groups[group_id] = preferred
        for alias in preferred["aliases"]:
            alias_groups[alias] = group_id
    return sorted(groups.values(), key=lambda item: item["id"])


def preprint_links(candidates, graph_papers, fetch):
    by_doi = {c["paper"]["doi"]: c for c in candidates if c["paper"]["doi"]}
    graph_by_doi = {normalize_doi(paper.get("doi")): paper["id"] for paper in graph_papers}
    edges = set()
    for subset in batches(by_doi):
        filters = [f"doi:{doi}" for doi in subset]
        for item in crossref_pages(filters, fetch):
            doi = normalize_doi(item.get("DOI"))
            relations = item.get("relation", {})
            for relation, reverse in (("is-preprint-of", False), ("has-preprint", True)):
                for linked in relations.get(relation, []):
                    if linked.get("id-type") != "doi":
                        continue
                    related_doi = normalize_doi(linked.get("id"))
                    if doi and related_doi and doi != related_doi:
                        edges.add((related_doi, doi) if reverse else (doi, related_doi))

    for preprint_doi, journal_doi in sorted(edges):
        for doi, other in ((preprint_doi, journal_doi), (journal_doi, preprint_doi)):
            if doi in by_doi:
                related = graph_by_doi.get(other) or other
                by_doi[doi]["related"].append(related)
                by_doi[doi]["related"].sort()
        if preprint_doi in by_doi:
            by_doi[preprint_doi]["metadata"]["published_version"] = journal_doi
        if journal_doi in by_doi:
            item = by_doi[journal_doi]["metadata"]
            item["preprints"] = sorted(set(item.get("preprints", [])) | {preprint_doi})
            item["type"] = "journal-version"
    return [
        candidate for candidate in candidates
        if not (
            candidate["paper"]["status"] == "preprint"
            and candidate["metadata"].get("published_version") in (by_doi.keys() | graph_by_doi.keys())
        )
    ]


def normalize_notice_type(value):
    raw = re.sub(r"[-_\s]+", "-", str(value or "").strip().lower())
    return NOTICE_TYPES.get(raw, "unclassified")


def year_from_dates(*values):
    for value in values:
        year = re.search(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)", str(value or ""))
        if year:
            return int(year[0])
    return None


def notice_date(value):
    if isinstance(value, dict):
        parts = (value.get("date-parts") or [[]])[0]
        if len(parts) == 3:
            try:
                return date(*parts).isoformat()
            except (TypeError, ValueError):
                pass
        value = value.get("date-time") or value
    value = str(value or "").strip()
    for date_format, text in (("%Y-%m-%d", value[:10]), ("%m/%d/%Y", value)):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            pass
    return ""


def notice_candidate(*, doi, pmid, title, date_info, nature, source, affected, key, context=""):
    normalized = normalize_notice_type(nature)
    identifiers = aliases(doi, pmid)
    notice_key = f"doi:{doi}" if doi else f"pmid:{pmid}" if pmid else key
    url = paper_url(doi, pmid, "") if identifiers else "https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/"
    precise_date = next((value for value in map(notice_date, date_info) if value), "")
    year = year_from_dates(precise_date, *date_info)
    return {
        "id": stable_id("notice", f"{notice_key}:{normalized}"), "kind": "notice",
        "paper": {
            "id": stable_id("paper", notice_key), "title": plain_text(title) or "Publication notice",
            "doi": doi, "pmid": pmid, "year": year, "status": "notice", "url": url,
        },
        "aliases": sorted(identifiers), "related": sorted(affected), "excerpts": [],
        "metadata": {
            "type": normalized, "reported_type": str(nature or ""), "source": source,
            "requires_review": normalized == "unclassified" or not identifiers or year is None,
            "affected": sorted(affected), "source_note": context, "notice_date": precise_date,
        },
    }


def crossref_notices(by_doi, fetch):
    for subset in batches(by_doi):
        filters = [f"updates:{doi}" for doi in subset]
        for item in crossref_pages(filters, fetch):
            updates = item.get("update-to", [])
            title = (item.get("title") or ["Publication update"])[0]
            for update in updates:
                original = normalize_doi(update.get("DOI"))
                if original not in by_doi:
                    continue
                doi = normalize_doi(item.get("DOI"))
                if doi == original:
                    continue
                yield notice_candidate(
                    doi=doi, pmid="", title=title,
                    date_info=(update.get("updated"), item.get("published"), item.get("issued")),
                    nature=update.get("type") or update.get("label"),
                    source=update.get("source") or "crossref",
                    affected=by_doi[original], key=f"crossref:{original}",
                )


def retraction_watch_notices(by_doi, by_pmid, fetch):
    if not by_doi and not by_pmid:
        return
    raw = fetch(RETRACTION_WATCH, limit=RETRACTION_WATCH_LIMIT)
    with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        required_columns = {
            "Record ID", "OriginalPaperDOI", "OriginalPaperPubMedID", "RetractionNature",
            "RetractionDOI", "RetractionPubMedID",
        }
        if not required_columns.issubset(rows.fieldnames or ()):
            raise DiscoveryError("Retraction Watch download is missing expected columns")
        for row in rows:
            original_doi = normalize_doi(row.get("OriginalPaperDOI"))
            original_pmid = normalize_pmid(row.get("OriginalPaperPubMedID"))
            affected = by_doi.get(original_doi, set()) | by_pmid.get(original_pmid, set())
            if not affected:
                continue
            doi = normalize_doi(row.get("RetractionDOI"))
            pmid = normalize_pmid(row.get("RetractionPubMedID"))
            if doi == original_doi:
                doi = ""
            if pmid == original_pmid:
                pmid = ""
            nature = row["RetractionNature"]
            yield notice_candidate(
                doi=doi, pmid=pmid, title=f'{nature} notice for {row.get("Title") or "a cited paper"}',
                date_info=(row.get("RetractionDate"),), nature=nature,
                source="retraction-watch", affected=affected, key=f'retraction-watch:{row["Record ID"]}',
                context=f'Retraction Watch record {row["Record ID"]}',
            )


def merge_notices(candidates):
    results = {}
    identity_index = {}
    for candidate in candidates:
        notice_type = candidate["metadata"]["type"]
        identities = {(notice_type, alias) for alias in candidate["aliases"]}
        matching = {identity_index[key] for key in identities if key in identity_index}
        group = [candidate] + [results.pop(key) for key in matching]
        preferred = min(group, key=lambda item: (not item["paper"]["doi"],
                                                  item["metadata"]["source"] != "publisher",
                                                  item["id"]))
        paper = preferred["paper"]
        for item in group:
            paper["doi"] = paper["doi"] or item["paper"]["doi"]
            paper["pmid"] = paper["pmid"] or item["paper"]["pmid"]
            paper["year"] = paper["year"] or item["paper"]["year"]
            preferred["metadata"]["notice_date"] = (preferred["metadata"]["notice_date"] or
                                                        item["metadata"]["notice_date"])
        preferred["related"] = sorted({value for item in group for value in item["related"]})
        preferred["metadata"]["affected"] = preferred["related"]
        preferred["metadata"]["sources"] = sorted({source for item in group for source in
                                                   item["metadata"].get("sources", [item["metadata"]["source"]])})
        preferred["metadata"]["requires_review"] = notice_type == "unclassified" or not (
            paper["year"] and (paper["doi"] or paper["pmid"])
        )
        preferred["aliases"] = sorted({alias for item in group for alias in item["aliases"]})
        results[preferred["id"]] = preferred
        for alias in preferred["aliases"]:
            identity_index[(notice_type, alias)] = preferred["id"]
    priority = {"correction": 0, "concern": 1, "retraction": 2, "unclassified": 3}
    return sorted(results.values(), key=lambda item: (
        item["metadata"]["notice_date"] or "9999", priority[item["metadata"]["type"]], item["id"]
    ))


def discover(graph, checkpoint, *, today=None, fetch=fetch_bytes, queries=None):
    """Collect metadata, leaving all input cursors intact if any required feed fails.

    Persist the returned checkpoint atomically with the returned candidates. Abstracts and
    article excerpts are deliberately excluded from persisted discovery records.
    """
    today = today or datetime.now(timezone.utc).date()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    if queries is None:
        queries = graph.get("discovery_queries", DEFAULT_QUERIES)
    graph_papers = graph.get("papers", [])
    known = set()
    by_doi, by_pmid = {}, {}
    for paper in graph_papers:
        doi = normalize_doi(paper.get("doi"))
        pmid = normalize_pmid(paper.get("pmid"))
        known.update(aliases(doi, pmid))
        if doi:
            by_doi.setdefault(doi, set()).add(paper["id"])
        if pmid:
            by_pmid.setdefault(pmid, set()).add(paper["id"])

    window = source_window(checkpoint, "europe-pmc", today)
    discovery = []
    for topic, base_query in sorted(queries.items()):
        query = f"({base_query}) AND (FIRST_PDATE:{window} OR CREATION_DATE:{window})"
        discovery.extend(europe_candidate(record, topic) for record in europe_pages(query, fetch))
    publications = merge_publications(discovery)
    publications = [candidate for candidate in publications if known.isdisjoint(candidate["aliases"])
                    and NOTICE_PUBLICATION_TYPES.isdisjoint(candidate["metadata"]["publication_types"])]
    publications = preprint_links(publications, graph_papers, fetch)
    notices = merge_notices([
        *crossref_notices(by_doi, fetch),
        *retraction_watch_notices(by_doi, by_pmid, fetch),
    ])
    return {
        "candidates": notices + publications,
        "checkpoint": dict(checkpoint, **{source: today.isoformat() for source in
                                         ("europe-pmc", "crossref", "retraction-watch")}),
    }


def element_text(element):
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def excerpts_from_xml(raw, *, notice=False):
    root = ElementTree.fromstring(raw)
    for element in root.iter():
        element.tag = element.tag.split("}")[-1]
    excerpts = []
    figures = root.findall("./body//fig") + root.findall("./floats-group//fig")
    for figure in figures[:24]:
        label = element_text(figure.find("./label")) or figure.get("id", "")
        caption = element_text(figure.find("./caption"))
        if label and caption:
            excerpts.append({"locator": f"{label} caption", "text": caption[:1400]})

    def paragraphs(section, label, budget):
        for index, paragraph in enumerate(section.findall("./p"), 1):
            text = element_text(paragraph)
            if text and budget > 0:
                text = text[:min(budget, 1400)]
                excerpts.append({"locator": f"{label}, paragraph {index}", "text": text})
                budget -= len(text)
        for subsection in section.findall("./sec"):
            title = element_text(subsection.find("./title")) or subsection.get("id", "subsection")
            budget = paragraphs(subsection, f"{label} / {title}", budget)
        return budget

    body = root.find("./body")
    if body is not None:
        remaining = 18000
        if notice or root.get("article-type") in {"correction", "retraction", "expression-of-concern"}:
            paragraphs(body, "Publication notice", remaining)
        else:
            for section in body.findall("./sec"):
                title = element_text(section.find("./title")) or section.get("id", "section")
                role = f'{title} {section.get("sec-type", "")}'.lower()
                if any(key in role for key in ("result", "finding", "experiment", "method", "material")):
                    remaining = paragraphs(section, title, remaining)
    return excerpts


def exact_europe_record(candidate, fetch):
    metadata, paper = candidate["metadata"], candidate["paper"]
    if metadata.get("europe_pmc_id") and metadata.get("europe_pmc_source"):
        source_id = metadata["europe_pmc_id"]
        source = metadata["europe_pmc_source"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", source_id) or not re.fullmatch(r"[A-Za-z]+", source):
            raise DiscoveryError("Invalid Europe PMC source identifiers")
        query = f'EXT_ID:"{source_id}" AND SRC:{source}'
    elif paper["pmid"]:
        query = f'EXT_ID:"{normalize_pmid(paper["pmid"])}" AND SRC:MED'
    elif paper["doi"]:
        doi = normalize_doi(paper["doi"]).replace("\\", "\\\\").replace('"', '\\"')
        query = f'DOI:"{doi}"'
    else:
        return None

    known = aliases(paper["doi"], paper["pmid"])
    for item in europe_pages(query, fetch):
        pmid = item.get("pmid") or (item["id"] if item["source"] == "MED" else "")
        if known & aliases(item.get("doi"), pmid) or (
            item["id"] == metadata.get("europe_pmc_id") and item["source"] == metadata.get("europe_pmc_source")
        ):
            return item
    return None


def hydrate_one(candidate, fetch):
    result = dict(candidate, excerpts=[], metadata=dict(candidate["metadata"]))
    record = exact_europe_record(candidate, fetch)
    if record is None:
        result["metadata"]["access"] = "metadata-only"
        return result

    abstract = plain_text(record.get("abstractText"))
    if abstract:
        result["excerpts"].append({"locator": "Abstract", "text": abstract[:2500]})
    pmcid = str(record.get("pmcid") or "")
    sections = []
    if record.get("isOpenAccess") == "Y" and re.fullmatch(r"PMC\d+", pmcid):
        try:
            sections = excerpts_from_xml(fetch(f"{EUROPE}/{pmcid}/fullTextXML"),
                                         notice=candidate["kind"] == "notice")
        except HTTPError as error:
            if error.code not in {403, 404}:
                raise
    result["excerpts"].extend(sections)
    result["metadata"]["access"] = "full-text" if sections else "abstract-only" if abstract else "metadata-only"
    return result


def hydrate(candidate, *, fetch=fetch_bytes, graph=None):
    """Reacquire temporary, located source text for a bounded review context."""
    result = hydrate_one(candidate, fetch)
    related = set(candidate.get("related", ()))
    if not graph or not related or not result["excerpts"]:
        return result
    result["metadata"]["related_access"] = {}
    budget = 20000
    for paper in graph.get("papers", ()):
        if not related.intersection({paper["id"], paper["doi"], paper["pmid"]} - {""}):
            continue
        if budget <= 0:
            result["metadata"]["related_access"][paper["id"]] = "context-limit"
            continue
        linked = hydrate_one({"kind": "publication", "paper": paper, "metadata": {}}, fetch)
        result["metadata"]["related_access"][paper["id"]] = linked["metadata"]["access"]
        for excerpt in linked["excerpts"]:
            excerpt_text = excerpt["text"][:budget]
            if not excerpt_text:
                break
            result["excerpts"].append({"locator": f'Related paper {paper["id"]} · {excerpt["locator"]}',
                                       "text": excerpt_text})
            budget -= len(excerpt_text)
    return result
