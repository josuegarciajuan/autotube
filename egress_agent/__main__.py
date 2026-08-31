#!/usr/bin/env python3
"""Punto de entrada del agente egress.

Uso:
    python3 -m egress_agent --config /path/agent_config.json [--port 9101]

O por variables de entorno (AGENT_*). Ver egress_agent/config.py.
"""
from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Egress agent por cuenta Google")
    parser.add_argument("--config", help="JSON con la configuración del agente")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from egress_agent.config import AgentConfig

    if args.config:
        cfg = AgentConfig.from_json(args.config)
    else:
        cfg = AgentConfig.from_env()

    if not cfg.slug:
        print("ERROR: falta slug (--config o AGENT_SLUG)", file=sys.stderr)
        return 2

    host = args.host or cfg.host
    port = args.port or cfg.port

    import uvicorn
    from egress_agent.server import create_app

    app = create_app(cfg)
    print(f"[egress-agent] {cfg.slug} en http://{host}:{port} "
          f"(egress_label={cfg.egress_label or 'sin-etiqueta'})")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
