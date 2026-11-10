#!/usr/bin/env python3
"""Refresh the human receptor inventory from GPCRdb and HGNC snapshots."""

import argparse
import csv
import hashlib
import html
import io
import json
import re
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen


GPCRDB = "GPCRdb"
HGNC = "HGNC"
RTK_CLASSIFICATION = "Robinson et al. human RTK classification"
GPCR_CLASSES = ("001", "002", "003", "004", "006", "007", "008", "009", "010", "011")
GPCR_URL = "https://gpcrdb.org/services/proteinfamily/proteins/{}/Homo%20sapiens/"
HGNC_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
RTK_FAMILIES = {
    "EGFR / ErbB": "EGFR ERBB2 ERBB3 ERBB4",
    "Insulin receptor": "INSR IGF1R INSRR",
    "PDGFR": "PDGFRA PDGFRB CSF1R KIT FLT3",
    "VEGFR": "FLT1 KDR FLT4",
    "FGFR": "FGFR1 FGFR2 FGFR3 FGFR4",
    "PTK7": "PTK7",
    "Trk": "NTRK1 NTRK2 NTRK3",
    "ROR": "ROR1 ROR2",
    "MuSK": "MUSK",
    "MET": "MET MST1R",
    "TAM": "TYRO3 AXL MERTK",
    "TIE": "TEK TIE1",
    "Eph": "EPHA1 EPHA2 EPHA3 EPHA4 EPHA5 EPHA6 EPHA7 EPHA8 EPHA10 EPHB1 EPHB2 EPHB3 EPHB4 EPHB6",
    "RET": "RET",
    "RYK": "RYK",
    "DDR": "DDR1 DDR2",
    "ROS": "ROS1",
    "Lemur kinase": "LMTK1 LMTK2 LMTK3",
    "ALK / LTK": "ALK LTK",
    "STYK1": "STYK1",
}
DISPLAY_NAMES = {"NTRK1": "TrkA", "NTRK2": "TrkB", "NTRK3": "TrkC"}


def snapshot(name, url, cache):
    path = cache / name if cache else None
    if path and path.exists():
        return path.read_bytes()
    request = Request(url, headers={"User-Agent": "Cell-Signaling atlas receptor inventory"})
    with urlopen(request, timeout=90) as response:
        data = response.read()
    if path:
        path.write_bytes(data)
    return data


def plain(text):
    return html.unescape(re.sub(r"<[^>]*>", "", text)).strip()


def record(gene, label, family, subfamily, source, url):
    symbol = gene["symbol"]
    aliases = [gene["name"]]
    for field in ("alias_symbol", "prev_symbol", "alias_name"):
        aliases.extend(gene[field].split("|"))
    aliases = {alias for alias in aliases if alias and alias.casefold() not in {symbol.casefold(), label.casefold()}}
    return {
        "id": re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-"),
        "label": label,
        "gene": symbol,
        "family": family,
        "subfamily": subfamily,
        "aliases": sorted(aliases, key=lambda value: (value.casefold(), value)),
        "source": source,
        "url": url,
    }


def hgnc_url(gene):
    return f"https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/{gene['hgnc_id']}"


def sensory_group(gene):
    for group in gene["gene_group"].split("|"):
        if group.startswith("Olfactory receptors, family "):
            return group
        if group in {"Opsin receptors", "Taste 1 receptors", "Taste 2 receptors", "Vomeronasal receptors"}:
            return group
    return None


