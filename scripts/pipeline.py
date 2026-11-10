"""Discover, checkpoint, independently review, and publish one candidate."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from atlas import GRAPH, Invalid, ROOT, apply_proposal, digest, read, require, write
from curate import Client, assess, warn
from discover import DiscoveryError, discover, hydrate

STATE_REF = "refs/heads/atlas-state"
PROPOSAL_PREFIX = "atlas/proposal-"
LOG = Path("data/changelog.json")
STATE = Path(".atlas-local/state.json")


def command(*argv, input_text=None, timeout=None):
    result = subprocess.run(argv, cwd=ROOT, input=input_text, text=True, capture_output=True,
                            check=True, timeout=timeout)
    return result.stdout.strip()


def git(*argv, input_text=None):
    return command("git", *argv, input_text=input_text)


def load_state(path):
    if path.exists():
        return read(path)
    return {"checkpoint": {}, "queue": {}}


def fetch_state(path):
    state = git("ls-remote", "--heads", "origin", STATE_REF)
    if not state:
        write(path, {"checkpoint": {}, "queue": {}})
        return
    revision = state.split()[0]
    git("fetch", "--no-tags", "origin", STATE_REF)
    write(path, json.loads(git("show", f"{revision}:state.json")))


def store_state(path):
    previous = git("ls-remote", "--heads", "origin", STATE_REF)
    if previous:
        revision = previous.split()[0]
        git("fetch", "--no-tags", "origin", STATE_REF)
        if read(path) == json.loads(git("show", f"{revision}:state.json")):
            return
    blob = git("hash-object", "-w", str(path.resolve()))
    tree = git("mktree", input_text=f"100644 blob {blob}\tstate.json\n")
    parents = ("-p", revision) if previous else ()
    commit = git("commit-tree", tree, *parents, "-m", "Checkpoint atlas literature queue")
    git("push", "origin", f"{commit}:{STATE_REF}")


def collect(graph, state):
    for entry in read(ROOT / LOG):
        ident = entry["candidate"]
        row = state["queue"].get(ident, {})
        published = {"id": ident, "kind": row.get("candidate", {}).get("kind", "publication"),
                     "aliases": row.get("candidate", {}).get("aliases", [])}
        state["queue"][ident] = {"candidate": published, "status": "published"}
    known = {alias: ident for ident, row in state["queue"].items()
             if row["candidate"].get("kind") == "publication"
             for alias in row["candidate"].get("aliases", [])}
    result = discover(graph, state["checkpoint"])
    for candidate in result["candidates"]:
        public = {key: candidate[key] for key in ("id", "kind", "paper", "aliases", "related")}
        allowed = {"type", "affected", "requires_review", "pmcid", "source", "europe_pmc_id",
                   "europe_pmc_source", "non_primary", "published_version", "notice_date"}
        public["metadata"] = {key: value for key, value in candidate.get("metadata", {}).items() if key in allowed}
        ident = candidate["id"]
        if public["kind"] == "publication":
            # PMID-only records can acquire a DOI after a prior poll.
            existing = {known[alias] for alias in public["aliases"] if alias in known}
            if len(existing) > 1:
                for match in existing:
                    if state["queue"][match]["status"] == "pending":
                        state["queue"][match].update(status="escalated", reason="Bibliographic identities need reconciliation")
                continue
            if existing:
                ident = next(iter(existing))
                public["id"] = ident
            known.update({alias: ident for alias in public["aliases"]})
        status = "screened" if public["metadata"].get("non_primary") else "pending"
        row = state["queue"].setdefault(ident, {"candidate": public, "status": status})
        if row["status"] == "pending":
            row["candidate"] = public
    state["checkpoint"] = result["checkpoint"]


def select(state):
    actionable = [value for value in state["queue"].values() if value["status"] in {"pending", "reviewed"}]
    return min(actionable, key=lambda row: (row["status"] != "reviewed", row["candidate"]["kind"] != "notice"), default=None)


def curate(graph, state):
    row = select(state)
    if row is None or row["status"] == "reviewed":
        return
    def reread(candidate):
        return hydrate(candidate, graph=graph)
    result = assess(graph, reread(row["candidate"]), Client(), reread)
    if result["status"] == "reviewed":
        try:
            apply_proposal(graph, result["proposal"], result["review"], row["candidate"])
        except (Invalid, TypeError, KeyError):
            result = {"status": "escalated", "reason": "The reviewed proposal failed evidence or graph validation"}
        else:
            result["base"] = digest(graph)
    row.update(result)


def current_pr(branch):
    pulls = json.loads(command("gh", "pr", "list", "--state", "all", "--head", branch,
                               "--base", "main", "--json", "number,state,headRefOid", "--limit", "1"))
    return pulls[0] if pulls else None


def prepare_publication(graph, state):
    row = select(state)
    if row is None or row["status"] != "reviewed":
        return None
    ident = row["candidate"]["id"]
    log = read(ROOT / LOG)
    if any(record["candidate"] == ident for record in log):
        candidate = row["candidate"]
        row.clear()
        row.update({"candidate": {"id": ident, "kind": candidate["kind"], "aliases": candidate["aliases"]},
                    "status": "published"})
        return None
    if row["base"] != digest(graph):
        candidate = row["candidate"]
        row.clear()
        row.update({"candidate": candidate, "status": "pending", "reason": "The evidence base changed after review"})
        return None
    graph_after = apply_proposal(graph, row["proposal"], row["review"], row["candidate"])
    # Name the branch for the specific base and reviewed content.
    revision = digest([row["base"], row["review"]["proposal"]])
    return row, PROPOSAL_PREFIX + revision[:24], graph_after, log


def publish(graph, state):
    prepared = prepare_publication(graph, state)
    if prepared is None:
        return
    row, branch, graph_after, log = prepared
    ident = row["candidate"]["id"]
    require(git("rev-parse", "HEAD") == git("ls-remote", "origin", "refs/heads/main").split()[0], "Main changed during publication")
    pr = current_pr(branch)
    if pr and pr["state"] != "OPEN":
        row.update({"status": "escalated", "reason": "The publication PR was closed outside the workflow"})
        return
    entry = {
        "candidate": ident,
        "published": datetime.now(timezone.utc).date().isoformat(),
        "summary": row["proposal"]["summary"],
        "changes": {key: [record["id"] for record in records] for key, records in row["proposal"]["changes"].items()},
        "review": row["review"]["reason"],
    }
    remote = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote:
        head = remote.split()[0]
        git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        changed = set(git("diff", "--name-only", "HEAD", head).splitlines())
        prior_log = json.loads(git("show", f"{head}:{LOG.as_posix()}"))
        require(changed == {GRAPH.as_posix(), LOG.as_posix()}
                and git("rev-parse", f"{head}^") == git("rev-parse", "HEAD")
                and json.loads(git("show", f"{head}:{GRAPH.as_posix()}")) == graph_after
                and prior_log[:-1] == log
                and {k: v for k, v in prior_log[-1].items() if k != "published"}
                == {k: v for k, v in entry.items() if k != "published"}, "Staged revision does not match the exact reviewed patch")
    else:
        write(ROOT / GRAPH, graph_after)
        write(ROOT / LOG, log + [entry])
        git("add", "--", str(GRAPH), str(LOG))
        git("commit", "-m", "Apply independently reviewed atlas evidence")
        head = git("rev-parse", "HEAD")
        git("push", "origin", f"{head}:refs/heads/{branch}")
    if not pr:
        command("gh", "pr", "create", "--head", branch, "--base", "main",
                "--title", "Update signaling atlas evidence",
                "--body", "Independently reviewed evidence update. See the graph and changelog diff for sources and affected records.")
        pr = current_pr(branch)
    require(pr and pr["headRefOid"] == head, "Pull request head does not match the reviewed revision")
    # A pending status leaves the proposal checkpointed for the next run.
    command("gh", "pr", "checks", str(pr["number"]), "--required", "--watch", "--interval", "10", timeout=420)
    command("gh", "pr", "merge", str(pr["number"]), "--squash", "--match-head-commit", head)
    print(f"Merged checked revision of pull request #{pr['number']}")


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("stage", choices=("fetch", "discover", "store", "curate", "publish"))
    cli.add_argument("--state", type=Path, default=ROOT / STATE)
    args = cli.parse_args()
    try:
        if args.stage == "fetch":
            fetch_state(args.state)
            return
        if args.stage == "store":
            store_state(args.state)
            return
        state = load_state(args.state)
        graph = read(ROOT / GRAPH)
        if args.stage == "discover":
            collect(graph, state)
        elif args.stage == "curate":
            curate(graph, state)
        else:
            publish(graph, state)
        write(args.state, state)
        counts = {}
        for row in state["queue"].values():
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
        print(json.dumps(counts, sort_keys=True))
    except (OSError, subprocess.SubprocessError, DiscoveryError, ValueError, KeyError, TypeError) as error:
        warn(error)
        sys.exit(1)


if __name__ == "__main__":
    main()
