import io
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import auth
import agent_confirmations
import agent_demo
from agent_demo import BuyCancelPlanner
from agent_runtime import AgentClient, Runtime
from db import connect, get_order, initialize, snapshot
from server import ShopHandler


class AgentOrchestrationTests(unittest.TestCase):
    def setUp(self):
        auth.reset_sessions()
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "orchestration.sqlite3"
        self.env = patch.dict("os.environ", {"SHOP_DB_BACKEND": "sqlite", "SHOP_DB_PATH": str(self.db)})
        self.env.start()
        initialize(self.db)
        agent_confirmations.store.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ShopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.session_file = str(Path(self.temp.name) / "session.json")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        agent_confirmations.store.reset()
        self.env.stop()
        self.temp.cleanup()
        auth.reset_sessions()

    def client(self):
        client = AgentClient(self.base, session_file=self.session_file)
        client.login("customer")
        return client

    def run_demo(self, client, run_id="orchestration-1", approver=None):
        planner = BuyCancelPlanner(query="苹果", quantity=1, reason="编排测试取消")
        report = Runtime(client, planner, approver or (lambda action, confirmation: True),
                         run_id=run_id).run()
        return planner, report

    def order_count(self):
        with closing(connect(self.db)) as connection:
            return connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def test_scripted_demo_end_to_end(self):
        planner, report = self.run_demo(self.client())
        self.assertTrue(report.ok, report.error)
        self.assertEqual(len(report.steps), 10)
        self.assertEqual(report.steps[-1].action.tool, "customer.get_my_order")
        self.assertEqual(get_order(planner.order_id)["status"], "cancelled")
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)
        self.assertEqual(self.order_count(), 1)

    def test_replay_same_session_is_idempotent(self):
        client = self.client()
        first_planner, first = self.run_demo(client, run_id="replay-1")
        second_planner, second = self.run_demo(client, run_id="replay-1")
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        self.assertEqual(first_planner.order_id, second_planner.order_id)
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)

    def test_denied_approval_blocks_commit(self):
        _, report = self.run_demo(self.client(), run_id="denied-1",
                                  approver=lambda action, confirmation: False)
        self.assertFalse(report.ok)
        self.assertIn("APPROVAL_DENIED", {step.error.get("code") for step in report.steps if step.error})
        self.assertEqual(self.order_count(), 0)
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)

    def test_cli_reuses_session_file(self):
        arguments = ["--base", self.base, "--run-id", "cli-replay",
                     "--session-file", self.session_file, "--query", "苹果"]
        with redirect_stdout(io.StringIO()):
            first = agent_demo.main(arguments)
            second = agent_demo.main(arguments)
        self.assertEqual((first, second), (0, 0))
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(snapshot(self.db)["products"][0]["stock"], 24)


if __name__ == "__main__":
    unittest.main()
