import collections
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from atlas import Invalid, apply_proposal, build, digest, read, validate, validate_registry, write


def paper(ident, number, status="published"):
    doi = f"10.99999/atlas-fixture-{number}"
    return dict(id=ident, title=f"Synthetic experiment {number}", year=2026, doi=doi,
                pmid="", url=f"https://doi.org/{doi}", status=status)


def evidence(ident, finding="The intervention changed the junction readout."):
    return dict(paper=ident, locator="Figure 2B", context="Human cell line A, plasma membrane",
                perturbation="Ligand A and pathway-selective genetic perturbation",
                readout="Activity measurement", finding=finding)


def node(ident, kind="junction"):
    return dict(id=ident, label=ident, kind=kind, compartment="plasma membrane",
                summary="Synthetic molecular state")


def edge(source, target, paper_id):
    return dict(id=f"{source}-{target}", source=source, target=target, effect="activation",
                tier="demonstrated", status="active", summary="Synthetic molecular event",
                evidence=[evidence(paper_id)], reason="", assumption="", missing_link="", test="")


def reviewed(source, changes, notice_type=None, affected=None):
    candidate = dict(id=source["id"], paper=source, kind="notice" if notice_type else "paper",
                     related=[affected["doi"]] if affected else [], metadata={"type": notice_type})
    proposal = dict(candidate=candidate["id"], summary="Update from a synthetic publication", changes=changes)
    review = dict(proposal=digest(proposal), verdict="approve", reason="Independent synthetic reference check")
    return proposal, review, candidate


