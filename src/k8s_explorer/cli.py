import argparse
import asyncio
import json
import logging
import signal
import sys
import threading
from pathlib import Path

import uvicorn

from k8s_explorer.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Community Kubernetes agent explorer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Initialize the explorer schema in its dedicated database")
    sync = sub.add_parser("sync", help="Fetch and index configured repositories")
    sync.add_argument("--catalogue", type=Path)
    sync.add_argument("--limit", type=int)
    sub.add_parser("worker", help="Periodically fetch and index the configured corpus")
    sub.add_parser("serve", help="Serve read-only MCP tools over HTTP")
    sub.add_parser("stdio", help="Serve read-only MCP tools over stdio")
    call = sub.add_parser("call", help="Call a read-only tool and print JSON")
    call.add_argument("tool")
    call.add_argument("--args", default="{}", help="JSON object of tool arguments")
    sub.add_parser("tools", help="Print tool input/output schemas")
    discover = sub.add_parser(
        "discover", help="Discover public GitHub repos by topic; print a YAML catalogue"
    )
    discover.add_argument("--topic", default="k8s-at-home")
    discover.add_argument("--max-pages", type=int, default=5)
    evaluation = sub.add_parser("eval", help="Export and evaluate frozen retrieval corpora")
    actions = evaluation.add_subparsers(dest="eval_action", required=True)
    export = actions.add_parser("export", help="Read-only export of the latest source-backed corpus")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--repo-id", action="append")
    export.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    validate = actions.add_parser(
        "validate", help="Validate frozen corpus, source judgments, and provider schemas"
    )
    validate.add_argument("--corpus", type=Path, required=True)
    validate.add_argument("--judgments", type=Path, required=True)
    validate.add_argument("--providers", type=Path)
    rebind = actions.add_parser("rebind", help="Rebind unchanged calibration evidence to a new corpus")
    rebind.add_argument("--corpus", type=Path, required=True)
    rebind.add_argument("--judgments", type=Path, required=True)
    rebind.add_argument("--output", type=Path, required=True)
    run = actions.add_parser(
        "run", help="Run BM25, exact dense, and optional reciprocal rank fusion baselines"
    )
    run.add_argument("--corpus", type=Path, required=True)
    run.add_argument("--judgments", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--mode", choices=("lexical", "dense", "hybrid"), default="lexical")
    run.add_argument("--providers", type=Path)
    run.add_argument("--provider", action="append")
    run.add_argument("--confirm-disposable-database", action="store_true")
    run.add_argument("--verify-live-corpus", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    try:
        if args.command == "eval":
            import os

            from k8s_explorer.evaluation import (
                export_corpus,
                rebind_judgments,
                run_evaluation,
                validate_files,
            )
            from k8s_explorer.store import IndexStore

            if args.eval_action == "validate":
                print(json.dumps(validate_files(args.corpus, args.judgments, args.providers)))
            elif args.eval_action == "rebind":
                print(json.dumps(rebind_judgments(args.corpus, args.judgments, args.output)))
            elif args.eval_action == "run":
                live_store = (
                    IndexStore(Settings().database_url.get_secret_value())
                    if args.verify_live_corpus
                    else None
                )
                report = run_evaluation(
                    args.corpus,
                    args.judgments,
                    args.output,
                    mode=args.mode,
                    providers_path=args.providers,
                    provider_ids=args.provider,
                    database_url=os.environ.get("EXPLORER_EVAL_DATABASE_URL"),
                    confirmed=args.confirm_disposable_database,
                    live_store=live_store,
                )
                print(
                    json.dumps(
                        {
                            "corpus_id": report["corpus_id"],
                            "output": str(args.output),
                            "lexical": {k: v for k, v in report["lexical"].items() if k != "queries"},
                            "provider_runs": len(report["providers"]),
                        }
                    )
                )
            else:
                from k8s_explorer.service import Explorer

                explorer = Explorer(Settings())
                print(
                    json.dumps(
                        export_corpus(
                            explorer.store, explorer.corpus, args.output, args.repo_id, args.max_bytes
                        )
                    )
                )
            return
        from k8s_explorer.service import Explorer

        settings = Settings()
        if args.command == "discover":
            import yaml

            from k8s_explorer.catalogue import GitHubCatalogue

            token = settings.github_token
            found = GitHubCatalogue(token.get_secret_value() if token else None).discover(
                args.topic, args.max_pages
            )
            print(
                yaml.safe_dump(
                    {"repositories": [r.model_dump(exclude={"id"}) for r in found["repositories"]]}
                )
            )
            if found["incomplete"]:
                logging.warning("Discovery is partial; increase the page budget or narrow the topic")
            return
        explorer = Explorer(settings)
        if args.command == "init-db":
            explorer.store.initialize()
            print(json.dumps({"status": "initialized"}))
        elif args.command == "sync":
            from k8s_explorer.runtime import sync_catalogue

            result = sync_catalogue(explorer, args.catalogue, args.limit)
            print(json.dumps(result, default=str))
            if result["failed"]:
                sys.exit(1)
        elif args.command == "worker":
            from k8s_explorer.runtime import sync_catalogue

            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            while not stop.is_set():
                try:
                    result = sync_catalogue(explorer)
                    logging.info(
                        "Corpus sync completed: %d repositories, %d failures",
                        len(result["items"]),
                        result["failed"],
                    )
                except Exception as exc:
                    logging.error("Corpus sync failed (%s)", type(exc).__name__)
                stop.wait(explorer.settings.sync_interval)
        elif args.command == "serve":
            from k8s_explorer.http import create_app

            uvicorn.run(create_app(explorer), host=explorer.settings.host, port=explorer.settings.port)
        elif args.command == "stdio":
            explorer.mcp.run(transport="stdio")
        elif args.command == "tools":
            print(json.dumps([t.model_dump(mode="json") for t in asyncio.run(explorer.mcp.list_tools())]))
        else:
            arguments = json.loads(args.args)
            if not isinstance(arguments, dict):
                raise ValueError("--args must be a JSON object")
            print(json.dumps(explorer.call(args.tool, arguments), default=str))
    except Exception as exc:
        print(json.dumps({"error": "Operation failed", "error_type": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
