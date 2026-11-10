# Cell Signaling Atlas

An offline explorer and evidence base for human GPCR and RTK signaling. The first release curates Gq and TrkB signaling. The receptor inventory is broader than the current set of curated routes.

## Open the atlas

Download the `cell-signaling-atlas` artifact from a passing **Atlas checks** run on `main`, unzip it, and open the HTML in a browser. To build the same file with Python 3.9 or later, run:

```sh
python3 scripts/atlas.py --output dist/cell-signaling-atlas.html
python3 -m unittest discover -s tests -v
```

Select a receptor, signaling hub, or junction. The arrows show activation, inhibition, recruitment, production, depletion, and feedback. Select a junction for readouts and responses. Select a connection for its experiments and the context in which it was tested. The filters retain upstream steps needed to reach a match. Retired connections appear in the revision history.

## Evidence records

`data/pilot.json` contains molecular states, directed connections, assay records, responses, papers, and coverage. Each claim links to a paper and an experimental location. Demonstrated steps, cross-study routes, and hypotheses are labeled separately. A hypothesis records its assumption, missing link, and a discriminating test. Contested and retired connections keep their supporting and conflicting records.

The receptor registry records the pinned source dates, checksums, and classification boundaries. Rebuild instructions are in `scripts/import_receptors.py`. PubMed identifiers and publisher DOIs link to the original literature. The atlas stores original summaries and bibliographic metadata.

## Activate hosted maintenance

1. Open the saved **Cell-Signaling** Codex Cloud environment after the repository has a default branch. Run the pilot validation commands there. Confirm that the environment can reach Europe PMC and Crossref if it is used for literature retrieval.
2. In the repository, create and install a dedicated publisher GitHub App with **Contents** and **Pull requests** write access for this repository. Add `ATLAS_PUBLISHER_APP_ID` as an Actions variable and `ATLAS_PUBLISHER_PRIVATE_KEY` as an Actions secret. The publisher token goes to Git and GitHub commands. The curation model gets research packets without Git credentials or tools.
3. Enable repository auto-merge and protect `main`. Require pull requests, up-to-date branches, and the **Validate atlas** status check from GitHub Actions. Leave human approval optional for passing automated evidence updates. Run the check once on the default branch before selecting it in protection settings.
4. Set the Actions secret `OPENAI_API_KEY` using a dedicated API project. Set `ATLAS_RUN_LIMIT_USD` and the current full-price `ATLAS_INPUT_USD_PER_MTOK` and `ATLAS_OUTPUT_USD_PER_MTOK` rates for the configured model in `scripts/curate.py`. Set a project budget and alerts in the API dashboard. The runner reserves estimated input and maximum output cost before each call and stops when another call would exceed the run limit. Optionally set `ATLAS_CROSSREF_MAILTO` to a public service contact.
5. After the pilot passes, set `ATLAS_ENABLED` to `true` and dispatch **Update atlas from literature** with publication unchecked to verify discovery and the checkpoint. Enable publication for the next manual run and verify review, the exact-head pull request checks, the automatic merge, and a fresh HTML artifact. Scheduled runs start at 10:17 UTC daily while enabled. Set `ATLAS_ENABLED` to `false` to pause new runs.

The public `atlas-state` branch checkpoints candidates and completed review results. It stores summaries and metadata. Source excerpts are re-fetched for the separate curator and reviewer calls and are kept out of the state branch and workflow logs. The workflow processes at most one queued candidate per run. A failed source fetch leaves its previous checkpoint intact. An interrupted review can resume from the saved candidate. A reviewed patch is reapplied and checked against the exact GitHub pull request revision.

Researchers can inspect entries marked `escalated` in the state branch and submit a conventional pull request with additional evidence. Updates that would change the workflow or settings use the maintainer review process. When a retraction removes the sole valid support for a connection, retire the connection from the active graph and preserve its history and any shared junctions.

The pilot uses fixture-based checks for addition, contextual narrowing, correction, retraction, and retry behavior. The two API passes are separate model requests with fresh source retrieval for independent review. Live usage, timing, and curation quality require a configured hosted benchmark.
