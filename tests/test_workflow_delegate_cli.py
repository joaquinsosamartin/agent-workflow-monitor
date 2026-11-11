"""Unit tests for delegation CLI. No child process or network is executed."""
import argparse
import unittest
from unittest.mock import MagicMock, patch

from monitor.workflow_delegate_cli import delegate


class DelegateCliTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(
            plan="plan1",phase="review",provider="claude",agent="qa",
            orchestrator_provider="codex",orchestrator_session="codex-session",
            session=None,port=8765,
            command=["--","claude","-p","Private prompt must not be transmitted"],
        )

    @patch("monitor.workflow_delegate_cli.local_key",return_value="local-test-key")
    @patch("monitor.workflow_delegate_cli.send")
    @patch("monitor.workflow_delegate_cli.subprocess.Popen")
    def test_reports_pid_and_exit_code_without_transmitting_prompt(
        self, mock_popen, mock_send, _mock_key
    ):
        child=MagicMock()
        child.pid=5100
        child.wait.return_value=0
        mock_popen.return_value=child
        self.assertEqual(delegate(self.args()),0)
        self.assertEqual(mock_send.call_count,2)
        first=mock_send.call_args_list[0].args
        second=mock_send.call_args_list[1].args
        self.assertEqual(first[2]["provider"],"claude")
        self.assertEqual(first[2]["orchestrator_provider"],"codex")
        self.assertEqual(first[2]["pid"],5100)
        self.assertEqual(second[2]["exit_code"],0)
        self.assertNotIn("Private prompt",str(first)+str(second))
        mock_popen.assert_called_once_with([
            "claude","-p","Private prompt must not be transmitted"
        ])

    @patch("monitor.workflow_delegate_cli.local_key",return_value="local-test-key")
    @patch("monitor.workflow_delegate_cli.send",side_effect=ConnectionError("offline"))
    @patch("monitor.workflow_delegate_cli.subprocess.Popen")
    def test_unavailable_monitor_does_not_leave_untracked_child(
        self,mock_popen,_mock_send,_mock_key
    ):
        child=MagicMock()
        child.pid=5100
        child.poll.return_value=None
        child.wait.return_value=1
        mock_popen.return_value=child
        with self.assertRaises(ConnectionError):
            delegate(self.args())
        child.terminate.assert_called_once()


if __name__=="__main__":
    unittest.main()