def downstream(graph, start):
    neighbors = collections.defaultdict(list)
    for connection in graph["edges"]:
        if connection["status"] != "retired":
            neighbors[connection["source"]].append(connection["target"])
    visited = {start}
    queue = collections.deque([start])
    while queue:
        for target in neighbors[queue.popleft()]:
            if target not in visited:
                visited.add(target)
                queue.append(target)
    return visited - {start}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.original = paper("original", 1)
        self.other = paper("other", 2)
        self.connection = edge("hub-a", "messenger", self.original["id"])
        self.independent = edge("hub-b", "messenger", self.other["id"])
        self.graph = {
            "papers": [self.original, self.other],
            "nodes": [node("hub-a", "hub"), node("hub-b", "hub"), node("messenger", "messenger"), node("output", "endpoint")],
            "edges": [self.connection, self.independent, edge("messenger", "output", self.other["id"])],
            "assays": [], "responses": [], "coverage": [],
        }

    def test_new_paper_adds_branch_and_rerun_has_no_changes(self):
        source = paper("new-publication", 3)
        args = reviewed(source, {"papers": [source], "nodes": [node("new-junction")],
                                 "edges": [edge("hub-a", "new-junction", source["id"])]})
        before = deepcopy(self.graph)
        updated = apply_proposal(self.graph, *args)
        self.assertEqual(self.graph, before)
        self.assertEqual(downstream(updated, "hub-a"), {"messenger", "output", "new-junction"})
        self.assertEqual(downstream(updated, "hub-b"), {"messenger", "output"})
        with self.assertRaisesRegex(Invalid, "(No evidence changes|new connected junction)"):
            apply_proposal(updated, *args)

    def test_negative_result_contests_matching_branch_in_context(self):
        source = paper("negative-publication", 3)
        negative = evidence(source["id"], "No change was detected with ligand B in cell line B.")
        negative["context"] = "Human cell line B, plasma membrane"
        negative["perturbation"] = "Ligand B"
        connection = dict(self.connection, status="contested", reason="Ligand B showed no effect in cell line B.",
                          summary="Ligand A in cell line A supports this step. Ligand B in cell line B did not.",
                          evidence=self.connection["evidence"] + [negative])
        coverage = dict(id="negative-context", scope="Hub A with ligand B in cell line B", status="no_effect",
                        summary="No effect under the measured conditions.", evidence=[negative])
        args = reviewed(source, {"papers": [source], "edges": [connection], "coverage": [coverage]})
        result = validate(apply_proposal(self.graph, *args))
        self.assertEqual(result["edges"][connection["id"]], connection)
        self.assertEqual(result["edges"][self.independent["id"]], self.independent)
        self.assertEqual(result["coverage"][coverage["id"]]["status"], "no_effect")

    def test_correction_changes_only_the_affected_paper_and_edge(self):
        notice = paper("correction-notice", 3, "notice")
        corrected = dict(self.original, status="corrected")
        revision = evidence(notice["id"], "Figure 2B labels were corrected. The result is specific to cell line A.")
        amended = dict(self.connection, summary="Activation in cell line A, as corrected by the publisher.",
                       evidence=self.connection["evidence"] + [revision])
        args = reviewed(notice, {"papers": [notice, corrected], "edges": [amended]}, "correction", self.original)
        graph = apply_proposal(self.graph, *args)
        index = validate(graph)
        self.assertEqual(index["papers"][self.original["id"]], corrected)
        self.assertEqual(index["papers"][self.other["id"]], self.other)
        self.assertEqual(index["edges"][self.connection["id"]], amended)
        self.assertEqual(graph["edges"][1:], self.graph["edges"][1:])
        self.assertEqual(graph["nodes"], self.graph["nodes"])

    def test_retraction_retires_solely_supported_route_and_keeps_shared_junction(self):
        notice = paper("retraction-notice", 3, "notice")
        retracted = dict(self.original, status="retracted")
        revision = evidence(notice["id"], "The publisher retracted the experiment underlying this edge.")
        retirement = dict(self.connection, status="retired", reason="Sole primary evidence retracted.",
                          evidence=self.connection["evidence"] + [revision])
        args = reviewed(notice, {"papers": [notice, retracted], "edges": [retirement]}, "retraction", self.original)
        graph = apply_proposal(self.graph, *args)
        index = validate(graph)
        self.assertEqual(index["edges"][self.connection["id"]], retirement)
        self.assertEqual(index["papers"][self.original["id"]], retracted)
        self.assertIn("messenger", index["nodes"])
        self.assertEqual(downstream(graph, "hub-a"), set())
        self.assertEqual(downstream(graph, "hub-b"), {"messenger", "output"})
        with self.assertRaisesRegex(Invalid, "no current support"):
            apply_proposal(self.graph, *reviewed(notice, {"papers": [notice, retracted]}, "retraction", self.original))

    def test_notice_cannot_leave_the_affected_paper_published(self):
        notice = paper("retraction-notice", 3, "notice")
        review_evidence = evidence(notice["id"], "Publisher notice names the original experiment.")
        incomplete = dict(self.connection, evidence=self.connection["evidence"] + [review_evidence])
        args = reviewed(notice, {"papers": [notice], "edges": [incomplete]}, "retraction", self.original)
        with self.assertRaises(Invalid):
            apply_proposal(self.graph, *args)

    def test_notice_cannot_modify_an_unrelated_edge(self):
        notice = paper("correction-notice", 3, "notice")
        corrected = dict(self.original, status="corrected")
        revision = evidence(notice["id"], "Publisher correction applies to the original experiment.")
        amended = dict(self.connection, evidence=self.connection["evidence"] + [revision])
        unrelated = dict(self.independent, evidence=self.independent["evidence"] + [revision])
        args = reviewed(notice, {"papers": [notice, corrected], "edges": [amended, unrelated]}, "correction", self.original)
        with self.assertRaises(Invalid):
            apply_proposal(self.graph, *args)

    def test_unmatched_or_disagreed_review_and_invalid_graph_cannot_publish(self):
        source = paper("new-publication", 3)
        proposal, review, candidate = reviewed(source, {"papers": [source], "edges": [edge("hub-a", "missing", source["id"])]})
        changed_proposal = dict(proposal, summary="Edited after independent review")
        with self.assertRaisesRegex(Invalid, "Independent approval"):
            apply_proposal(self.graph, changed_proposal, review, candidate)
        with self.assertRaisesRegex(Invalid, "Independent approval"):
            apply_proposal(self.graph, proposal, dict(review, verdict="escalate"), candidate)
        with self.assertRaisesRegex(Invalid, "unknown edge endpoint"):
            apply_proposal(self.graph, proposal, review, candidate)


class PilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = read(ROOT / "data/pilot.json")
        cls.registry = read(ROOT / "data/receptors.json")

    def test_pilot_paths_and_measurement_modalities(self):
        graph = validate(self.graph)
        self.assertIn(len(graph["nodes"]), range(15, 26))
        self.assertTrue({"gq", "gi"} <= downstream(self.graph, "htr2a"))
        shared = {"pip2", "ip3", "dag", "calcium", "pkc"}
        self.assertTrue(shared | {"plc-beta", "kcnq", "p63rhogef", "rhoa", "rock"} <= downstream(self.graph, "gq"))
        self.assertTrue(shared | {"trkb-y816", "plc-gamma", "trkb-y515", "shc", "erk"} <= downstream(self.graph, "ntrk2"))
        self.assertIn("plc-gamma", downstream(self.graph, "trkb-y816"))
        self.assertIn("shc", downstream(self.graph, "trkb-y515"))
        self.assertNotIn("shc", downstream(self.graph, "trkb-y816"))
        self.assertNotIn("plc-gamma", downstream(self.graph, "trkb-y515"))
        modalities = {assay["modality"] for assay in graph["assays"].values()}
        self.assertTrue({"FRET", "BRET", "Single fluorophore"} <= modalities)

    def test_registry_classification(self):
        validate_registry(self.registry)
        receptors = {receptor["id"]: receptor for receptor in self.registry["receptors"]}
        self.assertEqual(collections.Counter(receptor["family"] for receptor in receptors.values()), {"GPCR": 823, "RTK": 58})
        self.assertEqual(len({receptor["subfamily"] for receptor in receptors.values() if receptor["family"] == "RTK"}), 20)
        for ident in ("htr2a", "or51e2", "rho", "rgr", "tas1r2", "tas2r38", "gpr157"):
            self.assertEqual(receptors[ident]["family"], "GPCR", ident)
        for ident in ("ntrk2", "erbb3", "epha10", "ptk7", "ryk", "styk1"):
            self.assertEqual(receptors[ident]["family"], "RTK", ident)

    def test_offline_html_contains_exact_versioned_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "atlas.html"
            build(ROOT, output)
            page = output.read_text(encoding="utf-8")
        payload = re.search(r'<script type="application/json" id="atlas-data">(.*?)</script>', page, re.S)
        self.assertIsNotNone(payload)
        self.assertEqual(json.loads(payload.group(1)), {"graph": self.graph, "registry": self.registry,
                                                        "history": read(ROOT / "data/changelog.json")})
        self.assertNotRegex(page, r"<(?:script\b[^>]*\bsrc\s*=|link\b[^>]*\brel=[\"']stylesheet)")
        self.assertNotIn("ATLAS_DATA", page)

    def test_record_text_cannot_escape_inline_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            graph = deepcopy(self.graph)
            title = "</script><script id=injected>alert(1)</script>"
            graph["papers"][0]["title"] = title
            write(repo / "data/pilot.json", graph)
            write(repo / "data/receptors.json", self.registry)
            write(repo / "data/changelog.json", [])
            (repo / "web").symlink_to(ROOT / "web", target_is_directory=True)
            output = repo / "atlas.html"
            build(repo, output)
            page = output.read_text(encoding="utf-8")
        self.assertNotIn(title, page)
        payload = re.search(r'<script type="application/json" id="atlas-data">(.*?)</script>', page, re.S)
        self.assertEqual(json.loads(payload.group(1))["graph"]["papers"][0]["title"], title)


if __name__ == "__main__":
    unittest.main()
