import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import discover


def europe_page(records, *, total=None, cursor=None):
    return json.dumps({
        "hitCount": len(records) if total is None else total,
        "nextCursorMark": cursor,
        "resultList": {"result": records},
    }).encode()


def crossref_page(records, *, total=None, cursor=None):
    return json.dumps({
        "message": {
            "total-results": len(records) if total is None else total,
            "next-cursor": cursor,
            "items": records,
        }
    }).encode()


def article(pmid, doi="", *, pmcid="", source="MED", oa=False):
    return {
        "id": pmid, "source": source, "pmid": pmid if source == "MED" else "",
        "doi": doi, "pmcid": pmcid, "title": "<i>Signaling</i> study",
        "firstPublicationDate": "2026-11-05", "firstIndexDate": "2026-11-06",
        "isOpenAccess": "Y" if oa else "N", "abstractText": "This text must stay out of saved candidates.",
    }


RETRACTION_WATCH_CSV = b"""Record ID,OriginalPaperDOI,OriginalPaperPubMedID,RetractionNature,RetractionDOI,RetractionPubMedID,Title,RetractionDate
01,10.1234/original,900,Retraction,10.1234/notice,901,Paper,2026-11-01
02,,800,Expression of concern,,801,Other Paper,2026-11-01
03,10.1234/untracked,100,Correction,10.1234/unrelated,101,Untracked,2026-11-01
04,10.1234/original,900,Reinstatement,,,Paper,2026-11-01
"""


