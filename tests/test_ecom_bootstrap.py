import unittest

from ecom_bootstrap import (
    BootstrapKit,
    build_bootstrap_commands,
    build_bootstrap_preface_commands,
)


class Req_Tree:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Req_Exec:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Req_Read:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class BootstrapTest(unittest.TestCase):
    def test_bootstrap_command_order(self):
        kit = BootstrapKit(
            req_tree=Req_Tree,
            req_exec=Req_Exec,
            format_result=lambda cmd, result: "",
        )

        commands = build_bootstrap_commands(kit)

        self.assertEqual(commands[0].root, "/")
        self.assertEqual(commands[1].root, "/docs")
        self.assertEqual(commands[2].path, "/bin/sql")
        self.assertIn("sqlite_schema", commands[2].stdin)
        self.assertIn("product_variant_properties", commands[3].stdin)
        self.assertEqual(commands[4].path, "/bin/date")
        self.assertEqual(commands[5].path, "/bin/id")

    def test_bootstrap_starts_with_agents_read_candidates(self):
        kit = BootstrapKit(
            req_tree=Req_Tree,
            req_exec=Req_Exec,
            req_read=Req_Read,
            format_result=lambda cmd, result: "",
        )

        commands = build_bootstrap_preface_commands(kit)

        self.assertEqual(commands[0].path, "/AGENTS.md")
        self.assertEqual(commands[1].path, "/AGENTS.MD")


if __name__ == "__main__":
    unittest.main()
