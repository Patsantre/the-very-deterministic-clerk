import unittest

from ecom_bootstrap import BootstrapKit, build_bootstrap_commands


class Req_Tree:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Req_Exec:
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


if __name__ == "__main__":
    unittest.main()
