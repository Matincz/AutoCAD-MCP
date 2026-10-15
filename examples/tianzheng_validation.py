"""Repeatable T30 validation in a newly created drawing over Streamable HTTP.

Registration, tested geometry and unavailable recipes are separate outcomes.
This runner never writes the document that was active when it connected.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ALIGNMENTS = ["ALXLEFT", "ALXRIGHT", "ALXMID", "ALYTOP", "ALYBOTTOM", "ALYMID"]


async def validate(url: str, output: Path) -> None:
    report = {"all_functional_commands_verified": False, "registration": [], "functional": [],
              "environment": "Live installed T30 defaults; other modes remain unverified"}
    target = None
    started = time.perf_counter()

    def persist():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async with streamablehttp_client(url, sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                value = response.structuredContent
                if value is None:
                    value = json.loads(next(c.text for c in response.content if c.type == "text"))
                if not value.get("ok"):
                    report["stopped_error"] = value
                    persist()
                    # Never retry CAD crashes, ambiguous instances or timeouts.
                    raise RuntimeError(json.dumps(value, ensure_ascii=False))
                return value["payload"]

            async def context():
                ctx = await call("transaction", {"operation": "context"})
                if ctx["doc_id"] != target or ctx["active_doc_id"] != target:
                    raise RuntimeError("Active document changed; validation stopped before mutation")
                return ctx

            async def mutate(tool, operation, **kwargs):
                ctx = await context()
                return await call(tool, {"operation": operation, "doc_id": target,
                    "expected_revision": ctx["revision"], "worker_generation": ctx["worker_generation"],
                    "lease_token": ctx.get("lease_token"), "idempotency_key": "t30-validation-" + uuid.uuid4().hex,
                    **kwargs})

            ready = await call("system", {"operation": "ensure_ready"})
            if not ready.get("ready") or not ready["dispatcher"]["loaded"]:
                raise RuntimeError("AutoCAD and dispatcher must be ready")
            created = await call("transaction", {"operation": "create",
                "idempotency_key": "t30-validation-" + uuid.uuid4().hex,
                "data": {"name": "T30-validation"}})
            target = created["doc_id"]
            report["document"] = created
            await context()
            empty = await call("tianzheng", {"operation": "inspect", "doc_id": target})
            if empty["entity_count"] or empty["command_active"]:
                raise RuntimeError("Created document is not blank and idle")
            catalogue = []
            for offset in (0, 200, 400):
                page = await call("tianzheng", {"operation": "catalogue", "data": {"offset": offset, "limit": 200}})
                catalogue.extend(page["commands"])
                registry = await call("tianzheng", {"operation": "registry", "doc_id": target,
                    "data": {"offset": offset, "limit": 200}})
                report["registration"].extend(registry["commands"])
            persist()

            async def execute(command, data):
                before = time.perf_counter()
                result = await mutate("tianzheng", "execute", data={"command": command, **data})
                passed = bool(result.get("completed") and result.get("geometry_verified"))
                report["functional"].append({"command": command, "passed": passed,
                    "seconds": round(time.perf_counter() - before, 3), "result": result})
                persist()
                if not passed:
                    raise RuntimeError("Geometry verification failed; do not resend")
                print(f"{command}: verified", flush=True)
                return result

            for index, command in enumerate(ALIGNMENTS):
                base = index * 2000
                before = await call("tianzheng", {"operation": "inspect", "doc_id": target})
                old = {e["handle"] for e in before["entities"]}
                await mutate("entity", "create_rectangle", x1=base, y1=0, x2=base+200, y2=100)
                await mutate("entity", "create_rectangle", x1=base+500, y1=400, x2=base+800, y2=600)
                after = await call("tianzheng", {"operation": "inspect", "doc_id": target})
                handles = [e["handle"] for e in after["entities"] if e["handle"] not in old]
                if len(handles) != 2:
                    raise RuntimeError("Alignment fixtures missing")
                await execute(command, {"selection_handles": handles})

            points = [[23000,0,0],[27000,0,0],[30000,3000,0],[30000,7000,0],
                      [27000,10000,0],[23000,10000,0],[20000,7000,0],[20000,3000,0],[23000,0,0]]
            walls = await execute("TGWall", {"points": points})
            if len(walls["created_objects"]) != 8:
                raise RuntimeError("Expected eight real T30 walls")
            top = next(e["handle"] for e in walls["created_objects"]
                       if e["bounds"]["min"][1] > 9800 and e["bounds"]["max"][1] < 10200)
            door = await execute("TOpening", {"points": [[25000,0,0]]})
            if door["created_objects"][0].get("opening_kind") != "门":
                raise RuntimeError("Current opening defaults did not create a door")
            await execute("TBanWin", {"points": [[24000,10000,0],[26000,10000,0]], "wall_handles": [top]})
            final = await call("tianzheng", {"operation": "inspect", "doc_id": target})
            if final["entity_count"] != 22 or final["command_active"]:
                raise RuntimeError("Final count or command state differs")
            report["final_inspect"] = final
            verified = {entry["command"].upper() for entry in report["functional"] if entry["passed"]}
            report["not_functionally_tested"] = [entry["command"] for entry in catalogue if entry["command"].upper() not in verified]
            ctx = await context()
            report["saved"] = await call("drawing", {"operation": "save", "data": {
                "path": created["actual_path"], "doc_id": target, "expected_revision": ctx["revision"],
                "worker_generation": ctx["worker_generation"], "lease_token": ctx.get("lease_token"),
                "idempotency_key": "t30-validation-" + uuid.uuid4().hex}})
            report["seconds"] = round(time.perf_counter() - started, 3)
            report["implemented_recipes_verified"] = True
            persist()
            print(f"Verified {len(verified)} recipes; {len(report['not_functionally_tested'])} commands still pending.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(validate(args.url, args.output))


if __name__ == "__main__":
    main()
