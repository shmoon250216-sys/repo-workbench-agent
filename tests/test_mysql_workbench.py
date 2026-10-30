"""Run the existing HTTP/login/SSE/approval workflow contract on real MySQL."""

import os
import unittest
from unittest.mock import patch

import test_workbench as workbench_suite
from test_storage import MYSQL_URL


@unittest.skipUnless(MYSQL_URL, "Real MySQL URL not configured")
class MySQLWorkbenchTests(workbench_suite.WorkbenchTests):
    def setUp(self):
        with patch.dict(os.environ, {"REPO_AGENT_DATABASE_URL": MYSQL_URL}):
            super().setUp()