def inventory(cache, retrieved):
    hgnc_data = snapshot("hgnc-complete.tsv", HGNC_URL, cache)
    genes = {
        row["symbol"]: row for row in csv.DictReader(io.StringIO(hgnc_data.decode("utf-8-sig")), delimiter="\t")
        if row["status"] == "Approved"
    }
    accessions = {}
    for row in genes.values():
        for accession in filter(None, row["uniprot_ids"].split("|")):
            accessions.setdefault(accession, []).append(row)

    receptors = {}
    excluded = []
    gpcr_digest = hashlib.sha256()
    for slug in GPCR_CLASSES:
        raw = snapshot(f"gpcr-human-{slug}.json", GPCR_URL.format(slug), cache)
        gpcr_digest.update(raw)
        proteins = json.loads(raw)
        if not proteins or any(protein["species"] != "Homo sapiens" for protein in proteins):
            raise ValueError(f"Expected a nonempty human GPCRdb class {slug}")
        for protein in proteins:
            direct = [genes[symbol] for symbol in protein["genes"] if symbol in genes]
            matched = direct or accessions.get(protein["accession"], [])
            if len(matched) > 1:
                # Shared UniProt records can represent several olfactory loci. The
                # GPCRdb entry name gives its representative gene, and the HGNC
                # sensory pass below indexes the remaining individual loci.
                representative = protein["entry_name"].removesuffix("_human")
                matched = [row for row in matched if row["symbol"].lower() == representative]
            if len(matched) != 1:
                raise ValueError(f"Review GPCRdb to HGNC mapping for {protein['entry_name']}: {len(matched)} matches")
            gene = matched[0]
            symbol = gene["symbol"]
            if gene["locus_group"] != "protein-coding gene":
                excluded.append(symbol)
                continue
            if symbol in receptors:
                raise ValueError(f"Duplicate GPCRdb gene {symbol}")
            receptor_class = plain(protein["receptor_class"])
            subfamily = f"{receptor_class} / {plain(protein['family'])}"
            receptors[symbol] = record(gene, plain(protein["name"]), "GPCR", subfamily, GPCRDB,
                                       f"https://gpcrdb.org/protein/{protein['entry_name']}/")

    supplemented = []
    for symbol, gene in genes.items():
        group = sensory_group(gene)
        if group and symbol not in receptors and gene["locus_group"] == "protein-coding gene":
            receptors[symbol] = record(gene, gene["name"], "GPCR", group, HGNC, hgnc_url(gene))
            supplemented.append(symbol)

    for family, symbols in RTK_FAMILIES.items():
        for symbol in symbols.split():
            gene = genes[symbol]
            if gene["locus_group"] != "protein-coding gene" or symbol in receptors:
                raise ValueError(f"Review RTK roster entry {symbol}")
            receptors[symbol] = record(gene, DISPLAY_NAMES.get(symbol, gene["name"]), "RTK", family,
                                       RTK_CLASSIFICATION, hgnc_url(gene))

    records = sorted(receptors.values(), key=lambda item: (item["family"], item["gene"]))
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("Receptor IDs must be unique")
    return {
        "sources": [
            {"name": GPCRDB, "url": "https://doi.org/10.1093/nar/gkae1065", "release": f"Human REST snapshot retrieved {retrieved}"},
            {"name": HGNC, "url": "https://hgnc.genenames.org/download/", "release": f"Complete set retrieved {retrieved}"},
            {"name": RTK_CLASSIFICATION, "url": "https://pubmed.ncbi.nlm.nih.gov/11114734/", "release": "2000 classification with current HGNC symbols"},
        ],
        "notes": [
            "Inventory entries classify loci. Signaling coverage is established separately by the evidence graph.",
            "GPCRdb data are adapted under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). This inventory keeps human protein-coding entries, strips display markup, and normalizes symbols to HGNC.",
            "HGNC sensory loci without separate GPCRdb entries in the snapshot: " + ", ".join(sorted(supplemented)) + ". HGNC classifies RGR as an opsin, and its listing does not imply demonstrated G-protein signaling.",
            "GPCRdb entries excluded because HGNC does not classify the human locus as protein-coding: " + ", ".join(sorted(excluded)) + ". HGNC classifies TAS2R2 and VN1R5 as protein-coding with gene/pseudogene annotations.",
            "The canonical RTK roster includes atypical or pseudokinase family members such as ERBB3, EPHA10, EPHB6, PTK7, ROR1, ROR2, RYK, and STYK1. FGFRL1 lacks the intracellular kinase domain and falls outside this roster.",
            f"Source SHA-256 digests for GPCRdb class responses concatenated in importer order and for HGNC TSV, respectively: {gpcr_digest.hexdigest()} and {hashlib.sha256(hgnc_data).hexdigest()}.",
        ],
        "receptors": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="Read cached snapshots and save any missing downloads here")
    parser.add_argument("--retrieved", type=date.fromisoformat, default=date.today(), help="Source snapshot date (YYYY-MM-DD)")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/receptors.json")
    args = parser.parse_args()
    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)
    data = inventory(args.cache, args.retrieved.isoformat())
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(data['receptors'])} receptors to {args.output}")


if __name__ == "__main__":
    main()