class DiscoveryTests(unittest.TestCase):
    def test_window_paging_and_alias_deduplication(self):
        checkpoint = {"europe-pmc": "2026-11-08"}
        queries_seen = []

        def fetch(url, **kwargs):
            parsed = urlsplit(url)
            parameters = parse_qs(parsed.query)
            if parsed.hostname == "api.crossref.org":
                return crossref_page([])
            queries_seen.append(parameters["query"][0])
            if parameters["cursorMark"] == ["*"]:
                return europe_page([
                    article("12"), article("90", "10.1234/known"),
                ], total=4, cursor="more")
            self.assertEqual(parameters["cursorMark"], ["more"])
            return europe_page([
                article("12", "https://doi.org/10.1234/New", pmcid="PMC333", oa=True),
                article("PMC333", "10.1234/new", pmcid="PMC333", oa=True, source="PMC"),
            ], total=4)

        graph = {"papers": [{"id": "known", "doi": "10.1234/known", "pmid": "90"}]}
        result = discover.discover({"papers": []}, checkpoint, today="2026-11-10", fetch=fetch,
                                   queries={"test": "TITLE_ABS:Gq"})
        self.assertEqual(checkpoint, {"europe-pmc": "2026-11-08"})
        self.assertIn("FIRST_PDATE:[2026-10-25 TO 2026-11-10]", queries_seen[0])
        self.assertIn("CREATION_DATE:[2026-10-25 TO 2026-11-10]", queries_seen[0])
        self.assertEqual(result["checkpoint"]["europe-pmc"], "2026-11-10")
        candidate = next(item for item in result["candidates"] if item["paper"]["doi"] == "10.1234/new")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(candidate["paper"]["pmid"], "12")
        self.assertIn("pmcid:PMC333", candidate["aliases"])
        self.assertIn("europe-pmc:pmc:pmc333", candidate["aliases"])
        self.assertEqual(candidate["excerpts"], [])
        self.assertNotIn("stay out", str(candidate))

        def with_known(url, **kwargs):
            if url == discover.RETRACTION_WATCH:
                return RETRACTION_WATCH_CSV
            return fetch(url, **kwargs)

        result = discover.discover(graph, checkpoint, today="2026-11-10", fetch=with_known,
                                   queries={"test": "TITLE_ABS:Gq"})
        self.assertEqual([item["paper"]["doi"] for item in result["candidates"]], ["10.1234/new"])

    def test_incomplete_page_keeps_checkpoint_unchanged(self):
        checkpoint = {"europe-pmc": "2026-11-08"}

        def fetch(url):
            return europe_page([article("12")], total=3, cursor="*")

        with self.assertRaisesRegex(discover.DiscoveryError, "pagination ended"):
            discover.discover({}, checkpoint, today="2026-11-10", fetch=fetch, queries={"test": "TrkB"})
        self.assertEqual(checkpoint, {"europe-pmc": "2026-11-08"})

    def test_crossref_and_retraction_watch_merge_without_losing_pmid_notices(self):
        def fetch(url, **kwargs):
            if url == discover.RETRACTION_WATCH:
                self.assertGreater(kwargs["limit"], discover.MAX_RESPONSE)
                return RETRACTION_WATCH_CSV
            parameters = parse_qs(urlsplit(url).query)
            self.assertEqual(parameters["filter"], ["updates:10.1234/original"])
            return crossref_page([{
                "DOI": "10.1234/notice", "title": ["Retraction statement"],
                "update-to": [{
                    "DOI": "10.1234/original", "type": "retraction", "source": "publisher",
                    "updated": {"date-parts": [[2026, 11, 1]]},
                }],
            }])

        graph = {"papers": [
            {"id": "original", "doi": "10.1234/original", "pmid": "900"},
            {"id": "pmid-only", "doi": "", "pmid": "800"},
        ]}
        result = discover.discover(graph, {}, today="2026-11-10", fetch=fetch, queries={})
        candidates = {item["metadata"]["type"]: item for item in result["candidates"]}
        self.assertEqual(set(candidates), {"retraction", "concern", "unclassified"})
        retraction = candidates["retraction"]
        self.assertEqual(retraction["paper"]["title"], "Retraction statement")
        self.assertEqual(retraction["paper"]["status"], "notice")
        self.assertEqual(retraction["paper"]["pmid"], "901")
        self.assertEqual(retraction["metadata"]["sources"], ["publisher", "retraction-watch"])
        self.assertEqual(retraction["metadata"]["affected"], ["original"])
        self.assertFalse(retraction["metadata"]["requires_review"])
        self.assertEqual(candidates["concern"]["related"], ["pmid-only"])
        self.assertTrue(candidates["unclassified"]["metadata"]["requires_review"])
        self.assertEqual(result["checkpoint"]["retraction-watch"], "2026-11-10")

    def test_explicit_version_relation_prioritizes_journal_with_separate_provenance(self):
        def fetch(url, **kwargs):
            parameters = parse_qs(urlsplit(url).query)
            if "query" in parameters:
                return europe_page([
                    article("PPR12", "10.1234/preprint", source="PPR"),
                    article("87", "10.1234/journal"),
                ])
            return crossref_page([
                {"DOI": "10.1234/preprint", "relation": {
                    "is-preprint-of": [{"id": "10.1234/journal", "id-type": "doi"}],
                }},
            ])

        result = discover.discover({}, {}, today="2026-11-10", fetch=fetch, queries={"test": "TrkB"})
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["paper"]["doi"], "10.1234/journal")
        self.assertEqual(candidate["metadata"]["type"], "journal-version")
        self.assertEqual(candidate["related"], ["10.1234/preprint"])
        self.assertNotIn("doi:10.1234/preprint", candidate["aliases"])

    def test_hydration_extracts_located_results_and_keeps_full_text_out_of_candidate(self):
        candidate = discover.europe_candidate(
            article("12", "10.1234/test", pmcid="PMC333", oa=True), "pilot")

        def fetch(url, **kwargs):
            if url.endswith("/fullTextXML"):
                return b"""<article><body>
                <sec sec-type="results"><title>Results</title>
                <p>Perturbing PLC reduced activity.</p>
                <sec><title>Gq response</title><p>Second result.</p></sec></sec>
                <sec><title>Methods</title><p>Reporter was calibrated.</p></sec>
                <fig><label>Figure 1</label><caption><p>Ca and DAG.</p></caption></fig>
                <sec><title>References</title><p>Omitted citations.</p></sec>
                </body></article>"""
            return europe_page([article("12", "10.1234/test", pmcid="PMC333", oa=True)])

        hydrated = discover.hydrate(candidate, fetch=fetch)
        self.assertEqual(candidate["excerpts"], [])
        self.assertEqual(hydrated["metadata"]["access"], "full-text")
        locations = {item["locator"]: item["text"] for item in hydrated["excerpts"]}
        self.assertEqual(locations["Results, paragraph 1"], "Perturbing PLC reduced activity.")
        self.assertEqual(locations["Results / Gq response, paragraph 1"], "Second result.")
        self.assertEqual(locations["Figure 1 caption"], "Ca and DAG.")
        self.assertEqual(locations["Methods, paragraph 1"], "Reporter was calibrated.")
        self.assertNotIn("References, paragraph 1", locations)

    def test_hydration_never_reads_non_oa_full_text(self):
        record = article("12", "10.1234/test", pmcid="PMC333")
        candidate = discover.europe_candidate(record, "pilot")

        def fetch(url, **kwargs):
            self.assertTrue(url.startswith(f"{discover.EUROPE}/search?"))
            return europe_page([record])

        result = discover.hydrate(candidate, fetch=fetch)
        self.assertEqual(result["metadata"]["access"], "abstract-only")
        self.assertEqual([item["locator"] for item in result["excerpts"]], ["Abstract"])

    def test_related_paper_text_is_labeled_and_cannot_stand_in_for_a_notice(self):
        graph = {"papers": [{"id": "prior", "doi": "10.1234/prior", "pmid": "12"}]}
        notice = discover.notice_candidate(
            doi="10.1234/notice", pmid="13", title="Correction", nature="correction",
            date_info=("2026-11-01",), source="publisher", affected={"prior"}, key="notice")
        requests = []

        def fetch(url):
            query = parse_qs(urlsplit(url).query)["query"][0]
            requests.append(query)
            if '"12"' in query:
                return europe_page([article("12", "10.1234/prior")])
            return europe_page([article("13", "10.1234/notice")])

        result = discover.hydrate(notice, fetch=fetch, graph=graph)
        self.assertEqual(result["metadata"]["related_access"], {"prior": "abstract-only"})
        self.assertEqual(result["excerpts"][-1]["locator"], "Related paper prior · Abstract")
        requests.clear()

        def unavailable_notice(url):
            requests.append(url)
            return europe_page([])

        result = discover.hydrate(notice, fetch=unavailable_notice, graph=graph)
        self.assertEqual(result["metadata"]["access"], "metadata-only")
        self.assertEqual(result["excerpts"], [])
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
