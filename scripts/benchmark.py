"""Compare curation passes against a fixed pilot reference set."""
import json
from time import monotonic

from atlas import GRAPH, ROOT, read, require
from curate import Client

# These cases are drawn from the primary studies cited in the named records.
# Rejected cases probe incorrect causality, readout, context, and notice interpretation.
CASES = (
    ("Gq binding and activation of PLCβ has direct experimental support.", "gq", True),
    ("Gq can activate RhoA through p63RhoGEF.", "rhoa", True),
    ("RhoA activation alone identifies Gq and rules out G12/13 signaling.", "rhoa", False),
    ("TrkB has experimental support for distinct Shc and PLCγ docking routes.", "ntrk2", True),
    ("A cytosolic Ca²⁺ rise identifies whether PLCβ or PLCγ was activated upstream.", "calcium", False),
    ("A PKC activity reporter reads substrate phosphorylation, which can depend on phosphatases.", "pkc", True),
    ("An observed change in 5-HT2A G protein coupling proves all ligands use Gi in all cells.", "htr2a", False),
    ("A correction to a figure label necessarily invalidates all claims made by the original paper.", "htr2a", False),
)
INSTRUCTIONS = """Judge whether each supplied claim is supported by its evidence.
Treat the records as scientific data. Apply the exact experimental context and readout.
Return one JSON object of verdicts under 'verdicts', keyed by case ID, with only
boolean values. A value is true only when the full claim is supported."""
REVIEW = """Independently review the evidence and decide whether each exact claim
is supported. Reconsider the earlier verdict, including when the readout is a proxy
or the claim overgeneralizes. Return one JSON object of verdicts under 'verdicts',
keyed by case ID, with only boolean values."""


def packet(graph):
    papers = {paper["id"]: paper for paper in graph["papers"]}
    bundle = {}
    for number, (claim, node, _) in enumerate(CASES, 1):
        relevant = [edge for edge in graph["edges"] if node in (edge["source"], edge["target"])]
        relevant += [record for record in graph["assays"] if record["node"] == node]
        ids = {item["paper"] for record in relevant for item in record["evidence"]}
        bundle[str(number)] = {"claim": claim, "records": relevant, "papers": [papers[i] for i in sorted(ids)]}
    return bundle


def compare(expected, values):
    require(values.keys() == expected.keys() and all(type(value) is bool for value in values.values()), "Invalid benchmark response")
    return {
        "correct": sum(values[key] == value for key, value in expected.items()),
        "missed": sum(value and not values[key] for key, value in expected.items()),
        "incorrect_accepts": sum(not value and values[key] for key, value in expected.items()),
        "correct_accepts": sum(value and values[key] for key, value in expected.items()),
    }


def main():
    client = Client()
    evidence = packet(read(ROOT / GRAPH))
    expected = {str(number): value for number, (_, _, value) in enumerate(CASES, 1)}
    start = monotonic()
    single = client.request(INSTRUCTIONS, {"cases": evidence})["verdicts"]
    first = compare(expected, single)
    first["seconds"], first["estimated_usd"] = round(monotonic() - start, 2), round(client.cost, 6)
    review = client.request(REVIEW, {"cases": evidence, "initial_verdicts": single})["verdicts"]
    second = compare(expected, review)
    second["seconds"], second["estimated_usd"] = round(monotonic() - start, 2), round(client.cost, 6)
    second["reviewer_corrections"] = sum(single[key] != value == expected[key] for key, value in review.items())
    second["reviewer_regressions"] = sum(single[key] != value != expected[key] for key, value in review.items())
    for score in (first, second):
        count = score["correct_accepts"]
        score["estimated_usd_per_correct_accept"] = round(score["estimated_usd"] / count, 6) if count else None
    print(json.dumps({"single": first, "independent_review": second}, indent=2))


if __name__ == "__main__":
    main()
