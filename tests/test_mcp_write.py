"""Task AG.5: запись через MCP (сессия SDK в памяти, настоящий svn через file:///): планы не меняют данные, применение
без согласия невозможно, долгая операция уходит в фон и отвечает job_status, лимит одновременных вызовов."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:
    import mcp.types                                    # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

from svnhelper import mcp_server                                                                   # noqa: E402
from svnhelper.agent_policy import MODE_CONFIRM, MODE_FULL                                         # noqa: E402
from svnhelper.ops import jobrun, make_context                                                     # noqa: E402
from svnhelper.ops.result import Result                                                            # noqa: E402
from svnhelper.svn_core import SvnClient                                                           # noqa: E402
from test_ops_write import HAVE_SVN, NAME, WriteOpsBase, policy                                   # noqa: E402


def parse(result) -> dict:
    out = json.loads(result.content[0].text)
    assert out == result.structuredContent
    return out


@unittest.skipUnless(HAVE_MCP and HAVE_SVN, "нет пакета mcp или svn")
class McpWriteTests(WriteOpsBase, unittest.IsolatedAsyncioTestCase):
    def session(self, ctx=None):
        from mcp.shared.memory import create_connected_server_and_client_session
        return create_connected_server_and_client_session(mcp_server.build_server(ctx or self.ctx))

    async def test_plan_changes_nothing_and_apply_needs_the_token(self):
        (self.mine / "из mcp.txt").write_text("x", encoding="utf-8")
        before = self.head()
        async with self.session() as client:
            plan = parse(await client.call_tool("save_plan", {"project": NAME, "message": "Сохранение через MCP"}))
            self.assertTrue(plan["ok"], plan)
            self.assertEqual(self.head(), before)
            refused = parse(await client.call_tool("save_apply", {"project": NAME, "message": "Сохранение через MCP"}))
            self.assertEqual((refused["ok"], refused["error"]["exit_code"]), (False, 6))
            self.assertEqual(self.head(), before)
            done = parse(await client.call_tool("save_apply", {"project": NAME, "message": "Сохранение через MCP",
                                                               "token": plan["data"]["plan_id"]}))
            self.assertTrue(done["ok"], done)
            self.assertEqual(done["data"]["number"], before + 1)
        self.assertEqual(self.head(), before + 1)

    async def test_read_only_policy_blocks_apply_and_plan_over_mcp(self):
        from svnhelper.agent_policy import Policy, SIG_OK
        ctx = self.use(Policy(file_mode="read_only", ceiling="write", signature=SIG_OK))
        (self.mine / "из mcp.txt").write_text("x", encoding="utf-8")
        async with self.session(ctx) as client:
            for tool in ("save_plan", "save_apply"):
                result = await client.call_tool(tool, {"project": NAME, "message": "Сохранение через MCP"})
                self.assertTrue(result.isError, tool)
                self.assertEqual(parse(result)["error"]["exit_code"], 6)

    async def test_full_mode_applies_in_one_call(self):
        ctx = self.use(policy(MODE_FULL))
        (self.mine / "из mcp.txt").write_text("x", encoding="utf-8")
        before = self.head()
        async with self.session(ctx) as client:
            done = parse(await client.call_tool("save_apply", {"project": NAME, "message": "Сохранение без токена"}))
        self.assertTrue(done["ok"], done)
        self.assertEqual(self.head(), before + 1)

    async def test_list_commands_tool_and_unknown_tool(self):
        async with self.session() as client:
            catalog = parse(await client.call_tool("list_commands", {"markdown": True, "project": NAME}))
            bad = await client.call_tool("save", {"project": NAME})
        self.assertTrue(catalog["ok"])
        names = {item["tool"] for item in catalog["data"]["commands"]}
        self.assertIn("save_apply", names)
        self.assertIn(f'"{NAME}"', catalog["data"]["markdown"])
        self.assertTrue(bad.isError)


class JobBoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_job_returns_result_and_slow_job_runs_in_background(self):
        board = jobrun.JobBoard()
        fast = await board.run("save_apply", lambda: Result("save_apply", data={"number": 5}), wait=2)
        self.assertEqual(fast.data["number"], 5)
        release = asyncio.Event()
        import threading
        gate = threading.Event()

        def slow():
            gate.wait(5)
            return Result("save_apply", data={"number": 7})

        running = await board.run("save_apply", slow, wait=0.3)
        self.assertEqual(running.data["state"], "running")
        job_id = running.data["job_id"]
        self.assertEqual(board.status(job_id).data["state"], "running")
        gate.set()
        for _ in range(50):
            if board.get(job_id).done.is_set():
                break
            await asyncio.sleep(0.05)
        self.assertEqual(board.status(job_id).data["number"], 7)
        unknown = board.status("нет такого")
        self.assertFalse(unknown.ok)
        del release

    async def test_too_many_running_jobs_are_refused(self):
        import threading
        board = jobrun.JobBoard()
        gate = threading.Event()
        self.addCleanup(gate.set)
        for _ in range(jobrun.MAX_RUNNING):
            board.start("x", lambda: (gate.wait(5), Result("x"))[1])
        from svnhelper.ops.errors import OpsError
        with self.assertRaises(OpsError):
            board.start("x", lambda: Result("x"))

    async def test_gate_limits_parallel_read_calls(self):
        self.assertEqual(mcp_server.MAX_CONCURRENT, 4)


if __name__ == "__main__":
    unittest.main()
