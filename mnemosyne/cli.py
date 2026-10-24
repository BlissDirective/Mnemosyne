"""Command-line interface — the same operations as the MCP server, for humans,
scripts, and hooks (e.g. run `reindex` after a `git pull`, or `prime` from a
Claude Code SessionStart hook).

    python -m mnemosyne.cli remember "User prefers Python" --type preference
    python -m mnemosyne.cli recall "what language does the user like"
    python -m mnemosyne.cli update <id> --confidence 0.95
    python -m mnemosyne.cli reflect --dry-run
    python -m mnemosyne.cli stats
    python -m mnemosyne.cli reindex [--force]
    python -m mnemosyne.cli prime [query]
    python -m mnemosyne.cli journal [-n 20]
    python -m mnemosyne.cli queue                      # proposals awaiting review
    python -m mnemosyne.cli propose --id <id> | --text "..." [--target canon]
    python -m mnemosyne.cli promote <id> | reject <id> --reason "..."

Runs as the writer in MNEMOSYNE_WRITER (default "owner").
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import MemoryEngine


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mnemosyne", description="Local-first memory engine")
    p.add_argument("--root", default=None, help="Memory home (default: $MNEMOSYNE_HOME or ~/mnemosyne)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remember", help="store a memory")
    r.add_argument("text")
    r.add_argument("--type", default="semantic")
    r.add_argument("--entities", nargs="*", default=[])
    r.add_argument("--tags", nargs="*", default=[])
    r.add_argument("--source", default="cli")
    r.add_argument("--confidence", type=float, default=0.7)
    r.add_argument("--ring", default=None,
                   help="canon | team:<name> | proposed | private:<writer> "
                        "(default: your private ring)")

    q = sub.add_parser("recall", help="retrieve memories")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=8)
    q.add_argument("--types", nargs="*", default=[])
    q.add_argument("--include-superseded", action="store_true",
                   help="also show superseded duplicates and expired world_facts")
    q.add_argument("--scope", nargs="*", default=[],
                   help="rings to search (default: canon + yours + your teams; 'all')")

    u = sub.add_parser("update", help="edit a memory in place (preserves id/links/history)")
    u.add_argument("id")
    u.add_argument("--text", default=None, help="new text (re-embeds)")
    u.add_argument("--type", default=None)
    u.add_argument("--entities", nargs="*", default=None, help="replaces the entity list")
    u.add_argument("--tags", nargs="*", default=None, help="replaces the tag list")
    u.add_argument("--confidence", type=float, default=None)
    u.add_argument("--valid-from", default=None, help='ISO date; "" clears it')
    u.add_argument("--valid-to", default=None, help='ISO date; "" clears it')
    u.add_argument("--source", default=None)

    rf = sub.add_parser("reflect", help="consolidate memories")
    rf.add_argument("--dry-run", action="store_true")

    sub.add_parser("stats", help="show statistics")

    ri = sub.add_parser("reindex", help="rebuild derived index from notes")
    ri.add_argument("--force", action="store_true")

    fg = sub.add_parser("forget", help="delete a memory by id")
    fg.add_argument("id")

    pr = sub.add_parser(
        "prime",
        help="print a compact context block for session-start hooks "
             "(Claude Code SessionStart adds this stdout to Claude's context)")
    pr.add_argument("query", nargs="?", default=None,
                    help="optional topic to recall; omitted = best standing context")
    pr.add_argument("-k", type=int, default=10)

    pp = sub.add_parser("propose", help="put a memory up for review")
    pp.add_argument("--id", default=None, help="copy this memory into the queue")
    pp.add_argument("--text", default=None, help="new proposal text (or a correction)")
    pp.add_argument("--type", default=None)
    pp.add_argument("--target", default="canon")

    pm = sub.add_parser("promote", help="reviewer: promote a proposal")
    pm.add_argument("id")
    pm.add_argument("--target", default=None)

    rj = sub.add_parser("reject", help="reviewer: reject a proposal")
    rj.add_argument("id")
    rj.add_argument("--reason", required=True)

    sub.add_parser("queue", help="list proposals awaiting review (oldest first)")

    jr = sub.add_parser("journal", help="show the latest audit-journal events")
    jr.add_argument("-n", type=int, default=20, help="how many events (newest last)")

    args = p.parse_args(argv)

    from .config import load_config

    if args.cmd == "prime":
        # Hook-safe: never block a session on a memory error — print whatever we
        # can to stdout, errors to stderr, and always exit 0.
        try:
            engine = MemoryEngine(load_config(args.root))
            engine.sync()
            print(engine.prime(query=args.query, k=args.k))
            engine.close()
        except BrokenPipeError:
            pass  # consumer (e.g. `| head`) closed stdout early — fine
        except Exception as e:
            print(f"[mnemosyne] prime failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 0

    engine = MemoryEngine(load_config(args.root))
    engine.sync()

    try:
        _dispatch(engine, args)
    except (PermissionError, LookupError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    return 0


def _dispatch(engine: MemoryEngine, args) -> None:
    if args.cmd == "remember":
        mem = engine.remember(args.text, type=args.type, entities=args.entities,
                              tags=args.tags, source=args.source, confidence=args.confidence,
                              ring=args.ring)
        note = " (quarantined for review)" if (
            mem.ring == "proposed" and args.ring != "proposed") else ""
        print(f"stored {mem.id} [{mem.type.value}] ring={mem.ring} "
              f"writer={mem.writer}{note}")
    elif args.cmd == "recall":
        hits = engine.recall(args.query, k=args.k, types=args.types or None,
                             include_superseded=args.include_superseded,
                             scope=args.scope or None)
        if not hits:
            print("(no matches)")
        for h in hits:
            print(f"[{h.memory.type.value}] {h.memory.text}")
            print(f"    id={h.memory.id} ring={h.memory.ring} writer={h.memory.writer} "
                  f"score={h.score:.4f} via={'+'.join(h.strategies)}")
    elif args.cmd == "update":
        mem = engine.update(args.id, text=args.text, type=args.type,
                            entities=args.entities, tags=args.tags,
                            confidence=args.confidence,
                            valid_from=args.valid_from, valid_to=args.valid_to,
                            source=args.source)
        print(f"updated {mem.id} [{mem.type.value}]" if mem else "not found")
    elif args.cmd == "reflect":
        print(json.dumps(engine.reflect(dry_run=args.dry_run).to_json(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))
    elif args.cmd == "reindex":
        print(json.dumps(engine.sync(force=args.force), indent=2))
    elif args.cmd == "forget":
        print("deleted" if engine.forget(args.id) else "not found")
    elif args.cmd == "propose":
        mem = engine.propose(args.id, text=args.text, target=args.target, type=args.type)
        print(f"proposed {mem.id} -> {mem.proposed_target}")
    elif args.cmd == "promote":
        mem = engine.promote(args.id, target=args.target)
        print(f"promoted {mem.id} -> {mem.ring}")
    elif args.cmd == "reject":
        mem = engine.reject(args.id, args.reason)
        print(f"rejected {mem.id} -> {mem.ring}")
    elif args.cmd == "queue":
        queue = engine.pending_proposals()
        if not queue:
            print("(review queue empty)")
        for m in queue:
            print(f"{m.id}  by {m.proposed_by} -> {m.proposed_target}  [{m.type.value}] {m.text}")
    elif args.cmd == "journal":
        events = list(engine.journal.events())
        for ev in events[-args.n:] if args.n > 0 else events:
            print(f"{ev['ts']}  {ev['writer']:<16} {ev['op']:<10} {ev.get('memory_id') or ''}")


if __name__ == "__main__":
    sys.exit(main())
